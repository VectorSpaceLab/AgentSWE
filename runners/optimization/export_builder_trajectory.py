#!/usr/bin/env python3
"""Export the complete Builder Codex session as auditable JSONL artifacts.

Harbor already preserves both the raw Codex session JSONL and its ATIF
trajectory.  This utility copies them under the campaign run directory and
also emits one JSON object per ATIF step, so downstream analysis does not need
to know Harbor's job-directory layout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_jsonl(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            count += 1
    if count == 0:
        raise RuntimeError(f"empty Codex session JSONL: {path}")
    return count


def locate_builder_trial(job_dir: Path) -> Path:
    candidates = sorted(
        path.parent.parent
        for path in job_dir.glob("*/agent/trajectory.json")
        if path.is_file()
    )
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one Builder trial trajectory under {job_dir}, found {len(candidates)}"
        )
    return candidates[0]


def locate_session(trial_dir: Path) -> Path:
    candidates = sorted((trial_dir / "agent" / "sessions").rglob("*.jsonl"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one Codex session JSONL under {trial_dir / 'agent' / 'sessions'}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def export(run_dir: Path, jobs_dir: Path, run_id: str, expected_model: str) -> dict[str, Any]:
    job_dir = jobs_dir / f"formal-persistent-builder-codex-{run_id}"
    trial_dir = locate_builder_trial(job_dir)
    atif_path = trial_dir / "agent" / "trajectory.json"
    trajectory = read_json(atif_path)
    if trajectory.get("schema_version") != "ATIF-v1.7":
        raise RuntimeError("Builder trajectory is not ATIF-v1.7")
    agent = trajectory.get("agent") or {}
    if agent.get("model_name") != expected_model:
        raise RuntimeError(
            f"Builder trajectory model {agent.get('model_name')!r} != {expected_model!r}"
        )
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("Builder ATIF trajectory has no steps")
    session_path = locate_session(trial_dir)
    raw_count = validate_jsonl(session_path)

    out = run_dir / "builder_trajectory"
    out.mkdir(parents=True, exist_ok=True)
    raw_out = out / "raw_codex_session.jsonl"
    atif_out = out / "atif_steps.jsonl"
    shutil.copyfile(session_path, raw_out)
    with atif_out.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "record_type": "trajectory_metadata",
            "exported_at": now(),
            "schema_version": trajectory.get("schema_version"),
            "session_id": trajectory.get("session_id"),
            "agent": agent,
            "run_id": run_id,
            "source_trajectory": str(atif_path),
            "source_session": str(session_path),
            "step_count": len(steps),
        }, ensure_ascii=False, sort_keys=True) + "\n")
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                raise RuntimeError(f"ATIF step {index} is not an object")
            record = {"record_type": "step", "step_index": index, **step}
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    manifest = {
        "schema_version": "1.0",
        "exported_at": now(),
        "run_id": run_id,
        "builder_model": expected_model,
        "builder_trial": str(trial_dir),
        "native_session_id": trajectory.get("session_id"),
        "raw_codex_session_jsonl": str(raw_out),
        "raw_codex_session_jsonl_sha256": digest(raw_out),
        "raw_codex_event_count": raw_count,
        "atif_source": str(atif_path),
        "atif_steps_jsonl": str(atif_out),
        "atif_steps_jsonl_sha256": digest(atif_out),
        "atif_step_count": len(steps),
        "complete_builder_session_export": True,
    }
    write_json(out / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-model", required=True)
    args = parser.parse_args()
    result = export(args.run_dir.resolve(), args.jobs_dir.resolve(), args.run_id, args.expected_model)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
