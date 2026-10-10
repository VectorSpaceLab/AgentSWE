#!/usr/bin/env python3
"""One-stop Dyad Edit Agent-loop orchestration.

The command is offline by default. Explicit live modes use one uninterrupted
Builder session for up to ten accepted Candidates, fresh evaluator feedback
after each acceptance, and latest-accepted freeze on Builder exit or the round
limit.
Formal mode uses the full public/hidden inventories; pilot mode uses both
public dev cases per Candidate and test_001 once after freeze. The hidden lower broker is
always created fresh after freeze. Result and Code score are never claimed by
the pilot path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

JUDGE_BROKER_SCRIPT = Path("@@AGENTSWE_EDITING_CONTROL@@/judge_broker_xhigh.py")
PUBLIC_REEVALUATIONS = 2  # re-runs of one product after evaluator-voided public evaluations


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
CODE_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
CODE_JUDGE_RUNNER_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.candidate_materialize import materialize  # noqa: E402
from harbor.builder_protocol import (  # noqa: E402
    BUILDER_EFFORT,
    BUILDER_MODEL,
    DEV_CASES,
    HIDDEN_CASES,
    LOWER_EFFORT,
    LOWER_MODEL,
    PLACEHOLDER,
    feedback_digest,
    tree_digest,
    write_json,
)

from evaluator.semantic_execution import normalize_execution, score_public, public_case_feedback
from harbor.stable_product import product_source_digest
from evaluator.frozen_evidence import (validate_freeze, timestamp, sha256 as evidence_sha256,
    modes_digest, write_once as write_evidence_once, fsync_directory)
from harbor.build_preflight import run_typecheck
from harbor.native_builder_evidence import observe_thread, verify_native
from harbor.native_builder_runner import run_native_builder
from harbor import direct_harbor_builder as direct_builder

BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
LOWER_IMAGE = "agentswe/edit-candidate-python311:0826"
SHARED_XHIGH_BROKER = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)
PROVIDER_KEYS = {"AGENTSWE_PROVIDER_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"}
ACCEPTANCE_REQUIRED_FILES = (
    "src/ipc/types/acceptance.ts",
    "src/ipc/handlers/acceptance_handlers.ts",
)
ACCEPTANCE_SERVICE_FILES = (
    "src/ipc/services/acceptance_store.ts",
    "src/ipc/services/acceptance_service.ts",
)
MODEL_ARTIFACT_SCHEMA = "dyad-lower-agent-artifact-v3"
MODEL_ARTIFACT_OWNER = "model_via_dyad_typed_chat"
MODEL_ARTIFACT_PRODUCER = "model finish action captured from Dyad persisted typed chat"
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"
# 5 h Builder cap (D2, 2026-09-19).  The shared driver
# @@AGENTSWE_EDITING_CONTROL@@/formal_commands.py passes
# --builder-timeout 18000, so the generated Harbor task.toml must stop the
# native Builder BEFORE the outer wait SIGTERMs it; otherwise run_builder
# reports 124 and both exit_code == 0 gates below void the run.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
# The pilot/readiness branch keeps its historical 8 h generated cap: readiness
# passes --builder-timeout 28800 explicitly and its two-round canary approaches
# neither bound, so readiness behaviour is unchanged.
PILOT_BUILDER_TASK_TIMEOUT_SECONDS = 28_800
# run_native_builder turns an outer-deadline SIGTERM into 124 and a
# KeyboardInterrupt into 130.  Only 124 is a survivable interrupt; 125 and 130
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


def formal_max_rounds_interrupt(lifecycle: "Lifecycle", exit_code: Any) -> bool:
    """True only for a formal Builder killed after spending every round.

    dyad never freezes mid-run, so there is no freeze_reason to read: submit
    returns 409 submission_limit_reached once the budget is spent and the
    freeze happens after Builder exit.  A Builder that has consumed the whole
    accepted-round budget and is then killed by the outer deadline has produced
    complete evidence; anything else has not.  The pilot/readiness path is
    excluded so its own builder_exit == 0 gate keeps deciding it.
    """
    return bool(
        exit_code in BUILDER_INTERRUPT_EXIT_CODES
        and not lifecycle.pilot_not_formal
        and getattr(lifecycle, "readiness_profile", None) is None
        and lifecycle.required_valid_rounds is None
        and lifecycle.max_dev_rounds
        and len(lifecycle.records) == lifecycle.max_dev_rounds
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def broker_base(endpoint: str) -> str:
    return endpoint.removesuffix("/v1/responses").rstrip("/")


def broker_health(endpoint: str) -> dict[str, Any]:
    # Health is a loopback-only evaluator probe. Never send it through the
    # host HTTP proxy; proxied 127.0.0.1 requests can return a misleading 502.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(broker_base(endpoint) + "/healthz", timeout=5) as response:
        value = json.loads(response.read())
    return value if isinstance(value, dict) else {}


def broker_stats(endpoint: str) -> dict[str, Any]:
    request = urllib.request.Request(broker_base(endpoint) + "/stats", headers={"Authorization": "Bearer stats-only-placeholder"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("broker stats must be an object")
    return value


def fresh_xhigh_result_judge(stats: dict[str, Any]) -> bool:
    return _judge_runtime().fresh_judge_stats(stats)


def reject_external_result_judge_endpoint(*, formal: bool, endpoint: str | None) -> None:
    if formal and endpoint:
        raise ValueError(
            "--result-judge-broker-endpoint is evaluator-owned in formal mode and must not be supplied"
        )


def formal_finalizer_command(
    *, run_dir: Path, output: Path, credential: Path, result_judge_endpoint: str,
    acceptance_cases: tuple[str, ...] = (),
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "evaluator/formal_finalize.py"),
        "--run-dir", str(run_dir),
        "--output", str(output),
        "--credential-file", str(credential),
        "--result-judge-broker-endpoint", result_judge_endpoint,
        "--result-judge", str(RESULT_JUDGE_ENTRY),
        "--code-judge", str(CODE_JUDGE_RUNNER_ENTRY),
    ]
    if acceptance_cases:
        if any(case not in HIDDEN_CASES for case in acceptance_cases):
            raise ValueError("acceptance cases must be hidden inventory members")
        command.extend(["--acceptance-cases", *acceptance_cases])
    return command


def sensitive_text(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return True
    return any(f"{key}=" in text for key in PROVIDER_KEYS)


def validate_delivery(delivery: Path) -> list[str]:
    """Validate the three-file Builder delivery without consuming a round."""
    errors: list[str] = []
    if not delivery.is_dir():
        return ["submission directory is missing"]
    entries = list(delivery.iterdir())
    names = {item.name for item in entries}
    if any(item.is_symlink() or not item.is_file() for item in entries):
        errors.append("delivery top level must contain regular files only")
    expected = {"solution.patch", "edit_report.json", "run_report.json"}
    errors.extend(f"missing {name}" for name in sorted(expected - names))
    errors.extend(f"unexpected top-level artifact {name}" for name in sorted(names - expected))
    if errors:
        return errors
    if any(sensitive_text(delivery / name) for name in expected):
        errors.append("provider credential material is forbidden in Candidate delivery")
    try:
        from dev_cases.public_harness import validate_submission
        validate_submission(ROOT / "input/repository", delivery)
    except Exception as exc:
        errors.append(f"delivery contract failure: {type(exc).__name__}: {exc}")
    return errors


def command_result(command: list[str], cwd: Path, *, timeout: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        completed = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "timeout")
        timed_out = True
    return {"command": command, "exit_code": completed.returncode, "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": completed.stdout[-5000:] if isinstance(completed.stdout, str) else "",
            "stderr_tail": completed.stderr[-5000:] if isinstance(completed.stderr, str) else ""}


def copy_runtime(source: Path, destination: Path, dependency_root: Path) -> Path:
    def remove_readonly(function: Any, path: str, _exc: Any) -> None:
        # Disposable runtimes can inherit mode 0555 from a frozen Candidate.
        # Make only the failing evaluator-owned path writable, then retry the
        # removal operation supplied by shutil.
        failed = Path(path)
        # unlink/rmdir authorization is controlled by the parent directory;
        # chmod the entry too when it is a real directory so recursive cleanup
        # can descend into it.
        for writable in (failed.parent, failed):
            try:
                if writable.is_dir() and not writable.is_symlink():
                    writable.chmod(writable.stat().st_mode | stat.S_IRWXU)
            except FileNotFoundError:
                pass
        function(path)

    if destination.exists() or destination.is_symlink():
        shutil.rmtree(destination, onerror=remove_readonly)
    # Preserve repository symlinks.  The pinned Dyad tree intentionally has
    # CLAUDE.md -> AGENTS.md and .agents/skills -> ../.claude/skills; following
    # them during copy is unreliable on the shared filesystem and can raise a
    # spurious ENOENT before the baseline typecheck or Candidate build starts.
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "node_modules", "out"))
    # A frozen Candidate root is mode 0555. copytree preserves that mode on
    # the disposable runtime root, so creating its evaluator-owned
    # node_modules would otherwise fail before hidden product entry.
    for runtime_dir in [destination, *(
        path for path in destination.rglob("*")
        if path.is_dir() and not path.is_symlink()
    )]:
        runtime_dir.chmod(
            runtime_dir.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR
        )
    dependency = dependency_root / "node_modules"
    if dependency.is_dir():
        # The prepared dependency tree is root-owned and tsgo writes
        # node_modules/.tmp/*.tsbuildinfo during the normal typecheck.  A
        # symlink therefore turns an evaluator runtime check into EACCES.
        # Use a reflinked evaluator-owned copy so each attempt is writable
        # without duplicating the full dependency payload on disk.
        target = destination / "node_modules"
        target.mkdir()
        # dependency_root/node_modules is commonly a symlink to the pinned
        # prepared tree.  Copying that path with ``cp -a`` preserves the
        # symlink, recreating the root-owned write failure.  Resolve the source
        # and copy its contents so the attempt owns a real writable tree.
        subprocess.run(
            ["cp", "-a", "--reflink=auto", f"{dependency.resolve()}/.", str(target)],
            check=True,
        )
    nested = dependency_root / "testing/fake-llm-server/node_modules"
    if nested.is_dir():
        target = destination / "testing/fake-llm-server/node_modules"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["cp", "-a", "--reflink=auto", str(nested), str(target)],
            check=True,
        )
    return destination


def regular_tree(root: Path) -> bool:
    # Repository snapshots can legitimately contain relative symlinks.  The
    # pinned Dyad source even carries a deliberately broken documentation link
    # (``CLAUDE.md -> "AGENTS.md\\n"``).  Following it during freeze caused a
    # spurious ENOENT; rejecting every symlink made preserving the accepted
    # Candidate digest impossible.  Allow only relative links whose lexical
    # target stays inside the frozen tree.  Existence is not required because
    # a broken documentation link is still immutable repository metadata.
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.readlink()
        if target.is_absolute():
            return False
        parts: list[str] = []
        for part in (path.relative_to(root).parent / target).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    return False
                parts.pop()
            else:
                parts.append(part)
    return True


def readonly_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            # A symlink entry is controlled by its parent directory.  Once all
            # parents are mode 0555, an unprivileged evaluator cannot replace
            # it; chmod would affect the target rather than the link itself.
            continue
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def model_authored_artifact(value: Any) -> bool:
    """Require a captured model finish artifact before accepting ``valid``."""
    artifact = value.get("artifact") if isinstance(value, dict) else None
    provenance = value.get("artifact_provenance") if isinstance(value, dict) else None
    return (
        value.get("artifact_present") is True
        and isinstance(artifact, dict)
        and artifact.get("schema_version") == MODEL_ARTIFACT_SCHEMA
        and isinstance(provenance, dict)
        and provenance.get("artifact_owner") == MODEL_ARTIFACT_OWNER
        and provenance.get("producer_entry") == MODEL_ARTIFACT_PRODUCER
        and provenance.get("evaluator_synthesized") is False
        and provenance.get("exists") is True
    )


class Lifecycle:
    """Evaluator-owned state for one continuously connected Builder."""

    def __init__(self, *, run_dir: Path, workspace: Path, public_endpoint: str,
                 baseline: dict[str, Any], public_cases: tuple[str, ...] = DEV_CASES,
                 pilot_not_formal: bool = False, max_dev_rounds: int = 10,
                 result_judge_endpoint: str | None = None,
                 readiness_profile: str | None = None,
                 required_valid_rounds: int | None = None,
                 current_binding: dict[str, Any] | None = None) -> None:
        token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.run_dir = run_dir
        self.workspace = workspace
        self.baseline = baseline
        self.result_judge_endpoint = result_judge_endpoint
        self.token = token
        self.session_id = f"dyad-builder-session-{token[:16]}"
        # Stamped into the freeze so the sealed document itself attests the
        # binding this run was dispatched under, not the one in force later.
        self.current_binding = current_binding
        self.connection_id = f"dyad-builder-connection-{token[16:32]}"
        self.socket_directory = Path(tempfile.mkdtemp(prefix='dyad-builder-', dir='/tmp'))
        self.socket_path = self.socket_directory / 'controller.sock'
        self.native_observations = []
        self.feedback_deliveries = []
        self.accepted_deliveries = {}
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.lock = threading.RLock()
        self.records: list[dict[str, Any]] = []
        self.feedback_path: Path | None = None
        self.feedback_digest: str | None = None
        self.freeze_manifest: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.preflight_count = 0
        self.public_cases = tuple(public_cases)
        self.pilot_not_formal = bool(pilot_not_formal)
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        self.max_dev_rounds = max_dev_rounds
        if readiness_profile not in (None, READINESS_PROFILE):
            raise ValueError("unsupported readiness profile")
        if required_valid_rounds is not None and not 1 <= required_valid_rounds <= max_dev_rounds:
            raise ValueError("required_valid_rounds must be within max_dev_rounds")
        self.readiness_profile = readiness_profile
        self.required_valid_rounds = required_valid_rounds

    def event(self, name: str, **fields: Any) -> None:
        self.events.append({"event": name, "at": now(), "epoch_ns": time.time_ns(),
                            "builder_session_id": self.session_id, "builder_connection_id": self.connection_id, **fields})

    def _snapshot_workspace(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.workspace, destination, symlinks=False)
        return destination

    def preflight(self, trigger: str) -> dict[str, Any]:
        with self.lock:
            self.preflight_count += 1
            number = len(self.records) + 1
            attempt = self.run_dir / "preflight" / f"candidate_{number:03d}_attempt_{self.preflight_count:03d}"
            delivery = self._snapshot_workspace(attempt / "delivery")
            result: dict[str, Any] = {
                "schema_version": "dyad-builder-preflight-v1", "candidate_number": number,
                "trigger": trigger, "submission_consumed": False,
                "delivery_digest": tree_digest(delivery), "delivery_errors": validate_delivery(delivery),
                "baseline_typecheck": self.baseline,
            }
            if result["delivery_errors"]:
                result.update(classification="candidate_delivery_failure", ready_for_submission=False)
            else:
                candidate = attempt / "candidate"
                try:
                    build = materialize(ROOT / "input/repository", delivery, candidate)
                    missing = [path for path in ACCEPTANCE_REQUIRED_FILES if not (candidate / path).is_file()]
                    service_path = next((path for path in ACCEPTANCE_SERVICE_FILES if (candidate / path).is_file()), None)
                    result["materialization"] = build
                    result["acceptance_product_files"] = {
                        **{path: path not in missing for path in ACCEPTANCE_REQUIRED_FILES},
                        "service": service_path,
                        "service_candidates": list(ACCEPTANCE_SERVICE_FILES),
                    }
                    if service_path is None:
                        missing.append("one of " + ", ".join(ACCEPTANCE_SERVICE_FILES))
                    if missing:
                        raise ValueError("Acceptance product patch is incomplete: " + ", ".join(missing))
                    # Bracket the one step that could rewrite the materialized
                    # product, so immutability is measured rather than asserted.
                    source_before = product_source_digest(candidate)
                    typecheck = run_typecheck(candidate, dependency_root=ROOT / "input/repository",
                                              output=attempt / "typecheck", baseline=self.baseline)
                    result["candidate_typecheck"] = typecheck
                    source_after = product_source_digest(candidate)
                    result["readiness_source_immutability"] = {
                        "schema_version": "dyad-readiness-source-immutability-v1",
                        "measured_around": "candidate typecheck",
                        "before": {"digest": source_before},
                        "after": {"digest": source_after},
                        "unchanged": source_before == source_after,
                    }
                    result["candidate_digest"] = tree_digest(candidate)
                    result["candidate_path"] = str(candidate)
                    result["ready_for_submission"] = typecheck.get("ready_for_submission") is True
                    result["classification"] = typecheck["classification"]
                    if typecheck.get("failure_attribution"):
                        result["failure_attribution"] = typecheck["failure_attribution"]
                except ValueError as exc:
                    result.update(classification="candidate_materialization_failure", ready_for_submission=False,
                                  error_type=type(exc).__name__, error_detail=str(exc)[-1500:])
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    # A failed Git/copy/tool process alone does not prove Candidate fault.
                    result.update(classification="infrastructure_invalid", ready_for_submission=False,
                                  error_type=type(exc).__name__, error_detail=str(exc)[-1500:])
            path = attempt / "preflight.json"
            write_json(path, result)
            result["preflight_path"] = str(path)
            self.event("candidate_preflight", candidate_number=number, trigger=trigger,
                       classification=result["classification"], ready_for_submission=result["ready_for_submission"],
                       submission_consumed=False, preflight_path=str(path))
            return result

    def _run_public_case(self, candidate: Path, case_id: str, endpoint: str, destination: Path) -> dict[str, Any]:
        runtime = copy_runtime(candidate, destination / "runtime", ROOT / "input/repository")
        output = destination / "result.json"
        command = [sys.executable, str(ROOT / "evaluator/harness/run_lower_agent_case.py"),
                   "--repository", str(runtime), "--case", str(ROOT / "dev_cases" / case_id),
                   "--broker-endpoint", endpoint, "--output", str(output), "--mode", "headless"]
        execution = command_result(command, ROOT, timeout=720)
        try:
            value = read_json(output)
        except Exception as exc:
            value = {"classification": "infrastructure-invalid", "failure_class": "lower_runner_no_result", "error": str(exc)}
        value["runner_execution"] = execution
        value["case_id"] = case_id
        value = normalize_execution(value, candidate_digest=tree_digest(candidate),
            case_id=case_id, output=output, evidence_root=destination)
        value["semantic_feedback"] = score_public(value, output=destination / "semantic_score",
            evidence_root=destination, rubric=ROOT / "evaluator/result_rubric.md",
            broker_endpoint=self.result_judge_endpoint)
        write_json(destination / "scored_execution.json", value)
        return value

    @staticmethod
    def _public_valid(value: dict[str, Any]) -> bool:
        semantic = value.get("semantic_feedback") or {}
        return (semantic.get("contract_valid") is True and semantic.get("round_consumed") is True
                and semantic.get("classification") in {"scoreable", "candidate_zero"}
                and type(semantic.get("score")) is int and 0 <= semantic["score"] <= 100)

    # D19 (2026-09-20, formal 0920-fh-002): a public case the EVALUATOR voided.
    # The lower runner books it infrastructure-invalid and stamps
    # `attribution.owner = evaluator/provider` with
    # `candidate_behavior_evaluable = false` (evaluator/harness/
    # run_lower_agent_case.py:89-92), and no judge round was consumed, so it
    # holds no Candidate result of any kind -- neither a score nor a refusal.
    @staticmethod
    def _public_case_evaluator_voided(value: dict[str, Any]) -> bool:
        attribution = value.get("attribution") if isinstance(value.get("attribution"), dict) else {}
        semantic = value.get("semantic_feedback") or {}
        return (value.get("infra_valid") is False
                and attribution.get("owner") == "evaluator/provider"
                and attribution.get("candidate_behavior_evaluable") is False
                and semantic.get("round_consumed") is not True
                and semantic.get("score") is None)

    # A submission is accountable when at least one dev case produced a real
    # Candidate evaluation and every other case was voided by the evaluator
    # itself.  Anything else -- no evaluable case at all, or a case that failed
    # for a reason the evaluator cannot attribute to itself -- stays terminal
    # and keeps today's whole-submission void.
    @classmethod
    def _public_accountable(cls, dev: list) -> bool:
        return bool(dev) and any(cls._public_valid(item) for item in dev) and all(
            cls._public_valid(item) or cls._public_case_evaluator_voided(item) for item in dev)

    def _make_feedback(self, record: dict[str, Any]) -> tuple[Path, str]:
        public = [public_case_feedback(item) for item in record["dev"]]
        # A case the evaluator voided carries no score at all: averaging it in
        # as 0 would charge an evaluator/provider fault to the product.  It is
        # named instead, with its reason, so the Builder can see why its round
        # covered fewer cases than the inventory.
        scored = [item for item in record["dev"] if self._public_valid(item)]
        voided = [item for item in record["dev"] if not self._public_valid(item)]
        scores = [item["semantic_feedback"]["score"] for item in scored]
        mean = (sum(scores) / len(scores)) if scores else 0.0
        value = {
            "schema_version": "agentswe-edit-feedback/v1",
            "builder_session_id": self.session_id,
            "builder_connection_id": self.connection_id,
            "source_submission": record["submission_number"],
            "source_candidate_digest": record["candidate_digest"],
            "public_inventory": list(self.public_cases), "public_cases": public,
            "dev_mean": mean, "dev_passed": mean > 60,
            "dev_cases_scored": [item.get("case_id") for item in scored],
            "dev_cases_not_scored": [item.get("case_id") for item in voided],
            "dev_mean_basis": ("dev_mean is the mean over the dev cases whose evaluation completed; "
                               "a case the evaluator voided is listed in dev_cases_not_scored, carries "
                               "no score, and is not counted as a zero"),
            "summary": "Use the public Dyad Acceptance trajectory and artifact to revise the product patch.",
            "pilot_not_formal": self.pilot_not_formal,
            "oracle_included": False, "hidden_fixture_content_included": False,
            "native_suite_used_as_result": False,
        }
        path = self.run_dir / "feedback" / f"candidate_{record['submission_number']:03d}_feedback.json"
        write_json(path, value)
        return path, feedback_digest(path)

    def bind_native_thread(self):
        observation = observe_thread(self.run_dir, self.native_observations)
        if self.native_observations and observation['thread_id'] != self.session_id:
            raise RuntimeError('Builder native thread changed')
        if not self.native_observations:
            if self.records:
                raise RuntimeError('Accepted ledger lacks native thread binding')
            self.session_id = observation['thread_id']
        self.native_observations.append(observation)
        write_json(self.run_dir / 'native_thread_observations.json', self.native_observations)

    @staticmethod
    def public_preflight(value):
        public = {key: value.get(key) for key in ('classification', 'ready_for_submission',
            'submission_consumed', 'candidate_number', 'delivery_errors', 'acceptance_product_files')
            if key in value}
        typecheck = value.get('candidate_typecheck')
        if isinstance(typecheck, dict):
            public['candidate_typecheck'] = {key: typecheck.get(key) for key in
                ('exit_code', 'timed_out', 'stdout_tail', 'stderr_tail') if key in typecheck}
        def clean(item):
            if isinstance(item, dict): return {key: clean(v) for key, v in item.items()}
            if isinstance(item, list): return [clean(v) for v in item]
            if isinstance(item, str):
                return re.sub(r"/(?:home|data|run/secrets)/[^\s\"'<>]+", '[evaluator-private-path]', item)
            return item
        return clean(public)

    @staticmethod
    def write_attempt(path, value, *, initial=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        if initial:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(data); output.flush(); os.fsync(output.fileno())
        else:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temp = Path(output.name)
                output.write(data); output.flush(); os.fsync(output.fileno())
            os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)

    def accepted_payload(self, record, *, cached=False):
        return {'accepted': True, 'submission_number': record['submission_number'],
            'candidate_digest': record['candidate_digest'],
            # The delivery tree -- exactly the three submitted files -- is what
            # the readiness contract calls the candidate; the Builder has to be
            # able to quote it back in the next round's run_report.json.
            'delivery_digest': record['delivery_digest'],
            'builder_session_id': self.session_id,
            'feedback_digest': record['feedback_digest'],
            'feedback_digest_ack': record.get('feedback_digest_ack'),
            'feedback': read_json(Path(record['feedback_path'])),
            'submission_consumed': not cached, 'idempotent': cached}

    def native_attestation(self, exit_code, *, allow_interrupted: bool = False):
        records, errors = [], []
        for item in self.records:
            if tree_digest(Path(item['candidate_path'])) != item['candidate_digest']:
                errors.append('accepted Candidate changed after evaluation')
            if product_source_digest(Path(item['candidate_path'])) != item['product_source_digest']:
                errors.append('accepted product source changed after evaluation')
            records.append({**item, 'builder_session_id': item['session_id'],
                'build': {'candidate_repo_digest': item['product_source_digest']}})
        proof = verify_native(self.run_dir, records, self.feedback_deliveries, self.native_observations,
                              allow_interrupted=allow_interrupted)
        proof['errors'].extend(errors)
        proof['valid'] = bool(proof['valid'] and not errors and (exit_code == 0 or allow_interrupted))
        proof['builder_exit_code'] = exit_code
        proof['max_dev_rounds_interrupt_accepted'] = bool(allow_interrupted)
        write_json(self.run_dir / 'builder_native_attestation.json', proof)
        return proof

    def submit(self, acknowledged_feedback: str | None, public_endpoint: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.bind_native_thread()
            if self.freeze_manifest is not None:
                return 409, {'classification': 'lifecycle_frozen', 'submission_consumed': False}
            delivery_digest = tree_digest(self.workspace)
            if delivery_digest in self.accepted_deliveries:
                return 200, self.accepted_payload(self.accepted_deliveries[delivery_digest], cached=True)
            number = len(self.records) + 1
            if number > self.max_dev_rounds:
                return 409, {'classification': 'submission_limit_reached', 'submission_consumed': False}
            if ((number == 1 and acknowledged_feedback is not None) or
                    (number >= 2 and acknowledged_feedback != self.feedback_digest)):
                return 422, {'classification': 'feedback_protocol_failure', 'submission_consumed': False}
            preflight = self.preflight('submission_gate')
            if not preflight.get('ready_for_submission'):
                self.event('submission_not_consumed', candidate_number=number,
                    classification=preflight.get('classification'))
                return (503 if preflight.get("classification") == "infrastructure_invalid" else 422), self.public_preflight(preflight)
            candidate = Path(str(preflight['candidate_path']))
            digest = str(preflight['candidate_digest'])
            product_digest = product_source_digest(candidate)
            for prior in self.records:
                if product_digest == prior['product_source_digest']:
                    self.accepted_deliveries[delivery_digest] = prior
                    return 200, self.accepted_payload(prior, cached=True)
            attempt_file = self.run_dir / 'public_attempts' / (product_digest + '.json')
            if attempt_file.exists():
                previous = read_json(attempt_file)
                if previous.get('product_source_digest') != product_digest:
                    raise RuntimeError('public attempt identity mismatch')
                # D17 mirror (2026-09-20, formal 0920-fh-001): an attempt the EVALUATOR voided
                # (state not_evaluable: a public case booked infrastructure-invalid, e.g. an
                # upstream stream that died mid-response) holds no Candidate result, so
                # evaluating the same product again is not a replay. The voided evidence is
                # archived beside the attempt file, never overwritten; at most
                # PUBLIC_REEVALUATIONS re-evaluations per product.
                voided = sorted(attempt_file.parent.glob(product_digest + '.voided-*.json'))
                if previous.get('state') == 'not_evaluable' and len(voided) < PUBLIC_REEVALUATIONS:
                    attempt_file.rename(attempt_file.with_name(product_digest + '.voided-%03d.json' % (len(voided) + 1)))
                    self.event('public_attempt_reevaluated', candidate_number=number,
                        product_source_digest=product_digest, voided_attempts=len(voided) + 1)
                else:
                    return 503, {'classification': 'public_infrastructure_failure',
                        'submission_consumed': False, 'retry_same_candidate': False,
                        'reason': 'The earlier attempt for this exact product is preserved without replay; submit a revised product.',
                        'public_cases': [public_case_feedback(item) for item in previous.get('dev', [])]}
            self.write_attempt(attempt_file, {'candidate_digest': digest, 'product_source_digest': product_digest, 'state': 'reserved_before_dev', 'started_at': now()}, initial=True)
            public_dir = self.run_dir / 'lifecycle' / ('product_' + digest) / 'public'
            dev = []
            for case_id in self.public_cases:
                item = self._run_public_case(candidate, case_id, public_endpoint, public_dir / case_id)
                dev.append(item)
                self.write_attempt(attempt_file, {'candidate_digest': digest, 'product_source_digest': product_digest, 'state': 'in_progress', 'dev': dev})
            # D19: an evaluator/provider fault on ONE dev case voids THAT case's
            # evaluation, not the Candidate's whole submission.  With ~150-200
            # lower-agent calls per case and a ~1-2% per-call provider failure
            # rate, an all-or-nothing gate over a 2-case dev set made voiding
            # the common outcome: 5 of 7 submissions were lost that way in
            # 0920-fh-002 and the Builder used 2 of its 5 rounds.  The voided
            # case is excluded from dev_mean (never scored 0), named in the
            # feedback with its reason, and its evidence is preserved.
            voided_cases = [item for item in dev if not self._public_valid(item)]
            if voided_cases and not self._public_accountable(dev):
                self.write_attempt(attempt_file, {'candidate_digest': digest, 'product_source_digest': product_digest, 'state': 'not_evaluable', 'dev': dev})
                self.event('submission_not_consumed', candidate_number=number, classification='public_infrastructure_failure')
                return 503, {'classification': 'public_infrastructure_failure', 'submission_consumed': False,
                    'retry_same_candidate': True, 'reason': 'The public evaluation was voided by an evaluator-side fault; its evidence is preserved and this exact product may be resubmitted unchanged.',
                    'public_cases': [public_case_feedback(item) for item in dev]}
            if voided_cases:
                self.event('public_case_evaluation_voided', candidate_number=number,
                    product_source_digest=product_digest,
                    voided_cases=[item.get('case_id') for item in voided_cases],
                    scored_cases=[item.get('case_id') for item in dev if self._public_valid(item)])
            record = {'submission_number': number, 'candidate_digest': digest, 'product_source_digest': product_digest,
                'candidate_path': str(candidate), 'session_id': self.session_id,
                'connection_id': self.connection_id, 'dev': dev,
                'voided_dev_cases': [item.get('case_id') for item in voided_cases],
                'delivery_digest': delivery_digest, 'feedback_digest_ack': acknowledged_feedback,
                'accepted_at': now(), 'submission_consumed': True, 'pilot_not_formal': self.pilot_not_formal}
            if not post_freeze_superseded(self, record):
                self.records.append(record)
            self.feedback_path, self.feedback_digest = self._make_feedback(record)
            record.update(feedback_path=str(self.feedback_path), feedback_digest=self.feedback_digest)
            self.accepted_deliveries[delivery_digest] = record
            self.event('submission_accepted', candidate_number=number, candidate_digest=digest,
                feedback_digest_ack=acknowledged_feedback, submission_consumed=True)
            self._write_records()
            self.write_attempt(attempt_file, {'candidate_digest': digest, 'product_source_digest': product_digest, 'state': 'accepted', 'record': record})
            return 200, self.accepted_payload(record)

    def _write_records(self) -> None:
        write_json(self.run_dir / "lifecycle" / "dev_lifecycle.json", self.records)

    def _freeze(self, candidate: Path) -> None:
        frozen = self.run_dir / "lifecycle" / "frozen_candidate"
        document = self.run_dir / "lifecycle" / "freeze_manifest.json"
        seal = document.with_suffix(".sha256")
        if any(path.exists() or path.is_symlink() for path in (frozen, document, seal)):
            raise RuntimeError('refusing to replace existing frozen evidence')
        records_path = document.parent / 'dev_lifecycle.json'
        if records_path.is_symlink() or json.loads(records_path.read_text()) != self.records:
            raise RuntimeError('accepted records differ from the original durable ledger')
        accepted_modes = modes_digest(candidate)
        shutil.copytree(candidate, frozen, symlinks=True)
        if not regular_tree(frozen):
            raise RuntimeError("frozen Candidate contains an escaping symlink")
        digest = tree_digest(frozen)
        if digest != self.records[-1]["candidate_digest"]:
            raise RuntimeError("freeze copy changed the accepted Candidate digest")
        self.freeze_manifest = {
            "schema_version": "dyad-agentloop-freeze-v3",
            # A formal freeze is not bound to the readiness profile; only the
            # readiness consumers (evaluator/readiness_smoke.py:115,
            # evaluator/readiness_bundle.py:134) read this field, and both
            # require it to equal the profile constant, which is exactly what
            # self.readiness_profile is on a readiness run.
            "readiness_profile": self.readiness_profile,
            "candidate_path": str(frozen.resolve()),
            "hidden_only_after_freeze": True,
            "accepted_candidate_path": str(candidate.resolve()),
            "accepted_modes_sha256": accepted_modes,
            "accepted_history_sha256": evidence_sha256(records_path),
            "public_cases": list(self.public_cases),
            "candidate_digest": digest,
            "source_submission": self.records[-1]["submission_number"],
            "accepted_submission_count": len(self.records),
            "max_dev_rounds": self.max_dev_rounds,
            "accepted_candidate_digests": [item["candidate_digest"] for item in self.records],
            "source_submission_id": f"candidate-{self.records[-1]['submission_number']:03d}",
            "builder_session_id": self.session_id,
            "builder_connection_id": self.connection_id,
            "feedback_digest": self.feedback_digest,
            "feedback_consumed": bool(self.records) and all(
                isinstance(item.get("feedback_digest"), str)
                and (index == 0 or item.get("feedback_digest_ack") == self.records[index - 1].get("feedback_digest"))
                for index, item in enumerate(self.records)
            ),
            "feedback_chain_complete": bool(self.records) and all(
                isinstance(item.get("feedback_digest"), str)
                and (index == 0 or item.get("feedback_digest_ack") == self.records[index - 1].get("feedback_digest"))
                for index, item in enumerate(self.records)
            ),
            "frozen_at": now(), "hidden_started_at": None,
            "readonly": False,
            "pilot_not_formal": self.pilot_not_formal,
            "current_binding": self.current_binding,
            "delivery_candidate_digest": self.records[-1]["delivery_digest"],
            "submission_sha256": {
                name: evidence_sha256(Path(self.records[-1]["candidate_path"]).parent / "delivery" / name)
                for name in ("solution.patch", "edit_report.json", "run_report.json")
            } if self.readiness_profile else None,
        }
        readonly_tree(frozen)
        self.freeze_manifest["readonly"] = True
        self.freeze_manifest["candidate_digest_after_readonly"] = tree_digest(frozen)
        if self.freeze_manifest["candidate_digest_after_readonly"] != digest:
            raise RuntimeError("readonly freeze changed Candidate digest")
        write_evidence_once(document, self.freeze_manifest)
        write_evidence_once(seal, evidence_sha256(document) + "\n")
        records_path.chmod(0o444)
        validate_freeze(self.freeze_manifest, self.run_dir)

    def status(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            # The identity the contract means is the native thread, and it is
            # only observable once the Builder's rollout exists. Binding on read
            # keeps --status from handing out the pre-binding placeholder, which
            # a Builder copies into run_report.json inside the hashed submission
            # tree and can never correct afterwards.
            try:
                self.bind_native_thread()
            except Exception as exc:
                return 200, {'builder_session_id': None, 'builder_session_id_pending': True,
                    'reason': 'native Builder thread is not observable yet: '
                              + type(exc).__name__ + '; call --status again before submitting',
                    'accepted_submissions': len(self.records),
                    'max_dev_rounds': self.max_dev_rounds,
                    'frozen': self.freeze_manifest is not None}
            return 200, {'builder_session_id': self.session_id,
                'accepted_submissions': len(self.records), 'max_dev_rounds': self.max_dev_rounds,
                'frozen': self.freeze_manifest is not None,
                **(self.accepted_payload(self.records[-1], cached=True) if self.records else {})}

    def witness(self, builder_exit_code: int | None = None) -> dict[str, Any]:
        value = {
            "schema_version": "agentswe-builder-session-witness/v1",
            "model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT,
            "single_connection": True, "transport": "evaluator-owned-single-session",
            "session_id": self.session_id, "connection_id": self.connection_id,
            "builder_exit_code": builder_exit_code,
            "feedback_consumed": bool(self.records) and all(
                item["submission_number"] == 1 or item.get("feedback_digest_ack") for item in self.records
            ),
            "feedback_digest": self.feedback_digest,
            "submissions": [
                {"submission_number": item["submission_number"], "session_id": item["session_id"],
                 "connection_id": item["connection_id"], "candidate_digest": item["candidate_digest"],
                 **({"feedback_digest": item["feedback_digest_ack"]} if item["submission_number"] >= 2 else {})}
                for item in self.records
            ],
        }
        write_json(self.run_dir / "builder_session_witness.json", value)
        return value

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.socket_path.unlink(missing_ok=True)
        self.socket_directory.rmdir()


def start_submission_server(lifecycle: Lifecycle, public_endpoint: str) -> None:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                request = json.loads(self.rfile.readline(1 << 20))
                if not isinstance(request, dict) or request.get("token") != lifecycle.token:
                    status, payload = 401, {"classification": "controller_auth_failure"}
                elif request.get("action") == "validate":
                    payload = lifecycle.public_preflight(lifecycle.preflight("builder_request"))
                    status = 200 if payload.get("ready_for_submission") else 422
                elif request.get("action") == "submit":
                    status, payload = lifecycle.submit(request.get("feedback_digest"), public_endpoint)
                elif request.get("action") == "status":
                    status, payload = lifecycle.status()
                else:
                    status, payload = 400, {"classification": "controller_protocol_failure"}
            except Exception as exc:
                status, payload = 500, {"classification": "evaluator_controller_failure",
                                        "error_type": type(exc).__name__,
                                        "error_detail": "Controller failed; private execution evidence is retained."}
            self.wfile.write(json.dumps({"status": status, "payload": payload}, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()
            if status == 200 and payload.get('feedback_digest'):
                with lifecycle.lock:
                    receipt = {'candidate_number': payload['submission_number'],
                        'feedback_digest': payload['feedback_digest'], 'builder_session_id': lifecycle.session_id,
                        'payload_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
                    lifecycle.feedback_deliveries.append(receipt)
                    write_json(lifecycle.run_dir / 'native_feedback_deliveries.json', lifecycle.feedback_deliveries)
                    lifecycle.event('feedback_delivered', feedback_digest=payload['feedback_digest'])

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    lifecycle.socket_path.unlink(missing_ok=True)
    lifecycle.server = Server(str(lifecycle.socket_path), Handler)
    lifecycle.socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    threading.Thread(target=lifecycle.server.serve_forever, daemon=True).start()


def write_builder_helpers(task: Path) -> None:
    script = '''#!/usr/bin/env python3
import argparse
import json
import os
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--wait", action="store_true")
parser.add_argument("--validate", action="store_true")
parser.add_argument("--status", action="store_true")
parser.add_argument("--feedback-digest")
args = parser.parse_args()
action = "status" if args.status else "validate" if args.validate or os.path.basename(__file__) == "validate_dev_candidate" else "submit"
request = {"token": os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"], "action": action}
if args.feedback_digest is not None:
    request["feedback_digest"] = args.feedback_digest
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
    sock.connect(os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"])
    sock.sendall((json.dumps(request) + "\\n").encode())
    response = json.loads(sock.makefile().readline())
print(json.dumps(response, ensure_ascii=False, indent=2))
raise SystemExit(0 if response.get("status") == 200 else 1)
'''
    for name in ("validate_dev_candidate", "submit_dev_candidate"):
        path = task / "environment" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def stage_builder_package(run_dir: Path, public_cases: tuple[str, ...] = DEV_CASES) -> tuple[Path, dict[str, Any]]:
    package = run_dir / "builder_package"
    if package.exists():
        raise RuntimeError('refusing to replace an existing Builder package')
    shutil.copytree(ROOT / "input", package / "input", symlinks=False,
                    ignore=shutil.ignore_patterns("repository"))
    # The Builder must receive the pinned product repository as part of its
    # public package.  The previous staging code intentionally excluded the
    # repository from the broad input copy but forgot to add it back, leaving
    # the Builder with only input/*.md and dev cases and causing a mount-level
    # failure before Candidate 1 could be prepared.
    # Preserve repository links as links while staging.  The pinned snapshot
    # contains a deliberately relative CLAUDE.md -> AGENTS.md link whose
    # target is not visible from the host staging path; following links makes
    # copytree fail before Harbor can start the Builder.  Runtime materializers
    # handle dependency links separately, while the Builder only needs the
    # source snapshot and can safely inspect the link metadata.
    shutil.copytree(ROOT / "input" / "repository", package / "input" / "repository",
                    symlinks=True)
    staged_repository = package / "input" / "repository"
    # Never pass host-absolute links into the Builder package.  The pinned
    # snapshot uses absolute links only for evaluator-prepared dependencies;
    # materialize those approved trees at the run-local package path and fail
    # closed on every other absolute target.
    for link in list(staged_repository.rglob("*")):
        if not link.is_symlink():
            continue
        target = link.readlink()
        if not target.is_absolute():
            continue
        if link.name != "node_modules":
            raise RuntimeError(f"staged repository contains an unapproved absolute symlink: {link} -> {target}")
        resolved = target.resolve()
        if not resolved.is_dir():
            raise RuntimeError(f"approved dependency symlink target is missing: {target}")
        link.unlink()
        shutil.copytree(resolved, link, symlinks=True)
    for link in staged_repository.rglob("*"):
        if link.is_symlink() and link.readlink().is_absolute():
            raise RuntimeError(f"staged repository still contains an absolute symlink: {link}")
    (package / "dev_cases").mkdir()
    # The reduced pilot still needs the public harness entrypoints and its
    # selected public assets.  Copy only public support files; do not expose
    # hidden cases, evaluator code, or the unselected dev-case directories.
    for name in ("agentloop_inventory.json", "public_harness.py", "run_dev_case.py"):
        shutil.copy2(ROOT / "dev_cases" / name, package / "dev_cases" / name)
    assets = ROOT / "dev_cases" / "assets"
    staged_assets = package / "dev_cases" / "assets"
    staged_assets.mkdir()
    for name in ("public_probe.integration.test.ts", "acceptance-benchmark.js", *[f"{case_id}.json" for case_id in public_cases]):
        source_asset = assets / name
        if source_asset.is_file():
            shutil.copy2(source_asset, staged_assets / name)
    for case_id in public_cases:
        shutil.copytree(ROOT / "dev_cases" / case_id, package / "dev_cases" / case_id,
                        symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = {"root": str(package), "visible": ["input/", "dev_cases/agentloop_inventory.json", "dev_cases/public_harness.py", "dev_cases/run_dev_case.py", "dev_cases/assets/", *[f"dev_cases/{case_id}/" for case_id in public_cases]],
                "hidden_oracle_mounted": False, "evaluator_source_mounted": False,
                "credential_mounted": False}
    write_json(run_dir / "builder_visibility_manifest.json", manifest)
    return package, manifest


def builder_config(*, run_dir: Path, package: Path, workspace: Path,
                   lifecycle: Lifecycle, provider_config: Path, image: str,
                   broker_endpoint: str) -> Path:
    task = run_dir / "builder_task"
    # Formal must stop inside the outer --builder-timeout; the pilot/readiness
    # branch keeps the historical cap it has always been launched with.
    builder_task_timeout_seconds = (
        PILOT_BUILDER_TASK_TIMEOUT_SECONDS if lifecycle.pilot_not_formal
        else GENERATED_BUILDER_TASK_TIMEOUT_SECONDS
    )
    (task / "environment").mkdir(parents=True, exist_ok=True)
    (task / "tests").mkdir(parents=True, exist_ok=True)
    worktree = run_dir / 'builder_workspace/worktree'
    shutil.copytree(package / 'input/repository', worktree, symlinks=True)
    persistent_home = run_dir / 'builder_workspace/codex_home'
    persistent_home.mkdir(parents=True, exist_ok=False)
    resource_check = task / 'environment/builder_resource_check.py'
    shutil.copyfile(ROOT / 'harbor/builder_resource_check.py', resource_check)
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n'
        '[task]\nname = "local/dyad-acceptance-agentloop-builder"\n'
        'version = "1.0.0"\ndescription = "Dyad typed Acceptance up-to-ten-round Edit lifecycle"\n'
        'artifacts = [{ source = "/workspace/submission", destination = "builder_submission" }]\n'
        f'[agent]\ntimeout_sec = {float(builder_task_timeout_seconds)}\nuser = "root"\nnetwork_mode = "public"\n'
        '[verifier]\ntimeout_sec = 300.0\nuser = "root"\nnetwork_mode = "no-network"\n'
        '[environment]\ndocker_image = "' + image + '"\nnetwork_mode = "public"\n'
        'build_timeout_sec = 900.0\ncpus = 8\nmemory_mb = 16384\nstorage_mb = 32768\nworkdir = "/workspace"\n'
        '[environment.healthcheck]\ncommand = "python3 /usr/local/lib/agentswe-builder-resource-check.py"\nretries = 1\ntimeout_sec = 10.0\n',
        encoding="utf-8")
    # The default text below is the pre-v2 lifecycle: it offers to finish after
    # one accepted submission and allows up to ten, both of which contradict a
    # profile that requires exactly two. It also names none of the metadata the
    # profile makes mandatory, which is why no dyad delivery has ever been
    # usable as readiness evidence. Selected here rather than by an early
    # return: everything after this line -- helpers, compose, job config -- is
    # needed by both paths.
    readiness_instruction = """# Dyad Acceptance Edit Builder — v2 readiness

You are one uninterrupted Builder session. Read only /builder-package/input and
/builder-package/dev_cases; hidden cases and evaluator source are unavailable.
Edit the writable product copy /workspace/worktree and use /workspace/submission
to deliver exactly solution.patch, edit_report.json, and run_report.json.

Each case gives your delivered agent a 500 s wall budget for the whole run
(model calls included). The hidden smoke case must reach a final decision and
persist its artifact well inside that budget: bound the number of model steps
and stop looping once the acceptance flow is complete; a case that is still
calling the model when the budget ends is a Candidate failure with no artifact.

Run validate_dev_candidate before each submission, then submit with
submit_dev_candidate. Complete exactly two accepted rounds and stop. After the
first accepted submission the evaluator returns feedback and its digest; make
one genuine revision with a different product digest and submit the second with
--feedback-digest <exact-latest-digest>.

run_report.json must carry four further fields in both rounds. Round 1:
builder_session_id copied verbatim from the builder_session_id that
`submit_dev_candidate --status` returns -- run it once before your first
submission and copy that exact string; do not invent one and do not use any
identifier from your environment. If that response has builder_session_id null
and builder_session_id_pending true, your session is not observable yet: wait a
moment and call --status again, and never write null or a placeholder. submission_number 1,
revision_of_candidate_digest null, feedback_digest null. Round 2: the same
builder_session_id, submission_number 2, revision_of_candidate_digest set to the
delivery_digest the evaluator returned for the accepted round 1 (the field named
delivery_digest in that response, not candidate_digest), and feedback_digest set
to the exact feedback digest you acknowledged.

Round 2's edit_report.json must also carry feedback_response: a string of prose
that you write, explaining how this revision answers the feedback. It is your
explanation, not a copy of the evaluator's feedback object.

The evaluator freezes the second accepted submission, then a new independent
hidden broker with zero calls runs the hidden suite. Candidate and lower
evaluation use only the broker-only-placeholder token. This run is readiness
evidence and publishes no formal score.
"""
    (task / "instruction.md").write_text(
        readiness_instruction if lifecycle.readiness_profile else
        """# Dyad Acceptance Edit Builder — accepted-submission lifecycle

You are one uninterrupted upper Builder session. Read only
/builder-package/input and /builder-package/dev_cases; hidden cases and
evaluator source are unavailable. Read the full public input documents.
Edit the writable product copy /workspace/worktree and use /workspace/submission
to deliver exactly solution.patch, edit_report.json, and run_report.json.

Use validate_dev_candidate before each submission. Submit the first accepted
submission with submit_dev_candidate. After each accepted submission, the
evaluator returns feedback plus a digest. Consume that exact feedback in this
same session. You may finish after the first accepted submission. If you choose
another submission, make a genuine revision with a different product digest and
submit it with --feedback-digest. You may make up
to 5 accepted submissions; infrastructure-invalid attempts do not consume a
slot. Do not stop merely because a dev mean exceeds 60. When the Builder exits,
the evaluator freezes the latest accepted submission. After that freeze, a new
independent hidden broker with zero calls and failures runs the hidden suite.
The native Builder uses its configured direct provider authentication. Candidate
and lower evaluation use only the broker-only-placeholder token. Submit only
the three required delivery files. Evaluation credentials and hidden data are
private to the evaluator. A failed or unknown attempt of the exact same product
is preserved without replay. Duplicate accepted deliveries return cached feedback.
""", encoding="utf-8")
    write_builder_helpers(task)
    (task / "tests" / "test.sh").write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    (task / "tests" / "test.sh").chmod(0o755)
    # Harbor validates CODEX_CONFIG_TOML_PATH on the host before the task
    # container starts, so the configured path must be the run-local host
    # absolute path. Mount the same path into the container.
    provider_host_path = provider_config.resolve()
    if not provider_host_path.is_absolute() or not provider_host_path.is_file():
        raise RuntimeError("Builder provider config must be an existing run-local host file")
    provider_target = str(provider_host_path)
    socket_target = "/run/agentswe/dyad-builder-controller.sock"
    write_json(task / "environment" / "docker-compose.yaml", {"services": {"main": {
        "command": ["sh", "-c", "mkdir -p /tmp/codex-home && exec sleep infinity"],
        "cpu_quota": 800000, "cpu_period": 100000,
        "volumes": [
            {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
            {"type": "bind", "source": str(persistent_home), "target": "/tmp/codex-home"},
            {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
            {"type": "bind", "source": str(package), "target": "/builder-package", "read_only": True},
            {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
            {"type": "bind", "source": str(lifecycle.socket_path), "target": socket_target},
            {"type": "bind", "source": str(task / "environment" / "validate_dev_candidate"), "target": "/usr/local/bin/validate_dev_candidate", "read_only": True},
            {"type": "bind", "source": str(task / "environment" / "submit_dev_candidate"), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
            {"type": "bind", "source": str(provider_host_path), "target": provider_target, "read_only": True},
        ],
        "environment": {"AGENTSWE_DEV_CONTROLLER_SOCKET": socket_target,
                        "AGENTSWE_DEV_CONTROLLER_TOKEN": lifecycle.token,
                        "AGENTSWE_BUILDER_BROKER_TOKEN": PLACEHOLDER},
    }}})
    config = run_dir / "builder_job_config.json"
    write_json(config, {"job_name": f"dyad-acceptance-builder-{run_dir.name}",
                        "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1,
                        "n_concurrent_trials": 1, "quiet": True,
                        "retry": {"max_retries": 0},
                        "environment": {"type": "docker", "delete": True, "cpu_enforcement_policy": "limit"},
                        "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL,
                                    "env": {"CODEX_HOME": "/tmp/agentswe-codex-home",
                                            "CODEX_CONFIG_TOML_PATH": provider_target,
                                            "AGENTSWE_BUILDER_BROKER_TOKEN": PLACEHOLDER},
                                    "kwargs": {"reasoning_effort": BUILDER_EFFORT,
                                               "web_search": "live"}}],
                        "tasks": [{"path": str(task)}]})
    return config


def start_broker(*, name: str, script: Path, credential: Path, port: int,
                 image: str, provider_url: str, broker_kind: str = "lower",
                 ownership: dict[str, str] | None = None,
                 ownership_key: str | None = None, evidence_dir: Path | None = None,
                 defer_removal: bool = False) -> str:
    if broker_kind == "result_judge_xhigh":
        if evidence_dir is None or ownership is None or ownership_key is None:
            raise ValueError("Result requires owned evidence and container registration")
        instance = _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=evidence_dir / "result_judge.cid", upstream=provider_url,
            defer_removal=defer_removal)
        ownership[ownership_key] = instance.container_id
        return instance.endpoint
    if broker_kind not in {"lower", "builder_xhigh", "result_judge_xhigh"}:
        raise ValueError(f"unsupported broker kind: {broker_kind}")
    if broker_kind == "builder_xhigh":
        script = SHARED_XHIGH_BROKER
    provider_base = provider_url.removesuffix("/v1/responses").rstrip("/")
    command = ["docker", "run", "-d", "--rm", "--name", name,
               "--label", "agentswe.owner=dyad-formal-one-stop",
               "--network", "host", "-v", f"{script.resolve()}:/broker.py:ro",
               "-v", f"{credential.resolve()}:/run/secrets/provider.env:ro",
               "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
               "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", image,
               "python3", "/broker.py", "--credential-file", "/run/secrets/provider.env"]
    if broker_kind == "lower":
        if evidence_dir is None:
            raise ValueError("lower broker requires run-owned durable evidence")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        ledger = script.resolve().with_name("request_ledger.py")
        if not ledger.is_file():
            raise ValueError("lower broker request ledger is missing")
        image_position = command.index(image)
        command[image_position:image_position] = [
            "-v", f"{ledger}:/request_ledger.py:ro",
            "-v", f"{JUDGE_BROKER_SCRIPT.parent / 'responses_stream.py'}:/responses_stream.py:ro",
            "-v", f"{evidence_dir.resolve()}:/ledger:rw",
            "-e", "AGENTSWE_EVALUATOR_PROXY_URL=",
        ]
    if broker_kind == "builder_xhigh":
        command.extend([
            "--bind", "0.0.0.0", "--port", str(port),
            "--provider-url", provider_base + "/v1/responses",
            "--max-runtime-calls", "0", "--max-runtime-tokens", "0",
            "--role", "builder",
            "--reasoning-effort", "max",
        ])
    else:
        command.extend([
            # Dyad's product-specific medium broker accepts an upstream base
            # URL and appends /v1/responses itself.
            "--upstream", provider_base,
            "--bind", "127.0.0.1", "--port", str(port),
            "--stats-file", "/ledger/stats.json",
        ])
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("broker_container_start_failure: " + result.stderr[-1200:])
    container_id = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not container_id:
        raise RuntimeError("broker_container_start_failure: docker returned no container id")
    if ownership is not None and ownership_key is not None:
        ownership[ownership_key] = container_id
    endpoint = f"http://127.0.0.1:{port}/v1/responses"
    last_error = "no health response"
    for _ in range(60):
        try:
            health = broker_health(endpoint)
            if (health.get("ok") is True or health.get("status") == "ok"
                    or health.get("ready") is True):
                # The lower broker and the shared Builder broker expose
                # different health schemas; start_broker is used for both.
                return endpoint
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
    raise RuntimeError("broker_health_failure: " + last_error)


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


def remove_container(name: str, owned_id: str | None = None) -> dict[str, Any]:
    if not owned_id:
        return {"name": name, "owned_id": None, "ownership_proven": False,
                "cleanup_attempted": False, "absent_after_cleanup": False,
                "cleanup_error": "no current-run container id was captured"}
    remove_error = None
    inspect_error = None
    in_progress = False
    remove_stderr = None
    try:
        removed = subprocess.run(["docker", "rm", "-f", owned_id], text=True,
                                 capture_output=True, check=False)
        remove_exit_code = removed.returncode
        remove_stderr = (removed.stderr or "")[-500:] if removed.returncode else None
        in_progress = removal_in_progress(removed)
        if in_progress:
            await_daemon_removal(owned_id)
    except Exception as exc:
        remove_exit_code = None
        remove_error = f"{type(exc).__name__}: {exc}"
    try:
        inspected = subprocess.run(["docker", "inspect", owned_id], text=True,
                                   capture_output=True, check=False)
        inspect_exit_code = inspected.returncode
        detail = (inspected.stdout + "\n" + inspected.stderr).lower()
        if inspected.returncode == 0:
            absent_after_cleanup = False
        elif "no such object" in detail or "no such container" in detail:
            absent_after_cleanup = True
        else:
            absent_after_cleanup = False
            inspect_error = f"docker inspect failed with exit code {inspected.returncode}: {detail[-800:]}"
    except Exception as exc:
        inspect_exit_code = None
        inspect_error = f"{type(exc).__name__}: {exc}"
        absent_after_cleanup = False
    return {
        "name": name,
        "owned_id": owned_id,
        "ownership_proven": True,
        "cleanup_attempted": True,
        "remove_exit_code": remove_exit_code,
        "remove_error": remove_error,
        **({"remove_stderr": remove_stderr} if remove_stderr is not None else {}),
        **({"removal_in_progress_at_rm": True} if in_progress else {}),
        "inspect_exit_code": inspect_exit_code,
        "inspect_error": inspect_error,
        "absent_after_cleanup": absent_after_cleanup,
    }


def run_builder(harbor: Path, config: Path, run_dir: Path, timeout: int, *, lifecycle,
                credential, proxy='http://127.0.0.1:7890', provider_url='https://api.deepseek.com/v1') -> int:
    return run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
        harbor=harbor, timeout=timeout, proxy=proxy, provider_url=provider_url).returncode


def validate_hidden_dir(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_dir():
        return ["hidden case directory is missing"]
    resolved = path.resolve()
    if resolved == (ROOT / "test_cases").resolve() or ROOT in resolved.parents:
        return ["hidden cases must be evaluator-issued outside the sibling"]
    for case_id in HIDDEN_CASES:
        item = path / f"{case_id}.json"
        if not item.is_file() or item.is_symlink():
            errors.append(f"missing evaluator-issued {case_id}.json")
            continue
        try:
            value = read_json(item)
            if not any(isinstance(value.get(key), str) and value[key].strip()
                       for key in ("input", "prompt", "task", "input_markdown")):
                errors.append(f"{case_id}.json has no task input")
        except Exception as exc:
            errors.append(f"{case_id}.json invalid: {type(exc).__name__}")
    return errors


def validate_pilot_hidden_case(path: Path) -> list[str]:
    """Validate the one evaluator-issued hidden spec used by --pilot."""
    if not path.is_file() or path.is_symlink():
        return [f"missing evaluator-issued {path.name}"]
    try:
        value = read_json(path)
    except Exception as exc:
        return [f"{path.name} invalid: {type(exc).__name__}"]
    if not any(
        isinstance(value.get(key), str) and value[key].strip()
        for key in ("input", "prompt", "task", "input_markdown")
    ):
        return [f"{path.name} has no task input"]
    return []


def hidden_input(spec: Path, destination: Path) -> Path:
    value = read_json(spec)
    text = next((value[key] for key in ("input", "prompt", "task", "input_markdown")
                 if isinstance(value.get(key), str) and value[key].strip()), None)
    if not isinstance(text, str):
        raise ValueError(f"hidden spec has no task input: {spec.name}")
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "input.md"
    path.write_text(text, encoding="utf-8")
    return path


def hidden_result_real(value: dict[str, Any]) -> bool:
    """Accept real hidden behavior, including artifact-less Candidate failure."""
    if value.get('classification') in ('candidate_artifact_failure', 'candidate_timeout'):
        # A Candidate the evaluator's case deadline cut off is as real a hidden
        # behaviour as one that finished without an artifact: both are proved by
        # the same shared Candidate-zero verdict below, so neither should make
        # the attestation's all_cases_real marker read False.
        from execution_contract import classify_candidate_execution
        verdict = classify_candidate_execution(value, case_id=value.get('case_id'),
            candidate_digest=value.get('candidate_digest'))
        return verdict['classification'] == 'candidate_zero'
    classification = value.get("classification")
    if classification not in {"valid", "candidate_failure"}:
        return False
    if value.get("product_entry_observed") is not True:
        return False
    if int(value.get("broker_calls_delta", 0) or 0) <= 0:
        return False
    if int(value.get("broker_successful_calls_delta", 0) or 0) <= 0:
        return False
    return classification == "candidate_failure" or model_authored_artifact(value)


def execute_hidden(lifecycle: Lifecycle, endpoint: str, hidden_dir: Path,
                   dependency_root: Path, case_ids: tuple[str, ...] = HIDDEN_CASES,
                   pilot_not_formal: bool = False) -> dict[str, Any]:
    manifest = lifecycle.freeze_manifest
    if not isinstance(manifest, dict):
        raise RuntimeError("hidden execution requested before freeze")
    document, frozen = validate_freeze(manifest, lifecycle.run_dir)
    if (not case_ids or len(set(case_ids)) != len(case_ids)
            or tuple(case_id for case_id in HIDDEN_CASES if case_id in case_ids) != tuple(case_ids)):
        raise ValueError("hidden cases must be a nonempty canonical ordered subset")
    if tuple(case_ids) != HIDDEN_CASES and not pilot_not_formal:
        raise ValueError('a hidden subset must be explicitly non-formal')
    started = now()
    if timestamp(started) < timestamp(manifest["frozen_at"]):
        raise ValueError("hidden execution timestamp precedes freeze")
    # Reserve once before any lower execution; failure/unknown receipts stay put.
    hidden_root = lifecycle.run_dir / "hidden_after_freeze"
    hidden_root.mkdir(parents=True, exist_ok=False)
    fsync_directory(hidden_root.parent)
    write_evidence_once(hidden_root / "execution_intent.json", {
        "expected_cases": list(case_ids), "freeze_manifest_sha256": evidence_sha256(document),
        "candidate_digest": manifest["candidate_digest"], "started_at": started,
        "retry_same_candidate": False})
    before_suite = broker_stats(endpoint)
    before_runtime = before_suite.get("runtime") if isinstance(before_suite.get("runtime"), dict) else {}
    fresh = all(
        type(before_runtime.get(key)) is int and before_runtime[key] == 0
        for key in ("calls", "successful_calls", "failures", "provider_failures")
    )
    if not fresh:
        raise RuntimeError("hidden pilot/formal broker must be fresh with zero calls/failures")
    cases: dict[str, Any] = {}
    for case_id in case_ids:
        case_dir = lifecycle.run_dir / "hidden_after_freeze" / case_id
        runtime = copy_runtime(frozen, case_dir / "repository", dependency_root)
        input_path = hidden_input(hidden_dir / f"{case_id}.json", case_dir / "case")
        output = case_dir / "result.json"
        command = [sys.executable, str(ROOT / "evaluator/harness/run_lower_agent_case.py"),
                   "--repository", str(runtime), "--case", str(input_path),
                   "--case-id", case_id,
                   "--broker-endpoint", endpoint, "--output", str(output), "--mode", "headless"]
        case_started = now()
        digest_before = tree_digest(frozen)
        if timestamp(case_started) < timestamp(manifest["frozen_at"]) or digest_before != manifest["candidate_digest"]:
            raise ValueError("hidden case cannot start with invalid freeze identity/time")
        begin_path = case_dir / 'execution_started.json'
        write_evidence_once(begin_path, {'case_id': case_id, 'started_at': case_started,
            'freeze_manifest_sha256': evidence_sha256(document), 'command': command})
        execution = command_result(command, ROOT, timeout=720)
        runner_path = case_dir / 'runner_execution.json'
        write_evidence_once(runner_path, execution)
        timing = {"case_id": case_id, "started_at": case_started, "finished_at": now(),
                  "freeze_manifest_sha256": evidence_sha256(document),
                  "execution_started_sha256": evidence_sha256(begin_path),
                  "runner_execution_sha256": evidence_sha256(runner_path),
                  "frozen_digest_before": digest_before, "frozen_digest_after": tree_digest(frozen)}
        timing_path = case_dir / "execution_timing.json"
        write_evidence_once(timing_path, timing)
        try:
            result = read_json(output)
        except Exception as exc:
            result = {"classification": "infrastructure-invalid",
                      "failure_class": "hidden_result_unreadable", "error": str(exc)}
        result.update({"case_id": case_id, "runner_execution": execution,
                       "frozen_digest_before": manifest["candidate_digest"],
                       "runtime_candidate_digest": tree_digest(runtime),
                       "execution_timing_path": str(timing_path),
                       "execution_timing_sha256": evidence_sha256(timing_path),
                       "executed_after_freeze": timestamp(case_started) >= timestamp(manifest["frozen_at"])})
        result = normalize_execution(result, candidate_digest=manifest["candidate_digest"],
            case_id=case_id, output=output, evidence_root=case_dir)
        write_json(case_dir / "bound_execution.json", result)
        cases[case_id] = result
    after_suite = broker_stats(endpoint)
    attestation = {
        "schema_version": "dyad-agentloop-hidden-after-freeze-attestation-v2",
        "hidden_after_freeze": True, "hidden_started_at": started,
        "hidden_finished_at": now(),
        "case_ids": list(case_ids), "case_count": len(cases),
        "expected_cases": list(case_ids), "executed_cases": list(cases),
        "freeze_manifest": str(document), "freeze_manifest_sha256": evidence_sha256(document),
        "all_cases_started_after_freeze": bool(cases) and all(
            item.get("executed_after_freeze") is True for item in cases.values()),
        "pilot_not_formal": pilot_not_formal,
        "frozen_candidate_digest_before": manifest["candidate_digest"],
        "frozen_candidate_digest_after": tree_digest(frozen),
        "frozen_digest_stable": tree_digest(frozen) == manifest["candidate_digest"],
        "broker_before_suite": before_suite, "broker_after_suite": after_suite,
        "fresh_broker_zero_call_before_hidden": fresh,
        "fresh_broker_initial_runtime": before_runtime,
        "cases": cases,
        "all_cases_real_and_behavior_or_candidate_failure": all(hidden_result_real(item) for item in cases.values()),
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "result_axis": "N/A" if pilot_not_formal else None,
        "code_axis": "N/A" if pilot_not_formal else None,
    }
    write_json(lifecycle.run_dir / "hidden-after-freeze-attestation.json", attestation)
    return attestation


def builder_attestation(lifecycle: Lifecycle, exit_code: int,
                        started_ns: int, finished_ns: int) -> dict[str, Any]:
    records = lifecycle.records
    interrupted_at_max_rounds = formal_max_rounds_interrupt(lifecycle, exit_code)
    native = lifecycle.native_attestation(exit_code, allow_interrupted=interrupted_at_max_rounds)
    value: dict[str, Any] = {
        "schema_version": "dyad-agentloop-builder-session-attestation-v1",
        "builder_session_id": lifecycle.session_id,
        "builder_connection_id": lifecycle.connection_id,
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "builder_exit_code": exit_code,
        "builder_timed_out": exit_code == 124,
        "single_harbor_invocation": True,
        "single_continuous_session": native['valid'],
        "native_attestation": native,
        "started_epoch_ns": started_ns,
        "finished_epoch_ns": finished_ns,
        "events": lifecycle.events,
        "accepted_candidate_count": len(records),
        "max_dev_rounds": lifecycle.max_dev_rounds,
        "required_valid_rounds": lifecycle.required_valid_rounds,
        "readiness_profile": lifecycle.readiness_profile,
        "accepted_submissions_public_dev_complete": bool(records) and all(
            len(record.get("dev", [])) == len(lifecycle.public_cases)
            and lifecycle._public_accountable(record.get("dev", []))
            for record in records
        ),
        "feedback_delivered": bool(records) and bool(lifecycle.feedback_digest),
        "feedback_digest": lifecycle.feedback_digest,
        "feedback_consumed": bool(records) and all(
            item["submission_number"] == 1 or item.get("feedback_digest_ack") for item in records
        ),
        "distinct_candidate_digests": bool(records) and len({item.get("candidate_digest") for item in records}) == len(records),
        "freeze": lifecycle.freeze_manifest is not None,
        "preflight_attempts": lifecycle.preflight_count,
    }
    value["builder_interrupted_at_max_dev_rounds"] = interrupted_at_max_rounds
    value["complete"] = all((
        exit_code == 0 or interrupted_at_max_rounds, native['valid'],
        (len(records) == lifecycle.required_valid_rounds if lifecycle.required_valid_rounds is not None
         else 1 <= len(records) <= lifecycle.max_dev_rounds),
        value["accepted_submissions_public_dev_complete"],
        value["feedback_delivered"], value["feedback_consumed"],
        value["distinct_candidate_digests"], value["freeze"],
    ))
    lifecycle.witness(exit_code)
    write_json(lifecycle.run_dir / "builder_session_attestation.json", value)
    return value


def baseline_typecheck(run_dir: Path) -> dict[str, Any]:
    return run_typecheck(ROOT / "input/repository", dependency_root=ROOT / "input/repository",
                         output=run_dir / "baseline_typecheck")


def save_stats(run_dir: Path, label: str, endpoint: str | None) -> dict[str, Any]:
    if endpoint is None:
        write_json(run_dir / f"{label}_broker_stats.json", {"status": "not_started"})
        return {"role": label, "started": False, "stats_saved": False, "endpoint": None}
    try:
        write_json(run_dir / f"{label}_broker_stats.json", broker_stats(endpoint))
        return {"role": label, "started": True, "stats_saved": True, "endpoint": endpoint}
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[-800:]}"
        write_json(run_dir / f"{label}_broker_stats_error.json", {
            "classification": "broker_stats_collection_failure",
            "error_type": type(exc).__name__, "error_detail": str(exc)[-800:],
        })
        return {"role": label, "started": True, "stats_saved": False,
                "endpoint": endpoint, "error": error}


def offline_self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        run_dir = Path(raw)
        workspace = run_dir / "workspace"
        workspace.mkdir()
        lifecycle = Lifecycle(run_dir=run_dir, workspace=workspace,
                              public_endpoint="http://127.0.0.1:1/v1/responses",
                              baseline={"required_pass": False, "classification": "static"})
        try:
            assert LOWER_MODEL == "deepseek-flash"
            assert BUILDER_EFFORT == "max" and LOWER_EFFORT == "high"
            assert PLACEHOLDER == "broker-only-placeholder"
            assert DEV_CASES == ("dev_001", "dev_002")
            assert HIDDEN_CASES == tuple(f"test_{i:03d}" for i in range(1, 7))
            assert lifecycle.session_id != lifecycle.connection_id
            lock = (ROOT / "protocol_lock.json").read_text(encoding="utf-8")
            assert '"reasoning_effort": "max"' in lock
            assert '"reasoning_effort": "high"' in lock
            assert not sensitive_text(ROOT / "protocol_lock.json")
            assert validate_hidden_dir(run_dir / "missing")
            assert lifecycle.witness(0)["feedback_consumed"] is False
            fresh_stats = {
                "schema_version": "agentswe-judge-broker-stats/v1",
                "protocol": {
                    "model": "deepseek-flash", "reasoning_effort": "max",
                    "max_output_tokens": 64000, "inner_retries": 0,
                    "max_upstream_attempts_per_transport": 1,
                    "absolute_deadline_seconds_max": 900, "redirects_allowed": False,
                    "response_transport_modes": ["stream", "nonstream"],
                    "downstream_response_format": "terminal_json",
                    "connect_timeout_seconds_max": 30,
                    "upstream_attempt_marker": "before_first_http_bytes_after_connection",
                },
                "runtime": {key: 0 for key in (
                    "calls", "completed_calls", "successful_calls", "failures",
                    "upstream_attempts", "tokens", "usage_unknown_calls", "in_flight_calls",
                )},
            }
            assert fresh_xhigh_result_judge(fresh_stats)
            fresh_stats["runtime"]["upstream_attempts"] = 1
            assert not fresh_xhigh_result_judge(fresh_stats)
            reject_external_result_judge_endpoint(formal=False, endpoint="ignored-by-pilot")
            try:
                reject_external_result_judge_endpoint(
                    formal=True, endpoint="http://external.invalid/v1/responses"
                )
            except ValueError:
                pass
            else:
                raise AssertionError("formal mode accepted an externally owned Result-judge endpoint")
        finally:
            lifecycle.close()
    return {
        "self_test": "PASS", "network_calls": 0, "docker_started": False,
        "formal_execution_started": False,
        "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT},
        "lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT},
        "public_cases": list(DEV_CASES), "hidden_cases": list(HIDDEN_CASES),
        "candidate_credential": PLACEHOLDER,
        "formal_result_claimed": False, "code_score_claimed": False,
    }


def credential_has_key(path: Path) -> bool:
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key.strip() in PROVIDER_KEYS and value.strip().strip("'\""):
                return True
    except (OSError, UnicodeError):
        return False
    return False


def write_static_artifacts(output: Path) -> dict[str, Any]:
    """Write an explicit offline audit record; this path never starts network work."""
    output.mkdir(parents=True, exist_ok=True)
    lock_path = ROOT / "protocol_lock.json"
    lock = read_json(lock_path)
    lock["static_audit"] = {
        "generated_at": now(),
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
    }
    write_json(output / "protocol_lock.json", lock)
    summary = {
        "schema_version": "dyad-agentloop-static-summary-v1",
        "status": "static_only",
        "classification": "offline_static_audit",
        "generated_at": now(),
        "output_dir": str(output),
        "protocol_lock": "protocol_lock.json",
        "checks": {
            "builder_model": BUILDER_MODEL,
            "builder_reasoning_effort": BUILDER_EFFORT,
            "lower_model": LOWER_MODEL,
            "lower_reasoning_effort": LOWER_EFFORT,
            "candidate_credential": PLACEHOLDER,
            "public_cases": list(DEV_CASES),
            "hidden_cases": list(HIDDEN_CASES),
            "hidden_after_freeze": True,
            "same_builder_session": True,
            "distinct_candidate_digest_required": True,
            "feedback_digest_required": True,
            "fresh_hidden_broker_required": True,
            "acceptance_product_files": {
                "required": list(ACCEPTANCE_REQUIRED_FILES),
                "service_candidates": list(ACCEPTANCE_SERVICE_FILES),
            },
        },
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "result_axis": "N/A",
        "code_axis": "N/A",
    }
    write_json(output / "summary.json", summary)
    return summary


def pilot_dry_run(output: Path, *, readiness_profile: str | None = None, max_dev_rounds: int = 10, n_concurrent: int = 1) -> int:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"pilot dry-run directory is not empty: {output}")
    if readiness_profile not in (None, READINESS_PROFILE):
        raise ValueError("unsupported readiness profile")
    v2 = readiness_profile == READINESS_PROFILE
    if v2 and (max_dev_rounds != 2 or n_concurrent != 1):
        raise ValueError("v2 readiness profile requires max_dev_rounds=2 and n_concurrent=1")
    output.mkdir(parents=True, exist_ok=True)
    provider = (output / "builder_provider.toml").resolve()
    public_cases = ("dev_001",) if v2 else DEV_CASES
    summary = {
        "schema_version": "dyad-agentloop-pilot-dry-run-v1",
        "status": "pilot_dry_run_ready", "pilot_not_formal": True,
        "readiness_profile": readiness_profile, "required_valid_rounds": 2 if v2 else None,
        "max_dev_rounds": max_dev_rounds, "n_concurrent": n_concurrent,
        "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True},
        "public_cases": list(public_cases), "hidden_cases": ["test_001"],
        "lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER},
        "broker_topology": {"public_lower": "independent-medium", "hidden_lower": "new-independent-medium-after-freeze", "hidden_initial_calls_required": 0},
        "provider_config_host_path": str(provider),
        "provider_config_container_path": str(provider),
        "host_path_equals_container_path": True,
        "network_calls": 0, "docker_started": False, "harbor_started": False,
        "provider_calls": 0, "pilot_execution_started": False,
        "formal_result_claimed": False, "code_score_claimed": False,
        "result_axis": "N/A", "code_axis": "N/A",
    }
    write_json(output / "pilot_protocol_lock.json", summary)
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def verify_readiness_binding(args: argparse.Namespace) -> dict[str, Any] | None:
    """Verify the readiness binding through the coordinator's own verifier.

    The binding covers all ten tasks, so this refuses anything the shared
    ``verify_binding`` does not reproduce byte for byte rather than trusting
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


def run_formal(args: argparse.Namespace) -> int:
    """Run the live formal lifecycle or the explicitly non-formal reduced pilot."""
    pilot = bool(args.pilot)
    readiness_profile = args.readiness_profile
    readiness_binding = verify_readiness_binding(args)
    v2 = readiness_profile == READINESS_PROFILE
    if readiness_profile not in (None, READINESS_PROFILE):
        raise ValueError("unsupported readiness profile")
    if v2 and (not pilot or args.max_dev_rounds != 2 or args.n_concurrent != 1):
        raise ValueError("v2 readiness profile requires --pilot, --max-dev-rounds 2 and --n-concurrent 1")
    public_cases = ("dev_001",) if v2 else DEV_CASES
    hidden_cases = ("test_001",) if (pilot or v2) else HIDDEN_CASES
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing run directory: {run_dir}")
    run_dir.mkdir(parents=True)
    if v2:
        # The Builder runner and the attestation both sit below the parsed
        # arguments, so the verified binding doubles as the runtime marker.
        # Written after mkdir: creating the directory earlier would trip the
        # refuse-to-overwrite guard above.
        write_json(run_dir / "readiness_current_binding.json", readiness_binding)
    credential = args.credential_file.resolve()
    hidden_dir = args.hidden_cases_dir.resolve()
    # Do not inspect credential contents in the host-side launcher.  The
    # evaluator-owned Docker broker is the only component allowed to read the
    # credential; its health/provider result is the authoritative validity
    # check.  A host-side key preflight would both violate the isolation
    # contract and reject root-only secret files before the broker can mount
    # them.
    if not credential.is_file():
        write_json(run_dir / "summary.json", {
            "status": "preflight_failed", "classification": "credential_mount_preflight_failure",
            "formal_result_claimed": False, "code_score_claimed": False,
        })
        return 2
    hidden_errors = validate_hidden_dir(hidden_dir) if not pilot else validate_pilot_hidden_case(
        hidden_dir / "test_001.json"
    )
    if hidden_errors:
        write_json(run_dir / "summary.json", {
            "status": "preflight_failed", "classification": "hidden_inventory_preflight_failure",
            "errors": hidden_errors, "formal_result_claimed": False, "code_score_claimed": False,
        })
        return 2
    harbor = args.harbor.resolve()
    if not harbor.is_file() or not os.access(harbor, os.X_OK):
        write_json(run_dir / "summary.json", {
            "status": "preflight_failed", "classification": "builder_launcher_preflight_failure",
            "formal_result_claimed": False, "code_score_claimed": False,
        })
        return 2

    ports: set[int] = set()
    required_ports = 5
    while len(ports) < required_ports:
        ports.add(free_port())
    allocated = sorted(ports)
    public_port, builder_port, hidden_port = allocated[:3]
    result_judge_port = allocated[3]
    public_judge_port = allocated[-1]
    public_judge_endpoint = None
    public_endpoint = f"http://127.0.0.1:{public_port}/v1/responses"
    builder_host_endpoint = f"http://127.0.0.1:{builder_port}/v1/responses"
    builder_container_endpoint = f"http://{args.builder_host_address}:{builder_port}/v1/responses"
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    result_judge_endpoint: str | None = None
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {
        "public_lower": f"dyad-public-lower-{suffix}",
        "public_judge": f"dyad-public-judge-{suffix}",
        "builder": f"dyad-builder-{suffix}",
        "hidden_lower": f"dyad-hidden-lower-{suffix}",
    }
    names["result_judge"] = f"dyad-result-judge-{suffix}"
    started = {key: False for key in names}
    attempted = {key: False for key in names}
    container_ids: dict[str, str] = {}
    lifecycle: Lifecycle | None = None
    builder_exit = -1
    try:
        attempted["public_lower"] = True
        public_endpoint = start_broker(
            name=names["public_lower"], script=ROOT / "evaluator/broker/candidate_broker.py",
            credential=credential, port=public_port, image=args.lower_image,
            provider_url=args.provider_url, ownership=container_ids,
            ownership_key="public_lower", evidence_dir=run_dir / "brokers/public_lower")
        started["public_lower"] = True
        attempted["public_judge"] = True
        public_judge_endpoint = start_broker(name=names["public_judge"], script=JUDGE_BROKER_SCRIPT,
            credential=credential, port=public_judge_port, image=args.builder_image,
            provider_url=args.provider_url, broker_kind="result_judge_xhigh",
            ownership=container_ids, ownership_key="public_judge", evidence_dir=run_dir / "brokers/public_judge")
        started["public_judge"] = True
        public_judge_initial = broker_stats(public_judge_endpoint)
        write_json(run_dir / "public_judge_broker_initial.json", public_judge_initial)
        if not fresh_xhigh_result_judge(public_judge_initial):
            raise RuntimeError("fresh public Result judge failed xhigh/zero-call gate")
        baseline = baseline_typecheck(run_dir)
        write_json(run_dir / "baseline_typecheck.json", baseline)
        if baseline.get("environment_healthy") is not True:
            raise RuntimeError("baseline build environment is infrastructure-invalid; see baseline_typecheck.json")
        package, visibility = stage_builder_package(run_dir, public_cases)
        workspace = run_dir / "builder_workspace" / "submission"
        workspace.mkdir(parents=True)
        lifecycle = Lifecycle(run_dir=run_dir, workspace=workspace,
                              public_endpoint=public_endpoint, baseline=baseline,
                              public_cases=public_cases, pilot_not_formal=pilot,
                              max_dev_rounds=args.max_dev_rounds, result_judge_endpoint=public_judge_endpoint,
                              readiness_profile=readiness_profile,
                              required_valid_rounds=2 if v2 else None,
                              current_binding=readiness_binding)
        start_submission_server(lifecycle, public_endpoint)
        provider_config = run_dir / "builder_provider.toml"
        direct_builder.write_provider(provider_config, args.builder_base_url)
        config = builder_config(
            run_dir=run_dir, package=package, workspace=workspace,
            lifecycle=lifecycle, provider_config=provider_config,
            image=args.builder_image, broker_endpoint=builder_container_endpoint)
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "dyad-agentloop-pilot-protocol-v1" if pilot else "dyad-agentloop-formal-protocol-v1",
            "pilot_not_formal": pilot,
            "timeout_contract": builder_timeout_contract(args.builder_timeout),
            "readiness_profile": readiness_profile,
            "required_valid_rounds": 2 if v2 else None,
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT,
                        "single_harbor_invocation": True,
                        "single_continuous_session": True, "credential": "native-direct-auth-tmpfs",
                        "transport": "native-codex-cli-direct"},
            "public_lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT,
                             "credential": PLACEHOLDER},
            "hidden_lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT,
                             "credential": PLACEHOLDER,
                             "fresh_broker_started_after_freeze": True},
            "public_result_judge": {"model": "deepseek-flash", "reasoning_effort": "max",
                "owner": "formal_one_stop", "startup_stage": "before-builder",
                "separate_from_hidden_judge": True, "semantic_feedback_required": True},
            "result_judge": {
                "model": "deepseek-flash", "reasoning_effort": "max",
                "owner": "formal_one_stop", "startup_stage": "post-hidden",
                "fresh_zero_call_required": True,
                "external_endpoint_accepted": False,
                "started_in_pilot": pilot,
            },
            "public_cases": list(public_cases), "hidden_cases": list(hidden_cases),
            "builder_visibility": visibility,
            "baseline_typecheck": "recorded separately; not a Result score",
            "result_axis": "N/A", "code_axis": "N/A",
        })
        started_ns = time.time_ns()
        builder_exit = run_builder(args.harbor.resolve(), config, run_dir, args.builder_timeout,
            lifecycle=lifecycle, credential=credential, proxy=args.builder_proxy, provider_url=args.builder_base_url)
        finished_ns = time.time_ns()
        lifecycle.event("builder_invocation_finished", exit_code=builder_exit)
        native = lifecycle.native_attestation(
            builder_exit, allow_interrupted=formal_max_rounds_interrupt(lifecycle, builder_exit))
        if native['valid'] and lifecycle.records and lifecycle.freeze_manifest is None:
            lifecycle._freeze(Path(str(lifecycle.records[-1]["candidate_path"])))
        attestation = builder_attestation(lifecycle, builder_exit, started_ns, finished_ns)
        if pilot:
            attestation.update({
                "schema_version": "dyad-agentloop-pilot-builder-session-attestation-v1",
                "pilot_not_formal": True,
                "accepted_submissions_public_dev_complete": bool(lifecycle.records) and all(
                    len(record.get("dev", [])) == len(public_cases)
                    and lifecycle._public_accountable(record.get("dev", []))
                    for record in lifecycle.records
                ),
            })
            attestation["complete"] = all((
                builder_exit == 0, native['valid'],
                (len(lifecycle.records) == lifecycle.required_valid_rounds if lifecycle.required_valid_rounds is not None
                 else 1 <= len(lifecycle.records) <= lifecycle.max_dev_rounds),
                attestation["accepted_submissions_public_dev_complete"],
                attestation["feedback_delivered"], attestation["feedback_consumed"],
                attestation["distinct_candidate_digests"], attestation["freeze"],
            ))
            write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {
                "status": "builder_lifecycle_incomplete",
                "classification": "builder_timeout_or_incomplete_lifecycle" if builder_exit == 124 else "builder_lifecycle_incomplete",
                "builder_session_attestation": "builder_session_attestation.json",
                "formal_result_claimed": False, "code_score_claimed": False,
                "result_axis": "N/A", "code_axis": "N/A",
            })
            return 2
        attempted["hidden_lower"] = True
        hidden_endpoint = start_broker(
            name=names["hidden_lower"], script=ROOT / "evaluator/broker/candidate_broker.py",
            credential=credential, port=hidden_port, image=args.lower_image,
            provider_url=args.provider_url, ownership=container_ids,
            ownership_key="hidden_lower", evidence_dir=run_dir / "brokers/hidden_lower")
        started["hidden_lower"] = True
        hidden = execute_hidden(lifecycle, hidden_endpoint, hidden_dir, ROOT / "input/repository",
                                case_ids=hidden_cases, pilot_not_formal=pilot)
        assert result_judge_port is not None
        attempted["result_judge"] = True
        result_judge_endpoint = start_broker(
            name=names["result_judge"], script=JUDGE_BROKER_SCRIPT,
            credential=credential, port=result_judge_port, image=args.builder_image,
            provider_url=args.provider_url, broker_kind="result_judge_xhigh",
            ownership=container_ids, ownership_key="result_judge", evidence_dir=run_dir / "brokers",
            # Handed to the coordinator under readiness, so it has to outlive
            # its own stop; --rm would delete it there and then.
            defer_removal=readiness_binding is not None,
        )
        started["result_judge"] = True
        result_judge_initial = broker_stats(result_judge_endpoint)
        write_json(run_dir / "result_judge_broker_initial.json", {
            "schema_version": "dyad-result-judge-broker-initial-v1",
            "owner": "formal_one_stop",
            "container": names["result_judge"],
            "endpoint": result_judge_endpoint,
            "started_after_hidden": True,
            "external_endpoint_accepted": False,
            "fresh_zero_call": fresh_xhigh_result_judge(result_judge_initial),
            "stats": result_judge_initial,
        })
        if not fresh_xhigh_result_judge(result_judge_initial):
            raise RuntimeError("fresh Result-judge broker failed xhigh/zero-call gate")
        if v2:
            # Readiness replaces the formal finalizer with one independent smoke
            # per judge over the frozen candidate: no score threshold, nothing
            # publishable, and the coordinator -- not this pipeline -- admits the
            # exported bundle.
            from evaluator.readiness_smoke import run as run_readiness_judges
            readiness = run_readiness_judges(
                task_root=ROOT, run_dir=run_dir, hidden=hidden,
                credential=args.credential_file.resolve(),
                result_endpoint=result_judge_endpoint)
            complete = readiness["readiness_judges_complete"]
            write_json(run_dir / "summary.json", {
                "schema_version": "dyad-readiness-summary/v1",
                "status": "readiness_evidence_complete" if complete else "readiness_evidence_incomplete",
                "readiness_profile": readiness_profile, "pilot_not_formal": True,
                "public_cases": ["dev_001"], "hidden_cases": ["test_001"],
                "builder_session_attestation": "builder_session_attestation.json",
                "freeze_manifest": "lifecycle/freeze_manifest.json",
                "hidden_attestation": "hidden-after-freeze-attestation.json",
                "formal_result_claimed": False, "code_score_claimed": False,
                "score_threshold": None, "readiness_judge_smoke": "readiness_judge_smoke.json",
                "pipeline_ready": False, "admission_required": True})
            write_json(run_dir / "one_stop_summary.json", read_json(run_dir / "summary.json"))
            return 0 if complete else 2
        finalizer_output = run_dir / ("acceptance_aggregation.json" if pilot else "formal_aggregation.json")
        finalizer = subprocess.run(formal_finalizer_command(
            run_dir=run_dir,
            output=finalizer_output,
            credential=args.credential_file.resolve(),
            result_judge_endpoint=result_judge_endpoint,
            acceptance_cases=hidden_cases if pilot else (),
        ), text=True, capture_output=True, check=False)
        aggregation = read_json(finalizer_output) if finalizer_output.is_file() else {}
        write_json(run_dir / "summary.json", {
            "status": ("pilot_pipeline_complete" if pilot else "completed") if finalizer.returncode == 0 and (not pilot or aggregation.get("acceptance_complete")) else "formal_finalization_refused",
            "pilot_not_formal": pilot,
            "acceptance_complete": bool(pilot and aggregation.get("acceptance_complete")),
            "classification": "real_same_session_two_public_feedback_freeze_" + ("one_hidden_smoke" if pilot else "six_hidden"),
            "builder_session_attestation": "builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "hidden-after-freeze-attestation.json",
            "hidden_case_count": len(hidden_cases),
            "hidden_all_real": hidden["all_cases_real_and_behavior_or_candidate_failure"],
            "formal_result_claimed": not pilot and aggregation.get("formal_result_publishable") is True,
            "code_score_claimed": aggregation.get("code_score_publishable") is True,
            "formal_aggregation": aggregation,
            "finalizer_exit_code": finalizer.returncode,
            "finalizer_stderr_tail": finalizer.stderr[-1500:],
            "combined_score": None,
            "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent,
        })
        write_json(run_dir / "one_stop_summary.json", read_json(run_dir / "summary.json"))
        return 0 if finalizer.returncode == 0 and (not pilot or aggregation.get("acceptance_complete")) else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {
            "status": "orchestration_failed", "classification": "evaluator_orchestration_failure",
            "error_type": type(exc).__name__, "error_detail": str(exc)[-1500:],
            "builder_exit_code": builder_exit,
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        })
        return 2
    finally:
        try:
            if lifecycle is not None:
                lifecycle.close()
        finally:
            endpoints = {
                "public_lower": public_endpoint,
                "public_judge": public_judge_endpoint,
                "builder": builder_host_endpoint,
                "hidden_lower": hidden_endpoint,
                "result_judge": result_judge_endpoint,
            }
            stats_receipts = [
                save_stats(run_dir, key, endpoints.get(key) if started[key] else None)
                for key in names
            ]
            cleanup = []
            retained_judge = None
            retention_error = None
            if readiness_binding is not None and container_ids.get("result_judge"):
                # The coordinator needs one provably owned container that still
                # exists; removing everything here leaves it an empty
                # declaration, which it refuses.
                try:
                    from harbor.readiness_resources import retain_container
                    transport = (run_dir / "brokers/result_judge-judge-transport").resolve()
                    retained_judge = retain_container(
                        container_ids["result_judge"], run_dir,
                        expected_name=names["result_judge"],
                        expected_mount=(str(transport), "/evidence"))
                except Exception as exc:
                    # Unprovable ownership must not become a silently skipped
                    # cleanup; fall through and remove it as usual.
                    retention_error = f"{type(exc).__name__}: {exc}"
            for key, name in names.items():
                if not attempted[key]:
                    continue
                if retained_judge is not None and key == "result_judge":
                    cleanup.append({"name": name, "owned_id": container_ids.get(key),
                                    "ownership_proven": True, "cleanup_attempted": False,
                                    "absent_after_cleanup": False, "removal_deferred": True,
                                    "retained_terminal": True, "role": key,
                                    "startup_attempted": True, "startup_completed": started[key]})
                    continue
                try:
                    receipt = remove_container(name, container_ids.get(key))
                except Exception as exc:
                    receipt = {
                        "name": name, "owned_id": container_ids.get(key),
                        "ownership_proven": bool(container_ids.get(key)),
                        "cleanup_attempted": bool(container_ids.get(key)),
                        "absent_after_cleanup": False,
                        "cleanup_error": f"{type(exc).__name__}: {exc}",
                    }
                receipt.update({"role": key, "startup_attempted": True,
                                "startup_completed": started[key]})
                cleanup.append(receipt)
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
                "schema_version": "dyad-agentloop-cleanup-attestation-v2",
                "removal_deferred": retained_judge is not None,
                "coordinator_cleanup_required": retained_judge is not None,
                "retained_resources": retained_resources,
                "retention_error": retention_error,
                "containers": names,
                "result_judge_owner": "formal_one_stop",
                "external_result_judge_endpoint_accepted": False,
                "stats_receipts": stats_receipts,
                "stats_saved_before_cleanup": all(
                    not started[item["role"]] or item.get("stats_saved") is True
                    for item in stats_receipts
                ),
                "cleanup_finished_at": now(), "cleanup_results": cleanup,
                "unrelated_containers_touched": False,
                "all_started_containers_absent": all(
                    item.get("absent_after_cleanup") is True
                    for item in cleanup if item.get("startup_completed") is True
                ),
                "all_attempted_containers_absent": all(
                    item.get("absent_after_cleanup") is True for item in cleanup
                ),
            })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="run offline protocol checks; starts no Docker or network")
    parser.add_argument("--run-formal", action="store_true",
                        help="explicitly run the real Builder/public/freeze/hidden lifecycle")
    parser.add_argument("--pilot", action="store_true",
                        help="run the reduced non-formal smoke; legacy pilot uses both dev cases")
    parser.add_argument("--readiness-profile", choices=(READINESS_PROFILE,),
                        help="select the explicit v2 readiness dispatch profile")
    parser.add_argument("--readiness-binding-file", type=Path,
                        help="coordinator-issued binding this readiness run is bound to")
    parser.add_argument("--readiness-binding-sha256",
                        help="expected SHA256 of the binding file, verified before dispatch")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --pilot, write a provider-free wiring receipt and exit")
    parser.add_argument("--run-dir", type=Path,
                        help="new evaluator-owned output directory; existing paths are refused")
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--result-judge-broker-endpoint",
                        help="reserved compatibility option; formal mode rejects external ownership")
    parser.add_argument("--hidden-cases-dir", type=Path,
                        help="external evaluator-issued test_001.json through test_006.json directory")
    parser.add_argument("--credential-file", type=Path,
                        default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--harbor", type=Path,
                        default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--lower-image", default=LOWER_IMAGE)
    parser.add_argument("--builder-host-address", default="172.17.0.1")
    parser.add_argument("--provider-url",
                        default=os.environ.get("AGENTSWE_PROVIDER_URL", "https://api.deepseek.com/v1/responses"))
    parser.add_argument("--builder-timeout", type=int, default=28920)
    parser.add_argument('--builder-base-url', default='https://api.deepseek.com/v1')
    parser.add_argument('--builder-proxy', default='http://127.0.0.1:7890')
    args = parser.parse_args(argv)
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent is fixed at 1")
    if args.readiness_profile is not None:
        if not args.pilot:
            parser.error("--readiness-profile requires --pilot; legacy formal surface is unchanged")
        if args.max_dev_rounds != 2:
            parser.error("--readiness-profile requires --max-dev-rounds 2")
    try:
        reject_external_result_judge_endpoint(
            formal=bool(args.run_formal), endpoint=args.result_judge_broker_endpoint
        )
    except ValueError as exc:
        parser.error(str(exc))
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
            detail = str(exc)
            if "10/10 readiness gate" not in detail:
                detail = "10/10 readiness gate: " + detail
            parser.error(f"formal readiness refused: {detail}")
    install_summary_writer(
        args.run_dir,
        max_dev_rounds=args.max_dev_rounds,
        n_concurrent=args.n_concurrent,
        mode="formal" if args.run_formal else "pilot" if args.pilot else "static",
    )
    if args.dry_run and not args.pilot:
        parser.error("--dry-run requires --pilot")
    if sum(bool(value) for value in (args.self_test, args.run_formal, args.pilot)) > 1:
        parser.error("--self-test, --run-formal, and --pilot are mutually exclusive")
    if args.self_test:
        print(json.dumps(offline_self_test(), indent=2, ensure_ascii=False))
        return 0
    if args.pilot and args.dry_run:
        if args.run_dir is None:
            parser.error("--pilot --dry-run requires --run-dir")
        return pilot_dry_run(args.run_dir.resolve(), readiness_profile=args.readiness_profile,
                             max_dev_rounds=args.max_dev_rounds, n_concurrent=args.n_concurrent)
    if not args.run_formal and not args.pilot:
        if args.run_dir is None:
            parser.error("--run-dir is required for a static audit")
        output = args.run_dir.resolve()
        if output.exists() and any(output.iterdir()):
            raise RuntimeError(f"static run directory is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        summary = write_static_artifacts(output)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    if args.run_dir is None or args.hidden_cases_dir is None:
        parser.error("--run-formal/--pilot requires --run-dir and --hidden-cases-dir")
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
    return run_formal(args)


# --- 0921b freeze/submit race guard (harness, package 113-freeze-race) ------
# A submission admitted before the freeze (the fence is evaluated only at
# submit() entry) can still be running its dev evaluation in a
# ThreadingUnixStreamServer handler thread when the Builder process exits and
# the driver freezes the run.  The finished record was then appended to the
# accepted history minutes after the seal, so the sealed freeze no longer
# matched the controller history and the finalizer refused the cell.
# Two guards, both no-ops when nothing is in flight:
#   1. every freeze entry point first waits for accepted submissions that are
#      still being evaluated in OTHER threads (bounded by
#      AGENTSWE_FREEZE_QUIESCE_SECONDS, default 45 min), so the seal describes
#      the true last accepted round;
#   2. the accepted history refuses a record once the run is sealed; the
#      finished evaluation is archived under post_freeze_attempts/ as a
#      superseded post-freeze completion instead of contradicting the seal.
import functools as _fr_functools
import inspect as _fr_inspect
import json as _fr_json
import os as _fr_os
import threading as _fr_threading
import time as _fr_time
from pathlib import Path as _FrPath

_FR_CV = _fr_threading.Condition()
_FR_INFLIGHT = {}
_FR_CAP_DEFAULT = 2700.0
_FR_FREEZE_METHODS = ('freeze', 'freeze_latest', 'freeze_candidate_2',
                      'freeze_on_builder_exit', '_freeze')


def _fr_cap():
    raw = _fr_os.environ.get('AGENTSWE_FREEZE_QUIESCE_SECONDS')
    try:
        value = float(raw) if raw else _FR_CAP_DEFAULT
    except (TypeError, ValueError):
        return _FR_CAP_DEFAULT
    return value if value > 0 else _FR_CAP_DEFAULT


def _fr_begin():
    ident = _fr_threading.get_ident()
    with _FR_CV:
        _FR_INFLIGHT[ident] = _FR_INFLIGHT.get(ident, 0) + 1


def _fr_end():
    ident = _fr_threading.get_ident()
    with _FR_CV:
        remaining = _FR_INFLIGHT.get(ident, 0) - 1
        if remaining > 0:
            _FR_INFLIGHT[ident] = remaining
        else:
            _FR_INFLIGHT.pop(ident, None)
        _FR_CV.notify_all()


def _fr_others():
    ident = _fr_threading.get_ident()
    return sum(count for key, count in _FR_INFLIGHT.items() if key != ident)


def _fr_run_dir(owner):
    for attribute in ('run_dir', 'lifecycle_dir', 'directory'):
        value = getattr(owner, attribute, None)
        if isinstance(value, _FrPath):
            return value
        if isinstance(value, str) and value:
            return _FrPath(value)
    return None


def _fr_write(owner, name, payload):
    directory = _fr_run_dir(owner)
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(
            _fr_json.dumps(payload, indent=2, ensure_ascii=False, default=str) + '\n',
            encoding='utf-8')
    except (OSError, TypeError, ValueError):
        pass


def quiesce_accepted_submissions(owner=None, reason='freeze'):
    """Wait for accepted submissions still being evaluated in other threads."""
    cap = _fr_cap()
    started = _fr_time.monotonic()
    with _FR_CV:
        entered = _fr_others()
        while _fr_others() > 0:
            remaining = cap - (_fr_time.monotonic() - started)
            if remaining <= 0:
                break
            _FR_CV.wait(min(5.0, remaining))
        outstanding = _fr_others()
    receipt = {
        'schema_version': 'agentswe-submission-quiesce/v1',
        'reason': reason,
        'inflight_at_entry': entered,
        'inflight_at_exit': outstanding,
        'quiesced': outstanding == 0,
        'waited_seconds': round(_fr_time.monotonic() - started, 3),
        'cap_seconds': cap,
    }
    if entered:
        _fr_write(owner, 'submission_quiesce.json', receipt)
    return receipt


def _fr_sealed(owner):
    if getattr(owner, 'frozen', None):
        return True
    if getattr(owner, 'freeze_manifest', None):
        return True
    directory = _fr_run_dir(owner)
    try:
        return bool(directory is not None and (directory / 'freeze_manifest.json').exists())
    except OSError:
        return False


def post_freeze_superseded(owner, record):
    """True when this evaluation finished after the seal: archive, never accept."""
    if not _fr_sealed(owner):
        return False
    if isinstance(record, dict):
        record['consumed'] = False
        record['submission_consumed'] = False
        record['consumes_capability_round'] = False
        record['retry_same_round'] = False
        record['retry_allowed'] = False
        record['post_freeze_superseded'] = True
        record['non_consuming_reason'] = 'post_freeze_completion'
    directory = _fr_run_dir(owner)
    if directory is not None:
        try:
            archive = directory / 'post_freeze_attempts'
            archive.mkdir(parents=True, exist_ok=True)
            index = len(list(archive.glob('attempt_*.json'))) + 1
            payload = record if isinstance(record, dict) else {'record': repr(record)}
            (archive / ('attempt_%03d.json' % index)).write_text(
                _fr_json.dumps(payload, indent=2, ensure_ascii=False, default=str) + '\n',
                encoding='utf-8')
        except (OSError, TypeError, ValueError):
            pass
    return True


def _fr_wrap_submit(cls, name='submit'):
    original = getattr(cls, name, None)
    if not _fr_inspect.isfunction(original) or getattr(original, '_fr_inflight', False):
        return False

    @_fr_functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        _fr_begin()
        try:
            return original(self, *args, **kwargs)
        finally:
            _fr_end()

    wrapper._fr_inflight = True
    setattr(cls, name, wrapper)
    return True


def _fr_wrap_freeze(cls, name):
    original = getattr(cls, name, None)
    if not _fr_inspect.isfunction(original) or getattr(original, '_fr_quiesce', False):
        return False

    @_fr_functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        quiesce_accepted_submissions(self, reason=name)
        return original(self, *args, **kwargs)

    wrapper._fr_quiesce = True
    setattr(cls, name, wrapper)
    return True


_fr_wrap_submit(Lifecycle)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(Lifecycle, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__ == "__main__":
    raise SystemExit(main())
