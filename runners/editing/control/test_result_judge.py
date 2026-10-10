#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("result_judge.py")
SPEC = importlib.util.spec_from_file_location("result_judge", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> int:
    valid = {
        "case_id": "test_001",
        "result_state": "scoreable",
        "result_score": 63,
        "task_completion": {"score": 31, "max": 50, "evidence": "completed the core task"},
        "evidence_grounding": {"score": 20, "max": 30, "evidence": "bound claims to receipts"},
        "recovery_and_safety": {"score": 12, "max": 20, "evidence": "avoided duplicate effects"},
        "major_errors": ["missed one optional verification"],
        "assessment": "partially successful",
    }
    verified, errors = MODULE.validate_response(json.dumps(valid), "test_001")
    assert not errors and verified["result_score"] == 63
    invalid = dict(valid)
    invalid["result_score"] = 99
    _, errors = MODULE.validate_response(json.dumps(invalid), "test_001")
    assert errors
    assert MODULE.MODEL == "deepseek-flash"
    assert MODULE.REASONING_EFFORT == "max"
    requests_seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests_seen.append(payload)
            body = json.dumps({"status": "completed", "model": "deepseek-flash", "id": "mock-response",
                               "output_text": json.dumps(valid), "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("task.md", "rubric.md", "artifact.json", "trajectory.json", "native.json", "oracle.json"):
                (root / name).write_text("{}" if name.endswith(".json") else "test", encoding="utf-8")
            completed = subprocess.run([
                sys.executable, str(MODULE_PATH),
                "--case-id", "test_001",
                "--task-input", str(root / "task.md"),
                "--rubric", str(root / "rubric.md"),
                "--agent-artifact", str(root / "artifact.json"),
                "--trajectory", str(root / "trajectory.json"),
                "--native-evidence", str(root / "native.json"),
                "--oracle-summary", str(root / "oracle.json"),
                "--broker-endpoint", f"http://127.0.0.1:{server.server_port}/v1/responses",
                "--output-dir", str(root / "out"),
            ], text=True, capture_output=True, check=False)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            contract = json.loads((root / "out/result_score_contract.json").read_text())
            assert contract["provider_usage"]["logical_requests"] == 1
            assert contract["provider_usage"]["completed_responses"] == 1
            assert contract["provider_usage"]["transport_attempts"] == 1
            assert contract["judge"]["model"] == "deepseek-flash"
            assert contract["judge"]["reasoning_effort"] == "max"
            assert contract["judge"]["credential_boundary"] == "real credential held only by evaluator-owned broker"
            assert len(requests_seen) == 1
            assert requests_seen[0]["model"] == "deepseek-flash"
            assert requests_seen[0]["reasoning"] == {"effort": "max"}
    finally:
        server.shutdown()
        server.server_close()
    print("PASS result_judge strict contract; mock_logical_requests=1; external_provider_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
