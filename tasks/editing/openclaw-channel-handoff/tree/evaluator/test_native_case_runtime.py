"""Case fixture/client unit acceptance; FakeCluster is never a Candidate."""
from __future__ import annotations
import base64
from dataclasses import replace
import http.client
import json
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from evaluator.case_service import CASES, make_private_facts, write_private_case
from evaluator.connector_world import canonical,digest
from evaluator.native_case_client import Handles
from evaluator.native_case_runtime import NativeCaseRuntime


class UnixConnection(http.client.HTTPConnection):
    def __init__(self,path):
        super().__init__('localhost',timeout=5); self.path=str(path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout); self.sock.connect(self.path)


class FakeCluster:
    """Only an initial-state/RPC recording stub, no handoff implementation."""
    def __init__(self,**kwargs):
        self.ids={'A':'synthetic-gateway-a','B':'synthetic-gateway-b'}
        self.calls=[]; self.health={}; self.closed=False
        self.fail_method=None; self.concurrent=0; self.peak=0
        self.lock=threading.Lock(); self.faults=[]
        self.generations={'A':0,'B':0}
        self.sandboxes={role:SimpleNamespace(namespace={'pid':100+index,
            'start_ticks':500+index,'net_inode':800+index},verify_namespace=lambda:None)
            for index,role in enumerate(('A','B'))}
    def start(self):
        self.health={'A':{'status':'ok'},'B':{'status':'ok'}}
    def rpc(self,gateway,method,params):
        with self.lock:
            self.calls.append((gateway,method,params))
            self.concurrent+=1; self.peak=max(self.peak,self.concurrent)
        try:
            if method==self.fail_method:
                return {'status':'error','response':None,'stderr_tail':'unknown native method '+method}
            value={'ok':True,'task_id':params.get('task_id'),'revision':1,'owner_epoch':1}
            if method in {'handoff.bind','handoff.grant.delegate','handoff.grant.rotate'}:
                value['grant']='synthetic-grant-'+secrets.token_hex(24)
                value.pop('ok')  # Published grant records have no task-mutation ok field.
            if method=='handoff.start': value['capability']='synthetic-capability-'+secrets.token_hex(24)
            if method=='handoff.delivery.reconcile': value['delivery_state']='verified'
            if method=='handoff.owner.acquire': time.sleep(.03)
            return {'status':'ok','method':method,'response':value,'stdout_tail':'','stderr_tail':''}
        finally:
            with self.lock:self.concurrent-=1
    def crash_gateway(self,role):
        self.faults.append(('crash',role));return {'unit_stub':True,'at':time.monotonic()}
    def restart_gateway(self,role):
        self.faults.append(('restart',role));self.generations[role]+=1
    def close(self):self.closed=True;return {'closed':True,'unit_stub':True}


class NativeCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='oc-case-test-')
        self.root=Path(self.temp.name)
        self.runs=[]
        self.mock=patch('evaluator.native_case_runtime.GatewayCluster',FakeCluster)
        self.mock.start()
    def tearDown(self):
        for run in self.runs:
            evidence=run.finish()
            self.assertTrue(evidence['world']['cleanup_complete'])
            self.assertEqual(evidence['client']['active_requests'],0)
            self.assertEqual(evidence['client']['active_connections'],0)
        self.mock.stop(); self.temp.cleanup()
    def create(self,case='test_001',failure=None,initialize=True):
        root=self.root/str(len(self.runs));root.mkdir()
        workspace=root/'workspace';workspace.mkdir()
        run=NativeCaseRuntime(facts=make_private_facts(case),product=root/'product',state=root/'state',
            workspace=workspace,runtime={},output=root,endpoint='synthetic-only',deadline=time.monotonic()+20)
        run.cluster.fail_method=failure
        self.runs.append(run);run.start()
        if initialize:
            status,_=self.client(run)
            self.assertEqual(status,200)
        return run
    def client(self,run,body=None,path=None):
        c=UnixConnection(run.client.socket_path)
        try:
            c.request('GET' if body is None else 'POST',path or ('/native-case/context' if body is None else '/native-case/rpc'),
                body=canonical(body) if body is not None else None,headers={'Content-Type':'application/json'})
            response=c.getresponse();return response.status,json.loads(response.read())
        finally:c.close()
    def request(self,run,method='handoff.inspect',gateway='A',**extra):
        return {'gateway':gateway,'method':method,'params':{'task_id':run.task_id,
            'actor':run.aliases['source_actor'],'capability':run.aliases['task_capability'],**extra}}
    def provider(self,run,role,path,body,key):
        c=UnixConnection(run.world.socket_path(role))
        try:
            c.request('POST',path,body=canonical(body),headers={'Content-Type':'application/json',
                'Authorization':'Bearer '+getattr(run.world.adapters[role],'token',''), 'Idempotency-Key':key})
            response=c.getresponse();raw=response.read()
            return response.status,json.loads(raw)
        finally:c.close()
    def message(self,run,route=None):
        return {'dispatch_id':'unit-dispatch','delivery_id':'unit-delivery','route':route or run.route,
            'intent':'user_result','disclosure':'full','media_type':'text/plain',
            'content_base64':base64.b64encode(run.note).decode(),'content_sha256':digest(run.note)}
    def verified_message(self,run,route=None):
        message=self.message(run,route)
        _,accept=self.provider(run,'channel','/v1/messages',message,message['dispatch_id'])
        body={'dispatch_id':message['dispatch_id'],'delivery_id':message['delivery_id'],
              'provider_receipt_id':accept['provider_receipt_id']}
        self.provider(run,'channel','/v1/receipts',body,message['dispatch_id'])
        end=time.monotonic()+1
        while len(run.world.events)<2 and time.monotonic()<end:time.sleep(.005)
        return message

    def test_eight_cases_create_real_dynamic_assets_and_no_solver_actions(self):
        seen=set()
        for case in CASES:
            run=self.create(case);view=run.public_context()
            self.assertEqual(view['seed_status'],'ready')
            self.assertNotIn(run.task_id,seen);seen.add(run.task_id)
            self.assertTrue(set(method for _,method,_ in run.cluster.calls)<=
                {'handoff.bind','handoff.grant.delegate','handoff.start'})
            self.assertFalse((run.workspace/'agent_result.json').exists())
            self.assertFalse((run.workspace/'run_report.json').exists())
            text=json.dumps(view)
            for secret in (run.facts.task_nonce,run.facts.callback_token,run.facts.oracle_decision):
                self.assertNotIn(secret,text)
            if case=='test_005':
                self.assertEqual(len(run.asset),32771)
                self.assertGreater(len(run.asset),2*16384)
                self.assertEqual(digest(run.public_asset.read_bytes()),run.facts.attachment_sha256)
            if case in {'dev_002','test_004'}:
                start=[p for _,m,p in run.cluster.calls if m=='handoff.start'][0]
                self.assertEqual(start['schema_version'],1)
                self.assertEqual(view['destination_type'],'thread')

    def test_services_and_observer_snapshots_do_not_start_any_owner_lease(self):
        run=self.create('test_005',initialize=False)
        deadline=run.deadline
        for _ in range(3):
            self.assertEqual(run.public_context()['seed_status'],'awaiting_first_context')
        self.assertEqual(json.loads((run.workspace/'case_view.json').read_text())['seed_status'],
                         'awaiting_first_context')
        self.assertEqual(run.cluster.calls,[])
        self.assertIsNone(run.initialization_evidence()['started_monotonic'])
        requested=time.monotonic()
        status,context=self.client(run)
        self.assertEqual(status,200);self.assertEqual(context['seed_status'],'ready')
        self.assertGreaterEqual(run.seed_started_monotonic,requested)
        self.assertEqual(run.deadline,deadline)
        start=[params for _,method,params in run.cluster.calls if method=='handoff.start']
        self.assertEqual(len(start),1);self.assertEqual(start[0]['lease_ms'],60000)

    def test_outer_projection_replacement_does_not_follow_symlink(self):
        # Matches launch_case's pre-existing projection, plus a hostile
        # symlink variation; no protected target bytes may be overwritten.
        root=self.root/'projection';root.mkdir();workspace=root/'workspace';workspace.mkdir()
        protected=root/'protected';protected.write_text('keep exact bytes')
        (workspace/'case_view.json').symlink_to(protected)
        run=NativeCaseRuntime(facts=make_private_facts('test_001'),product=root/'product',
            state=root/'state',workspace=workspace,runtime={},output=root,
            endpoint='synthetic-only',deadline=time.monotonic()+20)
        self.runs.append(run);run.start()
        self.assertFalse((workspace/'case_view.json').is_symlink())
        self.assertEqual(protected.read_text(),'keep exact bytes')
        self.assertEqual(run.cluster.calls,[])

    def test_all_eight_original_lease_lengths_are_preserved_and_never_refreshed(self):
        for case in CASES:
            run=self.create(case);before=list(run.cluster.calls)
            for _ in range(2): self.assertEqual(self.client(run)[0],200)
            self.assertEqual(run.cluster.calls,before)
            start=[params for _,method,params in before if method=='handoff.start']
            self.assertEqual(len(start),1)
            self.assertEqual(start[0]['lease_ms'],1000 if case in {'dev_001','test_001'} else 60000)
            self.assertFalse(run.initialization_evidence()['lease_refreshed_on_later_context'])

    def test_concurrent_first_context_reads_materialize_only_one_native_task(self):
        run=self.create(initialize=False)
        entered,release=threading.Event(),threading.Event()
        original=run.cluster.rpc
        def slow(gateway,method,params):
            if method=='handoff.start':
                entered.set();self.assertTrue(release.wait(3))
            return original(gateway,method,params)
        results=[]
        with patch.object(run.cluster,'rpc',side_effect=slow):
            first=threading.Thread(target=lambda:results.append(self.client(run)))
            second=threading.Thread(target=lambda:results.append(self.client(run)))
            first.start();self.assertTrue(entered.wait(2));second.start()
            release.set();first.join(3);second.join(3)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertEqual([code for code,_ in results],[200,200])
        self.assertEqual([method for _,method,_ in run.cluster.calls].count('handoff.start'),1)
        self.assertEqual(results[0][1]['authorization_handles'],results[1][1]['authorization_handles'])

    def test_unknown_initialization_exception_is_not_retried_or_candidate_zero(self):
        run=self.create(initialize=False)
        with patch.object(run.cluster,'rpc',side_effect=RuntimeError('unit injected unknown outcome')) as rpc:
            self.assertEqual(self.client(run)[0],503)
            self.assertEqual(self.client(run)[0],503)
            self.assertEqual(rpc.call_count,1)
        self.assertEqual(run.seed_status,'initialization_failed')
        self.assertIn('native_context_initialization:RuntimeError',run.infra_errors)
        self.assertEqual(run.client.snapshot()['active_requests'],0)

    def test_rpc_before_context_cannot_initialize_or_select_implicit_operations(self):
        run=self.create(initialize=False)
        request={'gateway':'A','method':'handoff.inspect','params':{'task_id':run.task_id}}
        self.assertEqual(self.client(run,request)[0],409)
        self.assertEqual(run.cluster.calls,[])
        self.assertEqual(self.client(run,None,'/other')[0],404)
        self.assertEqual(run.cluster.calls,[])

    def test_expired_context_never_initializes_or_extends_the_case_budget(self):
        run=self.create(initialize=False);run.deadline=time.monotonic()-1
        self.assertEqual(self.client(run)[0],408)
        self.assertEqual(run.cluster.calls,[])
        self.assertIsNone(run.seed_started_monotonic)

    def test_finishing_without_agent_context_performs_no_solver_or_seed_actions(self):
        run=self.create(initialize=False);evidence=run.finish()
        self.assertEqual(evidence['initial_state_operations'],[])
        self.assertEqual(evidence['seed_status'],'awaiting_first_context')
        self.assertIsNone(evidence['initialization']['started_monotonic'])
        self.assertFalse(evidence['evaluator_authored_agent_artifact'])

    def test_no_unrelated_delegation_prerequisite_for_receipt_case(self):
        run=self.create('test_003',failure='handoff.grant.delegate')
        self.assertEqual(run.seed_status,'ready')
        self.assertEqual([m for _,m,_ in run.cluster.calls],['handoff.bind','handoff.start'])

    def test_native_grant_success_does_not_require_invented_ok_field(self):
        for method in ('handoff.bind','handoff.grant.delegate','handoff.grant.rotate'):
            value={'grant':'synthetic-grant','grant_id':'synthetic-id'}
            self.assertIs(NativeCaseRuntime.value({'status':'ok','method':method,'response':value}),value)
            self.assertIsNone(NativeCaseRuntime.value({'status':'error','method':method,'response':value}))
            self.assertIsNone(NativeCaseRuntime.value({'status':'ok','method':method,'response':{**value,'ok':False}}))
        self.assertIsNone(NativeCaseRuntime.value({'status':'ok','method':'handoff.start','response':{'capability':'synthetic'}}))

    def test_missing_secondary_feature_keeps_main_task_available(self):
        run=self.create('test_002',failure='handoff.grant.delegate')
        self.assertEqual(run.seed_status,'ready_with_candidate_limitations')
        self.assertIn('task_capability',run.aliases)
        self.assertNotIn('reduced_actor',run.aliases)
        self.assertEqual(run.infra_errors,[])

    def test_missing_primary_feature_is_visible_candidate_limitation(self):
        run=self.create(failure='handoff.bind')
        self.assertEqual(run.seed_status,'candidate_feature_unavailable')
        status,view=self.client(run)
        self.assertEqual(status,200);self.assertIn('unknown native method',json.dumps(view['seed_error']))
        self.assertEqual(run.infra_errors,[])
        self.assertFalse((run.workspace/'agent_result.json').exists())

    def test_handles_are_stable_immutable_and_cannot_cross_cases(self):
        first,second=Handles(),Handles()
        old=first.put('raw-secret','source');new=second.put('new-secret','source')
        self.assertNotEqual(old,new)
        self.assertEqual(first.put('raw-secret','source'),old)
        self.assertEqual(first.resolve(old),'raw-secret')
        with self.assertRaises(ValueError):second.resolve(old)
        with self.assertRaises(ValueError):first.put('changed-secret','source')
        with self.assertRaises(ValueError):first.resolve(dict(old,other='override'))

    def test_client_dispatches_only_exact_explicit_native_call(self):
        run=self.create();request=self.request(run,observed_owner_epoch=77,idempotency_key='agent-chosen')
        code,value=self.client(run,request)
        self.assertEqual(code,200);self.assertEqual(value['status'],'ok')
        gateway,method,params=run.cluster.calls[-1]
        self.assertEqual((gateway,method),('A','handoff.inspect'))
        self.assertEqual(params['observed_owner_epoch'],77)
        self.assertEqual(params['idempotency_key'],'agent-chosen')
        self.assertEqual(params['actor'],run.actor_values['source'])
        self.assertEqual(params['capability'],run.capability)
        self.assertEqual(run.rpc_events[-1]['origin'],'agent_client')
        self.assertTrue(value['evidence_reference'].startswith('rpc-'))

    def test_client_rejects_operator_privilege_foreign_handles_and_extra_fields(self):
        run=self.create();other=self.create();before=len(run.cluster.calls)
        request=self.request(run);request['method']='handoff.bind'
        self.assertEqual(self.client(run,request)[0],400)
        request=self.request(run);request['params']['task_id']=other.task_id
        self.assertEqual(self.client(run,request)[0],400)
        request=self.request(run);request['params']['actor']=other.aliases['source_actor']
        self.assertEqual(self.client(run,request)[0],400)
        request=self.request(run);request['target_url']='http://127.0.0.1:9'
        self.assertEqual(self.client(run,request)[0],400)
        self.assertEqual(self.client(run,{},'/stats')[0],404)
        self.assertEqual(len(run.cluster.calls),before)

    def test_agent_selected_batch_is_concurrent_without_parameter_repair(self):
        run=self.create()
        commands=[self.request(run,'handoff.owner.acquire',gateway,expected_revision=39,observed_owner_epoch=7)
                  for gateway in ('A','B')]
        status,values=self.client(run,commands,'/native-case/batch')
        self.assertEqual(status,200);self.assertEqual(len(values),2)
        self.assertEqual(run.cluster.peak,2)
        for _,_,params in run.cluster.calls[-2:]:
            self.assertEqual(params['expected_revision'],39);self.assertEqual(params['observed_owner_epoch'],7)

    def test_rotation_adds_new_handle_without_rebinding_old_authority(self):
        run=self.create('test_002');old=run.aliases['source_actor'];oldvalue=run.handles.resolve(old)
        request=self.request(run,'handoff.delivery.enqueue',delivery_id='selected-by-agent')
        self.assertEqual(self.client(run,request)[0],200)
        new=run.aliases['current_source_actor']
        self.assertNotEqual(old,new);self.assertEqual(run.handles.resolve(old),oldvalue)
        self.assertEqual([m for _,m,_ in run.cluster.calls].count('handoff.grant.rotate'),1)
        self.assertEqual(self.client(run,request)[0],200)
        self.assertEqual([m for _,m,_ in run.cluster.calls].count('handoff.grant.rotate'),1)
        self.assertTrue(any(e['origin']=='environment_grant_rotation' for e in run.rpc_events))

    def test_claimed_verification_without_external_receipt_creates_no_callback(self):
        run=self.create('test_006')
        result={'status':'ok','response':{'ok':True,'delivery_state':'verified'}}
        run.after_agent_call('A','handoff.delivery.reconcile',{'delivery_id':'unit-delivery'},result)
        self.assertIsNone(run.callback)
        self.verified_message(run,dict(run.route,peer_id='foreign'))
        run.after_agent_call('A','handoff.delivery.reconcile',{'delivery_id':'unit-delivery'},result)
        self.assertIsNone(run.callback)

    def test_callback_requires_independent_exact_source_receipt_and_duplicates_are_identical(self):
        run=self.create('test_006');self.verified_message(run)
        result={'status':'ok','response':{'ok':True,'delivery_state':'verified'}}
        run.after_agent_call('A','handoff.delivery.reconcile',{'delivery_id':'unit-delivery'},result)
        self.assertIsNotNone(run.callback)
        self.assertNotIn(run.facts.callback_token,json.dumps(run.callback))
        run.after_agent_call('A','handoff.compact',{}, {'status':'ok','response':{'ok':True}})
        events=run.public_context()['provider_events']
        self.assertEqual(len(events),2);self.assertEqual(events[0],events[1])
        self.assertFalse(any(m=='handoff.interaction.complete' for _,m,_ in run.cluster.calls))

    def origin_event(self,run,gateway='B'):
        return {'sequence':2,'world_instance_id':run.world.instance_id,
            'response_lost_after_native_acceptance':True,
            'gateway_origin':{'verified':True,'gateway_id':run.cluster.ids[gateway],
                'generation':run.cluster.generations[gateway],
                'namespace':dict(run.cluster.sandboxes[gateway].namespace)}}

    def test_case_fault_kills_the_admitted_gateway_synchronously_only_once(self):
        for gateway in ('A','B'):
            run=self.create('test_005');event=self.origin_event(run,gateway)
            run.response_loss('channel',event);self.assertFalse(run.fault_started)
            release=threading.Event()
            original=run.cluster.restart_gateway
            def restart(role):
                release.wait(2);original(role)
            with patch.object(run.cluster,'restart_gateway',side_effect=restart):
                try:
                    run.response_loss('media',event)
                    self.assertEqual(run.cluster.faults,[('crash',gateway)])
                finally:release.set();run.fault_thread.join(timeout=2)
            self.assertEqual(run.cluster.faults,[('crash',gateway),('restart',gateway)])
            run.response_loss('media',event)
            self.assertEqual(len(run.cluster.faults),2)
            self.assertEqual(run.infra_errors,[])

    def test_fault_refuses_missing_foreign_or_stale_admission(self):
        for change in ('missing','foreign_world','foreign_gateway','stale_generation','wrong_namespace'):
            run=self.create('test_005');event=self.origin_event(run)
            if change=='missing':event.pop('gateway_origin')
            if change=='foreign_world':event['world_instance_id']='foreign'
            if change=='foreign_gateway':event['gateway_origin']['gateway_id']='foreign'
            if change=='stale_generation':event['gateway_origin']['generation']=-1
            if change=='wrong_namespace':event['gateway_origin']['namespace']['net_inode']+=1
            with self.assertRaises(RuntimeError):run.response_loss('media',event)
            self.assertFalse(run.fault_started);self.assertEqual(run.cluster.faults,[])

    def test_post_migration_restart_targets_the_agent_selected_gateway(self):
        run=self.create('test_004')
        run.after_agent_call('B','handoff.migrate',{}, {'status':'ok','response':{'ok':True}})
        run.fault_thread.join(timeout=2)
        self.assertEqual(run.cluster.faults,[('crash','B'),('restart','B')])

    def test_existing_private_bundle_is_not_overwritten(self):
        root=self.root/'private';private,_=write_private_case(root,'test_004')
        before=private.read_bytes()
        with self.assertRaises(ValueError):write_private_case(root,'test_004')
        self.assertEqual(private.read_bytes(),before)

    def test_client_exception_cannot_become_an_ordinary_partial_candidate_result(self):
        run=self.create()
        with patch.object(run.cluster,'rpc',side_effect=RuntimeError('synthetic evaluator failure')):
            status,value=self.client(run,self.request(run))
        self.assertEqual(status,200)
        self.assertEqual(value['error'],'evaluator_native_client_error')
        self.assertIn('native_client_dispatch:RuntimeError',run.finish()['infrastructure_errors'])

    def test_service_failure_remains_infrastructure_even_after_complete_cleanup(self):
        run=self.create()
        run.world.failure='synthetic-native-service-failure'
        evidence=run.finish()
        self.assertTrue(evidence['world']['cleanup_complete'])
        self.assertIn('native_connector_world_unverified',evidence['infrastructure_errors'])


if __name__=='__main__':unittest.main()
