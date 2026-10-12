"""Two native Gateways with shared SQLite, distinct netns and host relays.

This class starts/stops products and forwards exact RPCs. It contains no
task-solving operations, Agent recovery sequence or semantic Result score.
The native lower Agent runs separately; neither Gateway hosts its reasoning.
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from lower_agent.product_sandbox import ProductSandbox


class GatewayCluster:
    def __init__(self, *, product, state, workspace, runtime, output, endpoint,
                 deadline, world, product_readonly=False, case_socket=None,
                 gateway_token=None, primary_port=None):
        from lower_agent import launcher as native
        self.native = native
        self.product, self.output = Path(product), Path(output)
        self.state, self.workspace = Path(state), Path(workspace)
        self.runtime, self.deadline, self.world = runtime, deadline, world
        self.node = Path(str(runtime['node']))
        self.token = gateway_token or secrets.token_urlsafe(24)
        self.instance_id = secrets.token_hex(12)
        self.ids = {role:'case-'+self.instance_id+'-'+role.lower() for role in ('A','B')}
        self.endpoint, self.product_readonly, self.case_socket = endpoint, product_readonly, case_socket
        used=set()
        def port():
            value=native.free_port()
            while value in used:value=native.free_port()
            used.add(value)
            return value
        if primary_port is not None:
            if type(primary_port) is not int or not 1024<=primary_port<=65535:
                raise ValueError('invalid explicit primary Gateway port')
            used.add(primary_port)
        self.ports={'B':primary_port or port(),'A':port()}
        self.bridge=port()
        self.services={role:{'socket':world.socket_path(role),'port':port(),
                             'token':getattr(adapter,'token',None)}
                       for role,adapter in world.adapters.items()}
        # Same loopback ports in distinct netns preserve one public config.
        isolated=f'http://127.0.0.1:{self.bridge}/v1/responses'
        config=native.write_config(self.state,self.workspace,isolated,self.token)
        self.env=native.env_for(self.state,config,isolated,runtime)
        self.primary=self.peer=None
        self.sandboxes={}
        self.retired_sandboxes=[]
        self.generations={'A':0,'B':0}
        self.events=[];self.health={};self.closed=False
        self.lifecycle_lock=threading.RLock()
        self.restarting=set()
        self.sandbox=self._new_sandbox('B')
        self.peer_generation=0

    def _new_sandbox(self, role):
        holder={}
        def probe():
            sandbox=holder.get('sandbox')
            if sandbox is None:raise RuntimeError('Gateway sandbox not started')
            sandbox.verify_namespace()
            return dict(sandbox.namespace)
        credential=self.world.register_gateway_origin(self.ids[role],self.generations[role],probe)
        output=(self.output if role=='B' and self.generations[role]==0 else
                self.output/f"gateway-{role.lower()}-{self.generations[role]}-sandbox")
        output.mkdir(parents=True,exist_ok=True)
        sandbox=ProductSandbox(product=self.product,state=self.state,workspace=self.workspace,
            runtime=self.runtime,output=output,endpoint=self.endpoint,bridge_port=self.bridge,
            env=self.env,deadline=self.deadline,product_readonly=self.product_readonly,
            fixture_services=self.services,gateway_id=self.ids[role],case_socket=self.case_socket,
            native_origin_credential=credential)
        holder['sandbox']=sandbox
        self.sandboxes[role]=sandbox
        return sandbox

    def command(self, role):
        return [str(self.node),str(self.product/'openclaw.mjs'),'gateway',
                '--port',str(self.ports[role]),'--bind','loopback','--auth','token',
                '--token',self.token,'--allow-unconfigured']

    def _start_gateway(self, role):
        sandbox=self.sandboxes.get(role) or self._new_sandbox(role)
        startup_deadline=min(self.deadline,time.monotonic()+self.native.GATEWAY_STARTUP_SECONDS)
        generation=self.generations[role]
        path=self.output/('gateway-b.log' if role=='B' and generation==0 else
                          f'gateway-{role.lower()}-{generation}.log')
        with path.open('x') as log:
            process=sandbox.start(self.command(role),log,startup_deadline=startup_deadline)
        if role=='B':
            self.primary=process;self.sandbox=sandbox
        else:
            self.peer={'process':process,'namespace':dict(sandbox.namespace),'stopped':False}
        health=self.native.wait_gateway_health(process,path,self.node,self.product,self.ports[role],
            self.token,self.env,self.deadline,sandbox=sandbox,startup_deadline=startup_deadline)
        self.health[role]=health
        if not health or health.get('status')!='ok':
            raise RuntimeError(f'actual Gateway {role} did not reach native health')
        self.events.append({'event':'gateway_startup_budget','gateway':role,'generation':generation,
            'deadline':startup_deadline,'completed_at':time.monotonic(),
            'namespace':dict(sandbox.namespace)})
        return health

    def start(self):
        if self.primary is not None or self.closed:
            raise RuntimeError('cluster cannot be started twice')
        if not self.world.started:
            raise RuntimeError('native connector world must be started before Gateway')
        try:
            # Serialize only empty-state initialization; user RPC batches
            # remain concurrent and enter their explicitly chosen netns.
            self._start_gateway('B')
            self._start_gateway('A')
            if self.sandboxes['A'].namespace['net_inode']==self.sandboxes['B'].namespace['net_inode']:
                raise RuntimeError('Gateway origin isolation requires distinct network namespaces')
            self.events.append({'event':'both_gateways_ready','at':time.monotonic(),
                                'distinct_network_namespaces':True})
            return self.health
        except BaseException:
            self.close()
            raise

    def rpc(self, role, method, params, *, limit_seconds=None):
        if role not in self.ids or self.closed:
            raise ValueError('invalid or closed Gateway')
        with self.lifecycle_lock:
            if role in self.restarting:
                # An expected injected outage is visible to the Agent. Do
                # not choose retries or turn it into an evaluator exception.
                return {'status':'error','method':method,'response':None,
                        'dispatched':False,'error':'selected_gateway_restarting'}
            sandbox=self.sandboxes[role]
        return self.native.deadline_rpc(self.node,self.product,self.ports[role],self.token,
            method,params,self.env,self.deadline,limit_seconds,sandbox=sandbox)

    def crash_gateway(self, role):
        if role not in self.sandboxes or self.closed:
            raise ValueError('invalid or closed Gateway')
        with self.lifecycle_lock:
            self.restarting.add(role)
        event=self.sandboxes[role].crash_gateway()
        if role=='A':self.peer['stopped']=True
        self.events.append({'event':'gateway_crash','gateway':role,'gateway_id':self.ids[role],
            'generation':self.generations[role],'evidence':event,'at':time.monotonic()})
        return event

    def restart_gateway(self, role):
        old=self.sandboxes[role]
        if old.process is None or old.process.poll() is None:
            raise RuntimeError('Gateway must already be stopped before restart')
        old.close(2)
        self.retired_sandboxes.append(old)
        del self.sandboxes[role]
        self.generations[role]+=1
        if role=='A':self.peer_generation=self.generations[role]
        health=self._start_gateway(role)
        with self.lifecycle_lock:
            self.restarting.discard(role)
        self.events.append({'event':'gateway_restarted','gateway':role,'gateway_id':self.ids[role],
            'generation':self.generations[role],'at':time.monotonic()})
        return health

    def crash_peer(self):return self.crash_gateway('A')

    def restart_peer(self):return self.restart_gateway('A')

    def snapshot(self):
        return {'schema_version':'openclaw-native-gateway-cluster-v2',
            'instance_id':self.instance_id,'gateway_ids':dict(self.ids),
            'shared_state_path':str(self.state),'ports':dict(self.ports),
            'health':dict(self.health),'lifecycle_events':list(self.events),
            'namespaces':{role:dict(sandbox.namespace or {}) for role,sandbox in self.sandboxes.items()},
            'generations':dict(self.generations),'closed':self.closed,
            'restarting':sorted(self.restarting),
            'sandbox_evidence':[dict(s.attestation) for s in
                [*self.sandboxes.values(),*self.retired_sandboxes]],
            'native_handoff_semantics_verified':False,'agent_case_verified':False,
            'origin_scope':'independently registered per-Gateway sandbox; not native-method causality proof'}

    def close(self):
        if not self.closed:
            errors=[]
            for sandbox in self.sandboxes.values():
                try:sandbox.close(2)
                except Exception as exc:errors.append(type(exc).__name__)
            self.closed=True
            if errors:raise RuntimeError('Gateway sandbox cleanup incomplete: '+','.join(errors))
        return self.snapshot()
