"""0920 hardening self-tests: dimensions, deterministic assertions, ceilings.

Provider-free.  Run with:

    python3 -m unittest evaluator.tests.test_0920_hardening -v

What it proves:

* the task-local Result dimensions are positive integers summing to 100, the
  rubric documents exactly those IDs, and the shared judge's own
  ``load_dimensions``/``schema_agrees_with_validator`` accept them;
* the deterministic surface assertions are satisfiable -- the behaviour the
  published spec describes turns every assertion true for all six hidden cases
  (this is ``reference_control.py``, executed here as a test);
* every ceiling is *determinate*: a reference-correct workspace issues no
  violated condition, and each degradation issues exactly the expected one;
* the ceilings are *enforced*, not advisory: the shared
  ``result_judge.validate_response`` rejects a judge answer that exceeds the
  contract's effective ceiling and accepts the same answer at the ceiling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")
SHARED_JUDGE = SHARED_ROOT / "result_judge.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# The shared judge imports its sibling transport modules by bare name, exactly as
# readiness_smoke.py does through PYTHONPATH.
if SHARED_ROOT.is_dir() and str(SHARED_ROOT) not in sys.path:
    sys.path.append(str(SHARED_ROOT))

from agentloop.evaluator import surface_assertions as sa  # noqa: E402
from evaluator.tests import reference_control as control  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAPS = _load(ROOT / "evaluator" / "result_score_caps.py", "openwiki_result_score_caps")
RUBRIC = ROOT / "agentloop" / "evaluator" / "result_rubric.md"
DIMENSIONS = ROOT / "agentloop" / "evaluator" / "result_dimensions.json"
CASES = tuple("test_%03d" % index for index in range(1, 7))


def _oracle_file(directory: Path, case_id: str, block: dict) -> Path:
    path = directory / "private-oracle-comparison.json"
    path.write_text(json.dumps({
        "schema_version": "openwiki-semantic-oracle-comparison/v1", "case_id": case_id,
        "candidate_visible": False, "deterministic_surface_assertions": block,
    }, sort_keys=True), encoding="utf-8")
    return path


def _stub(directory: Path, name: str, value: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


class DimensionContract(unittest.TestCase):
    def test_dimensions_sum_to_one_hundred(self):
        value = json.loads(DIMENSIONS.read_text(encoding="utf-8"))
        self.assertTrue(value)
        for name, maximum in value.items():
            self.assertIsInstance(maximum, int)
            self.assertGreater(maximum, 0)
        self.assertEqual(sum(value.values()), 100)
        self.assertEqual(json.loads((ROOT / "evaluator" / "result_dimensions.json").read_text()), value,
                         "the two result_dimensions.json copies must stay identical")

    def test_rubric_documents_exactly_those_dimensions(self):
        text = RUBRIC.read_text(encoding="utf-8")
        for name in json.loads(DIMENSIONS.read_text(encoding="utf-8")):
            self.assertIn(name, text, "rubric must describe dimension " + name)

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_shared_judge_accepts_the_dimensions_and_ceilings(self):
        judge = _load(SHARED_JUDGE, "openwiki_shared_result_judge")
        dimensions = judge.load_dimensions(judge.rubric_dimensions_path(RUBRIC))
        self.assertEqual(dimensions, json.loads(DIMENSIONS.read_text(encoding="utf-8")))
        semantic = {"c9_exact_retry_idempotency_not_demonstrated": 35,
                    "c10_fail_closed_boundary_not_demonstrated": 35,
                    "c11_production_cli_not_the_actor": 30}
        self.assertEqual(judge.schema_agrees_with_validator("test_001", dimensions, semantic), [])


class DeterministicAssertions(unittest.TestCase):
    def test_reference_behaviour_satisfies_every_assertion(self):
        for case_id in CASES:
            spec = control.load_manifest(case_id)
            with tempfile.TemporaryDirectory() as raw:
                initial, observed = control.build_reference_workspace(case_id, spec, Path(raw))
                value = sa.evaluate(case_id, spec, initial, observed)
            failed = sorted(name for name, passed in value["assertion_results"].items() if not passed)
            self.assertEqual(failed, [], "%s: reference behaviour must satisfy every assertion" % case_id)
            self.assertEqual(value["surface_families_satisfied_count"], value["surface_families_total"])
            self.assertTrue(value["complete_active_generation_observed"])
            self.assertEqual(value["observation_notes"], [])

    def test_an_empty_workspace_satisfies_only_the_scope_family(self):
        """A rollout that produced nothing broke no boundary, and delivered nothing."""
        spec = control.load_manifest("test_001")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "initial").mkdir()
            (root / "observed").mkdir()
            value = sa.evaluate("test_001", spec, root / "initial", root / "observed")
        self.assertEqual(value["surface_families_satisfied"],
                         {"change_impact": False, "static_publication": False,
                          "full_text_search": False, "durable_convergence": False,
                          "production_safety": True})
        self.assertFalse(value["complete_active_generation_observed"])

    def test_each_published_obligation_is_individually_detectable(self):
        spec = control.load_manifest("test_002")
        for name, mutate in control.DEGRADATIONS.items():
            with tempfile.TemporaryDirectory() as raw:
                observed = control.degrade_and_check("test_002", spec, Path(raw), name, mutate)
            self.assertIs(observed, False, "degrading %s must flip exactly that assertion" % name)


class Ceilings(unittest.TestCase):
    def _contract(self, directory: Path, case_id: str, block: dict, findings=None):
        oracle = _oracle_file(directory, case_id, block)
        native = _stub(directory, "launcher_result.json", {"valid": True})
        record = _stub(directory, "execution_record.json",
                       {"artifact_validation": {"quality_findings": list(findings or [])}})
        return CAPS.build_contract(case_id=case_id, rubric=RUBRIC, native_evidence=native,
                                   oracle_summary=oracle, execution_record=record)

    def test_reference_behaviour_triggers_no_ceiling(self):
        for case_id in CASES:
            spec = control.load_manifest(case_id)
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                initial, observed = control.build_reference_workspace(case_id, spec, root)
                block = sa.evaluate(case_id, spec, initial, observed)
                contract = self._contract(root, case_id, block)
            violated = sorted(entry["cap_id"] for entry in contract["entries"]
                              if entry["status"] == "violated")
            self.assertEqual(violated, [], "%s: correct behaviour must trigger no ceiling" % case_id)
            self.assertIsNone(CAPS.effective_ceiling(contract))
            semantic = sorted(entry["cap_id"] for entry in contract["entries"]
                              if entry["status"] == "semantic_review")
            self.assertEqual(len(semantic), 3)

    def test_empty_workspace_is_capped_at_the_coverage_floor(self):
        spec = control.load_manifest("test_001")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "initial").mkdir()
            (root / "observed").mkdir()
            block = sa.evaluate("test_001", spec, root / "initial", root / "observed")
            contract = self._contract(root, "test_001", block)
        # One family (production_safety) is vacuously satisfied by a rollout that
        # wrote nothing, so the graded coverage ceiling is BASE + one STEP.
        self.assertEqual(CAPS.effective_ceiling(contract), CAPS.COVERAGE_BASE + CAPS.COVERAGE_STEP)
        violated = {entry["cap_id"] for entry in contract["entries"] if entry["status"] == "violated"}
        self.assertIn("c1_no_complete_active_generation", violated)
        self.assertIn("c2_impact_evidence_unsound", violated)
        self.assertIn("c8_surface_family_coverage", violated)

    def test_artifact_contract_finding_is_its_own_ceiling(self):
        spec = control.load_manifest("test_003")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial, observed = control.build_reference_workspace("test_003", spec, root)
            block = sa.evaluate("test_003", spec, initial, observed)
            contract = self._contract(root, "test_003", block,
                                      findings=["artifact schema or claimed case fields are "
                                                "incomplete or inconsistent"])
        self.assertEqual(CAPS.effective_ceiling(contract), 55)

    def test_every_entry_carries_a_requirement_reference(self):
        spec = control.load_manifest("test_005")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            initial, observed = control.build_reference_workspace("test_005", spec, root)
            block = sa.evaluate("test_005", spec, initial, observed)
            contract = self._contract(root, "test_005", block)
        seen = set()
        for entry in contract["entries"]:
            self.assertNotIn(entry["cap_id"], seen)
            seen.add(entry["cap_id"])
            self.assertTrue(entry["requirement_ref"].strip())
            self.assertTrue(entry["reason"].strip())
            self.assertIn(entry["status"], {"violated", "not_violated", "unavailable", "semantic_review"})
            if entry["status"] != "unavailable":
                self.assertTrue(entry["evidence_refs"])

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_shared_judge_boundary_accepts_and_enforces_the_contract(self):
        judge = _load(SHARED_JUDGE, "openwiki_shared_result_judge_enforce")
        dimensions = json.loads(DIMENSIONS.read_text(encoding="utf-8"))
        spec = control.load_manifest("test_001")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "initial").mkdir()
            (root / "observed").mkdir()
            block = sa.evaluate("test_001", spec, root / "initial", root / "observed")
            oracle = _oracle_file(root, "test_001", block)
            native = _stub(root, "launcher_result.json", {"valid": True})
            record = _stub(root, "execution_record.json", {"artifact_validation": {"quality_findings": []}})
            path = root / "result_score_caps.json"
            CAPS.write_contract(path, case_id="test_001", rubric=RUBRIC, native_evidence=native,
                                oracle_summary=oracle, execution_record=record)
            inputs = {"rubric": RUBRIC, "native_evidence": native, "oracle_summary": oracle}
            cap, value = judge.load_score_caps(path, "test_001", inputs)
        semantic = {entry["cap_id"]: entry["maximum_score"] for entry in value["entries"]
                    if entry["status"] == "semantic_review"}

        def answer(total):
            names = list(dimensions)
            scores = {name: 0 for name in names}
            remaining = total
            for name in names:
                take = min(dimensions[name], remaining)
                scores[name] = take
                remaining -= take
            return json.dumps({
                "case_id": "test_001", "result_state": "scoreable", "result_score": total,
                "dimensions": {name: {"score": scores[name], "max": dimensions[name],
                                      "evidence": "self-test"} for name in names},
                "ceiling_assessments": {cap_id: {"violated": False, "evidence": "self-test"}
                                        for cap_id in semantic},
                "major_errors": [], "assessment": "self-test",
            })

        self.assertEqual(cap, CAPS.COVERAGE_BASE + CAPS.COVERAGE_STEP)
        _, errors = judge.validate_response(answer(45), "test_001", dimensions, cap, semantic)
        self.assertTrue(any("exceeds evidenced task-local ceiling" in error for error in errors),
                        "a 45 must be rejected against a %d ceiling" % cap)
        _, errors = judge.validate_response(answer(cap), "test_001", dimensions, cap, semantic)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
