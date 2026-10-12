#!/usr/bin/env python3
"""DeepTutor one-stop formal Agent-loop orchestration.

One Harbor Builder process owns one continuous deepseek-flash/xhigh session.  It
submits one through ten distinct accepted Candidates, receives evaluator
feedback after each public evaluation, and returns when done.  The evaluator
then freezes the latest accepted Candidate and starts a separate, fresh deepseek-flash/medium
broker for the six hidden cases.  Formal Result/Code publication remains gated
behind the formal finalizer.  Pilot mode records one explicitly non-formal
test_001 measurement so the reduced end-to-end pipeline includes evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from one_stop_contract import install_summary_writer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
RESULT_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentloop.candidate_adapter import materialize_candidate
from agentloop.execution_evidence import attest_execution
from agentloop.protocol import (
    AUTHORITATIVE_SOURCE,
    BUILDER_EFFORT,
    BUILDER_MODEL,
    DEV_CASES,
    HIDDEN_CASES,
    LOWER_EFFORT,
    LOWER_MODEL,
    PLACEHOLDER_TOKEN,
    STATS_TOKEN,
    load_case,
    read_json,
    tree_digest,
    utc_now,
    validate_delivery,
    write_json,
)
from agentloop.run_hidden import broker_stats_view, read_broker_stats, run as run_hidden, stats_delta
from agentloop.prior_product_guard import PriorProductGuard
from agentloop.two_round_controller import AcceptedSubmissionController
from agentloop.public_feedback import clean, build_feedback, public_case_feedback, fixture_tool_diagnostic
from harbor.native_builder_evidence import observe_thread, verify_native
from harbor.native_builder_runner import run_native_builder
from harbor import direct_harbor_builder as direct_builder


BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
# 5 h Builder cap (D2, 2026-09-19).  The shared driver
# @@AGENTSWE_EDITING_CONTROL@@/formal_commands.py passes
# --builder-timeout 18000, so the generated Harbor task.toml has to stop the
# native Builder BEFORE the outer wait SIGTERMs it; otherwise the run reports
# 124 instead of a Builder exit.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
# The pilot/readiness branch keeps its historical 8 h generated cap: readiness
# passes --builder-timeout 28800 explicitly and its two-round canary approaches
# neither bound, so readiness behaviour is unchanged.
PILOT_BUILDER_TASK_TIMEOUT_SECONDS = 28_800
# run_native_builder turns an outer-deadline SIGTERM into 124 and a
# KeyboardInterrupt into 130.  Only 124 is a survivable interrupt.
BUILDER_INTERRUPT_EXIT_CODES = (124,)
# Direct egress: the broker sets up no proxy for an empty value.
EVALUATOR_PROXY_URL = ""


# --- judge resample (2026-09-20) ---------------------------------------------
# A shared Result judge that completed one paid-for response and then refused to
# publish it is an evaluator-side SAMPLING fault: the apparatus is healthy, the
# sample is not.  Booking it as infrastructure loses the Candidate's public
# round -- and on the ai-scientist tree it latched a terminal that ended the
# whole formal run (0905-edit-codex-xhigh-0919-fw-001-ai-scientist, 03:04 CST:
# deepseek-flash emitted a complete 100/100 verdict missing exactly one `}`,
# result_judge.close_unclosed_json appended it at the END of the text instead of
# at the container the model left open, and the misnested parse produced 31
# validation errors).  Across the 121 result contracts on this host 8 of the 65
# real judge calls are `model_output_invalid` -- about 12% -- so this is a
# routine event, not a freak one.
#
# The hidden axis already recovers by setting the refused attempt aside and
# judging again (see <run>/formal_scoring/result_axis/<case>.attempt-001-invalid
# on this host).  This gives the public round the same remedy, automatically and
# exactly once.  Nothing measured is repeated: the Candidate product is not
# re-executed, no lower-agent call is re-issued, the immutable evidence inputs
# are reused byte for byte, and the refused attempt is renamed rather than
# deleted so both verdicts stay auditable.  The control plane's
# one-response-per-immutable-directory contract (execution_scoring.py:96-116,
# result_judge.py:943-946) is preserved because the retry gets a fresh
# directory.
RESAMPLEABLE_JUDGE_STATES = {"model_output_invalid"}
JUDGE_RESAMPLE_ATTEMPTS = 1
# Everything the shared judge (result_judge.py:885-1016), the shared scorer
# (execution_scoring.py:96-119) and the task-local once-guards author inside the
# judge directory.  Any OTHER file there was written by this caller before the
# judge ran and is restored byte for byte into the fresh attempt.
JUDGE_AUTHORED_FILES = (
    "scoring_intent.json", "input_manifest.json", "judge_prompt.txt",
    "logical_request_started.json", "provider_response.json",
    "provider_response-attempts.json", "model_response.json",
    "result_eval_result.json", "result_score_contract.json",
    "task_judge_intent.json", "task_judge_invocation.json",
    "task_cli_stdout.log", "task_cli_stderr.log",
)


def refused_judge_state(judge_dir):
    """The judge's own evaluation_state when it answered and refused to publish.

    None keeps today's behaviour untouched.  A resample is offered only when the
    contract proves the apparatus worked: exactly one completed response with
    complete usage accounting, refused over the model's output rather than over
    the infrastructure.  A missing, unreadable, transport-failed or
    ``infrastructure_error`` contract returns None and stays terminal.
    """
    import json as _json
    from pathlib import Path as _Path
    try:
        contract = _json.loads(
            (_Path(judge_dir) / "result_score_contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict) or contract.get("contract_valid") is True:
        return None
    usage = contract.get("provider_usage")
    if not isinstance(usage, dict):
        return None
    if usage.get("completed_responses") != 1 or usage.get("usage_complete") is not True:
        return None
    state = contract.get("evaluation_state")
    return state if state in RESAMPLEABLE_JUDGE_STATES else None


def resample_refused_verdict(judge_dir, rejudge, *, recreate_dir=True):
    """Ask the evaluator's own Result judge again, in a new immutable directory.

    Returns the new judge outcome, or None when nothing was resampled and the
    caller must keep the outcome it already holds.  ``recreate_dir`` is False
    for callers whose own once-guard creates the directory with
    ``mkdir(exist_ok=False)``.
    """
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    judge_dir = _Path(judge_dir)
    outcome = None
    for attempt in range(1, JUDGE_RESAMPLE_ATTEMPTS + 1):
        state = refused_judge_state(judge_dir)
        if state is None:
            return outcome
        retired = judge_dir.parent / ("%s.attempt-%03d-%s" % (judge_dir.name, attempt, state))
        if retired.exists() or not judge_dir.is_dir():
            return outcome
        judge_dir.rename(retired)
        restored = []
        if recreate_dir:
            judge_dir.mkdir(parents=True)
            for item in sorted(retired.iterdir()):
                if item.name in JUDGE_AUTHORED_FILES or item.name.startswith("provider_response-attempt-"):
                    continue
                (_shutil.copytree if item.is_dir() else _shutil.copy2)(item, judge_dir / item.name)
                restored.append(item.name)
        (judge_dir.parent / ("judge_resample_%03d.json" % attempt)).write_text(
            _json.dumps({
                "schema_version": "agentswe-edit-judge-resample-v1",
                "attempt": attempt,
                "judge_dir": str(judge_dir),
                "retired_attempt": str(retired),
                "retired_evaluation_state": state,
                "restored_caller_inputs": restored,
                "candidate_product_re_executed": False,
                "lower_agent_calls_re_issued": 0,
                "reason": "the shared Result judge completed one response and refused to publish "
                          "it; the evaluator's own judge is resampled into a new immutable "
                          "directory and the refused attempt is retained",
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        outcome = rejudge()
    return outcome


def builder_timeout_contract(effective_outer_timeout: int) -> dict[str, Any]:
    """Prove the outer deadline covers the generated task and cleanup margin."""
    if isinstance(effective_outer_timeout, bool) or effective_outer_timeout <= 0:
        raise ValueError("effective outer Builder timeout must be a positive integer")
    required = DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS
    return {
        "generated_task_timeout_seconds": GENERATED_BUILDER_TASK_TIMEOUT_SECONDS,
        "cleanup_margin_seconds": BUILDER_CLEANUP_MARGIN_SECONDS,
        "effective_outer_timeout_seconds": int(effective_outer_timeout),
        "covers_task_plus_cleanup": int(effective_outer_timeout) >= required,
        "required_outer_timeout_seconds": required,
    }
DEFAULT_HARBOR = Path("@@AGENTSWE_HARBOR_BIN@@")
DEFAULT_CREDENTIAL = Path(
    "@@AGENTSWE_CREDENTIAL_FILE@@"
)
DEFAULT_RUNTIME_PYTHON = Path(
    "@@AGENTSWE_ENVS@@/deeptutor-task-env-v1/venv/bin/python"
)
BUILDER_BROKER_SCRIPT = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)
JUDGE_BROKER_SCRIPT = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/judge_broker_xhigh.py"
)
INFRA = {
    "broker_infrastructure_error",
    "provider_infrastructure_error",
    "credential_infrastructure_error",
    "mount_infrastructure_error",
    "docker_infrastructure_error",
    "evaluator_infrastructure_error",
    "launcher_infrastructure_error",
    "runtime_dependency_infrastructure_error",
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                value = json.loads(response.read())
            if isinstance(value, dict) and (value.get("ok") is True or value.get("status") == "ok"):
                return
        except (OSError, HTTPError, URLError, TimeoutError, ValueError) as exc:
            last = exc
            time.sleep(0.25)
    raise RuntimeError(f"broker on port {port} did not become healthy: {last}")


def _broker_ready(port: int, *, broker_kind: str, role: str) -> None:
    """Validate the health and the schema-specific zero-call startup state."""
    _health(port)
    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        health = json.loads(response.read())
    stats = read_broker_stats(f"http://127.0.0.1:{port}/v1/responses")
    if broker_kind == "responses_xhigh":
        if (
            role not in {"builder", "result_judge"}
            or health.get("status") != "ok"
            or health.get("model") != BUILDER_MODEL
            or (health.get("effort") if role == "result_judge" else health.get("reasoning_effort")) != BUILDER_EFFORT
        ):
            raise RuntimeError("shared Responses broker is evaluator-only and must expose status=ok health")
        runtime = stats.get("runtime")
        if not isinstance(runtime, dict) or any(
            int(runtime.get(key, -1)) != 0 for key in ("calls", "failures", "tokens")
        ) or runtime.get("budget_exceeded") is True:
            raise RuntimeError("shared Builder broker failed its nested zero-call stats gate")
        if role == "result_judge":
            protocol = stats.get("protocol", {})
            if (stats.get("schema_version") != "agentswe-judge-broker-stats/v1"
                    or protocol.get("inner_retries") != 0
                    or protocol.get("max_upstream_attempts_per_transport") != 1
                    or protocol.get("absolute_deadline_seconds_max") != 900):
                raise RuntimeError("Result requires single-upstream judge-only transport; Builder broker forbidden")
    else:
        view = broker_stats_view(stats)
        if role == "builder" or health.get("ok") is not True:
            raise RuntimeError("DeepTutor lower broker must be product-specific and expose ok health")
        if view.get("model") != LOWER_MODEL or view.get("reasoning_effort") != LOWER_EFFORT:
            raise RuntimeError("DeepTutor lower broker model/reasoning lock mismatch")
        if any(int(view.get(key, -1)) != 0 for key in ("calls", "failures", "successful_calls", "provider_failures")):
            raise RuntimeError("DeepTutor lower broker failed its flat zero-call stats gate")


def _inspect_container(reference: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["docker", "inspect", reference],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {"present": None, "absent": False, "error": f"{type(exc).__name__}: {exc}"}
    error = (completed.stderr or "").strip()
    lowered = error.lower()
    absent = completed.returncode != 0 and (
        "no such object" in lowered or "no such container" in lowered
    )
    return {
        "present": completed.returncode == 0,
        "absent": absent,
        "exit_code": completed.returncode,
        "error": None if completed.returncode == 0 or absent else error or "docker inspect failed",
    }


class BrokerProcess:
    """Evaluator-owned Docker broker; the host never opens the credential."""

    def __init__(
        self,
        *,
        run_dir: Path,
        role: str,
        credential: Path,
        python: str,
        max_calls: int,
        max_tokens: int,
        broker_kind: str = "deeptutor",
        defer_removal: bool = False,
    ) -> None:
        # Public input/04_resources.md sets case defaults; broker counters are per trusted context.
        if role in {'public', 'hidden'} and broker_kind == 'deeptutor':
            max_calls = min(max_calls, 12) if max_calls > 0 else 12
            # Mirrors agentloop/broker.py PUBLIC_CASE_MAX_TOKENS and the number
            # disclosed in input/04_resources.md; see the note there.
            max_tokens = min(max_tokens, 240_000) if max_tokens > 0 else 240_000
        self.run_dir = run_dir.resolve()
        self.role = role
        self.port = free_port()
        self.endpoint_local = f"http://127.0.0.1:{self.port}/v1/responses"
        self.endpoint_builder = f"http://172.17.0.1:{self.port}/v1/responses"
        self.credential = credential.resolve()
        self.broker_kind = broker_kind
        self.defer_removal = bool(defer_removal)
        self.stats_file = self.run_dir / f"{role}_broker_stats.json"
        self.stdout_path = self.run_dir / f"{role}_broker.stdout.log"
        self.stderr_path = self.run_dir / f"{role}_broker.stderr.log"
        self.container_name = (
            "deeptutor-" + role + "-broker-"
            + hashlib.sha256(f"{self.run_dir}:{role}:{time.time_ns()}".encode()).hexdigest()[:16]
        )
        self.container_id: str | None = None
        self.cidfile = self.run_dir / f"{role}_broker.cid"
        if role == "result_judge":
            if broker_kind != "responses_xhigh":
                raise ValueError("Result judge requires the xhigh transport role")
            if str(JUDGE_BROKER_SCRIPT.parent) not in sys.path:
                sys.path.insert(0, str(JUDGE_BROKER_SCRIPT.parent))
            from judge_broker_runtime import start_judge_broker
            self._judge_runtime = start_judge_broker(name=self.container_name, credential=self.credential,
                image=BUILDER_IMAGE, port=self.port, cidfile=self.cidfile, defer_removal=self.defer_removal)
            self.stats_file = self._judge_runtime.stats_path
            self.container_id = self._judge_runtime.container_id
            self.process = None
            write_json(self.run_dir / f"{role}_broker_lifecycle.json", {
                **read_json(self._judge_runtime.lifecycle_path), "role": role,
                "owner": "evaluator", "broker_kind": broker_kind,
                "broker_script": str(JUDGE_BROKER_SCRIPT), "single_upstream_judge_broker": True,
                "credential_mounted_to_candidate": False, "credential_read_by_host_process": False})
            return
        if role == 'builder':
            if broker_kind != 'responses_xhigh':raise ValueError('Builder requires xhigh shared transport')
            shared='@@AGENTSWE_EDITING_CONTROL@@'
            if shared not in sys.path:sys.path.insert(0,shared)
            from builder_broker_runtime import start_builder_broker
            start_builder_broker(name=self.container_name,credential=self.credential,image=BUILDER_IMAGE,port=self.port,cidfile=self.cidfile)
            self.container_id=self.cidfile.read_text().strip();self.process=None
            self.stats_file=self.run_dir / (self.cidfile.stem+'-builder-transport') / 'broker_stats.json'
            write_json(self.run_dir / f'{role}_broker_lifecycle.json',{'role':role,'owner':'evaluator','broker_kind':broker_kind,
                'container_id':self.container_id,'cidfile':str(self.cidfile),'stats_file':str(self.stats_file),
                'shared_builder_runtime':True,'credential_mounted_to_candidate':False})
            return
        self.cidfile.unlink(missing_ok=True)
        # Harbor/evaluator host code must not read the root-only credential.
        # Docker mounts it into the broker container as the sole secret
        # transport; the Candidate and host-side launcher see only the
        # placeholder and broker endpoint.
        if broker_kind not in {"deeptutor", "responses_xhigh"}:
            raise ValueError(f"unsupported broker kind: {broker_kind}")
        if (broker_kind == "responses_xhigh") != (role in {"builder", "result_judge"}):
            raise ValueError("responses_xhigh is reserved for evaluator Builder/Result-judge roles; public/hidden use the DeepTutor lower broker")
        broker_script = (
            BUILDER_BROKER_SCRIPT.resolve()
            if broker_kind == "responses_xhigh"
            else (ROOT / "agentloop/broker.py").resolve()
        )
        command = [
            "docker", "run", "--network", "host",
            "--name", self.container_name,
            "--cidfile", str(self.cidfile),
            "-v", f"{ROOT.resolve()}:/benchmark:ro",
            "-v", "@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py:/responses_stream.py:ro",
            "-v", f"{broker_script}:/broker.py:ro",
            "-v", f"{self.credential}:/run/secrets/agentswe.env:ro",
            "-v", f"{self.run_dir}:/evidence",
            "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
            "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
            "-e", "AGENTSWE_EVALUATOR_PROXY_URL=" + EVALUATOR_PROXY_URL,
            BUILDER_IMAGE,
        ]
        if not self.defer_removal:
            command.insert(2, "--rm")
        if broker_kind == "responses_xhigh":
            command.extend([
                "python3", "/broker.py",
                "--credential-file", "/run/secrets/agentswe.env",
                "--bind", "127.0.0.1", "--port", str(self.port),
                # The shared xhigh broker calls this option provider-url;
                # passing the lower-broker's --upstream spelling makes it
                # exit before healthz and masks the real Builder path.
                "--provider-url", "https://api.deepseek.com/v1/responses",
                "--stats-file", f"/evidence/{self.role}_broker_stats.json",
                "--max-runtime-calls", "0", "--max-runtime-tokens", "0",
            ])
        else:
            command.extend([
                # Keep the sibling broker inside its package tree.  It uses
                # protocol.py from agentloop; launching a lone /broker.py
                # mount loses that import context and fails before /healthz.
                "python3", "/benchmark/agentloop/broker.py",
                "--credential-file", "/run/secrets/agentswe.env",
                "--stats-file", f"/evidence/{self.role}_broker_stats.json",
                "--bind", "127.0.0.1", "--port", str(self.port),
                "--max-calls", str(max_calls), "--max-tokens", str(max_tokens),
            ])
            if role == "builder":
                command.append("--builder")
        self.stdout = self.stdout_path.open("w", encoding="utf-8")
        self.stderr = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self.stdout,
            stderr=self.stderr,
            start_new_session=True,
        )
        for _ in range(100):
            if self.cidfile.is_file():
                value = self.cidfile.read_text(encoding="utf-8").strip()
                if value:
                    self.container_id = value
                    break
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        if self.container_id is None:
            self.stop()
            raise RuntimeError(f"{role} broker did not yield an owned Docker container id")
        write_json(self.run_dir / f"{role}_broker_lifecycle.json", {
            "schema_version": "agentswe-deeptutor-broker-lifecycle/v1",
            "role": role,
            "owner": "evaluator",
            "transport": "docker_read_only_secret_mount",
            "credential_mounted_to_candidate": False,
            "credential_read_by_host_process": False,
            "credential_container_path": "/run/secrets/agentswe.env",
            "container_name": self.container_name,
            "broker_endpoint": self.endpoint_local,
            "broker_kind": broker_kind,
            "broker_script": str(broker_script),
            "single_upstream_judge_broker": role == "result_judge",
        })
        try:
            _broker_ready(self.port, broker_kind=broker_kind, role=role)
        except Exception:
            self.stop()
            raise

    def stop(self) -> dict[str, Any]:
        if getattr(self, "_judge_runtime", None) is not None:
            try:
                write_json(self.run_dir / f"{self.role}_broker_stats_snapshot.json",
                           read_broker_stats(self.endpoint_local))
            except Exception:
                pass
            receipt = {"role": self.role, **self._judge_runtime.close()}
            write_json(self.run_dir / f"{self.role}_broker_cleanup.json", receipt)
            return receipt
        if self.defer_removal and self.container_id:
            from harbor.readiness_resources import retain_container
            receipt = {'role': self.role, **retain_container(self.container_id, self.run_dir,
                expected_name=self.container_name, expected_mount=(str(self.run_dir), '/evidence'))}
            if self.process is not None:
                self.process.wait(timeout=30)
            for handle_name in ('stdout', 'stderr'):
                handle = getattr(self, handle_name, None)
                if handle is not None and not handle.closed:
                    handle.close()
            write_json(self.run_dir / f'{self.role}_broker_cleanup.json', receipt)
            return receipt
        if self.broker_kind == "responses_xhigh":
            try:
                # The judge broker owns its durable per-attempt ledger. A
                # pre-stop observation must not race or overwrite that ledger.
                destination = self.run_dir / f"{self.role}_broker_stats_snapshot.json"
                write_json(destination, read_broker_stats(self.endpoint_local))
            except Exception:
                pass
        process_error = None
        try:
            if getattr(self, "process", None) is not None and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        except (OSError, subprocess.SubprocessError) as exc:
            process_error = f"{type(exc).__name__}: {exc}"
        present_before: bool | None = None
        remove_exit_code: int | None = None
        remove_error: str | None = None
        inspect_error: str | None = None
        in_progress = False
        absent_after = self.container_id is None
        if self.container_id is not None:
            before = _inspect_container(self.container_id)
            present_before = before.get("present")
            if self.defer_removal:
                remove_error = "deferred_to_evaluator_owned_cleanup"
                after = _inspect_container(self.container_id)
                absent_after = False if after.get("present") else after.get("absent") is True
                inspect_error = after.get("error")
            else:
                try:
                    removed = subprocess.run(
                        ["docker", "rm", "-f", self.container_id],
                        text=True, capture_output=True, check=False,
                    )
                    remove_exit_code = removed.returncode
                    remove_error = (removed.stderr or "").strip() or None
                    # The CLI was killed above if the --rm broker's removal outlasted its wait.
                    in_progress = removal_in_progress(removed)
                    if in_progress:
                        await_daemon_removal(self.container_id)
                except OSError as exc:
                    remove_error = f"{type(exc).__name__}: {exc}"
                after = _inspect_container(self.container_id)
                absent_after = after.get("absent") is True
                inspect_error = after.get("error")
        for handle_name in ("stdout", "stderr"):
            handle = getattr(self, handle_name, None)
            if handle is not None and not handle.closed:
                handle.close()
        receipt = {
            "role": self.role,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "present_before_cleanup": present_before,
            "remove_exit_code": remove_exit_code,
            "remove_error": remove_error,
            "inspect_error": inspect_error,
            "process_error": process_error,
            **({"removal_in_progress_at_rm": True} if in_progress else {}),
            "absent_after_cleanup": absent_after,
        }
        write_json(self.run_dir / f"{self.role}_broker_cleanup.json", receipt)
        return receipt


# Docker 29 removes an exited or stopped --rm container asynchronously: `docker rm -f` then answers
# "removal of container ... is already in progress" and `docker inspect` still shows it for a few seconds.
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5


def removal_in_progress(removed: subprocess.CompletedProcess) -> bool:
    return removed.returncode != 0 and "already in progress" in ((removed.stdout or "") + (removed.stderr or "")).lower()


def await_daemon_removal(container_id: str) -> None:
    """Wait (bounded) until the daemon has finished removing a container; absence is judged afterwards."""
    wait_until = time.monotonic() + REMOVAL_WAIT_SECONDS
    while time.monotonic() < wait_until:
        try:
            if subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True,
                              check=False).returncode:
                return
        except (OSError, subprocess.SubprocessError):
            return
        time.sleep(REMOVAL_POLL_SECONDS)


def stop_brokers(brokers: tuple[BrokerProcess | None, ...]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for broker in brokers:
        if broker is None:
            continue
        try:
            results.append(broker.stop())
        except Exception as exc:
            results.append({
                "role": broker.role,
                "container_name": broker.container_name,
                "container_id": broker.container_id,
                "absent_after_cleanup": False,
                "cleanup_error": f"{type(exc).__name__}: {exc}",
            })
    return results


def merge_startup_cleanup_receipts(run_dir: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_role = {str(item.get("role")): item for item in results if item.get("role")}
    for path in sorted(run_dir.glob("*_broker_cleanup.json")):
        try:
            receipt = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        role = str(receipt.get("role") or "")
        if role and role not in by_role:
            by_role[role] = receipt
    return list(by_role.values())


def _terminal_build_results(
    build: dict[str, Any], expected_cases: tuple[str, ...] = DEV_CASES
) -> dict[str, dict[str, Any]]:
    classification = str(build.get("classification") or "candidate_build_failure")
    infra_valid = classification not in INFRA and build.get("infra_valid") is not False
    reason = "; ".join(str(item) for item in build.get("errors") or [classification])
    return {
        case_id: {
            "schema_version": "agentswe-deeptutor-public-case/v1",
            "case_id": case_id,
            "terminal": True,
            "valid": infra_valid,
            "infra_valid": infra_valid,
            "classification": classification,
            "score": 0 if infra_valid else None,
            "reason": reason,
            "lower_agent_executed": False,
            "broker_delta": None,
        }
        for case_id in expected_cases
    }


def _artifact_valid(output: Path, expected_case_id: str | None = None) -> bool:
    try:
        value = read_json(output / "agent_result.json")
        required = ("schema_version", "case_id", "status", "summary", "artifacts")
        if any(field not in value for field in required):
            return False
        if any(
            not isinstance(value.get(field), str) or not str(value.get(field)).strip()
            for field in ("schema_version", "case_id", "status", "summary")
        ):
            return False
        if not isinstance(value.get("artifacts"), dict):
            return False
        return expected_case_id is None or value.get("case_id") == expected_case_id
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _broker_failure(delta: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(delta, dict):
        return None
    if int(delta.get("provider_failures", 0) or 0) > 0:
        return "provider_infrastructure_error", "evaluator broker recorded a provider failure"
    # delivery failures are not this case's calls: broker.delivery_failure() fires when the
    # upstream request already succeeded and only the write back to the client socket
    # failed, so it increments neither `calls` nor `failures`.  The broker is a run-level
    # singleton sliced per case by stats_delta(), and the dominant producer of a delivery
    # failure is the PREVIOUS case being killed at its timeout with one request in flight:
    # that orphan resolves after that case's broker_after snapshot and so lands in this
    # case's window.  The raw count stays in broker_delta.json as evidence.
    for counter in ("failures", "protocol_failures", "upstream_failures"):
        value = int(delta.get(counter, 0) or 0)
        if value > 0:
            return "broker_infrastructure_error", f"evaluator broker recorded {counter}={value}"
    return None


def run_public_case(
    *,
    case_id: str,
    repository: Path,
    output: Path,
    broker_endpoint: str,
    runtime_python: str,
    result_judge_endpoint: str | None = None,
    readiness_profile: str | None = None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    candidate_digest = tree_digest(repository)
    # The product may write artifacts or caches.  Each dev gets a fresh copy;
    # the submitted source that will be frozen is never a runtime workspace.
    runtime_repository = output / "candidate_runtime"
    source_repository=repository
    repository = runtime_repository
    prompt = ROOT / str(load_case(case_id).get("prompt_file") or "")
    started_at = utc_now()
    try:
        before = read_broker_stats(broker_endpoint)
    except Exception as exc:
        return {
            "case_id": case_id,
            "terminal": True,
            "valid": False,
            "infra_valid": False,
            "classification": "broker_infrastructure_error",
            "score": None,
            "reason": f"broker stats unavailable before public case: {type(exc).__name__}: {exc}",
            "started_at": started_at,
        }
    command = [
        runtime_python,
        str(ROOT / "agentloop/lower_agent_launcher.py"),
        "--repository",
        str(repository),
        "--source-repository", str(source_repository),
        "--prompt",
        str(prompt),
        "--output",
        str(output),
        "--broker-endpoint",
        broker_endpoint,
        "--execute",
        "--python",
        runtime_python,
    ]
    process_exit = -1
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
            check=False,
        )
        process_exit = completed.returncode
        stdout = completed.stdout[-8000:]
        stderr = completed.stderr[-8000:]
    except subprocess.TimeoutExpired as exc:
        stderr = f"public lower-agent timeout: {exc}"
    except OSError as exc:
        stderr = f"public launcher OS error: {exc}"
    try:
        after = read_broker_stats(broker_endpoint)
        delta = stats_delta(before, after)
    except Exception as exc:
        after = None
        delta = None
        stats_error = f"{type(exc).__name__}: {exc}"
    else:
        stats_error = None
    launcher: dict[str, Any] | None
    try:
        launcher = read_json(output / "launcher_result.json")
    except (OSError, ValueError, json.JSONDecodeError):
        launcher = None
    observed = str((launcher or {}).get("classification") or "")
    if stats_error:
        classification, infra_valid, reason = (
            "evaluator_infrastructure_error",
            False,
            f"broker stats unavailable after public case: {stats_error}",
        )
    elif launcher is None:
        classification, infra_valid, reason = (
            "launcher_infrastructure_error",
            False,
            "launcher_result.json was not produced",
        )
    elif observed in INFRA:
        classification, infra_valid, reason = observed, False, str(
            launcher.get("error") or launcher.get("stderr_tail") or observed
        )
    elif (broker_failure := _broker_failure(delta)) is not None:
        classification, reason = broker_failure
        infra_valid = False
    elif observed == "candidate_fixture_precondition_failure" and launcher.get("runtime_probe", {}).get("infra_valid") is True:
        classification, infra_valid, reason = "candidate_capability_gap", True, str(launcher.get("error", observed))
    elif delta is not None and int(delta.get("successful_calls", 0)) <= 0:
        classification, infra_valid, reason = (
            "candidate_agent_failure",
            True,
            "executed lower agent made no model call while broker evidence shows no infrastructure failure",
        )
    elif observed.startswith("candidate_"):
        classification, infra_valid, reason = observed, True, str(
            launcher.get("classification_reason") or observed
        )
    elif not _artifact_valid(output, case_id):
        classification, infra_valid, reason = (
            "candidate_contract_failure",
            True,
            "DeepTutor did not produce a parseable terminal artifact",
        )
    else:
        classification, infra_valid, reason = (
            "candidate_valid",
            True,
            "real DeepTutor lower-agent execution produced a terminal artifact",
        )
    record = {
        "schema_version": "agentswe-deeptutor-public-case/v1",
        "case_id": case_id,
        "terminal": True,
        "valid": infra_valid,
        "infra_valid": infra_valid,
        "classification": classification,
        "score": None,
        "reason": reason,
        "started_at": started_at,
        "finished_at": utc_now(),
        "lower_agent_executed": bool(launcher and launcher.get("executed")),
        "terminal_product_artifact": _artifact_valid(output, case_id),
        "process_exit_code": process_exit,
        "broker_before": before,
        "broker_after": after,
        "broker_delta": delta,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
    }
    shared_root = Path("@@AGENTSWE_EDITING_CONTROL@@")
    if str(shared_root) not in sys.path:
        sys.path.insert(0, str(shared_root))
    from execution_scoring import judge_execution_case
    artifact = output / "agent_result.json"
    trajectory = output / "trajectory.jsonl"
    record = attest_execution(record, launcher or {}, output=output, case_id=case_id, candidate_digest=candidate_digest)
    # Scoring identity hashes this immutable evidence, not the score summary
    # that is written after the judge returns.
    write_json(output / "public_execution_evidence.json", record)
    # Builder-visible harness/tool errors, at Create parity: which evaluator
    # fixture tool call ran, which one raised, and with what exception class.
    # Attached AFTER public_execution_evidence.json is written, so the immutable
    # evidence the scoring identity hashes (execution_scoring.judge_execution_case
    # native_evidence) is byte-identical to before this patch.
    _fixture_diagnostic = fixture_tool_diagnostic(output)
    if _fixture_diagnostic:
        record['fixture_diagnostic'] = _fixture_diagnostic
    if readiness_profile:
        from agentloop.readiness import PROFILE, execution_valid
        if readiness_profile != PROFILE:
            raise ValueError('unknown readiness profile')
        record['readiness_execution_valid']=execution_valid(record)
        record.update(evaluation_mode='readiness_public_execution',judge_invoked=False,score=None)
        write_json(output / 'public_case_result.json', record)
        return record
    judge_dir = output / "semantic_scoring"

    def rejudge():
        return judge_execution_case(case_input=output / "executed_task.md", rubric=ROOT / "evaluator/result_rubric.md",
            artifact=artifact, raw_trajectory=trajectory, native_evidence=output / "public_execution_evidence.json",
            private_oracle=output / "fixture/fixture-observation.json", execution_record=record,
            candidate_digest=candidate_digest, case_id=case_id, output=judge_dir, broker_endpoint=result_judge_endpoint)

    judgement = rejudge()
    again = resample_refused_verdict(judge_dir, rejudge)
    if again is not None:
        judgement = again
    record.update({"score": judgement["score"], "classification": judgement["classification"],
        "valid": judgement["contract_valid"], "infra_valid": judgement["contract_valid"],
        "semantic_judgement": judgement, "reason": judgement.get("reason", reason)})
    write_json(output / "public_case_result.json", record)
    return record


class BuilderLifecycle:
    def __init__(
        self,
        *,
        run_dir: Path,
        workspace: Path,
        source: Path,
        public_broker: str,
        runtime_python: str,
        dev_cases: tuple[str, ...] = DEV_CASES,
        max_dev_rounds: int = 10,
        pilot_not_formal: bool = False,
        result_judge_endpoint: str | None = None,
        readiness_profile: str | None = None,
        current_binding: dict[str, Any] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.workspace = workspace
        self.source = source
        self.public_broker = public_broker
        self.runtime_python = runtime_python
        self.dev_cases = tuple(dev_cases)
        self.max_dev_rounds = max_dev_rounds
        self.pilot_not_formal = bool(pilot_not_formal)
        self.result_judge_endpoint = result_judge_endpoint
        self.readiness_profile=readiness_profile
        self.current_binding=current_binding
        self.prior_product_guard = PriorProductGuard.from_environment()
        self.token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.session_id = f"builder-session-{self.token[:20]}"
        self.socket_session_id = self.session_id
        self.native_observations = []
        self.feedback_deliveries = []
        # A feedback is "received" only when a write to the Builder's socket
        # returned. The Builder's shell-tool timeout can kill the helper while
        # the evaluator is still answering, and the response write then raises
        # BrokenPipeError: the round is accepted and its feedback bytes are on
        # disk, but feedback_deliveries stays empty and verify_native rejects
        # the whole session. Keep the ready response so a later connection can
        # deliver it for real, and receipt it at that later moment -- never
        # before, and never for a write that did not happen.
        # A dedicated lock: self.lock is held by submit() and validate()
        # for the whole evaluation, and a recovery connection must never
        # queue behind it -- status() deliberately takes no lock either.
        self.feedback_ledger_lock = threading.Lock()
        self.feedback_write_failures = []
        self.undelivered_feedback = {}
        self.public_attempts = {}
        self.delivery_records = {}
        self.connection_id = f"harbor-process-{hashlib.sha256(self.token.encode()).hexdigest()[:20]}"
        self.witness = {
            "schema_version": "agentswe-deeptutor-builder-witness/v1",
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "builder_model": BUILDER_MODEL,
            "builder_reasoning_effort": BUILDER_EFFORT,
            "transport": "evaluator-owned-single-session",
            "single_connection": True,
            "started_at": utc_now(),
        }
        self.controller = AcceptedSubmissionController(
            run_dir / "lifecycle", self.session_id, builder_witness=self.witness,
            dev_cases=self.dev_cases, max_dev_rounds=self.max_dev_rounds,
            pilot_not_formal=self.pilot_not_formal,
            readiness_profile=readiness_profile,current_binding=current_binding,
        )
        self.socket_path = Path(tempfile.gettempdir()) / f"deeptutor-builder-{self.token[:12]}.sock"
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.lock = threading.Lock()
        self.build_manifests: dict[int, dict[str, Any]] = {}
        self.submission_attempts: dict[int, int] = {}

    def start(self) -> None:
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    value = json.loads(self.rfile.readline(1 << 20))
                    if value.get("token") != owner.token:
                        code, payload = 401, {"error": "unauthorized"}
                    elif value.get("session_id") != owner.socket_session_id:
                        code, payload = 409, {"error": "builder_session_mismatch"}
                    elif value.get("action") == "status":
                        code, payload = 200, owner.status()
                    elif value.get("action") == "validate":
                        code, payload = owner.validate()
                    elif value.get("action") == "submit":
                        code, payload = owner.submit(str(value.get("feedback_digest") or ""))
                    elif value.get("action") == "feedback":
                        code, payload = owner.feedback()
                    else:
                        code, payload = 400, {"error": "unknown_action"}
                except Exception as exc:
                    code, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
                payload = owner.public_payload(payload)
                try:
                    self.wfile.write(
                        json.dumps({"status": code, "payload": payload}, ensure_ascii=False).encode()
                        + b"\n"
                    )
                    self.wfile.flush()
                except OSError as exc:
                    # The Builder's helper was killed, or its shell tool timed
                    # out, while the evaluator was still answering. Nothing
                    # arrived, so nothing is receipted: record the failed write
                    # and keep the ready response deliverable by a later
                    # --status or --feedback connection.
                    if code == 200:
                        owner.feedback_write_failed(payload, exc)
                    return
                if code == 200:
                    # feedback_written itself decides what this write actually
                    # carried, per record and by digest, so a --status or
                    # --feedback response can mint the receipt a lost submit
                    # owed -- and only when the bytes really went out.
                    owner.feedback_written(payload)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.socket_path.unlink(missing_ok=True)
        self.server = Server(str(self.socket_path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bind_native_thread(self):
        observation = observe_thread(self.run_dir, self.native_observations)
        if self.native_observations and observation['thread_id'] != self.session_id:
            raise RuntimeError('Builder native thread changed')
        if not self.native_observations:
            if self.controller.records:
                raise RuntimeError('Existing accepted ledger has no native thread binding')
            self.session_id = observation['thread_id']
            self.witness['session_id'] = self.session_id
            self.witness['native_thread_id'] = self.session_id
            self.controller.session_id = self.session_id
            self.controller.builder_witness = dict(self.witness)
            from agentloop.protocol import object_digest
            self.controller.builder_witness_digest = object_digest(self.witness)
            write_json(self.controller.run_dir / 'builder_session_opened.json', {
                'session_id': self.session_id, 'builder_witness': self.witness,
                'builder_witness_digest': self.controller.builder_witness_digest,
                'native_thread_observation': observation, 'formal_result_claimed': False})
        self.native_observations.append(observation)
        write_json(self.run_dir / 'native_thread_observations.json', self.native_observations)

    def public_payload(self, result):
        record = result.get('record') or {}
        feedback = result.get('feedback')
        if isinstance(record, dict) and record.get('consumed') is True:
            number = int(record['round'])
            feedback = read_json(self.controller.run_dir / f'feedback_after_candidate_{number:03d}.json')
            return {'state': 'accepted', 'submission_number': number,
                'builder_session_id': self.session_id, 'candidate_digest': record['candidate_digest'],
                **({'candidate_digest':record['delivery_candidate_digest'],'materialized_candidate_digest':record['candidate_digest']} if self.readiness_profile else {}),
                'feedback_digest': feedback['feedback_digest'],
                'feedback_digest_ack': record.get('feedback_digest_consumed'),
                'feedback': feedback, 'round_consumed': not result.get('idempotent', False),
                'idempotent': result.get('idempotent', False), 'dev_passed': record.get('dev_passed')}
        allowed = ('state', 'error', 'candidate_number', 'submission_consumed',
                   'retry_same_round', 'new_round_consumed', 'candidate_rounds_consumed',
                   'max_dev_rounds', 'builder_session_id')
        payload = {key: clean(result[key]) for key in allowed if key in result}
        if isinstance(result.get('build'), dict): payload['build'] = build_feedback(result['build'])
        if isinstance(record, dict) and record.get('infrastructure_cases'):
            payload['classification'] = 'evaluator_infrastructure_error'
            payload['dev_results'] = {key: public_case_feedback(value) for key, value in record.get('dev_results', {}).items()}
        if isinstance(feedback, dict):
            payload.update(feedback=feedback, feedback_digest=feedback['feedback_digest'])
            payload['submission_number'] = feedback['source_submission']
        if isinstance(result.get('records'), list):
            # Already an evaluator-built public projection of accepted rounds.
            # Like the accepted branch's feedback above, it must not be
            # re-clean()ed: the feedback bytes have to stay exactly the bytes
            # the embedded feedback_digest was taken over.
            payload['records'] = result['records']
        return payload

    @staticmethod
    def _carries_feedback(record):
        """True only when the authoritative feedback bytes themselves went out.

        This is verify_native's own computation (harbor/native_builder_evidence
        .py:304-311) run against what the socket write carried: pop the
        embedded feedback_digest, re-encode the rest exactly as
        two_round_controller built it -- agentloop/protocol.py:object_digest on
        the formal path, agentloop/readiness.py:canonical_feedback, which adds
        a trailing newline, on the readiness path -- and require the sha256 to
        be the popped digest. A reference that names a digest without carrying
        its bytes fails this and must never count as a delivery.
        """
        feedback = record.get('feedback')
        digest = record.get('feedback_digest')
        if not digest or not isinstance(feedback, dict) or not feedback:
            return False
        body = dict(feedback)
        if body.pop('feedback_digest', None) != digest:
            return False
        try:
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        except (TypeError, ValueError):
            return False
        return any(hashlib.sha256((encoded + suffix).encode('utf-8')).hexdigest() == digest
                   for suffix in ('', '\n'))

    @staticmethod
    def _delivered_records(payload):
        """Per-record views of what one response actually carried.

        A submit or accepted response is one record at top level; a status or
        feedback response carries the same per-record dicts under "records".
        Both are built from the same accepted record and the same
        feedback_after_candidate_NNN.json.
        """
        records = (payload or {}).get('records')
        if isinstance(records, list) and records:
            # A status or feedback response may also repeat its latest record
            # at the top level for compatibility. The records list is
            # authoritative and already contains it, so the top-level copy must
            # not mint a second receipt for the same round.
            for record in records:
                if isinstance(record, dict) and record.get('submission_number'):
                    yield record
            return
        if isinstance(payload, dict) and payload.get('submission_number'):
            yield payload

    def feedback_written(self, payload):
        """Receipt every feedback the socket write that just returned carried."""
        with self.feedback_ledger_lock:
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                digest = record['feedback_digest']
                payload_sha256 = hashlib.sha256(
                    json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.undelivered_feedback.pop(number, None)
                if any(row.get('candidate_number') == number and row.get('feedback_digest') == digest
                       and row.get('payload_sha256') == payload_sha256
                       for row in self.feedback_deliveries):
                    continue
                self.feedback_deliveries.append({'candidate_number': number,
                    'feedback_digest': digest,
                    'builder_session_id': self.session_id,
                    'evidence': 'socket_write_and_flush_completed',
                    'at': utc_now(),
                    'payload_sha256': payload_sha256})
            write_json(self.run_dir / 'native_feedback_deliveries.json', self.feedback_deliveries)

    def feedback_write_failed(self, payload, exc):
        """A ready response the Builder's socket never took.

        This is the opposite of a receipt: it records that delivery did not
        happen, and keeps the response deliverable by a later connection.
        """
        with self.feedback_ledger_lock:
            numbers = []
            receipted = {row.get('candidate_number') for row in self.feedback_deliveries}
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                numbers.append(number)
                if number not in receipted:
                    self.undelivered_feedback[number] = record
            self.feedback_write_failures.append({'at': utc_now(),
                'error': f"{type(exc).__name__}: {exc}",
                'candidate_numbers': numbers, 'builder_session_id': self.session_id})
            write_json(self.run_dir / 'native_feedback_write_failures.json', {
                'schema_version': 'deeptutor-feedback-delivery-ledger/v1',
                'write_failures': self.feedback_write_failures,
                'undelivered_candidate_numbers': sorted(self.undelivered_feedback)})

    def _record_view(self, record):
        """One accepted round in the same public shape, with its own feedback.

        Only controller.feedback_record -- the latest round's -- was ever
        published, so once round 2 is accepted nothing could re-deliver round 1,
        which is precisely the round whose write was lost. Reading each round's
        own feedback_after_candidate_NNN.json makes every accepted round
        recoverable and makes a receipt minted from this response provable. The
        digest is the body's own embedded feedback_digest, exactly the value
        native_attestation binds the record to.
        """
        number = int(record['round'])
        feedback = read_json(self.controller.run_dir / f'feedback_after_candidate_{number:03d}.json')
        return {'state': 'accepted', 'submission_number': number,
            'builder_session_id': self.session_id,
            'candidate_digest': record.get('delivery_candidate_digest', record['candidate_digest']),
            'feedback_digest': feedback['feedback_digest'],
            'feedback_digest_ack': record.get('feedback_digest_consumed'),
            'feedback': feedback, 'dev_passed': record.get('dev_passed')}

    def accepted_record_views(self):
        views = []
        for record in self.controller.records:
            try:
                views.append(self._record_view(record))
            except (OSError, ValueError, KeyError):
                continue
        return views

    def feedback(self):
        """Re-deliver one accepted record whose earlier response was lost.

        This re-reads the same authoritative feedback the accepted submission
        already carries; it issues nothing new and consumes no round. The
        oldest response a failed write left undelivered is preferred.
        """
        views = self.accepted_record_views()
        if not views:
            return 404, {'error': 'no accepted submission has feedback yet'}
        pending = [n for n in sorted(self.undelivered_feedback) if 1 <= n <= len(views)]
        return 200, {'builder_session_id': self.session_id,
            'candidate_rounds_consumed': len(self.controller.records),
            'max_dev_rounds': self.max_dev_rounds,
            'records': [views[(pending[0] if pending else len(views)) - 1]]}

    def status(self):
        if self.readiness_profile:
            self.bind_native_thread()
        return {'builder_session_id': self.session_id,
            'candidate_rounds_consumed': len(self.controller.records),
            'max_dev_rounds': self.max_dev_rounds, 'feedback': self.controller.feedback_record,
            'records': self.accepted_record_views()}

    def native_attestation(self, exit_code):
        records = []
        source_errors = []
        for record in self.controller.records:
            number = int(record['round'])
            path = self.controller.run_dir / f'feedback_after_candidate_{number:03d}.json'
            feedback = read_json(path)
            if tree_digest(Path(record['candidate_path'])) != record['candidate_digest']:
                source_errors.append('accepted product changed after public evaluation')
            records.append({'builder_session_id': record['session_id'],
                'candidate_digest': record.get('delivery_candidate_digest',record['candidate_digest']), 'feedback_path': str(path),
                'readiness_profile':self.readiness_profile,
                'feedback_digest': feedback['feedback_digest'],
                'feedback_digest_ack': record.get('feedback_digest_consumed'),
                'build': {'candidate_repo_digest': record['candidate_digest']}})
        budget_complete = len(records) == self.max_dev_rounds
        proof = verify_native(self.run_dir, records, self.feedback_deliveries,
            self.native_observations, allow_interrupted=(exit_code != 0 and budget_complete))
        proof['errors'].extend(source_errors)
        proof['valid'] = bool(proof['valid'] and not source_errors and (exit_code == 0 or budget_complete))
        proof['builder_exit_code'] = exit_code
        proof['accepted_count'] = len(records)
        write_json(self.run_dir / 'builder_native_attestation.json', proof)
        return proof

    def _prepare_build(self, *, round_no: int, trigger: str) -> tuple[Path, Path, dict[str, Any]]:
        """Snapshot and build-check the delivery without consuming a Candidate round."""
        if self.readiness_profile:
            from agentloop.readiness import validate_metadata
            previous=self.controller.records[-1] if self.controller.records else None
            expected={'builder_session_id':self.session_id,'submission_number':round_no,
                'revision_of_candidate_digest':previous['delivery_candidate_digest'] if previous else None,
                'feedback_digest':self.controller.feedback_record['feedback_digest'] if previous else None}
            validate_metadata(self.workspace,expected)
        self.submission_attempts[round_no] = self.submission_attempts.get(round_no, 0) + 1
        attempt = self.submission_attempts[round_no]
        category = "validations" if trigger == "builder_request" else "submissions"
        submission = (
            self.run_dir
            / category
            / f"candidate_{round_no:03d}_attempt_{attempt:03d}"
        )
        shutil.copytree(self.workspace, submission, symlinks=False)
        build_dir = (
            self.run_dir
            / ("validation_builds" if trigger == "builder_request" else "builds")
            / f"candidate_{round_no:03d}_attempt_{attempt:03d}"
        )
        build = materialize_candidate(
            self.source,
            submission,
            build_dir,
            runtime_python=self.runtime_python,
            readiness=bool(self.readiness_profile),
        )
        build.update({
            "candidate_number": round_no,
            "submission_attempt": attempt,
            "submission_consumed": False,
            "retry_same_round": build.get("valid") is not True,
            "preflight_trigger": trigger,
        })
        write_json(build_dir / "build_manifest.json", build)
        return submission, build_dir, build

    def validate(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            if self.readiness_profile:
                self.bind_native_thread()
            round_no = len(self.controller.records) + 1
            if round_no > self.max_dev_rounds:
                return 409, {"error": "maximum accepted Candidate rounds are already consumed"}
            _submission, _build_dir, build = self._prepare_build(
                round_no=round_no, trigger="builder_request"
            )
            if build.get("valid") is True:
                return 200, {
                    "state": "candidate_ready_for_submission",
                    "candidate_number": round_no,
                    "submission_consumed": False,
                    "retry_same_round": False,
                    "build": build,
                }
            classification = str(build.get("classification") or "candidate_build_failure")
            return (503 if classification in INFRA else 422), {
                "state": "preflight_rejected",
                "candidate_number": round_no,
                "submission_consumed": False,
                "retry_same_round": True,
                "build": build,
            }

    def submit(self, feedback_digest: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.bind_native_thread()
            delivery_errors = validate_delivery(self.workspace,readiness=bool(self.readiness_profile))
            if delivery_errors:
                return 422, {'state': 'delivery_invalid', 'error': '; '.join(delivery_errors), 'submission_consumed': False}
            delivery_digest = tree_digest(self.workspace)
            existing = self.delivery_records.get(delivery_digest)
            if existing is not None:
                if self.readiness_profile:
                    return 409, {'state':'duplicate_delivery_rejected','new_round_consumed':False}
                if tree_digest(Path(existing['candidate_path'])) != existing['candidate_digest']:
                    raise RuntimeError('cached accepted Candidate bytes changed')
                return 200, {'state': 'accepted', 'idempotent': True,
                    'new_round_consumed': False, 'record': existing}
            round_no = len(self.controller.records) + 1
            if round_no > self.max_dev_rounds:
                return 409, {"error": "maximum accepted Candidate rounds are already consumed"}
            if round_no > 1:
                expected = str((self.controller.feedback_record or {}).get("feedback_digest") or "")
                if not expected or feedback_digest != expected:
                    return 409, {"error": "the next Candidate must acknowledge the exact evaluator feedback digest"}
            elif feedback_digest:
                return 409, {"error": "Candidate 1 cannot acknowledge feedback"}

            submission, _build_dir, build = self._prepare_build(
                round_no=round_no, trigger="submission_gate"
            )
            self.build_manifests[round_no] = build
            if build.get("valid") is not True:
                classification = str(build.get("classification") or "candidate_build_failure")
                return (503 if classification in INFRA else 422), {
                    "state": (
                        "infrastructure_preflight_not_consumed"
                        if classification in INFRA
                        else "candidate_preflight_not_consumed"
                    ),
                    "candidate_number": round_no,
                    "submission_consumed": False,
                    "retry_same_round": True,
                    "build": build,
                }

            candidate_for_controller = Path(str(build["repository"]))
            submitted_digest = tree_digest(candidate_for_controller)
            if self.prior_product_guard is not None and self.prior_product_guard.blocks(candidate_for_controller):
                refusal = {'state': 'prior_product_execution_replay_blocked',
                    'candidate_digest': submitted_digest, 'submission_consumed': False,
                    'new_round_consumed': False, 'retry_same_round': False,
                    'error': 'This exact product was dispatched in a sealed prior run; preserved without replay.'}
                write_json(_build_dir / 'prior_product_refusal.json',
                    {**refusal, 'guard': self.prior_product_guard.binding()})
                return 503, refusal
            existing = next((record for record in self.controller.records if record.get("candidate_digest") == submitted_digest), None)
            if existing is not None:
                if self.readiness_profile:
                    return 409, {'state':'duplicate_product_rejected','new_round_consumed':False}
                return 200, {"state": "accepted", "idempotent": True, "new_round_consumed": False,
                    "record": existing, "feedback": self.controller.feedback_record, "freeze": self.controller.frozen}
            attempt_file = self.run_dir / 'public_attempts' / (submitted_digest + '.json')
            if attempt_file.exists():
                previous = read_json(attempt_file)
                if previous.get('candidate_digest') != submitted_digest:
                    raise RuntimeError('public attempt identity mismatch')
                return 503, {'state': 'infrastructure_attempt_not_consumed',
                    'error': 'An earlier execution of this exact product is incomplete or infrastructure-invalid; it is preserved without replay.',
                    'retry_same_round': False, 'new_round_consumed': False}
            write_json(attempt_file, {'candidate_digest': submitted_digest,
                'state': 'reserved_before_dev', 'started_at': utc_now()})
            submission_attempt = int(build["submission_attempt"])
            public_attempt_root = (
                self.run_dir
                / "public"
                / f"candidate_{round_no:03d}_attempt_{submission_attempt:03d}"
            )
            dev_results = {
                case_id: run_public_case(
                    case_id=case_id,
                    repository=candidate_for_controller,
                    output=public_attempt_root / case_id,
                    broker_endpoint=self.public_broker,
                    runtime_python=self.runtime_python,
                    result_judge_endpoint=self.result_judge_endpoint,
                    readiness_profile=self.readiness_profile,
                )
                for case_id in self.dev_cases
            }
            readiness_evidence=None
            if self.readiness_profile:
                # Imported under its own name: `delivery_digest` is already the
                # workspace tree digest (a str) from the top of this method, and
                # a function-scope import rebinds the local for the whole body.
                # It used to leave line 1047 keying delivery_records with a
                # function, so json.dumps raised TypeError and the Builder got a
                # 400 instead of the accepted round's candidate digest -- which
                # round 2 must echo back and has no other way to learn.
                from agentloop.readiness import delivery_digest as readiness_delivery_digest
                previous=self.controller.records[-1] if self.controller.records else None
                readiness_evidence={'delivery_path':str(submission),'delivery_candidate_digest':readiness_delivery_digest(submission),
                    'revision_of_candidate_digest':previous['delivery_candidate_digest'] if previous else None,
                    'readiness_build':build['readiness_build'],'build_manifest_path':str(_build_dir/'build_manifest.json'),
                    'public_execution_path':str(public_attempt_root/'dev_001/public_case_result.json')}
            record = self.controller.submit(
                candidate_for_controller,
                round_no,
                dev_results,
                feedback=self.controller.feedback_record if round_no > 1 else None,
                builder_session_id=self.session_id,
                builder_witness=self.witness,
                readiness_evidence=readiness_evidence,
            )
            write_json(attempt_file, {'candidate_digest': submitted_digest,
                'state': 'accepted' if record.get('consumed') else 'infrastructure_invalid',
                'record': record})
            if record.get("consumed") is not True:
                return 503, {
                    "state": "infrastructure_attempt_not_consumed",
                    "record": record,
                    "retry_same_round": True,
                }
            self.delivery_records[delivery_digest] = record
            write_json(self.run_dir / 'accepted_delivery_index.json', {
                digest: {'candidate_digest': item['candidate_digest'], 'round': item['round'],
                    'session_id': item['session_id']} for digest, item in self.delivery_records.items()})
            return 200, {
                "state": "accepted",
                "record": record,
                "feedback": self.controller.feedback_record,
                "freeze": None,
            }

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.socket_path.unlink(missing_ok=True)


def stage_builder_package(
    destination: Path, source: Path, dev_cases: tuple[str, ...] = DEV_CASES
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        ROOT / "input",
        destination / "input",
        symlinks=False,
        ignore=shutil.ignore_patterns("repository"),
    )
    (destination / "dev_cases").mkdir()
    for case_id in dev_cases:
        shutil.copytree(ROOT / "dev_cases" / case_id, destination / "dev_cases" / case_id, symlinks=False)
    shutil.copytree(source, destination / "input" / "repository", symlinks=False)


SUBMIT_WAITING_INSTRUCTION = """

## Waiting for an evaluation, and recovering a lost response

A submission runs the public dev cases and can take far longer than one shell
tool call, so `submit_dev_candidate` hands the submit to a detached helper and
returns early instead of holding one long command. If it prints status 202 with
state submission_in_progress, run `submit_dev_candidate --await-only` again, as
many times as needed, until it prints the real response. Never wrap the command
in `nohup`, never background or `disown` it, and never kill it.

At any time, including while an evaluation is still running,
`submit_dev_candidate --status` answers within seconds with every accepted
record, its own full feedback and its exact digest, and
`submit_dev_candidate --feedback` reprints one accepted record's feedback and
digest. If a response is ever lost, recover it with one of those two commands;
do not resubmit an unchanged Candidate.
"""


def write_builder_task(
    *,
    run_dir: Path,
    public: Path,
    workspace: Path,
    controller: BuilderLifecycle,
    provider_config: Path,
) -> Path:
    task = run_dir / "builder_task"
    # Formal must stop inside the outer --builder-timeout; the pilot/readiness
    # branch keeps the historical cap it has always been launched with.
    builder_task_timeout_seconds = (
        PILOT_BUILDER_TASK_TIMEOUT_SECONDS if controller.pilot_not_formal
        else GENERATED_BUILDER_TASK_TIMEOUT_SECONDS
    )
    worktree = run_dir / "builder_workspace" / "worktree"
    if worktree.exists():
        raise RuntimeError('refusing to replace an existing Builder worktree')
    shutil.copytree(public / "input" / "repository", worktree, symlinks=True)
    # Give the Builder a local, run-owned baseline so delivery generation is a
    # bounded mechanical step.  The baseline never enters the Candidate: only
    # the Builder-authored Git patch in /workspace/submission is materialized.
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "AgentSWE Builder Baseline"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "builder-baseline@invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-gpg-sign", "-m", "Evaluator-owned Builder baseline"],
        cwd=worktree,
        check=True,
    )
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    resource_check = task / 'environment/builder_resource_check.py'
    shutil.copyfile(ROOT / 'harbor/builder_resource_check.py', resource_check)
    persistent_home = run_dir / 'builder_workspace/codex_home'
    persistent_home.mkdir(parents=True, exist_ok=False)
    instruction = """# DeepTutor Edit Builder — one continuous feedback session

You are the only upper Builder. Read /builder-package/input, the visible public dev
case, and /builder-package/input/repository. Never inspect hidden cases,
evaluator code, credentials, prior runs, or Candidate snapshots.

Work directly in the writable source tree at /workspace/worktree. Keep exactly
solution.patch, edit_report.json, and run_report.json in /workspace/submission;
never copy the repository or its .git directory into the submission directory.
The worktree already has a run-owned baseline commit. After editing, generate
the patch with exactly:
`mkdir -p /workspace/submission && git -C /workspace/worktree add -N . && git -C /workspace/worktree diff --binary --no-ext-diff HEAD -- > /workspace/submission/solution.patch`.
This yields repository-relative Git headers in the exact form
`diff --git a/path b/path` and `+++ b/path`; do not use `diff -ruN` headers or
`/workspace/...` paths. Before each submission, first run
`test -s /workspace/submission/solution.patch`,
then run this parser-compatible path check:
`python3 - <<'PY'\nfrom pathlib import Path\np=Path('/workspace/submission/solution.patch').read_text()\nlines=p.splitlines()\nassert any(line.startswith('diff --git a/') and ' b/' in line for line in lines) or any(line.startswith('+++ b/') for line in lines)\nfor line in lines:\n    if line.startswith('diff --git '):\n        parts=line.split()\n        assert len(parts) >= 4 and parts[2].startswith('a/') and parts[3].startswith('b/')\n    elif line.startswith(('--- ', '+++ ')):\n        path=line[4:].split('\\t', 1)[0]\n        assert path == '/dev/null' or path.startswith(('a/', 'b/'))\nPY`
and equivalent `test -s` checks for edit_report.json and run_report.json. If a
JSON report is missing, write it before validating; the minimum valid templates
are `{"commands": [], "compatibility_notes": [], "limitations": []}` for
edit_report.json and
`{"schema_version":"1.0","status":"completed","artifact_paths":["solution.patch","edit_report.json","run_report.json"],"errors":[],"runtime_seconds":0,"peak_memory_bytes":0,"api_calls":{"gateway":0,"serper":0,"web_retrieval":0}}` for run_report.json.
Then run `validate_dev_candidate`; validation failures do not consume an
accepted-submission slot, so fix them and validate again. Create a Candidate
and run `submit_dev_candidate --wait`. The evaluator materializes it and
executes every configured public case through the real patched DeepTutor
mastery agent. Read the returned feedback and exact digest. In this same
uninterrupted session, make a genuine feedback-driven revision for every later
submission, regenerate all three delivery files, validate again, and submit
with the exact latest feedback digest. Each accepted submission must have a
distinct digest. You may make at most five accepted submissions; infrastructure
invalid attempts do not consume a slot. A dev mean above 60 is recorded as
`dev_passed` feedback and does not freeze the Candidate. When done, return from
the Builder session; the evaluator freezes the latest accepted Candidate and
only then runs the hidden cases.

Your Candidate must implement the lower-agent terminal artifact boundary in
the actual DeepTutor product. When the evaluator supplies
`DEEPTUTOR_AGENT_RESULT`, the product's mastery/chat loop must require the
model's final response to be one JSON object with `schema_version`, `case_id`,
`status`, `summary`, and `artifacts`, then atomically persist that exact
model-authored object to the supplied path. Implement this in product code at
the common agent-loop final-result seam. Do not create a default object, copy a
native result, turn tool events into an artifact, or fill a missing/malformed
response. The evaluator only captures the product-written file after process
exit and treats a missing file as Candidate evidence failure; raw trajectory
remains separate.

Before the first submission, also implement the complete public mastery-tool
contract in `/builder-package/input/05_mastery_tool_contract.md`. Prioritize
the full registered surface and its importability over optional refactoring:
the original five mastery tools alone are an incomplete Candidate. Verify that
the product's registered name and type collections agree. Do not copy evaluator
code or synthesize tool results; implement the product capability in the
Candidate repository.

Use this bounded first-submission source map instead of inventorying the whole
repository:

- registry and existing tool patterns:
  `deeptutor/capabilities/mastery/tools.py`,
  `deeptutor/tools/mastery_tool.py`;
- mastery loop and persistent state patterns:
  `deeptutor/capabilities/mastery/loop.py`,
  `deeptutor/learning/models.py`, `deeptutor/learning/storage.py`, and
  `deeptutor/learning/service.py`;
- terminal model-response seam:
  `deeptutor/agents/chat/agent_loop.py`,
  `deeptutor/core/agentic/loop.py`, and `deeptutor/services/file_io.py`.

Before the first edit, use at most eight read-only shell/tool actions. Keep each
inspection under 200 output lines and 12,000 characters. Do not run repository-
wide `find`, repository-wide `rg`, `diff -qr`, or whole-file dumps of large
modules. Read narrow symbol ranges from the source-map files, then implement a
compact shared path-local state/service plus thin registered tool classes. Do
not spend the first submission window exhaustively modeling hidden-style edge
cases; expose the complete public names with safe, persistent, fail-closed
behavior and use public evaluator feedback to deepen semantics.

Keep the first Builder turn bounded: once the complete public registry and the
terminal-artifact seam form a smallest coherent product slice and the public
validation checks pass, submit immediately. Use evaluator feedback for deeper
behavioral revisions instead of spending the entire first turn on optional
refactoring or exhaustive hidden-style implementation. A partial five-tool
registry is not coherent and must not be submitted as complete.

The Builder image is intentionally not the product's dependency-complete lower runtime.
If an optional product import such as `yaml` is unavailable during a
Builder-side probe, record it and continue with Python 3 syntax/compile checks,
AST/registry-shape checks, and the required delivery validation. Do not run the full DeepTutor agent loop,
install dependencies, or debug the complete product
environment before the first submission; the evaluator-owned lower runtime
performs dependency-complete execution after Candidate acceptance. As soon as
compile/shape checks pass, generate the three delivery files, validate, and
submit. This instruction changes only Builder pacing and does not waive product
implementation or lower-runtime validation requirements.

Every evaluator case begins with the evaluator calling the product tool
`mastery_remediation_claim` (path, consumer_id, event_id, lease_seconds) and
requires its response to carry the persisted `delivery` row before any model
call is made. A product whose claim returns no delivery fails the case
precondition: the case ends with zero model calls and is recorded as a fatal
Candidate product failure. Make sure that tool and its persisted delivery work.
"""
    if controller.pilot_not_formal and not controller.readiness_profile:
        instruction += """

This is a non-formal pilot with at most two accepted submissions. Each
submission is evaluated on both public cases, dev_001 and dev_002. After
receiving the first authoritative feedback, revise the product, acknowledge
that feedback digest and submit the second distinct Candidate, then stop.
The evaluator freezes the latest accepted Candidate and runs only test_001.
Independent Result and Code judges measure smoke evidence; this is not a
formal six-hidden evaluation.
"""
    if controller.readiness_profile:
        instruction=instruction.replace('both public cases','dev_001').replace('both public dev cases','dev_001')
        instruction += '''
Readiness profile single-dev-two-round-hidden-smoke-v1 requires exactly two
accepted dev_001 rounds in this uninterrupted native session. Obtain your actual
builder_session_id from submit_dev_candidate --status before preparing reports.
Include builder_session_id, submission_number, revision_of_candidate_digest and
feedback_digest in run_report.json in addition to its seven existing fields.
For round 1 both digest fields are null. For round 2 use the accepted response's
candidate_digest (delivery identity) and exact feedback_digest; include a nonempty
edit_report.feedback_response explaining the functional revision. Both patch bytes
and materialized product must change. Validate using validate_dev_candidate before
each submit. The evaluator uses its same isolated dependency-complete Python for
the build. After the second accepted round, exit successfully. The evaluator then
freezes round 2, executes test_001 and independent Result/Code smoke. Public scores
are not admission thresholds and public rounds do not invoke a semantic judge.
'''
    (task / "instruction.md").write_text(
        instruction + SUBMIT_WAITING_INSTRUCTION,
        encoding="utf-8",
    )
    submit = task / "environment" / "submit_dev_candidate"
    submit.write_text(
        """#!/usr/bin/env python3
\"\"\"Builder-side controller client.

A dev evaluation runs the public cases and takes far longer than one shell tool
call, so the submit runs in a detached, fully daemonised holder. The foreground
process only polls a local response file in short bounded waits: a shell-tool
timeout can kill the poller without cutting the evaluator response off. Nothing
here needs nohup, backgrounding or kill. Every request still carries the
Builder session id the evaluator binds the socket to.
\"\"\"
import argparse, json, os, socket, sys, time
from pathlib import Path

TMP = Path(os.environ.get("TMPDIR") or "/tmp")
RESPONSE = TMP / "agentswe-submit-response.json"
HOLDER = TMP / "agentswe-submit-holder.pid"
VALIDATE_RESPONSE = TMP / "agentswe-validate-response.json"
VALIDATE_HOLDER = TMP / "agentswe-validate-holder.pid"

parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]))
parser.add_argument("--feedback-digest", dest="feedback_digest",
                    help="exact digest of the feedback this revision answers")
parser.add_argument("--wait", action="store_true",
                    help="accepted for compatibility; a submit always waits by polling")
parser.add_argument("--status", action="store_true",
                    help="every accepted record, its own full feedback and its exact digest")
parser.add_argument("--feedback", action="store_true",
                    help="reprint one accepted record's evaluator feedback and its exact digest")
parser.add_argument("--validate", action="store_true", help="run the evaluator-owned preflight")
parser.add_argument("--await-only", dest="await_only", action="store_true",
                    help="keep waiting for the submission already running")
parser.add_argument("--poll-seconds", dest="poll_seconds", type=float, default=240.0,
                    help="how long this call polls before reporting 202")
known, _extra = parser.parse_known_args()


def call(payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"])
        sock.sendall((json.dumps({"token": os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"], "session_id": os.environ["AGENTSWE_BUILDER_SESSION_ID"], **payload}) + "\\n").encode())
        return json.loads(sock.makefile().readline())


def finish(result):
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("status") in (200, 202) else 1)


def holder_alive():
    try:
        os.kill(int(HOLDER.read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def alive(pid_path):
    try:
        os.kill(int(pid_path.read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def poll_path(path, deadline):
    while True:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(2)


def poll(deadline):
    return poll_path(RESPONSE, deadline)


def detach(request, response_path, holder_path):
    # Run one request in a fully daemonised holder. The foreground caller only
    # polls response_path, so a shell-tool timeout kills the poller and never
    # the evaluator connection: the answer still lands on disk and the next
    # call reprints it instead of the evaluator writing to a closed pipe.
    for item in (response_path, holder_path):
        try:
            item.unlink()
        except OSError:
            pass
    if os.fork() == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        holder_path.write_text(str(os.getpid()))
        try:
            result = call(request)
        except Exception as exc:
            result = {"status": 599, "payload": {"error": type(exc).__name__ + ": " + str(exc)}}
        part = response_path.with_suffix(".part")
        part.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(str(part), str(response_path))
        try:
            holder_path.unlink()
        except OSError:
            pass
        os._exit(0)
    os.wait()


name = os.path.basename(sys.argv[0])
if known.status:
    finish(call({"action": "status"}))
if known.feedback:
    finish(call({"action": "feedback"}))
if known.validate or name == "validate_dev_candidate":
    # The preflight copies the delivery and runs the controlled build, which can
    # outlast one shell tool call. Route it through the same detached holder the
    # submit uses: a killed foreground poller loses nothing, and running the
    # same command again (optionally with --await-only) reprints the preflight
    # the holder already stored.
    if not known.await_only and not alive(VALIDATE_HOLDER):
        detach({"action": "validate"}, VALIDATE_RESPONSE, VALIDATE_HOLDER)
    preflight = poll_path(VALIDATE_RESPONSE, time.time() + max(1.0, known.poll_seconds))
    if preflight is None:
        print(json.dumps({"status": 202, "payload": {"state": "validate_in_progress",
            "reason": "the evaluator preflight is still building this Candidate; run "
                      "submit_dev_candidate --validate --await-only again to keep waiting. "
                      "The preflight result is kept on disk, so a shell-tool timeout never "
                      "loses it and the next call reprints it"}},
            ensure_ascii=False, indent=2))
        raise SystemExit(3)
    finish(preflight)

if not known.await_only and not holder_alive():
    for item in (RESPONSE, HOLDER):
        try:
            item.unlink()
        except OSError:
            pass
    if os.fork() == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        # Detach every inherited descriptor so the Builder shell tool sees EOF
        # at once and never waits on this holder.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        HOLDER.write_text(str(os.getpid()))
        try:
            result = call({"action": "submit", "feedback_digest": known.feedback_digest or ""})
        except Exception as exc:
            result = {"status": 599, "payload": {"error": type(exc).__name__ + ": " + str(exc)}}
        part = RESPONSE.with_suffix(".part")
        part.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(str(part), str(RESPONSE))
        try:
            HOLDER.unlink()
        except OSError:
            pass
        os._exit(0)
    os.wait()

result = poll(time.time() + max(1.0, known.poll_seconds))
if result is None:
    print(json.dumps({"status": 202, "payload": {"state": "submission_in_progress",
        "reason": "the evaluator is still running the public dev cases; run "
                  "`submit_dev_candidate --await-only` again to keep waiting, or "
                  "`submit_dev_candidate --status` for every accepted record and its feedback"}},
        ensure_ascii=False))
    raise SystemExit(3)
finish(result)
""",
        encoding="utf-8",
    )
    submit.chmod(0o755)
    (task / "tests" / "test.sh").write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    (task / "tests" / "test.sh").chmod(0o755)
    (task / "task.toml").write_text(
        f'''schema_version = "1.4"
[task]
name = "local/deeptutor-adaptive-remediation-agentloop-builder"
version = "1.0.0"
        description = "DeepTutor Edit accepted-submission public feedback Builder"
artifacts = [{{ source = "/workspace/submission", destination = "builder_submission" }}]
[agent]
timeout_sec = {float(builder_task_timeout_seconds)}
user = "root"
network_mode = "public"
[verifier]
timeout_sec = 300.0
user = "root"
network_mode = "no-network"
[environment]
docker_image = "{BUILDER_IMAGE}"
network_mode = "public"
build_timeout_sec = 900.0
cpus = 8
memory_mb = 16384
storage_mb = 32768
workdir = "/workspace"
[environment.healthcheck]
command = "python3 /usr/local/lib/agentswe-builder-resource-check.py"
retries = 1
timeout_sec = 10.0
''',
        encoding="utf-8",
    )
    provider_host_path = provider_config.resolve()
    if not provider_host_path.is_absolute() or not provider_host_path.is_file():
        raise RuntimeError("Builder provider config must be an existing run-local host file")
    write_json(task / "environment" / "docker-compose.yaml", {
        "services": {"main": {
            "command": ["sh", "-c", "mkdir -p /tmp/codex-home && exec sleep infinity"],
            "cpu_quota": 800000, "cpu_period": 100000,
            "volumes": [
                {"type": "bind", "source": str(persistent_home), "target": "/tmp/codex-home"},
                {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
                {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
                {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
                {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
                {"type": "bind", "source": str(controller.socket_path), "target": "/run/deeptutor-builder.sock", "read_only": True},
                {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
                {"type": "bind", "source": str(submit), "target": "/usr/local/bin/validate_dev_candidate", "read_only": True},
                {"type": "bind", "source": str(provider_host_path), "target": str(provider_host_path), "read_only": True},
            ],
            "environment": {
                "AGENTSWE_DEV_CONTROLLER_SOCKET": "/run/deeptutor-builder.sock",
                "AGENTSWE_DEV_CONTROLLER_TOKEN": controller.token,
                "AGENTSWE_BUILDER_SESSION_ID": controller.socket_session_id,
                # The run-owned baseline is initialized by the host UID and
                # mounted into a root-run Builder container.  Supply the sole
                # allowed worktree through protected command-scope Git config
                # so Git does not reject the bind mount as dubious ownership.
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "/workspace/worktree",
            },
        }}
    })
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"deeptutor-agentloop-builder-{run_dir.name}",
        "jobs_dir": str(run_dir / "jobs"),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "quiet": True,
        "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": not bool(getattr(controller, "readiness_profile", None)), "cpu_enforcement_policy": "limit"},
        "agents": [{
            "import_path": "agentswe_codex_resume:CodexResume",
            "model_name": BUILDER_MODEL,
            "env": {
                "CODEX_HOME": "/tmp/agentswe-codex-home",
                "CODEX_CONFIG_TOML_PATH": str(provider_host_path),
            },
            "kwargs": {"reasoning_effort": BUILDER_EFFORT},
        }],
        "tasks": [{"path": str(task)}],
    })
    return config


def run_harbor(harbor: Path, config: Path, run_dir: Path, timeout: int, *, lifecycle, credential, proxy='http://127.0.0.1:7890', provider_url='https://api.deepseek.com/v1') -> int:
    return run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
        harbor=harbor, timeout=timeout, proxy=proxy, provider_url=provider_url).returncode


def protocol_lock() -> dict[str, Any]:
    return {
        "schema_version": "agentswe-deeptutor-formal-protocol/v1",
        "builder": {
            "model": BUILDER_MODEL,
            "reasoning_effort": BUILDER_EFFORT,
            "same_session": True,
        },
        "lower": {
            "product": "DeepTutor",
            "model": LOWER_MODEL,
            "reasoning_effort": LOWER_EFFORT,
            "candidate_credential": PLACEHOLDER_TOKEN,
        },
        "public_cases_per_candidate": list(DEV_CASES),
        "hidden_cases": list(HIDDEN_CASES),
        "hidden_after_freeze": True,
        "formal_result_claimed": False,
        "code_score_claimed": False,
    }


def pilot_lock() -> dict[str, Any]:
    return {
        "schema_version": "agentswe-deeptutor-pilot-protocol/v1",
        "pilot_not_formal": True,
        "max_accepted_submissions": 2,
        "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "same_session": True},
        "lower": {"product": "DeepTutor", "model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER_TOKEN},
        "public_cases_per_candidate": list(DEV_CASES),
        "hidden_cases": ["test_001"],
        "fresh_hidden_broker_after_freeze": True,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "result_axis": "N/A",
        "code_axis": "N/A",
    }


def static_audit(run_dir: Path) -> dict[str, Any]:
    required = (
        "agentloop/broker.py",
        "agentloop/candidate_adapter.py",
        "agentloop/lower_agent_launcher.py",
        "agentloop/runtime_probe.py",
        "agentloop/two_round_controller.py",
        "agentloop/run_hidden.py",
    )
    missing = [relative for relative in required if not (ROOT / relative).is_file()]
    lock = protocol_lock()
    write_json(run_dir / "protocol_lock.json", lock)
    summary = {
        "schema_version": "agentswe-deeptutor-one-stop-static-audit/v1",
        "status": "static_audit_pass" if not missing else "static_audit_fail",
        "mode": "provider-free-static-audit",
        "required_files_missing": missing,
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "lower_model": LOWER_MODEL,
        "lower_reasoning_effort": LOWER_EFFORT,
        "candidate_credential": PLACEHOLDER_TOKEN,
        "public_case_count": len(DEV_CASES),
        "hidden_case_count": len(HIDDEN_CASES),
        "network_calls": 0,
        "docker_started": False,
        "broker_started": False,
        "harbor_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "audited_at": utc_now(),
    }
    write_json(run_dir / "summary.json", summary)
    return summary


def self_test() -> dict[str, Any]:
    required = (
        ROOT / "agentloop/broker.py",
        ROOT / "agentloop/candidate_adapter.py",
        ROOT / "agentloop/lower_agent_launcher.py",
        ROOT / "agentloop/two_round_controller.py",
        ROOT / "agentloop/run_hidden.py",
    )
    passed = (
        all(path.is_file() for path in required)
        and LOWER_MODEL == "deepseek-flash"
        and BUILDER_EFFORT == "max"
        and LOWER_EFFORT == "high"
        and PLACEHOLDER_TOKEN == "broker-only-placeholder"
        and DEV_CASES == ("dev_001", "dev_002")
        and HIDDEN_CASES == tuple(f"test_{index:03d}" for index in range(1, 7))
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "default_mode": "provider-free-static-audit",
        "formal_requires_explicit_flag": True,
        "pilot_path_ready": passed,
        "pilot_public_cases": list(DEV_CASES),
        "pilot_hidden_cases": ["test_001"],
        "pilot_not_formal": True,
    }


def pilot_public_complete(record: dict[str, Any], readiness: bool = False) -> bool:
    """A smoke submission must carry the same two-dev coverage as formal."""
    results = record.get("dev_results", {})
    return isinstance(results, dict) and set(results) == ({'dev_001'} if readiness else set(DEV_CASES))


def run_pilot(args: argparse.Namespace) -> int:
    """Run two public dev cases per Candidate and one scored hidden smoke."""
    readiness_profile=getattr(args,'readiness_profile',None)
    binding=None
    if readiness_profile:
        from agentloop.readiness import sha,validate_binding
        if sha(args.readiness_binding_file)!=args.readiness_binding_sha256:
            raise ValueError('coordinator readiness binding file changed')
        binding=validate_binding(read_json(args.readiness_binding_file))
        # Admission is bound to the authoritative external DeepTutor source and
        # the coordinator registry.  Verify it under the shared lock before
        # creating any broker or provider process.
        control_root = os.environ.get('READINESS_CONTROL_ROOT', '@@AGENTSWE_EDITING_CONTROL@@')
        binding_mod = Path(control_root) / 'readiness_binding.py'
        if not binding_mod.is_file():
            raise RuntimeError('readiness binding verifier is missing')
        if binding_mod.is_file():
            import importlib.util
            # readiness_binding imports its siblings by bare name.
            if str(control_root) not in sys.path:
                sys.path.insert(0, str(control_root))
            spec = importlib.util.spec_from_file_location('readiness_binding', binding_mod)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            measured = module.verify_binding(ROOT.resolve(), binding, control_root=control_root)
            if measured != binding:
                raise ValueError('coordinator readiness binding changed during dispatch')
    PriorProductGuard.from_environment(required=True)
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError(f"pilot run directory must not already exist: {run_dir}")
    run_dir.mkdir(parents=True)
    source = args.source_repository.resolve()
    credential = args.credential_file.resolve()
    for path in (args.harbor.resolve(), credential, source, args.runtime_python.resolve()):
        if not path.exists():
            raise FileNotFoundError(path)
    public_cases = ('dev_001',) if readiness_profile else DEV_CASES
    hidden_cases = ("test_001",)
    builder_broker: BrokerProcess | None = None
    public_broker: BrokerProcess | None = None
    hidden_broker: BrokerProcess | None = None
    result_judge_broker: BrokerProcess | None = None
    lifecycle: BuilderLifecycle | None = None
    try:
        public_broker = BrokerProcess(
            run_dir=run_dir, role="public", credential=credential,
            python=args.broker_python, max_calls=12, max_tokens=240_000, defer_removal=bool(readiness_profile),
        )
        if not readiness_profile:
            result_judge_broker = BrokerProcess(
                run_dir=run_dir, role="result_judge", credential=credential,
                python=args.broker_python, max_calls=20, max_tokens=1_000_000, defer_removal=bool(readiness_profile),
                broker_kind="responses_xhigh",
            )
        public = run_dir / "builder_public_package"
        stage_builder_package(public, source, public_cases)
        workspace = run_dir / "builder_workspace" / "submission"
        workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(
            run_dir=run_dir, workspace=workspace, source=source,
            public_broker=public_broker.endpoint_local,
            runtime_python=str(args.runtime_python.absolute()),
            dev_cases=public_cases, max_dev_rounds=2, pilot_not_formal=True,
            result_judge_endpoint=result_judge_broker.endpoint_local if result_judge_broker else None,
            readiness_profile=readiness_profile,current_binding=binding,
        )
        lifecycle.start()
        provider_config = run_dir / "builder_broker_provider.toml"
        direct_builder.write_provider(provider_config, args.builder_base_url)
        if readiness_profile:
            control_root = os.environ.get('READINESS_CONTROL_ROOT', '@@AGENTSWE_EDITING_CONTROL@@')
            cfg = Path(control_root) / 'readiness_binding.py'
            if cfg.is_file():
                import importlib.util
                spec = importlib.util.spec_from_file_location('readiness_binding', cfg)
                mod = importlib.util.module_from_spec(spec); assert spec.loader is not None; spec.loader.exec_module(mod)
                retry_receipt = mod.configure_native_no_replay(provider_config)
                write_json(run_dir / 'builder_provider_retry_policy.json', retry_receipt)
        config = write_builder_task(
            run_dir=run_dir, public=public, workspace=workspace,
            controller=lifecycle, provider_config=provider_config,
        )
        write_json(run_dir / "protocol_lock.json", {**pilot_lock(),'readiness_profile':readiness_profile,
            'public_cases':list(public_cases),'current_binding':binding,'score_threshold':None})
        if readiness_profile:
            write_json(run_dir/'readiness_current_binding.json',binding)
        started = utc_now()
        builder_exit = run_harbor(args.harbor.resolve(), config, run_dir, args.builder_timeout, lifecycle=lifecycle, credential=credential, proxy=args.builder_proxy, provider_url=args.builder_base_url)
        finished = utc_now()
        records = lifecycle.controller.records
        native = lifecycle.native_attestation(builder_exit)
        if records and native['valid']:
            lifecycle.controller.freeze(builder_exit_evidence=native)
        attestation = {
            "schema_version": "agentswe-deeptutor-pilot-builder-attestation/v1",
            "pilot_not_formal": True, "builder_model": BUILDER_MODEL,
            "builder_reasoning_effort": BUILDER_EFFORT,
            "single_continuous_session": True, "single_harbor_invocation": True,
            "builder_session_id": lifecycle.session_id,
            "builder_connection_id": lifecycle.connection_id,
            "max_dev_rounds": lifecycle.max_dev_rounds,
            "builder_exit_code": builder_exit, "started_at": started,
            "finished_at": finished, "candidate_records": records,
            "candidate_1_public_complete": len(records) >= 1 and pilot_public_complete(records[0],bool(readiness_profile)),
            "candidate_2_public_complete": len(records) == 2 and pilot_public_complete(records[1],bool(readiness_profile)),
            "feedback_consumed": len(records) == 2 and bool(records[1].get("feedback_digest_consumed")),
            "distinct_candidate_digests": len(records) == 2 and records[0].get("candidate_digest") != records[1].get("candidate_digest"),
            "freeze": lifecycle.controller.frozen is not None,
        }
        attestation["complete"] = all((
            builder_exit == 0, native['valid'], len(records) == 2,
            attestation["candidate_1_public_complete"],
            attestation["candidate_2_public_complete"],
            attestation["feedback_consumed"], attestation["distinct_candidate_digests"],
            attestation["freeze"],
        ))
        write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {
                "schema_version": "agentswe-deeptutor-pilot-summary/v1",
                "status": "pilot_pipeline_incomplete", "pilot_not_formal": True,
                "formal_result_claimed": False, "code_score_claimed": False,
                "result_axis": "N/A", "code_axis": "N/A",
                "builder_session_attestation": "builder_session_attestation.json",
            })
            return 2
        hidden_broker = BrokerProcess(
            run_dir=run_dir, role="hidden", credential=credential,
            python=args.broker_python, max_calls=12, max_tokens=240_000, defer_removal=bool(readiness_profile),
        )
        hidden_before = read_broker_stats(hidden_broker.endpoint_local)
        if any(int(hidden_before.get(key, 0) or 0) != 0 for key in ("calls", "successful_calls", "failures", "provider_failures")):
            raise RuntimeError("pilot hidden broker was not fresh at zero calls")
        hidden = run_hidden(
            run_dir / "lifecycle" / "freeze_manifest.json",
            run_dir / "hidden",
            broker_endpoint=hidden_broker.endpoint_local,
            python_executable=str(args.runtime_python.absolute()),
            case_ids=hidden_cases, pilot_not_formal=True,
        )
        if readiness_profile:
            from evaluator.readiness_smoke import run as judge_smoke
            result_judge_broker=BrokerProcess(run_dir=run_dir,role='result_judge',credential=credential,
                python=args.broker_python,max_calls=1,max_tokens=1_000_000,broker_kind='responses_xhigh',defer_removal=True)
            aggregation=judge_smoke(task_root=ROOT,run_dir=run_dir,hidden=hidden,credential=credential,result_endpoint=result_judge_broker.endpoint_local)
            complete=aggregation['readiness_judges_complete']
            write_json(run_dir/'summary.json',{'schema_version':'deeptutor-readiness-summary/v1',
                'readiness_profile':readiness_profile,'status':'readiness_evidence_complete' if complete else 'readiness_evidence_incomplete',
                'formal_result_claimed':False,'code_score_claimed':False,'formal_complete':False,'public_cases':['dev_001'],
                'hidden_cases':['test_001'],'readiness_judge_smoke':'readiness_judge_smoke.json',
                'requires_coordinator_terminal_cleanup_and_admission':True})
            return 0 if complete else 2
        score_command = [
            sys.executable, str(ROOT / "evaluator/formal_finalize.py"),
            "--run-dir", str(run_dir), "--credential-file", str(credential),
            "--result-judge-broker-endpoint", result_judge_broker.endpoint_local,
            "--acceptance-cases", *hidden_cases,
        ]
        scored = subprocess.run(score_command, text=True, capture_output=True, check=False)
        score_output = run_dir / "acceptance_aggregation.json"
        aggregation = read_json(score_output) if score_output.is_file() else {}
        complete = bool(scored.returncode == 0 and aggregation.get("acceptance_complete"))
        write_json(run_dir / "pilot_summary.json", {
            "schema_version": "agentswe-deeptutor-pilot-summary/v2",
            "status": "pilot_pipeline_complete" if complete else "pilot_pipeline_incomplete",
            "pilot_not_formal": True, "public_cases": list(public_cases),
            "max_dev_rounds": lifecycle.max_dev_rounds,
            "hidden_cases": list(hidden_cases), "hidden_broker_initial": hidden_before,
            "builder_session_attestation": "builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "hidden/hidden-after-freeze-attestation.json",
            "formal_aggregation": str(score_output),
            "acceptance_complete": complete,
            "formal_result_claimed": False,
            "code_score_claimed": bool(aggregation.get("code_score_publishable")),
            "result_axis": aggregation.get("result_axis", "N/A"),
            "code_axis": aggregation.get("code_axis", "N/A"),
            "scorer_exit_code": scored.returncode,
            "scorer_stderr_tail": scored.stderr[-2000:],
            "heuristic_fallback_used": False,
        })
        write_json(run_dir / "summary.json", read_json(run_dir / "pilot_summary.json"))
        return 0 if complete else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {
            "schema_version": "agentswe-deeptutor-pilot-summary/v1",
            "status": "pilot_pipeline_infrastructure_failure",
            "pilot_not_formal": True, "classification": "evaluator_infrastructure_error",
            "error": f"{type(exc).__name__}: {exc}",
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        })
        return 2
    finally:
        controller_close_error = None
        if lifecycle is not None:
            try:
                lifecycle.close()
            except Exception as exc:
                controller_close_error = f"{type(exc).__name__}: {exc}"
        cleanup_results = merge_startup_cleanup_receipts(
            run_dir, stop_brokers((result_judge_broker, hidden_broker, public_broker, builder_broker))
        )
        if readiness_profile:
            from harbor.readiness_resources import retained_manifest
            retained_manifest(run_dir, (result_judge_broker, hidden_broker, public_broker, builder_broker))
        write_json(run_dir / "cleanup_attestation.json", {
            "schema_version": "agentswe-deeptutor-pilot-cleanup/v2",
            "pilot_not_formal": True,
            "controller_close_error": controller_close_error,
            "brokers_stopped": all(item["absent_after_cleanup"] for item in cleanup_results),
            "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
            "completed": controller_close_error is None and all(item["absent_after_cleanup"] for item in cleanup_results),
            "unrelated_containers_touched": False,
            "containers": cleanup_results,
        })


def pilot_dry_run(run_dir: Path) -> int:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"pilot dry-run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        **pilot_lock(),
        "schema_version": "agentswe-deeptutor-pilot-dry-run/v1",
        "status": "pilot_dry_run_ready",
        "network_calls": 0, "docker_started": False, "harbor_started": False,
        "provider_calls": 0, "pilot_execution_started": False,
        "broker_topology": {
            "builder": "independent-xhigh",
            "public_lower": "independent-medium",
            "hidden_lower": "new-independent-medium-after-freeze",
            "hidden_initial_calls_required": 0,
        },
        "provider_config_path": "run-local absolute host path mounted to identical container path",
    }
    write_json(run_dir / "pilot_protocol_lock.json", pilot_lock())
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--run-formal",
        action="store_true",
        help="explicitly start evaluator brokers, Harbor, provider calls, and hidden execution",
    )
    parser.add_argument(
        "--pilot", action="store_true",
        help="run the real non-formal dev_001 feedback loop and test_001 after freeze",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="with --pilot, write a provider-free wiring receipt and exit")
    parser.add_argument('--readiness-profile',choices=['single-dev-two-round-hidden-smoke-v1'])
    parser.add_argument('--readiness-binding-file',type=Path)
    parser.add_argument('--readiness-binding-sha256')
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run provider-free orchestration contract checks and exit",
    )
    parser.add_argument("--harbor", type=Path, default=DEFAULT_HARBOR)
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL)
    parser.add_argument("--source-repository", type=Path, default=AUTHORITATIVE_SOURCE)
    parser.add_argument("--runtime-python", type=Path, default=DEFAULT_RUNTIME_PYTHON)
    parser.add_argument("--broker-python", default=sys.executable)
    parser.add_argument("--builder-timeout", type=int, default=28920)
    parser.add_argument('--builder-base-url', default='https://api.deepseek.com/v1')
    parser.add_argument('--builder-proxy', default='http://127.0.0.1:7890')
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--result-judge-broker-endpoint", help=argparse.SUPPRESS)
    parser.add_argument("--code-contract", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.readiness_profile:
        if not args.pilot or args.run_formal:
            parser.error('--readiness-profile requires --pilot only')
        if not args.dry_run and (not args.readiness_binding_file or not args.readiness_binding_sha256):
            parser.error('readiness requires coordinator binding file and SHA')
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent is fixed at 1")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    try:
        _timeout_contract = builder_timeout_contract(args.builder_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not _timeout_contract["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({_timeout_contract['required_outer_timeout_seconds']} seconds required)")
    if args.run_formal and args.result_judge_broker_endpoint:
        parser.error("formal Result-judge broker is owned internally; do not pass an external endpoint")
    if args.run_formal:
        try:
            import importlib.util
            admission_path = Path("@@AGENTSWE_EDITING_CONTROL@@/readiness_admission.py")
            if str(admission_path.parent) not in sys.path:
                sys.path.insert(0, str(admission_path.parent))
            admission_spec = importlib.util.spec_from_file_location("agentswe_formal_readiness", admission_path)
            if admission_spec is None or admission_spec.loader is None:
                raise RuntimeError("evaluator readiness admission module is unavailable")
            admission_module = importlib.util.module_from_spec(admission_spec)
            admission_spec.loader.exec_module(admission_module)
            admission_module.require_formal_readiness(ROOT)
        except Exception as exc:
            parser.error(f"formal readiness refused: {exc}")
    effective_max_dev_rounds = 2 if args.pilot else args.max_dev_rounds
    install_summary_writer(
        args.run_dir,
        max_dev_rounds=effective_max_dev_rounds,
        n_concurrent=args.n_concurrent,
        mode="formal" if args.run_formal else "pilot" if args.pilot else "static",
    )

    if args.dry_run and not args.pilot:
        parser.error("--dry-run requires --pilot")
    if sum(bool(value) for value in (args.self_test, args.run_formal, args.pilot)) > 1:
        parser.error("--self-test, --run-formal, and --pilot are mutually exclusive")
    if args.self_test:
        result = self_test()
        print(
            "SELF_TEST=" + result["status"]
            + " network_calls=0 docker_started=false formal_execution_started=false"
        )
        return 0 if result["status"] == "PASS" else 1
    if args.run_dir is None:
        parser.error("--run-dir is required unless --self-test is used")

    if args.pilot:
        if args.dry_run:
            return pilot_dry_run(args.run_dir.resolve())
        return run_pilot(args)

    run_dir = args.run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory must be new and empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    if not args.run_formal and not args.pilot:
        summary = static_audit(run_dir)
        print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if summary["status"] == "static_audit_pass" else 1

    # Nothing above this explicit gate starts a broker, Harbor, Docker, or a
    # network request. Formal prerequisites are intentionally checked only in
    # formal mode so the default audit remains provider-free.
    PriorProductGuard.from_environment(required=True)
    for path in (args.harbor, args.credential_file, args.source_repository, args.runtime_python):
        if not path.exists():
            raise FileNotFoundError(path)
    pilot = False
    readiness_profile = None
    public_cases = DEV_CASES
    hidden_cases = HIDDEN_CASES
    write_json(run_dir / "protocol_lock.json", protocol_lock())

    builder_broker: BrokerProcess | None = None
    public_broker: BrokerProcess | None = None
    hidden_broker: BrokerProcess | None = None
    result_judge_broker: BrokerProcess | None = None
    controller: BuilderLifecycle | None = None
    try:
        public_broker = BrokerProcess(
            run_dir=run_dir, role="public", credential=args.credential_file,
            python=args.broker_python, max_calls=12, max_tokens=240_000, defer_removal=bool(readiness_profile),
        )
        result_judge_broker = BrokerProcess(
            run_dir=run_dir, role="result_judge", credential=args.credential_file,
            python=args.broker_python, max_calls=40, max_tokens=3_000_000, defer_removal=bool(readiness_profile),
            broker_kind="responses_xhigh",
        )
        public = run_dir / "builder_public_package"
        stage_builder_package(public, args.source_repository.resolve(), public_cases)
        workspace = run_dir / "builder_workspace" / "submission"
        workspace.mkdir(parents=True)
        controller = BuilderLifecycle(
            run_dir=run_dir,
            workspace=workspace,
            source=args.source_repository.resolve(),
            public_broker=public_broker.endpoint_local,
            runtime_python=str(args.runtime_python.absolute()),
            dev_cases=public_cases,
            max_dev_rounds=args.max_dev_rounds,
            pilot_not_formal=pilot,
            result_judge_endpoint=result_judge_broker.endpoint_local,
        )
        controller.start()
        provider_config = run_dir / "builder_broker_provider.toml"
        direct_builder.write_provider(provider_config, args.builder_base_url)
        if readiness_profile:
            control_root = os.environ.get('READINESS_CONTROL_ROOT', '@@AGENTSWE_EDITING_CONTROL@@')
            cfg = Path(control_root) / 'readiness_binding.py'
            if cfg.is_file():
                import importlib.util
                spec = importlib.util.spec_from_file_location('readiness_binding', cfg)
                mod = importlib.util.module_from_spec(spec); assert spec.loader is not None; spec.loader.exec_module(mod)
                retry_receipt = mod.configure_native_no_replay(provider_config)
                write_json(run_dir / 'builder_provider_retry_policy.json', retry_receipt)
        config = write_builder_task(
            run_dir=run_dir, public=public, workspace=workspace,
            controller=controller, provider_config=provider_config,
        )
        builder_started_at = utc_now()
        builder_exit = run_harbor(args.harbor.resolve(), config, run_dir, args.builder_timeout, lifecycle=controller, credential=args.credential_file, proxy=args.builder_proxy, provider_url=args.builder_base_url)
        builder_finished_at = utc_now()
        native = controller.native_attestation(builder_exit)
        if controller.controller.records and native['valid']:
            controller.controller.freeze()
        lifecycle_ready = (
            native['valid']
            and 1 <= len(controller.controller.records) <= args.max_dev_rounds
            and controller.controller.frozen is not None
            and all(
                set(record.get("dev_results", {})) == set(public_cases)
                and all(item.get("terminal") is True for item in record["dev_results"].values())
                for record in controller.controller.records
            )
        )
        write_json(run_dir / "builder_process.json", {
            "exit_code": builder_exit,
            "started_at": builder_started_at,
            "finished_at": builder_finished_at,
            "timed_out": builder_exit == 124,
            "session_id": controller.session_id,
            "connection_id": controller.connection_id,
            "same_process_session": True,
            "lifecycle_ready_for_hidden": lifecycle_ready,
        })
        hidden: dict[str, Any]
        if lifecycle_ready:
            # Hidden receives a newly started zero-call broker, independent of
            # both Builder and public-dev broker state.
            hidden_broker = BrokerProcess(
                run_dir=run_dir, role="hidden", credential=args.credential_file,
                python=args.broker_python, max_calls=12, max_tokens=240_000,
            )
            hidden = run_hidden(
                run_dir / "lifecycle" / "freeze_manifest.json",
                run_dir / "hidden",
                broker_endpoint=hidden_broker.endpoint_local,
                python_executable=str(args.runtime_python.absolute()),
                case_ids=hidden_cases,
                pilot_not_formal=pilot,
            )
        else:
            hidden = {
                "status": "not_started",
                "reason": "same-session accepted-submission public lifecycle did not pass the freeze gate",
                "formal_result_eligible": False,
            }
        summary = {
            "schema_version": "agentswe-deeptutor-one-stop-summary/v1",
            "status": ("pilot_pipeline_complete" if hidden.get("all_cases_real") else "pilot_pipeline_incomplete") if pilot else ("formal_evidence_complete" if hidden.get("formal_result_eligible") else "formal_evidence_incomplete"),
            "pilot_not_formal": pilot,
            "builder_exit_code": builder_exit,
            "timeout_contract": builder_timeout_contract(args.builder_timeout),
            "builder_session_id": controller.session_id,
            "candidate_rounds_consumed": len(controller.controller.records),
            "candidate_digests_distinct": len({item.get("candidate_digest") for item in controller.controller.records}) == len(controller.controller.records),
            "freeze_manifest": str(run_dir / "lifecycle" / "freeze_manifest.json") if controller.controller.frozen else None,
            "hidden_attestation": str(run_dir / "hidden" / "hidden-after-freeze-attestation.json") if (run_dir / "hidden" / "hidden-after-freeze-attestation.json").is_file() else None,
            "failure_attribution": ["runtime_dependency", "provider", "broker", "launcher", "candidate_delivery", "candidate_build", "candidate_capability", "candidate_behavior"],
            "formal_result_claimed": False,
            "code_score_claimed": False,
        }
        finalizer_exit = None
        finalizer_result = None
        # The finalizer independently classifies Candidate-zero vs N/A and
        # evaluates frozen Code; a failed lower run must not suppress Code.
        if not pilot and lifecycle_ready:
            command = [
                sys.executable, str(ROOT / "evaluator" / "formal_finalize.py"),
                "--run-dir", str(run_dir),
                "--credential-file", str(args.credential_file.resolve()),
                "--result-judge-broker-endpoint", result_judge_broker.endpoint_local,
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            finalizer_exit = completed.returncode
            if (run_dir / "formal_aggregation.json").is_file():
                finalizer_result = read_json(run_dir / "formal_aggregation.json")
        summary.update({
            "formal_finalizer_exit": finalizer_exit,
            "formal_aggregation": "formal_aggregation.json" if finalizer_result else None,
            "formal_result_claimed": bool(finalizer_result and finalizer_result.get("formal_result_publishable")),
            "code_score_claimed": bool(finalizer_result and finalizer_result.get("code_score_publishable")),
            "result_axis": finalizer_result.get("result_axis") if finalizer_result else "N/A",
            "code_axis": finalizer_result.get("code_axis") if finalizer_result else "N/A",
            "combined_score": None,
            "result_judge_broker_owned_by_one_stop": result_judge_broker is not None,
            "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent,
        })
        write_json(run_dir / "summary.json", summary)
        write_json(run_dir / "one_stop_summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
        if pilot:
            return 0 if summary["status"] == "pilot_pipeline_complete" else 2
        return 0 if summary["status"] == "formal_evidence_complete" and finalizer_exit == 0 else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {
            "schema_version": "agentswe-deeptutor-one-stop-summary/v1",
            "status": "orchestration_infrastructure_failure",
            "classification": "evaluator_infrastructure_error",
            "error": f"{type(exc).__name__}: {exc}",
            "formal_result_claimed": False,
            "code_score_claimed": False,
        })
        raise
    finally:
        controller_close_error = None
        if controller is not None:
            try:
                controller.close()
            except Exception as exc:
                controller_close_error = f"{type(exc).__name__}: {exc}"
        cleanup_results = merge_startup_cleanup_receipts(
            run_dir,
            stop_brokers((result_judge_broker, hidden_broker, public_broker, builder_broker)),
        )
        write_json(run_dir / "cleanup_attestation.json", {
            "completed_at": utc_now(),
            "builder_controller_closed": controller_close_error is None and (controller is None or controller.server is None),
            "controller_close_error": controller_close_error,
            "brokers_stopped": all(item["absent_after_cleanup"] for item in cleanup_results),
            "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
            "completed": controller_close_error is None and all(item["absent_after_cleanup"] for item in cleanup_results),
            "unrelated_containers_touched": False,
            "containers": cleanup_results,
        })


if __name__ == "__main__":
    raise SystemExit(main())
