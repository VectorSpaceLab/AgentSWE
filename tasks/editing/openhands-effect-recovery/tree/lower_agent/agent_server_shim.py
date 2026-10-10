#!/usr/bin/env python3
"""Evaluator-owned local Agent Server boundary for the Canvas lower driver.

It exposes only a tiny conversation-like planning endpoint and delegates the
model request to the locked broker. It does not execute recovery actions or
contain any expected case answer.
"""
from __future__ import annotations
import argparse, json, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--bind",default="127.0.0.1"); ap.add_argument("--port",type=int,required=True); ap.add_argument("--broker",required=True); ap.add_argument("--evaluation-scope"); args=ap.parse_args()
    if args.evaluation_scope is not None and not re.fullmatch(r"[0-9a-f]{8,64}", args.evaluation_scope):
        raise SystemExit("evaluation scope must be an evaluator hex digest")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,_format:str,*_args:object)->None: return
        def send_json(self,code:int,value:object)->None:
            body=json.dumps(value,ensure_ascii=False).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self)->None:  # noqa: N802
            self.send_json(200,{"ok":True,"boundary":"openhands-agent-server-shim/v1"}) if self.path=="/healthz" else self.send_json(404,{"error":"not_found"})
        def do_POST(self)->None:  # noqa: N802
            if self.path!="/v1/plan": self.send_json(404,{"error":"not_found"}); return
            try:
                size=int(self.headers.get("Content-Length","0")); request=json.loads(self.rfile.read(size));
                payload={"model":"ignored-by-agent-server-shim","input":request.get("task",""),"metadata":request.get("metadata",{}),"reasoning":{"effort":"ignored-by-agent-server-shim"}}
                body = None
                last_error = None
                for attempt in range(3):
                    upstream=urllib.request.Request(args.broker,data=json.dumps(payload).encode(),method="POST",headers={"Content-Type":"application/json","Authorization":"Bearer broker-only-placeholder",
                             **({"X-AgentSWE-Evaluation":args.evaluation_scope} if args.evaluation_scope else {})})
                    try:
                        with urllib.request.urlopen(upstream,timeout=180) as response:
                            body=json.loads(response.read())
                        break
                    except urllib.error.HTTPError as exc:
                        last_error = exc
                        if exc.code not in {502, 503, 504} or attempt == 2:
                            raise
                        time.sleep(0.5 * (attempt + 1))
                if not isinstance(body, dict):
                    raise RuntimeError(f"provider returned no plan: {last_error}")
                self.send_json(200,{"schema_version":"agentswe-agent-server-plan/v1","output":body.get("output",[]),"output_text":body.get("output_text","")})
            except Exception as exc: self.send_json(502,{"error":"agent_server_provider_failure","type":type(exc).__name__})
    ThreadingHTTPServer((args.bind,args.port),Handler).serve_forever(); return 0

if __name__=="__main__": raise SystemExit(main())
