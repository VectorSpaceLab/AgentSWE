#!/usr/bin/env python3
"""Candidate patch/materialization/build adapter for the Python hook product."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from agentloop.protocol import candidate_tree_digest, sha256_file, write_json
except ModuleNotFoundError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.protocol import candidate_tree_digest, sha256_file, write_json  # type: ignore

PLUGIN = Path("plugins/policy-provenance-ledger")
HOOK = PLUGIN / "hooks" / "policy_hook.py"
INSPECT = PLUGIN / "bin" / "policy-ledger-inspect"


class BuildInfrastructureError(RuntimeError):
    pass


def syntax_runtime() -> tuple[str, dict]:
    candidates = [shutil.which("python3.11"), shutil.which("python3.12"),
        "@@AGENTSWE_ENVS@@/claude-policy-python311/bin/python3.11",
        "@@AGENTSWE_ENVS@@/codex-project-memory-edit-v1/bin/python3.11",
        sys.executable]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        done = subprocess.run([candidate, "-c", "import sys,json,pathlib,hashlib; print(json.dumps(list(sys.version_info[:2])))"],
                              text=True, capture_output=True, timeout=15, check=False)
        if done.returncode == 0 and tuple(json.loads(done.stdout)) >= (3, 11):
            return candidate, {"valid": True, "evaluator_uid": os.geteuid(), "python": candidate,
                               "python_version": json.loads(done.stdout), "stdlib_imports_valid": True}
    raise BuildInfrastructureError("evaluator lacks the task-required Python 3.11+ stdlib runtime")


def patch_paths(patch: str) -> list[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("+++ b/"): paths.add(line[6:].split("\t", 1)[0])
        elif line.startswith("diff --git a/") and " b/" in line: paths.add(line.split(" b/", 1)[1])
    return sorted(paths)


def materialize(repository: Path, patch: Path, output: Path) -> dict[str, object]:
    if output.exists():
        raise BuildInfrastructureError("materialization output must be new; prior Candidates are immutable")
    python, preflight = syntax_runtime()
    shutil.copytree(repository, output, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    text = patch.read_text(encoding="utf-8")
    paths = patch_paths(text)
    if not paths or any(Path(p).is_absolute() or ".." in Path(p).parts or not p.startswith(PLUGIN.as_posix() + "/") for p in paths):
        raise ValueError("patch must contain non-empty repository-relative plugin-only paths")
    for command in (["git", "init", "-q"], ["git", "apply", "--check", str(patch)], ["git", "apply", str(patch)]):
        done = subprocess.run(command, cwd=output, text=True, capture_output=True, check=False, timeout=60)
        if done.returncode:
            message = f"materialize failed at {command[-1]}: {done.stderr[-800:]}"
            if command[1] == "init" or any(marker in done.stderr.lower() for marker in ("permission denied", "no space left", "read-only file", "disk quota")):
                raise BuildInfrastructureError(message)
            raise RuntimeError(message)
    second = subprocess.run(["git", "apply", "--check", str(patch)], cwd=output, text=True, capture_output=True, check=False, timeout=60)
    if second.returncode == 0: raise RuntimeError("patch remains applicable after one application")
    plugin = output / PLUGIN
    required = [plugin / ".claude-plugin/plugin.json", plugin / "hooks/hooks.json", output / HOOK, output / INSPECT]
    entries_valid = all(path.is_file() and not path.is_symlink() for path in required)
    compiled = None
    if entries_valid:
        compiled = subprocess.run([python, "-m", "py_compile", str(output / HOOK), str(output / INSPECT)], cwd=output, capture_output=True, text=True, check=False, timeout=30)
        if compiled.returncode and not any(label in compiled.stderr for label in ("SyntaxError", "IndentationError", "TabError")):
            raise BuildInfrastructureError("evaluator syntax check could not execute: " + compiled.stderr[-500:])
    build_valid = entries_valid and compiled.returncode == 0
    manifest = {"schema_version": "agentswe-claude-candidate-build/v1", "changed_paths": paths,
                "candidate_digest": candidate_tree_digest(output), "patch_sha256": sha256_file(patch),
                "hook": str(HOOK), "inspector": str(INSPECT), "python_syntax": "passed" if build_valid else "candidate_failed",
                "build_valid": build_valid, "environment_preflight": preflight,
                "build_diagnostic": "" if build_valid else (compiled.stderr[-2000:] if compiled else "Candidate plugin entry is incomplete"),
                "lower_entry": "candidate/plugins/policy-provenance-ledger/hooks/policy_hook.py + bin/policy-ledger-inspect"}
    write_json(output / ".agentloop_build.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--repository", type=Path, required=True); parser.add_argument("--patch", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(); result = materialize(args.repository.resolve(), args.patch.resolve(), args.output.resolve())
    if args.manifest: write_json(args.manifest, result)
    print(json.dumps(result, indent=2, ensure_ascii=False)); return 0


if __name__ == "__main__": raise SystemExit(main())
