"""Provider-free semantic/origin contract regressions, not product acceptance."""
import copy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agentloop"), str(ROOT / "evaluator"), str(ROOT / "evaluator/harness")]
import lower_agent_launcher as launcher
from scientific_capture import capture_science, public_checks, sha
from scientific_audit import assess_bundle, collect_scientific_audit
from oracles import SPECS


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


def authored_fixture():
    hashes = {"transaction_receipt.json": "b" * 64, "reproducibility_capsule.zip": "c" * 64}
    event = {"sequence": 1, "operation": "prepare", "exit_code": 0,
             "observation_digest": "d" * 64,
             "observation": {"receipt_digests": {"transaction_receipt.json": "b" * 64},
                             "receipt_projections": {"transaction_receipt.json": {"request_id": "request", "phase": "prepared"}}}}
    args = {"case_id": "test_006", "rollout_digest": "a" * 64, "hashes": hashes,
            "trajectory": [event], "trajectory_digest": "e" * 64, "runtime_digest": "f" * 64,
            "case_world_digest": "0" * 64}
    artifact = {"schema_version": 1, "case_id": "test_006", "rollout_digest": args["rollout_digest"],
                "artifact_path": "transaction_receipt.json", "artifact_digest": "b" * 64,
                "tool_events": launcher._expected_tool_events([event]),
                "observed_facts": launcher._expected_observed_facts([event], "e" * 64, "f" * 64, hashes, "0" * 64),
                "receipt_id": "request", "state": "prepared", "honest_recovery": True, "safe": False, "oracle_leak": False}
    return artifact, args


def science_fixture(root):
    """Minimal byte-binding fixture only; never claims to be a Candidate run."""
    p, w = root / "product", root / "workspace"
    p.mkdir(); w.mkdir()
    checks = public_checks()
    manifest = {"experiments": [], "release_policy": {"required_decision": "block"}}
    write(w / "evidence_manifest.json", manifest)
    att = {"schema_version": 1, "attestation_version": "4", "manifest_sha256": sha(w / "evidence_manifest.json"),
           "references": [], "metrics": [], "status": "verified"}
    att["digest"] = checks.canonical_sha256(att)
    (p / "validated_writeup.md").write_text("Scientific approval blocked; this is an archived audit.")
    contract = {"schema_version": 1, "contract_version": "5", "decision": "block", "expected_decision": "block",
                "claim_refs": [], "issue_kinds": [], "unresolved_items": [], "science_checks": {"bindings": True, "evidence": True},
                "evidence_attestation_digest": att["digest"], "writeup_sha256": sha(p / "validated_writeup.md")}
    contract["digest"] = checks.canonical_sha256(contract)
    write(p / "claim_ledger.json", {"claims": [], "evidence_attestation": att, "claim_contract": contract})
    write(p / "verification_report.json", {"decision": "block", "issues": [], "unresolved_items": [], "evidence_attestation": att,
          "evidence_attestation_digest": att["digest"], "claim_contract": contract})
    gate = {"schema_version": 1, "gate_version": "3", "decision": "block", "evidence_manifest_sha256": sha(w / "evidence_manifest.json"),
            "policy_sha256": checks.canonical_sha256(manifest["release_policy"]),
            "ledger_sha256": sha(p / "claim_ledger.json"), "report_sha256": sha(p / "verification_report.json"),
            "writeup_sha256": sha(p / "validated_writeup.md")}
    gate["digest"] = checks.canonical_sha256(gate)
    write(p / "transaction_receipt.json", {"phase": "committed", "science_gate": gate, "claim_contract_digest": contract["digest"]})
    with zipfile.ZipFile(p / "reproducibility_capsule.zip", "w") as archive:
        archive.writestr("capsule_manifest.json", json.dumps({"science_gate_digest": gate["digest"]}))
    return p, w


class ScientificSemanticsTests(unittest.TestCase):
    @unittest.skipUnless(Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py").is_file(), "remote shared production integration")
    def test_production_shared_accepts_task_caps_and_rejects_excess_without_clamping(self):
        shared = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
        spec = importlib.util.spec_from_file_location("agentswe_ai_production_cap_check", shared)
        judge = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(shared.parent), *sys.path]):
            spec.loader.exec_module(judge)
        from case_evidence import write_immutable_evidence
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            docs = {"native_evidence.json": {"observed": "test fixture"}, "oracle_summary.json": {"expected": "test fixture"}}
            write_immutable_evidence(root, docs)
            inputs = {"rubric": ROOT / "agentloop/result_rubric.md", "native_evidence": root / "native_evidence.json", "oracle_summary": root / "oracle_summary.json"}
            cap_id = "ai_semantic_claim_contract_v5"
            cap = {"schema_version": "agentswe-result-score-caps/v1", "case_id": "test_001",
                   **{key + "_sha256": sha(path) for key, path in inputs.items()},
                   "entries": [{"cap_id": cap_id, "maximum_score": 35, "status": "semantic_review", "requirement_ref": "input/03 §28", "reason": "Independent scientific adequacy review", "evidence_refs": ["native_evidence.json"]}]}
            write(root / "score_cap_contract.json", cap)
            ceiling, verified = judge.load_score_caps(root / "score_cap_contract.json", "test_001", inputs)
            self.assertIsNone(ceiling)
            self.assertEqual(verified["entries"], cap["entries"])
            dimensions = json.loads((ROOT / "evaluator/result_dimensions.json").read_text())
            response = {"case_id": "test_001", "result_state": "scoreable", "result_score": 100,
                        "dimensions": {key: {"score": maximum, "max": maximum, "evidence": "synthetic contract test only"} for key, maximum in dimensions.items()},
                        "major_errors": [], "assessment": "synthetic contract test only",
                        "ceiling_assessments": {cap_id: {"violated": False, "evidence": "Narrative adequate despite paraphrase"}}}
            _, errors = judge.validate_response(json.dumps(response), "test_001", dimensions, semantic_caps={cap_id: 35})
            self.assertEqual(errors, [])
            response["ceiling_assessments"][cap_id]["violated"] = True
            unchanged, errors = judge.validate_response(json.dumps(response), "test_001", dimensions, semantic_caps={cap_id: 35})
            self.assertTrue(any("ceiling" in error for error in errors))
            self.assertEqual(unchanged["result_score"], 100)

    def test_redundant_hash_typo_is_semantic_error_not_origin_failure(self):
        artifact, args = authored_fixture()
        artifact["observed_facts"]["final_release_artifact_hashes"] = {**args["hashes"], "reproducibility_capsule.zip": "c" * 65}
        original = copy.deepcopy(artifact)
        errors = launcher.validate_authored_artifact(artifact, **args)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["field"], "observed_facts.final_release_artifact_hashes")
        self.assertFalse(errors[0]["origin_failure"])
        self.assertEqual(artifact, original)

    def test_foreign_origin_and_unparseable_schema_still_rejected(self):
        artifact, args = authored_fixture()
        for field, value in (("case_id", "test_005"), ("rollout_digest", "1" * 64),
                             ("artifact_digest", "2" * 64), ("artifact_path", "../foreign.json"),
                             ("observed_facts", []), ("tool_events", {})):
            with self.subTest(field=field), self.assertRaises(launcher.ModelContentError):
                launcher.validate_authored_artifact({**artifact, field: value}, **args)

    def test_other_fact_misreports_are_retained_and_exposed(self):
        artifact, args = authored_fixture()
        artifact["observed_facts"]["operations"] = ["commit"]
        artifact["receipt_id"] = "made-up-receipt"
        artifact["state"] = "made-up-state"
        errors = launcher.validate_authored_artifact(artifact, **args)
        self.assertEqual({item["field"] for item in errors}, {"observed_facts.operations", "receipt_id", "state"})

    def test_captured_model_response_and_persisted_json_must_agree(self):
        artifact, _ = authored_fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); write(root / "model_final_response.txt", artifact)
            record = {"authoring_response_sha256": sha(root / "model_final_response.txt")}
            launcher.validate_author_response_origin(artifact, record, root)
            with self.assertRaises(launcher.ModelContentError):
                launcher.validate_author_response_origin({**artifact, "safe": True}, record, root)
            (root / "model_final_response.txt").write_text("{}")
            with self.assertRaises(launcher.ModelContentError):
                launcher.validate_author_response_origin(artifact, record, root)

    def test_block_archive_is_not_a_gate_violation_and_phrases_only_need_semantic_review(self):
        with tempfile.TemporaryDirectory() as temp:
            p, w = science_fixture(Path(temp))
            result = assess_bundle(product=p, workspace=w, spec=SPECS["test_001"], evidence_ref="unit")
            self.assertEqual(result["scientific_decision"], "block")
            self.assertEqual(result["archive_phase"], "committed")
            self.assertEqual(result["gate"]["status"], "not_violated")
            self.assertEqual(result["contract"]["status"], "semantic_review")
            self.assertFalse(result["legacy_phrase_checks_diagnostic_only"]["evidence"])

    def test_generated_bundle_missing_gate_or_contract_is_definite_violation(self):
        with tempfile.TemporaryDirectory() as temp:
            p, w = science_fixture(Path(temp))
            write(p / "transaction_receipt.json", {})
            ledger = json.loads((p / "claim_ledger.json").read_text()); ledger.pop("claim_contract")
            write(p / "claim_ledger.json", ledger)
            result = assess_bundle(product=p, workspace=w, spec=SPECS["test_001"], evidence_ref="unit")
            self.assertEqual(result["gate"]["status"], "violated")
            self.assertEqual(result["contract"]["status"], "violated")

    def test_no_scientific_bundle_is_unavailable_not_fabricated_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            result = assess_bundle(product=Path(temp), workspace=Path(temp), spec=SPECS["test_001"], evidence_ref="unit")
            self.assertEqual(result["gate"]["status"], "unavailable")
            self.assertEqual(result["contract"]["status"], "unavailable")

    def test_stage_science_survives_cancel_without_becoming_model_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); product, workspace = science_fixture(root)
            output = root / "run"; output.mkdir()
            stage = output / "state/sessions/stages/current"; stage.mkdir(parents=True)
            for name in ("claim_ledger.json", "verification_report.json", "validated_writeup.md", "reproducibility_capsule.zip"):
                (stage / name).write_bytes((product / name).read_bytes())
            receipt = json.loads((product / "transaction_receipt.json").read_text())
            receipt["stage_path"] = "stages/current"
            write(output / "transaction_receipt.json", receipt)
            capture = capture_science(output=output, workspace=workspace, context={}, sequence=1,
                                      receipts={"transaction_receipt.json": receipt})
            (stage / "verification_report.json").write_text("{}")
            audit = collect_scientific_audit(base=output, events=[{"sequence": 1, "scientific_capture": capture}], spec=SPECS["test_001"])
            self.assertEqual(audit["bundles"][0]["scientific_decision"], "block")
            self.assertTrue(audit["bundles"][0]["capture"]["not_model_authored"])
            self.assertFalse((output / "agent_result.json").exists())
            snapshot = output / capture["path"]
            (snapshot.parent / "product/verification_report.json").write_text("{}")
            with self.assertRaises(ValueError):
                collect_scientific_audit(base=output, events=[{"sequence": 1, "scientific_capture": capture}], spec=SPECS["test_001"])

    def test_all_eight_tasks_state_two_decision_axes_without_changing_fixtures(self):
        tasks = sorted((ROOT / "agentloop/cases").glob("*/task.md"))
        self.assertEqual(len(tasks), 8)
        for task in tasks:
            self.assertIn("Scientific approval and audit archival are distinct", task.read_text())

    def test_changed_evidence_rejected_before_any_byte_overwrite(self):
        from case_evidence import write_immutable_evidence
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = {"native_evidence.json": {"observed": 1}, "oracle_summary.json": {"expected": 1}}
            write_immutable_evidence(root, original)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            write_immutable_evidence(root, original)
            with self.assertRaisesRegex(ValueError, "new scoring directory"):
                write_immutable_evidence(root, {"new-input.json": {"new": True}, **original,
                                               "oracle_summary.json": {"expected": 2}})
            self.assertFalse((root / "new-input.json").exists())
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})

    def test_rejected_external_capsule_corruption_is_not_an_unsafe_candidate_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); product, workspace = science_fixture(root)
            (product / "reproducibility_capsule.zip").write_bytes(b"externally corrupted fixture")
            capture = capture_science(output=product, workspace=workspace, context={}, sequence=1,
                                      receipts={"transaction_receipt.json": json.loads((product / "transaction_receipt.json").read_text())})
            event = {"sequence": 1, "scientific_capture": capture, "operation": "commit", "exit_code": 2}
            rejected = collect_scientific_audit(base=product, events=[event], spec=SPECS["test_001"])
            self.assertFalse(rejected["bundles"][0]["cap_applicable"])
            self.assertEqual(rejected["result_score_caps"][0]["status"], "unavailable")
            accepted = collect_scientific_audit(base=product, events=[{**event, "exit_code": 0}], spec=SPECS["test_001"])
            self.assertEqual(accepted["result_score_caps"][0]["status"], "violated")


if __name__ == "__main__": unittest.main()
