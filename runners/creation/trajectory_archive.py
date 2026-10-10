#!/usr/bin/env python3
"""Archive Harbor Builder ATIF trajectories as newline-delimited JSON.

The original ``agent/trajectory.json`` remains authoritative.  This helper is
called by every latest-suite Create adapter after Harbor exits and only enables itself
for job configurations whose task path contains ``builder_task``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "harbor-builder-trajectory-jsonl-v1"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def archive_builder_trajectories(
    config_path: Path, process_dir: Path
) -> dict[str, Any]:
    """Persist every Builder ATIF step as one complete JSONL record."""
    archive: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "source_trajectories": [],
        "jsonl_files": [],
        "errors": [],
    }
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        tasks = config.get("tasks", [])
        is_builder = any(
            isinstance(task, dict)
            and "builder_task" in str(task.get("path", ""))
            for task in tasks
        )
        if not is_builder:
            return archive

        archive["enabled"] = True
        job_dir = Path(str(config["jobs_dir"])) / str(config["job_name"])
        for source in sorted(job_dir.rglob("agent/trajectory.json")):
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"trajectory must be a JSON object: {source}")
            steps = data.get("steps", [])
            if not isinstance(steps, list):
                raise ValueError(f"trajectory steps must be a list: {source}")
            destination = source.with_suffix(".jsonl")
            records = [
                {
                    "record_type": "trajectory_meta",
                    "schema_version": SCHEMA_VERSION,
                    "source": str(source),
                    "session_id": data.get("session_id"),
                    "agent": data.get("agent"),
                }
            ]
            records.extend(
                {
                    "record_type": "trajectory_step",
                    "session_id": data.get("session_id"),
                    "step": step,
                }
                for step in steps
            )
            destination.write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )
            archive["source_trajectories"].append(str(source))
            archive["jsonl_files"].append(str(destination))
    except Exception as exc:  # Archive failure must not rewrite Harbor status.
        archive["errors"].append(f"{type(exc).__name__}: {exc}")

    _write_json(process_dir / "builder_trajectory_archive.json", archive)
    return archive
