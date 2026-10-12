from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "input" / "repository"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evaluator.harness.common import run_build_gates
from evaluator.harness.evaluate import evaluate_case
from evaluator.harness.case_specs import HIDDEN_CASES
from evaluator.tests.reference_control import install_reference


INCOMPLETE_REVISION = '''import argparse,json
from pathlib import Path
def main():
 p=argparse.ArgumentParser();p.add_argument("--store");p.add_argument("--operation");a=p.parse_args();op=json.loads(Path(a.operation).read_text());b={"schema_version":"1.0","operation_id":op.get("operation_id"),"action":op.get("action"),"tenant_id":op.get("tenant_id"),"project_id":op.get("project_id")}
 if op.get("action")=="register":print(json.dumps({**b,"accepted":True,"record":{"revision_id":"incomplete-revision","revision_digest":"0"*64,"review_generation":0}}));return 0
 print(json.dumps({**b,"accepted":False,"error":{"code":"NOT_IMPLEMENTED","message":"incomplete control"}}));return 2
if __name__=="__main__":main()
'''


def prepare(root: Path, complete: bool, prior: bool = False, name: str | None = None) -> Path:
    repository = root / (name or ("cycle002_equivalent" if prior else ("complete" if complete else "entry_only")))
    shutil.copytree(SOURCE, repository)
    if complete or prior:
        install_reference(repository)
        if prior:
            # Local Cycle 002-equivalent control: preserve the direct capsule
            # and durable-run entry while omitting the Cycle 003 surfaces.
            for module in ("traceability_revisions.py", "traceability_execution.py"):
                (repository / "workflows" / module).unlink(missing_ok=True)
    else:
        (repository / "workflows" / "traceability_revisions.py").write_text(INCOMPLETE_REVISION)
        (repository / "workflows" / "traceability_execution.py").write_text("raise SystemExit(2)\n")
    return repository


def run_control(label: str, repository: Path) -> dict:
    gates = run_build_gates(repository)
    result = evaluate_case(repository, "test_001", build_gates_passed=True)
    return {
        "label": label,
        "score": result["score"],
        "valid": result.get("valid_artifact", False),
        "behavioral_zero_reason": result.get("behavioral_zero_reason"),
        "capability_observed": result.get("capability_observed"),
        "process_count": len(result.get("evidence", {}).get("processes", [])),
        "peak_memory_bytes": max((item.get("peak_memory_bytes", 0) for item in result.get("evidence", {}).get("processes", [])), default=0),
        "build_gates": [{"exit_code": gate.exit_code, "timed_out": gate.timed_out, "duration_seconds": gate.duration_seconds, "peak_memory_bytes": gate.peak_memory_bytes} for gate in gates],
    }


def run_complete_all(repository: Path) -> dict:
    gates = run_build_gates(repository)
    scores = []
    for case_id in HIDDEN_CASES:
        scores.append(evaluate_case(repository, case_id, build_gates_passed=True)["score"])
    return {"label": "complete_evaluator_all_six", "scores": scores, "mean_score": sum(scores) / len(scores), "build_gates_pass": all(g.exit_code == 0 and not g.timed_out for g in gates), "peak_memory_bytes": max((g.peak_memory_bytes for g in gates), default=0)}


def main() -> int:
    if importlib.util.find_spec("loguru") is None and not os.environ.get("DEEPCODE_CALIBRATION_BOOTSTRAPPED") and shutil.which("uv"):
        env = dict(os.environ); env["DEEPCODE_CALIBRATION_BOOTSTRAPPED"] = "1"
        return subprocess.run(["uv", "run", "--offline", "--no-project", "--with-requirements", str(SOURCE / "requirements.txt"), "--with", "pytest", "--with", "pytest-asyncio", "python", str(Path(__file__).resolve())], cwd=ROOT, env=env, check=False).returncode
    with tempfile.TemporaryDirectory(prefix="deepcode-cycle002-controls-") as directory:
        base = Path(directory)
        complete_all = run_complete_all(prepare(base, True, name="complete_all"))
        controls = [run_control("complete_evaluator_control", prepare(base, True)), run_control("entry_only_control", prepare(base, False)), run_control("cycle002_equivalent_control", prepare(base, False, prior=True)), run_control("public_core_only_control", prepare(base, False, prior=True, name="public_core_only"))]
    scores = [item["score"] for item in controls]
    if complete_all["scores"] != [100] * 6:
        raise RuntimeError(f"complete evaluator all-six control did not earn 100: {complete_all}")
    if controls[0]["score"] != 100:
        raise RuntimeError(f"complete evaluator control did not earn 100: {controls[0]}")
    if controls[1]["valid"] and controls[1]["score"] > 25:
        raise RuntimeError(f"incomplete control exceeded difficulty guardrail: {controls[1]}")
    print(json.dumps({"schema_version": "1.0", "valid": True, "complete_all_six": complete_all, "controls": controls, "resource_headroom": {"timeout_seconds": 600, "rss_limit_bytes": 2_147_483_648, "max_observed_rss_bytes": max(item["peak_memory_bytes"] for item in controls), "max_observed_runtime_seconds": max((gate["duration_seconds"] for item in controls for gate in item["build_gates"]), default=0)}, "arithmetic": {"scores": scores, "means": {"all_controls": sum(scores) / len(scores)}}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
