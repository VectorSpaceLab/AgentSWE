"""Provider-free tests for the explicit Dyad v2 readiness selector."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMAL = ROOT / "harbor/formal_one_stop.py"
PROFILE = "single-dev-two-round-hidden-smoke-v1"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-E", "-s", "-B", str(FORMAL), *args],
                          text=True, capture_output=True, check=False)


class DyadV2DispatchSelectorTests(unittest.TestCase):
    def test_dispatch_profile_metadata_is_explicit(self):
        value = json.loads((ROOT / "meta/dispatch_profile.json").read_text())
        self.assertEqual(value["profile"], PROFILE)
        self.assertEqual(value["selector"], "--readiness-profile " + PROFILE)
        self.assertEqual(value["mode"], "--pilot")
        self.assertEqual(value["public_cases"], ["dev_001"])
        self.assertEqual(value["required_valid_rounds"], 2)
        self.assertEqual(value["max_dev_rounds"], 2)
        self.assertEqual(value["hidden_cases_after_freeze"], ["test_001"])
        self.assertEqual(value["n_concurrent"], 1)
        self.assertTrue(value["same_builder_session"])
        self.assertTrue(value["exact_feedback_digest"])
        self.assertTrue(value["distinct_candidate_digest"])
        self.assertTrue(value["provider_free_staging"])
        self.assertFalse(value["production_deployed"])
        self.assertFalse(value["registry_or_gate_written"])

    def test_v2_dry_run_selects_single_dev_and_two_rounds(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_cli("--pilot", "--dry-run", "--readiness-profile", PROFILE,
                             "--run-dir", str(Path(raw) / "v2"),
                             "--max-dev-rounds", "2", "--n-concurrent", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["readiness_profile"], PROFILE)
        self.assertEqual(value["public_cases"], ["dev_001"])
        self.assertEqual(value["hidden_cases"], ["test_001"])
        self.assertEqual(value["required_valid_rounds"], 2)
        self.assertEqual(value["max_dev_rounds"], 2)
        self.assertEqual(value["n_concurrent"], 1)
        self.assertEqual(value["provider_calls"], 0)
        self.assertFalse(value["pilot_execution_started"])

    def test_v2_runtime_source_contains_profile_bound_shape(self):
        source = FORMAL.read_text(encoding="utf-8")
        self.assertIn("readiness_profile = args.readiness_profile", source)
        self.assertIn('public_cases = ("dev_001",) if v2 else DEV_CASES', source)
        self.assertIn('hidden_cases = ("test_001",) if (pilot or v2) else HIDDEN_CASES', source)
        self.assertIn('required_valid_rounds=2 if v2 else None', source)
        self.assertIn('len(records) == lifecycle.required_valid_rounds', source)

    def test_v2_builder_attestation_requires_exactly_two_records(self):
        import tempfile
        from types import SimpleNamespace
        import sys
        sys.path.insert(0, str(ROOT / "harbor"))
        from harbor import formal_one_stop as module
        def record(number, digest, ack=None):
            return {"submission_number": number, "candidate_digest": digest,
                    "feedback_digest_ack": ack,
                    "dev": [{"semantic_feedback": {"contract_valid": True,
                        "round_consumed": True, "classification": "scoreable", "score": 80}}]}
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            common = {
                "public_cases": ("dev_001",), "max_dev_rounds": 2,
                "required_valid_rounds": 2, "readiness_profile": PROFILE,
                "feedback_digest": "f" * 64, "freeze_manifest": {"ok": True},
                "session_id": "session", "connection_id": "connection",
                "_public_valid": staticmethod(lambda item: True),
                "preflight_count": 0, "events": [], "run_dir": run,
                "native_attestation": lambda code: {"valid": True},
                "witness": lambda code: None,
            }
            one = SimpleNamespace(records=[record(1, "a" * 64)], **common)
            self.assertFalse(module.builder_attestation(one, 0, 1, 2)["complete"])
            two = SimpleNamespace(records=[record(1, "a" * 64), record(2, "b" * 64, "f" * 64)], **common)
            self.assertTrue(module.builder_attestation(two, 0, 1, 2)["complete"])

    def test_v2_selector_rejects_wrong_round_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_cli("--pilot", "--dry-run", "--readiness-profile", PROFILE,
                             "--run-dir", str(Path(raw) / "v2"),
                             "--max-dev-rounds", "3", "--n-concurrent", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --max-dev-rounds 2", result.stderr)

    def test_v2_selector_rejects_legacy_formal_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_cli("--run-formal", "--readiness-profile", PROFILE,
                             "--run-dir", str(Path(raw) / "v2"),
                             "--max-dev-rounds", "2", "--n-concurrent", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --pilot", result.stderr)

    def test_legacy_pilot_dry_run_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_cli("--pilot", "--dry-run", "--run-dir", str(Path(raw) / "legacy"))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertIsNone(value["readiness_profile"])
        self.assertEqual(value["public_cases"], ["dev_001", "dev_002"])
        self.assertEqual(value["hidden_cases"], ["test_001"])
        self.assertEqual(value["max_dev_rounds"], 10)
        self.assertEqual(value["n_concurrent"], 1)
        self.assertEqual(value["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
