#!/usr/bin/env python3
"""Evaluator-owned GUI fixture/browser sidecar and narrow Candidate gateway."""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import http.server
import importlib.util
import json
import os
import secrets
import select
import signal
import subprocess
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


MAX_BODY = 64 * 1024
ALLOWED = {("GET", "/health"), ("GET", "/v1/observe"), ("POST", "/v1/action")}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def namespace(name: str) -> str:
    try:
        return os.readlink(f"/proc/self/ns/{name}")
    except OSError as exc:
        return f"unavailable:{type(exc).__name__}"


def import_harness(path: Path):
    spec = importlib.util.spec_from_file_location("trusted_gui_harness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load trusted GUI harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readiness(process: subprocess.Popen[str], timeout: float = 30) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("process stdout unavailable")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError(f"process readiness timeout; exit={process.poll()}")
    value = json.loads(process.stdout.readline())
    if not isinstance(value, dict) or value.get("ready") is not True:
        raise RuntimeError(f"process failed readiness: {value!r}")
    return value


class Gateway:
    def __init__(self, backend: str, backend_token: str, capability: str, audit: Path, case_id: str):
        parsed = urlsplit(backend)
        self.backend_host, self.backend_port = parsed.hostname, parsed.port
        self.backend_token, self.capability, self.audit, self.case_id = backend_token, capability, audit, case_id
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "EvaluatorGuiGateway/1"

            def log_message(self, *_):
                pass

            def send_value(self, status: int, body: bytes):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_allowed(self):
                started, path = now(), urlsplit(self.path).path
                evaluator_header = any(key.lower() == "x-evaluator-token" for key in self.headers)
                protected = path in {"/record", "/__evaluator__/state"} or path.startswith("/__evaluator__/")
                authorized = secrets.compare_digest(self.headers.get("X-GUI-Control-Token", ""), outer.capability)
                allowed = (self.command, path) in ALLOWED
                status, response_body = 403, b'{"error":"forbidden"}'
                request_body = b""
                try:
                    declared = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > MAX_BODY:
                    status, response_body = 413, b'{"error":"body outside limit"}'
                elif authorized and allowed:
                    request_body = self.rfile.read(declared) if declared else b""
                    connection = http.client.HTTPConnection(outer.backend_host, outer.backend_port, timeout=15)
                    try:
                        headers = {"X-GUI-Control-Token": outer.backend_token}
                        if request_body:
                            headers["Content-Type"] = "application/json"
                        connection.request(self.command, path, body=request_body or None, headers=headers)
                        response = connection.getresponse()
                        status, response_body = response.status, response.read(16 * 1024 * 1024)
                    finally:
                        connection.close()
                elif authorized:
                    status, response_body = 404, b'{"error":"operation not exposed"}'
                self.send_value(status, response_body)
                row = {
                    "timestamp_utc": started, "method": self.command, "path": path,
                    "status": status, "authorized": authorized, "allowed_operation": allowed,
                    "request_bytes": len(request_body), "response_bytes": len(response_body),
                    "protected_bridge_attempt": protected,
                    "evaluator_header_present": evaluator_header,
                }
                with outer.lock, outer.audit.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

            do_GET = handle_allowed
            do_POST = handle_allowed
            do_PUT = handle_allowed
            do_PATCH = handle_allowed
            do_DELETE = handle_allowed

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def summarize_gateway(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "request_count": len(rows), "methods": dict(Counter(row["method"] for row in rows)),
        "statuses": dict(Counter(str(row["status"]) for row in rows)),
        "protected_bridge_attempts": sum(bool(row["protected_bridge_attempt"]) for row in rows),
        "evaluator_header_attempts": sum(bool(row["evaluator_header_present"]) for row in rows),
        "unsupported_operation_attempts": sum(not bool(row["allowed_operation"]) for row in rows),
        "unauthorized_attempts": sum(not bool(row["authorized"]) for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--harness", required=True, type=Path)
    parser.add_argument("--browser-control", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--candidate-output", required=True, type=Path)
    args = parser.parse_args()
    evidence, control = args.evidence, args.control
    evidence.mkdir(parents=True, exist_ok=True)
    control.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence, 0o700)
    os.chmod(control, 0o700)
    gateway_audit = evidence / "audit/gateway_requests.jsonl"
    fixture_audit = evidence / "audit/fixture_requests.jsonl"
    gateway_audit.parent.mkdir(parents=True, exist_ok=True)
    gateway_audit.write_text("", encoding="utf-8")
    fixture_audit.write_text("", encoding="utf-8")
    harness = import_harness(args.harness)
    fixture = browser = None
    proxy = gateway = None
    backend_url = evaluator_token = browser_session = ""
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        case_dirs = [path for path in args.case_dir.iterdir() if path.is_dir()]
        if len(case_dirs) != 1:
            raise RuntimeError(f"GUI sidecar expected one active case, found {len(case_dirs)}")
        case_dir = case_dirs[0]
        fixture_script = case_dir / "assets/serve.py"
        source_input = case_dir / "input.md"
        viewport = harness.request_viewport(source_input)
        evaluator_token, browser_session = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        fixture = subprocess.Popen([
            os.sys.executable, str(fixture_script), "--host", "127.0.0.1", "--port", "0",
            "--evaluator-token", evaluator_token, "--browser-session", browser_session,
        ], cwd=case_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
           start_new_session=True, env=harness.isolated_environment(Path(os.sys.executable), evidence / "runtime/fixture"))
        fixture_info = readiness(fixture, 20)
        backend_url, case_id = str(fixture_info["url"]), str(fixture_info["case_id"])
        harness.wait_for_health(backend_url, case_id)
        proxy = harness.AuditProxy(backend_url, fixture_audit, browser_session)
        proxy.start()
        browser = subprocess.Popen([
            os.sys.executable, str(args.browser_control), "--fixture-url", proxy.url,
            "--case-id", case_id, "--viewport", json.dumps(viewport),
            "--evidence", str(evidence / "browser"), "--browser-session", browser_session,
        ], cwd=args.browser_control.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           text=True, start_new_session=True,
           env=harness.isolated_environment(Path(os.sys.executable), evidence / "runtime/browser"))
        browser_info = readiness(browser, 45)
        if browser_info.get("protocol") != "evaluator-controlled-browser-v1":
            raise RuntimeError("browser protocol identity mismatch")
        capability = secrets.token_urlsafe(32)
        gateway = Gateway(str(browser_info["url"]), str(browser_info["token"]), capability, gateway_audit, case_id)
        gateway.start()
        atomic_json(control / "gui-capability.json", {
            "protocol": "evaluator-controlled-browser-v1", "url": "http://gui-control:8765", "token": capability,
        })
        initial = harness.sanitize_bridge(harness.bridge_state(backend_url, evaluator_token))
        atomic_json(evidence / "audit/initial_state.json", initial)
        atomic_json(evidence / "process/gui_namespace.json", {
            "pid_namespace": namespace("pid"), "mount_namespace": namespace("mnt"),
            "network_namespace": namespace("net"), "fixture_listen_scope": "container-loopback-only",
            "candidate_gateway": "0.0.0.0:8765", "browser_debug_endpoint_exposed": False,
            "evaluator_token_exposed": False,
        })
        atomic_json(evidence / "ready.json", {
            "ready": True, "protocol": "evaluator-controlled-browser-v1", "case_id": case_id,
            "viewport": viewport, "started_at_utc": now(),
        }, mode=0o644)

        finalize = evidence / "finalize.request.json"
        while not stopping and not finalize.is_file():
            if browser.poll() is not None or fixture.poll() is not None:
                raise RuntimeError("trusted GUI process exited during Candidate execution")
            time.sleep(0.05)
        if stopping:
            return 0
        request = json.loads((control / "request.json").read_text(encoding="utf-8"))
        runner = json.loads((control / "result.json").read_text(encoding="utf-8"))
        final = harness.sanitize_bridge(harness.bridge_state(backend_url, evaluator_token))
        atomic_json(evidence / "audit/final_state.json", final)
        artifacts = harness.inspect_artifacts(args.candidate_output, case_id, viewport)
        atomic_json(evidence / "artifact_validation.json", artifacts)
        terminate(browser)
        browser = None
        fixture_rows = [json.loads(line) for line in fixture_audit.read_text().splitlines() if line.strip()]
        summary = summarize_gateway(gateway_audit)
        summary.update({
            "fixture_request_count": len(fixture_rows),
            "fixture_methods": dict(Counter(row["method"] for row in fixture_rows)),
            "fixture_statuses": dict(Counter(str(row["status"]) for row in fixture_rows)),
        })
        atomic_json(evidence / "audit/request_summary.json", summary)
        execution = {
            **runner, "harness_version": "compose-isolated-gui-v1", "case_id": case_id,
            "case_digest": request.get("case_digest"), "candidate_digest": request.get("candidate_digest"),
            "fixture_source_sha256": harness.sha256_file(fixture_script),
            "fixture_html_sha256": harness.sha256_file(case_dir / "assets/index.html"),
            "browser_control_protocol": "evaluator-controlled-browser-v1",
            "browser_control_url_exposed": True, "browser_control_token_exposed": True,
            "evaluator_bridge_token_exposed": False, "state_provenance": "evaluator-controlled-browser-v1",
            "isolation_pending": False, "browser_evidence_dir": str(evidence / "browser"),
            "candidate_declared_provider_usage": harness.declared_provider_usage(args.candidate_output),
            "candidate_pid_namespace": runner.get("runner_pid_namespace"),
            "gui_pid_namespace": namespace("pid"),
            "pid_namespace_separate": runner.get("runner_pid_namespace") != namespace("pid"),
            "candidate_mount_namespace": runner.get("runner_mount_namespace"),
            "gui_mount_namespace": namespace("mnt"),
            "mount_namespace_separate": runner.get("runner_mount_namespace") != namespace("mnt"),
            "candidate_network_namespace": runner.get("runner_network_namespace"),
            "gui_network_namespace": namespace("net"),
            "network_namespace_separate": runner.get("runner_network_namespace") != namespace("net"),
        }
        if not all(execution[key] for key in ("pid_namespace_separate", "mount_namespace_separate", "network_namespace_separate")):
            execution["infrastructure_error"] = "Candidate and trusted GUI namespaces are not separate"
        atomic_json(evidence / "execution.json", execution)
        atomic_json(evidence / "active_case_manifest.json", {
            "case_id": case_id, "source_sha256": request.get("source_input_sha256"),
            "staged_sha256": request.get("staged_input_sha256"),
            "application_links_rewritten": request.get("application_links_rewritten"),
            "copied_supporting_assets": [], "excluded_fixture_assets": ["assets/serve.py", "assets/index.html"],
            "isolation_note": "Candidate ran in a separate Compose service with no active-case or evidence mount.",
        })
        atomic_json(evidence / "partial_state_diagnostic.json", harness.partial_diagnostic(initial, final, execution, artifacts))
        atomic_json(evidence / "finalized.json", {
            "finalized": True, "case_id": case_id, "request_id": request.get("request_id"),
            "finished_at_utc": now(),
        }, mode=0o644)
        gateway.close()
        gateway = None
        while not stopping:
            time.sleep(0.2)
        return 0
    except Exception as exc:
        atomic_json(evidence / "infrastructure_error.json", {
            "evaluation_state": "infrastructure_error", "score_publishable": False,
            "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
            "occurred_at_utc": now(),
        }, mode=0o644)
        return 70
    finally:
        if gateway is not None:
            gateway.close()
        terminate(browser)
        if proxy is not None:
            proxy.close()
        terminate(fixture)


if __name__ == "__main__":
    raise SystemExit(main())
