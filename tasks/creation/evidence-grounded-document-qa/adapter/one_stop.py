#!/usr/bin/env python3
"""One Harbor-native lifecycle with one uninterrupted Builder agent session.

The Builder runs exactly once.  During that native Codex/Claude process it can
snapshot its current submission through a narrow host controller, which launches
the already-isolated Candidate and Eval Harbor jobs for the public dev cases.
The controller never exposes benchmark secrets, evaluator assets, or credentials
to the Builder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
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

import adapter


ROOT = Path(__file__).resolve().parent
BUILDER_BASE_IMAGE = "agentswe-os/builder-codex:0.144.1-node24.6.0"
BUILDER_AGENT = "codex"
BUILDER_MODEL = "gpt-5.6-sol"
BUILDER_HARNESS_VERSION = "0.144.1"
BUILDER_REASONING_EFFORT = "xhigh"
BUILDER_BASE_MANIFEST = adapter.HARBOR_ROOT / "builder-base-image" / "image_manifest.json"
DEFAULT_BUILDER_PACKAGE = adapter.ADAPTER_DIR.parent / "builder_package"
MAX_DEV_ROUNDS = 10
CONTROLLER_CONTAINER_SOCKET = "/run/agentswe-controller/dev-controller.sock"


def _env_nonnegative_number(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _eval_resume_enabled() -> bool:
    return os.environ.get("AGENTSWE_EVAL_RESUME_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _eval_resume_delay(attempt: int) -> float:
    minimum = _env_nonnegative_number("AGENTSWE_EVAL_RESUME_MIN_WAIT_SEC", 30.0)
    maximum = max(
        minimum,
        _env_nonnegative_number("AGENTSWE_EVAL_RESUME_MAX_WAIT_SEC", 300.0),
    )
    delay = min(maximum, minimum * (2 ** min(max(0, attempt - 1), 6)))
    if delay <= 0:
        return 0.0
    return min(maximum, delay * random.uniform(0.9, 1.1))


def builder_proxy_environment() -> dict[str, str]:
    """Return an optional infrastructure proxy without persisting its value."""
    value = os.environ.get("AGENTSWE_BUILDER_HTTP_PROXY", "").strip()
    if not value:
        return {}
    if not value.startswith(("http://", "https://")) or any(character in value for character in "\r\n"):
        raise RuntimeError("AGENTSWE_BUILDER_HTTP_PROXY must be a valid HTTP(S) URL")
    placeholder = "${AGENTSWE_BUILDER_HTTP_PROXY}"
    return {key: placeholder for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}


def builder_base_runtime_lock() -> dict[str, Any]:
    manifest = adapter.read_json(BUILDER_BASE_MANIFEST)
    expected = {"status": "verified", "image": BUILDER_BASE_IMAGE, "codex_version": "0.144.1", "networkless_runtime_verify": True, "credential_material_in_image_history": False, "auth_values_recorded": False}
    errors = [f"{key}: expected {value!r}, got {manifest.get(key)!r}" for key, value in expected.items() if manifest.get(key) != value]
    image_id = adapter.run_capture(["docker", "image", "inspect", BUILDER_BASE_IMAGE, "--format", "{{.Id}}"] ).strip()
    if not image_id or image_id != manifest.get("image_id"):
        errors.append(f"image_id: manifest {manifest.get('image_id')!r}, local {image_id!r}")
    if errors:
        raise RuntimeError("Reusable Codex Builder image lock failed: " + "; ".join(errors))
    return {"image": BUILDER_BASE_IMAGE, "image_id": image_id, "codex_version": manifest["codex_version"], "node_version": manifest["node_version"], "manifest": str(BUILDER_BASE_MANIFEST), "manifest_digest": adapter.file_digest(BUILDER_BASE_MANIFEST), "networkless_runtime_verify": True, "credential_material_in_image_history": False, "auth_values_recorded": False}


def protocol_source_digests() -> dict[str, str]:
    """Digest all executable adapter inputs, excluding generated run data."""
    paths: list[Path] = [ROOT / "adapter.py", ROOT / "one_stop.py", ROOT / "run.sh"]
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

    digests = {
        path.relative_to(ROOT).as_posix(): adapter.file_digest(path)
        for path in sorted(set(paths))
    }
    shared_trajectory_archive = adapter.REFINE_ROOT / "trajectory_archive.py"
    digests["shared/trajectory_archive.py"] = adapter.file_digest(
        shared_trajectory_archive
    )
    digests.update(adapter.shared_source_digests())
    return dict(sorted(digests.items()))

def assert_protocol_unchanged(
    *, expected_sources: dict[str, str], expected_benchmark_digest: str,
    benchmark: Path, phase: str
) -> None:
    actual_sources = protocol_source_digests()
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



def prepare_private_environment(source: Path, destination: Path) -> Path:
    """Create one run-private, writable copy of the benchmark environment."""
    if destination.exists():
        raise RuntimeError(f"Private Builder environment already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    completed = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{source.resolve()}:/source:ro",
            "-v", f"{destination.resolve()}:/destination",
            BUILDER_BASE_IMAGE,
            "bash", "-lc",
            "cp --archive --reflink=auto /source/. /destination/",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Failed to copy the run-private Builder environment: "
            + completed.stderr.strip()
        )
    if not (destination / "bin" / "python").is_file():
        raise RuntimeError("Run-private Builder environment has no bin/python")
    return destination.resolve()


def environment_tree_digest(path: Path) -> str:
    """Hash an environment completely, including root-only files."""
    try:
        return adapter.tree_digest(path)
    except PermissionError:
        completed = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{path.resolve()}:/environment:ro",
             BUILDER_BASE_IMAGE, "bash", "-lc",
             "cd /environment && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=gnu -cf - . | sha256sum"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Failed to hash the Builder environment: " + completed.stderr.strip())
        digest = completed.stdout.strip().split()[0]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeError("Builder environment digest output is invalid")
        return digest


def builder_task_toml() -> str:
    # Lite v1: optional Builder session-limit override for smoke tests only; formal runs keep 28800 s.
    seconds = os.environ.get("AGENTSWE_BUILDER_TIMEOUT_SEC", "").strip()
    text = _builder_task_toml_base()
    if seconds:
        text = text.replace("timeout_sec = 28800.0", f"timeout_sec = {float(seconds)}", 1)
    return text


def _builder_task_toml_base() -> str:
    """Return the single-step Builder task contract.

    There are deliberately no Repair steps or sessions.  The dev loop happens
    inside the one agent.run invocation while the native session remains alive.
    """
    return '''schema_version = "1.4"
artifacts = [{ source = "/workspace/submission", destination = "builder_submission" }]

[task]
name = "local/evidence-grounded-document-qa-persistent-builder-0812"
version = "2.0.0"
description = "One uninterrupted Harbor Builder session with controlled dev evaluation"
authors = [{ name = "AgentSWE Harbor Adapter" }]
keywords = ["agentswe", "builder", "long-horizon", "persistent-session", "0812"]

[metadata]
category = "software-engineering"
difficulty_explanation = "Builds and iterates a task-specific agent in one native code-agent session."

[agent]
timeout_sec = 28800.0
user = "root"
network_mode = "public"

[verifier]
timeout_sec = 120.0
user = "root"
environment_mode = "separate"
network_mode = "no-network"

[environment]
network_mode = "public"
build_timeout_sec = 900.0
cpus = 2
memory_mb = 8192
storage_mb = 16384
workdir = "/workspace"
'''


def builder_instruction(max_dev_rounds: int) -> str:
    return f"""# AgentSWE Evidence-Grounded Document QA Builder — one continuous Harbor session

You are the Builder code agent whose model+harness pair is being evaluated. This is
one long-horizon task. Your entire build/dev/repair loop occurs in this one native
session and this one writable workspace; no fresh repair conversation will be
created. Keep working in the current conversation until the candidate is frozen.

## Visibility and workspace

The physically trimmed public package is read-only at `/builder-package`. Read all
four documents under `/builder-package/input/` and both complete public cases under
`/builder-package/dev_cases/`. Create the final task-specific agent only under
`/workspace/submission`; its required entrypoint is `run_agent.py`.

You do not have the hidden cases, evaluator source, rubric, benchmark meta, other
submissions, candidate/evaluator outputs, or the shared resource `.env`. Do not try
to discover or request them. The dedicated Python prefix named by
`04_resources.md` is a run-private writable copy mounted at its documented path.
You may install dependencies and write environment caches there with `conda` or `pip`;
the explicit mounted private prefix is `{adapter.CONTAINER_ENV_PREFIX}`.
Any alternative environment path named in `04_resources.md` aliases this same copy.
Only the submission and this private prefix survive into Candidate execution.
Keep any system libraries or downloaded runtimes under this prefix (libraries in
`lib/`); installing with apt only into the Builder container's /usr is not retained.
Use `AGENTSWE_RUNTIME_PREFIX` to discover the private prefix programmatically;
the shared base prefix is not mounted and cannot be modified. Runtime resource
credentials are injected only into isolated Candidate/Eval jobs by the controller;
never place credentials in the submission.

## Public internet

Your Builder phase has public internet access. You may use normal harness features:
web search, official documentation, GitHub, package installation, and subagents.
Public internet access does not grant access to hidden benchmark data or evaluator
services. Do not upload benchmark inputs, candidate code, or generated artifacts to
third parties except ordinary model/harness traffic required to solve the task.

## Authoritative dev loop (maximum {max_dev_rounds} accepted submissions)

When `/workspace/submission` is runnable, execute:

    submit_dev_candidate

It atomically snapshots the candidate and immediately returns a submission ID while
isolated Harbor Candidate and Eval Jobs run on `dev_001` and `dev_002`. Check it with:

    submit_dev_candidate --status <submission-id>

or wait with periodic progress output:

    submit_dev_candidate --wait <submission-id>

The wait can take many minutes. If your shell tool imposes a timeout, use repeated
`--status` calls instead. The returned feedback is the only authoritative dev
feedback. Diagnose it, edit the same `/workspace/submission`, and submit again in
this same conversation. Identical digests are idempotent and do not consume another
round. Only one evaluation may be active at a time.

The controller freezes the exact submitted digest as soon as every dev case is
valid and mean score is strictly greater than 60, or when {max_dev_rounds} accepted
submissions have been evaluated. When the response says `frozen: true`, stop editing the submission and the dedicated environment,
briefly verify the files still match the frozen candidate, and finish this same
session. If you choose to finish early, the controller freezes your final valid
workspace (or the latest structurally valid submitted snapshot).

Do not copy the public package, dev cases, resource prefix, model transcripts,
credentials, or private reasoning into the deliverable.
"""


def builder_compose(
    *,
    public_package: Path,
    env_prefix: Path,
    workspace_submission: Path,
    controller_socket: Path,
    controller_token: str,
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
                    **builder_proxy_environment(),
                },
            }
        }
    }


def builder_verifier_compose(*, workspace_submission: Path) -> dict[str, Any]:
    """Mount only the final submission into the physically separate verifier."""
    return {
        "services": {
            "main": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(workspace_submission.resolve()),
                        "target": "/workspace/builder-final",
                        "read_only": True,
                    }
                ]
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
    public_dev = benchmark / "dev_cases"
    actual_dev_cases = {
        path.name for path in public_dev.iterdir() if path.is_dir()
    }
    if actual_dev_cases != set(adapter.DEV_CASES):
        raise RuntimeError(
            "Public dev case set differs from the fixed protocol: "
            f"{sorted(actual_dev_cases)}"
        )
    for case_id in adapter.DEV_CASES:
        for relative in (Path("input.md"), Path("assets")):
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
    for case_id in adapter.DEV_CASES:
        shutil.copytree(
            public_dev / case_id,
            destination / "dev_cases" / case_id,
        )

    visible_top_level = {path.name for path in destination.iterdir()}
    if visible_top_level != {"input", "dev_cases"}:
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
) -> tuple[Path, Path, Path]:
    task_dir = run_dir / "builder_task"
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ROOT / "builder-template", task_dir)
    (task_dir / "task.toml").write_text(builder_task_toml(), encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        builder_instruction(max_dev_rounds), encoding="utf-8"
    )
    adapter.write_json(
        task_dir / "environment" / "docker-compose.yaml",
        builder_compose(
            public_package=public_package,
            env_prefix=env_prefix,
            workspace_submission=workspace_submission,
            controller_socket=controller_socket,
            controller_token=controller_token,
        ),
    )
    adapter.write_json(
        task_dir / "tests" / "docker-compose.yaml",
        builder_verifier_compose(workspace_submission=workspace_submission),
    )

    # Harbor gives a built-in agent name precedence over import_path. Supplying
    # both would silently instantiate stock Codex and bypass the custom provider.
    agent: dict[str, Any] = {"model_name": model_name}
    if agent_import_path:
        agent["import_path"] = agent_import_path
        agent["env"] = {
            "CODEX_CONFIG_TOML_PATH": os.environ["CODEX_CONFIG_TOML_PATH"],
            "CODEX_MODEL_CATALOG_PATH": os.environ["CODEX_MODEL_CATALOG_PATH"],
            "CODEX_PROVIDER_API_KEY_ENV": "AGENTSWE_BUILDER_API_KEY",
            "AGENTSWE_BUILDER_API_KEY": "${AGENTSWE_BUILDER_API_KEY}",
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

    job_name = f"document-qa-persistent-builder-{agent_name}-{run_id}"
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


def run_adapter_phase(
    *,
    run_dir: Path,
    phase_id: str,
    candidate: Path,
    cases: tuple[str, ...] | None,
    benchmark: Path,
    env_prefix: Path,
    trusted_env_prefix: Path,
    trusted_env_digest: str,
    credential_file: Path,
    jobs_dir: Path,
    n_concurrent: int,
) -> tuple[dict[str, Any], Path]:
    evaluations_dir = run_dir / "evaluations"
    command = [
        sys.executable,
        str(ROOT / "adapter.py"),
        "--benchmark", str(benchmark),
        "--candidate", str(candidate),
        "--env-prefix", str(env_prefix),
        "--credential-file", str(credential_file),
        "--jobs-dir", str(jobs_dir),
        "--runs-dir", str(evaluations_dir),
        "--run-id", phase_id,
        "--n-concurrent", str(n_concurrent),
    ]
    if cases:
        command.extend(["--cases", *cases])
    log_dir = run_dir / "controller_logs" / phase_id
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        adapter_env = adapter.harbor_environment()
        adapter_env["AGENTSWE_TRUSTED_ENV_PREFIX"] = str(trusted_env_prefix)
        adapter_env["AGENTSWE_TRUSTED_ENV_DIGEST"] = trusted_env_digest
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=adapter_env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(f"{phase_id} failed; see {stderr_path}")
    phase_dir = evaluations_dir / phase_id
    summary = adapter.read_json(phase_dir / "score_summary.json")
    result_cases = summary.get("cases", [])
    expected_case_count = len(cases or adapter.DEFAULT_CASES)
    if len(result_cases) != expected_case_count or not all(
        case.get("contract_valid") is True for case in result_cases
    ):
        raise RuntimeError(
            f"{phase_id} produced missing or invalid Eval score contracts"
        )
    return summary, phase_dir


def dev_passed(summary: dict[str, Any]) -> bool:
    cases = summary.get("cases", [])
    return bool(cases) and all(
        case.get("validity_gate") is True and case.get("contract_valid") is True
        for case in cases
    ) and float(summary.get("mean_score", 0.0)) > 60.0


def feedback_text(summary: dict[str, Any], round_index: int) -> str:
    lines = [
        f"# Authoritative Harbor dev feedback — accepted submission {round_index}",
        "",
        f"Mean score: {summary.get('mean_score')}",
        "Pass rule: every case valid and mean strictly greater than 60.0.",
        "",
    ]
    for case in summary.get("cases", []):
        lines.extend(
            [
                f"## {case.get('case_id')}: {case.get('score')}/100",
                "",
                f"- validity_gate: {case.get('validity_gate')}",
                f"- score_contract_valid: {case.get('contract_valid')}",
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
    entry = root / "run_agent.py"
    if not root.is_dir():
        return ["submission directory missing"]
    if not entry.is_file() or entry.is_symlink():
        errors.append("run_agent.py missing or not a regular file")
    elif entry.stat().st_size == 0:
        errors.append("run_agent.py is empty")
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


EvaluationRunner = Callable[[int, int, Path], tuple[dict[str, Any], Path]]


def audit_builder_visibility(
    *, task_dir: Path, public_package: Path, credential_file: Path, benchmark: Path,
    private_environment: Path,
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
    from runtime_contract import audit_environment_mounts
    expected_targets.update(audit_environment_mounts(
        volumes, private_environment, str(adapter.CONTAINER_ENV_PREFIX)
    ))
    if targets != expected_targets:
        errors.append(f"unexpected Builder mount targets: {sorted(targets)}")
    if str(credential_file.resolve()) in sources:
        errors.append("resource credential file is mounted into Builder")
    if str(benchmark.resolve()) in sources:
        errors.append("benchmark root is mounted into Builder")
    environment_mounts = [
        volume for volume in volumes
        if str(volume.get("target")) == str(adapter.CONTAINER_ENV_PREFIX)
    ]
    if len(environment_mounts) != 1:
        errors.append("Builder must have exactly one dedicated environment mount")
    elif environment_mounts[0].get("read_only") is True:
        errors.append("Builder dedicated environment mount is not writable")
    elif Path(str(environment_mounts[0].get("source", ""))).resolve() != private_environment.resolve():
        errors.append("Builder is not using its run-private environment")
    visible_roots = {path.name for path in public_package.iterdir()}
    if visible_roots != {"input", "dev_cases"}:
        errors.append(f"unexpected public package roots: {sorted(visible_roots)}")
    forbidden_visible = [
        name for name in ("test_cases", "evaluator", "rubric", "meta", "submissions")
        if (public_package / name).exists()
    ]
    if forbidden_visible:
        errors.append(f"private roots visible in public package: {forbidden_visible}")
    if errors:
        raise RuntimeError("Builder visibility audit failed: " + "; ".join(errors))
    return {
        "verified": True,
        "mount_targets": sorted(targets),
        "public_package_roots": sorted(visible_roots),
        "builder_network_mode": "public",
        "builder_environment_writable": True,
        "builder_environment_private": True,
        "builder_environment_source": str(private_environment.resolve()),
        "builder_proxy_enabled": bool(builder_proxy_environment()),
        "builder_proxy_values_recorded": False,
        "credential_mount_count": 0,
        "hidden_mount_count": 0,
        "evaluator_mount_count": 0,
        "checked_at": adapter.utc_now(),
    }


class DevController:
    """Authenticated, single-flight dev evaluation controller."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        workspace_submission: Path,
        workspace_environment: Path | None = None,
        max_rounds: int,
        evaluation_runner: EvaluationRunner,
        token: str | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.workspace_submission = workspace_submission
        self.workspace_environment = workspace_environment
        self.max_rounds = max_rounds
        self.evaluation_runner = evaluation_runner
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
        socket_root = Path(f"/tmp/agentswe-controller-{os.getuid()}")
        socket_name = hashlib.sha256(
            str(self.run_dir.resolve()).encode("utf-8")
        ).hexdigest()[:24]
        self.socket_path = socket_root / f"{socket_name}.sock"
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._server_thread: threading.Thread | None = None

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
            if len(self._records) >= self.max_rounds:
                return 409, {"error": "round_limit_reached"}

            provisional = self.run_dir / "candidates" / "provisional"
            digest, errors = self._snapshot(provisional)
            if errors or digest is None:
                return 422, {
                    "error": "invalid_submission",
                    "details": errors,
                    "accepted_rounds": len(self._records),
                }

            environment_digest = self._environment_digest()
            identity_digest = f"{digest}:{environment_digest}"
            previous = self._record_by_digest.get(identity_digest)
            if previous is not None:
                shutil.rmtree(provisional, ignore_errors=True)
                return 200, self._public_record(previous, duplicate=True)

            round_index = len(self._records) + 1
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
                "environment_digest": environment_digest,
                "identity_digest": identity_digest,
                "state": "running",
                "submitted_at": adapter.utc_now(),
            }
            self._records.append(record)
            self._record_by_id[submission_id] = record
            self._record_by_digest[identity_digest] = record
            self._active_id = submission_id
            adapter.write_json(self.run_dir / "dev_lifecycle.json", self._records)
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
            environment_digest = record.get("environment_digest")
        try:
            resume_failures = 0
            while True:
                try:
                    if self._environment_digest() != environment_digest:
                        raise RuntimeError(
                            "Builder environment changed before Dev evaluation"
                        )
                    summary, phase_dir = self.evaluation_runner(
                        round_index, attempt_index, candidate
                    )
                    break
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    max_attempts = int(_env_nonnegative_number(
                        "AGENTSWE_EVAL_RESUME_MAX_ATTEMPTS", 0
                    ))
                    retryable = not message.startswith(
                        "RuntimeError: Builder environment changed"
                    )
                    can_resume = bool(
                        _eval_resume_enabled()
                        and retryable
                        and (max_attempts == 0 or resume_failures < max_attempts)
                    )
                    if not can_resume:
                        raise
                    resume_failures += 1
                    delay = _eval_resume_delay(resume_failures)
                    with self._condition:
                        event = {
                            "submission_id": submission_id,
                            "round": round_index,
                            "evaluation_attempt": attempt_index,
                            "candidate_digest": record["candidate_digest"],
                            "state": "paused_infrastructure",
                            "infrastructure_error": message,
                            "resume_attempt": resume_failures,
                            "retry_delay_sec": delay,
                            "recorded_at": adapter.utc_now(),
                        }
                        self._infrastructure_events.append(event)
                        record.update({
                            "paused_infrastructure": True,
                            "last_infrastructure_error": message,
                            "resume_attempts": resume_failures,
                            "retry_after_seconds": delay,
                        })
                        adapter.write_json(
                            self.run_dir / "dev_lifecycle.json", self._records
                        )
                        adapter.write_json(
                            self.run_dir / "infrastructure_events.json",
                            self._infrastructure_events,
                        )
                        self._condition.notify_all()
                    time.sleep(delay)
                    with self._condition:
                        self._attempt_counter += 1
                        attempt_index = self._attempt_counter
                        record["last_evaluation_attempt"] = attempt_index
            if self._environment_digest() != environment_digest:
                raise RuntimeError("Builder environment changed during Dev evaluation")
            passed = dev_passed(summary)
            feedback = feedback_text(summary, round_index)
            feedback_path = self.run_dir / "feedback" / f"round_{round_index:03d}.md"
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
                        "paused_infrastructure": False,
                        "resume_attempts": resume_failures,
                    }
                )
                self._active_id = None
                adapter.write_json(self.run_dir / "dev_lifecycle.json", self._records)
                self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                # Infrastructure failures do not consume an accepted capability round.
                record.update(
                    {
                        "state": "infrastructure_error",
                        "finished_at": adapter.utc_now(),
                        "infrastructure_error": f"{type(exc).__name__}: {exc}",
                    }
                )
                self._records.remove(record)
                self._record_by_digest.pop(record["identity_digest"], None)
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
                adapter.write_json(self.run_dir / "dev_lifecycle.json", self._records)
                adapter.write_json(
                    self.run_dir / "infrastructure_events.json",
                    self._infrastructure_events,
                )
                self._condition.notify_all()

    def _environment_digest(self) -> str | None:
        if self.workspace_environment is None:
            return None
        return environment_tree_digest(self.workspace_environment)

    def _freeze_from(
        self, source: Path, digest: str, reason: str,
        environment_digest: str | None = None,
    ) -> None:
        frozen = self.run_dir / "frozen_submission"
        if frozen.exists():
            shutil.rmtree(frozen)
        shutil.copytree(source, frozen, symlinks=True)
        actual = adapter.tree_digest(frozen)
        if actual != digest:
            raise RuntimeError("frozen submission digest mismatch")
        self._frozen = {
            "path": str(frozen),
            "digest": digest,
            "reason": reason,
            "frozen_at": adapter.utc_now(),
            "environment_path": (
                str(self.workspace_environment.resolve())
                if self.workspace_environment is not None else None
            ),
            "environment_digest": (
                environment_digest
                if environment_digest is not None else self._environment_digest()
            ),
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
            "accepted_rounds": len(self._records),
            "max_rounds": self.max_rounds,
            "frozen": bool(
                self._frozen
                and self._frozen.get("digest") == record.get("candidate_digest")
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
        if record.get("paused_infrastructure") is True:
            payload["paused_infrastructure"] = True
            payload["last_infrastructure_error"] = record.get(
                "last_infrastructure_error"
            )
            payload["resume_attempts"] = record.get("resume_attempts", 0)
            payload["retry_after_seconds"] = record.get("retry_after_seconds")
        if record["state"] == "infrastructure_error":
            payload["error"] = record.get("infrastructure_error")
            payload["round_consumed"] = False
        return payload

    def status(self, submission_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            record = self._record_by_id.get(submission_id)
            if record is None:
                return 404, {"error": "unknown_submission_id"}
            return 200, self._public_record(record)

    def wait_for_active(self) -> None:
        with self._condition:
            while self._active_id is not None:
                self._condition.wait(timeout=5)

    def freeze_after_builder_exit(self) -> dict[str, Any] | None:
        """Freeze final workspace, or fall back to the latest valid snapshot."""
        self.wait_for_active()
        with self._lock:
            if self._frozen:
                return dict(self._frozen)
            final_snapshot = self.run_dir / "candidates" / "builder_exit_final"
            digest, errors = self._snapshot(final_snapshot)
            if not errors and digest is not None:
                self._freeze_from(final_snapshot, digest, "builder_exit")
            else:
                valid_records = [
                    record for record in self._records
                    if record["state"] == "completed"
                ]
                if valid_records:
                    latest = valid_records[-1]
                    self._freeze_from(
                        Path(latest["candidate"]),
                        latest["candidate_digest"],
                        "builder_exit_invalid_workspace_latest_valid_submission",
                        environment_digest=latest.get("environment_digest"),
                    )
                else:
                    adapter.write_json(
                        self.run_dir / "freeze_failure.json",
                        {"errors": errors, "at": adapter.utc_now()},
                    )
                    return None
            return dict(self._frozen) if self._frozen else None


def builder_session_timed_out(run_dir: Path) -> bool:
    """Lite v1: true iff the Builder job's only runtime-health failure is the session limit (AgentTimeoutError)."""
    path = run_dir / "builder_process" / "runtime_infrastructure_failure.json"
    if not path.is_file():
        return False
    failures = json.loads(path.read_text(encoding="utf-8"))
    return bool(failures) and all(
        isinstance(f, dict) and f.get("exception_type") == "AgentTimeoutError" for f in failures
    )


def collect_builder_trial_evidence(
    *, job_dir: Path, expected_agent: str, expected_model: str,
    expected_harness_version: str | None, allow_session_timeout: bool = False
) -> dict[str, Any]:
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise RuntimeError(f"Builder Job result missing: {result_path}")
    job_result = adapter.read_json(result_path)
    stats = job_result.get("stats") or {}
    timed_out = False
    if allow_session_timeout:  # Lite v1: the single trial may carry exactly the session-limit exception
        exception_types = {
            name for entry in (stats.get("evals") or {}).values()
            for name in (entry.get("exception_stats") or {})
        }
        timed_out = stats.get("n_errored_trials") == 1 and exception_types == {"AgentTimeoutError"}
    if (
        stats.get("n_completed_trials") != 1
        or stats.get("n_errored_trials") != (1 if timed_out else 0)
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
    if trial_result.get("exception_info") is not None and not (
        timed_out and trial_result["exception_info"].get("exception_type") == "AgentTimeoutError"
    ):
        raise RuntimeError(f"Builder Trial has exception: {trial_result['exception_info']}")

    trajectory_path = trial / "agent" / "trajectory.json"
    if not trajectory_path.is_file():
        raise RuntimeError(
            "Builder native trajectory is missing; session continuity cannot be proven"
        )
    trajectory_jsonl = trajectory_path.with_suffix(".jsonl")
    if not trajectory_jsonl.is_file():
        raise RuntimeError(
            "Builder trajectory JSONL archive is missing; default trajectory persistence failed"
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
    return {
        "job_dir": str(job_dir),
        "builder_session_timed_out": timed_out,
        "trial_id": trial.name,
        "trial_result": str(trial / "result.json"),
        "builder_contract": str(contract_path),
        "artifact_contract_valid": contract.get("valid") is True,
        "artifact_contract_errors": contract.get("errors") or [],
        "trajectory": str(trajectory_path),
        "trajectory_jsonl": str(trajectory_jsonl),
        "native_session_id": session_id,
        "trajectory_agent": trajectory_agent,
        "requested_agent": expected_agent,
        "requested_model": expected_model,
        "requested_harness_version": expected_harness_version,
        "reported_agent_info": reported_agent_info,
        "continuity_contract": {
            "harbor_builder_jobs": 1,
            "harbor_builder_trials": 1,
            "native_agent_run_invocations": 1,
            "native_session_ids": [session_id],
            "fresh_repair_sessions": 0,
            "resume_invocations": 0,
            "mode": "single-uninterrupted-native-session",
            "verified": True,
        },
    }


def check_builder_auth(agent_name: str, agent_import_path: str | None = None) -> None:
    if agent_import_path:
        return
    if agent_name == "codex":
        auth_path = os.environ.get("CODEX_AUTH_JSON_PATH")
        forced = os.environ.get("CODEX_FORCE_AUTH_JSON", "").lower() in {"1", "true", "yes"}
        default_auth = Path.home() / ".codex" / "auth.json"
        if not os.environ.get("OPENAI_API_KEY") and not (
            auth_path and Path(auth_path).is_file()
        ) and not (forced and default_auth.is_file()):
            raise RuntimeError(
                "Codex Builder authentication missing: set OPENAI_API_KEY, "
                "CODEX_AUTH_JSON_PATH, or CODEX_FORCE_AUTH_JSON=1"
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
    parser.add_argument("--builder-reasoning-effort", default=BUILDER_REASONING_EFFORT)
    parser.add_argument("--builder-package", type=Path, default=DEFAULT_BUILDER_PACKAGE)
    parser.add_argument("--benchmark", type=Path, default=adapter.DEFAULT_BENCHMARK)
    parser.add_argument("--env-prefix", type=Path, default=adapter.DEFAULT_ENV_PREFIX)
    parser.add_argument("--credential-file", type=Path, default=adapter.DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--jobs-dir", type=Path, default=adapter.HARBOR_ROOT / "jobs")
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "one_stop_runs")
    parser.add_argument("--run-id")
    parser.add_argument("--max-dev-rounds", type=int, default=MAX_DEV_ROUNDS)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--stage-builder-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    required_builder_protocol = {
        "--builder-agent": (args.builder_agent, BUILDER_AGENT),
        "--builder-harness-version": (
            args.builder_harness_version,
            BUILDER_HARNESS_VERSION,
        ),
    }
    mismatches = [
        f"{flag} must be {expected!r}, got {actual!r}"
        for flag, (actual, expected) in required_builder_protocol.items()
        if actual != expected
    ]
    if mismatches:
        raise SystemExit(
            "Unified 0820 Document QA protocol mismatch: " + "; ".join(mismatches)
        )
    if args.max_dev_rounds != MAX_DEV_ROUNDS and os.environ.get("AGENTSWE_PROTOCOL_MODE") != "smoke":
        raise SystemExit(
            f"Document QA leaderboard protocol requires --max-dev-rounds {MAX_DEV_ROUNDS}"
        )
    if not 1 <= args.n_concurrent <= 2:  # Lite v1 (as 0918): dev/hidden evaluation may run two cases concurrently
        raise SystemExit("Document QA leaderboard protocol requires --n-concurrent 1 or 2")
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.runs_dir.resolve() / run_id
    if run_dir.exists():
        raise SystemExit(f"One-stop run already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    builder_package = args.builder_package.resolve()
    benchmark = args.benchmark.resolve()
    base_env_prefix = args.env_prefix.resolve()
    credential_file = args.credential_file.resolve()
    jobs_dir = args.jobs_dir.resolve()
    for required in (
        credential_file,
        base_env_prefix / "bin" / "python",
        ROOT / "builder-template" / "environment" / "submit_dev_candidate",
    ):
        if not required.exists():
            raise FileNotFoundError(required)

    benchmark_digest = adapter.tree_digest(benchmark)
    source_digests = protocol_source_digests()
    credential_stat = credential_file.stat()
    base_environment_digest = environment_tree_digest(base_env_prefix)
    protocol_lock = {
        "schema_version": "1.0",
        "created_at": adapter.utc_now(),
        "benchmark": str(benchmark),
        "benchmark_digest": benchmark_digest,
        "adapter_source_digests": source_digests,
        "builder_package_source": str(builder_package),
        "builder_package_source_digest": adapter.tree_digest(builder_package),
        "base_environment_prefix": str(base_env_prefix),
        "base_environment_digest": base_environment_digest,
        "trusted_runtime_prefix": str(base_env_prefix),
        "trusted_runtime_digest": base_environment_digest,
        "trusted_runtime_builder_visible": False,
        "credential_file": str(credential_file),
        "credential_file_mode": oct(credential_stat.st_mode & 0o777),
        "credential_values_recorded": False,
        "builder_base": builder_base_runtime_lock(),
        "builder_proxy": {"enabled": bool(builder_proxy_environment()), "values_recorded": False, "environment_keys": sorted(builder_proxy_environment())},
        "dev_cases": list(adapter.DEV_CASES),
        "hidden_cases": list(adapter.DEFAULT_CASES),
        "max_dev_rounds": args.max_dev_rounds,
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
                [str(base_env_prefix / "bin" / "python"), "--version"]
            ),
            "qa_env_python": adapter.run_capture(
                [str(base_env_prefix / "bin" / "python"), "--version"]
            ),
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
    workspace_submission.mkdir(parents=True)
    env_prefix = prepare_private_environment(
        base_env_prefix,
        run_dir / "builder_workspace" / "environment",
    )
    protocol_lock["private_environment_prefix"] = str(env_prefix)
    protocol_lock["private_environment_initial_digest"] = environment_tree_digest(env_prefix)
    protocol_lock["private_environment_writable"] = True
    protocol_lock["base_environment_unchanged_by_builder"] = True
    adapter.write_json(run_dir / "protocol_lock.json", protocol_lock)

    def evaluate(
        round_index: int, attempt_index: int, candidate: Path
    ) -> tuple[dict[str, Any], Path]:
        phase = f"dev round {round_index} attempt {attempt_index}"
        if environment_tree_digest(base_env_prefix) != base_environment_digest:
            raise RuntimeError("Trusted base environment changed before " + phase)
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
            cases=adapter.DEV_CASES,
            benchmark=benchmark,
            env_prefix=env_prefix,
            trusted_env_prefix=base_env_prefix,
            trusted_env_digest=base_environment_digest,
            credential_file=credential_file,
            jobs_dir=jobs_dir,
            n_concurrent=args.n_concurrent,
        )
        if environment_tree_digest(base_env_prefix) != base_environment_digest:
            raise RuntimeError("Trusted base environment changed during " + phase)
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
        workspace_environment=env_prefix,
        max_rounds=args.max_dev_rounds,
        evaluation_runner=evaluate,
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
    )
    visibility_audit = audit_builder_visibility(
        task_dir=run_dir / "builder_task",
        public_package=public_package,
        credential_file=credential_file,
        benchmark=benchmark,
        private_environment=env_prefix,
    )
    adapter.write_json(run_dir / "builder_visibility_audit.json", visibility_audit)

    if args.stage_builder_only:
        controller.stop()
        adapter.write_json(
            run_dir / "stage_result.json",
            {
                "status": "staged",
                "protocol": "single-uninterrupted-native-session",
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

    check_builder_auth(args.builder_agent, args.builder_agent_import_path)
    try:
        try:
            process = adapter.execute_harbor(config_path, run_dir / "builder_process")
        except RuntimeError:
            # Lite v1: when the Builder session hits the Harbor session limit (AgentTimeoutError is the only
            # runtime-health failure) the run is frozen as on a normal Builder exit; anything else still aborts.
            if not builder_session_timed_out(run_dir):
                raise
            process = adapter.read_json(run_dir / "builder_process" / "harbor_process.json")
            (run_dir / "builder_timeout_freeze.json").write_text(
                json.dumps({"exit_code": process["exit_code"], "reason": "AgentTimeoutError"}) + "\n", encoding="utf-8")
            print("builder_timeout_freeze", flush=True)
        if process["exit_code"] != 0 and not (run_dir / "builder_timeout_freeze.json").is_file():
            raise RuntimeError(f"Builder Harbor Job failed: {process['stderr']}")
        assert_protocol_unchanged(
            expected_sources=source_digests,
            expected_benchmark_digest=benchmark_digest,
            benchmark=benchmark,
            phase="after Builder Job",
        )
        if adapter.tree_digest(public_package) != public_package_digest:
            raise RuntimeError("Builder public package changed during Builder Job")
        if environment_tree_digest(base_env_prefix) != base_environment_digest:
            raise RuntimeError("Builder changed the shared base environment")
        controller.wait_for_active()
        frozen = controller.freeze_after_builder_exit()
    finally:
        controller.stop()

    builder_evidence = collect_builder_trial_evidence(
        job_dir=builder_job_dir,
        expected_agent=args.builder_agent,
        expected_model=args.builder_model,
        expected_harness_version=args.builder_harness_version,
        allow_session_timeout=(run_dir / "builder_timeout_freeze.json").is_file(),
    )
    if frozen is None:
        final = {
            "schema_version": "2.0",
            "status": "builder_artifact_invalid",
            "run_id": run_id,
            "builder": builder_evidence,
            "dev_lifecycle": controller.records,
            "infrastructure_events": controller.infrastructure_events,
            "hidden_scores": [0] * len(adapter.DEFAULT_CASES),
            "hidden_total": 0,
            "hidden_mean": 0.0,
            "failure_manifest": str(run_dir / "freeze_failure.json"),
        }
        adapter.write_json(run_dir / "one_stop_summary.json", final)
        print(json.dumps(final, indent=2, ensure_ascii=False))
        return 0

    frozen_path = Path(frozen["path"])
    frozen_digest = str(frozen["digest"])
    frozen_environment_digest = frozen.get("environment_digest")
    if not isinstance(frozen_environment_digest, str):
        raise RuntimeError("Frozen Builder environment digest is missing")
    if environment_tree_digest(env_prefix) != frozen_environment_digest:
        raise RuntimeError("Builder modified the environment after freeze")
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
    hidden_started_at = adapter.utc_now()
    if datetime.fromisoformat(hidden_started_at) <= datetime.fromisoformat(
        str(frozen["frozen_at"])
    ):
        raise RuntimeError("Hidden evaluation did not start strictly after freeze")
    assert_protocol_unchanged(
        expected_sources=source_digests,
        expected_benchmark_digest=benchmark_digest,
        benchmark=benchmark,
        phase="before hidden evaluation",
    )
    hidden_summary, hidden_dir = run_adapter_phase(
        run_dir=run_dir,
        phase_id=f"{run_id}-hidden",
        candidate=frozen_path,
        cases=None,
        benchmark=benchmark,
        env_prefix=env_prefix,
        trusted_env_prefix=base_env_prefix,
        trusted_env_digest=base_environment_digest,
        credential_file=credential_file,
        jobs_dir=jobs_dir,
        n_concurrent=args.n_concurrent,
    )
    assert_protocol_unchanged(
        expected_sources=source_digests,
        expected_benchmark_digest=benchmark_digest,
        benchmark=benchmark,
        phase="after hidden evaluation",
    )
    hidden_preflight = adapter.read_json(hidden_dir / "preflight.json")
    if hidden_preflight.get("benchmark_digest") != benchmark_digest:
        raise RuntimeError("Hidden evaluation used a different benchmark digest")
    if adapter.tree_digest(frozen_path) != frozen_digest:
        raise RuntimeError("Frozen submission changed during hidden evaluation")
    if environment_tree_digest(env_prefix) != frozen_environment_digest:
        raise RuntimeError("Frozen Builder environment changed during hidden evaluation")
    if environment_tree_digest(base_env_prefix) != base_environment_digest:
        raise RuntimeError("Trusted base environment changed during hidden evaluation")

    final = {
        "schema_version": "2.0",
        "status": "completed",
        "trusted_runtime_prefix": str(base_env_prefix),
        "trusted_runtime_digest": base_environment_digest,
        "trusted_runtime_builder_visible": False,
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
        "hidden_summary": str(hidden_dir / "score_summary.json"),
        "hidden_scores": [case["score"] for case in hidden_summary.get("cases", [])],
        "hidden_total": hidden_summary.get("total_score"),
        "hidden_mean": hidden_summary.get("mean_score"),
        "security_contract": {
            "builder_hidden_mounts": 0,
            "builder_evaluator_mounts": 0,
            "builder_credential_mounts": 0,
            "candidate_credentials": "controlled-role-injection",
            "eval_credentials": "controlled-role-injection",
            "hidden_started_after_freeze": True,
            "frozen_at": frozen["frozen_at"],
            "hidden_started_at": hidden_started_at,
            "builder_environment_writable": True,
            "builder_environment_private": True,
            "frozen_environment_digest": frozen_environment_digest,
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
        raise
