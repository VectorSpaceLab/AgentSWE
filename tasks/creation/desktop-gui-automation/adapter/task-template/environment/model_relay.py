#!/usr/bin/env python3
"""Non-general HTTP relay from the isolated Candidate network to its broker."""
from __future__ import annotations

import http.client
import http.server
import json
import os
from urllib.parse import urlsplit


MAX_BODY = 16 * 1024 * 1024
TARGET_HOST = os.environ.get("BROKER_TARGET_HOST", "172.17.0.1")
TARGET_PORT = int(os.environ.get("BROKER_TARGET_PORT", "18080"))
ALLOWED_PATH = os.environ.get("RELAY_ALLOWED_PATH", "/v1/responses")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CandidateModelRelay/1"

    def log_message(self, *_):
        pass

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            self.send_json(200, {"ready": True})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != ALLOWED_PATH or ".." in path:
            self.send_json(404, {"error": "only the provisioned Responses route is available"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.send_json(413, {"error": "request body outside relay limit"})
            return
        body = self.rfile.read(length)
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        connection = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT, timeout=660)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(MAX_BODY + 1)
            if len(payload) > MAX_BODY:
                raise RuntimeError("broker response exceeds relay limit")
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in {"content-type", "request-id", "x-request-id"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            self.send_json(502, {"error": f"broker transport failed: {type(exc).__name__}"})
        finally:
            connection.close()


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
