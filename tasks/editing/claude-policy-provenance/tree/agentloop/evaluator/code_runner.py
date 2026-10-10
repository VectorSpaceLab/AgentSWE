#!/usr/bin/env python3
"""Auditable static Code-axis runner; never reads Agent-loop Result files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from agentloop.evaluator.code_rubric import DIMENSIONS, rubric
except ModuleNotFoundError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.evaluator.code_rubric import DIMENSIONS, rubric  # type: ignore


def run(root: Path) -> dict[str, object]:
    plugin = root / "plugins/policy-provenance-ledger"; hook = plugin / "hooks/policy_hook.py"; inspect = plugin / "bin/policy-ledger-inspect"
    checks = {"interface_lifecycle": [plugin / ".claude-plugin/plugin.json", plugin / "hooks/hooks.json", hook, inspect], "requirement_mechanism_coverage": [hook], "analysis_evidence_integrity": [hook, inspect], "safety_privacy_side_effects": [hook], "recovery_honest_failure": [hook, inspect], "testability_observability": [hook, inspect], "maintainability_generalization": [hook], "resource_discipline": [hook, inspect]}
    dimensions = {}
    for name, paths in checks.items():
        present = sum(path.is_file() for path in paths); raw = round(DIMENSIONS[name] * present / len(paths))
        dimensions[name] = {"score": raw, "maximum": DIMENSIONS[name], "evidence": [str(path.relative_to(root)) for path in paths if path.is_file()]}
    return {"schema_version": "agentswe-code-score/v1", "rubric": rubric(), "dimensions": dimensions, "score": sum(item["score"] for item in dimensions.values()), "result_axis_read": False}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--candidate-root", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args(); value = run(args.candidate_root.resolve()); args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"); print(json.dumps(value, indent=2, ensure_ascii=False)); return 0

if __name__ == "__main__": raise SystemExit(main())
