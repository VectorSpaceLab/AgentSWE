#!/usr/bin/env python3
"""Materialize one immutable Candidate without touching the authoritative source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.source_identity import source_identity

ALLOWED_PREFIXES = (
    "src/api/conversation-service/", "src/api/runtime-service/", "src/api/recovery/",
    "src/stores/", "src/types/agent-server/", "src/hooks/",
    "src/components/features/conversation/", "__tests__/",
)
REQUIRED = {"solution.patch", "edit_report.json", "run_report.json"}


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*"), key=lambda x: str(x.relative_to(root))):
        rel = str(p.relative_to(root)).encode(); h.update(len(rel).to_bytes(8, "big")); h.update(rel)
        payload = p.read_bytes() if p.is_file() and not p.is_symlink() else (str(p).encode() if p.is_symlink() else b"")
        h.update(b"L" if p.is_symlink() else b"F" if p.is_file() else b"D"); h.update(len(payload).to_bytes(8, "big")); h.update(payload)
    return h.hexdigest()


def patch_paths(patch: Path) -> list[str]:
    result = []
    for line in patch.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            result.append(line.split(" b/", 1)[1])
        elif line.startswith("+++ b/"):
            result.append(line[6:].split("\t", 1)[0])
    return sorted(set(result))


def validate(submission: Path) -> list[str]:
    errors: list[str] = []
    names = {p.name for p in submission.iterdir()} if submission.is_dir() else set()
    errors += [f"missing {name}" for name in sorted(REQUIRED - names)]
    errors += [f"unexpected top-level artifact {name}" for name in sorted(names - REQUIRED)]
    if names == REQUIRED and any((submission / name).is_symlink() or not (submission / name).is_file() for name in REQUIRED):
        return ["delivery artifacts must be ordinary files"]
    patch = submission / "solution.patch"
    if patch.is_file() and not patch.read_text(encoding="utf-8").strip(): errors.append("empty solution.patch")
    if patch.is_file():
        for path in patch_paths(patch):
            parsed = Path(path)
            if parsed.is_absolute() or ".." in parsed.parts: errors.append(f"unsafe patch path: {path}")
            if not path.startswith(ALLOWED_PREFIXES): errors.append(f"forbidden patch path: {path}")
    for name in ("edit_report.json", "run_report.json"):
        try:
            value = json.loads((submission / name).read_text(encoding="utf-8"))
            if not isinstance(value, dict): errors.append(f"{name} must be an object")
        except Exception as exc: errors.append(f"{name}: {type(exc).__name__}")
    return errors


def run(cmd: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> dict[str, object]:
    p = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
    return {"command": cmd, "exit_code": p.returncode, "stdout_tail": p.stdout[-1000:], "stderr_tail": p.stderr[-1000:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository", type=Path, required=True)
    ap.add_argument("--submission", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--node-modules", type=Path, help="task-local prepared dependency tree")
    ap.add_argument("--run-typecheck", action="store_true")
    args = ap.parse_args(); repo, submission, output = map(lambda p: p.resolve(), (args.repository, args.submission, args.output))
    output.parent.mkdir(parents=True, exist_ok=True); shutil.rmtree(output, ignore_errors=True)
    errors = validate(submission)
    record: dict[str, object] = {"schema_version": "agentswe-candidate-materialization/v1", "source": str(repo), "errors": errors, "commands": []}
    if errors:
        (output.parent / "materialize_result.json").write_text(json.dumps(record, indent=2) + "\n"); return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo, output, symlinks=True)
    record["commands"].append(run(["git", "init", "-q"], output, 30))
    record["commands"].append(run(["git", "apply", "--check", str(submission / "solution.patch")], output, 60))
    if record["commands"][-1]["exit_code"] == 0:
        record["commands"].append(run(["git", "apply", str(submission / "solution.patch")], output, 60))
    if record["commands"][-1]["exit_code"] == 0:
        # Capture source before generated files; execution digest stays unchanged.
        record["product_source_identity"] = source_identity(output)
        node_modules = args.node_modules.resolve() if args.node_modules else None
        if node_modules is not None:
            if not node_modules.is_dir():
                record["errors"] = [f"prepared node_modules missing: {node_modules}"]
                (output.parent / "materialize_result.json").write_text(json.dumps(record, indent=2) + "\n")
                return 2
            # React Router/Vite and TypeScript write caches below node_modules
            # during the evaluator-owned type generation/typecheck.  The prepared
            # dependency tree is shared and root-owned, so linking it into a
            # Candidate turns an otherwise valid materialization into EACCES.  Give
            # every Candidate an evaluator-owned copy.  Reflinks keep this cheap on
            # filesystems that support them, while --reflink=auto remains portable.
            dependency_target = output / "node_modules"
            dependency_target.mkdir()
            dependency_copy = run(
                ["cp", "-a", "--reflink=auto", f"{node_modules}/.", str(dependency_target)],
                output,
                600,
            )
            record["commands"].append(dependency_copy)
            if dependency_copy["exit_code"] != 0:
                record["errors"] = ["failed to create evaluator-owned writable node_modules"]
                (output.parent / "materialize_result.json").write_text(
                    json.dumps(record, indent=2, ensure_ascii=False) + "\n"
                )
                return 2
        env = dict(os.environ)
        env.update({
            "CI": "1",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        })
        record["commands"].append(run(["node", "scripts/make-i18n-translations.cjs"], output, 120, env=env))
        if args.run_typecheck:
            record["commands"].append(run([str(output / "node_modules/.bin/react-router"), "typegen"], output, 180, env=env))
        if args.run_typecheck and record["commands"][-1]["exit_code"] == 0:
            record["commands"].append(run([str(output / "node_modules/.bin/tsc"), "--noEmit", "--skipLibCheck", "--pretty", "false"], output, 300, env=env))
    record["materialized_digest"] = digest(output) if output.exists() else None
    record["ok"] = bool(record["commands"] and record["commands"][-1]["exit_code"] == 0)
    (output.parent / "materialize_result.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return 0 if record["ok"] else 1


if __name__ == "__main__": raise SystemExit(main())
