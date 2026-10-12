"""No-provider native fixture regression against an explicit immutable Candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
CASES = ("dev_001", "dev_002", *(f"test_{i:03d}" for i in range(1, 7)))


def file_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*.json")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    summary = []
    for case_id in CASES:
        root = args.output / case_id
        root.mkdir()
        runtime = root / "runtime"
        runtime.mkdir()
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(runtime / "home"),
               "DEEPTUTOR_HOME": str(runtime / "deeptutor-home"), "PYTHONDONTWRITEBYTECODE": "1",
               "AGENTSWE_FIXTURE_RUNTIME_ROOT": str(runtime)}
        command = [args.python, "-I", str(ROOT / "agentloop/case_fixture.py"), "--repository", str(args.repository),
                   "--case-id", case_id, "--output", str(root / "fixture")]
        prepared = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
        if prepared.returncode:
            print(json.dumps({"case_id": case_id, "prepared": False, "stdout": prepared.stdout[-1500:], "stderr": prepared.stderr[-1500:]}), flush=True)
            return 2
        public = json.loads((root / "fixture/fixture-context.json").read_text())
        private = json.loads((root / "fixture/fixture-private.json").read_text())
        learning = runtime / "deeptutor-home/data/user/workspace/learning"
        before = file_hashes(learning)
        observed = subprocess.run([*command, "--observe"], env=env, capture_output=True, text=True, timeout=90)
        if observed.returncode:
            print(json.dumps({"case_id": case_id, "observed": False, "stdout": observed.stdout[-1500:], "stderr": observed.stderr[-1500:]}), flush=True)
            return 2
        oracle = json.loads((root / "fixture/fixture-observation.json").read_text())
        boundaries = [json.loads((root / "fixture" / name / "worker-boundary.json").read_text()) for name in ("prepare-worker", "observe-worker")]
        checks = {"real_persisted_learner_state": bool(before), "observer_does_not_mutate_product_state": before == file_hashes(learning),
                  "fixture_and_observer_product_imports_sandboxed": all(b["network_namespace"] == "isolated" and not b["broker_relay_mounted"] for b in boundaries),
                  "private_controller_not_mounted": all(b["private_fixture_mounted"] is False and b["extra_readonly_mounts"] == [str(ROOT / "agentloop/fixture_worker.py")] for b in boundaries),
                  "fixture_not_solution": private["solution_actions_performed"] is False,
                  "fixture_calls_not_agent_calls": all(call["owner"] == "evaluator_fixture_preparation" for call in private["fixture_calls"]),
                  "actual_durable_state_captured": bool(oracle["post_agent_durable_learning_state"]),
                  "private_oracle_hidden": oracle["candidate_visible"] is False,
                  "case_and_path_binding": public["case_id"] == case_id and public["session_id"] == public["path_id"],
                  "expected_invariants_present": len(private["expected_invariants"]) >= 3}
        if case_id == "dev_002":
            checks["graduated_history_actual"] = len(private["learning_before"]["quiz_attempts"]) == 4
        if case_id == "test_001":
            checks["claim_incident_actual"] = bool(public["delayed_old_response"]["delivery"]["claim_token"])
        if case_id == "test_003":
            checks["proof_actually_tampered"] = public["client_proof"]["snapshot"]["digest"] != private["original_proof"]["snapshot"]["digest"]
        if case_id == "test_004":
            checks["foreign_state_separate"] = private["foreign_path_id"] != public["path_id"]
            checks["foreign_payload_not_in_public"] = "foreign_learning_before" not in public
        if case_id == "test_005":
            checks["open_claim_and_handoff_exist"] = bool(public["old_claim"]) and bool(public["open_handoff"])
        if case_id == "test_006":
            checks["old_and_new_head_really_differ"] = private["original_proof"]["snapshot"]["digest"] != private["current_proof"]["snapshot"]["digest"]
        result = {"case_id": case_id, "passed": all(checks.values()), "checks": checks,
                  "preparation_calls": len(private["fixture_calls"]), "provider_calls": 0}
        summary.append(result)
        print(json.dumps(result), flush=True)
    (args.output / "verification.json").write_text(json.dumps({"all_passed": all(x["passed"] for x in summary), "cases": summary}, indent=2))
    return 0 if all(x["passed"] for x in summary) else 2

if __name__ == "__main__":
    raise SystemExit(main())
