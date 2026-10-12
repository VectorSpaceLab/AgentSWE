"""Materialize evaluator-owned case fixtures for a real lower-agent run.

Only the evaluator imports hidden manifests.  The lower agent receives the
materialized repository and a case-local request, never the oracle or its
per-case scoring data.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


def _materialize_case():
    root = Path(__file__).resolve().parents[2]
    dev_cases = root / "dev_cases"
    if str(dev_cases) not in sys.path:
        sys.path.insert(0, str(dev_cases))
    from harness_support import materialize_case  # type: ignore

    return materialize_case


def _load_spec(case_id: str, cases_root: Path) -> dict[str, Any]:
    if cases_root.name == "dev_cases":
        path = cases_root / "public_specs.json"
        values = json.loads(path.read_text(encoding="utf-8"))
        value = values.get(case_id)
    else:
        path = cases_root.parent / "evaluator" / "manifests" / f"{case_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("case_id", case_id) != case_id:
        raise ValueError(f"invalid evaluator case specification for {case_id}")
    value = dict(value)
    value.setdefault("case_id", case_id)
    return value


def materialize_runtime_case(
    case_id: str,
    cases_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    case_dir = cases_root / case_id
    if not case_dir.is_dir():
        raise FileNotFoundError(f"case directory missing: {case_dir}")
    spec = _load_spec(case_id, cases_root)
    output_root.mkdir(parents=True, exist_ok=False)
    fixture_root = output_root / "materialized"
    repo, materialized_spec, _ = _materialize_case()(case_dir, fixture_root, spec)
    runtime_repo = output_root / "repository"
    shutil.copytree(repo, runtime_repo, symlinks=True)

    # directories change sources point at evaluator temp paths.  Copy the
    # read-only snapshots into the case workspace and rewrite only those paths.
    # `materialize_case` returns the evaluator spec, while the actual runtime
    # change source is written into the generated manifest.  Read that
    # evaluator-produced manifest rather than assuming the source spec carries
    # derived paths.
    runtime_manifest = json.loads(
        (repo / "impact-manifest.json").read_text(encoding="utf-8")
    )
    change_source = runtime_manifest.get("change_source", {})
    if change_source.get("kind") == "directories":
        support = runtime_repo / ".case-support"
        support.mkdir(parents=True, exist_ok=True)
        for name in ("before", "after"):
            source = fixture_root / f"{name}-snapshot"
            if not source.is_dir():
                raise RuntimeError(f"directory change source missing {name} snapshot")
            shutil.copytree(source, support / name, symlinks=True)
        manifest_file = runtime_repo / "impact-manifest.json"
        runtime_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        runtime_manifest["change_source"]["before"] = ".case-support/before"
        runtime_manifest["change_source"]["after"] = ".case-support/after"
        manifest_file.write_text(
            json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return {
        "case_id": case_id,
        "repository": runtime_repo,
        "fixture_root": fixture_root,
        "manifest": runtime_repo / "impact-manifest.json",
        "change_source_kind": change_source.get("kind"),
    }


def runtime_case_paths(case_id: str, cases_root: Path, output_root: Path) -> dict[str, Any]:
    """Describe future case-owned paths without copying or constructing the world."""
    return {'case_id':case_id,'repository':output_root/'repository','fixture_root':output_root/'materialized',
        'manifest':output_root/'repository/impact-manifest.json','change_source_kind':None}
