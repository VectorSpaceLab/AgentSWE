#!/usr/bin/env python3
"""Small evaluator-owned Responses broker for DeepCode lower-agent runs.

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
from request_ledger import Stats
from responses_stream import direct_opener, read_response, strict_json, RedactedCapture
from collections import OrderedDict
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "deepseek-flash"
EFFORT = "high"
ALLOWED_EFFORTS = {"high", "max"}
INBOUND_PLACEHOLDER = "broker-only-placeholder"
MAX_ATTEMPTS = 1
# Retries for a transport that never reached the provider. 34 of the 50 recorded
# lower failures on 2026-09-15 were transport_attempts=0/not_submitted: nothing was
# sent, billed or generated, so resending is not a replay and the single-upstream
# guarantee for submitted requests is untouched.
UNSENT_MAX_RETRIES = 3
UNSENT_RETRY_BACKOFF_SECONDS = 1.0
MAX_CALL_SECONDS = 180.0
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# A provider answer that stopped on max_output_tokens is not a delivered answer.
# The evaluator forces reasoning effort "high" on every request, so when the
# reasoning alone exhausts the Candidate's output budget the Candidate gets a
# response with no message item at all.  Its runner then resends the identical
# message list as an "empty response" retry -- and used to be handed the identical
# stored response back out of the logical-request cache, 30 ms later, without any
# upstream call (0920-fh-003 test_001: 04:23:05.095 -> 04:23:05.125, against ~35 s
# for a real call at that context size).  A truncated answer therefore consumed
# the product's entire recovery budget for free.  Such a repeat is now escalated
# to a NEW logical identity, which is a fresh upstream call rather than a replay
# of one: at most TRUNCATED_RETRY_MAX of them per body, deterministic, so a third
# identical retry still returns the stored answer instead of billing forever.
TRUNCATED_RETRY_MAX = 2


def output_budget_truncated(response: Any) -> bool:
    """True for a provider answer that stopped on its max_output_tokens ceiling."""
    return (isinstance(response, dict) and response.get('status') == 'incomplete'
            and isinstance(response.get('incomplete_details'), dict)
            and response['incomplete_details'].get('reason') == 'max_output_tokens'
            and response.get('error') is None)


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

        These never reach transport_started, so the logical request keeps
        transport_attempts=0 until one attempt actually starts and no provider
        work is duplicated. Counted separately so the evidence shows recovery.
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
        truncated: bool = False,
    ) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        known = isinstance(usage, dict) and all(type(usage.get(name)) is int for name in ('input_tokens','output_tokens','total_tokens'))
        inp = usage.get('input_tokens') if known else None
        out = usage.get('output_tokens') if known else None
        self.ledger.record(ok=ok,usage=usage,error=None if ok else (failure_domain or 'broker')+':'+detail,
            request_id=request_id,response=payload if payload else None,
            output_budget_truncated=truncated)
        with self.lock:
            self.tokens["input"] += max(0, inp or 0)
            self.tokens["output"] += max(0, out or 0)
            self.tokens["total"] += max(0, (inp or 0) + (out or 0))
            event = {"at": time.time(), "status": status, "model": MODEL, "reasoning_effort": self.effort,
                     "input_tokens": inp, "output_tokens": out, "usage_state": "known" if known else "unknown"}
            if truncated:
                event["output_budget_truncated"] = True
                event["delivered_answer"] = False
            if not ok:
                event["failure_domain"] = failure_domain or "broker"
            (self.calls if ok else self.failures).append({**event, **({"detail": detail} if detail else {})})

    def stats(self) -> dict[str, Any]:
        snapshot = self.ledger.snapshot()
        rows = snapshot.get('requests', [])
        domains={name:sum(str(row.get('error','')).startswith(name+':') for row in rows) for name in ('provider','credential','protocol','broker')}
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


# Tool-call identifiers this broker has handed to the Candidate. The Candidate
# normalises them (sha1(id)[:9] in the product under test) and sends the
# normalised form back, which the provider refuses because it never issued it --
# reporting it, confusingly, as a missing reasoning_text. Remembering what was
# issued lets the original be restored on the way out.
ISSUED_CALL_IDS: "OrderedDict[str, None]" = OrderedDict()
ISSUED_CALL_IDS_LIMIT = 4096


def remember_call_id(call_id: str) -> None:
    if not isinstance(call_id, str) or not call_id:
        return
    ISSUED_CALL_IDS[call_id] = None
    while len(ISSUED_CALL_IDS) > ISSUED_CALL_IDS_LIMIT:
        ISSUED_CALL_IDS.popitem(last=False)


def original_call_id(candidate_id: Any) -> Any:
    """The provider id this one was derived from, or the id unchanged.

    Verification, not inference: a candidate id is accepted only when it is a
    prefix of exactly one remembered id's sha1, sha256 or md5. Content matching
    was rejected because (name, arguments) repeats across turns.
    """
    if not isinstance(candidate_id, str) or candidate_id in ISSUED_CALL_IDS:
        return candidate_id
    if len(candidate_id) < 8:
        return candidate_id
    matches = set()
    for issued in ISSUED_CALL_IDS:
        for algorithm in ("sha1", "sha256", "md5"):
            if hashlib.new(algorithm, issued.encode()).hexdigest().startswith(candidate_id):
                matches.add(issued)
                break
    if len(matches) == 1:
        return matches.pop()
    return candidate_id


def chat_to_responses(request: dict[str, Any], *, effort: str = EFFORT) -> dict[str, Any]:
    converted = {"model": MODEL, "store": False, "stream": False}
    messages = request.get("messages")
    if isinstance(messages, list):
        inputs=[]
        for message in messages:
            if not isinstance(message,dict): raise ValueError('chat messages must be objects')
            role=message.get('role');content=message.get('content')
            if role=='tool':
                # Restored together with the matching function_call below, or the
                # provider sees an output paired to a call it cannot find.
                inputs.append({'type':'function_call_output','call_id':original_call_id(message.get('tool_call_id')),'output':content if isinstance(content,str) else json.dumps(content)})
            else:
                if content: inputs.append({'role':role,'content':content})
                for call in message.get('tool_calls',[]):
                    fn=call.get('function',{});inputs.append({'type':'function_call','call_id':original_call_id(call.get('id')),'name':fn.get('name'),'arguments':fn.get('arguments','{}')})
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
            remember_call_id(tool_calls[-1]["id"])
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    # A budget-truncated answer must not be indistinguishable from a normal stop
    # on the Chat Completions surface: every OpenAI-compatible client, this one
    # included, keys its own continuation/recovery path on finish_reason
    # "length".  The Responses surface already carries status/incomplete_details
    # and DeepCode maps it itself (core/providers/openai_responses/parsing.py:19),
    # so this is the chat path catching up with what the wire already said.
    finish = "length" if output_budget_truncated(response) else ("tool_calls" if tool_calls else "stop")
    return {"id": response.get("id", "deepcode-response"), "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens":response.get("usage",{}).get("input_tokens",0),
                "completion_tokens":response.get("usage",{}).get("output_tokens",0),
                "total_tokens":response.get("usage",{}).get("total_tokens",0)}}


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepCodeAgentLoopBroker/1"

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
        # DeepCode's generic OpenAI-compatible provider uses Chat Completions
        # for a non-direct custom endpoint, while Codex-style clients use
        # Responses. Both are evaluator-owned transports and must be counted.
        if self.path.rstrip("/") not in {"/v1/responses", "/v1/chat/completions"}:
            self._json(404, {"error": "not_found"}); return
        request_id=None
        submitted=False
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
            base_request_id=hashlib.sha256(context_id.encode()+b'\0'+data).hexdigest()
            request_id=base_request_id
            truncated_attempt=0
            while True:
                prior=self.state.ledger.reserve(request_id,context_id=context_id)
                if prior is None:
                    break
                completed=self.state.ledger.completed_response(prior)
                if completed is None:
                    self._json(409,{'error':{'type':'prior_request_result_unknown'},'retry_allowed':False,'request_sha256':request_id});return
                if not prior.get('output_budget_truncated') or truncated_attempt>=TRUNCATED_RETRY_MAX:
                    # Unchanged for every completed answer, and the last resort for
                    # a truncated one: the Candidate still gets the bytes it is
                    # owed instead of an error it cannot act on.
                    self._deliver(completed,stream_requested=stream_requested);return
                truncated_attempt+=1
                request_id=hashlib.sha256(base_request_id.encode()+b'\0truncated-retry\0'
                                          +str(truncated_attempt).encode()).hexdigest()
            headers={'Content-Type':'application/json','Accept':'text/event-stream','User-Agent':'AgentSWE-DeepCode-AgentLoop/2.0','Authorization':f'Bearer {self.state.credential or ""}'}
            req=urllib.request.Request(self.state.upstream+upstream_path,data=data,headers=headers,method='POST')
            def request_started():
                nonlocal submitted
                self.state.ledger.transport_started(request_id)
                submitted=True
            capture_dir=None
            if self.state.ledger.path:
                capture_dir=self.state.ledger.path.parent/(self.state.ledger.path.stem+'-raw');capture_dir.mkdir(exist_ok=True)
            unsent_attempts=0
            while True:
                capture_handle=None;capture=None
                if capture_dir is not None:
                    name=request_id+('' if not unsent_attempts else '.unsent%03d'%unsent_attempts)
                    capture_handle=(capture_dir/(name+'.bin')).open('xb');capture=RedactedCapture(capture_handle,self.state.credential)
                try:
                    with direct_opener(on_request_start=request_started).open(req,timeout=MAX_CALL_SECONDS) as response:
                        raw,streamed=read_response(response.read1,content_type=response.headers.get('Content-Type',''),is_success=True,capture=capture.write if capture else None)
                        parsed=streamed if streamed is not None else strict_json(raw)
                    break
                except (urllib.error.URLError,OSError) as exc:
                    if (submitted or isinstance(exc,urllib.error.HTTPError)
                            or unsent_attempts>=UNSENT_MAX_RETRIES):
                        raise
                    unsent_attempts+=1
                    self.state.record_unsent_retry(type(exc).__name__)
                    time.sleep(UNSENT_RETRY_BACKOFF_SECONDS)
                finally:
                    if capture:capture.finish()
                    if capture_handle:capture_handle.close()
            # A response the provider finished inside the budget the Candidate
            # itself asked for still reaches the Candidate: HTTP 200, its own id,
            # complete usage, no error -- what a real API client receives and
            # handles. Every other non-completed status still fails.  It is NOT
            # booked as a delivered answer, though: the Candidate asked for 8192
            # without knowing the evaluator would force reasoning effort "high" on
            # top of it, so the ceiling it hit is ours.  It is recorded
            # delivered_answer=False, counted as an output_budget_truncation, and
            # never replayed to the retry that follows it (TRUNCATED_RETRY_MAX).
            # The transport counters (successful_calls, upstream_completion) stay
            # as they were, because the shared usage normalizers pin them; see
            # request_ledger.Stats.record.
            budget_limited=output_budget_truncated(parsed)
            if not isinstance(parsed,dict) or (parsed.get('status')!='completed' and not budget_limited) or not isinstance(parsed.get('id'),str) or not parsed['id']:
                self.state.record(ok=False,status=502,payload=parsed if isinstance(parsed,dict) else {},detail='response_not_typed_completed',failure_domain='provider',request_id=request_id)
                self._json(502,{'error':{'type':'response_not_typed_completed'},'retry_allowed':False});return
            self.state.record(ok=True,status=200,payload=parsed,request_id=request_id,truncated=budget_limited)
            self._deliver(parsed,stream_requested=stream_requested)
        except urllib.error.HTTPError as exc:
            # The provider's status and its own error body are the two facts that
            # separate "the evaluator built this request wrong" from "the
            # provider refused it", and both were being dropped: exc.code never
            # reached the ledger rows, and exc.read() was never called at all.
            # Three different mechanisms have been proposed for deepcode's
            # second-call failure and all three were refuted, because nothing on
            # disk says which one it is. Evidence only -- classification, retry
            # and the candidate-facing response are unchanged.
            try:
                upstream_body = exc.read().decode("utf-8", "replace")[:4000]
            except Exception:
                upstream_body = "(unreadable)"
            try:
                # Four reconstructions of this request all returned 200, so the
                # trigger is in the bytes nobody kept. Written beside the
                # success-path captures, under the same request sha, so a
                # failing call can be replayed exactly as it was sent.
                # Derived from the ledger path, not capture_dir, which is bound
                # inside the try and may not exist yet. Wrapped because evidence
                # must never be what makes an error handler raise.
                failed_dir = self.state.ledger.path.parent / (self.state.ledger.path.stem + "-raw")
                failed_dir.mkdir(exist_ok=True)
                (failed_dir / (str(request_id) + ".request.bin")).write_bytes(data or b"")
            except Exception:
                pass
            self.state.record(ok=False, status=exc.code, payload={}, request_id=request_id,
                              detail="%s: upstream_status=%s request_bytes=%s body=%s"
                                     % (type(exc).__name__, exc.code, len(data or b""),
                                        upstream_body),
                              failure_domain="provider" if submitted else "broker")
            self._json(502, {"error": {"type": "provider_failure", "message": "upstream provider rejected the evaluator broker request"}})
        except urllib.error.URLError as exc:
            self.state.record(ok=False, status=502, payload={}, detail=type(exc).__name__, failure_domain="provider" if submitted else "broker", request_id=request_id)
            self._json(502, {"error": {"type": "provider_failure", "message": "upstream provider transport failed"}})
        except (ValueError, json.JSONDecodeError) as exc:
            self.state.record(ok=False, status=400, payload={}, detail=type(exc).__name__, failure_domain="provider" if submitted else "protocol", request_id=request_id)
            self._json(400, {"error": {"type": "protocol_failure", "message": "invalid broker request"}})
        except OSError as exc:
            self.state.record(ok=False, status=502, payload={}, detail=type(exc).__name__, failure_domain="provider" if submitted else "broker", request_id=request_id)
            self._json(502, {"error": {"type": "broker_failure", "message": "evaluator broker failed"}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=0); parser.add_argument("--upstream", default=os.environ.get("AGENTSWE_UPSTREAM", "https://api.deepseek.com")); parser.add_argument("--credential-file"); parser.add_argument("--reasoning-effort", choices=sorted(ALLOWED_EFFORTS), default=EFFORT); parser.add_argument("--stats-file", type=Path); parser.add_argument("--dry-run", action="store_true"); args = parser.parse_args(argv)
    credential = None
    if args.credential_file:
        credential = load_credential(args.credential_file)
    elif os.environ.get("DEEPSEEK_API_KEY"):
        credential = os.environ["DEEPSEEK_API_KEY"]
    if not args.dry_run and not credential:
        raise RuntimeError("evaluator broker credential is unavailable")
    if not args.dry_run and not args.stats_file: raise RuntimeError("real lower broker requires a durable evaluator-owned --stats-file")
    state = BrokerState(args.upstream, credential, args.dry_run, effort=args.reasoning_effort, stats_file=args.stats_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler); server.state = state  # type: ignore[attr-defined]
    print(json.dumps({"port": server.server_port, "model": MODEL, "reasoning_effort": state.effort}), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: return 0
    finally: server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
