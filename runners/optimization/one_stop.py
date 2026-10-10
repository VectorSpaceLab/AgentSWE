#!/usr/bin/env python3
"""One Harbor-native lifecycle with one logical Builder agent session.

During the native Codex/Claude session the Builder can snapshot its current
submission through a narrow host controller, which launches the already-isolated
Candidate and Eval Harbor jobs for the public dev cases.  The opt-in resumable
profile may invoke the same native session again after a transient provider or
network failure, without creating a new Harbor Trial.  The controller never
exposes benchmark secrets, evaluator assets, or credentials to the Builder.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import adapter
except ModuleNotFoundError:  # direct import by isolated unit tests
    import importlib.util

    _adapter_spec = importlib.util.spec_from_file_location(
        "adapter", Path(__file__).resolve().parent / "adapter.py"
    )
    if _adapter_spec is None or _adapter_spec.loader is None:
        raise
    adapter = importlib.util.module_from_spec(_adapter_spec)
    sys.modules["adapter"] = adapter
    _adapter_spec.loader.exec_module(adapter)

try:
    from prompt_prior import Prior, prior_for_benchmark
except ModuleNotFoundError:  # direct import by isolated unit tests
    import importlib.util as _importlib_util

    _prior_spec = _importlib_util.spec_from_file_location(
        "prompt_prior", Path(__file__).resolve().parent / "prompt_prior.py"
    )
    if _prior_spec is None or _prior_spec.loader is None:
        raise
    _prior_module = _importlib_util.module_from_spec(_prior_spec)
    sys.modules["prompt_prior"] = _prior_module
    _prior_spec.loader.exec_module(_prior_module)
    Prior = _prior_module.Prior
    prior_for_benchmark = _prior_module.prior_for_benchmark


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
DEFAULT_BUILDER_PACKAGE = adapter.DEFAULT_BENCHMARK
MAX_DEV_ROUNDS = 5
CONTROLLER_CONTAINER_SOCKET = "/run/agentswe-controller/dev-controller.sock"
EVALUATION_PARALLELISM_POLICY = "cross_benchmark_parallel_explicit_ipam"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def evaluation_resume_max_attempts() -> int:
    raw = os.environ.get("AGENTSWE_EVAL_RESUME_MAX_ATTEMPTS", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def evaluation_resume_delay(resume_attempt: int) -> float:
    def read_float(name: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(name, str(default))))
        except ValueError:
            return default

    minimum = read_float("AGENTSWE_EVAL_RESUME_MIN_WAIT_SEC", 60.0)
    maximum = read_float("AGENTSWE_EVAL_RESUME_MAX_WAIT_SEC", 300.0)
    return min(maximum, minimum * (2 ** max(0, resume_attempt - 1)))


NATIVE_PROTOCOL_PATHS = {
    "browsecomp-search-agent-optimization-v1": (
        "__init__.py", "browsecomp_broker.py",
    ),
    "terminalbench-code-agent-optimization-v1": (
        "__init__.py", "responses_broker.py", "terminalbench_agent.py",
        "terminalbench_controller.py", "terminalbench_images", "wheelhouse",
    ),
    "tau3-tool-agent-optimization-v1": (
        "__init__.py", "responses_broker.py", "tau3_controller.py", "tau3_runtime.py",
    ),
    "pinchbench-openclaw-agent-optimization-v1": (
        "__init__.py", "responses_broker.py", "pinchbench_controller.py",
        "pinchbench_grader.py", "pinchbench_runtime.py",
    ),
    "osworld-desktop-agent-optimization-v1": (
        "__init__.py", "osworld_agent.py", "osworld_broker.py",
        "osworld_controller.py", "osworld_fixtures.py", "osworld_images",
        "osworld_network.py", "osworld_provider.py", "osworld_runtime.py",
    ),
}


def protocol_source_digests(benchmark_id: str) -> dict[str, str]:
    """Digest shared inputs and only the selected benchmark's native runtime."""
    native_paths = NATIVE_PROTOCOL_PATHS.get(benchmark_id)
    if native_paths is None:
        raise RuntimeError(f"no native protocol source set for {benchmark_id}")
    paths: list[Path] = [
        ROOT / "adapter.py",
        ROOT / "one_stop.py",
        ROOT / "run.sh",
        ROOT / "prompt_prior.py",
        REPO_ROOT / "builders" / "codex" / "codex_provider_agent.py",
        REPO_ROOT / "builders" / "codex" / "resumable_codex_agent.py",
        REPO_ROOT / "builders" / "codex" / "resumable_provider_codex_agent.py",
        active_provider_config(),
        active_model_catalog(),
    ]
    for directory in (
        ROOT / "builder-template",
        ROOT / "task-template",
        ROOT / "eval-template",
    ):
        paths.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    for relative in native_paths:
        path = adapter.NATIVE_ROOT / relative
        if path.is_dir():
            paths.extend(
                child
                for child in path.rglob("*")
                if child.is_file() and "__pycache__" not in child.parts
            )
        elif path.is_file():
            paths.append(path)
        else:
            raise RuntimeError(f"native protocol source missing for {benchmark_id}: {relative}")
    docker_config = Path(os.environ.get("AGENTSWE_DOCKER_CONFIG", str(adapter.HARBOR_ROOT / "docker-config")))
    buildx = docker_config / "cli-plugins" / "docker-buildx"
    if buildx.is_file():
        paths.append(buildx)
    if env_flag("OPTIMIZATION_INFRA_RESUME"):
        overlay_root = ROOT / "harbor_overlay"
        paths.extend(
            path
            for path in overlay_root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    values: dict[str, str] = {}
    for path in sorted(set(paths)):
        key = path.as_posix()
        for base, prefix in ((ROOT, ""), (REPO_ROOT, "repo:"), (adapter.HARBOR_ROOT, "../")):
            if path.is_relative_to(base):
                key = prefix + path.relative_to(base).as_posix()
                break
        values[key] = adapter.file_digest(path)
    return values


def assert_protocol_unchanged(
    *, expected_sources: dict[str, str], expected_benchmark_digest: str,
    benchmark: Path, phase: str
) -> None:
    benchmark_id = str(adapter.benchmark_contract(benchmark)["benchmark_id"])
    actual_sources = protocol_source_digests(benchmark_id)
    if actual_sources != expected_sources:
        changed = sorted(
            key
            for key in set(expected_sources) | set(actual_sources)
            if expected_sources.get(key) != actual_sources.get(key)
        )
        raise RuntimeError(
            f"Adapter protocol sources changed during {phase}: {changed}"
        )
    actual_benchmark_digest = adapter.tree_digest(benchmark)
    if actual_benchmark_digest != expected_benchmark_digest:
        raise RuntimeError(
            f"Benchmark tree changed during {phase}: "
            f"expected {expected_benchmark_digest}, got {actual_benchmark_digest}"
        )


def builder_task_toml() -> str:
    """Return the single-step Builder task contract.

    There are deliberately no Repair steps or fresh sessions.  The dev loop
    happens inside one logical native session; the opt-in infrastructure-resume
    profile may use Codex's native ``resume`` operation after a transient API
    failure.
    """
    return '''schema_version = "1.4"
artifacts = [{ source = "/workspace/submission", destination = "builder_submission" }]

[task]
name = "local/formal-theorem-proving-persistent-builder-0811"
version = "2.0.0"
description = "One uninterrupted Harbor Builder session with controlled dev evaluation"
authors = [{ name = "AgentSWE Harbor Adapter" }]
keywords = ["agentswe", "builder", "long-horizon", "persistent-session", "0811"]

[metadata]
category = "software-engineering"
difficulty_explanation = "Builds and iterates a task-specific agent in one native code-agent session."

[agent]
timeout_sec = @AGENTSWE_BUILDER_TIMEOUT_SEC@
user = "root"
network_mode = "public"

[verifier]
timeout_sec = 120.0
user = "root"
environment_mode = "shared"
network_mode = "no-network"

[environment]
network_mode = "public"
build_timeout_sec = 900.0
cpus = 2
memory_mb = 8192
storage_mb = 16384
workdir = "/workspace"
'''


def builder_instruction(max_dev_rounds: int, dev_case_count: int, prior_text: str) -> str:
    base = f"""# AgentSWE Optimization Builder — one continuous Harbor session

You are the Builder code agent whose model+harness pair is being evaluated. This is
one long-horizon task. Your entire build/dev/repair loop occurs in this one native
session and this one writable workspace; no fresh repair conversation will be
created. Keep working in the current conversation until the candidate is frozen.

## Visibility and workspace

The physically trimmed public package is read-only at `/builder-package`. Read all
four documents under `/builder-package/input/`, `task_contract.json`, the runnable
initial agent under `/builder-package/starter_harness/`, and all {dev_case_count}
public development cases. `/workspace/submission` has already been initialized as
an exact writable copy of that starter. Improve it in place; do not rebuild from an
empty submission. Its required entrypoint remains `run_harness.py`.

Only the paths listed in `task_contract.json` under `editable_space` may be changed.
The evaluator, runtime, task split, tools, model/provider and resource budgets are
fixed. The starter baseline and your candidate are evaluated through the same CLI,
runtime, model, budgets and evaluator. Scores measure final task outcomes, not
similarity to the starter or a particular implementation.

You do not have the hidden cases, evaluator source, rubric, benchmark meta, other
submissions, candidate/evaluator outputs, or the shared resource `.env`. Do not try
to discover or request them. The dedicated runtime prefix named by
`04_resources.md` is mounted read-only at its documented path. Runtime resource
credentials are injected only into isolated Candidate/Eval jobs by the controller;
never place credentials in the submission.

## Public internet

Your Builder phase has public internet access. You may use normal harness features:
web search, official documentation, GitHub, and package installation. Do not spawn
subagents, delegated agents, teammate threads, or additional Codex sessions. The
entire optimization and monitoring loop must remain in this one native Codex
session; multiple session directories make the formal Harbor trial invalid.
Public internet access does not grant access to hidden benchmark data or evaluator
services. Do not upload benchmark inputs, candidate code, or generated artifacts to
third parties except ordinary model/harness traffic required to solve the task.

## Authoritative dev loop (exactly {max_dev_rounds} accepted submissions)

When `/workspace/submission` is runnable, execute:

    submit_dev_candidate

It atomically snapshots the candidate and immediately returns a submission ID while
isolated Harbor Candidate and Eval Jobs run on the public optimization cases. Check it with:

    submit_dev_candidate --status <submission-id>

or wait with periodic progress output:

    submit_dev_candidate --wait <submission-id>

The wait can take many minutes. Keep the wait or status polling in this same agent
session; do not delegate monitoring to another agent or conversation. Poll no more
often than once per minute when an evaluation is healthy. The returned feedback is
the only authoritative dev feedback. Diagnose it, edit the same
`/workspace/submission`, and submit again in this same conversation. Identical
digests are idempotent and do not consume another round. Only one evaluation may be
active at a time.

You must complete exactly {max_dev_rounds} accepted submissions, even if an earlier
round scores above 60. After every non-final accepted round, use the feedback to make
a substantive next iteration and submit a different digest. Do not finish early.
After round {max_dev_rounds}, the controller selects the evaluated candidate with the
highest dev mean (earlier round wins an exact tie), restores that selected candidate
into `/workspace/submission`, and freezes it. When the response says `frozen: true`,
stop editing, verify the restored workspace matches the selected digest, and finish
this same session. Exiting before all {max_dev_rounds} accepted rounds makes the run
incomplete and no test evaluation is performed.

Do not copy the public package, dev cases, resource prefix, model transcripts,
credentials, or private reasoning into the deliverable.
"""
    if not prior_text.strip():
        return base
    return base.rstrip() + "\n\n" + prior_text.strip() + "\n"


def builder_compose(
    *,
    public_package: Path,
    env_prefix: Path,
    workspace_submission: Path,
    controller_socket: Path,
    controller_token: str,
    max_dev_rounds: int,
) -> dict[str, Any]:
    """Mount only Builder-visible data; notably there is no credential file."""
    return {
        "services": {
            "main": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(public_package.resolve()),
                        "target": "/builder-package",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(env_prefix.resolve()),
                        "target": str(adapter.CONTAINER_ENV_PREFIX),
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(workspace_submission.resolve()),
                        "target": "/workspace/submission",
                    },
                    {
                        "type": "bind",
                        "source": str(controller_socket.resolve()),
                        "target": CONTROLLER_CONTAINER_SOCKET,
                        "read_only": True,
                    },
                ],
                "environment": {
                    "AGENTSWE_DEV_CONTROLLER_SOCKET": CONTROLLER_CONTAINER_SOCKET,
                    "AGENTSWE_DEV_CONTROLLER_TOKEN": controller_token,
                    "AGENTSWE_DEV_MAX_ROUNDS": str(max_dev_rounds),
                },
            }
        }
    }


def prepare_public_builder_package(
    *, builder_package: Path, benchmark: Path, destination: Path
) -> Path:
    """Materialize the exact public view mounted into the Builder container."""
    required_inputs = tuple(f"0{i}_{name}.md" for i, name in (
        (1, "task_goal"),
        (2, "interface_and_delivery"),
        (3, "requirements_and_constraints"),
        (4, "resources"),
    ))
    for filename in required_inputs:
        source = builder_package / "input" / filename
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(source)
    contract = adapter.benchmark_contract(benchmark)
    dev_cases = adapter.split_cases(benchmark, "dev")
    public_dev = benchmark / "dev_cases"
    actual_dev_cases = {
        path.name for path in public_dev.iterdir() if path.is_dir()
    }
    if actual_dev_cases != set(dev_cases):
        raise RuntimeError(
            "Public dev case set differs from the fixed protocol: "
            f"{sorted(actual_dev_cases)}"
        )
    for case_id in dev_cases:
        for relative in (Path("input.md"),):
            required = public_dev / case_id / relative
            if not required.exists():
                raise FileNotFoundError(required)
        for path in (public_dev / case_id).rglob("*"):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise RuntimeError(
                    f"Unsafe filesystem entry in public dev case {case_id}: {path}"
                )

    if destination.exists():
        shutil.rmtree(destination)
    (destination / "input").mkdir(parents=True)
    for filename in required_inputs:
        shutil.copy2(builder_package / "input" / filename, destination / "input" / filename)
    (destination / "dev_cases").mkdir()
    for case_id in dev_cases:
        shutil.copytree(
            public_dev / case_id,
            destination / "dev_cases" / case_id,
        )

    starter = benchmark / "starter_harness"
    if not starter.is_dir() or starter.is_symlink():
        raise FileNotFoundError(starter)
    expected_starter_digest = contract["initial_artifact"]["digest"]
    if adapter.tree_digest(starter) != expected_starter_digest:
        raise RuntimeError("starter harness digest differs from task contract")
    shutil.copytree(starter, destination / "starter_harness")
    public_contract = json.loads(json.dumps(contract))
    public_contract["splits"]["hidden"] = {
        "count": len(contract["splits"]["hidden"]["cases"]),
        "cases_withheld": True,
    }
    adapter.write_json(destination / "task_contract.json", public_contract)
    visible_top_level = {path.name for path in destination.iterdir()}
    if visible_top_level != {"input", "dev_cases", "starter_harness", "task_contract.json"}:
        raise RuntimeError(f"Unexpected Builder package entries: {sorted(visible_top_level)}")
    return destination


def stage_builder_job(
    *,
    run_dir: Path,
    run_id: str,
    agent_name: str,
    agent_import_path: str | None,
    model_name: str,
    harness_version: str | None,
    reasoning_effort: str,
    public_package: Path,
    env_prefix: Path,
    workspace_submission: Path,
    controller_socket: Path,
    controller_token: str,
    jobs_dir: Path,
    max_dev_rounds: int,
    dev_case_count: int,
    prior: Prior,
) -> tuple[Path, Path, Path]:
    task_dir = run_dir / "builder_task"
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ROOT / "builder-template", task_dir)
    (task_dir / "task.toml").write_text(builder_task_toml().replace("@AGENTSWE_BUILDER_TIMEOUT_SEC@", str(float(os.environ.get("AGENTSWE_BUILDER_TIMEOUT_SEC", "57600")))), encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        builder_instruction(max_dev_rounds, dev_case_count, prior.text), encoding="utf-8"
    )
    adapter.write_json(task_dir / "prior_metadata.json", prior.metadata())
    adapter.write_json(
        task_dir / "environment" / "docker-compose.yaml",
        builder_compose(
            public_package=public_package,
            env_prefix=env_prefix,
            workspace_submission=workspace_submission,
            controller_socket=controller_socket,
            controller_token=controller_token,
            max_dev_rounds=max_dev_rounds,
        ),
    )

    agent: dict[str, Any] = {"model_name": model_name}
    if agent_import_path:
        agent["import_path"] = agent_import_path
        if agent_import_path in {"deepseek_codex_agent:DeepSeekCodex", "resumable_deepseek_codex_agent:ResumableDeepSeekCodex",
                                 "codex_provider_agent:ProviderCodex", "resumable_provider_codex_agent:ResumableDeepSeekCodex"}:
            # Lite: the provider config and key variable come from the launcher
            # (run_lite_one.sh); the defaults reproduce the deepseek-flash builder.
            key_env = os.environ.get("CODEX_PROVIDER_API_KEY_ENV") or "DEEPSEEK_API_KEY"
            agent["env"] = {
                "CODEX_CONFIG_TOML_PATH": str(active_provider_config()),
                "CODEX_MODEL_CATALOG_PATH": str(active_model_catalog()),
                "CODEX_PROVIDER_API_KEY_ENV": key_env,
                key_env: "${%s}" % key_env,
            }
        elif agent_import_path == "resumable_codex_agent:ResumableCodex":
            # The imported wrapper still delegates to Harbor's stock Codex
            # implementation.  Pass the same host-side auth/config selectors
            # that the built-in ``codex`` route receives, without serializing
            # credential contents into the job config.
            agent["env"] = {
                "CODEX_CONFIG_TOML_PATH": "${CODEX_CONFIG_TOML_PATH}",
                "CODEX_AUTH_JSON_PATH": "${CODEX_AUTH_JSON_PATH}",
                "CODEX_FORCE_AUTH_JSON": "${CODEX_FORCE_AUTH_JSON}",
            }
    else:
        agent["name"] = agent_name
    kwargs: dict[str, Any] = {}
    if harness_version:
        kwargs["version"] = harness_version
    if agent_name in {"codex", "claude-code"}:
        kwargs["reasoning_effort"] = reasoning_effort
    if agent_name == "codex":
        kwargs["web_search"] = "live"
    if kwargs:
        agent["kwargs"] = kwargs

    job_name = f"formal-persistent-builder-{agent_name}-{run_id}"
    config = {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir.resolve()),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "quiet": True,
        "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": True, "force_build": False},
        "agents": [agent],
        "tasks": [{"path": str(task_dir.resolve())}],
    }
    config_path = run_dir / "builder_job_config.json"
    adapter.write_json(config_path, config)
    return config_path, jobs_dir.resolve() / job_name, task_dir


# Statuses run_adapter_phase leaves in a phase's infrastructure_resume_state.json while the phase is still going.
UNFINISHED_PHASE_STATUSES = {"running", "resuming", "paused_infrastructure"}


def record_phase_end(log_dir: Path, exc: BaseException) -> None:
    """Close a phase's resume state when the phase raises: status `infrastructure_error` (the caller, e.g. the dev
    controller, may replay it as a new phase) or `failed`, with finished_at and the error. States the phase already
    closed (completed, blocked on environment repair, resume exhausted) are left as they are."""
    state_path = log_dir / "infrastructure_resume_state.json"
    try:
        state = adapter.read_json(state_path)
    except (OSError, ValueError):
        return
    if not isinstance(state, dict) or state.get("status") not in UNFINISHED_PHASE_STATUSES:
        return
    attempts = sorted(log_dir.glob("attempt_[0-9][0-9][0-9]"))
    status = "infrastructure_error" if adapter.is_infrastructure_error(exc) else "failed"
    if state.get("status") != "paused_infrastructure":
        # a paused phase already holds the event for its last attempt
        state.setdefault("events", []).append({
            "attempt": int(attempts[-1].name[len("attempt_"):]) if attempts else None,
            "phase_id": state.get("phase_id"),
            "status": status,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[-4000:],
            "at": adapter.utc_now(),
        })
    state.update({"status": status, "finished_at": adapter.utc_now()})
    adapter.write_json(state_path, state)


def run_adapter_phase(**kwargs: Any) -> tuple[dict[str, Any], Path]:
    """Run one evaluation phase (see _run_adapter_phase); a phase that raises records how it ended."""
    try:
        return _run_adapter_phase(**kwargs)
    except Exception as exc:
        record_phase_end(kwargs["run_dir"] / "controller_logs" / kwargs["phase_id"], exc)
        raise


def _run_adapter_phase(
    *,
    run_dir: Path,
    phase_id: str,
    candidate: Path,
    cases: tuple[str, ...] | None,
    benchmark: Path,
    env_prefix: Path,
    credential_file: Path,
    jobs_dir: Path,
    n_concurrent: int,
    max_infrastructure_attempts: int = 3,
) -> tuple[dict[str, Any], Path]:
    evaluations_dir = run_dir / "evaluations"
    log_dir = run_dir / "controller_logs" / phase_id
    log_dir.mkdir(parents=True, exist_ok=True)
    expected_case_count = len(cases or adapter.split_cases(benchmark, "hidden"))
    expected_candidate_digest = adapter.tree_digest(candidate)
    resume_enabled = env_flag("OPTIMIZATION_INFRA_RESUME")
    if max_infrastructure_attempts == 0 and not resume_enabled:
        # Zero means unlimited only for the explicit resumable protocol.  Keep
        # direct strict callers fail-safe rather than accidentally looping
        # forever if they omit the optional argument.
        max_infrastructure_attempts = 3
    attempt_records: list[dict[str, Any]] = []
    resume_state_path = log_dir / "infrastructure_resume_state.json"
    adapter.write_json(
        resume_state_path,
        {
            "schema_version": "1.0",
            "phase_id": phase_id,
            "status": "running",
            "candidate_digest": expected_candidate_digest,
            "resume_count": 0,
            "events": [],
        },
    )
    attempt_index = 1
    while max_infrastructure_attempts == 0 or attempt_index <= max_infrastructure_attempts:
        attempt_phase_id = (
            phase_id if attempt_index == 1 else f"{phase_id}-infra-a{attempt_index:03d}"
        )
        command = [
            sys.executable,
            str(ROOT / "adapter.py"),
            "--benchmark", str(benchmark),
            "--candidate", str(candidate),
            "--env-prefix", str(env_prefix),
            "--credential-file", str(credential_file),
            "--jobs-dir", str(jobs_dir),
            "--runs-dir", str(evaluations_dir),
            "--run-id", attempt_phase_id,
            "--n-concurrent", str(n_concurrent),
            "--row-cache-dir", str(run_dir.parent / "row_cache"),
        ]
        if cases:
            command.extend(["--cases", *cases])
        else:
            command.append("--hidden")
        attempt_log_dir = log_dir / f"attempt_{attempt_index:03d}"
        attempt_log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = attempt_log_dir / "stdout.log"
        stderr_path = attempt_log_dir / "stderr.log"
        adapter.write_json(
            attempt_log_dir / "evaluation_parallelism.json",
            {
                "schema_version": "1.0",
                "phase_id": attempt_phase_id,
                "requested_n_concurrent": n_concurrent,
                "started_at": adapter.utc_now(),
                "policy": EVALUATION_PARALLELISM_POLICY,
                "compose_ipam_base": str(adapter.COMPOSE_IPAM_BASE),
                "compose_ipam_prefix": adapter.COMPOSE_IPAM_PREFIX,
                "cross_benchmark_lock": False,
            },
        )
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=adapter.harbor_environment(),
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
            )
        if completed.returncode != 0:
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            message = f"{attempt_phase_id} failed; see {stderr_path}"
            if adapter.is_infrastructure_error(stderr_text):
                if not resume_enabled:
                    raise RuntimeError(f"{message}")
                event = {
                    "attempt": attempt_index,
                    "phase_id": attempt_phase_id,
                    "status": "paused_infrastructure",
                    "error_type": "HarborProcessError",
                    "error_message": message,
                    "at": adapter.utc_now(),
                }
                attempt_records.append(event)
                state = adapter.read_json(resume_state_path)
                state["status"] = "paused_infrastructure"
                state["resume_count"] = attempt_index
                state.setdefault("events", []).append(event)
                adapter.write_json(resume_state_path, state)
                adapter.write_json(log_dir / "infrastructure_attempts.json", attempt_records)
                if (
                    max_infrastructure_attempts > 0
                    and attempt_index >= max_infrastructure_attempts
                ):
                    raise adapter.InfrastructureEvaluationError(
                        f"{message}\n{stderr_text[-4000:]}"
                    )
                delay = evaluation_resume_delay(attempt_index)
                time.sleep(delay)
                state["status"] = "resuming"
                state["last_wait_sec"] = delay
                adapter.write_json(resume_state_path, state)
                attempt_index += 1
                continue
            raise RuntimeError(message)
        phase_dir = evaluations_dir / attempt_phase_id
        summary = adapter.read_json(phase_dir / "score_summary.json")
        actual_candidate_digest = adapter.tree_digest(candidate)
        if actual_candidate_digest != expected_candidate_digest:
            raise RuntimeError(
                f"{attempt_phase_id} changed Candidate digest during evaluation: "
                f"expected {expected_candidate_digest}, got {actual_candidate_digest}"
            )
        result_cases = summary.get("cases", [])
        invalid_cases = [
            str(case.get("case_id"))
            for case in result_cases
            if case.get("contract_valid") is not True
        ]
        infrastructure_cases = [
            str(case.get("case_id"))
            for case in result_cases
            if case.get("infrastructure_failure") is True
        ]
        attempt_records.append(
            {
                "attempt": attempt_index,
                "phase_id": attempt_phase_id,
                "summary": str(phase_dir / "score_summary.json"),
                "case_count": len(result_cases),
                "invalid_cases": invalid_cases,
                "infrastructure_cases": infrastructure_cases,
                "candidate_digest": expected_candidate_digest,
                "accepted": (
                    len(result_cases) == expected_case_count
                    and not invalid_cases
                    and not infrastructure_cases
                ),
            }
        )
        adapter.write_json(log_dir / "infrastructure_attempts.json", attempt_records)
        if len(result_cases) != expected_case_count:
            raise RuntimeError(
                f"{attempt_phase_id} produced {len(result_cases)} Eval rows; "
                f"expected {expected_case_count}"
            )
        non_infrastructure_invalid = [
            case_id for case_id in invalid_cases if case_id not in infrastructure_cases
        ]
        if non_infrastructure_invalid:
            raise RuntimeError(
                f"{attempt_phase_id} produced invalid non-infrastructure Eval "
                f"contracts: {non_infrastructure_invalid}"
            )
        if not invalid_cases and not infrastructure_cases:
            state = adapter.read_json(resume_state_path)
            state.update(
                {
                    "status": (
                        "completed_after_resume"
                        if attempt_index > 1
                        else "completed_without_resume"
                    ),
                    "finished_at": adapter.utc_now(),
                    "resume_count": max(0, attempt_index - 1),
                }
            )
            adapter.write_json(resume_state_path, state)
            return summary, phase_dir
        deterministic_cases = [
            str(case.get("case_id"))
            for case in result_cases
            if case.get("deterministic_infrastructure_failure") is True
        ]
        if deterministic_cases:
            state = adapter.read_json(resume_state_path)
            event = {
                "attempt": attempt_index,
                "phase_id": attempt_phase_id,
                "status": "blocked_environment_repair",
                "infrastructure_cases": infrastructure_cases,
                "deterministic_cases": deterministic_cases,
                "invalid_cases": invalid_cases,
                "at": adapter.utc_now(),
            }
            state.update({
                "status": "blocked_environment_repair",
                "finished_at": adapter.utc_now(),
                "resume_count": max(0, attempt_index - 1),
            })
            state.setdefault("events", []).append(event)
            adapter.write_json(resume_state_path, state)
            raise adapter.DeterministicInfrastructureError(
                f"{phase_id} is blocked on deterministic environment repair; "
                f"cases: {sorted(deterministic_cases)}"
            )
        if attempt_index == max_infrastructure_attempts:
            if max_infrastructure_attempts > 0:
                state = adapter.read_json(resume_state_path)
                state.update(
                    {
                        "status": "resume_exhausted",
                        "resume_count": max(0, attempt_index - 1),
                        "finished_at": adapter.utc_now(),
                    }
                )
                state.setdefault("events", []).append(
                    {
                        "attempt": attempt_index,
                        "phase_id": attempt_phase_id,
                        "status": "resume_exhausted",
                        "infrastructure_cases": infrastructure_cases,
                        "invalid_cases": invalid_cases,
                        "at": adapter.utc_now(),
                    }
                )
                adapter.write_json(resume_state_path, state)
                raise adapter.InfrastructureEvaluationError(
                    f"{phase_id} exhausted {max_infrastructure_attempts} infrastructure "
                    f"attempts; remaining cases: {sorted(set(infrastructure_cases + invalid_cases))}"
                )
        if not resume_enabled:
            attempt_index += 1
            continue
        state = adapter.read_json(resume_state_path)
        event = {
            "attempt": attempt_index,
            "phase_id": attempt_phase_id,
            "status": "paused_infrastructure",
            "infrastructure_cases": infrastructure_cases,
            "invalid_cases": invalid_cases,
            "at": adapter.utc_now(),
        }
        state["status"] = "paused_infrastructure"
        state["resume_count"] = attempt_index
        state.setdefault("events", []).append(event)
        adapter.write_json(resume_state_path, state)
        delay = evaluation_resume_delay(attempt_index)
        time.sleep(delay)
        state["status"] = "resuming"
        state["last_wait_sec"] = delay
        adapter.write_json(resume_state_path, state)
        attempt_index += 1


def dev_passed(summary: dict[str, Any]) -> bool:
    cases = summary.get("cases", [])
    return bool(cases) and all(
        case.get("validity_gate") is True
        and case.get("contract_valid") is True
        and case.get("official_evaluation") is True
        for case in cases
    ) and float(summary.get("mean_score", 0.0)) > 60.0


def feedback_text(summary: dict[str, Any], round_index: int) -> str:
    lines = [
        f"# Authoritative Harbor dev feedback — accepted submission {round_index}",
        "",
        f"Mean score: {summary.get('mean_score')}",
        "Pass rule: every case is an official evaluation, every contract is valid, and mean is strictly greater than 60.0.",
        "",
    ]
    for case in summary.get("cases", []):
        lines.extend(
            [
                f"## {case.get('case_id')}: {case.get('score')}/100",
                "",
                f"- validity_gate: {case.get('validity_gate')}",
                f"- score_contract_valid: {case.get('contract_valid')}",
                f"- official_evaluation: {case.get('official_evaluation')}",
                f"- native_task_available: {case.get('native_task_available')}",
            ]
        )
        eval_path = Path(str(case.get("eval_result", "")))
        if eval_path.is_file():
            evaluation = adapter.read_json(eval_path)
            lines.append(f"- assessment: {evaluation.get('assessment', '')}")
            for error in evaluation.get("major_errors", []) or []:
                lines.append(f"- major_error: {error}")
            harness = evaluation.get("harness_result", {})
            if isinstance(harness, dict):
                for error in harness.get("errors", []) or []:
                    lines.append(f"- harness_error: {error}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def validate_submission(root: Path) -> list[str]:
    errors: list[str] = []
    entry = root / "run_harness.py"
    if not root.is_dir():
        return ["submission directory missing"]
    if not entry.is_file() or entry.is_symlink():
            errors.append("run_harness.py missing or not a regular file")
    elif entry.stat().st_size == 0:
        errors.append("run_harness.py is empty")
    forbidden_names = {
        "test_cases",
        "evaluator",
        "rubric",
        "rubric.md",
        "meta",
        "submissions",
        ".env",
    }
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        forbidden = next(
            (part for part in relative.parts if part in forbidden_names), None
        )
        if forbidden is not None:
            errors.append(f"forbidden path component in submission: {relative}")
        if path.is_symlink():
            errors.append(f"symbolic links are forbidden in submission: {relative}")
        elif not path.is_file() and not path.is_dir():
            errors.append(f"special filesystem object is forbidden: {relative}")
    return errors


def validate_editable_space(
    *, root: Path, starter: Path | None, editable_space: list[str] | tuple[str, ...] | None
) -> list[str]:
    """Reject candidate changes outside the contract's declared editable paths."""
    if starter is None or editable_space is None:
        return []
    errors: list[str] = []
    initial: dict[str, str] = {}
    for path in starter.rglob("*"):
        if path.is_file() and not path.is_symlink():
            initial[path.relative_to(starter).as_posix()] = adapter.file_digest(path)
    current: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            current[path.relative_to(root).as_posix()] = adapter.file_digest(path)
    changed = {
        rel for rel in set(initial) | set(current) if initial.get(rel) != current.get(rel)
    }
    for rel in sorted(changed):
        allowed = any(
            fnmatch.fnmatch(rel, pattern)
            or (pattern.endswith("/**") and rel.startswith(pattern[:-3]))
            for pattern in editable_space
        )
        if not allowed:
            errors.append(f"candidate changed path outside editable_space: {rel}")
    return errors


EvaluationRunner = Callable[[int, int, Path], tuple[dict[str, Any], Path]]


def audit_builder_visibility(
    *, task_dir: Path, public_package: Path, credential_file: Path, benchmark: Path
) -> dict[str, Any]:
    compose = adapter.read_json(task_dir / "environment" / "docker-compose.yaml")
    main = compose["services"]["main"]
    volumes = main.get("volumes", [])
    targets = {str(volume["target"]) for volume in volumes}
    sources = {str(Path(volume["source"]).resolve()) for volume in volumes}
    expected_targets = {
        "/builder-package",
        str(adapter.CONTAINER_ENV_PREFIX),
        "/workspace/submission",
        CONTROLLER_CONTAINER_SOCKET,
    }
    errors: list[str] = []
    if targets != expected_targets:
        errors.append(f"unexpected Builder mount targets: {sorted(targets)}")
    if str(credential_file.resolve()) in sources:
        errors.append("resource credential file is mounted into Builder")
    if str(benchmark.resolve()) in sources:
        errors.append("benchmark root is mounted into Builder")
    visible_roots = {path.name for path in public_package.iterdir()}
    if visible_roots != {"input", "dev_cases", "starter_harness", "task_contract.json"}:
        errors.append(f"unexpected public package roots: {sorted(visible_roots)}")
    forbidden_visible = [
        name for name in ("test_cases", "evaluator", "rubric", "meta", "submissions")
        if (public_package / name).exists()
    ]
    if forbidden_visible:
        errors.append(f"private roots visible in public package: {forbidden_visible}")
    source_contract = adapter.benchmark_contract(benchmark)
    public_contract = adapter.read_json(public_package / "task_contract.json")
    public_hidden = public_contract.get("splits", {}).get("hidden", {})
    source_hidden = source_contract["splits"]["hidden"]["cases"]
    if public_hidden != {"count": len(source_hidden), "cases_withheld": True}:
        errors.append("public task contract exposes or misstates hidden split metadata")
    public_text = json.dumps(public_contract, sort_keys=True)
    for row in source_hidden:
        for field in ("case_id", "upstream_id"):
            value = row.get(field)
            if isinstance(value, str) and json.dumps(value) in public_text:
                errors.append(f"hidden {field} leaked through public task contract")
                break
    if errors:
        raise RuntimeError("Builder visibility audit failed: " + "; ".join(errors))
    return {
        "verified": True,
        "mount_targets": sorted(targets),
        "public_package_roots": sorted(visible_roots),
        "builder_network_mode": "public",
        "credential_mount_count": 0,
        "hidden_mount_count": 0,
        "evaluator_mount_count": 0,
        "checked_at": adapter.utc_now(),
    }


def initialize_submission_from_starter(
    *, public_package: Path, workspace_submission: Path
) -> dict[str, Any]:
    """Atomically initialize the Builder workspace from the frozen starter."""
    starter = public_package / "starter_harness"
    contract = adapter.read_json(public_package / "task_contract.json")
    expected = str(contract["initial_artifact"]["digest"])
    actual = adapter.tree_digest(starter)
    if actual != expected:
        raise RuntimeError(
            f"public starter digest mismatch: expected {expected}, got {actual}"
        )
    workspace_submission.parent.mkdir(parents=True, exist_ok=True)
    staging = workspace_submission.parent / ".submission-initializing"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(starter, staging)
    if workspace_submission.exists():
        shutil.rmtree(workspace_submission)
    staging.replace(workspace_submission)
    initialized = adapter.tree_digest(workspace_submission)
    if initialized != expected:
        raise RuntimeError("initialized submission differs from starter")
    return {
        "initial_artifact": contract["initial_artifact"],
        "initial_artifact_digest": expected,
        "workspace_initial_digest": initialized,
        "editable_space": contract["editable_space"],
        "initialized_from_starter": True,
    }


def improvement_metrics(baseline_score: float, candidate_score: float) -> dict[str, Any]:
    absolute = candidate_score - baseline_score
    relative = absolute / abs(baseline_score) if baseline_score != 0 else None
    return {
        "baseline_score": baseline_score,
        "candidate_score": candidate_score,
        "absolute_improvement": absolute,
        "relative_improvement": relative,
        "relative_improvement_defined": relative is not None,
    }


def select_best_dev_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [
        record
        for record in records
        if record.get("state") == "completed" and record.get("dev_mean") is not None
    ]
    if not completed:
        raise RuntimeError("no completed dev submission is available for selection")
    return max(
        completed,
        key=lambda record: (
            float(record["dev_mean"]),
            -int(record["round"]),
        ),
    )


def paired_score_comparison(
    initial_summary: dict[str, Any], candidate_summary: dict[str, Any]
) -> dict[str, Any]:
    initial_cases = {
        str(case["case_id"]): case for case in initial_summary.get("cases", [])
    }
    candidate_cases = {
        str(case["case_id"]): case for case in candidate_summary.get("cases", [])
    }
    if not initial_cases or set(initial_cases) != set(candidate_cases):
        raise RuntimeError("initial and candidate test case sets differ")
    rows: list[dict[str, Any]] = []
    wins = losses = ties = 0
    for case_id in sorted(initial_cases):
        initial_score = float(initial_cases[case_id]["score"])
        candidate_score = float(candidate_cases[case_id]["score"])
        delta = candidate_score - initial_score
        if delta > 0:
            wins += 1
        elif delta < 0:
            losses += 1
        else:
            ties += 1
        rows.append(
            {
                "case_id": case_id,
                "initial_score": initial_score,
                "candidate_score": candidate_score,
                "delta": delta,
            }
        )
    initial_mean = float(initial_summary.get("mean_score", 0.0))
    candidate_mean = float(candidate_summary.get("mean_score", 0.0))
    return {
        "initial_mean": initial_mean,
        "candidate_mean": candidate_mean,
        "aggregate": improvement_metrics(initial_mean, candidate_mean),
        "candidate_wins": wins,
        "initial_wins": losses,
        "ties": ties,
        "paired_cases": rows,
    }


class DevController:
    """Authenticated, single-flight dev evaluation controller."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        workspace_submission: Path,
        max_rounds: int,
        evaluation_runner: EvaluationRunner,
        starter: Path | None = None,
        editable_space: list[str] | tuple[str, ...] | None = None,
        token: str | None = None,
        infrastructure_resume: bool = False,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.workspace_submission = workspace_submission
        self.max_rounds = max_rounds
        self.evaluation_runner = evaluation_runner
        self.starter = starter
        self.editable_space = editable_space
        self.infrastructure_resume = infrastructure_resume
        self.token = token or secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._records: list[dict[str, Any]] = []
        self._infrastructure_events: list[dict[str, Any]] = []
        self._record_by_id: dict[str, dict[str, Any]] = {}
        self._record_by_digest: dict[str, dict[str, Any]] = {}
        self._attempt_counter = 0
        self._active_id: str | None = None
        self._frozen: dict[str, Any] | None = None
        self._blocked_error: str | None = None
        socket_root = Path(f"/tmp/agentswe-controller-{os.getuid()}")
        socket_name = hashlib.sha256(
            str(self.run_dir.resolve()).encode("utf-8")
        ).hexdigest()[:24]
        self.socket_path = socket_root / f"{socket_name}.sock"
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._server_thread: threading.Thread | None = None

    def _accepted_rounds(self) -> int:
        return sum(record.get("state") == "completed" for record in self._records)

    def _persist_lifecycle(self) -> None:
        adapter.write_json(self.run_dir / "dev_lifecycle.json", self._records)
        adapter.write_json(
            self.run_dir / "infrastructure_events.json",
            self._infrastructure_events,
        )

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._records]

    @property
    def frozen(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._frozen) if self._frozen else None

    @property
    def infrastructure_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._infrastructure_events]

    @property
    def environment_repair_required(self) -> str | None:
        with self._lock:
            return self._blocked_error

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("controller already started")
        controller = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                raw = self.rfile.readline(65537)
                if len(raw) > 65536:
                    response = {"status": 413, "payload": {"error": "request_too_large"}}
                else:
                    try:
                        request = json.loads(raw.decode("utf-8"))
                        if request.get("token") != controller.token:
                            status, payload = 401, {"error": "unauthorized"}
                        elif request.get("action") == "submit":
                            status, payload = controller.submit()
                        elif request.get("action") == "status":
                            status, payload = controller.status(
                                str(request.get("submission_id", ""))
                            )
                        elif request.get("action") == "progress":
                            status, payload = controller.progress()
                        else:
                            status, payload = 400, {"error": "unknown_action"}
                        response = {"status": int(status), "payload": payload}
                    except Exception as exc:
                        response = {
                            "status": 400,
                            "payload": {"error": f"invalid_request: {type(exc).__name__}"},
                        }
                self.wfile.write(
                    json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
                )

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.parent.chmod(0o700)
        self.socket_path.unlink(missing_ok=True)
        self._server = Server(str(self.socket_path), Handler)
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"dev-controller-{self.run_id}",
            daemon=True,
        )
        self._server_thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._server_thread:
            self._server_thread.join(timeout=10)
        self._server = None
        self._server_thread = None
        self.socket_path.unlink(missing_ok=True)

    def _snapshot(self, destination: Path) -> tuple[str | None, list[str]]:
        errors = validate_submission(self.workspace_submission)
        errors.extend(
            validate_editable_space(
                root=self.workspace_submission,
                starter=self.starter,
                editable_space=self.editable_space,
            )
        )
        if errors:
            return None, errors
        digest_before = adapter.tree_digest(self.workspace_submission)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(self.workspace_submission, destination, symlinks=True)
        digest_copy = adapter.tree_digest(destination)
        digest_after = adapter.tree_digest(self.workspace_submission)
        if digest_before != digest_after or digest_before != digest_copy:
            shutil.rmtree(destination, ignore_errors=True)
            return None, ["submission changed while it was being snapshotted; retry"]
        return digest_copy, []

    def submit(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._blocked_error is not None:
                return 503, {
                    "error": "environment_repair_required",
                    "details": self._blocked_error,
                    "accepted_rounds": self._accepted_rounds(),
                    "max_rounds": self.max_rounds,
                    "frozen": False,
                }
            if self._frozen:
                return 409, {
                    "state": "frozen",
                    "frozen": True,
                    "reason": self._frozen["reason"],
                    "digest": self._frozen["digest"],
                }
            if self._active_id:
                return 409, {
                    "error": "evaluation_in_progress",
                    "submission_id": self._active_id,
                    "hint": f"submit_dev_candidate --status {self._active_id}",
                }
            if self._accepted_rounds() >= self.max_rounds:
                return 409, {"error": "round_limit_reached"}

            provisional = self.run_dir / "candidates" / "provisional"
            digest, errors = self._snapshot(provisional)
            if errors or digest is None:
                return 422, {
                    "error": "invalid_submission",
                    "details": errors,
                    "accepted_rounds": self._accepted_rounds(),
                }

            previous = self._record_by_digest.get(digest)
            if previous is not None:
                shutil.rmtree(provisional, ignore_errors=True)
                return 200, self._public_record(previous, duplicate=True)

            round_index = self._accepted_rounds() + 1
            self._attempt_counter += 1
            attempt_index = self._attempt_counter
            snapshot = (
                self.run_dir
                / "candidates"
                / f"round_{round_index:03d}_attempt_{attempt_index:03d}"
            )
            if snapshot.exists():
                shutil.rmtree(snapshot)
            provisional.rename(snapshot)
            submission_id = (
                f"dev-r{round_index:03d}-a{attempt_index:03d}-{digest[:12]}"
            )
            record: dict[str, Any] = {
                "round": round_index,
                "evaluation_attempt": attempt_index,
                "submission_id": submission_id,
                "candidate": str(snapshot),
                "candidate_digest": digest,
                "state": "running",
                "submitted_at": adapter.utc_now(),
            }
            self._records.append(record)
            self._record_by_id[submission_id] = record
            self._record_by_digest[digest] = record
            self._active_id = submission_id
            self._persist_lifecycle()
            worker = threading.Thread(
                target=self._evaluate,
                args=(submission_id,),
                name=f"dev-eval-{submission_id}",
                daemon=True,
            )
            worker.start()
            return 202, self._public_record(record)

    def _evaluate(self, submission_id: str) -> None:
        with self._lock:
            record = self._record_by_id[submission_id]
            round_index = int(record["round"])
            attempt_index = int(record["evaluation_attempt"])
            candidate = Path(record["candidate"])
        resume_attempt = 0
        expected_digest = str(record["candidate_digest"])
        while True:
            try:
                if adapter.tree_digest(candidate) != expected_digest:
                    raise RuntimeError(
                        "Candidate changed before evaluator replay; refusing to "
                        "evaluate a different digest"
                    )
                summary, phase_dir = self.evaluation_runner(
                    round_index, attempt_index + resume_attempt, candidate
                )
                if adapter.tree_digest(candidate) != expected_digest:
                    raise RuntimeError(
                        "Candidate changed during evaluator replay; digest continuity "
                        "contract was violated"
                    )
                lock_path = phase_dir / "evaluation_protocol_lock.json"
                if lock_path.is_file():
                    phase_lock = adapter.read_json(lock_path)
                    if phase_lock.get("candidate_digest") != expected_digest:
                        raise RuntimeError(
                            "Evaluator phase used a different Candidate digest"
                        )
                passed = dev_passed(summary)
                feedback = feedback_text(summary, round_index)
                feedback_path = (
                    self.run_dir / "feedback" / f"round_{round_index:03d}.md"
                )
                feedback_path.parent.mkdir(parents=True, exist_ok=True)
                feedback_path.write_text(feedback, encoding="utf-8")
                with self._condition:
                    record.update(
                        {
                            "state": "completed",
                            "finished_at": adapter.utc_now(),
                            "dev_summary": str(phase_dir / "score_summary.json"),
                            "dev_mean": summary.get("mean_score"),
                            "dev_passed": passed,
                            "feedback": str(feedback_path),
                            "accepted_round": True,
                            "candidate_digest_verified": True,
                        }
                    )
                    if round_index >= self.max_rounds:
                        best = select_best_dev_record(self._records)
                        self._freeze_from(
                            Path(best["candidate"]),
                            str(best["candidate_digest"]),
                            "max_dev_rounds_best_dev",
                            selected_round=int(best["round"]),
                            selected_dev_mean=float(best["dev_mean"]),
                            restore_workspace=True,
                        )
                    self._active_id = None
                    self._persist_lifecycle()
                    self._condition.notify_all()
                return
            except Exception as exc:
                if isinstance(exc, adapter.DeterministicInfrastructureError):
                    with self._condition:
                        record.update(
                            {
                                "state": "blocked_environment_repair",
                                "finished_at": adapter.utc_now(),
                                "infrastructure_error": f"{type(exc).__name__}: {exc}",
                                "accepted_round": False,
                                "round_consumed": False,
                            }
                        )
                        self._blocked_error = str(exc)
                        self._active_id = None
                        self._infrastructure_events.append(dict(record))
                        self._persist_lifecycle()
                        self._condition.notify_all()
                    return
                if not (
                    self.infrastructure_resume
                    and getattr(exc, "retryable", True) is not False
                    and adapter.is_infrastructure_error(exc)
                ):
                    with self._condition:
                        # Preserve the historical strict-mode failure behavior:
                        # the failed submission is not an accepted round and its
                        # snapshot is moved to an auditable failure directory.
                        record.update(
                            {
                                "state": "infrastructure_error",
                                "finished_at": adapter.utc_now(),
                                "infrastructure_error": f"{type(exc).__name__}: {exc}",
                                "accepted_round": False,
                            }
                        )
                        self._records.remove(record)
                        self._record_by_digest.pop(record["candidate_digest"], None)
                        failed_candidate = Path(record["candidate"])
                        preserved_candidate = (
                            self.run_dir
                            / "infrastructure_attempts"
                            / submission_id
                            / "candidate"
                        )
                        if failed_candidate.exists():
                            preserved_candidate.parent.mkdir(parents=True, exist_ok=True)
                            if preserved_candidate.exists():
                                shutil.rmtree(preserved_candidate)
                            failed_candidate.rename(preserved_candidate)
                            record["candidate"] = str(preserved_candidate)
                        self._infrastructure_events.append(dict(record))
                        self._active_id = None
                        self._persist_lifecycle()
                        self._condition.notify_all()
                    return

                resume_attempt += 1
                max_attempts = evaluation_resume_max_attempts()
                with self._condition:
                    record.update(
                        {
                            "state": "paused_infrastructure",
                            "last_infrastructure_error": (
                                f"{type(exc).__name__}: {exc}"
                            ),
                            "infrastructure_resume_attempts": resume_attempt,
                            "paused_at": adapter.utc_now(),
                            "accepted_round": False,
                            "candidate_digest_verified": (
                                adapter.tree_digest(candidate) == expected_digest
                            ),
                        }
                    )
                    self._infrastructure_events.append(
                        {
                            "submission_id": submission_id,
                            "round": round_index,
                            "candidate_digest": expected_digest,
                            "state": "paused_infrastructure",
                            "resume_attempt": resume_attempt,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "at": record["paused_at"],
                        }
                    )
                    self._persist_lifecycle()
                    self._condition.notify_all()

                if max_attempts > 0 and resume_attempt > max_attempts:
                    with self._condition:
                        record.update(
                            {
                                "state": "infrastructure_error",
                                "finished_at": adapter.utc_now(),
                                "infrastructure_error": (
                                    "evaluator infrastructure resume attempts "
                                    f"exhausted after {max_attempts}: {exc}"
                                ),
                                "accepted_round": False,
                            }
                        )
                        self._record_by_digest.pop(record["candidate_digest"], None)
                        self._active_id = None
                        self._persist_lifecycle()
                        self._condition.notify_all()
                    return

                delay = evaluation_resume_delay(resume_attempt)
                time.sleep(delay)
                with self._condition:
                    record.update(
                        {
                            "state": "running",
                            "resumed_at": adapter.utc_now(),
                            "next_resume_wait_sec": delay,
                        }
                    )
                    self._persist_lifecycle()
                    self._condition.notify_all()

    def _replace_workspace_contents(self, source: Path, digest: str) -> None:
        self.workspace_submission.mkdir(parents=True, exist_ok=True)
        for child in list(self.workspace_submission.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in source.iterdir():
            destination = self.workspace_submission / child.name
            if child.is_dir():
                shutil.copytree(child, destination, symlinks=True)
            else:
                shutil.copy2(child, destination, follow_symlinks=False)
        if adapter.tree_digest(self.workspace_submission) != digest:
            raise RuntimeError("restored Builder workspace digest mismatch")

    def _freeze_from(
        self,
        source: Path,
        digest: str,
        reason: str,
        *,
        selected_round: int | None = None,
        selected_dev_mean: float | None = None,
        restore_workspace: bool = False,
    ) -> None:
        frozen = self.run_dir / "frozen_submission"
        if frozen.exists():
            shutil.rmtree(frozen)
        shutil.copytree(source, frozen, symlinks=True)
        actual = adapter.tree_digest(frozen)
        if actual != digest:
            raise RuntimeError("frozen submission digest mismatch")
        if restore_workspace and adapter.tree_digest(self.workspace_submission) != digest:
            self._replace_workspace_contents(frozen, digest)
        self._frozen = {
            "path": str(frozen),
            "digest": digest,
            "reason": reason,
            "frozen_at": adapter.utc_now(),
            "selected_round": selected_round,
            "selected_dev_mean": selected_dev_mean,
            "selection_policy": "highest_dev_mean_then_earliest_round",
            "workspace_restored_to_selected_candidate": restore_workspace,
        }
        adapter.write_json(self.run_dir / "freeze_manifest.json", self._frozen)

    def _public_record(
        self, record: dict[str, Any], *, duplicate: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "submission_id": record["submission_id"],
            "round": record["round"],
            "evaluation_attempt": record["evaluation_attempt"],
            "candidate_digest": record["candidate_digest"],
            "state": record["state"],
            "duplicate": duplicate,
            "accepted_rounds": self._accepted_rounds(),
            "max_rounds": self.max_rounds,
            "frozen": bool(self._frozen),
            "selected_candidate": bool(
                self._frozen
                and self._frozen.get("digest") == record.get("candidate_digest")
            ),
            "candidate_digest_verified": record.get(
                "candidate_digest_verified", False
            ),
        }
        if record["state"] == "completed":
            payload["dev_mean"] = record.get("dev_mean")
            payload["dev_passed"] = record.get("dev_passed")
            feedback_path = Path(str(record.get("feedback", "")))
            payload["feedback"] = (
                feedback_path.read_text(encoding="utf-8")
                if feedback_path.is_file()
                else ""
            )
            if payload["frozen"] and self._frozen:
                payload["freeze_reason"] = self._frozen["reason"]
                payload["selected_digest"] = self._frozen.get("digest")
                payload["selected_round"] = self._frozen.get("selected_round")
                payload["selected_dev_mean"] = self._frozen.get("selected_dev_mean")
        if record["state"] == "infrastructure_error":
            payload["error"] = record.get("infrastructure_error")
            payload["round_consumed"] = False
        if record["state"] == "blocked_environment_repair":
            payload["error"] = record.get("infrastructure_error")
            payload["round_consumed"] = False
            payload["environment_repair_required"] = True
        if record["state"] == "paused_infrastructure":
            payload["error"] = record.get("last_infrastructure_error")
            payload["round_consumed"] = False
            payload["infrastructure_resume_attempts"] = record.get(
                "infrastructure_resume_attempts", 0
            )
            payload["candidate_digest_verified"] = record.get(
                "candidate_digest_verified", False
            )
        return payload

    def status(self, submission_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            record = self._record_by_id.get(submission_id)
            if record is None:
                return 404, {"error": "unknown_submission_id"}
            return 200, self._public_record(record)

    def progress(self) -> tuple[int, dict[str, Any]]:
        """Return liveness state without requiring a submission ID.

        The Builder watchdog uses this endpoint after a native Codex invocation
        returns.  It lets the watchdog wait for an evaluator to finish before
        resuming the same native session, while keeping candidate submission
        and scoring authority inside this controller.
        """
        with self._lock:
            active = self._record_by_id.get(self._active_id) if self._active_id else None
            return 200, {
                "accepted_rounds": self._accepted_rounds(),
                "max_rounds": self.max_rounds,
                "active_submission_id": self._active_id,
                "active_state": active.get("state") if active else None,
                "frozen": bool(self._frozen),
                "environment_repair_required": self._blocked_error is not None,
                "environment_repair_error": self._blocked_error,
            }

    def wait_for_active(self) -> None:
        with self._condition:
            while self._active_id is not None:
                self._condition.wait(timeout=5)

    def freeze_after_builder_exit(self) -> dict[str, Any] | None:
        """Return the five-round freeze, or fail closed on an early Builder exit."""
        self.wait_for_active()
        with self._lock:
            if self._frozen:
                return dict(self._frozen)
            if self._blocked_error is not None:
                adapter.write_json(
                    self.run_dir / "freeze_failure.json",
                    {
                        "reason": "environment_repair_required",
                        "error": self._blocked_error,
                        "accepted_rounds": self._accepted_rounds(),
                        "required_rounds": self.max_rounds,
                        "at": adapter.utc_now(),
                    },
                )
                return None
            adapter.write_json(
                self.run_dir / "freeze_failure.json",
                {
                    "reason": "builder_exited_before_required_dev_rounds",
                    "accepted_rounds": self._accepted_rounds(),
                    "required_rounds": self.max_rounds,
                    "at": adapter.utc_now(),
                },
            )
            return None


def collect_builder_trial_evidence(
    *, job_dir: Path, expected_agent: str, expected_model: str,
    expected_harness_version: str | None
) -> dict[str, Any]:
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise RuntimeError(f"Builder Job result missing: {result_path}")
    job_result = adapter.read_json(result_path)
    stats = job_result.get("stats") or {}
    if (
        stats.get("n_completed_trials") != 1
        or stats.get("n_errored_trials") != 0
        or stats.get("n_retries") != 0
    ):
        raise RuntimeError(f"Builder Job stats violate single-attempt protocol: {stats}")
    trials = [
        path for path in job_dir.iterdir()
        if path.is_dir() and (path / "result.json").is_file()
    ]
    if len(trials) != 1:
        raise RuntimeError(f"Expected exactly one Builder Trial, found {len(trials)}")
    trial = trials[0]
    contract_path = trial / "verifier" / "builder_contract.json"
    if not contract_path.is_file():
        raise RuntimeError(f"Builder contract missing: {contract_path}")
    contract = adapter.read_json(contract_path)
    trial_result = adapter.read_json(trial / "result.json")
    if trial_result.get("exception_info") is not None:
        raise RuntimeError(f"Builder Trial has exception: {trial_result['exception_info']}")

    trajectory_path = trial / "agent" / "trajectory.json"
    if not trajectory_path.is_file():
        raise RuntimeError(
            "Builder native trajectory is missing; session continuity cannot be proven"
        )
    trajectory = adapter.read_json(trajectory_path)
    session_id = trajectory.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Builder trajectory has no native session_id")
    trajectory_agent = trajectory.get("agent") or {}
    if trajectory.get("schema_version") != "ATIF-v1.7":
        raise RuntimeError("Builder trajectory is not ATIF-v1.7")
    if trajectory_agent.get("name") != expected_agent:
        raise RuntimeError(
            "Builder trajectory agent differs from the requested harness"
        )
    reported_agent_info = trial_result.get("agent_info") or {}
    if reported_agent_info.get("name") != expected_agent:
        raise RuntimeError("Harbor reported a different Builder harness")
    reported_model = (reported_agent_info.get("model_info") or {}).get("name")
    if reported_model != expected_model:
        raise RuntimeError(
            f"Harbor reported Builder model {reported_model!r}, expected "
            f"{expected_model!r}"
        )
    reported_version = reported_agent_info.get("version")
    if expected_harness_version and reported_version != expected_harness_version:
        raise RuntimeError(
            f"Harbor reported harness version {reported_version!r}, expected "
            f"{expected_harness_version!r}"
        )
    resume_state_path = trial / "infrastructure_resume_state.json"
    resume_state = (
        adapter.read_json(resume_state_path)
        if resume_state_path.is_file()
        else {"status": "unavailable", "resume_count": 0}
    )
    resume_count = int(resume_state.get("resume_count", 0) or 0)
    resumed = resume_count > 0 or resume_state.get("status") == "completed_after_resume"
    continuation_state_path = trial / "agent" / "builder_continuation_state.json"
    continuation_state = (
        adapter.read_json(continuation_state_path)
        if continuation_state_path.is_file()
        else None
    )
    continuation_count = int(
        continuation_state.get("continuation_count", 0)
        if isinstance(continuation_state, dict)
        else 0
    )
    return {
        "job_dir": str(job_dir),
        "trial_id": trial.name,
        "trial_result": str(trial / "result.json"),
        "builder_contract": str(contract_path),
        "artifact_contract_valid": contract.get("valid") is True,
        "artifact_contract_errors": contract.get("errors") or [],
        "trajectory": str(trajectory_path),
        "native_session_id": session_id,
        "trajectory_agent": trajectory_agent,
        "requested_agent": expected_agent,
        "requested_model": expected_model,
        "requested_harness_version": expected_harness_version,
        "reported_agent_info": reported_agent_info,
        "infrastructure_resume_state": str(resume_state_path),
        "infrastructure_resume": resume_state,
        "builder_continuation_state": (
            str(continuation_state_path)
            if continuation_state_path.is_file()
            else None
        ),
        "builder_continuation": continuation_state,
        "continuity_contract": {
            "harbor_builder_jobs": 1,
            "harbor_builder_trials": 1,
            # Backward-compatible logical-run count.  A resumable run may
            # invoke the CLI more than once, but all invocations belong to one
            # logical native session.
            "native_agent_run_invocations": 1 + continuation_count,
            "native_agent_initial_invocations": 1,
            "native_agent_resume_invocations": resume_count + continuation_count,
            "native_session_ids": [session_id],
            "fresh_repair_sessions": 0,
            "resume_invocations": resume_count + continuation_count,
            "mode": (
                "single-native-session-with-infrastructure-resume"
                if resumed
                else "single-uninterrupted-native-session"
            ),
            "same_logical_native_session": True,
            "same_trial": True,
            "same_container_required": True,
            "verified": True,
        },
    }


def active_provider_config() -> Path:
    """Codex provider config the builder uses (generated per run); digested wherever it lives."""
    value = os.environ.get("CODEX_CONFIG_TOML_PATH")
    return Path(value).resolve() if value else (ROOT / "deepseek-provider.toml")


def active_model_catalog() -> Path:
    """Codex model catalog the builder uses (generated per run)."""
    value = os.environ.get("CODEX_MODEL_CATALOG_PATH")
    return Path(value).resolve() if value else (ROOT / "codex_model_catalog_lite.json")


def check_builder_auth(agent_name: str) -> None:
    if agent_name == "codex":
        provider_key_env = os.environ.get("CODEX_PROVIDER_API_KEY_ENV")
        if provider_key_env:
            provider_config = os.environ.get("CODEX_CONFIG_TOML_PATH")
            if not provider_config or not Path(provider_config).is_file():
                raise RuntimeError(
                    "Codex custom-provider config missing: set CODEX_CONFIG_TOML_PATH"
                )
            if not os.environ.get(provider_key_env):
                raise RuntimeError(
                    f"Codex custom-provider credential missing: {provider_key_env}"
                )
            return
        auth_path = os.environ.get("CODEX_AUTH_JSON_PATH")
        forced = os.environ.get("CODEX_FORCE_AUTH_JSON", "").lower() in {"1", "true", "yes"}
        default_auth = Path.home() / ".codex" / "auth.json"
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        codex_home_auth = codex_home / "auth.json"
        if not os.environ.get("OPENAI_API_KEY") and not (
            auth_path and Path(auth_path).is_file()
        ) and not codex_home_auth.is_file() and not (forced and default_auth.is_file()):
            raise RuntimeError(
                "Codex Builder authentication missing: set OPENAI_API_KEY, "
                "CODEX_AUTH_JSON_PATH, CODEX_HOME, or CODEX_FORCE_AUTH_JSON=1"
            )
    if agent_name == "claude-code" and not any(
        os.environ.get(key)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    ):
        raise RuntimeError(
            "Claude Code Builder authentication missing: set ANTHROPIC_API_KEY, "
            "ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builder-agent", required=True)
    parser.add_argument("--builder-agent-import-path")
    parser.add_argument("--builder-model", required=True)
    parser.add_argument("--builder-harness-version", required=True)
    parser.add_argument("--builder-reasoning-effort", default="high")
    parser.add_argument("--builder-package", type=Path, default=DEFAULT_BUILDER_PACKAGE)
    parser.add_argument("--benchmark", type=Path, default=adapter.DEFAULT_BENCHMARK)
    parser.add_argument("--env-prefix", type=Path, default=adapter.DEFAULT_ENV_PREFIX)
    parser.add_argument("--credential-file", type=Path, default=adapter.DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--jobs-dir", type=Path, default=adapter.HARBOR_ROOT / "jobs")
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "one_stop_runs")
    parser.add_argument("--run-id")
    parser.add_argument("--max-dev-rounds", type=int, default=MAX_DEV_ROUNDS)
    parser.add_argument("--n-concurrent", type=int, default=10)
    parser.add_argument("--stage-builder-only", action="store_true")
    parser.add_argument(
        "--infrastructure-resume",
        action="store_true",
        help="Resume transient Builder/evaluator infrastructure failures in place",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_dev_rounds != MAX_DEV_ROUNDS and os.environ.get("AGENTSWE_PROTOCOL_MODE") != "smoke":
        raise SystemExit(
            f"Formal leaderboard protocol requires --max-dev-rounds {MAX_DEV_ROUNDS}"
        )
    if not 1 <= args.n_concurrent <= 20:
        raise SystemExit("--n-concurrent must be between 1 and 20")
    if args.infrastructure_resume:
        os.environ["OPTIMIZATION_INFRA_RESUME"] = "1"
        os.environ.setdefault(
            "OPTIMIZATION_HARBOR_OVERLAY", str(ROOT / "harbor_overlay")
        )
    if args.builder_agent_import_path and args.builder_agent != "codex":
        raise SystemExit(
            "--builder-agent-import-path is only approved for the codex Builder"
        )
    check_builder_auth(args.builder_agent)
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.runs_dir.resolve() / run_id
    if run_dir.exists():
        raise SystemExit(f"One-stop run already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    builder_package = args.builder_package.resolve()
    benchmark = args.benchmark.resolve()
    env_prefix = args.env_prefix.resolve()
    credential_file = args.credential_file.resolve()
    jobs_dir = args.jobs_dir.resolve()
    for required in (
        credential_file,
        env_prefix / "bin" / "python",
        ROOT / "builder-template" / "environment" / "submit_dev_candidate",
    ):
        if not required.exists():
            raise FileNotFoundError(required)

    contract = adapter.benchmark_contract(benchmark)
    dev_cases = adapter.split_cases(benchmark, "dev")
    hidden_cases = adapter.split_cases(benchmark, "hidden")
    benchmark_digest = adapter.tree_digest(benchmark)
    benchmark_id = str(contract["benchmark_id"])
    prior = prior_for_benchmark(benchmark_id)
    source_digests = protocol_source_digests(benchmark_id)
    credential_stat = credential_file.stat()
    protocol_lock = {
        "schema_version": "1.0",
        "created_at": adapter.utc_now(),
        "benchmark": str(benchmark),
        "benchmark_digest": benchmark_digest,
        "adapter_source_digests": source_digests,
        "builder_package_source": str(builder_package),
        "builder_package_source_digest": adapter.tree_digest(builder_package),
        "environment_prefix": str(env_prefix),
        "credential_file": str(credential_file),
        "credential_file_mode": oct(credential_stat.st_mode & 0o777),
        "credential_values_recorded": False,
        "dev_cases": list(dev_cases),
        "hidden_cases": list(hidden_cases),
        "task_contract": str(benchmark / "task_contract.json"),
        "task_contract_digest": adapter.file_digest(benchmark / "task_contract.json"),
        "initial_artifact": contract["initial_artifact"],
        "editable_space": contract["editable_space"],
        "max_dev_rounds": args.max_dev_rounds,
        "n_concurrent": args.n_concurrent,
        "builder_agent_import_path": args.builder_agent_import_path,
        "prior": prior.metadata(),
        "cross_benchmark_parallelism": EVALUATION_PARALLELISM_POLICY,
        "compose_ipam": {
            "base_cidr": str(adapter.COMPOSE_IPAM_BASE),
            "subnet_prefix": adapter.COMPOSE_IPAM_PREFIX,
            "registry": str(adapter.COMPOSE_IPAM_REGISTRY),
        },
        "candidate_selection": "highest_dev_mean_then_earliest_round",
        "test_comparison": "same_split_initial_vs_selected_candidate",
        "infrastructure_resume": {
            "enabled": args.infrastructure_resume,
            "mode": (
                "single-native-session-with-infrastructure-resume"
                if args.infrastructure_resume
                else "single-uninterrupted-native-session"
            ),
            "builder_max_resume_attempts": int(
                os.environ.get("AGENTSWE_INFRA_RESUME_MAX_ATTEMPTS", "0")
            ),
            "evaluator_max_resume_attempts": evaluation_resume_max_attempts(),
            "builder_min_wait_sec": os.environ.get(
                "AGENTSWE_INFRA_RESUME_MIN_WAIT_SEC", "60"
            ),
            "builder_max_wait_sec": os.environ.get(
                "AGENTSWE_INFRA_RESUME_MAX_WAIT_SEC", "300"
            ),
            "evaluator_min_wait_sec": os.environ.get(
                "AGENTSWE_EVAL_RESUME_MIN_WAIT_SEC", "60"
            ),
            "evaluator_max_wait_sec": os.environ.get(
                "AGENTSWE_EVAL_RESUME_MAX_WAIT_SEC", "300"
            ),
        },
        "versions": {
            "harbor": adapter.run_capture(
                [str(adapter.HARBOR_ROOT / "bin" / "harbor"), "--version"]
            ),
            "docker": adapter.run_capture(
                [
                    "docker",
                    "version",
                    "--format",
                    "{{.Client.Version}}/{{.Server.Version}}",
                ]
            ),
            "python": adapter.run_capture(
                [str(env_prefix / "bin" / "python"), "--version"]
            ),
            "lean": (adapter.run_capture([str(env_prefix / "bin" / "lean"), "--version"]) if (env_prefix / "bin" / "lean").exists() else "absent (Python-only optimization env)"),
            "lake": (adapter.run_capture([str(env_prefix / "bin" / "lake"), "--version"]) if (env_prefix / "bin" / "lake").exists() else "absent (Python-only optimization env)"),
        },
    }
    adapter.write_json(run_dir / "protocol_lock.json", protocol_lock)

    public_package = prepare_public_builder_package(
        builder_package=builder_package,
        benchmark=benchmark,
        destination=run_dir / "builder_public_package",
    )
    public_package_digest = adapter.tree_digest(public_package)
    protocol_lock["builder_public_package_digest"] = public_package_digest
    adapter.write_json(run_dir / "protocol_lock.json", protocol_lock)
    workspace_submission = run_dir / "builder_workspace" / "submission"
    starter_evidence = initialize_submission_from_starter(
        public_package=public_package, workspace_submission=workspace_submission
    )
    adapter.write_json(run_dir / "initial_artifact_evidence.json", starter_evidence)
    baseline_score = 0.0

    def evaluate(
        round_index: int, attempt_index: int, candidate: Path
    ) -> tuple[dict[str, Any], Path]:
        phase = f"dev round {round_index} attempt {attempt_index}"
        assert_protocol_unchanged(
            expected_sources=source_digests,
            expected_benchmark_digest=benchmark_digest,
            benchmark=benchmark,
            phase=f"before {phase}",
        )
        result = run_adapter_phase(
            run_dir=run_dir,
            phase_id=(
                f"{run_id}-dev-r{round_index:03d}-a{attempt_index:03d}"
            ),
            candidate=candidate,
            cases=dev_cases,
            benchmark=benchmark,
            env_prefix=env_prefix,
            credential_file=credential_file,
            jobs_dir=jobs_dir,
            n_concurrent=args.n_concurrent,
            max_infrastructure_attempts=(1 if args.infrastructure_resume else 3),
        )
        assert_protocol_unchanged(
            expected_sources=source_digests,
            expected_benchmark_digest=benchmark_digest,
            benchmark=benchmark,
            phase=f"after {phase}",
        )
        phase_preflight = adapter.read_json(result[1] / "preflight.json")
        if phase_preflight.get("benchmark_digest") != benchmark_digest:
            raise RuntimeError(f"{phase} used a different benchmark digest")
        return result

    controller = DevController(
        run_dir=run_dir,
        run_id=run_id,
        workspace_submission=workspace_submission,
        max_rounds=args.max_dev_rounds,
        evaluation_runner=evaluate,
        starter=public_package / "starter_harness",
        editable_space=contract["editable_space"],
        infrastructure_resume=args.infrastructure_resume,
    )
    controller.start()
    config_path, builder_job_dir, _ = stage_builder_job(
        run_dir=run_dir,
        run_id=run_id,
        agent_name=args.builder_agent,
        agent_import_path=args.builder_agent_import_path,
        model_name=args.builder_model,
        harness_version=args.builder_harness_version,
        reasoning_effort=args.builder_reasoning_effort,
        public_package=public_package,
        env_prefix=env_prefix,
        workspace_submission=workspace_submission,
        controller_socket=controller.socket_path,
        controller_token=controller.token,
        jobs_dir=jobs_dir,
        max_dev_rounds=args.max_dev_rounds,
        dev_case_count=len(dev_cases),
        prior=prior,
    )
    visibility_audit = audit_builder_visibility(
        task_dir=run_dir / "builder_task",
        public_package=public_package,
        credential_file=credential_file,
        benchmark=benchmark,
    )
    adapter.write_json(run_dir / "builder_visibility_audit.json", visibility_audit)

    if args.stage_builder_only:
        controller.stop()
        adapter.write_json(
            run_dir / "stage_result.json",
            {
                "status": "staged",
                "protocol": (
                    "single-native-session-with-infrastructure-resume"
                    if args.infrastructure_resume
                    else "single-uninterrupted-native-session"
                ),
                "builder_config": str(config_path),
                "job_dir": str(builder_job_dir),
                "builder_network": "public",
                "builder_has_resource_credentials": False,
                "visibility_audit": str(run_dir / "builder_visibility_audit.json"),
                "protocol_lock": str(run_dir / "protocol_lock.json"),
                "note": "Controller socket is ephemeral; stage-only configs are for validation, not later execution.",
            },
        )
        print(run_dir)
        return 0

    baseline_summary, baseline_dir = run_adapter_phase(
        run_dir=run_dir,
        phase_id=f"{run_id}-baseline",
        candidate=workspace_submission,
        cases=dev_cases,
        benchmark=benchmark,
        env_prefix=env_prefix,
        credential_file=credential_file,
        jobs_dir=jobs_dir,
        n_concurrent=args.n_concurrent,
        max_infrastructure_attempts=(0 if args.infrastructure_resume else 3),
    )
    baseline_score = float(baseline_summary.get("mean_score", 0.0))
    adapter.write_json(run_dir / "baseline.json", {
        "score": baseline_score,
        "summary": str(baseline_dir / "score_summary.json"),
        "candidate_digest": starter_evidence["initial_artifact_digest"],
        "same_evaluator_runtime_model_budget": True,
        "builder_visible": False,
    })

    try:
        process = adapter.execute_harbor(config_path, run_dir / "builder_process")
        if process["exit_code"] != 0:
            raise RuntimeError(f"Builder Harbor Job failed: {process['stderr']}")
        assert_protocol_unchanged(
            expected_sources=source_digests,
            expected_benchmark_digest=benchmark_digest,
            benchmark=benchmark,
            phase="after Builder Job",
        )
        if adapter.tree_digest(public_package) != public_package_digest:
            raise RuntimeError("Builder public package changed during Builder Job")
        controller.wait_for_active()
        frozen = controller.freeze_after_builder_exit()
    finally:
        controller.stop()

    builder_evidence = collect_builder_trial_evidence(
        job_dir=builder_job_dir,
        expected_agent=args.builder_agent,
        expected_model=args.builder_model,
        expected_harness_version=args.builder_harness_version,
    )
    if frozen is None:
        blocked_error = controller.environment_repair_required
        final = {
            "schema_version": "3.0",
            "status": (
                "environment_repair_required"
                if blocked_error is not None
                else "builder_exited_before_required_dev_rounds"
            ),
            "run_id": run_id,
            "builder": builder_evidence,
            "dev_lifecycle": controller.records,
            "infrastructure_events": controller.infrastructure_events,
            "required_dev_rounds": args.max_dev_rounds,
            "accepted_dev_rounds": controller._accepted_rounds(),
            "leaderboard_eligible": False,
            "failure_manifest": str(run_dir / "freeze_failure.json"),
        }
        if blocked_error is not None:
            final["environment_repair_error"] = blocked_error
        adapter.write_json(run_dir / "one_stop_summary.json", final)
        print(json.dumps(final, indent=2, ensure_ascii=False))
        return 0

    frozen_path = Path(frozen["path"])
    frozen_digest = str(frozen["digest"])
    if builder_evidence["artifact_contract_valid"] is not True:
        raise RuntimeError(
            "A frozen candidate exists but the Builder final workspace contract is invalid"
        )
    builder_contract = adapter.read_json(Path(builder_evidence["builder_contract"]))
    workspace_digest_at_builder_end = builder_contract.get("submission_digest")
    if workspace_digest_at_builder_end != frozen_digest:
        raise RuntimeError(
            "Builder modified /workspace/submission after the controller froze it; "
            "the run violates the freeze protocol"
        )
    builder_evidence["continuity_contract"].update(
        {
            "workspace_digest_at_builder_end": workspace_digest_at_builder_end,
            "frozen_digest": frozen_digest,
            "post_freeze_workspace_unchanged": True,
        }
    )
    initial_test_started_at = adapter.utc_now()
    if datetime.fromisoformat(initial_test_started_at) <= datetime.fromisoformat(
        str(frozen["frozen_at"])
    ):
        raise RuntimeError("Initial test evaluation did not start strictly after freeze")
    assert_protocol_unchanged(
        expected_sources=source_digests,
        expected_benchmark_digest=benchmark_digest,
        benchmark=benchmark,
        phase="before test evaluations",
    )
    initial_test_summary, initial_test_dir = run_adapter_phase(
        run_dir=run_dir,
        phase_id=f"{run_id}-initial-test",
        candidate=public_package / "starter_harness",
        cases=hidden_cases,
        benchmark=benchmark,
        env_prefix=env_prefix,
        credential_file=credential_file,
        jobs_dir=jobs_dir,
        n_concurrent=args.n_concurrent,
        max_infrastructure_attempts=(0 if args.infrastructure_resume else 3),
    )
    candidate_test_started_at = adapter.utc_now()
    candidate_test_summary, candidate_test_dir = run_adapter_phase(
        run_dir=run_dir,
        phase_id=f"{run_id}-candidate-test",
        candidate=frozen_path,
        cases=hidden_cases,
        benchmark=benchmark,
        env_prefix=env_prefix,
        credential_file=credential_file,
        jobs_dir=jobs_dir,
        n_concurrent=args.n_concurrent,
        max_infrastructure_attempts=(0 if args.infrastructure_resume else 3),
    )
    assert_protocol_unchanged(
        expected_sources=source_digests,
        expected_benchmark_digest=benchmark_digest,
        benchmark=benchmark,
        phase="after test evaluations",
    )
    for test_dir in (initial_test_dir, candidate_test_dir):
        test_preflight = adapter.read_json(test_dir / "preflight.json")
        if test_preflight.get("benchmark_digest") != benchmark_digest:
            raise RuntimeError("Test evaluation used a different benchmark digest")
    if adapter.tree_digest(frozen_path) != frozen_digest:
        raise RuntimeError("Frozen submission changed during test evaluation")

    initial_test_cases = initial_test_summary.get("cases", [])
    candidate_test_cases = candidate_test_summary.get("cases", [])
    test_cases_valid = lambda cases: bool(cases) and all(
        case.get("official_evaluation") is True
        and case.get("validity_gate") is True
        and case.get("contract_valid") is True
        and case.get("infrastructure_failure") is not True
        for case in cases
    )
    paired_test = paired_score_comparison(initial_test_summary, candidate_test_summary)
    best_dev = select_best_dev_record(controller.records)
    leaderboard_eligible = (
        controller._accepted_rounds() == args.max_dev_rounds
        and frozen_digest == best_dev.get("candidate_digest")
        and test_cases_valid(initial_test_cases)
        and test_cases_valid(candidate_test_cases)
    )
    final = {
        "schema_version": "3.0",
        "status": "completed" if leaderboard_eligible else "protocol_smoke_only",
        "protocol": (
            "single-native-session-with-infrastructure-resume"
            if args.infrastructure_resume
            else "single-uninterrupted-native-session"
        ),
        "run_id": run_id,
        "builder_agent": args.builder_agent,
        "builder_model": args.builder_model,
        "builder_harness_version": args.builder_harness_version,
        "builder_reasoning_effort": args.builder_reasoning_effort,
        "builder_network": "public",
        "builder_has_resource_credentials": False,
        "builder_public_package": str(public_package),
        "builder_public_package_digest": public_package_digest,
        "protocol_lock": str(run_dir / "protocol_lock.json"),
        "benchmark_digest": benchmark_digest,
        "builder_visibility_audit": str(run_dir / "builder_visibility_audit.json"),
        "builder": builder_evidence,
        "dev_lifecycle": controller.records,
        "infrastructure_events": controller.infrastructure_events,
        "freeze": frozen,
        "required_dev_rounds": args.max_dev_rounds,
        "accepted_dev_rounds": controller._accepted_rounds(),
        "selected_dev_round": best_dev.get("round"),
        "selected_dev_digest": best_dev.get("candidate_digest"),
        "selected_dev_score": best_dev.get("dev_mean"),
        "initial_test_summary": str(initial_test_dir / "score_summary.json"),
        "candidate_test_summary": str(candidate_test_dir / "score_summary.json"),
        "initial_test_scores": [case["score"] for case in initial_test_cases],
        "candidate_test_scores": [case["score"] for case in candidate_test_cases],
        "initial_test_total": initial_test_summary.get("total_score"),
        "candidate_test_total": candidate_test_summary.get("total_score"),
        "initial_test_mean": initial_test_summary.get("mean_score"),
        "candidate_test_mean": candidate_test_summary.get("mean_score"),
        "test_comparison": paired_test,
        "hidden_summary": str(candidate_test_dir / "score_summary.json"),
        "hidden_scores": [case["score"] for case in candidate_test_cases],
        "hidden_total": candidate_test_summary.get("total_score"),
        "hidden_mean": candidate_test_summary.get("mean_score"),
        "leaderboard_eligible": leaderboard_eligible,
        "baseline": str(run_dir / "baseline.json"),
        "dev_improvement": improvement_metrics(
            baseline_score, float(best_dev["dev_mean"])
        ),
        "test_improvement": paired_test["aggregate"],
        "improvement": {
            **paired_test["aggregate"],
            "comparison_split": "test",
        },
        "candidate_dev_score": best_dev.get("dev_mean"),
        "official_hidden_scores": (
            [case["score"] for case in candidate_test_cases]
            if leaderboard_eligible else None
        ),
        "security_contract": {
            "builder_hidden_mounts": 0,
            "builder_evaluator_mounts": 0,
            "builder_credential_mounts": 0,
            "candidate_credentials": "controlled-role-injection",
            "eval_credentials": "controlled-role-injection",
            "initial_test_started_after_freeze": True,
            "candidate_test_started_after_freeze": True,
            "frozen_at": frozen["frozen_at"],
            "initial_test_started_at": initial_test_started_at,
            "candidate_test_started_at": candidate_test_started_at,
        },
    }
    adapter.write_json(run_dir / "one_stop_summary.json", final)
    print(json.dumps(final, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"one-stop adapter error: {exc}", file=sys.stderr)
        # Preserve an auditable terminal state even when an exception escapes
        # the normal Builder/evaluator path.  Previously the shell launcher
        # could observe the non-zero exit while the run directory was left
        # with dev_lifecycle.json stuck at `running` and no summary at all.
        # This is deliberately fail-closed: it never invents scores, freezes
        # a candidate, or makes an infrastructure failure leaderboard-eligible.
        try:
            parsed = parse_args()
            if parsed.run_id:
                failure_dir = (parsed.runs_dir or (Path.cwd() / "one_stop_runs")).resolve() / parsed.run_id
                if failure_dir.is_dir():
                    failure = {
                        "schema_version": "3.0",
                        "status": "runner_error",
                        "run_id": parsed.run_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "leaderboard_eligible": False,
                        "failure_manifest": str(failure_dir / "freeze_failure.json"),
                        "at": adapter.utc_now(),
                    }
                    adapter.write_json(failure_dir / "one_stop_summary.json", failure)
        except Exception as summary_exc:
            print(f"could not write runner failure summary: {summary_exc}", file=sys.stderr)
        raise
