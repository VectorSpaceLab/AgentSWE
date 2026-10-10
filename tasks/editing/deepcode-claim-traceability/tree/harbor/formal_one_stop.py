#!/usr/bin/env python3
"""DeepCode one-stop Agent-loop formal orchestration.

The default invocation performs only a static configuration audit. The
expensive path is opt-in with --run-formal and enforces this lifecycle:

    one deepseek-flash/xhigh Builder session
    -> 1..10 distinct accepted Candidates, each running dev_001 and dev_002
    -> exact evaluator-feedback digest consumption before every later submission
    -> immutable latest-accepted Candidate freeze on Builder exit or round limit
    -> fresh independent deepseek-flash/medium hidden broker
    -> test_001 through test_006 -> attestation and broker stats

The lower agent is always the Candidate-modified DeepCode product. This
orchestrator never substitutes an external general-purpose coding agent.
Result and Code scoring remain independent downstream axes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

JUDGE_BROKER_SCRIPT = Path("@@AGENTSWE_EDITING_CONTROL@@/judge_broker_xhigh.py")


def _judge_runtime():
    """Evaluator-only runtime; never used for Builder or lower roles."""
    import sys
    if str(JUDGE_BROKER_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(JUDGE_BROKER_SCRIPT.parent))
    import judge_broker_runtime
    return judge_broker_runtime

from typing import Any

from one_stop_contract import install_summary_writer

ROOT = Path(__file__).resolve().parents[1]
RESULT_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harbor.native_builder_evidence import observe_thread, verify_native
from harbor.native_builder_runner import run_native_builder
from harbor import direct_harbor_builder as direct_builder
from evaluator.harness.public_feedback import clean, public_record
from evaluator.harness.controller import PublicEvaluationRejected
from evaluator.harness.public_semantic_feedback import rejection_feedback_payload

HARNESS = ROOT / "evaluator" / "harness"
MODEL = "deepseek-flash"
BUILDER_MODEL = "deepseek-flash"  # upper Builder (Codex harness) only
BUILDER_EFFORT = "max"
LOWER_EFFORT = "high"
BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
BROKER_IMAGE = BUILDER_IMAGE
# 5 h Builder cap (D2, 2026-09-19).  The shared driver
# @@AGENTSWE_EDITING_CONTROL@@/formal_commands.py passes
# --builder-timeout 18000, so the generated Harbor task.toml must stop the
# native Builder BEFORE the outer wait SIGTERMs it; otherwise run_harbor
# reports 124 and both exit_code == 0 gates below void the run.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
# The pilot/readiness branch keeps its historical 8 h generated cap: readiness
# passes --builder-timeout 28800 explicitly and its two-round canary approaches
# neither bound, so readiness behaviour is unchanged.
PILOT_BUILDER_TASK_TIMEOUT_SECONDS = 28_800
# run_native_builder turns an outer-deadline SIGTERM into 124 and a
# KeyboardInterrupt into 130.  Only 124 is a survivable interrupt; 130 and 125
# stay fatal.
BUILDER_INTERRUPT_EXIT_CODES = (124,)


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


def formal_max_rounds_interrupt(lifecycle: "SocketLifecycle", exit_code: Any) -> bool:
    """True only for a formal Builder killed after spending every round.

    deepcode never freezes mid-run, so there is no freeze_reason to read: the
    controller refuses submission max_dev_rounds+1 (evaluator/harness/
    controller.py:130) and the freeze happens after Builder exit.  A Builder
    that has consumed the whole accepted-round budget and is then killed by the
    outer deadline has produced complete evidence; anything else has not.
    Readiness/pilot are excluded so the readiness freeze keeps demanding
    returncode == 0.
    """
    return bool(
        exit_code in BUILDER_INTERRUPT_EXIT_CODES
        and getattr(lifecycle, "evidence_kind", None) == "formal"
        and getattr(lifecycle, "readiness_profile", None) is None
        and lifecycle.max_dev_rounds
        and len(lifecycle.controller.records) == lifecycle.max_dev_rounds
    )
DEFAULT_BUILDER_BROKER_SCRIPT = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)
DEEPCODE_BROKER_SCRIPT = HARNESS / "broker_server.py"
RUNTIME_PREPARER = HARNESS / "prepare_task_environment.py"
DEFAULT_PYTHON312 = Path(
    "@@AGENTSWE_PYTHON312_PREFIX@@/bin/python3.12"
)
CONTROLLER_SOCKET_IN_CONTAINER = "/run/agentswe-deepcode/controller.sock"
PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
PUBLIC_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{index:03d}" for index in range(1, 7))
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"
# The readiness profile exposes one dev case, so the eligibility gate has to
# expect that set rather than the formal one. Everything else is unchanged.
READINESS_PUBLIC_CASES = ("dev_001",)
INFRA_CLASSIFICATIONS = {
    "provider_failure",
    "credential_infrastructure_error",
    "protocol_infrastructure_error",
    "broker_infrastructure_error",
    "launcher_infrastructure_error",
    "evaluator_infrastructure_error",
}


def _runtime_has_product_dependencies(python_executable: Path) -> bool:
    """Check the minimum imports needed before starting a lower product run."""
    probe = subprocess.run(
        [
            str(python_executable),
            "-c",
            "import aiohttp,httpx,loguru,openai,pydantic_settings,yaml,rich",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return probe.returncode == 0


def ensure_runtime_python(run_dir: Path, requested: str | None) -> str:
    """Return a dependency-complete Python runtime for the Candidate product.

    The formal command templates intentionally do not expose a host-specific
    virtualenv path.  If the caller supplies one, it is authoritative; when it
    is omitted, reuse a dependency-complete interpreter if available and
    otherwise prepare the sibling's evaluator-owned Python 3.12 environment
    under the current run directory.  The prepared environment is run-local,
    so it cannot affect unrelated smoke or formal runs.
    """
    if requested:
        candidate = Path(requested).expanduser().absolute()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError(f"requested DeepCode runtime Python is not executable: {candidate}")
        if not _runtime_has_product_dependencies(candidate):
            raise RuntimeError(
                "requested DeepCode runtime is missing required product dependencies; "
                "use the sibling prepared runtime or omit --python"
            )
        return str(candidate)

    inherited = os.environ.get("DEEPCODE_PYTHON")
    if inherited:
        return ensure_runtime_python(run_dir, inherited)

    for candidate in (Path(sys.executable).absolute(), DEFAULT_PYTHON312):
        if candidate.is_file() and os.access(candidate, os.X_OK) and _runtime_has_product_dependencies(candidate):
            return str(candidate)

    if not RUNTIME_PREPARER.is_file():
        raise RuntimeError(f"DeepCode runtime preparer is missing: {RUNTIME_PREPARER}")
    environment_root = run_dir / "deepcode_runtime"
    command = [
        sys.executable,
        str(RUNTIME_PREPARER),
        "--environment-root",
        str(environment_root),
        "--python312",
        str(DEFAULT_PYTHON312),
    ]
    preparation = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    write_json(
        run_dir / "deepcode_runtime_preparation.json",
        {
            "schema_version": "deepcode-runtime-preparation-v1",
            "command": command,
            "exit_code": preparation.returncode,
            "stdout": preparation.stdout[-12000:],
            "stderr": preparation.stderr[-12000:],
            "environment_root": str(environment_root),
        },
    )
    runtime = environment_root / "venv" / "bin" / "python"
    if preparation.returncode != 0 or not runtime.is_file() or not _runtime_has_product_dependencies(runtime):
        raise RuntimeError(
            "DeepCode runtime preparation failed; see "
            f"{run_dir / 'deepcode_runtime_preparation.json'}"
        )
    return str(runtime)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_json_bytes(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object response")
    return value


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def broker_stats(endpoint: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(endpoint)
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/stats", "", ""))
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {STATS_TOKEN}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return read_json_bytes(response.read())


def broker_stats_or_error(endpoint: str) -> dict[str, Any]:
    try:
        return broker_stats(endpoint)
    except Exception as exc:
        return {
            "status": "unavailable",
            "classification": "broker_infrastructure_error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def broker_health(endpoint: str) -> dict[str, Any]:
    """Read the common health contract exposed by both evaluator brokers."""
    parsed = urllib.parse.urlsplit(endpoint)
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/healthz", "", ""))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as response:
        value = read_json_bytes(response.read())
    if response.status != 200:
        raise RuntimeError(f"broker health returned HTTP {response.status}")
    return value


def broker_health_or_error(endpoint: str) -> dict[str, Any]:
    try:
        return broker_health(endpoint)
    except Exception as exc:
        return {
            "status": "unavailable",
            "classification": "broker_infrastructure_error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _broker_protocol_matches(
    health: dict[str, Any],
    stats: dict[str, Any],
    *,
    effort: str,
    broker_kind: str,
) -> bool:
    """Validate the health/stats variants without conflating broker types."""
    # Both evaluator broker types must prove their locked model and effort at
    # startup; a generic status=ok endpoint is not sufficient evidence.
    if broker_kind == "deepcode":
        if health.get("model") != MODEL or health.get("reasoning_effort") != effort:
            return False
    elif (
        health.get("status") != "ok"
        or health.get("model") != MODEL
        or health.get("reasoning_effort") != effort
    ):
        return False
    runtime = stats.get("runtime", {})
    if (
        not isinstance(runtime, dict)
        or int(runtime.get("calls", -1)) != 0
        or int(runtime.get("failures", -1)) != 0
    ):
        return False
    if broker_kind == "deepcode":
        protocol = stats.get("protocol", {})
        if not isinstance(protocol, dict):
            return False
        if protocol.get("model") != MODEL or protocol.get("reasoning_effort") != effort:
            return False
    return True


def _responses_broker_upstream(upstream: str) -> str:
    """Convert the DeepCode base-URL option to the shared broker's full path."""
    parsed = urllib.parse.urlsplit(upstream)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/responses"):
        normalized_path = path
    elif path.endswith("/v1"):
        normalized_path = path + "/responses"
    else:
        normalized_path = path + "/v1/responses" if path else "/v1/responses"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, normalized_path, parsed.query, parsed.fragment)
    )


def start_broker(
    *,
    name: str,
    port: int,
    effort: str,
    credential: Path,
    image: str,
    upstream: str,
    script: Path,
    broker_kind: str,
    owned_containers: dict[str, str],
    evidence_dir: Path | None = None,
    proxy_url: str = "",  # direct egress; evaluator_proxy_url validates the shape
    defer_removal: bool = False,
) -> str:
    """Start an evaluator-owned broker and return its host endpoint.

    Builder xhigh uses the independently validated shared Responses broker;
    DeepCode public/hidden lower runs use the sibling's medium broker.
    """
    if broker_kind == 'responses_xhigh':
        if effort != BUILDER_EFFORT or evidence_dir is None:
            raise ValueError('Builder requires xhigh and run-owned evidence directory')
        shared='@@AGENTSWE_EDITING_CONTROL@@'
        if shared not in sys.path: sys.path.insert(0,shared)
        from builder_broker_runtime import start_builder_broker
        cidfile=evidence_dir / (name + '.builder.cid')
        start_builder_broker(name=name,credential=credential,image=image,port=port,cidfile=cidfile)
        owned_containers[name]=cidfile.read_text().strip()
        return f'http://127.0.0.1:{port}/v1/responses'
    if broker_kind == "judge_xhigh":
        if effort != BUILDER_EFFORT or script.resolve() != JUDGE_BROKER_SCRIPT or evidence_dir is None:
            raise ValueError("Result requires explicit judge-only script, xhigh, and owned evidence directory")
        instance = _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=evidence_dir / "result_judge.cid", upstream=upstream,
            defer_removal=defer_removal)
        owned_containers[name] = instance.container_id
        return instance.endpoint
    if broker_kind not in {"deepcode", "responses_xhigh"}:
        raise ValueError(f"unsupported broker kind: {broker_kind}")
    shared_runtime = "@@AGENTSWE_EDITING_CONTROL@@"
    if shared_runtime not in sys.path:
        sys.path.insert(0, shared_runtime)
    from responses_stream import evaluator_proxy_url
    proxy_url = evaluator_proxy_url(proxy_url)
    # Direct egress is permitted: evaluator_proxy_url above already refuses
    # anything that is not a loopback proxy, and an empty value means no
    # proxy at all rather than an unchecked one.
    script = script.resolve()
    if not script.is_file():
        raise FileNotFoundError(f"broker script missing: {script}")
    if broker_kind == "responses_xhigh" and effort != BUILDER_EFFORT:
        raise ValueError("shared Responses broker is reserved for Builder xhigh")
    if broker_kind == "deepcode" and effort != LOWER_EFFORT:
        raise ValueError("DeepCode product broker is reserved for the medium lower-agent role")
    if broker_kind == "deepcode" and script != DEEPCODE_BROKER_SCRIPT.resolve():
        raise ValueError("DeepCode broker kind must use the sibling broker_server.py")
    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        "host",
        "-v",
        f"{script}:/broker.py:ro",
        "-v",
        f"{credential.resolve()}:/run/secrets/agentswe.env:ro",
        "-v",
        "/etc/ssl/certs:/etc/ssl/certs:ro",
        "-e",
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        image,
    ]
    if broker_kind == 'deepcode':
        if evidence_dir is None: raise ValueError('DeepCode lower broker requires run-owned durable ledger directory')
        ledger_dir=evidence_dir / ('lower-broker-' + name);ledger_dir.mkdir(parents=True,exist_ok=False)
        position=command.index(image)
        command[position:position]=['-v',f'{script.parent / "request_ledger.py"}:/request_ledger.py:ro',
            '-v','@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py:/responses_stream.py:ro',
            '-v',f'{ledger_dir.resolve()}:/evaluator-ledger:rw',
            '-e', 'AGENTSWE_EVALUATOR_PROXY_URL=' + proxy_url]
    if broker_kind == "responses_xhigh":
        command.extend(
            [
                "python3",
                "/broker.py",
                "--bind",
                "0.0.0.0",
                "--port",
                str(port),
                "--provider-url",
                _responses_broker_upstream(upstream),
                "--credential-file",
                "/run/secrets/agentswe.env",
                "--max-runtime-calls",
                "0",
                "--max-runtime-tokens",
                "0",
            ]
        )
    else:
        command.extend(
            [
                "python3",
                "/broker.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--upstream",
                upstream,
                "--credential-file",
                "/run/secrets/agentswe.env",
                "--reasoning-effort",
                effort,
                "--stats-file",
                "/evaluator-ledger/stats.json",
            ]
        )
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{name}: broker launch failed: {completed.stderr[-1600:]}"
        )
    container_id = completed.stdout.strip()
    if not container_id:
        raise RuntimeError(f"{name}: docker run did not return a container id")
    owned_containers[name] = container_id
    endpoint = f"http://127.0.0.1:{port}/v1/responses"
    last = ""
    for _ in range(60):
        try:
            health = broker_health(endpoint)
            current = broker_stats(endpoint)
            if not _broker_protocol_matches(
                health,
                current,
                effort=effort,
                broker_kind=broker_kind,
            ):
                raise RuntimeError("broker protocol or zero-call gate failed")
            return endpoint
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"{name}: broker health failed: {last}")


def inspect_container(reference: str) -> dict[str, Any]:
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


def remove_and_verify_broker(name: str, container_id: str) -> dict[str, Any]:
    before = inspect_container(container_id)
    in_progress = False
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True,
            capture_output=True,
            check=False,
        )
        remove_exit_code = removed.returncode
        remove_error = (removed.stderr or "").strip() or None
        in_progress = removal_in_progress(removed)
        if in_progress:
            await_daemon_removal(container_id)
    except OSError as exc:
        remove_exit_code = None
        remove_error = f"{type(exc).__name__}: {exc}"
    after = inspect_container(container_id)
    return {
        "name": name,
        "container_id": container_id,
        "owned": True,
        "present_before_cleanup": before.get("present"),
        "inspect_before": before,
        "remove_exit_code": remove_exit_code,
        "remove_error": remove_error,
        **({"removal_in_progress_at_rm": True} if in_progress else {}),
        "inspect_after": after,
        "absent_after_cleanup": after.get("absent") is True,
    }


def public_round_complete(record: dict[str, Any], expected_cases: tuple[str, ...] = PUBLIC_CASES) -> bool:
    dev = record.get("dev")
    if not isinstance(dev, list) or len(dev) != len(expected_cases):
        return False
    if {str(item.get("case_id")) for item in dev if isinstance(item, dict)} != set(expected_cases):
        return False
    # A candidate-behavior failure is still an evaluable public round: the
    # lower product ran, produced its case record, and the broker evidence is
    # authoritative.  Some DeepCode lower launchers expose stdout/stderr
    # through the nested runtime record rather than materializing the optional
    # top-level log paths.  Requiring those optional paths incorrectly blocks
    # the required post-freeze hidden pilot after a valid behavior failure.
    return all(
        isinstance(item, dict)
        and item.get("semantic_score_contract_valid") is True
        and isinstance(item.get("case_id"), str)
        and isinstance(item.get("broker_delta"), dict)
        and int(item["broker_delta"].get("failures", 0) or 0) == 0
        for item in dev
    )


@dataclass
class SocketLifecycle:
    run_dir: Path
    workspace: Path
    public_endpoint: str
    runtime_python: str | None
    public_case_ids: tuple[str, ...] = PUBLIC_CASES
    hidden_case_ids: tuple[str, ...] = HIDDEN_CASES
    evidence_kind: str = "formal"
    max_dev_rounds: int = 10
    result_judge_endpoint: str | None = None
    # The controller already implements the whole readiness freeze contract
    # (two valid rounds, unchanged Builder session, a real product revision,
    # source immutability, delivery digests). It only needed to be told.
    readiness_profile: str | None = None
    current_binding: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        from evaluator.harness.builder_lifecycle import BuilderSession
        from evaluator.harness.controller import CandidateController

        token_source = f"{self.run_dir}:{time.time_ns()}"
        self.token = hashlib.sha256(token_source.encode()).hexdigest()
        self.session_id = f"deepcode-builder-session-{self.token[:16]}"
        self.socket_directory = Path(tempfile.mkdtemp(prefix='deepcode-builder-', dir='/tmp'))
        self.socket_path = self.socket_directory / 'controller.sock'
        self.native_observations = []
        self.feedback_deliveries = []
        self.accepted_deliveries = {}
        self.controller = CandidateController(
            base_repository=ROOT / "input" / "repository",
            run_dir=self.run_dir / "lifecycle",
            launcher=HARNESS / "deepcode_lower_agent.py",
            broker_endpoint=self.public_endpoint,
            runtime_python=self.runtime_python,
            hidden_root=ROOT / "test_cases",
            dry_run=False,
            public_case_ids=self.public_case_ids,
            hidden_case_ids=self.hidden_case_ids,
            evidence_kind=self.evidence_kind,
            max_dev_rounds=self.max_dev_rounds,
            result_judge_endpoint=self.result_judge_endpoint,
            readiness_profile=self.readiness_profile,
            current_binding=self.current_binding,
        )
        self.session = BuilderSession(
            self.controller,
            session_id=self.session_id,
            require_feedback_ack=True,
        )
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.lock = threading.RLock()
        self.validation_count = 0
        self.events: list[dict[str, Any]] = []

    def event(self, name: str, **fields: Any) -> None:
        self.events.append(
            {
                "event": name,
                "at": now(),
                "builder_session_id": self.session_id,
                **fields,
            }
        )

    def validate(self, trigger: str) -> tuple[int, dict[str, Any]]:
        """Validate a delivery without consuming either Candidate round."""
        from evaluator.harness.common import prepare_candidate, run_build_gates
        from evaluator.harness.candidate_adapter import tree_digest

        with self.lock:
            self.validation_count += 1
            number = len(self.controller.records) + 1
            output = (
                self.run_dir
                / "validations"
                / f"candidate_{number:03d}_attempt_{self.validation_count:03d}"
            )
            output.mkdir(parents=True, exist_ok=False)
            candidate_digest = tree_digest(self.workspace)
            prepared = None
            # The harness only expects the four lifecycle keys when it is told
            # this is a readiness run. Without that it demands exactly the seven
            # shared fields, while presubmit demands all eleven, and no delivery
            # can satisfy both. Build the same metadata submit() builds.
            readiness_metadata = None
            if self.readiness_profile:
                prior = self.controller.records[-1] if self.controller.records else None
                readiness_metadata = {
                    'builder_session_id': self.session_id,
                    'submission_number': number,
                    'revision_of_candidate_digest': prior['delivery_candidate_digest'] if prior else None,
                    'feedback_digest': self.session.feedback_digest if prior else None,
                }
            try:
                prepared = prepare_candidate(
                    self.workspace,
                    workspace_parent=output,
                    readiness_metadata=readiness_metadata,
                )
                gates = run_build_gates(
                    prepared.repository,
                    python_executable=self.runtime_python,
                )
                result: dict[str, Any] = {
                    "schema_version": "deepcode-agentloop-builder-preflight-v1",
                    "classification": "ready_for_submission",
                    "ready_for_submission": True,
                    "submission_consumed": False,
                    "candidate_number": number,
                    "candidate_delivery_digest": candidate_digest,
                    "changed_paths": prepared.changed_paths,
                    "build_gates": [gate.to_json() for gate in gates],
                    "trigger": trigger,
                }
            except Exception as exc:
                result = {
                    "schema_version": "deepcode-agentloop-builder-preflight-v1",
                    "classification": "candidate_preflight_failure",
                    "ready_for_submission": False,
                    "submission_consumed": False,
                    "candidate_number": number,
                    "candidate_delivery_digest": candidate_digest,
                    "error": f"{type(exc).__name__}: {exc}",
                    "trigger": trigger,
                }
            finally:
                if prepared is not None:
                    shutil.rmtree(prepared.workspace, ignore_errors=True)
            path = output / "validation_result.json"
            write_json(path, result)
            result["validation_result"] = str(path)
            self.event(
                "candidate_preflight",
                candidate_number=number,
                ready=result["ready_for_submission"],
                submission_consumed=False,
                validation_result=str(path),
            )
            return (200 if result["ready_for_submission"] else 422), result

    def bind_native_thread(self):
        observation = observe_thread(self.run_dir, self.native_observations)
        if self.native_observations and observation['thread_id'] != self.session_id:
            raise RuntimeError('Builder native thread changed')
        if not self.native_observations:
            if self.controller.records: raise RuntimeError('Accepted ledger lacks native thread binding')
            self.session_id = observation['thread_id']
            self.session.session_id = self.session_id
            self.session._write_attestation()
        self.native_observations.append(observation)
        write_json(self.run_dir/'native_thread_observations.json', self.native_observations)

    def accepted_payload(self, record, *, cached=False):
        number = record['source_submission']
        feedback = read_json(self.session.feedback_history_path(number))
        event = next((event for event in self.session.events if event.get('event') == 'candidate_submission_finished'
            and event.get('source_submission') == number), {})
        return {'builder_session_id': self.session_id, 'submission_number': number,
            'candidate_digest': record['candidate_digest'],
            # What the readiness contract calls the candidate is the delivered
            # triple, not the materialized repository; the next round's
            # run_report.json is checked against this one, so it has to be
            # readable here rather than guessed.
            'delivery_candidate_digest': record['delivery_candidate_digest'],
            'feedback_digest': feedback['feedback_digest'],
            'feedback_digest_ack': event.get('feedback_digest_ack'), 'feedback': feedback,
            'idempotent': cached, 'submission_consumed': not cached, 'state': 'feedback_ready'}

    def native_attestation(self, exit_code, *, allow_interrupted: bool = False):
        from evaluator.harness.candidate_adapter import tree_digest
        records, errors = [], []
        for record in self.controller.records:
            number = record['source_submission']
            candidate = self.controller.run_dir/'candidates'/f'candidate_{number:03d}'
            if tree_digest(candidate) != record['candidate_digest']:
                errors.append('accepted product changed after public evaluation')
            payload = self.accepted_payload(record)
            records.append({'builder_session_id': self.session_id, 'candidate_digest': record['candidate_digest'],
                'feedback_path': str(self.session.feedback_history_path(number)),
                'feedback_digest': payload['feedback_digest'], 'feedback_digest_ack': payload['feedback_digest_ack'],
                'build': {'candidate_repo_digest': record['candidate_digest']}})
        proof = verify_native(self.run_dir, records, self.feedback_deliveries, self.native_observations,
                              allow_interrupted=allow_interrupted)
        proof['errors'].extend(errors)
        proof['valid'] = bool(proof['valid'] and not errors and (exit_code == 0 or allow_interrupted))
        proof['builder_exit_code'] = exit_code
        proof['max_dev_rounds_interrupt_accepted'] = bool(allow_interrupted)
        write_json(self.run_dir/'builder_native_attestation.json', proof)
        return proof

    def submit(self, feedback_digest_ack: str | None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.bind_native_thread()
            from evaluator.harness.candidate_adapter import tree_digest
            delivery_digest = tree_digest(self.workspace)
            if delivery_digest in self.accepted_deliveries:
                return 200, self.accepted_payload(self.accepted_deliveries[delivery_digest], cached=True)
            number = len(self.controller.records) + 1
            status, preflight = self.validate('submission_gate')
            if status != 200: return status, preflight
            self.event('candidate_submission_started', candidate_number=number, feedback_digest_ack=feedback_digest_ack)
            try:
                record = self.session.submit(self.workspace, feedback_digest_ack=feedback_digest_ack)
            except Exception as exc:
                self.event('candidate_submission_rejected', candidate_number=number, error=f'{type(exc).__name__}: {exc}')
                payload = {'error': clean(str(exc)), 'submission_consumed': False,
                    'retry_same_candidate': False}
                if type(exc) is PublicEvaluationRejected and exc.public_diagnostics:
                    payload['public_execution_diagnostics'] = [value.public_dict()
                        for value in exc.public_diagnostics]
                if type(exc) is PublicEvaluationRejected:
                    try:
                        feedback = rejection_feedback_payload(exc.candidate_digest, exc.public_case_feedback)
                    except Exception:
                        feedback = None
                    if feedback is not None:
                        feedback['builder_session_id'] = self.session_id
                        payload['public_rejection_feedback'] = feedback
                return 422, payload
            self.event('candidate_submission_idempotent' if self.session.last_submit_idempotent else 'candidate_submission_finished',
                candidate_number=record['source_submission'], requested_candidate_number=number,
                candidate_digest=record['candidate_digest'], public_round_complete=public_round_complete(record, self.public_case_ids),
                submission_consumed=not self.session.last_submit_idempotent)
            self.accepted_deliveries[delivery_digest] = record
            return 200, self.accepted_payload(record, cached=self.session.last_submit_idempotent)

    def consume_feedback(self, feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            feedback = self.session.consume_feedback(feedback_digest)
            self.event(
                "feedback_consumed",
                feedback_digest=self.session.feedback_digest,
            )
            return 200, {
                "builder_session_id": self.session_id,
                "feedback_digest": self.session.feedback_digest,
                "feedback": feedback,
                "consumed": True,
            }

    def status(self) -> tuple[int, dict[str, Any]]:
        return 200, {'builder_session_id': self.session_id, 'accepted_submissions': len(self.controller.records),
            'max_dev_rounds': self.max_dev_rounds, 'frozen': self.controller.frozen is not None,
            **(self.accepted_payload(self.controller.records[-1], cached=True) if self.controller.records else {})}

    def start(self) -> None:
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    request = json.loads(self.rfile.readline(1 << 20))
                    if request.get("token") != owner.token:
                        status, payload = 401, {"error": "unauthorized"}
                    elif request.get("action") == "validate":
                        status, payload = owner.validate("builder_request")
                    elif request.get("action") == "submit":
                        status, payload = owner.submit(
                            request.get("feedback_digest_ack")
                        )
                    elif request.get("action") == "consume_feedback":
                        status, payload = owner.consume_feedback(
                            request.get("feedback_digest")
                        )
                    elif request.get("action") == "status":
                        status, payload = owner.status()
                    else:
                        status, payload = 400, {"error": "unknown_action"}
                except Exception as exc:
                    status, payload = 422, {
                        "error": f"{type(exc).__name__}: {exc}"
                    }
                payload = clean(payload)
                payload.pop('validation_result', None)
                self.wfile.write(
                    json.dumps(
                        {"status": status, "payload": payload},
                        ensure_ascii=False,
                    ).encode()
                    + b"\n"
                )
                self.wfile.flush()
                if status == 200 and payload.get('submission_number') and payload.get('feedback_digest'):
                    with owner.lock:
                        owner.feedback_deliveries.append({'candidate_number': payload['submission_number'],
                            'feedback_digest': payload['feedback_digest'], 'builder_session_id': owner.session_id,
                            'payload_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
                        write_json(owner.run_dir/'native_feedback_deliveries.json', owner.feedback_deliveries)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.socket_path.unlink(missing_ok=True)
        self.server = Server(str(self.socket_path), Handler)
        self.socket_path.chmod(0o600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.socket_path.unlink(missing_ok=True)
        self.socket_directory.rmdir()


def stage_public_package(run_dir: Path, public_case_ids: tuple[str, ...] = PUBLIC_CASES) -> Path:
    from evaluator.harness.public_package import prepare

    destination = prepare(ROOT, run_dir / "builder_public_package")
    for case_dir in (destination / "dev_cases").iterdir():
        if case_dir.is_dir() and case_dir.name not in public_case_ids:
            shutil.rmtree(case_dir)
    return destination


def prepare_builder_worktree(public: Path, destination: Path) -> Path:
    """Create the writable, Git-backed product tree used by the upper Builder.

    The Builder-visible package is intentionally read-only and contains no
    evaluator material.  The Builder still needs a writable product checkout
    at the path named by its task contract so it can inspect, edit, and diff
    the supplied product before writing a Candidate patch.
    """
    source = public / "input" / "repository"
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError(f"Builder-visible product repository is unavailable: {source}")
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "benchmark@invalid.local"],
        ["git", "config", "user.name", "Benchmark"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "pinned baseline"],
    ):
        result = subprocess.run(
            argv,
            cwd=destination,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"could not initialize Builder product worktree with {' '.join(argv)}: "
                f"{result.stderr.strip()}"
            )
    return destination


def write_builder_helper(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import argparse, json, os, socket, sys
p=argparse.ArgumentParser()
p.add_argument("--status", action="store_true")
p.add_argument("--validate", action="store_true")
p.add_argument("--consume-feedback", action="store_true")
p.add_argument("--feedback-digest")
p.add_argument("--wait", action="store_true")
a=p.parse_args()
name=os.path.basename(sys.argv[0])
if a.status:
    action="status"
elif a.validate or name == "validate_dev_candidate":
    action="validate"
elif a.consume_feedback or name == "consume_dev_feedback":
    action="consume_feedback"
else:
    action="submit"
request={"action": action}
if a.feedback_digest:
    request["feedback_digest_ack" if action == "submit" else "feedback_digest"] = a.feedback_digest
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect(os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"])
    s.sendall((json.dumps({"token": os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"], **request}) + "\\n").encode())
    response=json.loads(s.makefile().readline())
print(json.dumps(response, ensure_ascii=False, indent=2))
raise SystemExit(0 if response.get("status") == 200 else 1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_builder_provider(path: Path, base_url: str = 'https://api.deepseek.com/v1') -> None:
    direct_builder.write_provider(path, base_url)


def stage_builder_task(
    *,
    run_dir: Path,
    public: Path,
    workspace: Path,
    builder_worktree: Path,
    lifecycle: SocketLifecycle,
    provider: Path,
    image: str,
    pilot: bool = False,
) -> Path:
    task = run_dir / "builder_task"
    # Formal must stop inside the outer --builder-timeout; the pilot/readiness
    # branch keeps the historical cap it has always been launched with.
    builder_task_timeout_seconds = (
        PILOT_BUILDER_TASK_TIMEOUT_SECONDS if pilot else GENERATED_BUILDER_TASK_TIMEOUT_SECONDS
    )
    (task / "environment").mkdir(parents=True, exist_ok=True)
    (task / "tests").mkdir(parents=True, exist_ok=True)
    resource_check = task/'environment/builder_resource_check.py'
    shutil.copyfile(ROOT/'harbor/builder_resource_check.py', resource_check)
    persistent_home = run_dir/'builder_workspace/codex_home'
    persistent_home.mkdir(parents=True, exist_ok=False)
    (task / "task.toml").write_text(
        f"""schema_version = "1.4"
[task]
name = "local/deepcode-claim-traceability-agentloop-builder"
version = "1.0.0"
description = "DeepCode same-session {'one-case pilot' if pilot else 'two-public-dev formal'} feedback lifecycle"
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
docker_image = "{image}"
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
""",
        encoding="utf-8",
    )
    public_description = "dev_001 and dev_002"
    hidden_description = "test_001 only as pipeline evidence" if pilot else "all six hidden cases"
    # The eleven-key contract is the READINESS contract. Only a readiness run
    # builds readiness_metadata (SocketLifecycle.validate), and only then does
    # evaluator/harness/common.py widen expected_run_keys past the seven shared
    # fields; in formal it demands exactly seven, so printing the eleven-key
    # paragraph there refuses every compliant delivery 422 at preflight and at
    # the submission gate until the Builder guesses to drop four keys.
    readiness_key_contract = """run_report.json must carry exactly eleven keys: the seven above plus these four
lifecycle keys. A run_report with more or fewer keys is refused before it
reaches a case, and the refusal does not tell you which key was wrong.

- `builder_session_id`: the `builder_session_id` that submit_dev_candidate
  reports in its status payload. Read it; do not invent one.
- `submission_number`: 1 for your first accepted submission, 2 for the next.
- `revision_of_candidate_digest`: null on the first submission; on a later one,
  the `delivery_candidate_digest` reported for the submission this revises --
  the field of that exact name in the submit/status payload, not
  `candidate_digest`, which is a different digest of a different tree.
- `feedback_digest`: null on the first submission; on a later one, the exact
  digest you passed to consume_dev_feedback."""
    formal_key_contract = """run_report.json must carry exactly those seven keys and no others:
schema_version, status, artifact_paths, errors, runtime_seconds,
peak_memory_bytes, api_calls. A run_report with more or fewer keys is refused
before it reaches a case. Do not add session, submission or feedback fields to
it; the lifecycle identifiers travel in the submit/status payloads instead."""
    run_report_key_contract = (
        readiness_key_contract if lifecycle.readiness_profile else formal_key_contract
    )
    readiness_note = ("""

Under this readiness profile, complete exactly two accepted submissions and then
stop. The sentence above about finishing normally after the first accepted
submission does not apply here: one accepted submission is not a complete run.
The second submission must be a genuine revision with a distinct product digest,
must call consume_dev_feedback with the exact feedback digest returned for the
first, and must carry that digest in run_report.json.
""" if lifecycle.readiness_profile else "")
    (task / "instruction.md").write_text(
        f"""# DeepCode Edit Builder: same-session feedback lifecycle

You are the single upper Builder session. Use only /builder-package/input,
/builder-package/dev_cases, and the supplied repository. Do not inspect hidden
cases, evaluator code, credentials, prior runs, or Candidate snapshots.

Work in /workspace/worktree and maintain exactly these three delivery files in
/workspace/submission: solution.patch, edit_report.json, and run_report.json.
The host creates the pinned Git baseline, so every Git command in the Builder
container must use command-scoped `git -c safe.directory=/workspace/worktree
-C /workspace/worktree ...`; do not change global Git configuration.

Before each submission, first run `test -s /workspace/submission/solution.patch`
and equivalent `test -s` checks for edit_report.json and run_report.json. If a
JSON report is missing, write it before validating; the minimum valid templates
are `{{"commands": [], "compatibility_notes": [], "limitations": []}}` for
edit_report.json and
`{{"schema_version":"1.0","status":"completed","artifact_paths":["solution.patch","edit_report.json","run_report.json"],"errors":[],"runtime_seconds":0,"peak_memory_bytes":0,"api_calls":{{"gateway":0,"serper":0,"web_retrieval":0}}}}` for run_report.json.

{run_report_key_contract}

Then call validate_dev_candidate. A failed validation does not consume an
accepted submission slot. Create the first complete submission and call
submit_dev_candidate --wait. The evaluator runs {public_description} against
the Candidate's actual DeepCode product and returns authoritative feedback.

Read that feedback. You may finish normally after the first accepted submission.
If you choose to submit again, call consume_dev_feedback with the exact digest,
make a genuine product revision, regenerate the delivery files and submit with
that same feedback digest. Repeat this feedback-
revision cycle as needed, up to {lifecycle.max_dev_rounds} accepted submissions.
Every accepted submission must have a distinct product digest; infrastructure
invalid attempts do not consume a slot. Dev mean >60 is feedback, not an
automatic stop. Hidden cases are unavailable in this Builder session and
{hidden_description} executes only after Builder exit freezes the latest
accepted submission. This run is {'a non-formal pilot that never publishes Result or Code' if pilot else 'the formal lifecycle'}.{readiness_note}
""",
        encoding="utf-8",
    )
    helper = task / "environment" / "submit_dev_candidate"
    write_builder_helper(helper)
    validate = task / "environment" / "validate_dev_candidate"
    consume = task / "environment" / "consume_dev_feedback"
    shutil.copy2(helper, validate)
    shutil.copy2(helper, consume)
    validate.chmod(0o755)
    consume.chmod(0o755)
    test = task / "tests" / "test.sh"
    test.write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    test.chmod(0o755)
    # Harbor resolves CODEX_CONFIG_TOML_PATH on the host before container
    # startup, so the run-local provider must exist at this exact absolute path
    # on both the host and in the Builder container.
    provider_target = str(provider.resolve())
    write_json(
        task / "environment" / "docker-compose.yaml",
        {
            "services": {
                "main": {
                    "command": ["sh", "-c", "mkdir -p /tmp/codex-home && exec sleep infinity"],
                    "cpu_quota": 800000, "cpu_period": 100000,
                    "volumes": [
                        {"type": "bind", "source": str(persistent_home), "target": "/tmp/codex-home"},
                        {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
                        {
                            "type": "bind",
                            "source": str(public),
                            "target": "/builder-package",
                            "read_only": True,
                        },
                        {
                            "type": "bind",
                            "source": str(workspace),
                            "target": "/workspace/submission",
                        },
                        {
                            "type": "bind",
                            "source": str(builder_worktree),
                            "target": "/workspace/worktree",
                        },
                        {
                            "type": "bind",
                            "source": str(lifecycle.socket_path),
                            "target": CONTROLLER_SOCKET_IN_CONTAINER,
                            "read_only": True,
                        },
                        {
                            "type": "bind",
                            "source": str(helper),
                            "target": "/usr/local/bin/submit_dev_candidate",
                            "read_only": True,
                        },
                        {
                            "type": "bind",
                            "source": str(validate),
                            "target": "/usr/local/bin/validate_dev_candidate",
                            "read_only": True,
                        },
                        {
                            "type": "bind",
                            "source": str(consume),
                            "target": "/usr/local/bin/consume_dev_feedback",
                            "read_only": True,
                        },
                        {
                            "type": "bind",
                            "source": str(provider),
                            "target": provider_target,
                            "read_only": True,
                        },
                    ],
                    "environment": {
                        "AGENTSWE_DEV_CONTROLLER_SOCKET": CONTROLLER_SOCKET_IN_CONTAINER,
                        "AGENTSWE_DEV_CONTROLLER_TOKEN": lifecycle.token,
                        "AGENTSWE_BUILDER_BROKER_TOKEN": PLACEHOLDER,
                    },
                }
            }
        },
    )
    config = run_dir / "builder_job_config.json"
    write_json(
        config,
        {
            "job_name": f"deepcode-agentloop-builder-{run_dir.name}",
            "jobs_dir": str(run_dir / "jobs"),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "quiet": True,
            "retry": {"max_retries": 0},
            "environment": {"type": "docker", "delete": True, "cpu_enforcement_policy": "limit"},
            "agents": [
                {
                    "import_path": "agentswe_codex_resume:CodexResume",
                    "model_name": BUILDER_MODEL,
                    "env": {
                        "CODEX_HOME": "/tmp/agentswe-codex-home",
                        "CODEX_CONFIG_TOML_PATH": provider_target,
                        "AGENTSWE_BUILDER_BROKER_TOKEN": PLACEHOLDER,
                    },
                    "kwargs": {
                        "reasoning_effort": BUILDER_EFFORT,
                        "web_search": "live",
                    },
                }
            ],
            "tasks": [{"path": str(task)}],
        },
    )
    return config


def run_harbor(harbor: Path, config: Path, run_dir: Path, timeout_seconds: int, *, lifecycle,
               credential, proxy='http://127.0.0.1:7890', provider_url='https://api.deepseek.com/v1') -> dict[str, Any]:
    result = run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
        harbor=harbor, timeout=timeout_seconds, proxy=proxy, provider_url=provider_url)
    return {'exit_code': result.returncode, 'timed_out': result.returncode == 124,
        'stdout': str(run_dir/'builder.stdout.log'), 'stderr': str(run_dir/'builder.stderr.log')}


def freeze_if_ready(lifecycle: SocketLifecycle, *,
                    builder_exit_evidence: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if (
        lifecycle.controller.records
        and lifecycle.controller.frozen is None
    ):
        return lifecycle.session.freeze(builder_exit_evidence=builder_exit_evidence)
    return lifecycle.controller.frozen


def builder_attestation(
    lifecycle: SocketLifecycle,
    builder: dict[str, Any],
) -> dict[str, Any]:
    records = lifecycle.controller.records
    interrupted_at_max_rounds = formal_max_rounds_interrupt(lifecycle, builder['exit_code'])
    native = lifecycle.native_attestation(builder['exit_code'],
                                          allow_interrupted=interrupted_at_max_rounds)
    event_sessions = {
        event.get("builder_session_id")
        for event in lifecycle.events
        if event.get("builder_session_id")
    }
    result: dict[str, Any] = {
        "schema_version": "deepcode-agentloop-builder-session-attestation-v2",
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "builder_session_id": lifecycle.session_id,
        "single_harbor_invocation": True,
        "same_continuous_session": native['valid'],
        "native_attestation": native,
        "builder_process": builder,
        "accepted_submission_count": len(records),
        "evidence_kind": lifecycle.evidence_kind,
        "public_case_inventory": list(lifecycle.public_case_ids),
        "hidden_case_inventory": list(lifecycle.hidden_case_ids),
        "accepted_submissions_public_complete": bool(records) and all(public_round_complete(record, lifecycle.public_case_ids) for record in records),
        "feedback_consumed": lifecycle.session.feedback_consumed,
        "feedback_digest": lifecycle.session.feedback_digest,
        "latest_feedback_digest_ack": lifecycle.session.feedback_digest_ack,
        "feedback_ack_matches": bool(
            lifecycle.session.feedback_digest
            and lifecycle.session.feedback_digest_ack
            == lifecycle.session.feedback_digest
        ),
        "distinct_candidate_digests": bool(
            bool(records) and len({record.get("candidate_digest") for record in records}) == len(records)
        ),
        "freeze": lifecycle.controller.frozen,
        "events": lifecycle.events,
        "session_events": lifecycle.session.events,
    }
    result["builder_interrupted_at_max_dev_rounds"] = interrupted_at_max_rounds
    result["complete"] = all(
        (
            builder.get("exit_code") == 0 or interrupted_at_max_rounds,
            result["same_continuous_session"],
            1 <= len(records) <= lifecycle.max_dev_rounds,
            result["accepted_submissions_public_complete"],
            result["distinct_candidate_digests"],
            isinstance(result["freeze"], dict),
        )
    )
    result["formal_lifecycle_eligible"] = bool(lifecycle.evidence_kind == "formal" and lifecycle.public_case_ids == PUBLIC_CASES and lifecycle.hidden_case_ids == HIDDEN_CASES and result["complete"])
    # Readiness exposes dev_001 only, so the pin follows the profile instead of
    # being dropped: an unexpected case set must still fail this gate. The
    # evidence-side check (accepted_submissions_public_complete) already
    # validates each accepted round against exactly this same set.
    readiness = (lifecycle.run_dir / "readiness_current_binding.json").is_file()
    expected_public = READINESS_PUBLIC_CASES if readiness else PUBLIC_CASES
    result["pilot_lifecycle_eligible"] = bool(lifecycle.evidence_kind == "pilot" and lifecycle.public_case_ids == expected_public and lifecycle.hidden_case_ids == ("test_001",) and result["complete"])
    return result


def protocol_lock(
    *,
    formal_started: bool,
    builder_endpoint: str | None = None,
    public_endpoint: str | None = None,
    hidden_endpoint: str | None = None,
    builder_broker_script: str | None = None,
    public_broker_script: str | None = None,
    hidden_broker_script: str | None = None,
    pilot: bool = False,
) -> dict[str, Any]:
    public_cases = PUBLIC_CASES
    hidden_cases = ("test_001",) if pilot else HIDDEN_CASES
    return {
        "schema_version": "deepcode-agentloop-pilot-protocol-v1" if pilot else "deepcode-agentloop-formal-protocol-v2",
        "evidence_kind": "pilot" if pilot else "formal",
        "formal_execution_started": formal_started,
        "builder": {
            "model": BUILDER_MODEL,
            "reasoning_effort": BUILDER_EFFORT,
            "single_continuous_session": True,
            "provider_base_url": builder_endpoint,
            "transport": "native-codex-direct",
            "credential": "native-direct-auth-tmpfs",
            "builder_broker_started": False,
        },
        "lower_agent": {
            "product": "Candidate-modified DeepCode",
            "model": MODEL,
            "reasoning_effort": LOWER_EFFORT,
            "public_broker_endpoint": public_endpoint,
            "hidden_broker_endpoint": hidden_endpoint,
            "public_broker_script": public_broker_script,
            "hidden_broker_script": hidden_broker_script,
            "broker_kind": "deepcode",
            "candidate_credential": PLACEHOLDER,
            "real_credential_visible": False,
            "credential_mount_read_only": True,
        },
        "builder_direct_with_independent_public_hidden_brokers": True,
        "public_cases": list(public_cases),
        "hidden_cases": list(hidden_cases),
        "hidden_after_freeze_only": True,
        "result_axis": "N/A until six real hidden cases and independent Result judge",
        "code_axis": "N/A until independent Code judge",
        "formal_finalizer_allowed": not pilot,
    }


def static_audit(run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    public = stage_public_package(run_dir)
    lock_path = run_dir / "protocol_lock.json"
    write_json(lock_path, protocol_lock(formal_started=False))
    return {
        "schema_version": "deepcode-agentloop-formal-static-v1",
        "status": "static_config_ready",
        "formal_execution_started": False,
        "public_package": str(public),
        "protocol_lock": str(lock_path),
        "formal_result_claimed": False,
        "code_score_claimed": False,
    }


def verify_readiness_binding(args: argparse.Namespace) -> dict[str, Any] | None:
    """Verify the readiness binding through the coordinator's own verifier.

    The binding covers all ten tasks, so this refuses anything the shared
    ``verify_binding`` does not reproduce byte for byte, rather than trusting
    the file it was handed.
    """
    profile = getattr(args, "readiness_profile", None)
    if not profile:
        return None
    if profile != READINESS_PROFILE:
        raise ValueError("unsupported readiness profile")
    if not args.readiness_binding_file or not args.readiness_binding_sha256:
        raise ValueError("readiness requires binding file and SHA256")
    path = args.readiness_binding_file.resolve()
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != args.readiness_binding_sha256:
        raise ValueError("readiness binding bytes changed")
    value = read_json(path)
    control_root = Path(os.environ.get("READINESS_CONTROL_ROOT", "@@AGENTSWE_EDITING_CONTROL@@"))
    verifier = control_root / "readiness_binding.py"
    if not verifier.is_file():
        raise ValueError("readiness binding verifier missing")
    import importlib.util
    if str(control_root) not in sys.path:
        sys.path.insert(0, str(control_root))
    spec = importlib.util.spec_from_file_location("readiness_binding", verifier)
    if spec is None or spec.loader is None:
        raise ValueError("readiness binding verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = Path(os.environ.get("READINESS_SOURCE_OVERRIDE", str(ROOT))).resolve()
    measured = module.verify_binding(source, value, control_root=control_root)
    if measured != value:
        raise ValueError("readiness binding changed during verification")
    return value


def one_stop_exit_code(
    *,
    readiness: bool,
    execution_complete: bool,
    infrastructure_invalid: list[str],
    readiness_judges: dict[str, Any] | None,
    finalizer_exit: int | None,
    pilot: bool,
    acceptance_complete: bool,
) -> int:
    """Exit status of a run that reached the end of its hidden phase.

    Under a readiness binding the finalizer is skipped on purpose (the readiness
    judge smoke is the single judge evidence), so a smoke succeeds when its hidden
    execution is complete, nothing in it is infrastructure-invalid and its own
    judge smoke completed. Pilot and formal runs keep the finalizer gate.
    """
    if not execution_complete or infrastructure_invalid:
        return 2
    if readiness:
        complete = isinstance(readiness_judges, dict) and readiness_judges.get("readiness_judges_complete") is True
        return 0 if complete else 2
    return 0 if finalizer_exit == 0 and (not pilot or acceptance_complete) else 2


def run_execution(args: argparse.Namespace, *, pilot: bool) -> int:
    run_dir = args.run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(
            f"refusing to overwrite non-empty run directory: {run_dir}"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    readiness_binding = verify_readiness_binding(args)
    if readiness_binding is not None:
        if not pilot:
            raise ValueError("readiness requires the explicit pilot profile")
        # The Builder runner and the attestation both sit below the parsed
        # arguments, so the verified binding doubles as the runtime marker.
        write_json(run_dir / "readiness_current_binding.json", readiness_binding)
    args.runtime_python = ensure_runtime_python(run_dir, args.runtime_python)
    credential = args.credential_file.resolve()
    harbor = args.harbor.resolve()
    public_broker_script = args.broker_script.resolve()
    builder_broker_script = args.builder_broker_script.resolve()
    if not credential.is_file():
        raise FileNotFoundError(
            f"evaluator credential file missing: {credential}"
        )
    if not harbor.is_file():
        raise FileNotFoundError(f"Harbor executable missing: {harbor}")
    if not public_broker_script.is_file():
        raise FileNotFoundError(f"DeepCode broker script missing: {public_broker_script}")
    if public_broker_script != DEEPCODE_BROKER_SCRIPT.resolve():
        raise RuntimeError("public/hidden lower brokers must use DeepCode broker_server.py")
    dev_names = sorted(
        path.name for path in (ROOT / "dev_cases").iterdir() if path.is_dir()
    )
    hidden_names = sorted(
        path.name for path in (ROOT / "test_cases").iterdir() if path.is_dir()
    )
    if dev_names != list(PUBLIC_CASES):
        raise RuntimeError("public inventory must be exactly two cases")
    if hidden_names != list(HIDDEN_CASES):
        raise RuntimeError("hidden inventory must be exactly six cases")

    public_case_ids = READINESS_PUBLIC_CASES if readiness_binding is not None else PUBLIC_CASES
    hidden_case_ids = ("test_001",) if pilot else HIDDEN_CASES
    evidence_kind = "pilot" if pilot else "formal"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:12]
    public_name = f"deepcode-{evidence_kind}-public-{suffix}"
    builder_name = f"deepcode-{evidence_kind}-builder-{suffix}"
    hidden_name = f"deepcode-{evidence_kind}-hidden-{suffix}"
    judge_name = f"deepcode-{evidence_kind}-result-judge-{suffix}"
    public_endpoint: str | None = None
    builder_endpoint: str | None = None
    hidden_endpoint: str | None = None
    judge_endpoint: str | None = None
    owned_containers: dict[str, str] = {}
    lifecycle: SocketLifecycle | None = None
    summary: dict[str, Any] = {
        "schema_version": "deepcode-agentloop-pilot-summary-v1" if pilot else "deepcode-agentloop-formal-summary-v2",
        "evidence_kind": evidence_kind,
        "pilot": pilot,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "result_axis": "N/A; independent Result judge required",
        "code_axis": "N/A; independent Code judge required",
        "runtime_python": args.runtime_python,
    }
    try:
        public_port = free_port()
        builder_port = free_port()
        while builder_port == public_port:
            builder_port = free_port()
        public_endpoint = start_broker(
            name=public_name,
            port=public_port,
            effort=LOWER_EFFORT,
            credential=credential,
            image=args.broker_image,
            upstream=args.upstream,
            script=public_broker_script,
            broker_kind="deepcode",
            owned_containers=owned_containers,
            evidence_dir=run_dir,
        )
        builder_container_endpoint = args.builder_base_url
        public = stage_public_package(run_dir, public_case_ids)
        judge_endpoint = start_broker(name=judge_name, port=free_port(), effort=BUILDER_EFFORT,
            credential=credential, image=args.broker_image, upstream=args.upstream, script=JUDGE_BROKER_SCRIPT,
            broker_kind="judge_xhigh", owned_containers=owned_containers, evidence_dir=run_dir / "brokers",
            # Handed to the coordinator under readiness, so it has to outlive its
            # own stop; --rm would delete it there and then.
            defer_removal=readiness_binding is not None)
        workspace = run_dir / "builder_workspace" / "submission"
        workspace.mkdir(parents=True)
        builder_worktree = prepare_builder_worktree(
            public,
            run_dir / "builder_workspace" / "worktree",
        )
        provider = run_dir / "builder_provider.toml"
        write_builder_provider(provider, builder_container_endpoint)
        lifecycle = SocketLifecycle(
            run_dir,
            workspace,
            public_endpoint,
            args.runtime_python,
            public_case_ids,
            hidden_case_ids,
            evidence_kind,
            2 if readiness_binding is not None else args.max_dev_rounds,
            result_judge_endpoint=judge_endpoint,
            readiness_profile=READINESS_PROFILE if readiness_binding is not None else None,
            current_binding=readiness_binding,
        )
        lifecycle.start()
        config = stage_builder_task(
            run_dir=run_dir,
            public=public,
            workspace=workspace,
            builder_worktree=builder_worktree,
            lifecycle=lifecycle,
            provider=provider,
            image=args.builder_image,
            pilot=pilot,
        )
        write_json(
            run_dir / "protocol_lock.json",
            protocol_lock(
                formal_started=True,
                builder_endpoint=builder_container_endpoint,
                public_endpoint=public_endpoint,
                builder_broker_script=str(builder_broker_script),
                public_broker_script=str(public_broker_script),
                pilot=pilot,
            ),
        )
        lifecycle.event("builder_invocation_started")
        builder = run_harbor(
            harbor,
            config,
            run_dir,
            args.builder_timeout, lifecycle=lifecycle, credential=credential,
            proxy=args.builder_proxy, provider_url=args.builder_base_url,
        )
        lifecycle.event(
            "builder_invocation_finished",
            exit_code=builder["exit_code"],
            timed_out=builder["timed_out"],
        )
        # The readiness freeze checks the Builder's return code, its native
        # validity and the session the rounds came from. All three are known
        # here, and passing none of them made the check unsatisfiable. The
        # attestation is bound to a name rather than called a second time at
        # this site; builder_attestation below computes its own.
        builder_interrupted_at_max_rounds = formal_max_rounds_interrupt(lifecycle, builder['exit_code'])
        native_attestation = lifecycle.native_attestation(
            builder['exit_code'], allow_interrupted=builder_interrupted_at_max_rounds)
        if native_attestation['valid']:
            freeze_if_ready(lifecycle, builder_exit_evidence={
                'returncode': builder['exit_code'],
                'native_valid': True,
                'builder_session_id': lifecycle.session_id,
            })
        attestation = builder_attestation(lifecycle, builder)
        write_json(
            run_dir / "builder_session_attestation.json",
            attestation,
        )
        summary.update(
            {
                "status": "builder_lifecycle_incomplete",
                "builder": builder,
                "builder_session_attestation": str(
                    run_dir / "builder_session_attestation.json"
                ),
            }
        )
        lifecycle_eligible = attestation["pilot_lifecycle_eligible" if pilot else "formal_lifecycle_eligible"]
        if not lifecycle_eligible:
            write_json(run_dir / "summary.json", summary)
            return 2

        hidden_port = free_port()
        hidden_endpoint = start_broker(
            name=hidden_name,
            port=hidden_port,
            effort=LOWER_EFFORT,
            credential=credential,
            image=args.broker_image,
            upstream=args.upstream,
            script=public_broker_script,
            broker_kind="deepcode",
            owned_containers=owned_containers,
            evidence_dir=run_dir,
        )
        hidden_before = broker_stats(hidden_endpoint)
        hidden_health = broker_health(hidden_endpoint)
        write_json(run_dir / "hidden_broker_before.json", hidden_before)
        write_json(run_dir / "hidden_broker_health_before.json", hidden_health)
        if not _broker_protocol_matches(
            hidden_health,
            hidden_before,
            effort=LOWER_EFFORT,
            broker_kind="deepcode",
        ):
            raise RuntimeError(
                "fresh hidden broker failed model/effort/zero-call/zero-failure gate"
            )
        write_json(
            run_dir / "protocol_lock.json",
            protocol_lock(
                formal_started=True,
                builder_endpoint=builder_container_endpoint,
                public_endpoint=public_endpoint,
                hidden_endpoint=hidden_endpoint,
                builder_broker_script=str(builder_broker_script),
                public_broker_script=str(public_broker_script),
                hidden_broker_script=str(public_broker_script),
                pilot=pilot,
            ),
        )
        lifecycle.controller.broker_endpoint = hidden_endpoint
        hidden = lifecycle.controller.run_all_hidden()
        hidden_attestation_path = (
            run_dir
            / "lifecycle"
            / "hidden-after-freeze-attestation.json"
        )
        hidden_attestation = (
            read_json(hidden_attestation_path)
            if hidden_attestation_path.is_file()
            else {"complete": False}
        )
        readiness_judges = None
        if readiness_binding is not None:
            # Independent Result and Code judge smoke over the frozen candidate.
            # The smoke module reads this run's own freeze document and refuses
            # anything not sealed with the readiness profile, so the dispatch is
            # bound to the same freeze the controller wrote.
            from evaluator.readiness_smoke import run as run_readiness_smoke
            readiness_judges = run_readiness_smoke(
                task_root=ROOT, run_dir=run_dir, hidden=hidden_attestation,
                credential=credential, result_endpoint=judge_endpoint)
            write_json(run_dir / "readiness_judges.json", readiness_judges)
        classifications = {
            str(item.get("classification"))
            for item in hidden
            if isinstance(item, dict)
        }
        infrastructure_invalid = sorted(
            classifications & INFRA_CLASSIFICATIONS
        )
        execution_complete = hidden_attestation.get("pilot_complete" if pilot else "complete") is True
        summary.update(
            {
                "status": (
                    "pilot_pipeline_complete" if pilot
                    else "lifecycle_complete"
                    if execution_complete and not infrastructure_invalid
                    else (
                        "hidden_infrastructure_invalid"
                        if infrastructure_invalid
                        else "hidden_evidence_incomplete"
                    )
                ),
                "freeze_manifest": str(
                    run_dir / "lifecycle" / "freeze_manifest.json"
                ),
                "hidden": hidden,
                "hidden_attestation": str(hidden_attestation_path),
                "hidden_execution_complete": execution_complete,
                "hidden_infrastructure_classifications": infrastructure_invalid,
            }
        )
        finalizer_exit = None
        finalizer_result = None
        # Under a readiness binding the smoke above is the (single) judge evidence; the
        # acceptance finalizer would re-judge the same hidden case and break the
        # one-logical-request contract (0919-ds-001: two identical ledger rows).
        if readiness_binding is None and (run_dir / "lifecycle/freeze_manifest.json").is_file():
            write_json(run_dir / "result_judge_broker_before.json", broker_stats(judge_endpoint))
            command = [
                sys.executable, str(ROOT / "evaluator" / "formal_finalize.py"),
                "--run-dir", str(run_dir),
                "--credential-file", str(args.credential_file.resolve()),
                "--result-judge-broker-endpoint", judge_endpoint,
            ]
            if pilot:
                command.extend(["--acceptance-cases", *hidden_case_ids])
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            finalizer_exit = completed.returncode
            aggregation_path = run_dir / ("acceptance_aggregation.json" if pilot else "formal_aggregation.json")
            if aggregation_path.is_file():
                finalizer_result = read_json(aggregation_path)
        pilot_measurement = finalizer_result if pilot else None
        summary.update({
            "formal_finalizer_exit": finalizer_exit,
            "formal_finalizer_invoked": bool(not pilot and finalizer_exit is not None),
            "acceptance_finalizer_invoked": bool(pilot and finalizer_exit is not None),
            "acceptance_complete": bool(pilot and finalizer_result and finalizer_result.get("acceptance_complete")),
            "pilot_evidence_complete": execution_complete if pilot else False,
            "pilot_measurement": pilot_measurement if pilot else None,
            "formal_aggregation": "formal_aggregation.json" if finalizer_result else None,
            "formal_result_claimed": bool(finalizer_result and finalizer_result.get("formal_result_publishable")),
            "code_score_claimed": bool(finalizer_result and finalizer_result.get("code_score_publishable")),
            "result_axis": finalizer_result.get("result_axis") if finalizer_result else "N/A",
            "code_axis": finalizer_result.get("code_axis") if finalizer_result else "N/A",
            "combined_score": None,
            "result_judge_broker_owned_by_one_stop": judge_endpoint is not None,
            "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent,
            "timeout_contract": builder_timeout_contract(args.builder_timeout),
        })
        write_json(run_dir / "summary.json", summary)
        return one_stop_exit_code(
            readiness=readiness_binding is not None,
            execution_complete=execution_complete,
            infrastructure_invalid=infrastructure_invalid,
            readiness_judges=readiness_judges,
            finalizer_exit=finalizer_exit,
            pilot=pilot,
            acceptance_complete=bool(summary.get("acceptance_complete")),
        )
    except Exception as exc:
        import sys as _sys, traceback as _tb
        summary.update(
            {
                "status": "evaluator_infrastructure_error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": _tb.format_exc(),
            }
        )
        write_json(run_dir / "summary.json", summary)
        print(_tb.format_exc(), file=_sys.stderr)
        return 2
    finally:
        controller_close_error = None
        if lifecycle is not None:
            try:
                lifecycle.close()
            except Exception as exc:
                controller_close_error = f"{type(exc).__name__}: {exc}"
        for role, endpoint in (
            ("public", public_endpoint),
            ("builder", builder_endpoint),
            ("hidden", hidden_endpoint),
            ("result_judge", judge_endpoint),
        ):
            if endpoint:
                write_json(
                    run_dir / f"{role}_broker_health.json",
                    broker_health_or_error(endpoint),
                )
                write_json(
                    run_dir / f"{role}_broker_stats.json",
                    broker_stats_or_error(endpoint),
                )
        cleanup_results = []
        retained_judge = None
        retention_error = None
        if readiness_binding is not None and owned_containers.get(judge_name):
            # The coordinator needs one provably owned container that still
            # exists; removing everything here leaves it an empty declaration.
            try:
                from harbor.readiness_resources import retain_container
                judge_cid = run_dir / "brokers/result_judge.cid"
                transport = (judge_cid.parent / (judge_cid.stem + "-judge-transport")).resolve()
                retained_judge = retain_container(
                    owned_containers[judge_name], run_dir,
                    expected_name=judge_name,
                    expected_mount=(str(transport), "/evidence"))
            except Exception as exc:
                # Unprovable ownership must not become a silently skipped
                # cleanup; fall through and remove it as usual.
                retention_error = f"{type(exc).__name__}: {exc}"
        cleanup_order = (judge_name, hidden_name, public_name, builder_name)
        for name in cleanup_order:
            container_id = owned_containers.get(name)
            if container_id is None:
                continue
            if retained_judge is not None and name == judge_name:
                cleanup_results.append({"name": name, "container_id": container_id, "owned": True,
                                        "absent_after_cleanup": False, "removal_deferred": True,
                                        "retained_terminal": True, "cleanup_attempted": False})
                continue
            try:
                cleanup_results.append(remove_and_verify_broker(name, container_id))
            except Exception as exc:
                cleanup_results.append({
                    "name": name,
                    "container_id": container_id,
                    "owned": True,
                    "absent_after_cleanup": False,
                    "cleanup_error": f"{type(exc).__name__}: {exc}",
                })
        retained_resources = None
        if retained_judge is not None:
            try:
                import types as _types
                from harbor.readiness_resources import retained_manifest
                retained_resources = retained_manifest(
                    run_dir, [_types.SimpleNamespace(container_id=retained_judge["container_id"])])
            except Exception as exc:
                retention_error = ((retention_error + "; ") if retention_error else "") + \
                    f"retained_manifest: {type(exc).__name__}: {exc}"
        write_json(run_dir / "cleanup_attestation.json", {
            "schema_version": "agentswe-deepcode-cleanup-attestation-v2",
            "removal_deferred": retained_judge is not None,
            "coordinator_cleanup_required": retained_judge is not None,
            "retained_resources": retained_resources,
            "retention_error": retention_error,
            "completed": controller_close_error is None and all(item["absent_after_cleanup"] for item in cleanup_results),
            "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
            "controller_closed": controller_close_error is None and (lifecycle is None or not lifecycle.socket_path.exists()),
            "controller_close_error": controller_close_error,
            "unrelated_containers_touched": False,
            "containers": cleanup_results,
            "owned_container_ids": owned_containers,
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DeepCode one-stop Agent-loop formal orchestration"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--run-formal",
        action="store_true",
        help="start Harbor/provider execution; omitted means static audit only",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="run both public dev cases per Candidate, feedback, freeze and test_001 smoke evidence",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="provider-free static/pilot protocol check",
    )
    parser.add_argument(
        "--credential-file",
        type=Path,
        default=Path(
            "@@AGENTSWE_CREDENTIAL_FILE@@"
        ),
    )
    parser.add_argument(
        "--harbor",
        type=Path,
        default=Path("@@AGENTSWE_HARBOR_BIN@@"),
    )
    parser.add_argument("--broker-image", default=BROKER_IMAGE)
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--builder-host-address", default="172.17.0.1")
    parser.add_argument(
        "--broker-script",
        type=Path,
        default=DEEPCODE_BROKER_SCRIPT,
        help="DeepCode medium broker used by public and hidden lower-agent runs",
    )
    parser.add_argument(
        "--builder-broker-script",
        type=Path,
        default=DEFAULT_BUILDER_BROKER_SCRIPT,
        help="independently validated deepseek-flash/xhigh Builder broker",
    )
    parser.add_argument(
        "--upstream",
        default=os.environ.get(
            "AGENTSWE_UPSTREAM",
            "https://api.deepseek.com",
        ),
    )
    parser.add_argument("--python", dest="runtime_python")
    parser.add_argument("--builder-timeout", type=int, default=28920)
    parser.add_argument('--builder-base-url', default='https://api.deepseek.com/v1')
    parser.add_argument('--builder-proxy', default='http://127.0.0.1:7890')
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--readiness-profile", choices=[READINESS_PROFILE])
    parser.add_argument("--readiness-binding-file", type=Path)
    parser.add_argument("--readiness-binding-sha256")
    parser.add_argument("--result-judge-broker-endpoint", help=argparse.SUPPRESS)
    parser.add_argument("--code-contract", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent is fixed at 1")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    try:
        builder_timeout_contract(args.builder_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not builder_timeout_contract(args.builder_timeout)["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS} seconds required)")
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
    install_summary_writer(
        args.run_dir,
        max_dev_rounds=args.max_dev_rounds,
        n_concurrent=args.n_concurrent,
        mode="formal" if args.run_formal else "pilot" if args.pilot else "static",
    )
    if args.run_formal and args.pilot:
        parser.error("--run-formal and --pilot are mutually exclusive")
    if args.self_test:
        if args.run_formal:
            parser.error("--self-test and --run-formal are mutually exclusive")
        if not args.broker_script.is_file():
            parser.error(f"DeepCode broker script missing: {args.broker_script}")
        print(json.dumps({
            "self_test": "PASS",
            "execution_mode": "pilot" if args.pilot else "static",
            "provider_calls": 0,
            "docker_started": False,
            "harbor_started": False,
            "builder": {
                "model": BUILDER_MODEL,
                "reasoning_effort": BUILDER_EFFORT,
                "single_continuous_session": True,
                "transport": "native-codex-direct",
                "provider_base_url": args.builder_base_url,
                "builder_broker_started": False,
            },
            "lower_agent": {
                "product": "Candidate-modified DeepCode",
                "model": MODEL,
                "reasoning_effort": LOWER_EFFORT,
                "candidate_credential": PLACEHOLDER,
            },
            "lower_broker_lifecycle": {
                "public": "DeepCode broker_server.py for Candidate public rounds",
                "hidden": "fresh DeepCode broker_server.py only after latest accepted Candidate freeze",
                "hidden_initial_calls_required": 0,
                "hidden_initial_failures_required": 0,
                "endpoint_switch_required": True,
                "credential_mount_read_only": True,
            },
            "public_cases": list(PUBLIC_CASES),
            "hidden_cases": ["test_001"] if args.pilot else list(HIDDEN_CASES),
            "formal_result_claimed": False,
            "code_score_claimed": False,
        }, indent=2, ensure_ascii=False))
        return 0
    if args.run_formal:
        return run_execution(args, pilot=False)
    if args.pilot:
        return run_execution(args, pilot=True)
    print(
        json.dumps(
            static_audit(args.run_dir.resolve()),
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
