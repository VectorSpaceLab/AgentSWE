"""Actual Unix HTTP relays with synthetic namespace probes; no Agent score."""
import base64
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from evaluator.connector_world import ConnectorWorld,canonical,digest,ORIGIN_HEADER
from lower_agent.fixture_transport import NativeRelay,UnixConnection,placeholder
from lower_agent.gateway_cluster import GatewayCluster


class GatewayOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='oc-origin-')
        self.root=Path(self.temp.name)
        self.loss=[]
        self.world=ConnectorWorld(root=self.root/'world',deadline=time.monotonic()+15,
            response_loss_observer=lambda role,event:self.loss.append(event))
        self.alive={'A':True,'B':True};self.keys={};self.relays=[]
        for number,role in enumerate(('A','B'),1):
            self.keys[role]=self.world.register_gateway_origin(role,0,self.probe(role,number))
        self.world.start()
        self.a=self.relay('A');self.b=self.relay('B')
    def tearDown(self):
        for relay in self.relays:relay.close()
        self.assertTrue(self.world.close()['cleanup_complete'])
        self.temp.cleanup()
    def probe(self,role,number):
        def value():
            if not self.alive[role]:raise RuntimeError('synthetic namespace stopped')
            return {'pid':1000+number,'start_ticks':2000+number,'net_inode':3000+number}
        return value
    def relay(self,role):
        relay=NativeRelay(role='channel',upstream=self.world.socket_path('channel'),
            token=self.world.adapters['channel'].token,deadline=self.world.deadline,
            origin_credential=self.keys[role])
        self.relays.append(relay);return relay
    def message(self,key):
        return {'dispatch_id':key,'delivery_id':'delivery-'+key,
            'route':{'provider':'unit','account_id':'unit','peer_id':'private-unit','destination_type':'direct'},
            'intent':'user_result','disclosure':'full','media_type':'text/plain',
            'content_base64':base64.b64encode(b'unit request').decode(),'content_sha256':digest(b'unit request')}
    def request(self,path,key,*,host=False,claimed_origin=None):
        connection=UnixConnection(path,3)
        headers={'Content-Type':'application/json','Idempotency-Key':key,
            'Authorization':'Bearer '+(self.world.adapters['channel'].token if host else placeholder('channel'))}
        if claimed_origin is not None:headers[ORIGIN_HEADER]=claimed_origin
        try:
            connection.request('POST','/v1/messages',canonical(self.message(key)),headers=headers)
            response=connection.getresponse();data=response.read()
            return response.status,data
        except http.client.RemoteDisconnected:return None,None
        finally:connection.close()
    def event(self,key):
        end=time.monotonic()+2
        while time.monotonic()<end:
            rows=[event for event in self.world.events if event.get('request',{}).get('dispatch_id')==key]
            if rows:return rows[-1]
            time.sleep(.005)
        self.fail('no completed world event for '+key)

    def test_actual_relays_bind_separate_origins_and_strip_forged_client_header(self):
        self.assertEqual(self.request(self.a.socket_path,'a',claimed_origin=self.keys['B'])[0],202)
        self.assertEqual(self.request(self.b.socket_path,'b',claimed_origin=self.keys['A'])[0],202)
        self.assertEqual(self.event('a')['gateway_origin']['gateway_id'],'A')
        self.assertEqual(self.event('b')['gateway_origin']['gateway_id'],'B')
        self.assertTrue(self.event('a')['gateway_origin']['verified'])

    def test_direct_world_request_without_registered_origin_is_rejected(self):
        code,_=self.request(self.world.socket_path('channel'),'direct',host=True)
        self.assertEqual(code,403)
        self.assertEqual(self.world.adapters['channel'].accepted,{})
        self.assertFalse(self.world.request_origin(self.keys['A'],(os.getpid()+1,0,0))['verified'])

    def test_stopped_namespace_cannot_launder_origin_through_retained_relay(self):
        self.alive['A']=False
        self.assertEqual(self.request(self.a.socket_path,'stale')[0],403)
        self.assertEqual(self.world.adapters['channel'].accepted,{})
        self.assertEqual(self.request(self.b.socket_path,'live')[0],202)

    def test_response_loss_record_carries_admitted_namespace_before_observer(self):
        for role,relay in (('A',self.a),('B',self.b)):
            self.world.adapters['channel'].drop_next_response=True
            self.assertIsNone(self.request(relay.socket_path,'loss-'+role)[0])
            event=self.event('loss-'+role)
            self.assertTrue(event['response_lost_after_native_acceptance'])
            self.assertEqual(event['gateway_origin']['gateway_id'],role)
        self.assertEqual([event['gateway_origin']['gateway_id'] for event in self.loss],['A','B'])
        self.assertEqual(len(self.world.adapters['channel'].accepted),2)
        self.assertEqual(self.a.snapshot()['retries'],0)

    def test_generation_is_evaluator_owned_and_private_credential_not_in_evidence(self):
        credential=self.world.register_gateway_origin('A',1,self.probe('A',9))
        value=self.world.request_origin(credential,(os.getpid(),0,0))
        self.assertEqual(value['generation'],1)
        self.request(self.a.socket_path,'redaction')
        snapshot=json.dumps(self.world.snapshot())+(self.root/'world/events.jsonl').read_text()
        for key in (*self.keys.values(),credential):self.assertNotIn(key,snapshot)
        with self.assertRaises(ValueError):
            NativeRelay(role='channel',upstream=self.world.socket_path('channel'),
                deadline=self.world.deadline,port=12345,origin_credential=credential)

    def test_cluster_dispatch_enters_selected_gateway_sandbox(self):
        cluster=GatewayCluster.__new__(GatewayCluster)
        cluster.ids={'A':'A','B':'B'};cluster.closed=False
        cluster.lifecycle_lock=threading.RLock();cluster.restarting=set()
        cluster.sandboxes={'A':object(),'B':object()}
        cluster.node=cluster.product=Path('/synthetic');cluster.ports={'A':1,'B':2}
        cluster.token='local';cluster.env={};cluster.deadline=time.monotonic()+5
        seen=[]
        cluster.native=SimpleNamespace(deadline_rpc=lambda *args,**kwargs:seen.append(kwargs['sandbox']))
        cluster.rpc('A','handoff.status',{'untouched':'agent-selected'})
        cluster.rpc('B','handoff.status',{'untouched':'agent-selected'})
        self.assertEqual(seen,[cluster.sandboxes['A'],cluster.sandboxes['B']])


if __name__=='__main__':unittest.main()
