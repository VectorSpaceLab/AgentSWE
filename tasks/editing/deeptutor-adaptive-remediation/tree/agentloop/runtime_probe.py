#!/usr/bin/env python3
"""Probe a task-local Python against one materialized DeepTutor repository."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

try:
    from .protocol import utc_now, write_json
except ImportError:  # pragma: no cover
    from protocol import utc_now, write_json  # type: ignore


REQUIRED_IMPORTS = (
    "pydantic",
    "pydantic_settings",
    "openai",
    "yaml",
    "jinja2",
    "defusedxml",
    "aiosqlite",
    "deeptutor",
    "deeptutor_cli",
)


_PROBE = r'''
import importlib
import json
import platform
import sys
import traceback

required = json.loads(sys.argv[1])
imports = {}
for name in required:
    try:
        module = importlib.import_module(name)
        imports[name] = {
            "ok": True,
            "version": str(getattr(module, "__version__", "")),
            "file": str(getattr(module, "__file__", "")),
        }
    except Exception as exc:
        imports[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

tool_names = []
tool_definitions = []
tool_error = None
tool_exception = None
try:
    from deeptutor.capabilities.mastery.tools import MASTERY_TOOL_NAMES, MASTERY_TOOL_TYPES
    definitions = [tool_type().get_definition() for tool_type in MASTERY_TOOL_TYPES]
    tool_names = [definition.name for definition in definitions]
    tool_definitions = [definition.to_openai_schema() for definition in definitions]
    if list(MASTERY_TOOL_NAMES) != tool_names:
        tool_error = "MASTERY_TOOL_NAMES and MASTERY_TOOL_TYPES differ"
except Exception as exc:
    tool_error = f"{type(exc).__name__}: {exc}"
    tool_exception = {"type":type(exc).__name__,"message":str(exc),"frames":[{"filename":f.filename,"line":f.lineno,"name":f.name} for f in traceback.extract_tb(exc.__traceback__)]}

print(json.dumps({
    "python_version": platform.python_version(),
    "python_info": list(sys.version_info[:3]),
    "executable": sys.executable,
    "imports": imports,
    "pydantic_major": int(str(imports.get("pydantic", {}).get("version", "0")).split(".")[0] or 0),
    "mastery_tool_names": tool_names,
    "mastery_tool_definitions": tool_definitions,
    "mastery_tool_error": tool_error,
    "mastery_tool_exception": tool_exception,
}, sort_keys=True))
'''


def probe_runtime(
    python_executable: str,
    repository: Path,
    *,
    adapter_path: Path | None = None,
    output: Path | None = None,
    _runner=None,
) -> dict[str, Any]:
    repository = repository.resolve()
    adapter = (adapter_path or Path(__file__).resolve().parent).resolve()
    runtime_root = Path(tempfile.mkdtemp(prefix="deeptutor-probe-", dir=output.parent if output else None))
    for name in ("home", "tmp", "deeptutor-home", "cache"):
        (runtime_root / name).mkdir()
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(runtime_root / "home"),
        "TMPDIR": str(runtime_root / "tmp"), "DEEPTUTOR_HOME": str(runtime_root / "deeptutor-home"),
        "XDG_CACHE_HOME": str(runtime_root / "cache"), "PYTHONDONTWRITEBYTECODE": "1"}
    record: dict[str, Any] = {
        "schema_version": "agentswe-deeptutor-runtime-probe/v1",
        "probed_at": utc_now(),
        "python_requested": python_executable,
        "repository": str(repository),
        "adapter_path": str(adapter),
        "required_imports": list(REQUIRED_IMPORTS),
        "valid": False,
        "infra_valid": False,
        "classification": "runtime_dependency_infrastructure_error",
        "errors": [],
        "infrastructure_errors": [],
        "candidate_errors": [],
    }
    if not repository.is_dir():
        record["classification"] = "mount_infrastructure_error"
        record["errors"] = ["materialized repository is missing"]
    else:
        try:
            try:
                from .isolated_runtime import sandbox_command
                from .owned_resources import run_owned
            except ImportError:
                from isolated_runtime import sandbox_command
                from owned_resources import run_owned
            # -I prevents Candidate sitecustomize/PYTHONPATH on startup. Product
            # imports enter sys.path only after the network/FS namespace exists.
            script = "import sys; sys.path.insert(0, sys.argv.pop(1));\n" + _PROBE
            command, boundary = sandbox_command([python_executable, "-I", "-c", script, str(repository), json.dumps(REQUIRED_IMPORTS)],
                repository, runtime_root, python_executable, repository_readonly=True)
            record["sandbox"] = boundary
            completed, resources = (_runner or run_owned)(command, cwd="/", env=env, output=runtime_root / "probe_resources", timeout=60)
            record["preflight_resource_contract"] = resources
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            record["classification"] = "launcher_infrastructure_error"
            record["errors"] = [f"runtime probe could not execute: {type(exc).__name__}: {exc}"]
        else:
            record.update(
                exit_code=completed.returncode,
                stdout_tail=completed.stdout[-4000:],
                stderr_tail=completed.stderr[-4000:],
            )
            try:
                observed = json.loads(completed.stdout.strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                observed = None
            record["observed"] = observed
            infrastructure_errors: list[str] = []
            candidate_errors: list[str] = []
            if completed.returncode != 0 or not isinstance(observed, dict):
                infrastructure_errors.append("runtime probe did not return a valid JSON record")
            else:
                version = observed.get("python_info")
                if not isinstance(version, list) or len(version) < 2 or not ((3, 11) <= tuple(version[:2]) < (3, 14)):
                    infrastructure_errors.append("Python must be >=3.11,<3.14")
                if observed.get("pydantic_major") != 2:
                    infrastructure_errors.append("Pydantic v2 is required")
                imports = observed.get("imports")
                if not isinstance(imports, dict):
                    infrastructure_errors.append("runtime import inventory is missing")
                else:
                    for name in REQUIRED_IMPORTS:
                        item = imports.get(name)
                        if not isinstance(item, dict) or item.get("ok") is not True:
                            infrastructure_errors.append(f"required import unavailable: {name}")
                if observed.get("mastery_tool_error"):
                    candidate_errors.append(str(observed["mastery_tool_error"]))
                names = observed.get("mastery_tool_names")
                if not isinstance(names, list) or "mastery_learner_snapshot_chain_audit" not in names:
                    candidate_errors.append("registered chain-audit mastery tool is unavailable")
            record["infrastructure_errors"] = infrastructure_errors
            record["candidate_errors"] = candidate_errors
            record["errors"] = infrastructure_errors + candidate_errors
            if infrastructure_errors:
                record.update(
                    valid=False,
                    infra_valid=False,
                    classification="runtime_dependency_infrastructure_error",
                )
            elif candidate_errors:
                record.update(
                    valid=False,
                    infra_valid=True,
                    classification="candidate_capability_gap",
                )
            else:
                record.update(
                    valid=True,
                    infra_valid=True,
                    classification="runtime_ready",
                )
    if output is not None:
        write_json(output, record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", dest="python_executable", required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--adapter-path", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = probe_runtime(**vars(args))
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if value.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
