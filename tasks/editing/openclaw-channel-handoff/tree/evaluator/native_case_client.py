"""Evaluator-owned handle codec and passthrough to actual native Gateway CLI.

Only client-submitted methods/parameters are dispatched. Initial-state and
external-fault operations are separately recorded by the case runtime.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import secrets
import socket
import socketserver
import tempfile
import threading
import time

METHODS = frozenset('''handoff.status handoff.inspect handoff.owner.acquire
handoff.append handoff.pause handoff.resume handoff.cancel handoff.capability.rotate
handoff.effect.enqueue handoff.outbox.dispatch handoff.attachment.put handoff.attachment.read
handoff.delivery.enqueue handoff.delivery.pull handoff.delivery.ack handoff.delivery.dispatch
handoff.delivery.reconcile handoff.media.stage handoff.media.upload handoff.media.reconcile
handoff.interaction.ingest handoff.interaction.reconcile handoff.interaction.claim
handoff.interaction.complete handoff.migrate handoff.compact handoff.integrity.verify
handoff.integrity.repair'''.split())
SECRET_FIELDS = frozenset({'grant','capability','claim_token','callback_token','route',
    'provider_receipt_id','platform_message_id','provider_media_id','upload_session_id',
    'provider_interaction_id','provider_attachment_receipt_id','platform_attachment_id'})


class Handles:
    def __init__(self):
        self.values = {}
        self.reverse = {}
        self.lock = threading.RLock()
        self.instance = secrets.token_hex(12)

    def put(self, value, label=None):
        encoded = json.dumps(value,sort_keys=True,separators=(',',':'))
        key = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock:
            if key not in self.reverse:
                name = label+'-'+self.instance if label else 'opaque-'+secrets.token_hex(12)
                if name in self.values:
                    raise ValueError('handle alias cannot be rebound')
                self.values[name] = copy.deepcopy(value)
                self.reverse[key] = name
            return {'$handle':self.reverse[key]}

    def resolve(self, value):
        if isinstance(value,dict):
            if '$handle' in value:
                if set(value)!={'$handle'} or not isinstance(value['$handle'],str):
                    raise ValueError('malformed opaque handle reference')
                with self.lock:
                    if value['$handle'] not in self.values:
                        raise ValueError('unknown or foreign case handle')
                    return copy.deepcopy(self.values[value['$handle']])
            return {k:self.resolve(v) for k,v in value.items()}
        if isinstance(value,list):
            return [self.resolve(v) for v in value]
        return value

    def publish(self, value):
        if isinstance(value,dict):
            return {k:self.put(v) if k in SECRET_FIELDS and v is not None else self.publish(v)
                    for k,v in value.items()}
        if isinstance(value,list):
            return [self.publish(v) for v in value]
        return value


class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


class NativeCaseClient:
    def __init__(self, runtime, *, deadline):
        self.runtime, self.deadline = runtime, deadline
        self.temporary = tempfile.TemporaryDirectory(prefix='occ-')
        self.socket_path = Path(self.temporary.name)/'client.sock'
        self.lock = threading.Lock()
        self.connections = set()
        self.active_requests = 0
        self.stopped = threading.Event()
        self.closed = False
        client = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def setup(self):
                super().setup()
                self.connection.settimeout(max(.001,deadline-time.monotonic()))
                with client.lock: client.connections.add(self.connection)
            def finish(self):
                try: super().finish()
                finally:
                    with client.lock: client.connections.discard(self.connection)
            def reply(self,status,value):
                data = json.dumps(value,ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Connection','close')
                self.end_headers(); self.wfile.write(data); self.close_connection=True
            def do_GET(self):
                if self.path!='/native-case/context':
                    return self.reply(404,{'error':'route_not_exposed'})
                if time.monotonic()>=deadline or client.stopped.is_set():
                    return self.reply(408,{'error':'case_deadline_exhausted'})
                with client.lock: client.active_requests+=1
                try:
                    try:
                        context=runtime.context_for_agent()
                    except TimeoutError:
                        return self.reply(408,{'error':'case_deadline_exhausted'})
                    except Exception as exc:
                        with runtime.lock:
                            runtime.infra_errors.append('native_client_context:'+type(exc).__name__)
                        return self.reply(503,{'error':'evaluator_native_context_unavailable'})
                    status=503 if context.get('seed_status')=='initialization_failed' else 200
                    return self.reply(status,context)
                finally:
                    with client.lock: client.active_requests-=1
            def do_POST(self):
                if self.path not in {'/native-case/rpc','/native-case/batch'}:
                    return self.reply(404,{'error':'route_not_exposed'})
                if time.monotonic()>=deadline or client.stopped.is_set():
                    return self.reply(408,{'error':'case_deadline_exhausted'})
                if runtime.seed_status in {'not_started','awaiting_first_context','initializing','initialization_failed'}:
                    return self.reply(409,{'error':'case_context_required',
                        'seed_status':runtime.seed_status})
                lengths=self.headers.get_all('Content-Length',[])
                try:
                    if len(lengths)!=1 or self.headers.get('Transfer-Encoding'):
                        raise ValueError('ambiguous request framing')
                    length=int(lengths[0])
                    if not 0<=length<=512*1024:
                        raise ValueError('native client request too large')
                    raw=self.rfile.read(length)
                    if len(raw)!=length: raise ValueError('incomplete request')
                    value=json.loads(raw)
                    commands=value if self.path.endswith('/batch') else [value]
                    if not isinstance(commands,list) or not 1<=len(commands)<=2:
                        raise ValueError('batch requires one or two explicit native calls')
                    resolved=[client.validate(x) for x in commands]
                except (ValueError,TypeError):
                    return self.reply(400,{'error':'invalid_native_client_request'})
                with client.lock: client.active_requests+=1
                try:
                    if len(resolved)==1:
                        outputs=[client.dispatch(resolved[0])]
                    else:
                        # The Agent supplies both calls. The barrier only
                        # starts them together; it does not choose an owner,
                        # revision, grant, operation or recovery sequence.
                        barrier=threading.Barrier(2)
                        def one(item):
                            barrier.wait(timeout=2)
                            return client.dispatch(item)
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            outputs=list(pool.map(one,resolved))
                    self.reply(200,outputs if self.path.endswith('/batch') else outputs[0])
                finally:
                    with client.lock: client.active_requests-=1
        self.server=UnixServer(str(self.socket_path),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
        self.thread.start()
        self.watchdog=threading.Thread(target=self.expire,daemon=True)
        self.watchdog.start()

    def validate(self,value):
        if (not isinstance(value,dict) or set(value)!={'gateway','method','params'}
            or value['gateway'] not in {'A','B'} or value['method'] not in METHODS
            or not isinstance(value['params'],dict)):
            raise ValueError('not an allowed explicit native RPC')
        params=self.runtime.handles.resolve(value['params'])
        if params.get('task_id')!=self.runtime.task_id:
            raise ValueError('native client is scoped to the current task')
        return value['gateway'],value['method'],params

    def dispatch(self,item):
        gateway,method,params=item
        try:
            result=self.runtime.call(gateway,method,params,origin='agent_client')
            self.runtime.after_agent_call(gateway,method,params,result)
            return self.runtime.public_result(result)
        except Exception as exc:
            # An evaluator/client exception is not evidence of bad Candidate
            # behavior. Preserve its attribution even if the Agent later
            # produces an otherwise well-formed partial report.
            with self.runtime.lock:
                self.runtime.infra_errors.append('native_client_dispatch:'+type(exc).__name__)
            return {'status':'error','response':None,'error':'evaluator_native_client_error',
                    'outcome':'unknown; no semantic success inferred'}

    def cancel_connections(self):
        with self.lock: connections=list(self.connections)
        for connection in connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    def expire(self):
        if not self.stopped.wait(max(0,self.deadline-time.monotonic())):
            self.cancel_connections()

    def close(self):
        if self.closed: return self.snapshot()
        self.stopped.set(); self.cancel_connections()
        self.server.shutdown(); self.server.server_close()
        self.thread.join(timeout=1); self.watchdog.join(timeout=1)
        end=time.monotonic()+2
        while time.monotonic()<end:
            with self.lock: active=len(self.connections)+self.active_requests
            if not active: break
            time.sleep(.01)
        self.temporary.cleanup(); self.closed=True
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {'closed':self.closed,'active_requests':self.active_requests,
                    'active_connections':len(self.connections),'solver_operations':0}
