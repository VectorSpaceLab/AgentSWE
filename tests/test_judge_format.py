"""Release judge format (AGENTSWE_JUDGE_FORMAT): identity echo binding (QA, Security) and PDF re-verification.

    python3 -m unittest tests/test_judge_format.py      (standard library only; no model calls)
"""
from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tasks" / "creation" / "{task}" / "adapter" / "eval-template"


def load(task: str, rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, Path(str(TEMPLATE).format(task=task)) / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QA = load("evidence-grounded-document-qa", "solution/run_eval.py", "qa_run_eval")
SEC = load("authorized-vulnerability-validation", "solution/run_eval.py", "security_run_eval")
PDF_EVAL = load("scientific-pdf-translation", "solution/evidence_bundle.py", "pdf_bundle_eval")
PDF_VERIFY = load("scientific-pdf-translation", "tests/evidence_bundle.py", "pdf_bundle_verify")
SHA = "a" * 64


def dimension_response(module, *, dimension, maximum, **override):
    response = {"protocol": module.QUALITY_REVIEW_PROTOCOL, "case_id": "dev_001", "evidence_bundle_sha256": SHA,
                "review_id": "r-1", "dimension": dimension, "max": maximum, "score": maximum // 2,
                "evidence": ["observed in the frozen bundle"], "deductions": [], "major_errors": [],
                "assessment": "grounded in the frozen evidence", "rationale": "grounded in the frozen evidence"}
    response.update(override)
    return {k: v for k, v in response.items() if v is not ...}


class EchoBinding(unittest.TestCase):
    def check(self, module):
        dimension, maximum = next(iter(module.DIMENSION_MAXIMA.items()))
        call = dict(case_id="dev_001", dimension=dimension, maximum=maximum,
                    identity={"evidence_bundle_sha256": SHA}, review_id="r-1")
        for mode in ("exact", "release"):
            with self.subTest(module=module.__name__, mode=mode, echo="correct"):
                module.JUDGE_FORMAT, module.JUDGE_ECHO_REPAIRS[:] = mode, []
                module.validate_dimension_response(dimension_response(module, dimension=dimension, maximum=maximum), **call)
                self.assertEqual(module.JUDGE_ECHO_REPAIRS, [])
        with self.subTest(module=module.__name__, mode="exact", echo="wrong"):
            module.JUDGE_FORMAT, module.JUDGE_ECHO_REPAIRS[:] = "exact", []
            with self.assertRaisesRegex(ValueError, "invalid evidence_bundle_sha256"):
                module.validate_dimension_response(dimension_response(
                    module, dimension=dimension, maximum=maximum, evidence_bundle_sha256="b" * 64), **call)
        with self.subTest(module=module.__name__, mode="exact", echo="missing"):
            with self.assertRaisesRegex(ValueError, "invalid evidence_bundle_sha256"):
                module.validate_dimension_response(dimension_response(
                    module, dimension=dimension, maximum=maximum, evidence_bundle_sha256=...), **call)
        with self.subTest(module=module.__name__, mode="release", echo="missing"):
            module.JUDGE_FORMAT, module.JUDGE_ECHO_REPAIRS[:] = "release", []
            out = module.validate_dimension_response(dimension_response(
                module, dimension=dimension, maximum=maximum, evidence_bundle_sha256=...), **call)
            self.assertEqual(out["evidence_bundle_sha256"], SHA)
            self.assertEqual([(r["field"], r["echo"]) for r in module.JUDGE_ECHO_REPAIRS], [("evidence_bundle_sha256", "missing")])
        with self.subTest(module=module.__name__, mode="release", echo="wrong"):
            module.JUDGE_FORMAT, module.JUDGE_ECHO_REPAIRS[:] = "release", []
            out = module.validate_dimension_response(dimension_response(
                module, dimension=dimension, maximum=maximum, evidence_bundle_sha256="b" * 64, review_id="r-9"), **call)
            self.assertEqual((out["evidence_bundle_sha256"], out["review_id"]), (SHA, "r-1"))  # the request wins
            self.assertEqual(sorted((r["field"], r["echo"]) for r in module.JUDGE_ECHO_REPAIRS),
                             [("evidence_bundle_sha256", "wrong"), ("review_id", "wrong")])
        with self.subTest(module=module.__name__, mode="release", check="score bounds still enforced"):
            with self.assertRaisesRegex(ValueError, "outside rubric bounds"):
                module.validate_dimension_response(dimension_response(
                    module, dimension=dimension, maximum=maximum, score=maximum + 1), **call)

    def test_qa_dimension(self):
        self.check(QA)

    def test_security_dimension(self):
        self.check(SEC)

    def test_qa_image_wrong_echo(self):
        image = {"id": "img-1", "sha256": "c" * 64}
        response = {"protocol": QA.QUALITY_REVIEW_PROTOCOL, "case_id": "dev_001", "evidence_bundle_sha256": "d" * 64,
                    "image_review_id": "i-1", "image_id": "img-1", "image_sha256": "c" * 64,
                    "visible_observations": ["a chart"], "readability": "clear", "locator_assessment": "inspectable",
                    "defects": []}
        kwargs = dict(case_id="dev_001", identity={"evidence_bundle_sha256": SHA}, image_record=image, review_id="i-1")
        QA.JUDGE_FORMAT, QA.JUDGE_ECHO_REPAIRS[:] = "exact", []
        with self.assertRaisesRegex(ValueError, "image response has invalid evidence_bundle_sha256"):
            QA.validate_image_response(dict(response), **kwargs)
        QA.JUDGE_FORMAT = "release"
        out = QA.validate_image_response(dict(response), **kwargs)
        self.assertEqual(out["evidence_bundle_sha256"], SHA)
        self.assertEqual([(r["review"], r["field"]) for r in QA.JUDGE_ECHO_REPAIRS], [("image", "evidence_bundle_sha256")])


def pdf_case():
    units = [f"u{i}" for i in range(10)]
    bundle = {"source_pages": [{"page": 1, "units": [{"id": u} for u in units]}],
              "target_pages": [{"page": 1}, {"page": 2}],
              "alignment": {"functional": True}, "viewer": {"functional": True}}
    coverage = [{"source_unit": u, "status": "omitted" if u in ("u8", "u9") else "translated",
                 "evidence": "" if u in ("u8", "u9") else "target p1"} for u in units]
    ceilings = {name: {"applies": False, "evidence": "not observed", "source_units": [], "target_pages": []}
                for name in PDF_EVAL.CEILINGS}
    ceilings["material_meaning_reversal"] = {"applies": True, "evidence": "reversed claim",
                                             "source_units": ["u1", "u404"], "target_pages": [1]}
    return {"unit_coverage": coverage, "ceilings": ceilings}, bundle


class PdfReverification(unittest.TestCase):
    def test_copies_identical(self):
        base = Path(str(TEMPLATE).format(task="scientific-pdf-translation"))
        self.assertEqual((base / "solution/evidence_bundle.py").read_bytes(), (base / "tests/evidence_bundle.py").read_bytes())

    def test_exact_mode_keeps_paper_rules(self):
        review, bundle = pdf_case()
        with self.assertRaisesRegex(ValueError, "unknown ceiling source unit"):
            PDF_EVAL.validate_quality_review(copy.deepcopy(review), bundle, "exact")

    def test_release_mode_is_consistent_across_evaluation_and_verification(self):
        review, bundle = pdf_case()
        evaluated = copy.deepcopy(review)
        decision = PDF_EVAL.validate_quality_review(evaluated, bundle, "release")  # run_eval mutates the review
        names = [item["name"] for item in decision["applied_ceilings"]]
        self.assertEqual(names, ["material_meaning_reversal", "10_to_25pct_missing"])
        self.assertEqual(decision["maximum_total"], 45)
        # verify_score re-validates the stored (repaired) review with the same rules and must agree
        self.assertEqual(PDF_VERIFY.validate_quality_review(copy.deepcopy(evaluated), bundle, "release"), decision)

    def test_release_still_requires_grounded_judge_ceilings(self):
        review, bundle = pdf_case()
        review["ceilings"]["material_scientific_corruption"] = {"applies": True, "evidence": "claimed",
                                                               "source_units": [], "target_pages": []}
        with self.assertRaisesRegex(ValueError, "applied core ceiling needs exact source and target references"):
            PDF_EVAL.validate_quality_review(review, bundle, "release")


if __name__ == "__main__":
    unittest.main()
