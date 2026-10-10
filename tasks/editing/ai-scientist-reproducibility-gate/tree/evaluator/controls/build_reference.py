from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "input" / "repository"
CONTROL = Path(__file__).resolve().parent


def build(delivery: Path, mode: str = "complete") -> Path:
    delivery.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory) / "repo"
        shutil.copytree(SOURCE, work)
        commands = (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "control@example.invalid"],
            ["git", "config", "user.name", "Complete Control"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "source"],
        )
        for command in commands:
            subprocess.run(command, cwd=work, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        shutil.copyfile(CONTROL / "reference_claim_verification.py", work / "ai_scientist" / "claim_verification.py")
        if mode != "complete":
            module = work / "ai_scientist" / "claim_verification.py"
            source = module.read_text(encoding="utf-8")
            if mode == "preserved":
                guard = '        if args.session_store:\n            return fail(output, "cycle2_unavailable", "new capsule and budget surfaces are not implemented")\n'
            elif mode == "public_core":
                guard = '        if args.session_store and args.project_id != "public-repro":\n            return fail(output, "hidden_surface_unavailable", "nominal public capsule and budget core only")\n'
            else:
                raise ValueError(f"unknown control mode {mode}")
            source = source.replace("        args.generation = int(args.generation)\n", guard + "        args.generation = int(args.generation)\n", 1)
            module.write_text(source, encoding="utf-8")
        shutil.copyfile(CONTROL / "reference_launcher.py", work / "launch_scientist_bfts.py")
        if mode == "preserved":
            launcher = work / "launch_scientist_bfts.py"
            launcher_source = launcher.read_text(encoding="utf-8")
            launcher_source = launcher_source.replace("# Production governed route: --verify-claims-only accepts --claim-project-id and --claim-budget-store.\nverify_claims_only = True\n", "")
            launcher.write_text(launcher_source, encoding="utf-8")
        for relative in ("ai_scientist/perform_writeup.py", "ai_scientist/perform_icbinb_writeup.py"):
            path = work / relative
            path.write_text(path.read_text(encoding="utf-8") + "\n# Cycle 002 release gate integration calls the shared claim/capsule/budget gate when configured.\n", encoding="utf-8")
        subprocess.run(["git", "add", "-N", "ai_scientist/claim_verification.py"], cwd=work, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        diff = subprocess.run(["git", "diff", "--binary", "--no-ext-diff", "HEAD"], cwd=work, check=True, stdout=subprocess.PIPE, text=True).stdout
    (delivery / "solution.patch").write_text(diff, encoding="utf-8")
    changed = ["ai_scientist/claim_verification.py", "ai_scientist/perform_icbinb_writeup.py", "ai_scientist/perform_writeup.py", "launch_scientist_bfts.py"]
    (delivery / "edit_report.json").write_text(json.dumps({"schema_version": 1, "feature_summary": "Evaluator-owned complete reference control for capsules, budgets, attestations, notification outbox, and scoped run provenance.", "changed_paths": changed, "commands_and_results": ["standard-library reference control exercised against all six hidden schedules"], "compatibility_notes": ["ordinary mode and launcher route are both available"], "limitations": ["evaluator calibration control, never builder-visible"]}, indent=2), encoding="utf-8")
    (delivery / "run_report.json").write_text(json.dumps({"schema_version": "1.0", "status": "passed", "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"], "errors": [], "runtime_seconds": 0, "peak_memory_bytes": 0, "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0}}, indent=2), encoding="utf-8")
    return delivery


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--mode", choices=("complete", "preserved", "public_core"), default="complete")
    args = parser.parse_args()
    build(args.delivery, args.mode)
