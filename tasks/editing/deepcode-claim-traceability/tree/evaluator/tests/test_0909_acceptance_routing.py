"""Provider-free regression tests for execution-namespace and exact-case routing."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("routing_axes_0909", ROOT / "evaluator/formal_axes.py")
axes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(axes)
shared = getattr(axes, "SHARED", None) or axes._shared


class ExactTaskRoutingTests(unittest.TestCase):
    def test_injection_uses_actual_function_namespace(self):
        self.assertIs(shared, shared["hidden_document"].__globals__)

    def test_subset_routes_actual_bytes_and_private_comparison(self):
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            case = run / "hidden/cases/test_001"
            case.mkdir(parents=True)
            task = case / "executed_task.md"
            task.write_text("The actual dynamic task, not the static template.")
            sha = hashlib.sha256(task.read_bytes()).hexdigest()
            comparison = case / "private-oracle-comparison.json"
            comparison.write_text(json.dumps({"case_id": "test_001", "candidate_visible": False,
                "executed_task_sha256": sha, "expected_invariants": {"accepted_revision": 3}}))
            record = {"case_id": "test_001", "executed_task_path": str(task), "executed_task_sha256": sha,
                "private_oracle_comparison_path": str(comparison),
                "private_oracle_comparison_sha256": hashlib.sha256(comparison.read_bytes()).hexdigest()}
            (run / "hidden/hidden-after-freeze-attestation.json").write_text(json.dumps({"cases": [record]}))
            overlay, routes = axes.prepare_run_local_formal_root(run, ("test_001",))
            self.assertEqual((overlay / "test_cases/test_001/input.md").read_bytes(), task.read_bytes())
            self.assertEqual(routes, {"test_001": comparison})
            with self.assertRaisesRegex(ValueError, "missing hidden record"):
                axes.prepare_run_local_formal_root(run)
            comparison.write_text("{}")
            with self.assertRaisesRegex(ValueError, "comparison"):
                axes.prepare_run_local_formal_root(run, ("test_001",))

    def test_exact_routing_changes_namespace_read_by_shared_finalizer(self):
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            (run / "hidden").mkdir()
            (run / "hidden/hidden-after-freeze-attestation.json").write_text("{}")
            comparison = run / "oracle.json"
            comparison.write_text('{"observed_revision": 3}')
            overlay = run / "overlay"
            original = shared["ROOT"]
            def fake_finalize(argv):
                self.assertEqual(shared["ROOT"], overlay)
                target = run / "scored-oracle.json"
                shared["provenance_summary"]("test_001", {}, run / "unused", target)
                self.assertEqual(target.read_bytes(), comparison.read_bytes())
                return 0
            with mock.patch.object(axes, "prepare_run_local_formal_root", return_value=(overlay, {"test_001": comparison})), \
                 mock.patch.dict(shared, {"selected_case_ids": lambda argv: ("test_001",), "main": fake_finalize}):
                if hasattr(axes, "_shared_main"):
                    with mock.patch.object(axes, "_shared_main", fake_finalize):
                        self.assertEqual(axes.main(["--run-dir", str(run), "--acceptance-cases", "test_001"]), 0)
                else:
                    self.assertEqual(axes.main(["--run-dir", str(run), "--acceptance-cases", "test_001"]), 0)
            self.assertEqual(shared["ROOT"], original)

if __name__ == "__main__":
    unittest.main()
