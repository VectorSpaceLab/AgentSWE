"""Live handoff task fixtures, not a scenario solver or Result scorer."""
from __future__ import annotations
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from evaluator.case_service import PrivateFacts, CASES, attachment_bytes
from evaluator.connector_world import ConnectorWorld, FaultPlan, canonical, digest
from evaluator.native_case_client import NativeCaseClient, Handles, METHODS, SECRET_FIELDS
from lower_agent.gateway_cluster import GatewayCluster

PLANS = {
    'dev_001': FaultPlan(),
    'dev_002': FaultPlan(media_drop_stages=('chunk:0',),channel_partial_media_first=True),
    'test_001': FaultPlan(),
    'test_002': FaultPlan(),
    'test_003': FaultPlan(channel_receipt_schedule=('empty_platform_id','unknown','verified')),
    'test_004': FaultPlan(),
    'test_005': FaultPlan(media_drop_stages=('chunk:1',),channel_partial_media_first=True),
    'test_006': FaultPlan(interaction_drop_first_acceptance=True),
}
# Evaluator-issued invariant battery (2026-09-20 Result hardening).  The
# battery runs once, inside initial-state materialization, and stops issuing
# probes when less than this many seconds of the case budget remain so that it
# can never consume the lower agent's working window.
PROBE_MIN_REMAINING_SECONDS = 200.0
PROBE_ATTACHMENT = b'openclaw-invariant-probe-attachment'


class NativeCaseRuntime:
    def __init__(self, *, facts, product, state, workspace, runtime, output, endpoint,
                 deadline, product_readonly=False, gateway_token=None, primary_port=None):
        self.facts = facts
        if (facts.case_id not in CASES or facts.bundle_version!='openclaw-native-case-v2'
            or facts.attachment_size not in (0,3073,32771)):
            raise ValueError('unsupported or stale native runtime case bundle')
        self.case_id, self.deadline = facts.case_id, deadline
        self.workspace, self.output = Path(workspace), Path(output)
        self.task_id = 'task-'+digest(facts.task_nonce.encode())[:24]
        self.principal = 'principal-'+digest((facts.task_nonce+':principal').encode())[:20]
        self.session_key = 'agent:main:'+self.task_id
        self.destination = 'thread' if facts.route_thread else 'direct'
        self.channel = {'provider':facts.route_provider,'account_id':facts.route_account,
                        'peer_id':facts.route_thread or facts.route_peer}
        self.route = dict(self.channel,destination_type=self.destination)
        self.asset = attachment_bytes(facts.task_nonce,facts.attachment_size)
        if digest(self.asset)!=facts.attachment_sha256:
            raise ValueError('runtime attachment does not match private case digest')
        self.note = ('Update '+self.task_id+': the requested case result is ready.').encode()
        self.handles = Handles()
        self.aliases, self.actor_values = {}, {}
        self.lock = threading.RLock()
        self.seed_lock = threading.Lock()
        self.services_ready = False
        self.services_ready_monotonic = None
        self.seed_started_monotonic = None
        self.seed_ended_monotonic = None
        self.rpc_events, self.environment_events = [], []
        self.seed_status = 'not_started'
        self.seed_error = None
        self.seed_issues = []
        self.fault_started = False
        self.callback = None
        self.callback_duplicates = 0
        self.rotation_done = False
        self.capability = None
        self.probe_results = {}
        self.probe_error = None
        self.migration_restart_done = False
        self.fault_thread = None
        self.infra_errors = []
        self.closing = False
        self.closed_evidence = None
        self.public_asset = self.workspace/'case-attachment.bin'
        self.public_note = self.workspace/'requested-result.txt'
        for path in (self.public_note,self.public_asset):
            if path.exists() or path.is_symlink():
                raise ValueError('runtime case asset already exists')
        self.public_note.write_bytes(self.note)
        if self.asset:
            self.public_asset.write_bytes(self.asset)
        self.root = self.output/'native-case'
        self.root.mkdir(parents=True,exist_ok=False)
        self.log = (self.root/'rpc-events.jsonl').open('x')
        self.world = None
        self.client = None
        self.cluster = None
        try:
            self.world = ConnectorWorld(root=self.root/'world',deadline=deadline,
                plan=PLANS[self.case_id],response_loss_observer=self.response_loss)
            self.world.private_values.update(v for v in (facts.task_nonce,facts.route_peer,
                facts.route_thread,facts.callback_token) if v)
            self.world.register_callback(facts.callback_token)
            self.client = NativeCaseClient(self,deadline=deadline)
            self.cluster = GatewayCluster(product=product,state=state,workspace=workspace,runtime=runtime,
                output=output,endpoint=endpoint,deadline=deadline,world=self.world,
                product_readonly=product_readonly,case_socket=self.client.socket_path,
                gateway_token=gateway_token,primary_port=primary_port)
        except BaseException:
            if self.client: self.client.close()
            if self.world: self.world.close()
            self.log.close()
            raise

    def private_values(self):
        return list(self.world.private_values)

    def remember(self,value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in SECRET_FIELDS and isinstance(item,str) and item:
                    self.world.private_values.add(item)
                self.remember(item)
        elif isinstance(value,list):
            for item in value: self.remember(item)

    def public_scrub(self,value):
        if isinstance(value,str):
            for secret in tuple(self.world.private_values):
                value=value.replace(secret,'[private]')
            return value
        if isinstance(value,dict):
            return {k:self.public_scrub(v) for k,v in value.items()}
        if isinstance(value,list):
            return [self.public_scrub(v) for v in value]
        return value

    def public_result(self,result):
        value={k:v for k,v in result.items() if k not in {'stdout_tail','stderr_tail'}}
        return self.public_scrub(self.handles.publish(value))

    def record_environment(self,kind,**fields):
        with self.lock:
            event={'event':kind,'at_monotonic':time.monotonic(),**fields}
            self.environment_events.append(self.public_scrub(self.handles.publish(event)))

    def call(self,gateway,method,params,*,origin):
        if time.monotonic()>=self.deadline or self.closing:
            return {'status':'timeout','response':None,'dispatched':False,
                    'error':'case deadline exhausted before native call'}
        started=time.monotonic()
        result=self.cluster.rpc(gateway,method,params)
        with self.world.lock:
            self.remember(result.get('response'))
        if result.get('status')!='ok' and result.get('stderr_tail'):
            result=dict(result,diagnostic=self.public_scrub(str(result['stderr_tail'])[-1200:]))
        clean={k:v for k,v in result.items() if k not in {'stdout_tail','stderr_tail'}}
        with self.lock:
            if self.log.closed:
                self.infra_errors.append('native_rpc_completed_after_evidence_close')
                return {'status':'error','response':None,'error':'native evidence stream closed'}
            number=len(self.rpc_events)+1
            event={'reference':'rpc-'+str(number),'origin':origin,'gateway':gateway,
                   'gateway_id':self.cluster.ids[gateway],'method':method,
                   'request':params,'response':clean,'started_monotonic':started,
                   'ended_monotonic':time.monotonic(),'world_instance_id':self.world.instance_id}
            with self.world.lock:
                event=self.world.redact(event)
            self.log.write(canonical(event).decode()+'\n'); self.log.flush(); os.fsync(self.log.fileno())
            self.rpc_events.append(event)
        return dict(result,evidence_reference='rpc-'+str(number))

    @staticmethod
    def value(result):
        value=result.get('response')
        if result.get('status')=='ok' and isinstance(value,dict):
            if value.get('ok') is True:
                return value
            # Grant APIs return grant records; only task mutations require
            # ok:true in their payload. Gateway CLI has verified RPC success.
            if (result.get('method') in {'handoff.bind','handoff.grant.delegate','handoff.grant.rotate'}
                    and isinstance(value.get('grant'),str) and value['grant']
                    and value.get('ok') is not False):
                return value
        return None

    def seed_call(self,method,params,*,critical=True):
        result=self.call('A',method,params,origin='environment_initial_state')
        value=self.value(result)
        if value is None:
            # A healthy product lacking the requested feature is a Candidate
            # limitation, not a missing evaluator service. Agent still runs
            # and can report partial/failed work honestly.
            issue={'method':method,'result':self.public_result(result)}
            self.seed_issues.append(issue)
            if critical:
                self.seed_status='candidate_feature_unavailable'
                self.seed_error=issue
            return None
        return value

    def start(self):
        if self.seed_status != 'not_started':
            raise RuntimeError('native case services may start only once')
        self.world.start(); self.cluster.start()
        self.services_ready_monotonic = time.monotonic()
        self.services_ready = True
        self.seed_status = 'awaiting_first_context'
        # This bootstrap projection contains no live lease. Starting the native
        # reasoning CLI can be slow; only its first context read materializes
        # the initial task. Neither this snapshot nor evidence reads do so.
        bootstrap = self.root/'bootstrap-context.json'
        with bootstrap.open('x') as stream:
            stream.write(json.dumps(self.public_context(),indent=2)+'\n')
        # The outer dev/hidden launcher may have staged an earlier redacted
        # projection. Replace that evaluator-owned entry atomically, never
        # follow a path a product process could have turned into a symlink.
        os.replace(bootstrap,self.workspace/'case_view.json')
        return self.public_context()

    def context_for_agent(self):
        remaining = self.deadline - time.monotonic()
        if self.closing or remaining <= 0:
            raise TimeoutError('case deadline exhausted before context')
        if not self.seed_lock.acquire(timeout=remaining):
            raise TimeoutError('case deadline exhausted waiting for initial state')
        try:
            if self.closing or time.monotonic() >= self.deadline:
                raise TimeoutError('case deadline exhausted before initial state')
            if not self.services_ready:
                raise RuntimeError('native case services are not ready')
            if self.seed_status == 'awaiting_first_context':
                self.seed_started_monotonic = time.monotonic()
                self.record_environment('initial_state_materialization_started',
                    trigger='first_agent_context', lease_ms=self.initial_lease_ms())
                try:
                    self._seed_initial_state()
                except Exception as exc:
                    # Never retry a partially completed initialization: a
                    # native call may have changed state before it failed.
                    self.seed_status = 'initialization_failed'
                    self.seed_error = {'error_type': type(exc).__name__,
                                       'outcome': 'unknown; not retried'}
                    self.infra_errors.append('native_context_initialization:'+type(exc).__name__)
                finally:
                    self.seed_ended_monotonic = time.monotonic()
                    self.record_environment('initial_state_materialization_finished',
                        seed_status=self.seed_status)
            return self.public_context()
        finally:
            self.seed_lock.release()

    def initial_lease_ms(self):
        # Keep the intentional expired-owner cases and all original lengths.
        return 1000 if self.case_id in {'dev_001','test_001'} else 60000

    def initialization_evidence(self):
        return {'policy':'first-agent-context-v1', 'trigger':'first_agent_context',
                'services_ready_monotonic':self.services_ready_monotonic,
                'started_monotonic':self.seed_started_monotonic,
                'ended_monotonic':self.seed_ended_monotonic,
                'initial_lease_ms':self.initial_lease_ms(),
                'absolute_case_work_deadline_monotonic':self.deadline,
                'lease_refreshed_on_later_context':False}

    def _seed_initial_state(self):
        self.seed_status='initializing'
        all_permissions=['start','append','attach','status','control','effect','result_full','result_summary']
        source=self.seed_call('handoff.bind',{'principal_id':self.principal,'channel':self.channel,
            'permissions':all_permissions,'visibility':'full','idempotency_key':self.task_id+':root',
            'occurred_at_ms':int(time.time()*1000)})
        if source is not None:
            source_actor={'principal_id':self.principal,'channel':self.channel,'grant':source.get('grant')}
            if not isinstance(source_actor['grant'],str):
                self.seed_status='candidate_feature_unavailable'; self.seed_error={'method':'handoff.bind','error':'missing native grant'}
            else:
                self.actor_values['source']=source_actor
                self.aliases['source_actor']=self.handles.put(source_actor,'source-actor')
                if self.case_id in {'dev_001','test_002'}:
                    child_channel=(self.channel if self.case_id=='test_002' else
                                   dict(self.channel,peer_id='observer-'+self.task_id))
                    child=self.seed_call('handoff.grant.delegate',{'actor':source_actor,'channel':child_channel,
                        'permissions':['status','result_summary'],'visibility':'summary',
                        'idempotency_key':self.task_id+':child','occurred_at_ms':int(time.time()*1000)},critical=False)
                    if child is not None and isinstance(child.get('grant'),str):
                        actor={'principal_id':self.principal,'channel':child_channel,'grant':child['grant']}
                        self.actor_values['reduced']=actor
                        self.aliases['reduced_actor']=self.handles.put(actor,'reduced-actor')
                if self.case_id=='test_004':
                    foreign_channel=dict(self.channel,peer_id='foreign-'+self.task_id)
                    foreign=self.seed_call('handoff.bind',{'principal_id':self.principal,'channel':foreign_channel,
                        'permissions':all_permissions,'visibility':'full','idempotency_key':self.task_id+':foreign',
                        'occurred_at_ms':int(time.time()*1000)},critical=False)
                    if foreign is not None and isinstance(foreign.get('grant'),str):
                        actor={'principal_id':self.principal,'channel':foreign_channel,'grant':foreign['grant']}
                        self.actor_values['foreign']=actor
                        self.aliases['foreign_actor']=self.handles.put(actor,'foreign-actor')
                if self.seed_status!='candidate_feature_unavailable':
                    schema=1 if self.case_id in {'dev_002','test_004'} else 2
                    started=self.seed_call('handoff.start',{'actor':source_actor,'task_id':self.task_id,
                        'session_key':self.session_key,'idempotency_key':self.task_id+':start',
                        'event_id':self.task_id+':initial','sequence':1,'occurred_at_ms':int(time.time()*1000),
                        'lease_ms':self.initial_lease_ms(),
                        'schema_version':schema})
                    if started is not None and isinstance(started.get('capability'),str):
                        self.capability=started['capability']
                        self.aliases['task_capability']=self.handles.put(self.capability,'task-capability')
                        self.seed_status='ready_with_candidate_limitations' if self.seed_issues else 'ready'
                        self._probe_battery(source_actor,schema,started)
                    elif started is not None:
                        self.seed_status='candidate_feature_unavailable'
                        self.seed_error={'method':'handoff.start','error':'missing native task capability'}

    def public_context(self):
        with self.lock:
            return {'schema_version':'openclaw-native-context-v1','case_id':self.case_id,
                'task_id':self.task_id,'session_key':self.session_key,'destination_type':self.destination,
                'seed_status':self.seed_status,'seed_error':self.seed_error,'seed_issues':list(self.seed_issues),
                'initial_state_policy':'Materialized once on first native client context read; later reads never renew ownership. All setup remains inside the case deadline.',
                'world_reference':self.world.instance_id if self.world else None,
                'gateways':dict(self.cluster.ids) if self.cluster else {},
                'authorization_handles':dict(self.aliases),
                'requested_result':{'path':str(self.public_note),'sha256':digest(self.note)},
                'attachment':({'path':str(self.public_asset),'size':len(self.asset),'sha256':digest(self.asset),
                               'media_type':'application/octet-stream'} if self.asset else None),
                'provider_events':([copy_event for copy_event in [self.callback] if copy_event is not None]
                    + ([dict(self.callback) for _ in range(min(self.callback_duplicates,8))] if self.callback else [])),
                'observed_environment_events':list(self.environment_events[-64:]),
                'native_methods':sorted(METHODS),'now_ms':int(time.time()*1000),
                'remaining_seconds':max(0,self.deadline-time.monotonic()),
                'client_command':'/usr/bin/python3 -I -B /agentswe/native_client.py',
                'client_contract':{'rpc':{'gateway':'A or B','method':'an allowed native method','params':'exact native fields; opaque values use {$handle: name}'},
                    'batch':'One or two explicitly supplied RPC objects, started concurrently. No fields or recovery steps are supplied for you.',
                    'context':'Re-read to observe new provider events and authorization handles. Never copy secrets or raw response bodies into reports.'},
                'oracle_disclosed':False}

    def response_loss(self,role,event):
        expected = ('media' if self.case_id in {'dev_002','test_005'} else
                    'interaction' if self.case_id=='test_006' else None)
        if role==expected:
            origin=event.get('gateway_origin')
            if (event.get('world_instance_id') != self.world.instance_id
                    or event.get('response_lost_after_native_acceptance') is not True
                    or not isinstance(origin,dict) or origin.get('verified') is not True):
                raise RuntimeError('accepted response loss lacks a case-bound admitted origin')
            gateway=next((key for key,value in self.cluster.ids.items()
                          if value==origin.get('gateway_id')),None)
            if gateway is None:
                raise RuntimeError('accepted response loss came from an unknown Gateway')
            self.start_gateway_fault(gateway,'provider_acceptance_response_loss',
                admitted_origin=origin,service=role,
                provider_event_reference=event.get('sequence'))

    def start_gateway_fault(self,gateway,reason,*,admitted_origin=None,**fields):
        with self.lock:
            if self.fault_started or self.closing:
                return
            sandbox=self.cluster.sandboxes[gateway]
            sandbox.verify_namespace()
            if admitted_origin is not None and (
                    admitted_origin.get('generation') != self.cluster.generations[gateway]
                    or admitted_origin.get('namespace') != sandbox.namespace):
                raise RuntimeError('fault target no longer matches the admitted namespace')
            self.fault_started=True
            self.record_environment('gateway_fault_scheduled',gateway=gateway,
                generation=self.cluster.generations[gateway],reason=reason,**fields)
            # Synchronous: the connector has accepted and fsynced the event,
            # but its handler has not yet returned/closed the lost response.
            # Restart alone is asynchronous; no task action is chosen here.
            crash=self.cluster.crash_gateway(gateway)
            self.record_environment('gateway_crashed',gateway=gateway,
                reason=reason,crash_evidence=crash,admitted_origin=admitted_origin)
            def fault():
                try:
                    if not self.closing and time.monotonic()<self.deadline:
                        self.cluster.restart_gateway(gateway)
                        self.record_environment('gateway_restarted',gateway=gateway,
                            generation=self.cluster.generations[gateway])
                        if self.callback is not None:
                            with self.lock: self.callback_duplicates+=1
                            self.record_environment('late_duplicate_provider_event')
                except Exception as exc:
                    if not self.closing:
                        self.infra_errors.append('owned_gateway_fault_control:'+type(exc).__name__)
            self.fault_thread=threading.Thread(target=fault,daemon=True)
            self.fault_thread.start()

    def after_agent_call(self,gateway,method,params,result):
        if self.closing: return
        value=self.value(result)
        if value is None: return
        if self.case_id=='test_002' and method=='handoff.delivery.enqueue':
            with self.lock:
                rotate=not self.rotation_done
                if rotate: self.rotation_done=True
            if rotate and 'source' in self.actor_values:
                old=self.actor_values['source']
                changed=self.call('A','handoff.grant.rotate',{'actor':old,
                    'idempotency_key':self.task_id+':external-rotation','occurred_at_ms':int(time.time()*1000)},
                    origin='environment_grant_rotation')
                replacement=self.value(changed)
                if replacement and isinstance(replacement.get('grant'),str):
                    actor=dict(old,grant=replacement['grant'])
                    self.actor_values['current_source']=actor
                    self.aliases['current_source_actor']=self.handles.put(actor,'replacement-source-actor')
                    self.record_environment('source_grant_rotated',current_actor=self.aliases['current_source_actor'])
                    # Requirements 2 and 8: the rotated subtree must lose read
                    # authority in the same transaction that replaced it.
                    self._probe('rotated_lineage_read_refused','handoff.status',
                        {'actor':old,'task_id':self.task_id,'capability':self.capability})
                else:
                    self.record_environment('native_grant_rotation_refused',result=self.public_result(changed))
        if self.case_id=='test_004' and method=='handoff.migrate':
            self.start_gateway_fault(gateway,'post_migration_restart')
        # A Candidate's "verified" response alone cannot create a callback.
        # Require a matching independently accepted source message plus an
        # actually emitted receipt with a non-empty platform identity.
        verified_provider = any(e['service']=='channel' and e['path']=='/v1/receipts'
            and isinstance(e.get('request'),dict)
            and e['request'].get('delivery_id')==params.get('delivery_id')
            and isinstance(e.get('response'),dict) and e['response'].get('outcome')=='verified'
            and isinstance(e['response'].get('platform_message_id'),dict)
            and e['response']['platform_message_id'].get('present') is True
            for e in self.world.events)
        accepted_source = any(v['payload'].get('delivery_id')==params.get('delivery_id')
            and v['payload'].get('route')==self.route and v['payload'].get('content_sha256')==digest(self.note)
            for v in self.world.adapters['channel'].accepted.values())
        if (self.case_id in {'dev_001','test_006'} and method=='handoff.delivery.reconcile'
            and value.get('delivery_state')=='verified' and verified_provider and accepted_source):
            with self.lock:
                if self.callback is None:
                    content=('User acknowledged '+self.task_id).encode()
                    self.callback={'provider_event_id':'button-'+self.task_id,
                        'correlation_delivery_id':params['delivery_id'],'kind':'button','action_id':'acknowledge',
                        'callback_token':self.handles.put(self.facts.callback_token,'callback-authority'),
                        'content_base64':base64.b64encode(content).decode(),'media_type':'text/plain',
                        'destination_type':self.destination,'occurred_at_ms':int(time.time()*1000)}
                    self.record_environment('button_callback_available')
        if self.case_id=='test_006' and method=='handoff.compact' and self.callback is not None:
            with self.lock: self.callback_duplicates+=1
            self.record_environment('late_duplicate_provider_event')

    # --- Evaluator-issued product invariant battery (2026-09-20 hardening) ---
    # Published requirements 1, 3, 4, 5, 7, 10, 11, 12, 13, 14 and 21-24 oblige
    # the product to answer this battery.  The evaluator issues it, not the
    # lower agent, so it costs the rollout no model turns and its outcome
    # measures the Candidate product rather than the driver's diligence.  It
    # never changes seed_status and never appends an infrastructure error: a
    # Candidate limitation observed here is scored, not treated as a missing
    # evaluator service.
    def _probe(self, name, method, params, *, gateway='A'):
        if name in self.probe_results:
            raise RuntimeError('duplicate invariant probe name: '+name)
        if self.closing or (self.deadline-time.monotonic())<PROBE_MIN_REMAINING_SECONDS:
            self.probe_results[name]={'observation':'unavailable','method':method,
                'reason':'invariant probe budget guard'}
            return 'unavailable',None
        result=self.call(gateway,method,params,origin='environment_invariant_probe')
        response=result.get('response')
        if (result.get('status')=='timeout' or not result.get('dispatched')
                or not isinstance(response,dict)):
            observation,value='unavailable',None
        elif response.get('ok') is True:
            observation,value='accepted',response
        elif response.get('ok') is False or isinstance(response.get('error'),dict):
            observation,value='refused',response
        else:
            observation,value='unavailable',None
        entry={'observation':observation,'method':method,'reference':result.get('evidence_reference')}
        if observation=='refused':
            error=response.get('error') if isinstance(response.get('error'),dict) else {}
            entry['error_code']=error.get('code')
        self.probe_results[name]=entry
        return observation,value

    def _probe_battery(self,source_actor,schema,started):
        self.record_environment('product_invariant_battery_started')
        try:
            self._run_probe_battery(source_actor,schema,started or {})
        except Exception as exc:
            # A battery defect must never destroy a case or be attributed to
            # infrastructure; the affected outcomes stay unavailable.
            self.probe_error=type(exc).__name__
        self.record_environment('product_invariant_battery_finished',
            probes_executed=len(self.probe_results))

    def _run_probe_battery(self,source_actor,schema,started):
        base={'actor':source_actor,'task_id':self.task_id,'capability':self.capability}
        now=lambda:int(time.time()*1000)
        forged=dict(source_actor,grant='probe-forged-grant-'+digest((self.facts.task_nonce+':forged').encode()))
        state={'revision':started.get('revision'),'owner_epoch':started.get('owner_epoch'),
               'capability_epoch':started.get('capability_epoch'),'sequence':1}
        observation,value=self._probe('status_projection_available','handoff.status',dict(base))
        if observation=='accepted':
            for key in ('revision','owner_epoch','capability_epoch'):
                if isinstance(value.get(key),int): state[key]=value[key]
        self._probe('forged_grant_refused','handoff.status',dict(base,actor=forged))
        self._probe('unknown_field_refused','handoff.status',
            dict(base,openclaw_probe_unknown_field='invariant-probe'))
        self._probe('stale_revision_refused','handoff.append',{**base,
            'event_id':self.task_id+':probe-stale','idempotency_key':self.task_id+':probe-stale',
            'sequence':state['sequence']+1,'expected_revision':0,'owner_epoch':state['owner_epoch'],
            'occurred_at_ms':now(),'schema_version':schema,
            'event':{'type':'progress','note':'invariant probe'}})
        self._probe('internal_disclosure_combination_refused','handoff.delivery.enqueue',{**base,
            'delivery_id':'probe-internal-'+self.task_id,'intent':'internal_control',
            'disclosure':'internal','visibility':'principal','destination_type':self.destination,
            'content_base64':base64.b64encode(b'invariant probe').decode(),'media_type':'text/plain',
            'event_id':self.task_id+':probe-internal','idempotency_key':self.task_id+':probe-internal',
            'sequence':state['sequence']+1,'expected_revision':state['revision'],
            'owner_epoch':state['owner_epoch'],'occurred_at_ms':now()})
        if schema==2:
            self._run_schema2_probes(base,state,now)
        else:
            self._probe('schema1_compaction_refused','handoff.compact',{**base,
                'event_id':self.task_id+':probe-compact','idempotency_key':self.task_id+':probe-compact',
                'sequence':state['sequence']+1,'expected_revision':state['revision'],
                'owner_epoch':state['owner_epoch'],'occurred_at_ms':now()})
        if 'foreign' in self.actor_values:
            self._probe('foreign_lineage_refused','handoff.status',
                dict(base,actor=self.actor_values['foreign']))

    def _run_schema2_probes(self,base,state,now):
        payload=base64.b64encode(PROBE_ATTACHMENT).decode()
        put={**base,'content_base64':payload,'media_type':'application/octet-stream',
             'visibility':'source_channel','event_id':self.task_id+':probe-attach',
             'idempotency_key':self.task_id+':probe-attach','sequence':state['sequence']+1,
             'expected_revision':state['revision'],'owner_epoch':state['owner_epoch'],
             'occurred_at_ms':now()}
        observation,value=self._probe('attachment_content_addressed','handoff.attachment.put',put)
        attachment_id=None
        if observation=='accepted':
            state['sequence']+=1
            if isinstance(value.get('revision'),int): state['revision']=value['revision']
            attachment_id=value.get('attachment_id')
            self.probe_results['attachment_content_addressed']['address_matches']=bool(
                attachment_id=='sha256:'+digest(PROBE_ATTACHMENT)
                and value.get('sha256')==digest(PROBE_ATTACHMENT)
                and value.get('size')==len(PROBE_ATTACHMENT))
        observation,value=self._probe('exact_retry_replayed','handoff.attachment.put',dict(put))
        if observation=='accepted':
            self.probe_results['exact_retry_replayed']['replayed_without_new_object']=bool(
                value.get('replayed') is True and attachment_id is not None
                and value.get('attachment_id')==attachment_id)
        self._probe('changed_body_conflicts','handoff.attachment.put',
            dict(put,content_base64=base64.b64encode(PROBE_ATTACHMENT+b'-changed').decode()))
        if attachment_id:
            observation,value=self._probe('attachment_read_authorized','handoff.attachment.read',
                dict(base,attachment_id=attachment_id))
            if observation=='accepted':
                self.probe_results['attachment_read_authorized']['content_matches']=bool(
                    value.get('content_base64')==payload)
        observation,value=self._probe('logical_clock_monotonic','handoff.append',{**base,
            'event_id':self.task_id+':probe-rollback','idempotency_key':self.task_id+':probe-rollback',
            'sequence':state['sequence']+1,'expected_revision':state['revision'],
            'owner_epoch':state['owner_epoch'],'occurred_at_ms':max(1,now()-864000000),
            'schema_version':2,'event':{'type':'progress','note':'invariant probe'}})
        if observation=='accepted':
            state['sequence']+=1
            # Requirement 5 is non-decrease, not strict increase: comparing with
            # >= keeps this independent of the evaluator's own revision
            # bookkeeping, which a correct product need not mirror exactly.
            self.probe_results['logical_clock_monotonic']['monotonic']=bool(
                isinstance(value.get('revision'),int) and value['revision']>=(state['revision'] or 0)
                and value.get('owner_epoch')==state['owner_epoch']
                and value.get('capability_epoch')==state['capability_epoch'])
            if isinstance(value.get('revision'),int): state['revision']=value['revision']
        elif observation=='refused':
            # Refusing a rolled-back client time also preserves monotonicity.
            self.probe_results['logical_clock_monotonic']['monotonic']=True
        observation,value=self._probe('integrity_projection_valid','handoff.integrity.verify',dict(base))
        if observation=='accepted':
            self.probe_results['integrity_projection_valid']['valid']=bool(
                value.get('valid') is True and isinstance(value.get('projection_digest'),str)
                and len(value.get('projection_digest') or '')==64)
        observation,value=self._probe('compaction_proof_linked','handoff.compact',{**base,
            'event_id':self.task_id+':probe-compact','idempotency_key':self.task_id+':probe-compact',
            'sequence':state['sequence']+1,'expected_revision':state['revision'],
            'owner_epoch':state['owner_epoch'],'occurred_at_ms':now()})
        if observation=='accepted':
            state['sequence']+=1
            proof=value.get('compaction_proof') if isinstance(value.get('compaction_proof'),dict) else {}
            self.probe_results['compaction_proof_linked']['linked']=bool(
                isinstance(proof.get('generation'),int) and proof['generation']>=1
                and all(isinstance(proof.get(k),str) and len(proof.get(k) or '')==64
                        for k in ('projection_digest','generation_digest')))
            if isinstance(value.get('revision'),int): state['revision']=value['revision']
        observation,value=self._probe('state_survives_compaction','handoff.status',dict(base))
        if observation=='accepted':
            self.probe_results['state_survives_compaction']['preserved']=bool(
                isinstance(value.get('revision'),int) and value['revision']>=(state['revision'] or 0)
                and value.get('owner_epoch')==state['owner_epoch']
                and value.get('capability_epoch')==state['capability_epoch']
                and (attachment_id is None or attachment_id in canonical(value).decode()))

    # --- Required-outcome ledger (2026-09-20 hardening) -----------------------
    def _probe_view(self,name):
        view=self.probe_results.get(name)
        return dict(view) if isinstance(view,dict) else {'observation':'not_executed'}

    @staticmethod
    def _outcome(identifier,requirement,safety,satisfied,observed):
        return {'id':identifier,'requirement':requirement,'safety':bool(safety),
                'satisfied':satisfied,'observed':observed}

    def _refusal_outcome(self,identifier,name,requirement,*,safety=True):
        view=self._probe_view(name)
        observation=view.get('observation')
        satisfied=True if observation=='refused' else False if observation=='accepted' else None
        return self._outcome(identifier,requirement,safety,satisfied,view)

    def _success_outcome(self,identifier,name,requirement,key,*,safety=False):
        view=self._probe_view(name)
        observation=view.get('observation')
        if observation=='accepted':
            satisfied=bool(view.get(key)) if key else True
        elif observation=='refused':
            satisfied=False
        else:
            satisfied=None
        return self._outcome(identifier,requirement,safety,satisfied,view)

    def _monotonic_outcome(self):
        # Requirement 5 is satisfied either by accepting the rolled-back time
        # without lowering a counter or by refusing it outright; only an
        # unobservable probe leaves this null.
        view=self._probe_view('logical_clock_monotonic')
        observation=view.get('observation')
        satisfied=bool(view.get('monotonic')) if observation in {'accepted','refused'} else None
        return self._outcome('logical_clock_monotonic',
            'requirement 5: a rolled-back client time never lowers a stored counter',
            True,satisfied,view)

    def _world_outcome(self,identifier,requirement,safety,satisfied,observed):
        return self._outcome(identifier,requirement,safety,satisfied,observed)

    def _required_outcomes(self,counters):
        schema1=self.case_id in {'dev_002','test_004'}
        outcomes=[self._success_outcome('product_status_projection','status_projection_available',
                      'requirement 20 / 02 status: the product answers a bounded status projection',None),
            self._refusal_outcome('forged_grant_fails_closed','forged_grant_refused',
                      'requirement 1: a copied or never-issued grant never authorizes access'),
            self._refusal_outcome('unknown_field_fails_closed','unknown_field_refused',
                      '02 common rules: unknown RPC fields are rejected'),
            self._refusal_outcome('stale_revision_fails_closed','stale_revision_refused',
                      'requirements 3 and 4: a stale expected_revision commits nothing'),
            self._refusal_outcome('internal_disclosure_fails_closed','internal_disclosure_combination_refused',
                      'requirement 10 / 02 delivery: internal_control requires disclosure internal '
                      'and visibility source_channel; every other combination is invalid')]
        if schema1:
            outcomes.append(self._refusal_outcome('schema1_compaction_fails_closed','schema1_compaction_refused',
                'requirement 12: a schema-1 task cannot compact or repair before migration'))
        else:
            outcomes.extend([
                self._success_outcome('attachment_is_content_addressed','attachment_content_addressed',
                    'requirement 7: attachment_id is the server-computed sha256 content address',
                    'address_matches'),
                self._success_outcome('exact_retry_replays','exact_retry_replayed',
                    'requirement 3: an exact duplicate replays the committed response and creates '
                    'no second object','replayed_without_new_object'),
                self._refusal_outcome('changed_body_conflicts','changed_body_conflicts',
                    'requirement 3: the same operation key with a changed payload conflicts '
                    'and commits nothing'),
                self._success_outcome('attachment_read_authorized','attachment_read_authorized',
                    'requirement 7: attachment.read is the only principal-visible content path',
                    'content_matches'),
                self._monotonic_outcome(),
                self._success_outcome('integrity_projection_valid','integrity_projection_valid',
                    'requirement 13: integrity.verify recomputes the current projection digest',
                    'valid'),
                self._success_outcome('compaction_proof_linked','compaction_proof_linked',
                    'requirement 13: compaction advances a generation and returns a linked proof',
                    'linked'),
                self._success_outcome('state_survives_compaction','state_survives_compaction',
                    'requirements 13 and 14: compaction resets no counter and drops no reference',
                    'preserved')])
        if 'foreign' in self.actor_values:
            outcomes.append(self._refusal_outcome('foreign_lineage_fails_closed','foreign_lineage_refused',
                'requirements 1 and 8: a different channel lineage is not authorized on this task'))
        if self.case_id=='test_002':
            outcomes.append(self._refusal_outcome('rotated_lineage_fails_closed','rotated_lineage_read_refused',
                'requirements 2 and 8: a rotated or revoked grant subtree cannot read result content'))
        outcomes.extend(self._case_goal_outcomes(counters))
        return outcomes

    def _case_goal_outcomes(self,counters):
        accepted=counters['accepted_channel_messages']
        matching=counters['exact_route_and_requested_body_messages']
        goals=[self._world_outcome('no_foreign_route_effect',
                   'requirement 11: no message leaves on a route other than the immutable source route',
                   True,counters['foreign_route_messages']==0,
                   {'foreign_route_messages':counters['foreign_route_messages']}),
               self._world_outcome('no_uncorrelated_duplicate_effect',
                   'requirements 6 and 11: retry, takeover and restart produce no second external effect',
                   True,counters['unexpected_uncorrelated_messages']==0,
                   {'unexpected_uncorrelated_messages':counters['unexpected_uncorrelated_messages']})]
        if self.case_id in {'test_001','test_002','test_003','test_004','dev_001'}:
            goals.append(self._world_outcome('exactly_one_verified_user_result',
                'requirements 9 and 11: exactly one user result is accepted on the exact source '
                'route with the requested bytes',False,accepted>=1 and matching==1 and accepted<=2,
                {'accepted_channel_messages':accepted,
                 'exact_route_and_requested_body_messages':matching}))
        if self.case_id in {'dev_002','test_005'}:
            goals.extend([self._world_outcome('exactly_one_complete_upload',
                    'requirements 16 and 17: one logical media upload completes with the exact '
                    'ordered block set',False,
                    counters['matching_uploads']==1 and counters['exact_complete_uploads']==1,
                    {'matching_uploads':counters['matching_uploads'],
                     'exact_complete_uploads':counters['exact_complete_uploads']}),
                self._world_outcome('media_result_reaches_source_route',
                    'requirement 17: a media-bearing result is not silently downgraded to text',
                    False,matching==1,{'exact_route_and_requested_body_messages':matching})])
        if self.case_id in {'dev_001','test_006'}:
            goals.extend([self._world_outcome('exactly_one_correlated_reply',
                    'requirement 20: completing an interaction freezes at most one correlated reply',
                    False,counters['correlated_reply_messages']==1,
                    {'correlated_reply_messages':counters['correlated_reply_messages']}),
                self._world_outcome('interaction_verified_once',
                    'requirements 18 and 19: a duplicate provider event replays one durable '
                    'interaction instead of creating a second',False,
                    counters['verified_interactions']==1,
                    {'verified_interactions':counters['verified_interactions'],
                     'late_duplicate_source_events':counters['late_duplicate_source_events']})])
        return goals

    def finish(self):
        if self.closed_evidence is not None: return self.closed_evidence
        # Stop accepting new operations before destroying Gateway/namespace.
        # In-flight calls have the same deadline as the surrounding case.
        self.closing=True
        client_evidence=self.client.close() if self.client else {}
        if self.fault_thread:
            self.fault_thread.join(timeout=max(0,min(120,self.deadline-time.monotonic())))
        if self.fault_thread and self.fault_thread.is_alive():
            self.infra_errors.append('owned_peer_fault_thread_unresolved')
        cluster_evidence=self.cluster.close() if self.cluster else {}
        if self.client:
            end=time.monotonic()+2
            while self.client.snapshot()['active_requests'] and time.monotonic()<end:
                time.sleep(.01)
            client_evidence=self.client.snapshot()
            if client_evidence['active_requests'] or client_evidence['active_connections']:
                self.infra_errors.append('native_client_cleanup_unresolved')
        world_evidence=self.world.close()
        if world_evidence.get('failure') or not world_evidence.get('cleanup_complete'):
            self.infra_errors.append('native_connector_world_unverified')
        # Snapshot only after product writers and service requests have stopped.
        # External accepted state is independent of Candidate RPC claims.
        channel=self.world.adapters['channel']
        messages=[item['payload'] for item in channel.accepted.values()]
        matching=[m for m in messages if m.get('route')==self.route and m.get('content_sha256')==digest(self.note)]
        matching_ids={m.get('delivery_id') for m in matching}
        correlated_replies=[m for m in messages if m.get('route')==self.route
            and isinstance(m.get('reply_to'),dict)
            and m['reply_to'].get('correlation_delivery_id') in matching_ids]
        provider=self.world.adapters['media']
        uploads=[uid for uid,record in provider.uploads.items() if record['init'].get('content_sha256')==digest(self.asset)] if self.asset else []
        exact_uploads=[uid for uid in uploads if provider.exact_upload(uid,self.asset)]
        oracle_observations={'accepted_channel_messages':len(messages),
            'exact_route_and_requested_body_messages':len(matching),
            'foreign_route_messages':sum(m.get('route')!=self.route for m in messages),
            'unexpected_uncorrelated_messages':len(messages)-len(matching)-len(correlated_replies),
            'correlated_reply_messages':len(correlated_replies),
            'expected_attachment_size':len(self.asset),'matching_uploads':len(uploads),
            'exact_complete_uploads':len(exact_uploads),
            'verified_interactions':len(self.world.adapters['interaction'].accepted),
            'callback_source_event_created':self.callback is not None,
            'late_duplicate_source_events':self.callback_duplicates,
            'source_grant_rotation_attempted':self.rotation_done}
        # Required-outcome ledger: evaluator measurements, never Agent claims.
        # `satisfied` is null only when the evaluator could not observe the
        # outcome at all; null is not a failure and never counts as unmet.
        try:
            outcomes=self._required_outcomes(oracle_observations)
        except Exception as exc:
            outcomes=[]
            self.probe_error=self.probe_error or type(exc).__name__
        determinate=[item for item in outcomes if item['satisfied'] is not None]
        oracle_observations.update({
            'required_outcomes':outcomes,
            'required_outcomes_total':len(determinate),
            'required_outcomes_satisfied':sum(1 for item in determinate if item['satisfied']),
            'required_outcomes_unavailable':len(outcomes)-len(determinate),
            'required_safety_outcomes_failed':[item['id'] for item in determinate
                                               if item['safety'] and not item['satisfied']],
            'invariant_probe_error':self.probe_error,
            'ledger_interpretation':'Evaluator-measured outcomes for this case. A false entry was '
                'not achieved, whatever the artifact claims; a null entry was not observable and '
                'is neither success nor failure.'})
        with self.lock:
            native_events=list(self.rpc_events)
            self.log.close()
        self.closed_evidence={
            'schema_version':'openclaw-native-case-evidence-v1','case_id':self.case_id,
            'task_id':self.task_id,'world_instance_id':self.world.instance_id,
            'case_bundle_sha256':digest(canonical(asdict(self.facts))),
            'seed_status':self.seed_status,'seed_error':self.seed_error,'seed_issues':list(self.seed_issues),
            'initialization':self.initialization_evidence(),
            'initial_state_operations':[e for e in native_events if e['origin']=='environment_initial_state'],
            'agent_client_operations':[e for e in native_events if e['origin']=='agent_client'],
            'environment_operations':[e for e in native_events if e['origin'] not in {'environment_initial_state','agent_client'}],
            'environment_events':list(self.environment_events),'cluster':cluster_evidence,
            'client':client_evidence,'world':world_evidence,'infrastructure_errors':list(self.infra_errors),
            'oracle_observations':oracle_observations,
            'evaluator_authored_agent_artifact':False,'semantic_score_computed':False,
            'gateway_origin_verified_by_connector_alone':False}
        path=self.root/'evidence.json'
        path.write_text(json.dumps(self.closed_evidence,indent=2)+'\n')
        return self.closed_evidence
