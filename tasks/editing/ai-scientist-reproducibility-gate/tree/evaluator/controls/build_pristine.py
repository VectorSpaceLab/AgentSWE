from __future__ import annotations

import json
from pathlib import Path


def build(delivery: Path) -> Path:
    delivery.mkdir(parents=True, exist_ok=True)
    patch = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@\n+<!-- evaluator pristine behavioral control -->\n <div align=\"center\">\n"
    (delivery / "solution.patch").write_text(patch, encoding="utf-8")
    (delivery / "edit_report.json").write_text(json.dumps({"schema_version": 1, "feature_summary": "entry-only pristine control", "changed_paths": ["README.md"], "commands_and_results": [], "compatibility_notes": [], "limitations": ["No requested product module is present."]}), encoding="utf-8")
    (delivery / "run_report.json").write_text(json.dumps({"schema_version": "1.0", "status": "not_run", "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"], "errors": [], "runtime_seconds": 0, "peak_memory_bytes": 0, "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0}}), encoding="utf-8")
    return delivery


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", type=Path, required=True)
    build(parser.parse_args().delivery)
