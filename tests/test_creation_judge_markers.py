"""Creation judge infrastructure markers: only a standalone token voids a scored case.

    python3 -m unittest tests/test_creation_judge_markers.py      (standard library only; no model calls)

Every Creation eval's `normalize_evaluation_state` decides whether a judged case is an infrastructure error (not
scoreable). A case is an infrastructure error when the harness or the transport says so, when the judge's structured
`evaluation_state` is `infrastructure_error`, or when the judge's `assessment` / `major_errors` contain the
standalone token INFRASTRUCTURE_ERROR or INFRASTRUCTURE_INVALID: underscores, any case, and no letter, digit or
underscore immediately before or after it.

The previous rule replaced every non-alphanumeric character with `_` and matched substrings, so judge prose such as
"evaluator-side infrastructure errors" became INFRASTRUCTURE_ERRORS and voided a case the judge had scored 32/100 (a
web-research-report smoke). The first test below uses that judge output verbatim.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CREATION = ROOT / "tasks" / "creation"
TASKS = sorted(p.name for p in CREATION.iterdir()
               if (p / "adapter" / "eval-template" / "solution" / "run_eval.py").is_file())


def load_normalizer(task: str):
    """Load one task's run_eval.py without leaking its sys.path entry or sibling imports into other tests."""
    path = CREATION / task / "adapter" / "eval-template" / "solution" / "run_eval.py"
    saved_path, saved_modules = list(sys.path), set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(f"judge_markers_{task.replace('-', '_')}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            sys.modules.pop(name, None)
    return module.normalize_evaluation_state


NORMALIZERS = {task: load_normalizer(task) for task in TASKS}
HARNESS = {"evaluation_state": "scoreable", "fatal_gate": False, "fatal_reasons": [], "validity_gate": True}

# The judge output of the web-research-report smoke (score 32), assessment and major_errors verbatim. The run wrote
# the normalised state over the judge's own field; the judge was asked for, and scored as, "scoreable".
WEB_SMOKE_JUDGE = {
    "case_id": "test_001",
    "evaluation_state": "scoreable",
    "validity_gate": True,
    "score": 32,
    "dimensions": {},
    "assessment": (
        "Artifacts are structurally valid, but report.md is not the requested technical landscape decision report: "
        "it explicitly states the synthesis stage was unavailable and is essentially a large raw evidence dump. It "
        "gives no shortlist, confidence level, decision, version-pinned comparison matrix, evidence-comparability "
        "table, conflict analysis, or six-week pilot protocol. It also violates the research cutoff by using "
        "post-2026-06-30 Marker 2 material as evidence, and its evidence graph omits contradictions, dependence "
        "relations, and calculations for quantitative benchmark material. Score 32/100."),
    "major_errors": [
        "No decision, shortlist, confidence level, or recommendation for the six-week offline pilot.",
        "Required pilot protocol, version-pinned comparison matrix, evidence-comparability table, and conflict "
        "analysis are absent.",
        "Research cutoff is violated by using 2026-07-20 Marker 2 release/blog material as evidence and by mixing "
        "post-cutoff Marker versions without resolution.",
        "Evidence graph omits contradictions, shared benchmark/data dependence, and calculation records; all "
        "contradicts arrays are empty.",
        "Material Marker claims rely on S7 passages with exact_quote_found=false in harness validation, while "
        "S12/S14 are marked full_page despite evaluator-side infrastructure errors.",
        "Report length is roughly 32k words instead of the requested 2,000-2,600 words, and run_report/harness word "
        "counts disagree.",
    ],
}


def judge(assessment: str = "Artifacts are valid; ordinary rubric deductions apply.", major_errors=(), **fields):
    value = {"evaluation_state": "scoreable", "score": 40, "dimensions": {},
             "assessment": assessment, "major_errors": list(major_errors)}
    value.update(fields)
    return value


class StandaloneMarkers(unittest.TestCase):
    def check(self, model_result, expected, harness=None, transport_errors=()):
        self.assertEqual(len(NORMALIZERS), 10)
        for task, normalize in NORMALIZERS.items():
            with self.subTest(task=task):
                state = normalize(dict(model_result), dict(harness or HARNESS), list(transport_errors))
                self.assertEqual(state, expected)

    def test_real_web_smoke_prose_is_scoreable(self):
        self.check(WEB_SMOKE_JUDGE, "scoreable")

    def test_standalone_infrastructure_invalid_marker(self):
        self.check(judge("INFRASTRUCTURE_INVALID: the frozen evidence bundle could not be read."),
                   "infrastructure_error")

    def test_standalone_markers_any_case_and_punctuation(self):
        for text in ("infrastructure_error", "Infrastructure_Invalid", "(INFRASTRUCTURE_ERROR)",
                     "`INFRASTRUCTURE_INVALID`", "state: INFRASTRUCTURE_ERROR.", "INFRASTRUCTURE_ERROR-retry"):
            with self.subTest(text=text):
                self.check(judge(major_errors=[f"Evaluator note {text} recorded."]), "infrastructure_error")
                self.check(judge(text), "infrastructure_error")

    def test_structured_infrastructure_error_state(self):
        self.check(judge(evaluation_state="infrastructure_error"), "infrastructure_error")

    def test_longer_identifier_is_not_a_marker(self):
        self.check(judge("INFRASTRUCTURE_ERRORS were seen in the candidate's own log."), "scoreable")

    def test_marker_inside_a_longer_token_is_not_a_marker(self):
        for text in ("EVAL_INFRASTRUCTURE_ERROR", "INFRASTRUCTURE_ERROR2", "INFRASTRUCTURE_INVALIDATED",
                     "xINFRASTRUCTURE_INVALID", "INFRASTRUCTURE_ERROR_COUNT"):
            with self.subTest(text=text):
                self.check(judge(major_errors=[text]), "scoreable")

    def test_prose_without_underscore_is_not_a_marker(self):
        for text in ("evaluator-side infrastructure errors", "an infrastructure error in the candidate's sandbox",
                     "Infrastructure-invalid sources were cited", "INFRASTRUCTURE ERROR"):
            with self.subTest(text=text):
                self.check(judge(text), "scoreable")

    def test_harness_and_transport_still_decide(self):
        self.check(judge(), "infrastructure_error", transport_errors=["HTTP 503 after 5 attempts"])
        self.check(judge(), "infrastructure_error", harness=dict(HARNESS, evaluation_state="infrastructure_error"))

    def test_ordinary_states_pass_through(self):
        self.check(judge(), "scoreable")
        self.check(judge(evaluation_state="fatal_zero", score=0), "fatal_zero",
                   harness=dict(HARNESS, evaluation_state="fatal_zero", fatal_gate=True))


if __name__ == "__main__":
    unittest.main()
