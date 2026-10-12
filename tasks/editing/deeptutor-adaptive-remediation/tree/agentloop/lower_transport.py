"""Evaluator-owned per-case fixed-endpoint relay for a networkless Candidate."""
from __future__ import annotations
import hashlib
import http.server
import json
from pathlib import Path
import shutil
import socketserver
import tempfile
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

class UnixHTTPRelay:
    def __init__(self, endpoint: str, *, context_id: str | None = None):
        parsed=urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1","localhost"} or parsed.path != "/v1/responses" or parsed.username or parsed.query:
            raise ValueError("relay target must be one evaluator-owned loopback Responses endpoint")
        self.endpoint=endpoint
        self.context_id=context_id or hashlib.sha256(str(self).encode()).hexdigest()
        self.temp=Path(tempfile.mkdtemp(prefix="agentswe-lower-uds-"))
        self.path=self.temp/"lower.sock"
        self.events=[]
        relay=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_args): pass
            def send(self,status,raw):
                self.send_response(status); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                if self.path != "/agentswe/transport-health": return self.send(404,b'{"error":"unsupported_endpoint"}')
                self.send(200,b'{"ok":true,"transport":"fixed-uds-responses","model":"deepseek-flash","reasoning_effort":"high"}')
            def do_POST(self):
                if self.path != "/v1/responses":
                    relay.events.append({"kind":"rejected_endpoint","path":self.path})
                    return self.send(404,b'{"error":"unsupported_endpoint"}')
                length=int(self.headers.get("Content-Length","0"))
                if not 0 < length <= 8_000_000: return self.send(413,b'{"error":"body_limit"}')
                body=self.rfile.read(length)
                upstream=None
                try:
                    value=json.loads(body)
                    if value.get("model") != "deepseek-flash" or value.get("reasoning",{}).get("effort") != "high":
                        raise ValueError("model/reasoning lock mismatch")
                    request=urllib.request.Request(relay.endpoint,data=body,method="POST",
                        headers={"Authorization":"Bearer broker-only-placeholder","Content-Type":"application/json","X-AgentSWE-Context":relay.context_id})
                    with urllib.request.urlopen(request,timeout=260) as response:
                        raw=response.read(); status=response.status
                    relay.events.append({"kind":"forwarded_response","status":status})
                    upstream={"upstream_status":status,"response_bytes":len(raw)}
                    self.send(status,raw)
                except urllib.error.HTTPError as exc:
                    relay.events.append({"kind":"broker_http_error","status":exc.code})
                    self.send(exc.code,exc.read())
                except Exception as exc:
                    event={"kind":"relay_error","error_type":type(exc).__name__}
                    if upstream is not None:
                        # The broker's whole response was already read: only the write back to the product failed.
                        event.update(phase="reply_write",**upstream)
                    relay.events.append(event)
                    self.send(502,json.dumps({"error":"evaluator_transport_failure","type":type(exc).__name__}).encode())
        class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
            daemon_threads=True
        self.server=Server(str(self.path),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        self.path.unlink(missing_ok=True)
        self.temp.rmdir()
