#!/usr/bin/env python3
"""Run one active GUI case and preserve evaluator-grade evidence.

This runner deliberately keeps the fixture source path and evaluator token out of
the candidate command and environment. It stages only the request text, proxies
the fixture origin, captures protected state before and after execution, and
writes diagnostics that do not change the benchmark's formal 100-point rubric.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from . import HARNESS_VERSION
except ImportError:
    HARNESS_VERSION = "2.1.0"


OWNED_ARTIFACTS = (
    "automation_result.json",
    "initial_state.png",
    "decisive_step.png",
    "final_state.png",
    "run_report.json",
)
PROVIDER_FIELDS = ("deepseek", "gateway", "gateway_image", "serper", "web_retrieval")
SENSITIVE_KEY = re.compile(r"(?:answer|verification.?code|evaluator.?token|secret|credential)", re.I)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_dotenv(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"GATEWAY_API_KEY", "SERPER_TOKEN"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def isolated_environment(
    python_bin: Path, runtime_root: Path, credentials: dict[str, str] | None = None
) -> dict[str, str]:
    directories = {
        "HOME": runtime_root / "home",
        "TMPDIR": runtime_root / "tmp",
        "XDG_CACHE_HOME": runtime_root / "cache",
        "XDG_CONFIG_HOME": runtime_root / "config",
        "XDG_DATA_HOME": runtime_root / "data",
        "PYTHONPYCACHEPREFIX": runtime_root / "pycache",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.pathsep.join(
            (str(python_bin.resolve().parent), "/usr/local/bin", "/usr/bin", "/bin")
        ),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "TZ": os.environ.get("TZ", "UTC"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        **{key: str(path) for key, path in directories.items()},
    }
    environment["TMP"] = environment["TMPDIR"]
    environment["TEMP"] = environment["TMPDIR"]
    if os.environ.get("SSL_CERT_FILE"):
        environment["SSL_CERT_FILE"] = os.environ["SSL_CERT_FILE"]
    for name in ('PLAYWRIGHT_BROWSERS_PATH', 'CHROMIUM', 'LD_LIBRARY_PATH',
                 'FONTCONFIG_PATH', 'FONTCONFIG_FILE', 'PLAYWRIGHT_NODEJS_PATH'):
        if name in os.environ:
            environment[name] = os.environ[name]
    if credentials:
        environment.update(credentials)
    return environment


def sanitize_bridge(value: Any, key: str = "") -> Any:
    """Remove ephemeral challenge answers while retaining state/trace evidence."""
    if SENSITIVE_KEY.search(key):
        if value is None:
            return None
        return {"redacted": True, "value_type": type(value).__name__, "length": len(str(value))}
    if isinstance(value, dict):
        sanitized = {str(k): sanitize_bridge(v, str(k)) for k, v in value.items()}
        target = value.get("target")
        if isinstance(target, str) and SENSITIVE_KEY.search(target) and "value" in sanitized:
            raw_value = value.get("value")
            sanitized["value"] = {
                "redacted": True,
                "value_type": type(raw_value).__name__,
                "length": len(str(raw_value)) if raw_value is not None else 0,
            }
        return sanitized
    if isinstance(value, list):
        return [sanitize_bridge(item, key) for item in value]
    return value


def tree_rss_kib(root_pid: int) -> int:
    rows = subprocess.run(
        ["ps", "-e", "-o", "pid=,ppid=,rss="],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for row in rows:
        fields = row.split()
        if len(fields) != 3:
            continue
        try:
            pid, ppid, value = map(int, fields)
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
        rss[pid] = value
    pending = [root_pid]
    seen: set[int] = set()
    total = 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        pending.extend(children.get(pid, []))
    return total


def bridge_state(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/__evaluator__/state",
        headers={"X-Evaluator-Token": token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("protected bridge returned a non-object")
    return value


def wait_for_health(url: str, expected_case_id: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "fixture did not answer"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as response:
                payload = json.load(response)
            if payload.get("ready") is True and payload.get("case_id") == expected_case_id:
                return
            last_error = f"unexpected health payload: {payload!r}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"fixture health check failed: {last_error}")


def read_readiness_line(process: subprocess.Popen[str], timeout: float = 15.0) -> str:
    if process.stdout is None:
        raise RuntimeError("fixture stdout is unavailable")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        if process.poll() is not None:
            raise RuntimeError(f"fixture exited before readiness with code {process.returncode}")
        raise RuntimeError("fixture did not emit a readiness line within 15 seconds")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("fixture closed stdout before readiness")
    return line


def start_control_service(
    python_bin: Path,
    fixture_url: str,
    case_id: str,
    viewport: dict[str, int],
    evidence: Path,
    browser_session: str,
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    """Start the evaluator-owned browser/control process.

    The service gets the fixture origin and evaluator-only evidence directory
    from this harness.  The Candidate receives only its random loopback URL and
    token, never the fixture backend, source path, or browser debug endpoint.
    """
    script = Path(__file__).with_name("browser_control.py")
    if not script.is_file():
        raise RuntimeError("evaluator-owned browser control implementation is missing")
    process = subprocess.Popen(
        [
            str(python_bin), str(script), "--fixture-url", fixture_url,
            "--case-id", case_id, "--viewport", json.dumps(viewport),
            "--evidence", str(evidence / "browser"),
            "--browser-session", browser_session,
        ],
        cwd=str(script.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=isolated_environment(python_bin, evidence / "runtime" / "browser"),
        text=True, start_new_session=True,
    )
    try:
        line = read_readiness_line(process, timeout=25.0)
        payload = json.loads(line)
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            raise RuntimeError("browser control service did not become ready")
        if payload.get("protocol") != "evaluator-controlled-browser-v1":
            raise RuntimeError("browser control protocol identity mismatch")
        if not isinstance(payload.get("url"), str) or not isinstance(payload.get("token"), str):
            raise RuntimeError("browser control readiness omitted URL/token")
        return process, payload
    except Exception:
        terminate_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            terminate_group(process, signal.SIGKILL)
            process.wait()
        raise


def stage_input(source_input: Path, stage_root: Path, fixture_url: str) -> dict[str, Any]:
    """Create the only active-case path passed to the candidate.

    Desktop case application links are launch metadata, not supporting evidence.
    Replacing those links with the already-authorized loopback URL prevents an
    ordinary relative-path reader from receiving fixture Python/HTML sources.
    """
    text = source_input.read_text(encoding="utf-8")
    rewritten, count = re.subn(
        r"(?P<prefix>\[[^\]]*\]\()(?P<target>[^)\s]*assets/serve\.py)(?P<suffix>\))",
        lambda match: match.group("prefix") + fixture_url + match.group("suffix"),
        text,
        flags=re.I,
    )
    stage_root.mkdir(parents=True, exist_ok=False)
    staged_input = stage_root / "input.md"
    staged_input.write_text(rewritten, encoding="utf-8")
    os.chmod(staged_input, 0o444)
    return {
        "staged_input": str(staged_input),
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "staged_sha256": hashlib.sha256(rewritten.encode("utf-8")).hexdigest(),
        "application_links_rewritten": count,
        "copied_supporting_assets": [],
        "excluded_fixture_assets": ["assets/serve.py", "assets/index.html"],
        "isolation_note": (
            "Only staged input.md was passed to the candidate. Filesystem isolation must still be enforced "
            "by the outer sandbox because staging cannot prevent arbitrary reads of known host paths."
        ),
    }


class AuditProxy:
    def __init__(self, backend_url: str, audit_path: Path, browser_session: str):
        self.backend = urlsplit(backend_url)
        self.audit_path = audit_path
        self.browser_session = browser_session
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GuiBenchmarkAuditProxy/2.1"

            def log_message(self, *_: Any) -> None:
                pass

            def handle_request(self) -> None:
                started = utc_now()
                parsed_path = urlsplit(self.path).path
                declared_length = int(self.headers.get("Content-Length", "0") or 0)
                length = min(max(0, declared_length), 2_000_000)
                body = self.rfile.read(length) if length else None
                evaluator_header = any(key.lower() == "x-evaluator-token" for key in self.headers)
                prohibited_bridge = parsed_path == "/__evaluator__/state"
                cookie = self.headers.get("Cookie", "")
                session_ok = secrets.compare_digest(cookie, f"evaluator_browser_session={outer.browser_session}")
                unauthorized_record = not session_ok
                status = 403 if (prohibited_bridge or unauthorized_record) else 502
                response_body = b'{"error":"protected evaluator route"}' if prohibited_bridge else (b'{"error":"browser session required"}' if unauthorized_record else b"")
                response_headers: list[tuple[str, str]] = []
                if not prohibited_bridge and not unauthorized_record:
                    headers = {
                        key: value
                        for key, value in self.headers.items()
                        if key.lower() not in {"host", "connection", "x-evaluator-token"}
                    }
                    connection = http.client.HTTPConnection(outer.backend.hostname, outer.backend.port, timeout=30)
                    try:
                        connection.request(self.command, self.path, body=body, headers=headers)
                        response = connection.getresponse()
                        status = response.status
                        response_body = response.read()
                        response_headers = response.getheaders()
                    except Exception as exc:
                        response_body = json.dumps({"error": str(exc)[:300]}).encode("utf-8")
                    finally:
                        connection.close()
                self.send_response(status)
                for key, value in response_headers:
                    if key.lower() not in {"content-length", "connection", "transfer-encoding"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response_body)
                record = {
                    "timestamp_utc": started,
                    "method": self.command,
                    "path": parsed_path,
                    "status": status,
                    "request_bytes": len(body or b""),
                    "response_bytes": len(response_body),
                    "evaluator_header_present": evaluator_header,
                    "protected_bridge_attempt": prohibited_bridge,
                }
                with outer.lock:
                    with outer.audit_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

            do_GET = handle_request
            do_POST = handle_request
            do_PUT = handle_request
            do_PATCH = handle_request
            do_DELETE = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def read_png_metadata(path: Path) -> dict[str, Any]:
    from PIL import Image
    with Image.open(path) as image:
        if image.format != 'PNG':
            raise ValueError('not a PNG image')
        image.verify()
    with Image.open(path) as image:
        image.load()
        width, height = image.size
    return {"width": width, "height": height}


def request_viewport(input_path: Path) -> dict[str, int]:
    text = input_path.read_text(encoding="utf-8")
    match = re.search(r"(?im)^\s*[-*]?\s*Viewport\s*:\s*(\d{2,5})\s*[xX\u00d7]\s*(\d{2,5})", text)
    if not match:
        raise ValueError("active request has no parseable viewport")
    return {"width": int(match.group(1)), "height": int(match.group(2))}


def inspect_artifacts(
    output: Path,
    expected_case_id: str,
    expected_viewport: dict[str, int],
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "expected_case_id": expected_case_id,
        "expected_viewport": expected_viewport,
        "required": {},
        "json": {},
        "png": {},
        "errors": [],
    }
    for name in OWNED_ARTIFACTS:
        path = output / name
        entry: dict[str, Any] = {"exists": path.is_file() and not path.is_symlink()}
        if entry['exists']:
            entry.update({"bytes": path.stat().st_size, "sha256": sha256_file(path)})
        checks["required"][name] = entry
    for name in ("automation_result.json", "run_report.json"):
        path = output / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            checks["json"][name] = {
                "parseable": True,
                "top_level_type": type(value).__name__,
                "case_id": value.get("case_id") if isinstance(value, dict) else None,
                "status": value.get("status", value.get("task_status")) if isinstance(value, dict) else None,
            }
            if not isinstance(value, dict):
                checks['errors'].append(f'{name} must be an object (parsed {type(value).__name__})')
                continue
            if name == "automation_result.json":
                checks["json"][name].update(
                    {
                        "schema_version": value.get("schema_version"),
                        "viewport": value.get("viewport"),
                        "viewport_matches_request": value.get("viewport") == expected_viewport,
                        "trace_is_array": isinstance(value.get("action_trace"), list),
                        "evidence_is_object": isinstance(value.get("evidence"), dict),
                        "errors_is_array": isinstance(value.get("errors"), list),
                    }
                )
                if value.get("case_id") != expected_case_id:
                    checks["errors"].append("automation_result.json case_id mismatch")
                if value.get("schema_version") != "2.0":
                    checks["errors"].append("automation_result.json schema_version mismatch")
                if value.get("viewport") != expected_viewport:
                    checks["errors"].append("automation_result.json viewport mismatch")
            elif name == "run_report.json":
                usage = value.get("usage", {}) if isinstance(value, dict) else {}
                checks["json"][name].update(
                    {
                        "top_level_keys": sorted(value) if isinstance(value, dict) else [],
                        "artifacts_is_array": isinstance(value.get("artifacts"), list),
                        "errors_is_array": isinstance(value.get("errors"), list),
                        "usage_is_object": isinstance(usage, dict),
                        "provider_fields_present": isinstance(usage, dict) and all(field in usage for field in PROVIDER_FIELDS),
                    }
                )
        except Exception as exc:
            checks["json"][name] = {"parseable": False, "error": str(exc)[:300]}
            checks["errors"].append(f"{name} is not parseable UTF-8 JSON")
    for name in ("initial_state.png", "decisive_step.png", "final_state.png"):
        path = output / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            metadata = read_png_metadata(path)
            checks["png"][name] = {
                "readable": True,
                **metadata,
                "matches_requested_viewport": metadata == expected_viewport,
            }
            if metadata != expected_viewport:
                checks["errors"].append(f"{name} dimensions do not match the requested viewport")
        except ImportError as exc:
            checks['infrastructure_error'] = 'trusted PNG decoder unavailable: ' + str(exc)
        except Exception as exc:
            checks["png"][name] = {"readable": False, "error": str(exc)}
            checks["errors"].append(f"{name} is not a readable PNG")
    return checks


def declared_provider_usage(output: Path) -> dict[str, Any]:
    path = output / "run_report.json"
    result: dict[str, Any] = {"source": "candidate run_report.json", "available": False, "counts": {}}
    if not path.is_file():
        return result
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        usage = report.get("usage", {})
        counts = {name: usage.get(name) for name in PROVIDER_FIELDS}
        result.update(
            {
                "available": True,
                "counts": counts,
                "all_integer_nonnegative": all(
                    isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    for value in counts.values()
                ),
                "status": report.get("status"),
                "action_count": usage.get("action_count"),
                "elapsed_seconds": usage.get("elapsed_seconds"),
            }
        )
    except Exception as exc:
        result["error"] = str(exc)[:300]
    return result


def diff_paths(before: Any, after: Any, prefix: str = "", limit: int = 500) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, path: str) -> None:
        if len(changes) >= limit or left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                visit(left.get(key, "<missing>"), right.get(key, "<missing>"), f"{path}.{key}" if path else str(key))
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                changes.append({"path": f"{path}.length", "before": len(left), "after": len(right)})
            for index, (old, new) in enumerate(zip(left, right)):
                visit(old, new, f"{path}[{index}]")
            return
        changes.append({"path": path or "$", "before": left, "after": right})

    visit(before, after, prefix)
    return changes


def partial_diagnostic(
    initial: dict[str, Any],
    final: dict[str, Any],
    execution: dict[str, Any],
    artifact_checks: dict[str, Any],
) -> dict[str, Any]:
    initial_state = initial.get("state", {})
    final_state = final.get("state", {})
    trace = final.get("trace", []) if isinstance(final.get("trace"), list) else []
    outcomes = Counter(str(item.get("outcome", "unknown")) for item in trace if isinstance(item, dict))
    action_classes = Counter(str(item.get("action", "unknown")) for item in trace if isinstance(item, dict))
    return {
        "diagnostic_only": True,
        "formal_scoring_effect": "none; apply evaluator/rubric.md validity gates and 100-point dimensions unchanged",
        "candidate_exit_code": execution.get("exit_code"),
        "timed_out": execution.get("timeout"),
        "memory_violation": execution.get("memory_violation"),
        "artifact_presence": {name: row["exists"] for name, row in artifact_checks["required"].items()},
        "state_change_count": len(diff_paths(initial_state, final_state)),
        "state_changes": diff_paths(initial_state, final_state),
        "trace_event_count": len(trace),
        "trace_action_classes": dict(action_classes),
        "trace_outcomes": dict(outcomes),
        "last_trace_event": trace[-1] if trace else None,
        "decisive_snapshot_count": len(final.get("decisive_snapshots", []))
        if isinstance(final.get("decisive_snapshots"), list)
        else 0,
        "record_count": final.get("record_count"),
    }


def terminate_group(process: subprocess.Popen[Any], sig: int = signal.SIGTERM) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Desktop GUI Automation V2 case")
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--submission-dir", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--dotenv", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--memory-limit-mib", type=int, default=4096)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_dir = args.case_dir.resolve()
    submission_dir = args.submission_dir.resolve()
    python_bin = args.python.resolve()
    output = args.output.resolve()
    evidence = args.evidence_dir.resolve()
    source_input = case_dir / "input.md"
    fixture_script = case_dir / "assets" / "serve.py"
    if not source_input.is_file() or not fixture_script.is_file():
        raise SystemExit("case-dir must contain input.md and assets/serve.py")
    if not (submission_dir / "run_agent.py").is_file():
        raise SystemExit("submission-dir must contain run_agent.py")
    if output == evidence or output in evidence.parents or evidence in output.parents:
        raise SystemExit("output and evidence directories must be disjoint")
    output.mkdir(parents=True, exist_ok=True)
    if evidence.exists() and any(evidence.iterdir()):
        raise SystemExit(f"evidence directory must be empty: {evidence}")
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "process").mkdir()
    (evidence / "audit").mkdir()
    request_audit = evidence / "audit" / "requests.jsonl"
    request_audit.write_text("", encoding="utf-8")

    token = secrets.token_urlsafe(48)
    browser_session = secrets.token_urlsafe(48)
    trusted_python = Path(sys.executable).resolve()
    fixture_environment = isolated_environment(trusted_python, evidence / "runtime" / "fixture")
    fixture = subprocess.Popen(
        [
            str(trusted_python),
            str(fixture_script),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--evaluator-token",
            token,
            "--browser-session",
            browser_session,
        ],
        cwd=case_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=fixture_environment,
        text=True,
        start_new_session=True,
    )
    proxy: AuditProxy | None = None
    browser_control: subprocess.Popen[str] | None = None
    browser_info: dict[str, Any] = {}
    stage_root: Path | None = None
    candidate: subprocess.Popen[str] | None = None
    try:
        readiness_line = read_readiness_line(fixture)
        fixture_info = json.loads(readiness_line)
        backend_url = str(fixture_info["url"])
        case_id = str(fixture_info["case_id"])
        atomic_json(
            evidence / "process" / "fixture_ready.json",
            {"ready": bool(fixture_info.get("ready")), "case_id": case_id, "emitted_at_utc": utc_now()},
        )
        wait_for_health(backend_url, case_id)
        proxy = AuditProxy(backend_url, request_audit, browser_session)
        proxy.start()

        expected_viewport = request_viewport(source_input)
        browser_control, browser_info = start_control_service(
            trusted_python, proxy.url, case_id, expected_viewport, evidence, browser_session
        )

        stage_root = Path(tempfile.mkdtemp(prefix=f"gui-active-{case_id}-", dir=str(evidence)))
        shutil.rmtree(stage_root)
        stage_manifest = stage_input(source_input, stage_root, browser_info['url'])
        staged_input = Path(stage_manifest["staged_input"])
        atomic_json(evidence / "active_case_manifest.json", {"case_id": case_id, **stage_manifest})

        initial = sanitize_bridge(bridge_state(backend_url, token))
        atomic_json(evidence / "audit" / "initial_state.json", initial)

        environment = isolated_environment(
            python_bin,
            evidence / "runtime" / "candidate",
            load_dotenv(args.dotenv.resolve() if args.dotenv else None),
        )
        environment.update(
            {
                # GUI_FIXTURE_URL is retained as a logical application address
                # for reports.  Only GUI_CONTROL_URL can perform browser input;
                # the raw fixture/proxy origin is never exposed to the Candidate.
                "GUI_FIXTURE_URL": browser_info["url"],
                "GUI_CONTROL_URL": browser_info["url"],
                "GUI_CONTROL_TOKEN": browser_info["token"],
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = [
            str(python_bin),
            "run_agent.py",
            "--input",
            str(staged_input),
            "--output",
            str(output),
        ]
        start_utc = utc_now()
        started = time.monotonic()
        candidate = subprocess.Popen(
            command,
            cwd=submission_dir,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        timeout = False
        memory_violation = False
        peak_rss_kib = 0
        limit_kib = args.memory_limit_mib * 1024
        while candidate.poll() is None:
            elapsed = time.monotonic() - started
            peak_rss_kib = max(peak_rss_kib, tree_rss_kib(candidate.pid))
            timeout = elapsed > args.timeout_seconds
            memory_violation = peak_rss_kib > limit_kib
            if timeout or memory_violation:
                terminate_group(candidate, signal.SIGKILL)
                break
            time.sleep(0.25)
        stdout, stderr = candidate.communicate()
        wall_seconds = time.monotonic() - started
        (evidence / "process" / "stdout.txt").write_text(stdout, encoding="utf-8", errors="replace")
        (evidence / "process" / "stderr.txt").write_text(stderr, encoding="utf-8", errors="replace")

        final = sanitize_bridge(bridge_state(backend_url, token))
        atomic_json(evidence / "audit" / "final_state.json", final)
        artifact_checks = inspect_artifacts(output, case_id, expected_viewport)
        atomic_json(evidence / "artifact_validation.json", artifact_checks)
        provider_usage = declared_provider_usage(output)
        execution = {
            "harness_version": HARNESS_VERSION,
            "case_id": case_id,
            "command": [str(python_bin), "run_agent.py", "--input", "<staged-active-case>/input.md", "--output", str(output)],
            "candidate_cwd": str(submission_dir),
            "environment_keys": sorted(environment),
            "credential_environment_names": sorted(
                key for key in environment if key in {"GATEWAY_API_KEY", "SERPER_TOKEN"}
            ),
            "runtime_root": str(evidence / "runtime" / "candidate"),
            "start_utc": start_utc,
            "end_utc": utc_now(),
            "wall_seconds": round(wall_seconds, 3),
            "exit_code": candidate.returncode,
            "timeout": timeout,
            "timeout_limit_seconds": args.timeout_seconds,
            "memory_violation": memory_violation,
            "peak_process_tree_rss_kib": peak_rss_kib,
            "memory_limit_kib": limit_kib,
            "fixture_case_id": case_id,
            "fixture_source_sha256": sha256_file(fixture_script),
            "fixture_html_sha256": sha256_file(case_dir / "assets" / "index.html"),
            "browser_control_protocol": browser_info.get("protocol"),
            "browser_control_url_exposed": bool(browser_info.get("url")),
            "browser_control_token_exposed": "GUI_CONTROL_TOKEN" in environment,
            "state_provenance": None,
            "isolation_pending": True,
            "browser_evidence_dir": str(evidence / "browser"),
            "candidate_declared_provider_usage": provider_usage,
            "network_observation_scope": (
                "Fixture-origin traffic is audited by the loopback proxy. The harness does not provide host-wide "
                "packet capture; provider counts remain candidate-declared evidence."
            ),
        }
        atomic_json(evidence / "execution.json", execution)
        atomic_json(evidence / "partial_state_diagnostic.json", partial_diagnostic(initial, final, execution, artifact_checks))

        requests = [json.loads(line) for line in request_audit.read_text(encoding="utf-8").splitlines() if line.strip()]
        atomic_json(
            evidence / "audit" / "request_summary.json",
            {
                "request_count": len(requests),
                "methods": dict(Counter(row["method"] for row in requests)),
                "statuses": dict(Counter(str(row["status"]) for row in requests)),
                "protected_bridge_attempts": sum(bool(row["protected_bridge_attempt"]) for row in requests),
                "evaluator_header_attempts": sum(bool(row["evaluator_header_present"]) for row in requests),
            },
        )
        print(json.dumps(execution, ensure_ascii=False))
        return 0
    finally:
        if browser_control is not None and browser_control.poll() is None:
            # Ask the service to flush immutable receipts, then terminate the
            # process group if a Candidate kept an outstanding request open.
            terminate_group(browser_control, signal.SIGTERM)
            try:
                browser_control.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_group(browser_control, signal.SIGKILL)
                browser_control.wait()
        if proxy is not None:
            proxy.close()
        if candidate is not None and candidate.poll() is None:
            terminate_group(candidate, signal.SIGKILL)
            candidate.wait(timeout=5)
        terminate_group(fixture, signal.SIGTERM)
        try:
            fixture_stdout, fixture_stderr = fixture.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_group(fixture, signal.SIGKILL)
            fixture_stdout, fixture_stderr = fixture.communicate()
        (evidence / "process" / "fixture_stdout.txt").write_text(fixture_stdout, encoding="utf-8", errors="replace")
        (evidence / "process" / "fixture_stderr.txt").write_text(fixture_stderr, encoding="utf-8", errors="replace")
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
