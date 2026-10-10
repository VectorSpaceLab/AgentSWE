#!/usr/bin/env python3
"""Evaluator-owned Responses broker locked to deepseek-flash/xhigh.

This process is used only by the upper Builder.  The provider credential is
read by the broker and is never returned through health, stats, logs, or the
Builder environment.  Runtime clients authenticate with the fixed
``broker-only-placeholder`` token.
"""
from __future__ import annotations

import argparse
import http.server
import hashlib
from builder_request_ledger import RequestLedger
from responses_stream import direct_opener, read_response, strict_json, RedactedCapture
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MODEL = "deepseek-flash"
EFFORT = "max"
PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
MAX_BODY = 8 * 1024 * 1024
MAX_ATTEMPTS = 1
KEEPALIVE_SECONDS = 5.0
MAX_STREAM_SECONDS = 600.0
MAX_CALL_SECONDS = 1200.0
ERROR_SAMPLE_SECONDS = 2.0
ERROR_SAMPLE_BYTES = 32768


def provider_key(path: Path) -> str:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY"}:
            values[key.strip()] = value.strip().strip("'\"")
    # This evaluator deployment's proven Responses credential is DEEPSEEK_API_KEY.
    # Other legacy keys can coexist in the shared env file, so never select
    # them ahead of the canonical GATEWAY credential.
    for key in ("DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY", "OPENAI_API_KEY"):
        if values.get(key):
            return values[key]
    raise RuntimeError("credential file has no supported provider key")


def response_text(value: dict[str, Any]) -> str:
    direct = value.get("output_text")
    if isinstance(direct, str):
        return direct
    chunks: list[str] = []
    for item in value.get("output", []) if isinstance(value.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def chat_to_responses(messages: object) -> list[dict[str, object]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    result: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}:
            raise ValueError("invalid chat message")
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError("chat message content must be non-empty text")
        role = str(message["role"])
        result.append({"type": "message", "role": role, "content": [{
            "type": "output_text" if role == "assistant" else "input_text", "text": content,
        }]})
    return result


class State:
    def __init__(self, stats_path: Path | None = None) -> None:
        self.lock = threading.Lock()
        self.stats_path = stats_path
        self.calls = 0
        self.completed_calls = 0
        self.successful_calls = 0
        self.failures = 0
        self.provider_failures = 0
        self.protocol_failures = 0
        self.delivery_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.upstream_status_counts: dict[str, int] = {}
        self.actual_upstream_requests = 0
        self.unknown_usage_requests = 0
        self.cache_queries = 0
        self.in_flight_upstream_requests = 0
        if stats_path and stats_path.exists():
            old = strict_json(stats_path.read_bytes())
            if old.get('schema_version') != 'agentswe-builder-broker-stats/v2':
                raise ValueError('legacy Builder state requires explicit audit; refusing to reset it')
            runtime = old['runtime']
            for name in ('calls','completed_calls','successful_calls','failures','provider_failures',
                         'protocol_failures','delivery_failures','input_tokens','output_tokens','total_tokens',
                         'actual_upstream_requests','unknown_usage_requests','cache_queries','in_flight_upstream_requests'):
                setattr(self,name,runtime.get('known_'+name,runtime.get(name,0)))
            self.upstream_status_counts = old['upstream']['status_counts']
        with self.lock:
            self._persist_locked()

    def _value_locked(self) -> dict[str, Any]:
        usage_complete = self.unknown_usage_requests + self.in_flight_upstream_requests == 0
        return {
            "schema_version": "agentswe-builder-broker-stats/v2",
            "protocol": {"model": MODEL, "reasoning_effort": EFFORT, "max_upstream_attempts_per_identity": 1, "unknown_requests_replayed": False},
            "runtime": {
                "calls": self.calls, "failures": self.failures,
                "actual_upstream_requests": self.actual_upstream_requests,
                "unknown_usage_requests": self.unknown_usage_requests,
                "cache_queries": self.cache_queries,
                "usage_complete": usage_complete,
                "in_flight_upstream_requests": self.in_flight_upstream_requests,
                "known_total_tokens": self.total_tokens,
                "known_input_tokens": self.input_tokens,
                "known_output_tokens": self.output_tokens,
                "completed_calls": self.completed_calls,
                "successful_calls": self.successful_calls,
                "in_flight_calls": max(0, self.calls - self.completed_calls),
                "tokens": self.total_tokens if usage_complete else None,
                "provider_failures": self.provider_failures,
                "protocol_failures": self.protocol_failures,
                "delivery_failures": self.delivery_failures,
                "input_tokens": self.input_tokens if usage_complete else None,
                "output_tokens": self.output_tokens if usage_complete else None,
                "total_tokens": self.total_tokens if usage_complete else None,
            },
            "upstream": {"status_counts": dict(sorted(self.upstream_status_counts.items()))},
            "credential": {"builder_visible": PLACEHOLDER, "provider_secret_logged": False,
                           "credential_value_recorded": False},
        }

    def _persist_locked(self) -> None:
        if self.stats_path is None:
            return
        try:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.stats_path.with_suffix(self.stats_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._value_locked(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.stats_path)
        except OSError:
            # Evidence persistence must not turn an otherwise reachable
            # provider response into a protocol/provider failure. The
            # evaluator will fail closed later if the stats file is absent.
            raise

    def reserve(self) -> None:
        with self.lock:
            self.calls += 1
            self._persist_locked()

    def finish(self, *, failure: str | None, status: int | None = None,
               usage: dict[str, Any] | None = None, request_sent: bool = False) -> None:
        with self.lock:
            self.completed_calls += 1
            if failure:
                self.failures += 1
                if failure == "provider":
                    self.provider_failures += 1
                elif failure == "protocol":
                    self.protocol_failures += 1
                else:
                    self.delivery_failures += 1
            else:
                self.successful_calls += 1
            if status is not None:
                key = str(status)
                self.upstream_status_counts[key] = self.upstream_status_counts.get(key, 0) + 1
            if request_sent:
                self.in_flight_upstream_requests = max(0,self.in_flight_upstream_requests-1)
            if request_sent and not (isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens','total_tokens'))):
                self.unknown_usage_requests += 1
            usage = usage or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)
            self._persist_locked()

    def record_upstream_start(self) -> None:
        with self.lock:
            self.actual_upstream_requests += 1
            self.in_flight_upstream_requests += 1
            self._persist_locked()

    def reconcile(self, ledger) -> None:
        """Recover immutable requests after a process interruption; never resend."""
        summary = ledger.recover()
        with self.lock:
            self.calls = summary['calls'] + self.protocol_failures
            self.completed_calls = self.calls
            self.successful_calls = summary['completed']
            self.provider_failures = summary['failed']
            self.failures = self.provider_failures + self.protocol_failures + self.delivery_failures
            self.actual_upstream_requests = summary['sent']
            self.unknown_usage_requests = summary['unknown_usage']
            self.in_flight_upstream_requests = 0
            for name in ('input_tokens','output_tokens','total_tokens'):
                setattr(self,name,summary[name])
            self._persist_locked()

    def record_cache_query(self) -> None:
        with self.lock:
            self.cache_queries += 1
            self._persist_locked()

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return self._value_locked()

    def record_delivery_failure(self) -> None:
        """Record a broken client connection without double-counting a call."""
        with self.lock:
            self.delivery_failures += 1
            self.failures += 1
            self._persist_locked()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agentswe-claude-builder-broker/1"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "status": "ok", "model": MODEL, "reasoning_effort": EFFORT, "protocol": "agentswe-builder-single-upstream/v1"})
        elif self.path == "/stats":
            if self.headers.get("Authorization") != f"Bearer {STATS_TOKEN}":
                self.send_json(401, {"error": "stats auth"})
            else:
                self.send_json(200, self.state.stats())
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in {"/v1/responses", "/v1/chat/completions"}:
            self.send_json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {PLACEHOLDER}":
            self.send_json(401, {"error": "Builder must use placeholder credential"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("invalid request length")
            body = strict_json(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request is not an object")
            chat_mode = path == "/v1/chat/completions"
            if chat_mode:
                body["input"] = chat_to_responses(body.pop("messages"))
                body.pop("stream", None)
            body["model"] = MODEL
            body["reasoning"] = {"effort": EFFORT}
            body.pop("max_tokens", None)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.state.reserve()
            self.state.finish(failure="protocol")
            self.send_json(400, {"error": "broker_protocol_failure", "type": type(exc).__name__})
            return

        ledger = self.server.ledger
        try:
            identity, cached, error = ledger.claim(body, path, self.server.provider_url)
        except (ValueError, OSError) as exc:
            self.send_json(409, {"error": "builder_request_identity_failure", "type": type(exc).__name__})
            return
        if error or cached:
            self.state.record_cache_query()
            if error:
                self.send_json(409, {"error": error, "automatic_retry_allowed": False})
                return
            try:
                self.send_response(cached['status'])
                self.send_header('Content-Type',cached['content_type'])
                self.send_header('Content-Length',str(len(cached['payload'])))
                self.end_headers();self.wfile.write(cached['payload'])
            except OSError:
                self.state.record_delivery_failure()
            return
        self.state.reserve()
        request = urllib.request.Request(self.server.provider_url,
            data=json.dumps(body,ensure_ascii=False).encode(),
            headers={'Authorization':f'Bearer {self.server.provider_key}',
                     'Content-Type':'application/json','Accept':'text/event-stream, application/json'},method='POST')
        result = {}
        complete = threading.Event()
        phase_lock = threading.Lock()
        phase = {'closed': False, 'sent': False}
        deadline = time.monotonic() + MAX_CALL_SECONDS

        def fetch():
            response = None
            def started():
                with phase_lock:
                    if phase['closed'] or time.monotonic() >= deadline:
                        raise TimeoutError('builder_request_deadline_before_send')
                    ledger.sent(identity)
                    phase['sent'] = True
                    self.state.record_upstream_start()
            try:
                response = direct_opener(on_request_start=started).open(request,timeout=MAX_CALL_SECONDS)
                with phase_lock:
                    if phase['closed']:
                        return
                parsed = {}; read_done = threading.Event()
                capture_file = (identity/'upstream.raw').open('xb')
                capture = RedactedCapture(capture_file,self.server.provider_key)
                def reader():
                    try:
                        raw, value = read_response(response.read1, content_type=response.headers.get('Content-Type',''),
                                                   is_success=True,capture=capture.write)
                        parsed.update(raw=raw, value=value if value is not None else strict_json(raw))
                    except Exception as exc:parsed['error']=type(exc).__name__
                    finally:
                        try:
                            capture.finish();capture_file.close()
                        finally:
                            read_done.set()
                threading.Thread(target=reader,daemon=True).start()
                if not read_done.wait(min(MAX_STREAM_SECONDS,max(0,deadline-time.monotonic()))):
                    raise TimeoutError('builder_response_deadline')
                if 'error' in parsed:raise ValueError('invalid_builder_response_'+parsed['error'])
                value=parsed['value']
                if not isinstance(value,dict) or value.get('object')!='response' or value.get('status')!='completed' or not isinstance(value.get('id'),str) or not value['id'] or not isinstance(value.get('output'),list):
                    raise ValueError('builder_response_not_typed_completed')
                if value.get('model',MODEL) != MODEL:
                    raise ValueError('builder_response_model_mismatch')
                usage=value.get('usage')
                usage = usage if isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens','total_tokens')) else None
                payload=parsed['raw'].replace(self.server.provider_key.encode(),b'[REDACTED]')
                content_type=response.headers.get('Content-Type','application/json')
                if chat_mode:
                    payload=json.dumps({'id':value['id'],'object':'chat.completion','created':int(time.time()),'model':MODEL,
                        'choices':[{'index':0,'message':{'role':'assistant','content':response_text(value)},'finish_reason':'stop'}],
                        'usage':usage}).encode().replace(self.server.provider_key.encode(),b'[REDACTED]')
                    content_type='application/json'
                with phase_lock:
                    if phase['closed']:
                        return
                    ledger.complete(identity,payload=payload,status=200,content_type=content_type,usage=usage)
                    phase['closed'] = True
                    result.update(payload=payload,status=200,content_type=content_type)
                    self.state.finish(failure=None,status=int(response.status),usage=usage,request_sent=phase['sent'])
            except Exception as exc:
                status=exc.code if isinstance(exc,urllib.error.HTTPError) else None
                error_sample = None
                if isinstance(exc, urllib.error.HTTPError):
                    # urllib raises before assigning ``response``. Own and
                    # close that connection too; retain only a bounded private
                    # diagnostic sample, never forward provider errors to the Builder.
                    response = exc
                    sampled = {}; sample_done = threading.Event()
                    def sample_error(error_response=exc):
                        try:
                            sampled['raw'] = error_response.read1(ERROR_SAMPLE_BYTES)
                        except Exception as read_error:
                            sampled['error'] = type(read_error).__name__
                        finally:
                            sample_done.set()
                    threading.Thread(target=sample_error, daemon=True).start()
                    done = sample_done.wait(min(ERROR_SAMPLE_SECONDS, max(0, deadline-time.monotonic())))
                    error_sample = {'http_status': status, 'diagnostic_only': True,
                                    'sample_completed': done,
                                    'sample_capacity_bytes': ERROR_SAMPLE_BYTES,
                                    'body_not_proof_of_zero_billing': True}
                    if done and 'raw' in sampled:
                        raw = sampled['raw']
                        # If capacity ended inside an echoed key, discard the
                        # suffix before redaction rather than retaining a key prefix.
                        if len(raw) == ERROR_SAMPLE_BYTES:
                            raw = raw[:-len(self.server.provider_key.encode())]
                            error_sample['capacity_reached'] = True
                        error_sample['sample'] = raw.replace(self.server.provider_key.encode(), b'[REDACTED]')
                    elif done:
                        error_sample['read_error'] = sampled.get('error')
                with phase_lock:
                    if not phase['closed']:
                        if error_sample is not None:
                            try:
                                sample = error_sample.pop('sample', None)
                                if sample is not None:
                                    with (identity/'upstream_error_sample.redacted').open('xb') as handle:
                                        handle.write(sample)
                                    error_sample['sample_sha256'] = hashlib.sha256(sample).hexdigest()
                                    error_sample['sample_bytes'] = len(sample)
                                with (identity/'upstream_error_sample.json').open('x') as handle:
                                    json.dump(error_sample, handle, sort_keys=True)
                            except OSError:
                                # Auxiliary evidence failure cannot suppress
                                # the original provider failure or leave a
                                # completed HTTP transaction marked in-flight.
                                pass
                        fail(type(exc).__name__,status)
            finally:
                if response is not None:
                    threading.Thread(target=response.close,daemon=True).start()
                complete.set()

        def fail(cause,status):
            # Called under phase_lock. A timed-out worker may finish later but
            # cannot overwrite the outcome, send after cancellation or retry.
            phase['closed'] = True
            ledger.fail(identity,error=cause,status=status,sent=phase['sent'])
            self.state.finish(failure='provider',status=status,request_sent=phase['sent'])
            result.update(payload=json.dumps({'error':{'type':'builder_provider_outcome_unknown' if phase['sent'] else 'builder_provider_not_sent',
                'cause':cause,'automatic_retry_allowed':False}}).encode(),status=409,content_type='application/json')

        threading.Thread(target=fetch,daemon=True).start()
        connected=True
        while not complete.wait(min(KEEPALIVE_SECONDS,max(0,deadline-time.monotonic()))):
            if time.monotonic() >= deadline:
                with phase_lock:
                    if not phase['closed']:
                        fail('AbsoluteDeadlineExceeded',None)
                break
            if connected:
                try:
                    self.send_response_only(100);self.end_headers();self.wfile.flush()
                except OSError:connected=False
        payload=result.get('payload',b'{"error":"builder_request_evidence_failure"}')
        response_status=result.get('status',409)
        content_type=result.get('content_type','application/json')
        if not connected:
            self.state.record_delivery_failure()
            return
        try:
            self.send_response(response_status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.state.record_delivery_failure()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--provider-url", default=os.environ.get("AGENTSWE_PROVIDER_URL", "https://api.deepseek.com/v1/responses"))
    # Accepted for compatibility with the generic evaluator broker launcher.
    # The Builder broker remains hard-locked to xhigh and does not use runtime
    # budgets or caller-supplied role/effort values.
    parser.add_argument("--max-runtime-calls", type=int, default=0)
    parser.add_argument("--max-runtime-tokens", type=int, default=0)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--role", default="builder")
    parser.add_argument("--reasoning-effort", default=EFFORT)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    server.ledger = RequestLedger(args.stats_file.resolve().parent / "builder_requests")
    server.state = State(args.stats_file.resolve())
    server.state.reconcile(server.ledger)
    server.provider_url = args.provider_url  # type: ignore[attr-defined]
    server.provider_key = provider_key(args.credential_file.resolve())  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "model": MODEL, "reasoning_effort": EFFORT,
                      "bind": args.bind, "port": args.port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        server.ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
