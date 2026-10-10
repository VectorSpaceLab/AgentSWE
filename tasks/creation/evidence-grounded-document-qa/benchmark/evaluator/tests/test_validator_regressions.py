#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


EVALUATOR = Path(__file__).resolve().parents[1]
ROOT = EVALUATOR.parent
FIXTURE = Path(__file__).with_name("make_contract_fixture.py")


def run_validator(script: str, case: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EVALUATOR / script), str(case), str(output)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


class ValidatorRegressionTests(unittest.TestCase):
    def make_output(self, directory: Path) -> tuple[Path, Path]:
        case = ROOT / "dev_cases" / "dev_001"
        output = directory / "output"
        subprocess.run([sys.executable, str(FIXTURE), str(case), str(output)], check=True)
        return case, output

    def test_valid_fixture_passes_both_validators(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            self.assertEqual(run_validator("validate_artifacts.py", case, output).returncode, 0)
            self.assertEqual(run_validator("validate_source_native_review.py", case, output).returncode, 0)

    def test_bad_bundle_bytes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            bundle_path = output / "source_bundle.json"
            bundle = json.loads(bundle_path.read_text())
            bundle["sources"][0]["bytes_base64"] = base64.b64encode(b"wrong bytes").decode()
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            result = run_validator("validate_source_native_review.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not preserve exact asset bytes", result.stdout)

    def test_claim_manifest_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            manifest_path = output / "review_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["claim_targets"]["drifted"] = manifest["claim_targets"].pop("fixture-claim")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = run_validator("validate_source_native_review.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("claim IDs do not exactly equal", result.stdout)

    def test_manifest_dom_targets_cannot_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            manifest_path = output / "review_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["claim_targets"]["fixture-claim"]["dom_id"] = "evidence-fixture-evidence"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            review_path = output / "review.html"
            review = re.sub(r'<button id="claim-fixture-claim"[^>]*>.*?</button>', "", review_path.read_text(), count=1)
            review_path.write_text(review, encoding="utf-8")
            result = run_validator("validate_source_native_review.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DOM target IDs must be globally unique", result.stdout)

    def test_wrong_source_byte_range_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            claims_path = output / "claims_and_citations.json"
            claims = json.loads(claims_path.read_text())
            locator = claims["claims"][0]["supporting_evidence"][0]["locator"]
            locator["byte_start"], locator["byte_end"] = 0, 20
            claims_path.write_text(json.dumps(claims), encoding="utf-8")
            result = run_validator("validate_artifacts.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not contain the requested element ID", result.stdout)

    def test_success_report_requires_exact_artifact_inventory_and_error_array(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            report_path = output / "run_report.json"
            report = json.loads(report_path.read_text())
            report["artifact_paths"] = []
            report["errors"] = "not-an-array"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            result = run_validator("validate_artifacts.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("artifact_paths must list all six owned artifacts", result.stdout)
            self.assertIn("errors must be an array of strings", result.stdout)

    def test_claim_status_must_match_available_evidence_relations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            claims_path = output / "claims_and_citations.json"
            claims = json.loads(claims_path.read_text())
            claim = claims["claims"][0]
            evidence = claim["supporting_evidence"].pop()
            evidence["relation"] = "contradicts"
            claim["contradicting_evidence"] = [evidence]
            claims_path.write_text(json.dumps(claims), encoding="utf-8")
            manifest_path = output / "review_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["evidence_targets"]["fixture-evidence"]["relation"] = "contradicts"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = run_validator("validate_artifacts.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires supporting/scope evidence", result.stdout)

    def test_generic_html_byte_range_is_not_accepted_as_source_native(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            claims_path = output / "claims_and_citations.json"
            claims = json.loads(claims_path.read_text())
            evidence = claims["claims"][0]["supporting_evidence"][0]
            old = evidence["locator"]
            replacement = {"kind": "byte_range", "byte_start": old["byte_start"], "byte_end": old["byte_end"]}
            evidence["locator"] = replacement
            claims_path.write_text(json.dumps(claims), encoding="utf-8")
            manifest_path = output / "review_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["evidence_targets"]["fixture-evidence"]["native_locator"] = replacement
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = run_validator("validate_artifacts.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not source-native", result.stdout)

    def test_relative_file_dependency_in_review_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            review_path = output / "review.html"
            review = review_path.read_text(encoding="utf-8").replace(
                "<body>",
                '<body><img src="../../dev_cases/dev_001/assets/inverter_map.svg" alt="external">',
                1,
            )
            review_path.write_text(review, encoding="utf-8")
            result = run_validator("validate_source_native_review.py", case, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-self-contained element dependencies", result.stdout)

    def test_script_url_calls_are_not_css_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            review_path = output / "review.html"
            script = (
                "<script>function unused(blob,target,href){var u=URL.createObjectURL(blob);"
                "URL.revokeObjectURL(u);return [exactSourceUrl(target.source_id),new URL(href)];}</script>"
            )
            review = review_path.read_text(encoding="utf-8").replace("</body>", script + "</body>", 1)
            review_path.write_text(review, encoding="utf-8")
            result = run_validator("validate_source_native_review.py", case, output)
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_css_url_references_are_rejected(self) -> None:
        cases = {
            "style element": ("</style>", "body{background:url(../assets/bg.png)}</style>"),
            "style import": ("</style>", "@import \"theme.css\";</style>"),
            "style attribute": ("<body>", '<body><div style="background-image:url(bg.png)">x</div>'),
            "svg attribute": ("<body>", '<body><svg><rect fill="url(other.svg#g)"/></svg>'),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temp:
                case, output = self.make_output(Path(temp))
                review_path = output / "review.html"
                review_path.write_text(review_path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
                result = run_validator("validate_source_native_review.py", case, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("non-self-contained CSS reference", result.stdout)

    def test_css_fragment_and_data_references_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case, output = self.make_output(Path(temp))
            review_path = output / "review.html"
            review = review_path.read_text(encoding="utf-8").replace(
                "<body>",
                '<body><svg><defs><linearGradient id="g"></linearGradient></defs><rect fill="url(#g)"/></svg>'
                '<div style="background:url(data:image/gif;base64,R0lGODlhAQABAAAAACw=)">x</div>',
                1,
            )
            review_path.write_text(review, encoding="utf-8")
            result = run_validator("validate_source_native_review.py", case, output)
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_benchmark_rejects_missing_shared_code_contract_example(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            benchmark = Path(temp) / "benchmark"
            shutil.copytree(
                ROOT,
                benchmark,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "node_modules", ".cache", ".tmp_*"),
            )
            rubric_path = benchmark / "evaluator" / "code_rubric.md"
            rubric = rubric_path.read_text(encoding="utf-8")
            rubric = re.sub(r"```json\s*\{.*?\}\s*```", "", rubric, count=1, flags=re.S)
            rubric_path.write_text(rubric, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(benchmark / "evaluator" / "validate_benchmark.py"), str(benchmark)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one parseable JSON example object", result.stdout)


if __name__ == "__main__":
    unittest.main()
