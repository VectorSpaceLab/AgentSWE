#!/usr/bin/env python3
"""Stage and run the Desktop GUI Automation v2 Harbor pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ADAPTER_DIR = Path(__file__).resolve().parent
REFINE_ROOT = Path(os.environ.get("AGENTSWE_CREATION_SHARED", str(ADAPTER_DIR.parent.parent))).resolve()
if str(REFINE_ROOT) not in sys.path:
    sys.path.insert(0, str(REFINE_ROOT))
from trajectory_archive import archive_builder_trajectories
from runtime_contract import prepare_compose, shared_source_digests, assert_runtime_health
from trusted_browser import service_fragment
from agentswe_hosts import judge_environment, render_hosts
from candidate_broker_protocol import (
    BROKER_HOST,
    broker_port,
    candidate_endpoint,
    candidate_credential_file,
    candidate_task_network,
    configure_candidate_compose,
)
HARBOR_ROOT = Path(os.environ.get("AGENTSWE_HARBOR_ROOT", "${AGENTSWE_HOME}/harbor")).resolve()
DEFAULT_BENCHMARK = ADAPTER_DIR.parent / "benchmark"
DEFAULT_CANDIDATE = Path(os.environ.get("AGENTSWE_CANDIDATE", "candidate"))
DEFAULT_ENV_PREFIX = Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "envs" / "desktop-gui-automation-agent-v2"
DEFAULT_CREDENTIAL_FILE = Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "judge.env"
CONTAINER_ENV_PREFIX = Path(
    "/opt/agentswe/benchmark/envs/desktop-gui-automation-agent-v2"
)  # v2-lite (0917): documented-prefix contract (was /opt/agentswe-candidate-runtime)
CONTAINER_CANDIDATE_RUNTIME = CONTAINER_ENV_PREFIX
CONTAINER_TRUSTED_ENV_PREFIX = Path("/opt/agentswe-trusted-runtime")
CONTAINER_CREDENTIAL_FILE = Path("/run/secrets/agentswe.env")
CONTAINER_CANDIDATE_CREDENTIAL_FILE = Path(
    "/opt/agentswe/benchmark/envs/.env"
)
DEFAULT_CASES = tuple(
    item for item in os.environ.get(
        "AGENTSWE_HIDDEN_CASES",
        ",".join(f"test_{index:03d}" for index in range(1, 7)),
    ).split(",") if item
)
DEV_CASES = tuple(
    item for item in os.environ.get(
        "AGENTSWE_DEV_CASES", "dev_001,dev_002"
    ).split(",") if item
)
ALL_CASES = DEV_CASES + DEFAULT_CASES
EVALUATION_MODE = "eval-codex-gui-rubric-v1"
CANDIDATE_MODE = "deterministic-gui-candidate-run-v1"
DIMENSION_MAXIMA = {
    "requested_application_outcome": 35,
    "scope_preservation_side_effect_boundary": 20,
    "workflow_recovery_verification": 15,
    "visual_evidence_quality_decisive_step": 15,
    "trace_fidelity_auditability": 10,
    "artifact_report_validity": 5,
}


def trusted_env_prefix() -> Path:
    """Return the evaluator-owned runtime, never the Builder private prefix."""
    return Path(
        os.environ.get("AGENTSWE_TRUSTED_ENV_PREFIX", str(DEFAULT_ENV_PREFIX))
    ).resolve()


def trusted_runtime_digest() -> str:
    expected = os.environ.get("AGENTSWE_TRUSTED_ENV_DIGEST")
    return expected if expected else tree_digest(trusted_env_prefix())


def trusted_browser_fragment() -> dict[str, Any]:
    """Return a verified browser mount even when sealed files are root-only."""
    try:
        return service_fragment()
    except PermissionError:
        verifier = REFINE_ROOT / "trusted_browser.py"
        completed = subprocess.run(
            ["sudo", "-n", sys.executable, str(verifier)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "trusted browser bundle requires privileged manifest verification: "
                + completed.stderr[-500:]
            )
        record = json.loads(completed.stdout)
        root = Path(record["verified"]).resolve()
        return {
            "volumes": [{"type": "bind", "source": str(root),
                         "target": "/opt/agentswe-trusted-browser", "read_only": True}],
            "environment": {
                "NODE": "/opt/agentswe-trusted-browser/node",
                "CHROMIUM": "/opt/agentswe-trusted-browser/chrome/chrome",
                "LD_LIBRARY_PATH": "/opt/agentswe-trusted-browser/lib",
                "FONTCONFIG_PATH": "/opt/agentswe-trusted-browser/fonts/etc",
                "FONTCONFIG_FILE": "/opt/agentswe-trusted-browser/fonts/etc/fonts.conf",
            },
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }


def installed_chromium_dirs(prefix: Path | None = None) -> list[Path]:
    """Return evaluator-runtime Chromium installations without pinning a revision.

    Playwright revisions are part of the trusted environment snapshot.  The
    current shared runtime carries Chromium 1228, while older benchmark
    snapshots carried 1187; requiring one historical directory makes a valid
    immutable runtime fail preflight before the candidate is evaluated.
    """
    runtime = (prefix or trusted_env_prefix()).resolve()
    roots = (
        runtime / "browsers",
        runtime / ".cache" / "ms-playwright",
        runtime / "playwright-browsers",
    )
    found: set[Path] = set()
    for browsers in roots:
        if not browsers.is_dir():
            continue
        found.update(
            path
            for path in browsers.glob("chromium-*")
            if path.is_dir() and not path.name.startswith("chromium_headless_shell-")
        )
    return sorted(found)


def runtime_paths(prefix: Path | None = None) -> dict[str, Any]:
    """Normalize evaluator runtime locations without pinning a historical layout."""
    runtime = (prefix or trusted_env_prefix()).resolve()
    browser_dirs = installed_chromium_dirs(runtime)
    browser_root = browser_dirs[0].parent if browser_dirs else runtime / "browsers"
    font_dirs = [
        path
        for path in (
            runtime / "fonts",
            runtime / "share" / "fonts",
            runtime / "etc" / "fonts",
        )
        if path.is_dir()
    ]
    python = next(
        (path for path in (runtime / "bin" / "python", runtime / "bin" / "python3") if path.is_file()),
        runtime / "bin" / "python",
    )
    return {
        "host_prefix": str(runtime),
        "python": str(python),
        "browser_root": str(browser_root),
        "browser_dirs": [str(path) for path in browser_dirs],
        "font_dirs": [str(path) for path in font_dirs],
        "fontconfig_path": str(runtime / "etc" / "fonts") if (runtime / "etc" / "fonts").is_dir() else None,
    }


def runtime_environment(prefix: Path, container_prefix: Path) -> dict[str, str]:
    """Return stable in-container paths for browser/font discovery."""
    paths = runtime_paths(prefix)
    runtime = Path(paths["host_prefix"])
    browser_root = Path(paths["browser_root"])
    try:
        browser_target = container_prefix / browser_root.relative_to(runtime)
    except ValueError:
        browser_target = container_prefix / "browsers"
    env = {
        "PYTHONNOUSERSITE": "1",
        "PLAYWRIGHT_BROWSERS_PATH": str(browser_target),
        "AGENTSWE_RUNTIME_PREFIX": str(container_prefix),
    }
    if paths["fontconfig_path"]:
        env["FONTCONFIG_PATH"] = str(container_prefix / "etc" / "fonts")
    if paths["font_dirs"]:
        env["AGENTSWE_FONT_DIRS"] = ":".join(
            str(container_prefix / Path(font).relative_to(runtime))
            for font in paths["font_dirs"]
            if Path(font).is_relative_to(runtime)
        )
    return env


def adapter_source_digests() -> dict[str, str]:
    """Digest executable frozen-candidate evaluation sources only."""
    paths: list[Path] = [ADAPTER_DIR / "adapter.py", ADAPTER_DIR / "run.sh"]
    for directory in (ADAPTER_DIR / "task-template", ADAPTER_DIR / "eval-template"):
        paths.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    digests = {
        path.relative_to(ADAPTER_DIR).as_posix(): file_digest(path)
        for path in sorted(set(paths))
    }
    digests.update(shared_source_digests())
    return digests


def digest_mapping(value: dict[str, str]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def assert_evaluation_protocol_unchanged(
    *, expected_sources: dict[str, str], benchmark: Path,
    expected_benchmark_digest: str, phase: str
) -> None:
    actual_sources = adapter_source_digests()
    if actual_sources != expected_sources:
        changed = sorted(
            key
            for key in set(expected_sources) | set(actual_sources)
            if expected_sources.get(key) != actual_sources.get(key)
        )
        raise RuntimeError(
            f"Evaluation adapter sources changed during {phase}: {changed}"
        )
    actual_benchmark_digest = tree_digest(benchmark)
    if actual_benchmark_digest != expected_benchmark_digest:
        raise RuntimeError(
            f"Benchmark changed during {phase}: expected "
            f"{expected_benchmark_digest}, got {actual_benchmark_digest}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    value = prepare_compose(path, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def tree_digest(root: Path) -> str:
    """Return a stable digest over relative paths, file bytes, and symlink targets."""
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            kind = b"L"
            payload = os.readlink(path).encode("utf-8")
        elif path.is_file():
            digest.update(b"F")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind = b"O"
            payload = b""
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_capture(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=env,
    )
    output = completed.stdout.strip()
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit {completed.returncode}: {command!r}\n{output}"
        )
    return output


def preflight(
    benchmark: Path,
    candidate: Path,
    env_prefix: Path,
    cases: tuple[str, ...],
) -> dict[str, Any]:
    trusted_runtime = runtime_paths()
    candidate_runtime = runtime_paths(env_prefix)
    required_files = [
        candidate / "run_agent.py",
        benchmark / "evaluator" / "harness" / "run_case.py",
        Path(trusted_runtime["python"]),
        Path(candidate_runtime["python"]),
        HARBOR_ROOT / "bin" / "harbor",
        Path("/usr/bin/git"),
        Path("/usr/lib/git-core"),
        Path("/usr/share/git-core"),
    ]
    for case_id in cases:
        case_root = "dev_cases" if case_id in DEV_CASES else "test_cases"
        required_files.extend(
            [
                benchmark / case_root / case_id / "input.md",
                benchmark / case_root / case_id / "assets",
            ]
        )
    missing = [str(path) for path in required_files if not path.exists()]
    if not trusted_runtime["browser_dirs"]:
        missing.append(
            str(trusted_env_prefix() / "{browsers,.cache/ms-playwright}" / "chromium-<playwright-revision>")
        )
    if missing:
        raise FileNotFoundError("Missing required paths:\n" + "\n".join(missing))

    versions = {
        "harbor": run_capture([str(HARBOR_ROOT / "bin" / "harbor"), "--version"]),
        "docker": run_capture(
            ["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"]
        ),
        "git": run_capture(["git", "--version"]),
        "python": run_capture([trusted_runtime["python"], "--version"]),
        "playwright": run_capture([trusted_runtime["python"], "-c", "import playwright; print('installed')"]),
    }
    return {
        "checked_at": utc_now(),
        "benchmark": str(benchmark),
        "benchmark_digest": tree_digest(benchmark),
        "candidate": str(candidate),
        "candidate_digest": tree_digest(candidate),
        "environment_prefix": str(env_prefix),
        "trusted_runtime_prefix": trusted_runtime["host_prefix"],
        "trusted_runtime_digest": trusted_runtime_digest(),
        "runtime_paths": {
            "trusted": trusted_runtime,
            "candidate": candidate_runtime,
            "container_trusted_prefix": str(CONTAINER_TRUSTED_ENV_PREFIX),
            "container_candidate_prefix": str(CONTAINER_ENV_PREFIX),
        },
        "harness_digest": file_digest(benchmark / "evaluator" / "harness" / "run_case.py"),
        "cases": list(cases),
        "versions": versions,
    }


def compose_config(
    candidate: Path,
    case_dir: Path,
    env_prefix: Path,
    harness: Path,
    credential_file: Path = DEFAULT_CREDENTIAL_FILE,
    browser_control: Path | None = None,
    staged_input: Path | None = None,
) -> dict[str, Any]:
    """Physically isolate Candidate, fixture/browser, broker relay, and Harbor.

    ``main`` is a root-owned orchestrator.  It cannot execute Candidate source;
    ``candidate-runner`` is the only service mounting the submission/runtime,
    and ``gui-control`` is the only service mounting the active fixture and
    evaluator-owned Playwright/browser.  The Candidate network is internal and
    exposes only the two named, bounded services.
    """
    browser_control = (browser_control or harness.with_name("browser_control.py")).resolve()
    if not browser_control.is_file():
        raise FileNotFoundError(f"GUI browser control implementation missing: {browser_control}")
    if staged_input is None:
        staged_input = (case_dir / "input.md").resolve()
    staged_input = staged_input.resolve()
    placeholder = candidate_credential_file(credential_file)
    response_path = candidate_endpoint().split(f"http://{BROKER_HOST}:{broker_port()}", 1)[-1]
    evaluation_id = staged_input.parents[3].name if len(staged_input.parents) > 3 else "staged-gui"
    relayed_path = "/context/{}/{}/v1/responses".format(
        urllib.parse.quote(evaluation_id, safe=""), urllib.parse.quote(case_dir.name, safe="")
    )
    relay_endpoint = "http://model-relay:8080" + relayed_path
    shared_environment = runtime_environment(env_prefix, CONTAINER_CANDIDATE_RUNTIME)
    shared_environment.update({
        "CANDIDATE_RUNTIME_PREFIX": str(CONTAINER_CANDIDATE_RUNTIME),
        "AGENTSWE_RESPONSES_BASE_URL": relay_endpoint,
        "GATEWAY_RESPONSES_ENDPOINT": relay_endpoint,
        "AGENTSWE_GATEWAY_ENDPOINT": relay_endpoint,
        "OPENAI_BASE_URL": relay_endpoint.removesuffix("/responses"),
        "OPENAI_API_KEY": "broker-only-placeholder",
        "GATEWAY_API_KEY": "broker-only-placeholder",
        "AGENTSWE_REQUIRED_MODEL": "gpt-5.6-sol",
        "AGENTSWE_REQUIRED_REASONING_EFFORT": "medium",
    })
    trusted_environment = runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX)
    browser_fragment = trusted_browser_fragment()
    trusted_environment.update(browser_fragment["environment"])
    config = {
        "volumes": {
            "gui-control-queue": {},
            "gui-candidate-output": {},
            "gui-trusted-evidence": {},
        },
        "networks": {
            "candidate-control": {"internal": True, "attachable": False},
            "broker-egress": {"internal": False, "attachable": False},
        },
        "services": {
            "main": {
                "image": "${MAIN_IMAGE_NAME}",
                "init": True,
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=64m", "/solution:rw,exec,nosuid,nodev,size=64m"],
                "network_mode": "none",
                "depends_on": {
                    "candidate-runner": {"condition": "service_started"},
                    "gui-control": {"condition": "service_started"},
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(candidate),
                        "target": "/submission",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(staged_input),
                        "target": "/candidate-input/input.md",
                        "read_only": True,
                    },
                    {"type": "volume", "source": "gui-control-queue", "target": "/run-control"},
                    # v2-lite (0917): mount the shared volumes OUTSIDE /logs/artifacts. /logs/artifacts is a Harbor host bind;
                    # volumes nested under it never reach the host copy that the separate verifier receives.
                    # run_candidate.py publishes copies into /logs/artifacts/{candidate_output,trusted_evidence}.
                    {"type": "volume", "source": "gui-candidate-output", "target": "/gui-candidate-output"},
                    {"type": "volume", "source": "gui-trusted-evidence", "target": "/gui-trusted-evidence"},
                ],
            },
            "candidate-runner": {
                "image": "${MAIN_IMAGE_NAME}",
                "command": ["python3", "/isolation/candidate_runner.py"],
                "init": True,
                "user": "0:0",
                "read_only": True,
                "cap_drop": ["ALL"],
                "cap_add": ["SETUID", "SETGID", "KILL", "DAC_OVERRIDE", "CHOWN"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 512,
                "tmpfs": ["/tmp:rw,nosuid,nodev,size=1024m", "/solution:rw,exec,nosuid,nodev,size=64m"],
                "networks": ["candidate-control"],
                "volumes": [
                    {"type": "bind", "source": str(ADAPTER_DIR / "task-template/environment/candidate_runner.py"), "target": "/isolation/candidate_runner.py", "read_only": True},
                    {"type": "bind", "source": str(candidate), "target": "/submission", "read_only": True},
                    {"type": "bind", "source": str(staged_input), "target": "/candidate-input/input.md", "read_only": True},
                    {"type": "bind", "source": str(env_prefix), "target": str(CONTAINER_CANDIDATE_RUNTIME), "read_only": True},
                    {"type": "bind", "source": str(placeholder), "target": str(CONTAINER_CANDIDATE_CREDENTIAL_FILE), "read_only": True},
                    {"type": "bind", "source": "/usr/bin/git", "target": "/usr/bin/git", "read_only": True},
                    {"type": "bind", "source": "/usr/lib/git-core", "target": "/usr/lib/git-core", "read_only": True},
                    {"type": "bind", "source": "/usr/share/git-core", "target": "/usr/share/git-core", "read_only": True},
                    {"type": "volume", "source": "gui-control-queue", "target": "/run-control"},
                    {"type": "volume", "source": "gui-candidate-output", "target": "/candidate-output"},
                ],
                "environment": shared_environment,
            },
            "gui-control": {
                "image": "${MAIN_IMAGE_NAME}",
                "command": [
                    str(CONTAINER_TRUSTED_ENV_PREFIX / "bin/python"),
                    "/isolation/gui_sidecar.py", "--case-dir", "/active-case",
                    "--harness", "/harness/run_case.py", "--browser-control", "/harness/browser_control.py",
                    "--evidence", "/trusted-evidence", "--control", "/run-control",
                    "--candidate-output", "/candidate-output",
                ],
                "init": True,
                "user": "0:0",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 512,
                "tmpfs": ["/tmp:rw,nosuid,nodev,size=1024m", "/solution:rw,exec,nosuid,nodev,size=64m"],
                "networks": ["candidate-control"],
                "volumes": [
                    {"type": "bind", "source": str(ADAPTER_DIR / "task-template/environment/gui_sidecar.py"), "target": "/isolation/gui_sidecar.py", "read_only": True},
                    {"type": "bind", "source": str(case_dir), "target": f"/active-case/{case_dir.name}", "read_only": True},
                    {"type": "bind", "source": str(harness), "target": "/harness/run_case.py", "read_only": True},
                    {"type": "bind", "source": str(browser_control), "target": "/harness/browser_control.py", "read_only": True},
                    {"type": "bind", "source": str(trusted_env_prefix()), "target": str(CONTAINER_TRUSTED_ENV_PREFIX), "read_only": True},
                    {"type": "volume", "source": "gui-control-queue", "target": "/run-control"},
                    {"type": "volume", "source": "gui-candidate-output", "target": "/candidate-output", "read_only": True},
                    {"type": "volume", "source": "gui-trusted-evidence", "target": "/trusted-evidence"},
                    *browser_fragment["volumes"],
                ],
                "environment": trusted_environment,
            },
            "model-relay": {
                "image": "${MAIN_IMAGE_NAME}",
                "command": ["python3", "/relay/model_relay.py"],
                "init": True,
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=16m"],
                "networks": ["candidate-control", "broker-egress"],
                "volumes": [
                    {"type": "bind", "source": str(ADAPTER_DIR / "task-template/environment/model_relay.py"), "target": "/relay/model_relay.py", "read_only": True},
                ],
                "environment": {
                    "BROKER_TARGET_HOST": BROKER_HOST,
                    "BROKER_TARGET_PORT": str(broker_port()),
                    "RELAY_ALLOWED_PATH": relayed_path,
                },
            },
        }
    }
    return config


def candidate_verifier_compose_config(
    *, case_dir: Path, env_prefix: Path, tests_dir: Path | None = None
) -> dict[str, Any]:
    """Compose mounts for the physically separate, offline Candidate verifier."""
    volumes = [
        {
            "type": "bind",
            "source": str(case_dir.resolve()),
            "target": f"/active-case/{case_dir.name}",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(trusted_env_prefix()),
            "target": str(CONTAINER_TRUSTED_ENV_PREFIX),
            "read_only": True,
        },
        {"type": "bind", "source": "/usr/bin/git", "target": "/usr/bin/git", "read_only": True},
        {"type": "bind", "source": "/usr/lib/git-core", "target": "/usr/lib/git-core", "read_only": True},
        {"type": "bind", "source": "/usr/share/git-core", "target": "/usr/share/git-core", "read_only": True},
    ]
    if tests_dir is not None:
        # Harbor may reuse a verifier image across case IDs. These generated
        # files are mounted at runtime so the image cache cannot supply an old
        # case manifest or verifier script.
        for name in ("case_manifest.json",):
            path = tests_dir.resolve() / name
            volumes.append({
                "type": "bind",
                "source": str(path),
                "target": f"/tests/{name}",
                "read_only": True,
            })
    return {
        "services": {
            "main": {
                "volumes": volumes,
                "environment": runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX),
            }
        }
    }


def task_toml(case_id: str) -> str:
    """Backward-compatible candidate task contract used by unit tests."""
    return f'''[task]
name = "local/desktop-gui-automation-v2-{case_id}-0812"
version = "1.0.0"
description = "Harbor isolated candidate run for {case_id}"
authors = [{{ name = "AgentSWE Harbor Adapter" }}]
keywords = ["agentswe", "create-agent", "playwright", "desktop-gui", "0812"]

[metadata]
category = "gui-automation"
difficulty_explanation = "Runs one frozen created GUI agent on one active case."

[agent]
timeout_sec = 660.0
user = "root"
network_mode = "allowlist"
allowed_hosts = ["gateway.example.com", "judge.example.com"]

[verifier]
timeout_sec = 660.0
user = "root"
environment_mode = "separate"
network_mode = "no-network"

[verifier.environment]
network_mode = "no-network"
workdir = "/workspace"

[environment]
network_mode = "allowlist"
allowed_hosts = ["gateway.example.com", "judge.example.com"]
build_timeout_sec = 300.0
cpus = 1
memory_mb = 4096
storage_mb = 8192
workdir = "/workspace"
'''


def candidate_task_toml(case_id: str) -> str:
    """Candidate may call only the provisioned GATEWAY endpoint; verifier is offline."""
    return candidate_task_network(task_toml(case_id)).replace(
        "desktop-gui", "desktop-gui-candidate"
    ).replace(
        "Harbor isolated candidate run", "Harbor isolated candidate run"
    )


def eval_task_toml(case_id: str) -> str:
    """Eval phase can call only the GATEWAY Responses endpoint."""
    return f'''[task]
name = "local/desktop-gui-automation-v2-eval-{case_id}-0812"
version = "1.0.0"
description = "Independent Eval Codex rubric scoring for {case_id}"
authors = [{{ name = "AgentSWE Harbor Adapter" }}]
keywords = ["agentswe", "eval-codex", "playwright", "desktop-gui", "0812"]

[metadata]
category = "gui-automation"
difficulty_explanation = "Scores one immutable candidate artifact on one active case."
evaluation_mode = "{EVALUATION_MODE}"

[agent]
timeout_sec = 1800.0
user = "root"
network_mode = "allowlist"
allowed_hosts = ["gateway.example.com", "judge.example.com"]

[verifier]
timeout_sec = 660.0
user = "root"
environment_mode = "separate"
network_mode = "no-network"

[verifier.environment]
network_mode = "no-network"
workdir = "/workspace"

[environment]
network_mode = "allowlist"
allowed_hosts = ["gateway.example.com", "judge.example.com"]
build_timeout_sec = 300.0
cpus = 1
memory_mb = 4096
storage_mb = 8192
workdir = "/workspace"
'''


def stage_task(
    *,
    case_id: str,
    benchmark: Path,
    candidate: Path,
    env_prefix: Path,
    candidate_digest: str,
    tasks_dir: Path,
    credential_file: Path = DEFAULT_CREDENTIAL_FILE,
) -> Path:
    case_root = "dev_cases" if case_id in DEV_CASES else "test_cases"
    case_dir = (benchmark / case_root / case_id).resolve()
    task_dir = tasks_dir / case_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ADAPTER_DIR / "task-template", task_dir)
    shutil.copy2(
        benchmark / "evaluator" / "harness" / "run_case.py",
        task_dir / "tests" / "run_case.py",
    )
    shutil.copy2(
        benchmark / "evaluator" / "harness" / "__init__.py",
        task_dir / "tests" / "__init__.py",
    )

    case_digest = tree_digest(case_dir)
    source_input = case_dir / "input.md"
    source_text = source_input.read_text(encoding="utf-8")
    staged_text, rewritten_links = re.subn(
        r"(?P<prefix>\[[^\]]*\]\()(?P<target>[^)\s]*assets/serve\.py)(?P<suffix>\))",
        lambda match: match.group("prefix") + "http://gui-control:8765" + match.group("suffix"),
        source_text,
        flags=re.I,
    )
    staged_input = task_dir / "environment" / "staged-input.md"
    staged_input.write_text(staged_text, encoding="utf-8")
    staged_input.chmod(0o444)
    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "case_digest": case_digest,
        "candidate_digest": candidate_digest,
        "source_input_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "staged_input_sha256": hashlib.sha256(staged_text.encode("utf-8")).hexdigest(),
        "application_links_rewritten": rewritten_links,
        "gui_control_protocol": "evaluator-controlled-browser-v1",
        "isolation_protocol": "compose-isolated-gui-v1",
        "evaluation_mode": CANDIDATE_MODE,
        "container_env_prefix": str(CONTAINER_ENV_PREFIX),
        "trusted_wrapper_python": "/usr/bin/python3",
        "credential_path": str(CONTAINER_CANDIDATE_CREDENTIAL_FILE),
    }
    write_json(task_dir / "solution" / "case_manifest.json", manifest)
    write_json(task_dir / "tests" / "case_manifest.json", manifest)
    (task_dir / "task.toml").write_text(render_hosts(candidate_task_toml(case_id)), encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        f"Run the staged frozen candidate once on active case `{case_id}` through "
        "the evaluator-owned controlled browser. Candidate, fixture/browser, and "
        "trusted evidence execute in separate Compose services.\n",
        encoding="utf-8",
    )
    write_json(
        task_dir / "environment" / "docker-compose.yaml",
        compose_config(
            candidate=candidate.resolve(),
            case_dir=case_dir,
            env_prefix=env_prefix.resolve(),
            harness=(benchmark / "evaluator" / "harness" / "run_case.py").resolve(),
            browser_control=(benchmark / "evaluator" / "harness" / "browser_control.py").resolve(),
            staged_input=staged_input,
            credential_file=credential_file.resolve(),
        ),
    )
    write_json(
        task_dir / "tests" / "docker-compose.yaml",
        candidate_verifier_compose_config(
            case_dir=case_dir,
            env_prefix=env_prefix,
            tests_dir=task_dir / "tests",
        ),
    )
    return task_dir


def eval_compose_config(
    *,
    candidate_output: Path,
    case_dir: Path,
    env_prefix: Path,
    credential_file: Path,
    benchmark: Path,
) -> dict[str, Any]:
    """Mount only evaluator inputs; no candidate source is present."""
    evaluator = benchmark / "evaluator"
    volumes = [
        {"type": "bind", "source": str(candidate_output), "target": "/candidate-output", "read_only": True},
        {"type": "bind", "source": str(case_dir), "target": f"/active-case/{case_dir.name}", "read_only": True},
        {"type": "bind", "source": str(evaluator / "eval_prompt.md"), "target": "/evaluator/eval_prompt.md", "read_only": True},
        {"type": "bind", "source": str(evaluator / "rubric.md"), "target": "/evaluator/rubric.md", "read_only": True},
        {"type": "bind", "source": str(evaluator / "harness" / "run_case.py"), "target": "/evaluator/run_case.py", "read_only": True},
        {"type": "bind", "source": str(evaluator / "harness" / "__init__.py"), "target": "/evaluator/__init__.py", "read_only": True},
        {"type": "bind", "source": str(trusted_env_prefix()), "target": str(CONTAINER_TRUSTED_ENV_PREFIX), "read_only": True},
        {"type": "bind", "source": str(credential_file), "target": str(CONTAINER_CREDENTIAL_FILE), "read_only": True},
        {"type": "bind", "source": "/usr/bin/git", "target": "/usr/bin/git", "read_only": True},
        {"type": "bind", "source": "/usr/lib/git-core", "target": "/usr/lib/git-core", "read_only": True},
        {"type": "bind", "source": "/usr/share/git-core", "target": "/usr/share/git-core", "read_only": True},
    ]
    return {
        "services": {
            "main": {
                "volumes": volumes,
                "environment": {**runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX), **judge_environment()},
            }
        }
    }


def eval_verifier_compose_config(
    *, candidate_output: Path, case_dir: Path, env_prefix: Path,
    benchmark: Path, tests_dir: Path | None = None
) -> dict[str, Any]:
    """No-network verifier mounts; notably excludes candidate source and secrets."""
    evaluator = benchmark / "evaluator"
    volumes = [
        {"type": "bind", "source": str(candidate_output.resolve()), "target": "/candidate-output", "read_only": True},
        {"type": "bind", "source": str(case_dir.resolve()), "target": f"/active-case/{case_dir.name}", "read_only": True},
        {"type": "bind", "source": str(evaluator / "eval_prompt.md"), "target": "/evaluator/eval_prompt.md", "read_only": True},
        {"type": "bind", "source": str(evaluator / "rubric.md"), "target": "/evaluator/rubric.md", "read_only": True},
        {"type": "bind", "source": str(evaluator / "harness" / "run_case.py"), "target": "/evaluator/run_case.py", "read_only": True},
        {"type": "bind", "source": str(evaluator / "harness" / "__init__.py"), "target": "/evaluator/__init__.py", "read_only": True},
        {"type": "bind", "source": str(trusted_env_prefix()), "target": str(CONTAINER_TRUSTED_ENV_PREFIX), "read_only": True},
        {"type": "bind", "source": "/usr/bin/git", "target": "/usr/bin/git", "read_only": True},
        {"type": "bind", "source": "/usr/lib/git-core", "target": "/usr/lib/git-core", "read_only": True},
        {"type": "bind", "source": "/usr/share/git-core", "target": "/usr/share/git-core", "read_only": True},
    ]
    if tests_dir is not None:
        for name in ("eval_manifest.json",):
            path = tests_dir.resolve() / name
            volumes.append({
                "type": "bind",
                "source": str(path),
                "target": f"/tests/{name}",
                "read_only": True,
            })
    return {
        "services": {
            "main": {
                "volumes": volumes,
                "environment": runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX),
            }
        }
    }


def stage_eval_task(
    *,
    case_id: str,
    benchmark: Path,
    candidate_output: Path,
    candidate_digest: str,
    candidate_output_digest: str,
    env_prefix: Path,
    credential_file: Path,
    eval_tasks_dir: Path,
    candidate_trial: str,
    candidate_execution_contract: dict[str, Any],
) -> Path:
    case_root = "dev_cases" if case_id in DEV_CASES else "test_cases"
    case_dir = (benchmark / case_root / case_id).resolve()
    task_dir = eval_tasks_dir / case_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ADAPTER_DIR / "eval-template", task_dir)
    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "case_digest": tree_digest(case_dir),
        "candidate_digest": candidate_digest,
        "candidate_output_digest": candidate_output_digest,
        "candidate_trial": candidate_trial,
        "gui_evidence_protocol": "gui-trusted-evidence-v1",
        "candidate_execution_contract": candidate_execution_contract,
        "evaluation_mode": EVALUATION_MODE,
        "container_env_prefix": str(CONTAINER_TRUSTED_ENV_PREFIX),
        "trusted_wrapper_python": "/usr/bin/python3",
        "credential_path": str(CONTAINER_CREDENTIAL_FILE),
        "network_policy": {"mode": "allowlist", "allowed_hosts": ["gateway.example.com", "judge.example.com"]},
        "provider_budget": {"gateway_text": 300, "gateway_image": 100, "serper": 0, "web_retrieval": 0},
    }
    write_json(task_dir / "solution" / "eval_manifest.json", manifest)
    write_json(task_dir / "tests" / "eval_manifest.json", manifest)
    (task_dir / "task.toml").write_text(render_hosts(eval_task_toml(case_id)), encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        "# Independent Eval Codex\n\n"
        f"Score immutable candidate artifacts for active case `{case_id}`. "
        "The evaluator may call only gateway.example.com and must return strict JSON.\n",
        encoding="utf-8",
    )
    # Candidate output is copied into a per-run staging directory and mounted read-only.
    staged_output = task_dir / "input" / "candidate_output"
    shutil.copytree(candidate_output, staged_output)
    (task_dir / "environment" / "docker-compose.yaml").write_text(
        json.dumps(
            eval_compose_config(
                candidate_output=staged_output.resolve(),
                case_dir=case_dir,
                env_prefix=env_prefix.resolve(),
                credential_file=credential_file.resolve(),
                benchmark=benchmark,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(
        task_dir / "tests" / "docker-compose.yaml",
        eval_verifier_compose_config(
            candidate_output=staged_output,
            case_dir=case_dir,
            env_prefix=env_prefix,
            benchmark=benchmark,
            tests_dir=task_dir / "tests",
        ),
    )
    return task_dir


def stage_eval_job(
    *,
    run_dir: Path,
    run_id: str,
    benchmark: Path,
    candidate_job_dir: Path,
    candidate_digest: str,
    env_prefix: Path,
    credential_file: Path,
    cases: tuple[str, ...],
    jobs_dir: Path,
    n_concurrent: int,
) -> tuple[Path, Path, dict[str, str]]:
    eval_tasks_dir = run_dir / "eval_tasks"
    eval_inputs_dir = run_dir / "eval_inputs"
    eval_inputs_dir.mkdir(parents=True, exist_ok=True)
    task_dirs: list[Path] = []
    trial_names: dict[str, str] = {}
    for case_id in cases:
        trial_dirs = [p for p in candidate_job_dir.iterdir() if p.is_dir() and p.name.startswith(case_id + "__")]
        if len(trial_dirs) != 1:
            raise RuntimeError(f"Expected one candidate trial for {case_id}, found {len(trial_dirs)}")
        trial_dir = trial_dirs[0]
        output = trial_dir / "artifacts" / "logs" / "artifacts" / "candidate_output"
        if not output.is_dir():
            raise RuntimeError(f"Candidate output missing for {case_id}: {output}")
        candidate_contract_path = trial_dir / "verifier" / "score_contract.json"
        if not candidate_contract_path.is_file():
            raise RuntimeError(
                f"Candidate execution contract missing for {case_id}: "
                f"{candidate_contract_path}"
            )
        candidate_execution_contract = read_json(candidate_contract_path)
        if candidate_execution_contract.get("case_id") != case_id:
            raise RuntimeError(
                f"Candidate execution contract case mismatch for {case_id}"
            )
        if candidate_execution_contract.get("candidate_digest") != candidate_digest:
            raise RuntimeError(
                f"Candidate execution contract digest mismatch for {case_id}"
            )
        if candidate_execution_contract.get("output_digest") != tree_digest(output):
            raise RuntimeError(
                f"Candidate execution output digest mismatch for {case_id}"
            )
        staged = eval_inputs_dir / case_id / "candidate_output"
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(output, staged)
        trial_names[case_id] = trial_dir.name
        task_dirs.append(
            stage_eval_task(
                case_id=case_id,
                benchmark=benchmark,
                candidate_output=staged,
                candidate_digest=candidate_digest,
                candidate_output_digest=tree_digest(staged),
                env_prefix=env_prefix,
                credential_file=credential_file,
                eval_tasks_dir=eval_tasks_dir,
                candidate_trial=trial_dir.name,
                candidate_execution_contract=candidate_execution_contract,
            )
        )
    job_name = f"desktop-gui-automation-eval-0812-{run_id}"
    config = {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir.resolve()),
        "n_attempts": 1,
        "n_concurrent_trials": n_concurrent,
        "quiet": True,
        "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": True, "force_build": False},
        "agents": [{"name": "oracle"}],
        "tasks": [{"path": str(path.resolve())} for path in task_dirs],
    }
    config_path = run_dir / "eval_job_config.json"
    write_json(config_path, config)
    return config_path, jobs_dir.resolve() / job_name, trial_names


def stage_job(
    *,
    run_dir: Path,
    run_id: str,
    benchmark: Path,
    candidate: Path,
    env_prefix: Path,
    cases: tuple[str, ...],
    candidate_digest: str,
    jobs_dir: Path,
    n_concurrent: int,
    credential_file: Path = DEFAULT_CREDENTIAL_FILE,
) -> tuple[Path, Path]:
    tasks_dir = run_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_dirs = [
        stage_task(
            case_id=case_id,
            benchmark=benchmark,
            candidate=candidate,
            env_prefix=env_prefix,
            candidate_digest=candidate_digest,
            tasks_dir=tasks_dir,
            credential_file=credential_file,
        )
        for case_id in cases
    ]
    job_name = f"desktop-gui-automation-0812-{run_id}"
    config = {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir.resolve()),
        "n_attempts": 1,
        "n_concurrent_trials": n_concurrent,
        "quiet": True,
        "retry": {"max_retries": 0},
        "environment": {
            "type": "docker",
            "delete": True,
            "force_build": False,
        },
        "agents": [{"name": "oracle"}],
        "tasks": [{"path": str(path.resolve())} for path in task_dirs],
    }
    config_path = run_dir / "job_config.json"
    write_json(config_path, config)
    return config_path, jobs_dir.resolve() / job_name


def harbor_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HARBOR_ROOT": str(HARBOR_ROOT),
            "UV_TOOL_DIR": str(HARBOR_ROOT / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(HARBOR_ROOT / "bin"),
            "HARBOR_TELEMETRY": "off",
            "DOCKER_CONFIG": str(HARBOR_ROOT / "docker-config"),
            "PATH": str(HARBOR_ROOT / "bin") + os.pathsep + env.get("PATH", ""),
        }
    )
    return env


def execute_harbor(config_path: Path, run_dir: Path) -> dict[str, Any]:
    command = [str(HARBOR_ROOT / "bin" / "harbor"), "run", "--config", str(config_path)]
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "harbor.stdout.log"
    stderr_path = run_dir / "harbor.stderr.log"
    started_at = utc_now()
    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        completed = subprocess.run(
            command,
            cwd=HARBOR_ROOT,
            env=harbor_environment(),
            text=True,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    process = {
        "command": command,
        "started_at": started_at,
        "finished_at": utc_now(),
        "runtime_seconds": round(time.monotonic() - started, 3),
        "exit_code": completed.returncode,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    process["builder_trajectory_archive"] = archive_builder_trajectories(
        config_path, run_dir
    )
    write_json(run_dir / "harbor_process.json", process)
    assert_runtime_health(config_path, run_dir)
    return process


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_results(
    *,
    job_dir: Path,
    expected_cases: tuple[str, ...],
    expected_candidate_digest: str,
) -> dict[str, Any]:
    if not (job_dir / "result.json").is_file():
        raise FileNotFoundError(f"Harbor job result is missing: {job_dir / 'result.json'}")
    job_result = read_json(job_dir / "result.json")
    cases: dict[str, dict[str, Any]] = {}
    for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir()):
        result_path = trial_dir / "result.json"
        if not result_path.is_file():
            continue
        trial_result = read_json(result_path)
        harness_path = trial_dir / "verifier" / "harness_result.json"
        score_path = trial_dir / "verifier" / "score_contract.json"
        evidence_path = (
            trial_dir
            / "artifacts"
            / "logs"
            / "artifacts"
            / "run_evidence.json"
        )
        if not harness_path.is_file() or not score_path.is_file() or not evidence_path.is_file():
            raise RuntimeError(f"Trial evidence is incomplete: {trial_dir}")
        harness = read_json(harness_path)
        score = read_json(score_path)
        evidence = read_json(evidence_path)
        case_id = str(harness.get("case", ""))
        if case_id not in expected_cases:
            raise RuntimeError(f"Unexpected or missing case ID in {harness_path}: {case_id!r}")
        if case_id in cases:
            raise RuntimeError(f"Duplicate trial for {case_id}")
        if score.get("case_id") != case_id:
            raise RuntimeError(
                f"Candidate score contract case mismatch for {case_id}"
            )
        if evidence.get("candidate_digest_before") != expected_candidate_digest:
            raise RuntimeError(f"Candidate digest mismatch before {case_id}")
        if evidence.get("candidate_digest_after") != expected_candidate_digest:
            raise RuntimeError(f"Candidate changed during {case_id}")
        cases[case_id] = {
            "case_id": case_id,
            "validity_gate": bool(score.get("validity_gate")),
            "score": int(score["score"]),
            "reward": float(score["reward"]),
            "candidate_exit_code": int(evidence["candidate_exit_code"]),
            "trial_name": trial_dir.name,
            "trial_result": str(result_path),
            "harness_result": str(harness_path),
            "score_contract": str(score_path),
            "run_evidence": str(evidence_path),
            "exception_info": trial_result.get("exception_info"),
        }
    missing = sorted(set(expected_cases) - set(cases))
    if missing:
        raise RuntimeError(f"Missing Harbor trials for: {missing}")
    ordered = [cases[case_id] for case_id in expected_cases]
    total = sum(item["score"] for item in ordered)
    return {
        "evaluation_mode": CANDIDATE_MODE,
        "cases": ordered,
        "valid_case_count": sum(1 for item in ordered if item["validity_gate"]),
        "total_score": total,
        "max_total_score": 100 * len(ordered),
        "mean_score": round(total / len(ordered), 1),
        "mean_reward": round(sum(item["reward"] for item in ordered) / len(ordered), 6),
        "harbor_job_result": str(job_dir / "result.json"),
        "harbor_stats": job_result.get("stats"),
    }


def collect_eval_results(
    *,
    job_dir: Path,
    expected_cases: tuple[str, ...],
    expected_candidate_digest: str,
) -> dict[str, Any]:
    """Collect Eval Job artifacts and aggregate only verified contracts."""
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise FileNotFoundError(f"Eval Harbor result is missing: {result_path}")
    job_result = read_json(result_path)
    cases: dict[str, dict[str, Any]] = {}
    for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir()):
        trial_result_path = trial_dir / "result.json"
        contract_path = trial_dir / "verifier" / "score_contract.json"
        eval_path = trial_dir / "artifacts" / "logs" / "artifacts" / "eval" / "eval_result.json"
        if not trial_result_path.is_file():
            continue
        if not contract_path.is_file() or not eval_path.is_file():
            raise RuntimeError(f"Eval evidence is incomplete: {trial_dir}")
        contract = read_json(contract_path)
        evaluation = read_json(eval_path)
        case_id = str(contract.get("case_id", ""))
        if case_id not in expected_cases:
            raise RuntimeError(f"Unexpected case in Eval contract: {case_id!r}")
        if case_id in cases:
            raise RuntimeError(f"Duplicate Eval trial for {case_id}")
        if contract.get("candidate_digest") != expected_candidate_digest:
            raise RuntimeError(f"Candidate digest mismatch in Eval contract for {case_id}")
        # The path inside the container is not host-addressable. Use the staged
        # Eval input recorded beside the generated task and verify its digest at
        # collection time in addition to the manifest value.
        eval_task_path = Path(str(read_json(trial_result_path)["config"]["task"]["path"]))
        host_staged_output = eval_task_path / "input" / "candidate_output"
        if not host_staged_output.is_dir():
            raise RuntimeError(f"Staged Eval output missing for {case_id}")
        if tree_digest(host_staged_output) != contract.get("candidate_output_digest"):
            raise RuntimeError(f"Candidate output changed during Eval for {case_id}")
        cases[case_id] = {
            "case_id": case_id,
            "score": int(contract.get("score", 0)),
            "reward": float(json.loads((trial_dir / "verifier" / "reward.json").read_text())["reward"]),
            "validity_gate": bool(contract.get("validity_gate")),
            "contract_valid": bool(contract.get("contract_valid")),
            "provider_counts": contract.get("provider_counts", {}),
            "trial_name": trial_dir.name,
            "eval_result": str(eval_path),
            "score_contract": str(contract_path),
            "trial_result": str(trial_result_path),
            "eval_errors": evaluation.get("errors", []),
        }
    missing = sorted(set(expected_cases) - set(cases))
    if missing:
        raise RuntimeError(f"Missing Eval Harbor trials for: {missing}")
    ordered = [cases[case_id] for case_id in expected_cases]
    total = sum(item["score"] for item in ordered)
    return {
        "evaluation_mode": EVALUATION_MODE,
        "cases": ordered,
        "valid_case_count": sum(1 for item in ordered if item["validity_gate"]),
        "contract_valid_count": sum(1 for item in ordered if item["contract_valid"]),
        "total_score": total,
        "max_total_score": 100 * len(ordered),
        "mean_score": round(total / len(ordered), 1),
        "mean_reward": round(sum(item["reward"] for item in ordered) / len(ordered), 6),
        "harbor_job_result": str(result_path),
        "harbor_stats": job_result.get("stats"),
        "api_totals": {
            key: sum(int(item["provider_counts"].get(key, 0)) for item in ordered)
            for key in ("gateway_text", "gateway_image", "serper", "web_retrieval")
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Desktop GUI Automation v2 Harbor candidate and Eval Jobs."
    )
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--env-prefix", type=Path, default=DEFAULT_ENV_PREFIX)
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--jobs-dir", type=Path, default=HARBOR_ROOT / "jobs")
    parser.add_argument("--runs-dir", type=Path, default=ADAPTER_DIR / "runs")
    parser.add_argument("--run-id")
    parser.add_argument("--cases", nargs="+", default=list(DEFAULT_CASES))
    parser.add_argument("--n-concurrent", type=int, default=2)
    parser.add_argument("--stage-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.n_concurrent <= 2:
        raise SystemExit("--n-concurrent must be 1 or 2 on this host")
    cases = tuple(dict.fromkeys(args.cases))
    unknown = sorted(set(cases) - set(ALL_CASES))
    if unknown:
        raise SystemExit(f"Supported cases are dev_001/dev_002 and test_001..test_006: {unknown}")
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.runs_dir.resolve() / run_id
    if run_dir.exists():
        raise SystemExit(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    benchmark = args.benchmark.resolve()
    candidate = args.candidate.resolve()
    env_prefix = args.env_prefix.resolve()
    credential_file = args.credential_file.resolve()
    if not credential_file.is_file():
        raise FileNotFoundError(f"Credential file is missing: {credential_file}")
    # The file is intentionally root-readable only. Do not read, copy, or validate
    # its contents in the adapter process; Docker mounts it into the Eval container.
    preflight_result = preflight(benchmark, candidate, env_prefix, cases)
    write_json(run_dir / "preflight.json", preflight_result)
    candidate_digest = preflight_result["candidate_digest"]
    source_digests = adapter_source_digests()
    write_json(
        run_dir / "evaluation_protocol_lock.json",
        {
            "schema_version": "1.0",
            "created_at": utc_now(),
            "benchmark_digest": preflight_result["benchmark_digest"],
            "adapter_source_digest": digest_mapping(source_digests),
            "adapter_source_digests": source_digests,
            "cases": list(cases),
        },
    )

    config_path, job_dir = stage_job(
        run_dir=run_dir,
        run_id=run_id,
        benchmark=benchmark,
        candidate=candidate,
        env_prefix=env_prefix,
        cases=cases,
        candidate_digest=candidate_digest,
        jobs_dir=args.jobs_dir,
        n_concurrent=args.n_concurrent,
        credential_file=credential_file,
    )
    if args.stage_only:
        write_json(
            run_dir / "stage_result.json",
            {
                "status": "staged",
                "run_id": run_id,
                "job_config": str(config_path),
                "expected_job_dir": str(job_dir),
            },
        )
        print(run_dir)
        return 0

    assert_evaluation_protocol_unchanged(
        expected_sources=source_digests,
        benchmark=benchmark,
        expected_benchmark_digest=preflight_result["benchmark_digest"],
        phase="before Candidate Job",
    )

    process = execute_harbor(config_path, run_dir)
    if process["exit_code"] != 0:
        raise RuntimeError(
            f"Harbor job failed with exit {process['exit_code']}; see {process['stderr']}"
        )
    summary = collect_results(
        job_dir=job_dir,
        expected_cases=cases,
        expected_candidate_digest=candidate_digest,
    )
    _infra_cases_path = run_dir / "infra_failed_cases.json"
    if _infra_cases_path.is_file():
        _failed_cases = set(json.loads(_infra_cases_path.read_text()))
        cases = tuple(c for c in cases if c not in _failed_cases)
        if not cases:
            raise RuntimeError("Create runtime infrastructure failure; score withheld: all cases had broker failures")
    candidate_digest_after = tree_digest(candidate)
    if trusted_runtime_digest() != preflight_result["trusted_runtime_digest"]:
        raise RuntimeError("Trusted runtime changed during Candidate Job")
    if candidate_digest_after != candidate_digest:
        raise RuntimeError("Candidate tree changed during the Harbor evaluation")
    assert_evaluation_protocol_unchanged(
        expected_sources=source_digests,
        benchmark=benchmark,
        expected_benchmark_digest=preflight_result["benchmark_digest"],
        phase="after Candidate Job",
    )
    eval_config_path, eval_job_dir, eval_trial_names = stage_eval_job(
        run_dir=run_dir,
        run_id=run_id,
        benchmark=benchmark,
        candidate_job_dir=job_dir,
        candidate_digest=candidate_digest,
        env_prefix=env_prefix,
        credential_file=credential_file,
        cases=cases,
        jobs_dir=args.jobs_dir,
        n_concurrent=args.n_concurrent,
    )
    eval_run_dir = run_dir / "eval-run"
    assert_evaluation_protocol_unchanged(
        expected_sources=source_digests,
        benchmark=benchmark,
        expected_benchmark_digest=preflight_result["benchmark_digest"],
        phase="before Eval Job",
    )
    eval_process = execute_harbor(eval_config_path, eval_run_dir)
    if eval_process["exit_code"] != 0:
        raise RuntimeError(
            f"Eval Harbor job failed with exit {eval_process['exit_code']}; see {eval_process['stderr']}"
        )
    eval_summary = collect_eval_results(
        job_dir=eval_job_dir,
        expected_cases=cases,
        expected_candidate_digest=candidate_digest,
    )
    if trusted_runtime_digest() != preflight_result["trusted_runtime_digest"]:
        raise RuntimeError("Trusted runtime changed during Eval Job")
    assert_evaluation_protocol_unchanged(
        expected_sources=source_digests,
        benchmark=benchmark,
        expected_benchmark_digest=preflight_result["benchmark_digest"],
        phase="after Eval Job",
    )
    summary = {
        "schema_version": "1.1",
        "evaluation_mode": EVALUATION_MODE,
        "candidate_run": summary,
        "eval_run": eval_summary,
        "cases": eval_summary["cases"],
        "total_score": eval_summary["total_score"],
        "max_total_score": eval_summary["max_total_score"],
        "mean_score": eval_summary["mean_score"],
        "mean_reward": eval_summary["mean_reward"],
    }
    write_json(run_dir / "score_summary.json", summary)
    manifest = {
        "schema_version": "1.0",
        "adapter": "desktop-gui-automation-harbor-full-0812",
        "adapter_digest": digest_mapping(source_digests),
        "adapter_source_digests": source_digests,
        "evaluation_protocol_lock": str(
            run_dir / "evaluation_protocol_lock.json"
        ),
        "run_id": run_id,
        "started_from_preflight": preflight_result["checked_at"],
        "finished_at": utc_now(),
        "evaluation_mode": EVALUATION_MODE,
        "leaderboard_eligible": bool(eval_summary["contract_valid_count"] == len(cases)),
        "leaderboard_ineligibility_reason": (
            None
            if eval_summary["contract_valid_count"] == len(cases)
            else "One or more score contracts failed deterministic verification."
        ),
        "benchmark": str(benchmark),
        "benchmark_digest": preflight_result["benchmark_digest"],
        "candidate": str(candidate),
        "candidate_digest_before": candidate_digest,
        "candidate_digest_after": candidate_digest_after,
        "environment_prefix": str(env_prefix),
        "trusted_runtime_prefix": preflight_result["trusted_runtime_prefix"],
        "trusted_runtime_digest": preflight_result["trusted_runtime_digest"],
        "trusted_runtime_unchanged": True,
        "harness_digest": preflight_result["harness_digest"],
        "job_config": str(config_path),
        "harbor_job_dir": str(job_dir),
        "harbor_process": str(run_dir / "harbor_process.json"),
        "eval_job_config": str(eval_config_path),
        "eval_harbor_job_dir": str(eval_job_dir),
        "eval_harbor_process": str(eval_run_dir / "harbor_process.json"),
        "eval_trial_names": eval_trial_names,
        "credential_file": str(credential_file),
        "credential_values_emitted": False,
        "score_summary": str(run_dir / "score_summary.json"),
        "cases": list(cases),
        "n_concurrent": args.n_concurrent,
    }
    write_json(run_dir / "run_manifest.json", manifest)
    (ADAPTER_DIR / "LATEST_RUN").write_text(str(run_dir) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        raise
