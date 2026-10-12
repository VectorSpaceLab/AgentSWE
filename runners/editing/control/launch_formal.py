#!/usr/bin/env python3
"""Fail-closed launcher for the ten codex_xhigh Edit formal branches."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_runtime import control_command, control_environment
from formal_commands import build_formal_command, branch_input_evidence
from audit_readiness import tree_digest


ROOT = Path(__file__).resolve().parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> Any:
    path = ROOT / "formal_config.py"
    spec = importlib.util.spec_from_file_location("edit_formal_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def process_start_ticks(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def branch_log_paths(control_dir: Path, task: str) -> tuple[Path, Path]:
    """One-stop owns its fresh run directory; launcher logs live separately."""
    directory = control_dir / task
    directory.mkdir(parents=True, exist_ok=False)
    return directory / "branch.stdout.log", directory / "branch.stderr.log"


def capture_dispatch_bindings(cfg: Any, validated: dict[str, Any]) -> list[dict[str, str]]:
    """Pin shared execution inputs for the lifetime of the queued campaign."""
    paths = set(ROOT.glob('*.py')) | {
        cfg.CONTROL_PYTHON, cfg.ALIGNMENT_SNAPSHOT, cfg.RESULT_JUDGE, cfg.CREATE_CODE_JUDGE,
    }
    evidence = {str(path): {'path': str(path), 'resolved_path': str(path.resolve(strict=True)),
                            'sha256': sha256(path)} for path in paths}
    expected = list((validated.get('shared_runtime') or {}).values())
    expected.append(validated.get('configuration_delta_registry') or {})
    alignment = validated.get('create_alignment_verification') or {}
    expected.extend(alignment.get(key) or {} for key in ('snapshot', 'selected_manifest_provenance'))
    for key in ('formal_readiness_gate', 'post_repair_tree_snapshot', 'create_alignment_snapshot', 'case_coverage_matrix'):
        expected.append({'path': validated.get(key), 'sha256': validated.get(key + '_sha256')})
    matrix_path = validated.get('case_coverage_matrix')
    if matrix_path:
        for row in json.loads(Path(matrix_path).read_text()).get('tasks', []):
            ref = row.get('evaluator_readiness_admission') or {}
            if ref.get('path') and ref.get('sha256'):
                expected.append(ref)
                if sha256(Path(ref['path'])) != ref['sha256']:
                    raise RuntimeError('readiness admission changed after audit: ' + row['task'])
                admission = json.loads(Path(ref['path']).read_text())
                expected.extend([admission.get('contract') or {}, admission.get('smoke_manifest') or {}])
                expected.extend(admission.get('review_reports') or [])
    for ref in expected:
        if isinstance(ref, dict) and ref.get('path') and ref.get('sha256'):
            path = Path(ref['path'])
            if sha256(path) != ref['sha256']:
                raise RuntimeError('launch binding changed after validation: ' + str(path))
            evidence[str(path)] = {'path': str(path), 'resolved_path': str(path.resolve(strict=True)),
                                   'sha256': ref['sha256']}
    code_implementation = (validated.get('code_judge') or {}).get('authoritative_implementation')
    if code_implementation:
        path = Path(code_implementation)
        evidence[str(path)] = {'path': str(path), 'resolved_path': str(path.resolve(strict=True)),
                               'sha256': sha256(path)}
    return [evidence[path] for path in sorted(evidence)]


def verify_dispatch_bindings(branch: dict[str, Any], shared: list[dict[str, str]]) -> None:
    sibling = Path(branch['sibling'])
    if tree_digest(sibling) != branch['sibling_digest']:
        raise ValueError('queued task source changed: ' + branch['task'])
    if branch_input_evidence(branch['task'], sibling, Path(branch['run_dir'])) != branch['input_evidence']:
        raise ValueError('queued task inputs changed: ' + branch['task'])
    for ref in shared:
        path = Path(ref['path'])
        if str(path.resolve(strict=True)) != ref['resolved_path'] or sha256(path) != ref['sha256']:
            raise ValueError('queued campaign shared binding changed: ' + str(path))


def validate_ready(manifest_path: Path, python: Path) -> dict[str, Any]:
    audited = subprocess.run(
        control_command(python, ROOT / "audit_readiness.py"),
        cwd=ROOT,
        env=control_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if audited.returncode != 0:
        raise RuntimeError("fresh formal readiness audit failed:\n" + audited.stdout[-8000:])
    completed = subprocess.run(
        control_command(python, ROOT / "validate_formal_config.py", "--require-ready", "--output", str(manifest_path)),
        cwd=ROOT,
        env=control_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("formal config/readiness validation failed:\n" + completed.stdout[-8000:])
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or (value.get("validation") or {}).get("valid") is not True:
        raise RuntimeError("validator did not produce a valid launch manifest")
    if value.get("formal_launch_authorized") is not True:
        raise RuntimeError("validator did not authorize formal launch")
    return value


def branch_command(task: str, sibling: Path, run_dir: Path, credential: Path, python: Path) -> list[str]:
    return build_formal_command(task, sibling, run_dir, credential, python)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-label", required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--credential-file", type=Path)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 10:
        raise SystemExit("--batch-size must be 1..10")
    cfg = load_config()
    credential = (args.credential_file or cfg.CREDENTIAL_FILE).resolve()
    if not credential.is_file():
        raise SystemExit(f"missing evaluator credential file: {credential}")
    if credential.stat().st_mode & 0o077:
        raise SystemExit("credential file must not be group/world accessible")
    base_manifest_path = ROOT / "formal_launch_manifest.json"
    validated = validate_ready(base_manifest_path, cfg.CONTROL_PYTHON)
    if validated.get("selected_profiles") != ["codex_xhigh"] or validated.get("expected_branch_count") != 10:
        raise SystemExit("fail closed: selected profile or branch count mismatch")
    dispatch_bindings = capture_dispatch_bindings(cfg, validated)
    run_root = cfg.FORMAL_ROOT / "codex_xhigh"
    run_root.mkdir(parents=True, exist_ok=True)
    launch_id = f"0905-edit-codex-xhigh-{args.attempt_label}"
    launch_manifest_path = cfg.FORMAL_ROOT / f"launch_manifest_{args.attempt_label}.json"
    if launch_manifest_path.exists():
        raise SystemExit(f"launch manifest already exists: {launch_manifest_path}")
    branches: list[dict[str, Any]] = []
    validated_tasks = {
        item.get("task"): item
        for item in validated.get("tasks", [])
        if isinstance(item, dict) and isinstance(item.get("task"), str)
    }
    for task, sibling in cfg.TASKS.items():
        run_id = f"{launch_id}-{task}"
        run_dir = run_root / task / run_id
        if run_dir.exists():
            raise SystemExit(f"run directory already exists: {run_dir}")
        one_stop = sibling / "harbor/formal_one_stop.py"
        command = branch_command(task, sibling, run_dir, credential, cfg.CONTROL_PYTHON)
        inputs = branch_input_evidence(task, sibling, run_dir)
        branches.append({
            "task": task,
            "profile": "codex_xhigh",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "sibling": str(sibling),
            "sibling_digest": (validated_tasks.get(task) or {}).get("sibling_digest"),
            "command": command,
            "input_evidence": inputs,
            "state": "pending",
        })
    manifest: dict[str, Any] = {
        **{key: value for key, value in validated.items() if key not in {"branches", "tasks"}},
        "schema_version": "agentswe-0905-edit-formal-execution-manifest-v1",
        "launch_id": launch_id,
        "attempt_label": args.attempt_label,
        "created_at": now(),
        "launcher_pid": os.getpid(),
        "launcher_process_start_ticks": process_start_ticks(os.getpid()),
        "launch_mode": "scheduled_wait" if args.wait else "first_batch_only_non_wait",
        "formal_evaluation_started": False,
        "profile_scope": "codex_xhigh_only",
        "selected_profiles": ["codex_xhigh"],
        "expected_branch_count": 10,
        "batch_size": args.batch_size,
        "branches": branches,
        "dispatch_bindings": dispatch_bindings,
        "credential_file": str(credential),
        "credential_values_recorded": False,
        "create_alignment_snapshot_sha256": sha256(cfg.ALIGNMENT_SNAPSHOT),
        "result_judge_sha256": sha256(cfg.RESULT_JUDGE),
        "code_judge_sha256": sha256(cfg.CREATE_CODE_JUDGE),
    }
    control_dir = cfg.FORMAL_ROOT / "launch_control" / launch_id
    control_dir.mkdir(parents=True, exist_ok=False)
    manifest["control_dir"] = str(control_dir)
    write_json(launch_manifest_path, manifest)
    env = control_environment()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "AGENTSWE_RESULT_JUDGE": str(cfg.RESULT_JUDGE),
        "AGENTSWE_CODE_JUDGE": str(cfg.CREATE_CODE_JUDGE),
        "AGENTSWE_CREATE_ALIGNMENT_SNAPSHOT": str(cfg.ALIGNMENT_SNAPSHOT),
        "AGENTSWE_EDIT_PROFILE_SCOPE": "codex_xhigh_only",
    })
    active: list[tuple[subprocess.Popen[bytes], dict[str, Any], Any, Any]] = []
    failures = 0

    def persist() -> None:
        write_json(launch_manifest_path, manifest)

    def reap_one() -> None:
        nonlocal failures
        # Free a slot when any branch finishes. Waiting for the first branch
        # can strand capacity while later branches have already terminated.
        while True:
            finished = next((index for index, item in enumerate(active)
                             if item[0].poll() is not None), None)
            if finished is not None:
                break
            time.sleep(1)
        process, branch, stdout, stderr = active.pop(finished)
        code = process.wait()
        stdout.close()
        stderr.close()
        branch["exit_code"] = code
        branch["finished_at"] = now()
        branch["state"] = "completed" if code == 0 else "failed"
        failures += int(code != 0)
        persist()

    for branch in branches:
        while len(active) >= args.batch_size:
            reap_one()
        run_dir = Path(branch["run_dir"])
        try:
            if run_dir.exists():
                raise ValueError(f"branch run directory appeared before launch: {run_dir}")
            verify_dispatch_bindings(branch, dispatch_bindings)
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            branch.update(state='rejected_before_start', rejected_at=now(),
                          rejection=f'{type(exc).__name__}: {exc}')
            manifest.update(dispatch_halted_at=now(), dispatch_halt_reason=branch['rejection'])
            failures += 1
            manifest['failure_count'] = failures
            persist()
            break
        stdout_path, stderr_path = branch_log_paths(control_dir, branch["task"])
        stdout = stdout_path.open("wb")
        stderr = stderr_path.open("wb")
        process = subprocess.Popen(
            branch["command"], cwd=branch["sibling"], env=env,
            stdout=stdout, stderr=stderr, start_new_session=True,
        )
        branch.update({
            "pid": process.pid,
            "process_start_ticks": process_start_ticks(process.pid),
            "started_at": now(),
            "state": "running",
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        })
        manifest["formal_evaluation_started"] = True
        manifest.setdefault("formal_evaluation_started_at", branch["started_at"])
        active.append((process, branch, stdout, stderr))
        persist()
        if not args.wait:
            # Non-wait mode launches only the first batch; a monitor/resumer must
            # launch remaining branches deliberately to preserve concurrency.
            if len(active) >= args.batch_size:
                break
    if args.wait:
        while active:
            reap_one()
        if all(item['state'] in {'completed', 'failed'} for item in branches):
            manifest["completed_at"] = now()
        else:
            manifest['active_branches_drained_at'] = now()
        manifest["failure_count"] = failures
        persist()
    else:
        manifest["launch_mode"] = "first_batch_only_non_wait"
        persist()
    print(json.dumps({
        "launch_manifest": str(launch_manifest_path),
        "started_branches": sum("started_at" in item for item in branches),
        "running_branches": sum(item["state"] == "running" for item in branches),
        "pending_branches": sum(item["state"] == "pending" for item in branches),
        "profile_scope": "codex_xhigh_only",
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
