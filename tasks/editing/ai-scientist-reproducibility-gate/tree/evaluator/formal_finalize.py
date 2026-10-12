#!/usr/bin/env python3
"""AI Scientist formal Result/Code finalizer; no heuristic publication.

The small compatibility helpers below preserve the older negative-control API.
They validate publication boundaries only and never compute a semantic score.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from semantic_finalize import finalize


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def reject(run_dir: Path, reasons: list[str]) -> int:
    write_json(run_dir / "formal_aggregation.json", {
        "schema_version": "agentswe-edit-formal-aggregation-0905-v1",
        "formal_result_publishable": False,
        "code_score_publishable": False,
        "result_axis": "N/A",
        "code_axis": "N/A",
        "combined_score": None,
        "reasons": reasons,
    })
    return 2


def code_contract(path: Path, run_dir: Path) -> tuple[dict[str, Any] | str, bool, list[str]]:
    """Reject static/precheck or non-run-local Code records.

    Formal publication uses semantic_finalize and the Create Code judge.  This
    helper exists only for provider-free regression tests and legacy callers.
    """
    try:
        path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return "N/A", False, ["Code contract is outside run-local evidence"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return "N/A", False, [f"Code contract is invalid: {exc}"]
    if not isinstance(value, dict):
        return "N/A", False, ["Code contract is not an object"]
    marker = " ".join(str(value.get(key, "")) for key in ("scoring_mode", "evaluation_state", "schema_version")).lower()
    score = value.get("code_score", value.get("code_raw_score", value.get("score", value.get("total"))))
    if (
        isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100
        or not value.get("source_evidence") or value.get("formal_judge") is False
        or "static" in marker or "precheck" in marker or "review_required" in marker
    ):
        return "N/A", False, ["Code contract is not an independent publishable formal judge contract"]
    return value, True, []


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--run-dir" in arguments and (
        "--credential-file" not in arguments or "--result-broker-endpoint" not in arguments
    ):
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--run-dir", type=Path, required=True)
        known, _unknown = parser.parse_known_args(arguments)
        return reject(
            known.run_dir.resolve(),
            ["formal semantic judges require evaluator credential and Result-judge broker inputs"],
        )
    return finalize(["--layout", "ai_scientist", "--code-rubric", str(ROOT / "code_rubric.md"), *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
