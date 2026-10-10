#!/usr/bin/env python3
"""Small evaluator-owned Responses broker for Dyad lower-agent runs.

The server is the only process allowed to read the real upstream credential.
Candidate requests are accepted with a placeholder credential, have model and
reasoning forcibly rewritten, and are counted without persisting secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.append("@@AGENTSWE_EDITING_CONTROL@@")
from request_ledger import Stats as RequestStats
from responses_stream import direct_opener, read_response, strict_json, RedactedCapture
import json
import os
import threading
import math
import socket
from contextlib import contextmanager
from functools import partial
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "deepseek-flash"
EFFORT = "high"
ALLOWED_EFFORTS = {"high"}
INBOUND_PLACEHOLDER = "broker-only-placeholder"
MAX_ATTEMPTS = 1
MAX_CALL_SECONDS = 600.0
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# Retries for a transport that never reached the provider. 34 of the 50 recorded
# lower failures on 2026-09-15 were ``transport_attempts=0`` / ``not_submitted``:
# nothing was sent, billed or generated, so resending is not a replay and the
# single-upstream guarantee for submitted requests is untouched.
UNSENT_MAX_RETRIES = 3
UNSENT_RETRY_BACKOFF_SECONDS = 1.0



def _completed_or_budget_limited(value):
    """Accept a completed response, or one finished inside the Candidate's own
    output budget.

    The latter arrives as HTTP 200 with its own id, complete usage and no error
    -- exactly what a real API client receives when it sets max_output_tokens --
    so treating it as a provider fault invents a failure production cannot
    produce. Nothing else about a response is admitted here.
    """
    if not isinstance(value, dict):
        return False
    if value.get('status') == 'completed':
        return True
    details = value.get('incomplete_details')
    return (value.get('status') == 'incomplete' and isinstance(details, dict)
            and details.get('reason') == 'max_output_tokens'
            and value.get('error') is None)

class Stats(RequestStats):
    def snapshot(self):
        value=super().snapshot()
        rows=value.get('requests',[])
        value['runtime']['client_failures']=sum(str(v.get('error','')).startswith(('protocol:','credential:')) for v in rows)
        value['runtime']['provider_failures']=sum(str(v.get('error','')).startswith('provider:') for v in rows)
        return value


def locked_payload(value: dict[str, Any], api_path: str) -> dict[str, Any]:
    payload=dict(value);payload['model']=MODEL
    if api_path=='/v1/responses':
        payload['reasoning']={**(payload.get('reasoning') if isinstance(payload.get('reasoning'),dict) else {}),'effort':EFFORT}
        payload.pop('reasoning_effort',None)
    else:
        payload.pop('reasoning',None);payload['reasoning_effort']=EFFORT
    return payload


class BrokerState:
    def __init__(
        self,
        upstream: str,
        credential: str | None,
        dry_run: bool = False,
        *,
        effort: str = EFFORT,
        stats_file: Path | None = None,
    ) -> None:
        if effort not in ALLOWED_EFFORTS:
            raise ValueError(f"unsupported reasoning effort: {effort}")
        self.upstream = upstream.rstrip("/")
        self.credential = credential
        self.dry_run = dry_run
        self.effort = effort
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.calls: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.unsent_retries: list[dict[str, Any]] = []
        self.tokens = {"input": 0, "output": 0, "total": 0}
        self.ledger = Stats(stats_file, effort=effort, role="lower")

    def record_unsent_retry(self, error_type: str) -> None:
        """Count an attempt that failed before the provider received anything.

        These never reach ``ledger.transport_started``, so the logical request
        keeps ``transport_attempts=0`` until one attempt actually starts and no
        provider work is duplicated. Counted separately so the evidence shows
        recovery happened rather than hiding it.
        """
        with self.lock:
            self.unsent_retries.append({"at": time.time(), "error_type": error_type})

    def record(
        self,
        *,
        ok: bool,
        status: int,
        payload: dict[str, Any],
        detail: str = "",
        failure_domain: str | None = None,
        request_id: str | None = None,
    ) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        known = isinstance(usage, dict) and all(type(usage.get(name)) is int for name in ('input_tokens','output_tokens','total_tokens'))
        inp = usage.get('input_tokens') if known else None
        out = usage.get('output_tokens') if known else None
        self.ledger.record(ok=ok,usage=usage,error=None if ok else (failure_domain or 'broker')+':'+detail,
            request_id=request_id,response=payload if payload else None)
        with self.lock:
            self.tokens["input"] += max(0, inp or 0)
            self.tokens["output"] += max(0, out or 0)
            self.tokens["total"] += max(0, (inp or 0) + (out or 0))
            event = {"at": time.time(), "status": status, "model": MODEL, "reasoning_effort": self.effort,
                     "input_tokens": inp, "output_tokens": out, "usage_state": "known" if known else "unknown"}
            if not ok:
                event["failure_domain"] = failure_domain or "broker"
            (self.calls if ok else self.failures).append({**event, **({"detail": detail} if detail else {})})

    def stats(self) -> dict[str, Any]:
        snapshot = self.ledger.snapshot()
        rows = snapshot.get('requests', [])
        domains={name:sum(str(row.get('error','')).startswith(name+':') for row in rows) for name in ('provider','credential','protocol','broker','deadline')}
        snapshot['protocol'].update(credential_owner='evaluator-broker', candidate_credential='placeholder-only')
        snapshot['runtime'].update({name+'_failures':count for name,count in domains.items()})
        snapshot['calls']=[row for row in rows if row.get('ok')]
        snapshot['failures']=[row for row in rows if not row.get('ok')]
        with self.lock:
            snapshot['unsent_transport_retries']=list(self.unsent_retries)
        snapshot['runtime']['unsent_transport_retries']=len(snapshot['unsent_transport_retries'])
        snapshot['tokens']={name:snapshot['runtime'][field] for name,field in (('input','input_tokens'),('output','output_tokens'),('total','total_tokens'))}
        return snapshot


def load_credential(path: str) -> str:
    """Read only the supported key from an evaluator-owned env file."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            name, sep, value = line.partition("=")
            if sep and name.strip() in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}:
                values[name.strip()] = value.strip().strip('"').strip("'")
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if values.get(name):
            return values[name]
    raise RuntimeError("credential file has no supported provider key")


def chat_to_responses(request: dict[str, Any], *, effort: str = EFFORT) -> dict[str, Any]:
    converted = {"model": MODEL, "store": False, "stream": False}
    messages = request.get("messages")
    if isinstance(messages, list):
        inputs=[]
        for message in messages:
            if not isinstance(message,dict): raise ValueError('chat messages must be objects')
            role=message.get('role');content=message.get('content')
            if role=='tool':
                inputs.append({'type':'function_call_output','call_id':message.get('tool_call_id'),'output':content if isinstance(content,str) else json.dumps(content)})
            else:
                if content: inputs.append({'role':role,'content':content})
                for call in message.get('tool_calls',[]):
                    fn=call.get('function',{});inputs.append({'type':'function_call','call_id':call.get('id'),'name':fn.get('name'),'arguments':fn.get('arguments','{}')})
        converted['input']=inputs
    for source, target in (("max_tokens", "max_output_tokens"), ("temperature", "temperature")):
        if source in request:
            converted[target] = request[source]
    if isinstance(request.get("tools"), list):
        tools = []
        for item in request["tools"]:
            fn = item.get("function") if isinstance(item, dict) else None
            if isinstance(fn, dict):
                tools.append({"type": "function", "name": fn.get("name"), "description": fn.get("description"), "parameters": fn.get("parameters", {})})
        converted["tools"] = tools
    converted["reasoning"] = {"effort": effort}
    return converted


def responses_to_chat(response: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    text_parts.append(content["text"])
        elif item.get("type") == "function_call":
            tool_calls.append({"id": item.get("call_id") or item.get("id", "call_0"), "type": "function", "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")}})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": response.get("id", "deepcode-response"), "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens":response.get("usage",{}).get("input_tokens",0),
                "completion_tokens":response.get("usage",{}).get("output_tokens",0),
                "total_tokens":response.get("usage",{}).get("total_tokens",0)}}


DEADLINE_DETAIL='case_deadline_expired'


def deadline_domain(deadline_killed,submitted,unsubmitted_domain):
    """The evaluator's own case-deadline kill is neither a provider nor a broker fault.

    ``deadline_opener`` shuts this request's sockets down at the absolute case
    deadline, so every exception after that is the evaluator ending the case.
    Booking it as ``provider:`` invents an upstream failure and voids the cell
    (0921-fx-001 dyad test_004/test_005/test_006).
    """
    if deadline_killed:
        return 'deadline'
    return 'provider' if submitted else unsubmitted_domain


def deadline_detail(deadline_killed,exc):
    return DEADLINE_DETAIL if deadline_killed else type(exc).__name__


@contextmanager
def deadline_opener(deadline,on_request_start,deadline_killed=None):
    """Close only this request's sockets at the absolute case deadline.

    The shared connection class still owns TLS verification, explicit proxy,
    and the after-CONNECT request-start callback. No transport retry is added.
    """
    from responses_stream import BudgetHTTPConnection,BudgetHTTPSConnection,evaluator_proxy_url,_NoRedirect
    connections=[];sockets=[];lock=threading.Lock();expired=threading.Event()
    def expire():
        expired.set()
        with lock:
            for sock in sockets:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                sock.close()
            for conn in connections:conn.close()
    def expire_on_deadline():
        # Only the timer marks a deadline kill; the ``finally`` cleanup below
        # calls expire() on every ordinary exit and must stay invisible here.
        if deadline_killed is not None:deadline_killed.append(time.monotonic())
        expire()
    def remaining():
        value=deadline-time.monotonic()
        if value<=0 or expired.is_set():raise TimeoutError('lower case deadline expired')
        return value
    class DeadlineConnection:
        def __init__(self,*args,**kwargs):
            kwargs['timeout']=remaining();super().__init__(*args,**kwargs)
            with lock:connections.append(self)
        def connect(self):
            self.timeout=remaining();super().connect()
            with lock:sockets.append(self.sock)
            try:self.sock.settimeout(remaining())
            except BaseException:self.close();raise
        def send(self,data):
            self.timeout=remaining()
            if self.sock is not None:self.sock.settimeout(self.timeout)
            return super().send(data)
    class HTTPConnection(DeadlineConnection,BudgetHTTPConnection):pass
    class HTTPSConnection(DeadlineConnection,BudgetHTTPSConnection):pass
    class HTTP(urllib.request.HTTPHandler):
        def http_open(self,req):return self.do_open(partial(HTTPConnection,on_request_start=on_request_start),req)
    class HTTPS(urllib.request.HTTPSHandler):
        def __init__(self):super().__init__(context=ssl.create_default_context())
        def https_open(self,req):return self.do_open(partial(HTTPSConnection,on_request_start=on_request_start),req,context=self._context)
    proxy=evaluator_proxy_url()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':proxy} if proxy else {}),_NoRedirect(),HTTP(),HTTPS())
    timer=threading.Timer(remaining(),expire_on_deadline);timer.daemon=True;timer.start()
    try:yield opener
    finally:
        timer.cancel();expire();timer.join(timeout=1)


class Handler(BaseHTTPRequestHandler):
    server_version = "DyadAgentLoopBroker/1"

    @property
    def state(self) -> BrokerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _deliver(self, response, *, stream_requested):
        """Honor the actual SDK wire protocol, including tool-call chunks.

        The upstream response is already durably completed. Delivery failure
        must not overwrite that terminal intent or send the upstream again.
        """
        chat=self.path.rstrip('/')=='/v1/chat/completions'
        value=responses_to_chat(response) if chat else response
        try:
            if not stream_requested:
                self._json(200,value);return
            events=[]
            if chat:
                message=value['choices'][0]['message']
                base={'id':value['id'],'object':'chat.completion.chunk','created':int(time.time()),'model':MODEL}
                delta={'role':'assistant'}
                if message.get('content') is not None:delta['content']=message['content']
                events.append({**base,'choices':[{'index':0,'delta':delta,'finish_reason':None}]})
                for index,call in enumerate(message.get('tool_calls',[])):
                    events.append({**base,'choices':[{'index':0,'delta':{'tool_calls':[{'index':index,**call}]},'finish_reason':None}]})
                events.append({**base,'choices':[{'index':0,'delta':{},'finish_reason':value['choices'][0]['finish_reason']}]})
                events.append({**base,'choices':[],'usage':value['usage']})
            else:
                events.append({'type':'response.created','response':{**response,'status':'in_progress','output':[]}})
                for index,item in enumerate(response.get('output',[])):
                    if item.get('type')=='function_call':
                        events.append({'type':'response.output_item.added','output_index':index,'item':{**item,'status':'in_progress','arguments':''}})
                        events.append({'type':'response.function_call_arguments.delta','item_id':item.get('id'),'output_index':index,'delta':item.get('arguments','')})
                        events.append({'type':'response.function_call_arguments.done','item_id':item.get('id'),'output_index':index,'arguments':item.get('arguments','')})
                    elif item.get('type')=='message':
                        events.append({'type':'response.output_item.added','output_index':index,'item':{**item,'status':'in_progress','content':[]}})
                        for content_index,part in enumerate(item.get('content',[])):
                            if part.get('type')!='output_text':continue
                            common={'item_id':item.get('id'),'output_index':index,'content_index':content_index}
                            events.append({'type':'response.content_part.added',**common,'part':{**part,'text':''}})
                            events.append({'type':'response.output_text.delta',**common,'delta':part.get('text','')})
                            events.append({'type':'response.output_text.done',**common,'text':part.get('text','')})
                            events.append({'type':'response.content_part.done',**common,'part':part})
                    else:
                        events.append({'type':'response.output_item.added','output_index':index,'item':item})
                    events.append({'type':'response.output_item.done','output_index':index,'item':item})
                events.append({'type':'response.completed','response':response})
            raw=(''.join('data: '+json.dumps(event,ensure_ascii=False)+'\n\n' for event in events)+'data: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):
            with self.state.ledger.lock:
                self.state.ledger.value.setdefault('client_delivery_errors',[]).append({'response_id':response.get('id'),'upstream_state':'completed','upstream_retry_allowed':False})
                self.state.ledger._save()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/health", "/healthz"}: self._json(200, {"ok": True, "model": MODEL, "reasoning_effort": self.state.effort}); return
        if self.path == "/stats": self._json(200, self.state.stats()); return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        # Dyad's generic OpenAI-compatible provider uses Chat Completions
        # for a non-direct custom endpoint, while Codex-style clients use
        # Responses. Both are evaluator-owned transports and must be counted.
        if self.path.rstrip("/") not in {"/v1/responses", "/v1/chat/completions"}:
            self._json(404, {"error": "not_found"}); return
        request_id=None
        submitted=False
        deadline_killed=[]
        try:
            if self.headers.get("Authorization") != f"Bearer {INBOUND_PLACEHOLDER}":
                self.state.record(
                    ok=False,
                    status=401,
                    payload={},
                    detail="candidate_placeholder_missing",
                    failure_domain="credential",
                )
                self._json(401, {"error": {"type": "credential_failure", "message": "broker placeholder credential required"}})
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(raw)
            if not isinstance(request, dict): raise ValueError("request must be object")
            stream_requested=request.get("stream") is True
            deadline_header=self.headers.get('X-AgentSWE-Deadline-Monotonic')
            deadline=float(deadline_header) if deadline_header else time.monotonic()+MAX_CALL_SECONDS
            if not math.isfinite(deadline) or deadline<=time.monotonic():raise ValueError('lower case deadline expired')
            deadline=min(deadline,time.monotonic()+MAX_CALL_SECONDS)
            request["model"] = MODEL
            request["reasoning"] = {"effort": self.state.effort}
            if self.state.dry_run:
                response = {"id": "dry-run-response", "object": "response", "model": MODEL,
                            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "broker dry-run"}]}],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                self.state.record(ok=True, status=200, payload=response); self._json(200, response); return
            upstream_path = "/v1/responses" if self.path.rstrip("/") == "/v1/chat/completions" else self.path
            if self.path.rstrip("/") == "/v1/chat/completions":
                request = chat_to_responses(request, effort=self.state.effort)
            request['stream']=True
            request.pop('reasoning_effort',None)
            data=json.dumps(request,sort_keys=True).encode()
            context_id=self.headers.get('X-AgentSWE-Context')
            if not context_id or len(context_id)>256: raise ValueError('evaluator-owned lower context identity required')
            request_id=hashlib.sha256(context_id.encode()+b'\0'+data).hexdigest()
            prior=self.state.ledger.reserve(request_id,context_id=context_id)
            if prior is not None:
                completed=self.state.ledger.completed_response(prior)
                if completed is None:
                    self._json(409,{'error':{'type':'prior_request_result_unknown'},'retry_allowed':False,'request_sha256':request_id});return
                self._deliver(completed,stream_requested=stream_requested);return
            headers={'Content-Type':'application/json','Accept':'text/event-stream','User-Agent':'AgentSWE-Dyad-AgentLoop/2.0','Authorization':f'Bearer {self.state.credential or ""}'}
            req=urllib.request.Request(self.state.upstream+upstream_path,data=data,headers=headers,method='POST')
            def request_started():
                nonlocal submitted
                self.state.ledger.transport_started(request_id)
                submitted=True
            capture_dir=None
            if self.state.ledger.path:
                capture_dir=self.state.ledger.path.parent/(self.state.ledger.path.stem+'-raw');capture_dir.mkdir(exist_ok=True)
            # A transport that never reached the provider is retried: nothing was
            # sent, billed or produced, so no result is replayed. ``transport_started``
            # stays uncalled for those attempts, which keeps the ledger intent at
            # ``reserved_not_submitted``/``transport_attempts=0`` until one attempt
            # actually starts. A submitted attempt keeps single-upstream semantics.
            unsent_attempts=0
            while True:
                capture_handle=None;capture=None
                if capture_dir is not None:
                    name=request_id+('' if not unsent_attempts else '.unsent%03d'%unsent_attempts)
                    capture_handle=(capture_dir/(name+'.bin')).open('xb');capture=RedactedCapture(capture_handle,self.state.credential)
                try:
                    with deadline_opener(deadline,request_started,deadline_killed) as opener:
                        with opener.open(req,timeout=max(.001,deadline-time.monotonic())) as response:
                            raw,streamed=read_response(response.read1,content_type=response.headers.get('Content-Type',''),is_success=True,capture=capture.write if capture else None)
                            parsed=streamed if streamed is not None else strict_json(raw)
                    break
                except (urllib.error.URLError,OSError) as exc:
                    if (submitted or isinstance(exc,urllib.error.HTTPError)
                            or unsent_attempts>=UNSENT_MAX_RETRIES
                            or time.monotonic()+UNSENT_RETRY_BACKOFF_SECONDS>=deadline):
                        raise
                    unsent_attempts+=1
                    self.state.record_unsent_retry(type(exc).__name__)
                    time.sleep(UNSENT_RETRY_BACKOFF_SECONDS)
                finally:
                    if capture:capture.finish()
                    if capture_handle:capture_handle.close()
            if not isinstance(parsed,dict) or not _completed_or_budget_limited(parsed) or not isinstance(parsed.get('id'),str) or not parsed['id']:
                self.state.record(ok=False,status=502,payload=parsed if isinstance(parsed,dict) else {},detail='response_not_typed_completed',failure_domain='provider',request_id=request_id)
                self._json(502,{'error':{'type':'response_not_typed_completed'},'retry_allowed':False});return
            self.state.record(ok=True,status=200,payload=parsed,request_id=request_id)
            self._deliver(parsed,stream_requested=stream_requested)
        except urllib.error.HTTPError as exc:
            self.state.record(ok=False, status=exc.code, payload={}, detail=type(exc).__name__, failure_domain="provider" if submitted else "broker", request_id=request_id)
            self._json(502, {"error": {"type": "provider_failure", "message": "upstream provider rejected the evaluator broker request"}})
        except urllib.error.URLError as exc:
            self.state.record(ok=False, status=502, payload={}, detail=deadline_detail(deadline_killed, exc), failure_domain=deadline_domain(deadline_killed, submitted, "broker"), request_id=request_id)
            self._json(502, {"error": {"type": "provider_failure", "message": "upstream provider transport failed"}})
        except (ValueError, json.JSONDecodeError) as exc:
            self.state.record(ok=False, status=400, payload={}, detail=deadline_detail(deadline_killed, exc), failure_domain=deadline_domain(deadline_killed, submitted, "protocol"), request_id=request_id)
            self._json(400, {"error": {"type": "protocol_failure", "message": "invalid broker request"}})
        except OSError as exc:
            self.state.record(ok=False, status=502, payload={}, detail=deadline_detail(deadline_killed, exc), failure_domain=deadline_domain(deadline_killed, submitted, "broker"), request_id=request_id)
            self._json(502, {"error": {"type": "broker_failure", "message": "evaluator broker failed"}})


class BrokerServer(ThreadingHTTPServer):
    def __init__(self,address,upstream,secret,stats):
        super().__init__(address,Handler)
        self.upstream,self.secret,self.stats=upstream.rstrip('/'),secret,stats
        self.state=BrokerState(upstream,secret)
        self.state.ledger=stats


def read_secret(path):
    return load_credential(str(path)) if path is not None else os.environ.get('DEEPSEEK_API_KEY','').strip()


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--bind',default='127.0.0.1');parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--upstream',default='https://api.deepseek.com');parser.add_argument('--credential-file',type=Path)
    parser.add_argument('--stats-file',type=Path,required=True);args=parser.parse_args(argv)
    secret=read_secret(args.credential_file)
    if not secret:raise RuntimeError('evaluator broker credential is unavailable')
    server=BrokerServer((args.bind,args.port),args.upstream,secret,Stats(args.stats_file))
    print(json.dumps({'endpoint':f'http://{args.bind}:{server.server_port}/v1/responses','stats':f'http://{args.bind}:{server.server_port}/stats','protocol':{'model':MODEL,'reasoning_effort':EFFORT}}),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:return 0
    finally:server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
