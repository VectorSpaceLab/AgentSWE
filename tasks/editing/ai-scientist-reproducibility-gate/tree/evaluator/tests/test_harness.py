from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "evaluator" / "harness"
PUBLIC = ROOT / "dev_cases"
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(PUBLIC))

from benchmark_harness import execute_case, load_manifest, prepare_submission  # noqa: E402
from public_harness import (  # noqa: E402
    REQUIRED_DELIVERY,
    SOURCE_TREE_SHA256,
    allowed_patch_path,
    canonical_sha256,
    expected_charge,
    inspect_capsule,
    snapshot_source_hash,
    validate_delivery,
)
from oracles import SPECS  # noqa: E402
from public_specs import SPECS as PUBLIC_SPECS  # noqa: E402


def new_file_patch(path: str, source: str) -> str:
    lines = source.splitlines()
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)


def delivery(root: Path, patch: str, changed_paths: list[str], *, schema: bool = True, name: str = "submission") -> Path:
    submission = root / name
    submission.mkdir()
    (submission / "solution.patch").write_text(patch, encoding="utf-8")
    (submission / "edit_report.json").write_text(json.dumps({
        "schema_version": 1,
        "feature_summary": "calibration control",
        "changed_paths": changed_paths,
        "commands_and_results": [],
        "compatibility_notes": [],
        "limitations": [],
    }), encoding="utf-8")
    (submission / "run_report.json").write_text(json.dumps({
        "schema_version": "1.0" if schema else 1,
        "status": "not_run",
        "artifact_paths": list(REQUIRED_DELIVERY),
        "errors": [],
        "runtime_seconds": 0,
        "peak_memory_bytes": 0,
        "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0},
    }), encoding="utf-8")
    return submission


def referenced_hashes(value):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            yield value["path"], value["sha256"]
        for child in value.values():
            yield from referenced_hashes(child)
    elif isinstance(value, list):
        for child in value:
            yield from referenced_hashes(child)


class HarnessSelfTests(unittest.TestCase):
    def test_inventory_contexts_manifests_and_source_hash(self):
        self.assertEqual(tuple(PUBLIC_SPECS), ("dev_001", "dev_002"))
        self.assertEqual(tuple(name for name in SPECS if name.startswith("test_")), tuple(f"test_{i:03d}" for i in range(1, 7)))
        for case_id in (name for name in SPECS if name.startswith("test_")):
            manifest = load_manifest(case_id)
            self.assertEqual(sum(manifest["assertions"].values()), 100)
            context = json.loads((ROOT / "test_cases" / case_id / "assets" / "transaction_context.json").read_text())
            for key in ("tenant_id", "verification_id", "request_id", "owner_id", "generation", "project_id", "capsule_policy", "usage_statement", "budget_policy", "run_manifest", "attestation_policy", "notification_policy", "attestation_store", "notification_store"):
                self.assertIn(key, context)
        self.assertEqual(snapshot_source_hash(), SOURCE_TREE_SHA256)

    def test_new_surface_manifests_and_policies_are_behavioral(self):
        for case_id in tuple(SPECS):
            if not case_id.startswith("test_"):
                continue
            manifest = load_manifest(case_id)
            self.assertEqual(sum(manifest["assertions"][name] for name in manifest["assertions"] if name.startswith("PROVENANCE.")), 60)
            assets = ROOT / "test_cases" / case_id / "assets"
            self.assertTrue((assets / "attestation_policy.json").is_file())
            self.assertTrue((assets / "notification_policy.json").is_file())

    def test_delivery_schema_rejects_legacy_and_accepts_shared_contract(self):
        source = "import argparse\nargparse.ArgumentParser().parse_args()\n"
        patch = new_file_patch("ai_scientist/claim_verification.py", source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_submission(delivery(root, patch, ["ai_scientist/claim_verification.py"]), root / "work")
            self.assertTrue(prepared["compile_gate"] == "passed")
            bad = delivery(root, patch, ["ai_scientist/claim_verification.py"], schema=False, name="bad-submission")
            with self.assertRaises(Exception):
                prepare_submission(bad, root / "bad-work")
            legacy = delivery(root, patch, ["ai_scientist/claim_verification.py"], name="legacy-submission")
            report = json.loads((legacy / "run_report.json").read_text())
            report.pop("api_calls")
            report.update({"gateway": 0, "serper": 0, "web_retrieval": 0, "resource_usage": {"runtime_seconds": 0}})
            (legacy / "run_report.json").write_text(json.dumps(report))
            with self.assertRaises(Exception):
                validate_delivery(legacy)

    def test_patch_path_policy(self):
        self.assertTrue(allowed_patch_path("ai_scientist/claim_verification.py"))
        self.assertTrue(allowed_patch_path("launch_scientist_bfts.py"))
        for path in ("../evaluator/harness.py", "/tmp/result.py", "LICENSE", "dev_cases/dev_001/input.md"):
            self.assertFalse(allowed_patch_path(path))

    def test_fixture_hashes_and_capsule_charge_arithmetic(self):
        intentional = {("test_003", "raw_results.json"), ("test_006", "config.json")}
        seen = set()
        for group in ("dev_cases", "test_cases"):
            for case_dir in sorted((ROOT / group).glob("*_[0-9][0-9][0-9]")):
                manifest = json.loads((case_dir / "assets" / "evidence_manifest.json").read_text())
                for relative, expected in referenced_hashes(manifest):
                    path = case_dir / "assets" / relative
                    self.assertTrue(path.is_file())
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    key = (case_dir.name, relative)
                    if key in intentional:
                        self.assertNotEqual(actual, expected)
                        seen.add(key)
                    else:
                        self.assertEqual(actual, expected, f"unexpected fixture hash mismatch: {key}")
                for replay in manifest.get("replays", []):
                    self.assertEqual(hashlib.sha256((case_dir / "assets" / replay["command"][1]).read_bytes()).hexdigest(), replay["script_sha256"])
        self.assertEqual(seen, intentional)
        for case_id in ("dev_001", "dev_002", "test_001", "test_002", "test_003", "test_004", "test_005", "test_006"):
            case = ROOT / ("dev_cases" if case_id.startswith("dev") else "test_cases") / case_id
            context = json.loads((case / "assets" / "transaction_context.json").read_text())
            self.assertGreaterEqual(expected_charge(case / "assets", context), 0)

    def test_pristine_baseline_is_valid_behavioral_zero(self):
        patch = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@\n+<!-- calibration baseline -->\n <div align=\"center\">\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_submission(delivery(root, patch, ["README.md"]), root / "work")
            result = execute_case(prepared, "test_001", root / "run", timeout=30)
        self.assertTrue(result["valid"])
        self.assertEqual(result["score"], 0)
        self.assertEqual(sum(item["possible"] for item in result["assertions"]), 100)

    def test_incomplete_control_is_behavioral_and_below_target(self):
        source = '''from pathlib import Path
import argparse, json
def main():
 p=argparse.ArgumentParser(); p.add_argument("--workspace"); p.add_argument("--output",required=True); p.add_argument("--ledger",required=True); p.add_argument("--session-store"); p.add_argument("--operation"); p.add_argument("--capsule-policy"); p.add_argument("--usage-statement"); p.add_argument("--budget-policy"); p.add_argument("--budget-store"); p.add_argument("--tenant-id"); p.add_argument("--verification-id"); p.add_argument("--request-id"); p.add_argument("--owner-id"); p.add_argument("--generation"); p.add_argument("--project-id"); a=p.parse_args(); out=Path(a.output); out.mkdir(parents=True,exist_ok=True)
 if a.session_store:
  (out/"error.json").write_text(json.dumps({"schema_version":1,"code":"not_implemented","errors":["transaction unavailable"]})); return 2
 ledger={"schema_version":1,"claims":[{"claim_id":"c1","evidence":"replay deviation 0.0"},{"claim_id":"c2","evidence":"coverage 0.0"}]}; report={"schema_version":1,"decision":"block","claim_actions":[{},{}],"evidence_coverage":0.0,"unresolved_items":["rerun with more seeds"],"replay_summary":{"deviation":0.0,"tolerance":0.000001}}; out.joinpath("claim_ledger.json").write_text(json.dumps(ledger)); out.joinpath("verification_report.json").write_text(json.dumps(report)); out.joinpath("validated_writeup.md").write_text("blocked draft replay more seeds")
 return 0
if __name__=="__main__": raise SystemExit(main())
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = "ai_scientist/claim_verification.py"
            prepared = prepare_submission(delivery(root, new_file_patch(path, source), [path]), root / "work")
            result = execute_case(prepared, "test_001", root / "run", timeout=30)
        self.assertTrue(result["valid"])
        self.assertLessEqual(result["score"], 25)
        self.assertGreaterEqual(result["score"], 0)

    def test_snapshot_has_no_entry_or_git_metadata(self):
        self.assertFalse((ROOT / "input" / "repository" / ".git").exists())
        self.assertFalse((ROOT / "input" / "repository" / "ai_scientist" / "claim_verification.py").exists())

    def test_complete_evaluator_control_scores_100_on_all_cases(self):
        sys.path.insert(0, str(ROOT / "evaluator" / "controls"))
        from build_reference import build  # noqa: WPS433
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            submission = build(root / "delivery")
            result_file = root / "result.json"
            completed = subprocess.run([sys.executable, str(ROOT / "evaluator" / "harness" / "run_hidden.py"), "--submission", str(submission), "--work-dir", str(root / "work"), "--result", str(result_file)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(result_file.read_text())
            self.assertTrue(result["valid"])
            self.assertEqual(result["scores"], [100.0] * 6)


if __name__ == "__main__":
    unittest.main()
