#!/usr/bin/env python3
"""Negative controls for schema-v3 scoring, validity, isolation, and resources."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from common import MEMORY_LIMIT_BYTES, assertion, response_shape, run_monitored, scored
from evaluate_case import ASSERTION_MANIFEST, evaluate, validate_result_manifest
from evaluate_suite import aggregate_results, apply_once, public_isolation_findings, validate_case_layout

PACKAGE = Path(__file__).resolve().parents[2]
PUBLIC_FIXTURE = Path(__file__).resolve().parent / "public_fixture"

LOSSY_ADAPTER = r'''
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--request", required=True); parser.add_argument("--response", required=True); args=parser.parse_args()
    request=json.loads(Path(args.request).read_text()); operation=request["operation"]
    repositories=[]; admissions=[]
    for item in ((request.get("plan") or {}).get("repositories") or []):
        repositories.append({"id":item["id"],"repo":item["path"],"base_revision":item["base_revision"],"base_snapshot":None,"state":"blocked","candidate_commit":None,"tree_oid":None,"prepare_digest":None,"quarantine_digest":None,"objects_promoted":False})
        admissions.append({"repository_id":item["id"],"identity":item["path"],"state":"expired","fence":1,"expires_at_unix_ms":None})
    response={
      "schema_version":3,"operation":operation,"plan_id":request["plan_id"],"transaction_id":"lossy-fixed",
      "state":"blocked","reason":"deliberately lossy negative control","repo":request["repo"],
      "coordination":{"owner_id":None,"lease_token":None,"fence":1,"lease_state":"expired","expires_at_unix_ms":None,"admissions":admissions},
      "repositories":repositories,"subtasks":[],"integration_tests":[],
      "decision":{"state":"none","digest":None,"participant_order":[],"completed_participants":[]},
      "publication":{"state":"pending","targets":[]},
      "cleanup":{"state":"preserved","remaining_worktrees":[],"remaining_branches":[],"admissions_released":False},
      "ledger":{"schema_version":3,"generation":1,"event_count":1,"digest":"sha256:"+hashlib.sha256(b"lossy").hexdigest()},"errors":[]}
    text=json.dumps(response,sort_keys=True); Path(args.response).parent.mkdir(parents=True,exist_ok=True); Path(args.response).write_text(text+"\n"); print(text)
    return 0
if __name__=="__main__": raise SystemExit(main())
'''


def make_source(root: Path, adapter: bool) -> Path:
    source = root / "source"
    package = source / "aider"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n")
    (package / "__main__.py").write_text('print("self-test-aider")\n')
    if adapter:
        (package / "worktree_plan_adapter.py").write_text(LOSSY_ADAPTER)
    return source


def semantic_control(value: dict[str, object]) -> bool:
    critical = {"CLOSURE-PUBLIC-RECEIPTS", "CLOSURE-REBUILD-QUARANTINES", "CLOSURE-PUBLISH-GITLINK", "CLOSURE-ENTRY-FIDELITY"}
    assertions = {item["id"]: item for item in value.get("assertions", [])}
    return value.get("score", 101) <= 10 and all(identifier in assertions and not assertions[identifier]["passed"] for identifier in critical)


def main() -> int:
    checks: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="aider-repository-set-selftest-") as temporary:
        root = Path(temporary)
        baseline = make_source(root / "baseline", adapter=False)
        missing = subprocess.run([sys.executable, "-c", "import importlib.util,sys;sys.path.insert(0,sys.argv[1]);sys.exit(0 if importlib.util.find_spec('aider.worktree_plan_adapter') else 1)", str(baseline)], capture_output=True, text=True)
        checks.append({"id": "SELF-MISSING-ENTRY", "passed": missing.returncode == 1, "evidence": f"probe exit={missing.returncode}"})

        negative = make_source(root / "negative", adapter=True)
        lossy = evaluate("test_002", Path(sys.executable), negative, root / "case-lossy")
        checks.append({"id": "SELF-LOSSY-DETECTED", "passed": lossy["valid"] and semantic_control(lossy), "evidence": f"score={lossy['score']} cap={lossy['local_integrity_cap']}"})

        weakened = copy.deepcopy(lossy)
        target = next(item for item in weakened["assertions"] if item["id"] == "CLOSURE-REBUILD-QUARANTINES")
        target.update({"passed": True, "earned": target["points"]})
        weakened["raw_score"] += target["points"]
        weakened["score"] += target["points"]
        checks.append({"id": "SELF-SEMANTIC-WEAKENING", "passed": semantic_control(lossy) and not semantic_control(weakened), "evidence": f"original={lossy['score']} weakened={weakened['score']}"})

        perfect = scored("self_positive", [assertion("SELF-POSITIVE", 100, True, "evaluator-owned arithmetic")])
        capped = scored("self_cap", [assertion("SELF-CAP", 100, True, "evaluator-owned arithmetic")], cap=10, cap_reason="control")
        checks.append({"id": "SELF-SCORER", "passed": perfect["score"] == 100 and capped["score"] == 10, "evidence": f"perfect={perfect['score']} capped={capped['score']}"})

        omitted = copy.deepcopy(lossy)
        omitted["assertions"].pop()
        duplicated = copy.deepcopy(lossy)
        duplicated["assertions"].append(copy.deepcopy(duplicated["assertions"][0]))
        checks.append({"id": "SELF-ASSERTION-INVENTORY", "passed": not validate_result_manifest(lossy) and bool(validate_result_manifest(omitted)) and bool(validate_result_manifest(duplicated)), "evidence": f"omitted={validate_result_manifest(omitted)} duplicated={validate_result_manifest(duplicated)}"})

        bad_cap = copy.deepcopy(lossy)
        bad_cap["local_integrity_cap"], bad_cap["score"] = 0, 1
        checks.append({"id": "SELF-CAP-ACCOUNTING", "passed": bool(validate_result_manifest(bad_cap)), "evidence": str(validate_result_manifest(bad_cap))})

        cases = root / "cases"
        for case_id in ASSERTION_MANIFEST:
            (cases / case_id / "assets").mkdir(parents=True)
            (cases / case_id / "input.md").write_text("runtime input\n")
            (cases / case_id / "assets" / "scenario.json").write_text('{"schema_version":3}\n')
        validate_case_layout(cases)
        (cases / "test_006" / "input.md").unlink()
        omitted_detected = False
        try:
            validate_case_layout(cases)
        except Exception:
            omitted_detected = True
        checks.append({"id": "SELF-HIDDEN-INVENTORY", "passed": omitted_detected, "evidence": f"detected={omitted_detected}"})

        patch_repo = root / "patch-repo"
        patch_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=patch_repo, check=True)
        subprocess.run(["git", "config", "user.name", "Self Test"], cwd=patch_repo, check=True)
        subprocess.run(["git", "config", "user.email", "self@example.invalid"], cwd=patch_repo, check=True)
        (patch_repo / "value.txt").write_text("before\n")
        subprocess.run(["git", "add", "value.txt"], cwd=patch_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "before"], cwd=patch_repo, check=True)
        (patch_repo / "value.txt").write_text("after\n")
        patch_path = root / "once.patch"
        patch_path.write_text(subprocess.run(["git", "diff", "--", "value.txt"], cwd=patch_repo, text=True, capture_output=True, check=True).stdout)
        subprocess.run(["git", "checkout", "--", "value.txt"], cwd=patch_repo, check=True)
        apply_record = apply_once(patch_repo, patch_path)
        checks.append({"id": "SELF-PATCH-ONCE", "passed": apply_record["applied_count"] == 1 and apply_record["second_check_exit_code"] != 0, "evidence": str(apply_record)})

        leak_package = root / "leak-package"
        (leak_package / "dev_cases").mkdir(parents=True)
        (leak_package / "dev_cases" / "runner.py").write_text("from evaluator.harness import common\n")
        checks.append({"id": "SELF-PUBLIC-LEAKAGE", "passed": bool(public_isolation_findings(leak_package)), "evidence": str(public_isolation_findings(leak_package))})

        public_results = []
        for number in (1, 2):
            case_dir = PACKAGE / "dev_cases" / f"dev_{number:03d}"
            output = root / f"public-{number:03d}"
            completed = subprocess.run([
                sys.executable, str(PACKAGE / "dev_cases" / "run_dev_case.py"), "--case-dir", str(case_dir),
                "--python", sys.executable, "--source", str(PUBLIC_FIXTURE), "--output-dir", str(output),
            ], capture_output=True, text=True, timeout=30)
            result_path = output / "dev_result.json"
            public_results.append(completed.returncode == 0 and result_path.is_file() and json.loads(result_path.read_text()).get("valid") is True)
        checks.append({"id": "SELF-PUBLIC-POSITIVE", "passed": all(public_results), "evidence": str(public_results)})

        all_results = {case_id: evaluate(case_id, Path(sys.executable), negative, root / "all-cases" / case_id) for case_id in ASSERTION_MANIFEST}
        all_returned = all(result.get("valid") and not result.get("errors") and not validate_result_manifest(result) and 0 <= result.get("score", -1) <= 100 for result in all_results.values())
        checks.append({"id": "SELF-ALL-CASES-RETURN", "passed": all_returned, "evidence": str({key: {"score": value.get("score"), "errors": value.get("errors")} for key, value in all_results.items()})})

        aggregate = aggregate_results(all_results)
        missing_detected = invalid_detected = malformed_detected = False
        try:
            aggregate_results({key: value for key, value in all_results.items() if key != "test_006"})
        except Exception:
            missing_detected = True
        invalid = copy.deepcopy(all_results)
        invalid["test_006"]["valid"] = False
        try:
            aggregate_results(invalid)
        except Exception:
            invalid_detected = True
        malformed = copy.deepcopy(all_results)
        malformed["test_001"]["raw_score"] = 101
        try:
            aggregate_results(malformed)
        except Exception:
            malformed_detected = True
        checks.append({"id": "SELF-AGGREGATION-COMPLETE", "passed": len(aggregate["case_scores"]) == 6 and missing_detected and invalid_detected and malformed_detected, "evidence": f"missing={missing_detected} invalid={invalid_detected} malformed={malformed_detected}"})

        first = evaluate("test_002", Path(sys.executable), negative, root / "isolation-a")
        (root / "isolation-a" / "poison").write_text("must not cross cases")
        second = evaluate("test_001", Path(sys.executable), negative, root / "isolation-b")
        isolated = first["case_id"] == "test_002" and second["case_id"] == "test_001" and not (root / "isolation-b" / "poison").exists()
        checks.append({"id": "SELF-CASE-ISOLATION", "passed": isolated, "evidence": f"first={first['valid']} second={second['valid']}"})

        bad_shape = response_shape({"schema_version": 2}, "status", "p")
        checks.append({"id": "SELF-SCHEMA-REJECT", "passed": bool(bad_shape), "evidence": str(bad_shape)})
        without_decision = {
            "schema_version": 3, "operation": "status", "plan_id": "p", "transaction_id": "x", "state": "blocked", "reason": None, "repo": str(root),
            "coordination": {"owner_id": None, "lease_token": None, "fence": 1, "lease_state": "expired", "expires_at_unix_ms": None, "admissions": []},
            "repositories": [], "subtasks": [], "integration_tests": [], "publication": {"state": "pending", "targets": []},
            "cleanup": {"state": "preserved", "remaining_worktrees": [], "remaining_branches": [], "admissions_released": False},
            "ledger": {"schema_version": 3, "generation": 1, "event_count": 1, "digest": "sha256:" + "0" * 64}, "errors": [],
        }
        decision_required = any("decision" in error for error in response_shape(without_decision, "status", "p"))
        checks.append({"id": "SELF-DECISION-SCHEMA", "passed": decision_required, "evidence": f"required={decision_required}"})

        probe = run_monitored([sys.executable, "-c", "import subprocess,sys; subprocess.run([sys.executable,'-c','x=bytearray(1024*1024)'])"], cwd=root, timeout=10)
        harness_text = Path(__file__).with_name("common.py").read_text() + Path(__file__).with_name("prepare_environment.sh").read_text()
        forbidden_resource = any(marker in harness_text for marker in ("resource.setrlimit", "ulimit -v", "--jitless"))
        resource_ok = MEMORY_LIMIT_BYTES == 8 * 1024**3 and probe.returncode == 0 and probe.peak_pss_bytes > 0 and not probe.memory_exceeded and not forbidden_resource
        checks.append({"id": "SELF-RESOURCE-SEMANTICS", "passed": resource_ok, "evidence": f"limit={MEMORY_LIMIT_BYTES} peak={probe.peak_pss_bytes} forbidden={forbidden_resource}"})

    result = {"schema_version": 3, "passed": all(bool(item["passed"]) for item in checks), "checks": checks}
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
