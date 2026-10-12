#!/usr/bin/env python3
"""Stage and run the Document to Editable PPTX v2 Harbor benchmark."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ADAPTER_DIR = Path(__file__).resolve().parent
REFINE_ROOT = Path(os.environ.get("AGENTSWE_CREATION_SHARED", str(ADAPTER_DIR.parent.parent))).resolve()
if str(REFINE_ROOT) not in sys.path:
    sys.path.insert(0, str(REFINE_ROOT))
from trajectory_archive import archive_builder_trajectories
from runtime_contract import prepare_compose, shared_source_digests, assert_runtime_health
from agentswe_hosts import judge_environment, render_hosts
from candidate_broker_protocol import (
    candidate_task_network,
    configure_candidate_compose,
)
HARBOR_ROOT = Path(os.environ.get("AGENTSWE_HARBOR_ROOT", "${AGENTSWE_HOME}/harbor")).resolve()
DEFAULT_BENCHMARK = ADAPTER_DIR.parent / "benchmark"
DEFAULT_CANDIDATE = Path(os.environ.get("AGENTSWE_CANDIDATE", "candidate"))
DEFAULT_ENV_PREFIX = Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "envs" / "document-to-editable-pptx-agent-v2"
DEFAULT_CREDENTIAL_FILE = Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "judge.env"
CONTAINER_ENV_PREFIX = Path(
    "/opt/agentswe/benchmark/"
    "agent-create-0804/envs/"
    "document-to-editable-pptx-agent-v2"
)
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
GATEWAY_HOST = "gateway.example.com"
SERPER_HOST = "search.example.com"
GATEWAY_ENDPOINT = "https://gateway.example.com/v1/responses"
SERPER_ENDPOINT = "https://search.example.com/serp_search_v1"
EVALUATOR_LIBREOFFICE_ROOT = Path(
    "${AGENTSWE_HOME}/benchmark/agent-create-0804/"
    "tools/libreoffice/root"
)
EVALUATION_MODE = "eval-codex-pptx-rubric-v1"
CANDIDATE_MODE = "deterministic-pptx-validation-v1"
DIMENSION_MAXIMA = {
    "evidence_correctness_analytical_integrity": 25,
    "case_compliance_narrative": 16,
    "visual_storytelling_analytical_representation": 17,
    "ooxml_validity_editability_technical_construction": 18,
    "provenance_auditability": 14,
    "rendered_usability_notes_accessibility": 10,
}


def trusted_env_prefix() -> Path:
    """Return the evaluator-owned runtime, never the Builder private prefix."""
    return Path(
        os.environ.get("AGENTSWE_TRUSTED_ENV_PREFIX", str(DEFAULT_ENV_PREFIX))
    ).resolve()


def trusted_runtime_digest() -> str:
    expected = os.environ.get("AGENTSWE_TRUSTED_ENV_DIGEST")
    return expected if expected else tree_digest(trusted_env_prefix())


def evaluator_libreoffice_root() -> Path:
    """Resolve the evaluator renderer from the host or an explicit override."""
    candidates = [
        Path(value)
        for value in (os.environ.get("AGENTSWE_LIBREOFFICE_ROOT"),)
        if value
    ] + [
        EVALUATOR_LIBREOFFICE_ROOT,
        Path("${AGENTSWE_HOME}/AgentSWE/tools/libreoffice/root"),
        Path("${AGENTSWE_HOME}/benchmark/tools/libreoffice/root"),
    ]
    for candidate in candidates:
        if (candidate / "opt/libreoffice25.8/program/soffice").is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def runtime_paths(prefix: Path | None = None) -> dict[str, Any]:
    """Normalize Python, browser, font, Poppler, and renderer locations."""
    runtime = (prefix or trusted_env_prefix()).resolve()
    browser_roots = (
        runtime / "browsers",
        runtime / ".cache" / "ms-playwright",
        runtime / "playwright-browsers",
    )
    browser_dirs = sorted(
        {
            path
            for root in browser_roots
            if root.is_dir()
            for path in root.glob("chromium-*")
            if path.is_dir()
        }
    )
    font_dirs = [
        path
        for path in (runtime / "fonts", runtime / "share" / "fonts", runtime / "etc" / "fonts")
        if path.is_dir()
    ]
    python = next(
        (path for path in (runtime / "bin" / "python", runtime / "bin" / "python3") if path.is_file()),
        runtime / "bin" / "python",
    )
    pdftoppm = next(
        (path for path in (runtime / "bin" / "pdftoppm", Path(shutil.which("pdftoppm") or "")) if path.is_file()),
        runtime / "bin" / "pdftoppm",
    )
    renderer_root = evaluator_libreoffice_root()
    return {
        "host_prefix": str(runtime),
        "python": str(python),
        "pdftoppm": str(pdftoppm),
        "browser_root": str(browser_dirs[0].parent) if browser_dirs else str(runtime / "browsers"),
        "browser_dirs": [str(path) for path in browser_dirs],
        "font_dirs": [str(path) for path in font_dirs],
        "fontconfig_path": str(runtime / "etc" / "fonts") if (runtime / "etc" / "fonts").is_dir() else None,
        "libreoffice_root": str(renderer_root),
        "libreoffice_binary": str(renderer_root / "opt/libreoffice25.8/program/soffice"),
    }


def runtime_environment(prefix: Path, container_prefix: Path) -> dict[str, str]:
    paths = runtime_paths(prefix)
    runtime = Path(paths["host_prefix"])
    browser_root = Path(paths["browser_root"])
    pdftoppm = Path(paths["pdftoppm"])
    try:
        browser_target = container_prefix / browser_root.relative_to(runtime)
    except ValueError:
        browser_target = container_prefix / "browsers"
    try:
        pdftoppm_target = container_prefix / pdftoppm.relative_to(runtime)
    except ValueError:
        pdftoppm_target = container_prefix / "bin" / "pdftoppm"
    environment = {
        "PYTHONNOUSERSITE": "1",
        "PLAYWRIGHT_BROWSERS_PATH": str(browser_target),
        "AGENTSWE_RUNTIME_PREFIX": str(container_prefix),
        "AGENTSWE_PDFTOPPM": str(pdftoppm_target),
    }
    if paths["fontconfig_path"]:
        environment["FONTCONFIG_PATH"] = str(container_prefix / "etc" / "fonts")
    if paths["font_dirs"]:
        environment["AGENTSWE_FONT_DIRS"] = ":".join(
            str(container_prefix / Path(font).relative_to(runtime))
            for font in paths["font_dirs"]
            if Path(font).is_relative_to(runtime)
        )
    return environment


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
        benchmark / "evaluator" / "validate_pptx.py",
        benchmark / "evaluator" / "case_expectations.json",
        benchmark / "evaluator" / "render_pptx.py",
        benchmark / "evaluator" / "render_health.pptx",
        benchmark / "evaluator" / "evidence_bundle.py",
        Path(trusted_runtime["python"]),
        Path(trusted_runtime["pdftoppm"]),
        Path(trusted_runtime["libreoffice_binary"]),
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
                benchmark / case_root / case_id,
            ]
        )
    missing = [str(path) for path in required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required paths:\n" + "\n".join(missing))

    versions = {
        "harbor": run_capture([str(HARBOR_ROOT / "bin" / "harbor"), "--version"]),
        "docker": run_capture(
            ["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"]
        ),
        "git": run_capture(["git", "--version"]),
        "python": run_capture([trusted_runtime["python"], "--version"]),
        "libreoffice": run_capture(["libreoffice", "--version"]) if shutil.which("libreoffice") else "unavailable",
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
            "container_renderer_root": "/tools/libreoffice/root",
        },
        "harness_digest": file_digest(benchmark / "evaluator" / "validate_pptx.py"),
        "cases": list(cases),
        "versions": versions,
    }


def compose_config(
    candidate: Path,
    case_dir: Path,
    env_prefix: Path,
    credential_file: Path = DEFAULT_CREDENTIAL_FILE,
) -> dict[str, Any]:
    config = {
        "services": {
            "main": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(candidate),
                        "target": "/submission",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(case_dir),
                        "target": f"/active-case/{case_dir.name}",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(env_prefix),
                        "target": str(CONTAINER_ENV_PREFIX),
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(credential_file),
                        "target": str(CONTAINER_CANDIDATE_CREDENTIAL_FILE),
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/usr/bin/git",
                        "target": "/usr/bin/git",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/usr/lib/git-core",
                        "target": "/usr/lib/git-core",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/usr/share/git-core",
                        "target": "/usr/share/git-core",
                        "read_only": True,
                    },
                ],
                "environment": {
                    **runtime_environment(env_prefix, CONTAINER_ENV_PREFIX),
                    "PYTHONNOUSERSITE": "1",
                    "AGENTSWE_GATEWAY_ENDPOINT": GATEWAY_ENDPOINT,
                    "AGENTSWE_SERPER_ENDPOINT": SERPER_ENDPOINT,
                },
            }
        }
    }
    return configure_candidate_compose(config)


def candidate_verifier_compose_config(
    *, case_dir: Path, env_prefix: Path, benchmark: Path = DEFAULT_BENCHMARK,
    tests_dir: Path | None = None
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
        {
            "type": "bind",
            "source": str((benchmark / "evaluator" / "validate_pptx.py").resolve()),
            "target": "/evaluator/validate_pptx.py",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str((benchmark / "evaluator" / "case_expectations.json").resolve()),
            "target": "/evaluator/case_expectations.json",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str((benchmark / "evaluator" / "render_pptx.py").resolve()),
            "target": "/evaluator/render_pptx.py",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": str(evaluator_libreoffice_root()),
            "target": "/tools/libreoffice/root",
            "read_only": True,
        },
        {"type": "bind", "source": "/usr/bin/git", "target": "/usr/bin/git", "read_only": True},
        {"type": "bind", "source": "/usr/lib/git-core", "target": "/usr/lib/git-core", "read_only": True},
        {"type": "bind", "source": "/usr/share/git-core", "target": "/usr/share/git-core", "read_only": True},
    ]
    volumes.extend({"type": "bind", "source": str((benchmark / "evaluator" / name).resolve()),
                    "target": "/evaluator/" + name, "read_only": True}
                   for name in ("render_health.pptx", "evidence_bundle.py"))
    dependency_root = Path(os.environ.get('AGENTSWE_EVALUATOR_DEPENDENCIES', '${AGENTSWE_HOME}/agentSWE/0910-create-evaluator-v1/runtime-dependencies-v1'))
    for source, target in ((dependency_root / 'pptx/lib', '/opt/pptx-libs'),
                           (dependency_root / 'fonts/share', '/usr/share/fonts'),
                           (dependency_root / 'fonts/etc', '/etc/fonts')):
        if not source.is_dir():
            raise RuntimeError('trusted PPTX dependency missing: ' + str(source))
        volumes.append({'type': 'bind', 'source': str(source), 'target': target, 'read_only': True})
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
                "environment": {
                    **runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX),
                    "PYTHONNOUSERSITE": "1",
                    "LD_LIBRARY_PATH": "/opt/pptx-libs:/opt/agentswe-trusted-runtime/lib",
                    "FONTCONFIG_PATH": "/etc/fonts",
                    "FONTCONFIG_FILE": "/etc/fonts/fonts.conf",
                },
            }
        }
    }


def task_toml(case_id: str) -> str:
    """Backward-compatible candidate task contract used by unit tests."""
    return f'''[task]
name = "local/document-to-editable-pptx-v2-{case_id}-0812"
version = "1.0.0"
description = "Harbor isolated candidate run for {case_id}"
authors = [{{ name = "AgentSWE Harbor Adapter" }}]
keywords = ["agentswe", "create-agent", "pptx", "pilot-0812"]

[metadata]
category = "document-generation"
difficulty_explanation = "Runs one frozen created presentation agent on one active PPTX case."
resource_endpoints = ["{GATEWAY_ENDPOINT}", "{SERPER_ENDPOINT}", "public-http-https"]

[agent]
timeout_sec = 660.0
user = "root"
network_mode = "public"

[verifier]
timeout_sec = 660.0
user = "root"
environment_mode = "separate"
network_mode = "no-network"

[verifier.environment]
network_mode = "no-network"
workdir = "/workspace"

[environment]
network_mode = "public"
build_timeout_sec = 300.0
cpus = 1
memory_mb = 4096
storage_mb = 8192
workdir = "/workspace"
'''


def candidate_task_toml(case_id: str) -> str:
    """Candidate can use GATEWAY, Serper, and authorized public retrieval; verifier is offline."""
    return candidate_task_network(task_toml(case_id)).replace(
        "pilot-0812", "candidate-run-0812"
    ).replace(
        "Harbor isolated candidate run", "Harbor isolated candidate run"
    )


def eval_task_toml(case_id: str) -> str:
    """Eval phase can call only the GATEWAY Responses endpoint."""
    return f'''[task]
name = "local/document-to-editable-pptx-v2-eval-{case_id}-0812"
version = "1.0.0"
description = "Independent Eval Codex rubric scoring for {case_id}"
authors = [{{ name = "AgentSWE Harbor Adapter" }}]
keywords = ["agentswe", "eval-codex", "pptx", "0812"]

[metadata]
category = "document-generation"
difficulty_explanation = "Scores one immutable candidate artifact on one active case."
evaluation_mode = "{EVALUATION_MODE}"
resource_endpoints = ["{GATEWAY_ENDPOINT}"]

[agent]
timeout_sec = 1800.0
user = "root"
network_mode = "public"

[verifier]
timeout_sec = 660.0
user = "root"
environment_mode = "separate"
network_mode = "no-network"

[verifier.environment]
network_mode = "no-network"
workdir = "/workspace"

[environment]
network_mode = "public"
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
    shutil.copy2(benchmark / "evaluator" / "validate_pptx.py", task_dir / "tests" / "validate_pptx.py")
    for name in ("case_expectations.json", "render_pptx.py", "render_health.pptx", "evidence_bundle.py"):
        source = benchmark / "evaluator" / name
        if source.is_file(): shutil.copy2(source, task_dir / "tests" / name)

    case_digest = tree_digest(case_dir)
    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "case_digest": case_digest,
        "candidate_digest": candidate_digest,
        "evaluation_mode": CANDIDATE_MODE,
        "container_env_prefix": str(CONTAINER_ENV_PREFIX),
        "trusted_wrapper_python": "/usr/bin/python3",
        "credential_path": str(CONTAINER_CANDIDATE_CREDENTIAL_FILE),
        "required_resource_environment": ["GATEWAY_API_KEY", "SERPER_TOKEN"],
        "resource_endpoints": {
            "gateway": GATEWAY_ENDPOINT,
            "serper": SERPER_ENDPOINT,
        },
        "network_policy": {
            "mode": "public",
            "required_hosts": [GATEWAY_HOST, SERPER_HOST],
            "public_retrieval": True,
        },
    }
    write_json(task_dir / "solution" / "case_manifest.json", manifest)
    write_json(task_dir / "tests" / "case_manifest.json", manifest)
    (task_dir / "task.toml").write_text(render_hosts(candidate_task_toml(case_id)), encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        "# Frozen candidate execution\n\n"
        f"Run the staged frozen candidate once on active case `{case_id}`. "
        "The Harbor verifier applies deterministic OOXML, render, manifest and accessibility checks.\n",
        encoding="utf-8",
    )
    write_json(
        task_dir / "environment" / "docker-compose.yaml",
        compose_config(
            candidate.resolve(),
            case_dir,
            env_prefix.resolve(),
            credential_file.resolve(),
        ),
    )
    write_json(
        task_dir / "tests" / "docker-compose.yaml",
        candidate_verifier_compose_config(
            case_dir=case_dir,
            env_prefix=env_prefix,
            benchmark=benchmark,
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
    trusted_evidence: Path | None = None,
) -> dict[str, Any]:
    """Mount only evaluator inputs; no candidate source is present."""
    evaluator = benchmark / "evaluator"
    volumes = [
        {"type": "bind", "source": str(candidate_output), "target": "/candidate-output", "read_only": True},
        {"type": "bind", "source": str(case_dir), "target": f"/active-case/{case_dir.name}", "read_only": True},
        {"type": "bind", "source": str(evaluator / "eval_prompt.md"), "target": "/evaluator/eval_prompt.md", "read_only": True},
        {"type": "bind", "source": str(evaluator / "rubric.md"), "target": "/evaluator/rubric.md", "read_only": True},
        {"type": "bind", "source": str(evaluator / "validate_pptx.py"), "target": "/evaluator/validate_pptx.py", "read_only": True},
        {"type": "bind", "source": str(evaluator / "case_expectations.json"), "target": "/evaluator/case_expectations.json", "read_only": True},
        {"type": "bind", "source": str(evaluator / "render_pptx.py"), "target": "/evaluator/render_pptx.py", "read_only": True},
        {"type": "bind", "source": str(evaluator_libreoffice_root()), "target": "/tools/libreoffice/root", "read_only": True},
        {"type": "bind", "source": str(trusted_env_prefix()), "target": str(CONTAINER_TRUSTED_ENV_PREFIX), "read_only": True},
        {"type": "bind", "source": str(credential_file), "target": str(CONTAINER_CREDENTIAL_FILE), "read_only": True},
        {"type": "bind", "source": "/usr/bin/git", "target": "/usr/bin/git", "read_only": True},
        {"type": "bind", "source": "/usr/lib/git-core", "target": "/usr/lib/git-core", "read_only": True},
        {"type": "bind", "source": "/usr/share/git-core", "target": "/usr/share/git-core", "read_only": True},
    ]
    if trusted_evidence is not None:
        volumes.append({"type": "bind", "source": str(trusted_evidence.resolve()), "target": "/trusted-evidence", "read_only": True})
    return {
        "services": {
            "main": {
                "volumes": volumes,
                "environment": {
                    **runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX),
                    "PYTHONNOUSERSITE": "1",
                    **judge_environment(),
                },
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
        {"type": "bind", "source": str(evaluator / "validate_pptx.py"), "target": "/evaluator/validate_pptx.py", "read_only": True},
        {"type": "bind", "source": str(evaluator / "case_expectations.json"), "target": "/evaluator/case_expectations.json", "read_only": True},
        {"type": "bind", "source": str(evaluator / "render_pptx.py"), "target": "/evaluator/render_pptx.py", "read_only": True},
        {"type": "bind", "source": str(evaluator_libreoffice_root()), "target": "/tools/libreoffice/root", "read_only": True},
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
                "environment": {
                    **runtime_environment(trusted_env_prefix(), CONTAINER_TRUSTED_ENV_PREFIX),
                    "PYTHONNOUSERSITE": "1",
                },
            }
        }
    }


def file_digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def trusted_evidence_path(verifier_dir, contract):
    descriptor = contract.get('trusted_harness_result', {}).get('visual_evidence')
    if descriptor is None and contract.get('fatal_gate') is True:
        return None
    if not isinstance(descriptor, dict):
        raise RuntimeError('missing trusted visual descriptor')
    name = descriptor.get('directory')
    if not isinstance(name, str) or Path(name).name != name or not name.startswith('evidence-'):
        raise RuntimeError('unsafe trusted visual directory')
    return verifier_dir / name


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
    trusted_evidence_root: Path | None = None,
) -> Path:
    case_root = "dev_cases" if case_id in DEV_CASES else "test_cases"
    case_dir = (benchmark / case_root / case_id).resolve()
    task_dir = eval_tasks_dir / case_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ADAPTER_DIR / "eval-template", task_dir)
    shutil.copy2(benchmark / "evaluator" / "evidence_bundle.py", task_dir / "solution" / "evidence_bundle.py")
    execution = candidate_execution_contract
    trusted = execution.get('trusted_harness_result', {})
    if not isinstance(trusted, dict) or trusted.get('case') != case_id:
        raise RuntimeError('trusted PPTX verifier evidence missing')
    if file_digest_json(trusted) != execution.get('trusted_harness_result_sha256'):
        raise RuntimeError('trusted PPTX verifier evidence hash mismatch')
    if execution.get('case_digest') != tree_digest(case_dir):
        raise RuntimeError('trusted PPTX verifier case digest mismatch')
    descriptor = trusted.get('visual_evidence')
    staged_evidence = task_dir / 'input' / 'trusted_evidence'
    if execution.get('fatal_gate') is not True:
        if trusted_evidence_root is None or not isinstance(descriptor, dict):
            raise RuntimeError('trusted PPTX visual evidence missing')
        spec = importlib.util.spec_from_file_location('pptx_evidence', benchmark / 'evaluator' / 'evidence_bundle.py')
        reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader)
        bundle = reader.load_bundle(trusted_evidence_root, descriptor)
        if bundle.get('case_digest') != tree_digest(case_dir) or bundle.get('output_digest') != candidate_output_digest:
            raise RuntimeError('trusted PPTX visual identity mismatch')
        shutil.copytree(trusted_evidence_root, staged_evidence)
        reader.load_bundle(staged_evidence, descriptor)
        research_spec = importlib.util.spec_from_file_location('pptx_source_research', benchmark / 'evaluator' / 'source_research.py')
        research = importlib.util.module_from_spec(research_spec)
        research_spec.loader.exec_module(research)
        bundle['independent_sources'] = research.collect(case_dir, candidate_output, staged_evidence)
        # Host adds independently retrieved source bytes to the offline slide seal.
        # No submitted program is executed and no judge credential is provided here.
        (staged_evidence / 'evidence.json').unlink()
        descriptor = reader.seal(staged_evidence, bundle)
        trusted = dict(trusted, visual_evidence=descriptor)
        candidate_execution_contract = dict(candidate_execution_contract, trusted_harness_result=trusted,
            trusted_harness_result_sha256=file_digest_json(trusted))
    else:
        staged_evidence.mkdir(parents=True, exist_ok=False)
        bundle = {}
    manifest = {
        "schema_version": "1.0",
        "case_id": case_id,
        "case_digest": tree_digest(case_dir),
        "candidate_digest": candidate_digest,
        "candidate_output_digest": candidate_output_digest,
        "candidate_trial": candidate_trial,
        "visual_evidence_protocol": "pptx-visual-evidence-v1",
        "visual_evidence": descriptor,
        "visual_image_records": bundle.get("images", []),
        "candidate_execution_contract": candidate_execution_contract,
        "evaluation_mode": EVALUATION_MODE,
        "container_env_prefix": str(CONTAINER_TRUSTED_ENV_PREFIX),
        "trusted_wrapper_python": "/usr/bin/python3",
        "credential_path": str(CONTAINER_CREDENTIAL_FILE),
        "network_policy": {
            "mode": "public",
            "required_hosts": [GATEWAY_HOST, SERPER_HOST, "public-http-https"],
        },
        "provider_budget": {"gateway_text": 300, "gateway_image": 100, "serper": None, "web_retrieval": None},
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
                trusted_evidence=staged_evidence,
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
                trusted_evidence_root=trusted_evidence_path(trial_dir / 'verifier', candidate_execution_contract),
            )
        )
    job_name = f"document-to-editable-pptx-eval-0812-{run_id}"
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
    job_name = f"document-to-editable-pptx-0812-{run_id}"
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
        description="Run the Document to Editable PPTX v2 Harbor candidate and Eval Jobs."
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
        "adapter": "document-to-editable-pptx-harbor-full-0812",
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
