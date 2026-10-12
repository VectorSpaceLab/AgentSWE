#!/usr/bin/env python3
"""Evaluator-owned OpenAI-compatible broker and its process lifecycle.

The lower product receives only ``broker-only-placeholder``.  The real
credential is loaded by this evaluator process from a file and is never put in
the Candidate environment, command line, lifecycle manifest, or broker stats.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from ..protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, utc_now, write_json
except ImportError:  # module can be launched from the evaluator directory
    from agentloop.protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, utc_now, write_json



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

class CredentialError(RuntimeError):
    """The evaluator credential is absent or unusable."""


MAX_UPSTREAM_ATTEMPTS = 1
# The broker container runs with --rm. Once it has stopped, Docker 29 removes it asynchronously: inspect still
# shows it for several seconds and `docker rm -f` answers "removal of container ... is already in progress".
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5

def _decode_responses_payload(raw: bytes, content_type: str) -> dict[str, Any]:
    import io
    from .native_broker import read_response,strict_json
    stream=io.BytesIO(raw)
    body,completed=read_response(stream.read,content_type=content_type,is_success=True)
    value=completed if completed is not None else strict_json(body)
    if not isinstance(value,dict) or not _completed_or_budget_limited(value) or not isinstance(value.get('id'),str) or not value['id']:
        raise ValueError('response_not_typed_completed')
    return value

def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def resolve_credential(path: Path) -> tuple[str, str]:
    """Resolve a credential without returning or persisting it in metadata.

    ``OPENAI_API_KEY`` remains supported for OpenAI-compatible deployments.
    GATEWAY deployments commonly provide only ``DEEPSEEK_API_KEY``; it maps to the same
    upstream bearer token here.  A one-line raw token file is also accepted.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"credential file is unavailable: {type(exc).__name__}") from exc
    values: dict[str, str] = {}
    raw_lines: list[str] = []
    for original in text.splitlines():
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raw_lines.append(_dotenv_value(line))
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if key in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}:
            values[key] = _dotenv_value(raw)
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        value = values.get(key, "")
        if value and value != PLACEHOLDER_KEY:
            return value, key
    if len(raw_lines) == 1 and raw_lines[0] and raw_lines[0] != PLACEHOLDER_KEY:
        return raw_lines[0], "raw_token"
    raise CredentialError("credential file contains neither OPENAI_API_KEY nor DEEPSEEK_API_KEY")


def _upstream_url(base: str, request_path: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/v1") and request_path.startswith("/v1/"):
        return base + request_path[3:]
    return base + request_path


from agentloop.evaluator.native_broker import BrokerState as NativeState,Handler as NativeHandler,evaluator_proxy_url

class BrokerState(NativeState):
    def __init__(self,credential: Path,stats: Path,broker_instance_id=None,model=LOWER_MODEL,reasoning_effort=LOWER_EFFORT):
        if model!=LOWER_MODEL or reasoning_effort!=LOWER_EFFORT:raise ValueError('task broker only supports medium lower; use evaluator xhigh runtime for judges/Builder')
        key,source=resolve_credential(credential)
        self.stats_path=stats;self.credential_source_key=source;self.model=model;self.reasoning_effort=reasoning_effort
        super().__init__('',key,False,effort=reasoning_effort,stats_file=stats.with_name(stats.stem+'-request-ledger.json'))
        with self.ledger.lock:
            meta=self.ledger.value.setdefault('owner_metadata',{})
            if meta.get('broker_instance_id') and broker_instance_id and meta['broker_instance_id']!=broker_instance_id:
                raise ValueError('persistent broker instance mismatch')
            new_owner='broker_instance_id' not in meta
            self.instance_id=meta.setdefault('broker_instance_id',broker_instance_id or 'broker-'+uuid.uuid4().hex)
            if new_owner:self.ledger._save()
        self.flush_public()
    @property
    def stats(self):
        snapshot=NativeState.stats(self);runtime=snapshot['runtime'];intents=snapshot.get('logical_requests',{})
        return {'schema_version':2,'broker_instance_id':self.instance_id,'model':self.model,'reasoning_effort':self.reasoning_effort,
            'credential_source_key':self.credential_source_key,'credential_value_recorded':False,
            **runtime,'logical_intent_count':len(intents),'prompt_tokens':runtime['input_tokens'],'completion_tokens':runtime['output_tokens'],
            'pending_calls':sum(v['state']=='submitted_or_unknown' for v in intents.values()),
            'unknown_calls':sum(v['state'] in ('submitted_or_unknown','unknown_or_failed') for v in intents.values()),
            'reserved_not_submitted_count':sum(v['state']=='reserved_not_submitted' for v in intents.values()),
            'transport_measurement':snapshot.get('transport_measurement','legacy-unmeasured'),
            'forced_overrides':0,'max_upstream_attempts_per_logical_request':1,
            'request_ledger':str(self.ledger.path),'requests':snapshot.get('requests',[])}
    def flush_public(self):write_json(self.stats_path,self.stats)
    def record(self,**kwargs):
        super().record(**kwargs);self.flush_public()
    def key(self):return self.credential

def handler(state: BrokerState,upstream: str):
    base=upstream.rstrip('/')
    if base.endswith('/responses'):base=base[:-len('/responses')]
    if base.endswith('/v1'):base=base[:-len('/v1')]
    state.upstream=base
    class H(NativeHandler):
        @property
        def state(self):return state
        def do_GET(self):
            if self.path=='/healthz':self._json(200,{'ok':True,'broker_instance_id':state.instance_id,'model':state.model,'reasoning_effort':state.reasoning_effort})
            elif self.path=='/stats':self._json(200,state.stats)
            else:self._json(404,{'error':'not_found'})
    return H

def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class EvaluatorBrokerLifecycle:
    """Own one fresh evaluator broker for exactly one scoring stage."""

    def __init__(
        self,
        run_dir: Path,
        credential_file: Path,
        upstream: str,
        startup_timeout: float = 10.0,
        docker_image: str | None = None,
        model: str = LOWER_MODEL,
        reasoning_effort: str = LOWER_EFFORT,
        container_prefix: str = "openwiki-broker",
        proxy_url: str = "",  # direct egress; evaluator_proxy_url accepts empty
        defer_removal: bool = False,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.credential_file = credential_file.resolve()
        self.upstream = upstream
        self.startup_timeout = startup_timeout
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.container_prefix = container_prefix
        self.proxy_url = evaluator_proxy_url(proxy_url)
        self.defer_removal = defer_removal
        self.docker_image = docker_image or os.environ.get(
            "OPENWIKI_BROKER_IMAGE",
            "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812",
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None
        self.endpoint: str | None = None
        self.instance_id = f"broker-{uuid.uuid4().hex}"
        self.stats_path = self.run_dir / "broker_stats.json"
        self.lifecycle_path = self.run_dir / "broker_lifecycle.json"
        self.stdout_path = self.run_dir / "broker.stdout.log"
        self.stderr_path = self.run_dir / "broker.stderr.log"
        self.credential_source_key: str | None = None
        self._stdout = None
        self._stderr = None
        self.transport: str | None = None
        self.container_name: str | None = None
        self.container_id: str | None = None
        self.container_cidfile = self.run_dir / "container.cid"
        self._closed = False

    def _record(self, **extra: object) -> None:
        value = {
            "schema_version": "openwiki-evaluator-broker-lifecycle/v1",
            "broker_instance_id": self.instance_id,
            "owner": "evaluator",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "endpoint": self.endpoint,
            "credential_source_key": self.credential_source_key,
            "credential_value_recorded": False,
            "credential_mounted_to_candidate": False,
            "credential_transport": self.transport,
            "explicit_evaluator_proxy_url": self.proxy_url,
            "container_name": self.container_name,
            "stats": str(self.stats_path),
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
        }
        value.update(extra)
        write_json(self.lifecycle_path, value)

    def __enter__(self) -> "EvaluatorBrokerLifecycle":
        # A root-owned credential is intentionally unreadable to the host
        # evaluator user.  In that normal production case the broker runs in
        # Docker and receives only a read-only secret mount.  A readable file
        # remains useful for isolated tests and local development.
        if not self.credential_file.is_file():
            raise CredentialError("evaluator credential file is unavailable")
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.port = _free_local_port()
        self.endpoint = f"http://127.0.0.1:{self.port}/v1/responses"
        self._stdout = self.stdout_path.open("wb")
        self._stderr = self.stderr_path.open("wb")
        if urllib.parse.urlsplit(self.upstream).hostname in {"127.0.0.1","localhost"} and os.access(self.credential_file, os.R_OK):
            _, self.credential_source_key = resolve_credential(self.credential_file)
            self.transport = "host_process_readable_test_secret"
            argv = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--credential-file", str(self.credential_file),
                "--stats-file", str(self.stats_path),
                "--port", str(self.port),
                "--upstream", self.upstream,
                "--broker-instance-id", self.instance_id,
                "--model", self.model,
                "--reasoning-effort", self.reasoning_effort,
            ]
        else:
            self.credential_source_key = "evaluator_read_only_secret_mount"
            self.transport = "docker_read_only_secret_mount"
            self.container_name = self.container_prefix + "-" + self.instance_id[-12:]
            argv = [
                "docker", "run", *([] if self.defer_removal else ["--rm"]), "--network", "host",
                "--name", self.container_name,
                "--cidfile", str(self.container_cidfile),
                "--label", "agentswe.owner=openwiki-evaluator-broker",
                "-v", f"{Path(__file__).resolve().parents[2]}:/benchmark:ro",
                "-v", "@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py:/responses_stream.py:ro",
                "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
                "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                "-e", f"AGENTSWE_EVALUATOR_PROXY_URL={self.proxy_url}",
                "-v", f"{self.credential_file}:/run/secrets/agentswe.env:ro",
                "-v", f"{self.run_dir}:/evidence",
                self.docker_image,
                "python3", "/benchmark/agentloop/evaluator/broker.py",
                "--credential-file", "/run/secrets/agentswe.env",
                "--stats-file", "/evidence/broker_stats.json",
                "--port", str(self.port),
                "--bind", "127.0.0.1",
                "--upstream", self.upstream,
                "--broker-instance-id", self.instance_id,
                "--model", self.model,
                "--reasoning-effort", self.reasoning_effort,
            ]
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=self._stdout,
            stderr=self._stderr,
            close_fds=True,
            # Do not copy evaluator credentials into the broker environment;
            # the owned credential file is the sole secret transport.
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                "AGENTSWE_EVALUATOR_PROXY_URL": self.proxy_url,
            },
        )
        self._record(status="starting", started_at=utc_now(), pid=self.process.pid)
        health = f"http://127.0.0.1:{self.port}/healthz"
        deadline = time.monotonic() + self.startup_timeout
        last_error = "broker did not answer"
        while time.monotonic() < deadline:
            if self.container_id is None and self.container_cidfile.is_file():
                value = self.container_cidfile.read_text(encoding="ascii").strip()
                if value:
                    self.container_id = value
            if self.process.poll() is not None:
                last_error = f"broker exited with code {self.process.returncode}"
                break
            try:
                with urllib.request.urlopen(health, timeout=0.5) as response:
                    value = json.loads(response.read())
                if (value.get("broker_instance_id") == self.instance_id
                        and value.get("model") == self.model
                        and value.get("reasoning_effort") == self.reasoning_effort):
                    self._record(status="ready", started_at=utc_now(), pid=self.process.pid)
                    return self
                last_error = "broker instance id mismatch"
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: broker not ready"
            time.sleep(0.05)
        self.close(status="startup_failed", error=last_error)
        raise RuntimeError(last_error)

    def close(self, status: str = "stopped", error: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup_errors: list[str] = []
        retained_terminal = None
        if self.defer_removal:
            try:
                if self.container_id is None and self.container_cidfile.is_file():
                    self.container_id = self.container_cidfile.read_text(encoding='ascii').strip() or None
                if self.container_id:
                    from harbor.readiness_resources import retain_container
                    retained_terminal = retain_container(self.container_id, self.run_dir,
                        expected_name=self.container_name, expected_mount=(str(self.run_dir), '/evidence'))
            except Exception as exc:
                cleanup_errors.append(f'retention: {type(exc).__name__}: {exc}')
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        except Exception as exc:
            cleanup_errors.append(f"process_stop: {type(exc).__name__}: {exc}")
        for stream_name in ("_stdout", "_stderr"):
            stream = getattr(self, stream_name)
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception as exc:
                    cleanup_errors.append(f"{stream_name}: {type(exc).__name__}: {exc}")
        container_absent: bool | None = None
        container_inspect_error = None
        removal_in_progress_at_rm = None
        if self.container_id is None and self.container_cidfile.is_file():
            try:
                value = self.container_cidfile.read_text(encoding="ascii").strip()
                self.container_id = value or None
            except Exception as exc:
                cleanup_errors.append(f"container_id_read: {type(exc).__name__}: {exc}")
        if self.container_id is not None and not self.defer_removal:
            try:
                removed = subprocess.run(
                    ["docker", "rm", "-f", self.container_id],
                    capture_output=True, text=True, check=False,
                )
                if removed.returncode != 0 and "already in progress" in (removed.stdout + removed.stderr).lower():
                    # The client was killed above while the daemon was still removing the stopped --rm broker;
                    # wait for that removal and let the inspect below decide absence. Other refusals still fail.
                    removal_in_progress_at_rm = removed.stderr[-500:]
                    wait_until = time.monotonic() + REMOVAL_WAIT_SECONDS
                    while time.monotonic() < wait_until and subprocess.run(
                            ["docker", "inspect", self.container_id], capture_output=True, text=True,
                            check=False).returncode == 0:
                        time.sleep(REMOVAL_POLL_SECONDS)
                elif removed.returncode != 0:
                    cleanup_errors.append(f"docker_rm_exit_{removed.returncode}: {removed.stderr[-500:]}")
            except Exception as exc:
                cleanup_errors.append(f"docker_rm: {type(exc).__name__}: {exc}")
            try:
                inspected = subprocess.run(
                    ["docker", "inspect", self.container_id],
                    capture_output=True, text=True, check=False,
                )
                detail = (inspected.stdout + "\n" + inspected.stderr).lower()
                if inspected.returncode == 0:
                    container_absent = False
                elif "no such object" in detail or "no such container" in detail:
                    container_absent = True
                else:
                    container_absent = False
                    container_inspect_error = f"docker inspect exit {inspected.returncode}: {detail[-800:]}"
            except Exception as exc:
                container_absent = False
                container_inspect_error = f"{type(exc).__name__}: {exc}"
                cleanup_errors.append(f"docker_inspect: {container_inspect_error}")
        stats: dict[str, Any] | None = None
        try:
            value = json.loads(self.stats_path.read_text(encoding="utf-8"))
            stats = value if isinstance(value, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        self._record(
            status=status,
            stopped_at=utc_now(),
            exit_code=self.process.returncode if self.process is not None else None,
            container_absent=container_absent,
            container_id=self.container_id,
            inspect_error=container_inspect_error,
            final_stats=stats,
            removal_deferred=self.defer_removal,
            retained_terminal=retained_terminal,
            error=error,
            cleanup_errors=cleanup_errors,
            **({"removal_in_progress_at_rm": removal_in_progress_at_rm} if removal_in_progress_at_rm else {}),
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(status="failed" if exc is not None else "stopped", error=type(exc).__name__ if exc else None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--upstream", default=os.environ.get("AGENTSWE_UPSTREAM_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--broker-instance-id")
    parser.add_argument("--model", default=LOWER_MODEL)
    parser.add_argument("--reasoning-effort", default=LOWER_EFFORT)
    args = parser.parse_args()
    try:
        state = BrokerState(
            args.credential_file.resolve(), args.stats_file.resolve(), args.broker_instance_id,
            args.model, args.reasoning_effort,
        )
    except CredentialError as exc:
        print(f"credential_failure: {exc}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.bind, args.port), handler(state, args.upstream))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
