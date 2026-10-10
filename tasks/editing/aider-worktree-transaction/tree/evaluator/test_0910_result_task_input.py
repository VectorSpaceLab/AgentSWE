"""0-API controls using the real lower's captured task projection statements."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluator import formal_finalize as finalizer
from harbor import agentloop_controller as controller


def lower_projection(spec, case_work):
    """Execute the four actual task-writing statements, with no lower launch."""
    path = ROOT / "evaluator/harness/run_lower_agent_case.py"
    tree = ast.parse(path.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    selected = []
    for node in main.body:
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"task_input", "task_digest"} for target in node.targets)):
            selected.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            text = ast.get_source_segment(path.read_text(), node)
            if text.startswith('(root / "task_input.md").write_text') or text.startswith('(root / "task_input.sha256").write_text'):
                selected.append(node)
    if len(selected) != 4:
        raise AssertionError("actual lower task projection changed; review captured control")
    case_work.mkdir(parents=True)
    captured = ast.Module(body=selected, type_ignores=[])
    exec(compile(ast.fix_missing_locations(captured), str(path), "exec"),
         {"spec": spec, "root": case_work, "hashlib": hashlib})


class ResultTaskInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aider-result-task-control-")
        self.run = Path(self.tmp.name)
        self.dispatches = []

    def tearDown(self):
        self.tmp.cleanup()

    def case(self, case_id="dev_001", *, layout="current", outer=False, salt="one", task=None):
        owner = self.run / "lifecycle" if outer else self.run
        base = controller.CASES["hidden" if case_id.startswith("test_") else "public_dev"][case_id]
        spec = controller.case_spec(case_id, base)
        if task is not None:
            spec["task_input"] = task
            spec["task_input_sha256"] = hashlib.sha256(task.encode()).hexdigest()
        if layout == "current":
            attempt = owner / "product_attempts/deliveries" / hashlib.sha256(salt.encode()).hexdigest()
            directory = attempt / "evaluations" / case_id
            spec_path = attempt / f"spec_{case_id}.json"
        else:
            directory = owner / "evaluations" / ("hidden" if case_id.startswith("test_") else "candidate_001") / case_id
            spec_path = owner / f"spec_{case_id}.json"
        lower_projection(spec, directory / "case-work")
        spec_path.write_text(json.dumps(spec))
        trajectory = directory / "trajectory.log"
        trajectory.write_text("Synthetic complete provenance control; no model execution.\n")
        trajectory_digest = finalizer.sha256(trajectory)
        for name in ("lower.stdout.log", "native_evidence.json", "oracle_comparison.json"):
            (directory / name).write_text("{}\n")
        artifact = directory / "agent_artifact.json"
        artifact.write_text(json.dumps({"schema_version": "agentswe-aider-agent-result/v1", "case_id": case_id,
                    "observations": [], "decision": {}, "integrity": {}, "safety": {}}))
        record = {"case_id": case_id, "result": str(directory / "result.json"),
                  "broker": {"successful_calls": 1}, "trajectory_digest": trajectory_digest,
                  "artifact_sha256": finalizer.sha256(artifact),
                  "artifact_contract": {"source": "lower_product_workspace", "evaluator_synthesized": False,
                      "copied_after_lower_exit": True, "candidate_mount_read_only": True,
                      "preexisting_before_launch": False, "trajectory_artifact_reference": True,
                      "trajectory_digest": trajectory_digest, "copy_path": str(artifact),
                      "exists": True, "valid_json": True, "case_id_matches": True,
                      "write_evidence": {"accepted": True}}}
        (directory / "result.json").write_text(json.dumps(record))
        return directory, spec_path, spec, record

    def capture(self, argv, **kwargs):
        self.dispatches.append(argv)
        task_path = Path(argv[argv.index("--task-input") + 1])
        self.assertEqual(task_path.name, "result_task_input.md")
        self.assertEqual(task_path.stat().st_mode & 0o222, 0)
        output = Path(argv[argv.index("--output-dir") + 1])
        output.mkdir(parents=True, exist_ok=True)
        (output / "result_score_contract.json").write_text(json.dumps({"control_only": True}))
        return subprocess.CompletedProcess(argv, 0, "controlled dispatch boundary", "")

    def judge(self, case_id, record):
        with patch.object(finalizer.subprocess, "run", side_effect=self.capture):
            return finalizer.run_result_judge(self.run, case_id, record, "http://unused.invalid/v1/responses")

    def test_actual_two_public_and_six_hidden_task_projections_reach_judge_boundary(self):
        for case_id in [*controller.CASES["public_dev"], *controller.CASES["hidden"]]:
            with self.subTest(case=case_id):
                hidden = case_id.startswith("test_")
                directory, spec_path, spec, record = self.case(case_id, layout="legacy" if hidden else "current", outer=True)
                result = self.judge(case_id, record)
                binding = result["task_input_binding"]
                self.assertEqual(binding["source"], str(directory / "case-work/task_input.md"))
                self.assertEqual(binding["spec_path"], str(spec_path))
                self.assertEqual(binding["task_input_sha256"], spec["task_input_sha256"])
                self.assertEqual(Path(binding["snapshot"]).read_bytes(), (directory / "case-work/task_input.md").read_bytes())
                self.assertEqual(binding["binding"], "lower_projection_verified_against_case_spec")
        self.assertEqual(len(self.dispatches), 8)

    def test_public_attempt_ignores_other_candidate_and_global_spec(self):
        directory, spec_path, spec, record = self.case(task="Actual case text without final newline")
        (self.run / "spec_dev_001.json").write_text(json.dumps({"case_id": "dev_001", "task_input": "wrong global Candidate"}))
        (directory / "task_input.md").write_text("wrong legacy Candidate\n")
        result = self.judge("dev_001", record)
        self.assertEqual(Path(result["task_input_binding"]["snapshot"]).read_text(), spec["task_input"] + "\n")
        self.assertEqual(result["task_input_binding"]["spec_path"], str(spec_path))

    def test_text_and_rewritten_sidecar_cannot_override_evaluator_spec(self):
        directory, _, _, record = self.case()
        task = directory / "case-work/task_input.md"
        task.write_text("changed by lower\n")
        task.with_suffix(".sha256").write_text(finalizer.sha256(task) + "\n")
        with self.assertRaisesRegex(ValueError, "projection differs"):
            self.judge("dev_001", record)
        self.assertFalse(self.dispatches)

    def test_invalid_sidecar_is_rejected(self):
        directory, _, _, record = self.case()
        (directory / "case-work/task_input.sha256").write_text("0" * 64)
        with self.assertRaisesRegex(ValueError, "sidecar differs"):
            self.judge("dev_001", record)
        self.assertFalse(self.dispatches)

    def test_missing_attempt_spec_never_uses_global_spec(self):
        _, spec_path, spec, record = self.case()
        (self.run / "spec_dev_001.json").write_text(json.dumps(spec))
        spec_path.unlink()
        with self.assertRaisesRegex(ValueError, "no bound evaluator spec"):
            self.judge("dev_001", record)
        self.assertFalse(self.dispatches)

    def test_missing_new_projection_never_uses_legacy_file_or_global_spec(self):
        directory, _, spec, record = self.case()
        (directory / "case-work/task_input.md").unlink()
        (directory / "task_input.md").write_text(spec["task_input"])
        with self.assertRaisesRegex(FileNotFoundError, "projection is missing"):
            self.judge("dev_001", record)
        self.assertFalse(self.dispatches)

    def test_wrong_case_and_spec_digest_are_rejected(self):
        _, spec_path, spec, record = self.case()
        for field, value, error in [("case_id", "dev_002", "case binding"), ("task_input_sha256", "0" * 64, "content digest")]:
            with self.subTest(field=field):
                spec_path.write_text(json.dumps({**spec, field: value}))
                with self.assertRaisesRegex(ValueError, error):
                    self.judge("dev_001", record)
        self.assertFalse(self.dispatches)

    def test_projection_symlink_to_another_case_is_rejected(self):
        directory, _, _, record = self.case()
        foreign = self.run / "foreign-task.md"
        foreign.write_bytes((directory / "case-work/task_input.md").read_bytes())
        path = directory / "case-work/task_input.md"
        path.unlink(); path.symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.judge("dev_001", record)
        self.assertFalse(self.dispatches)

    def test_legacy_hidden_spec_fallback_projects_task_text_for_judge(self):
        directory, spec_path, spec, record = self.case("test_001", layout="legacy", outer=True)
        shutil.rmtree(directory / "case-work")
        result = self.judge("test_001", record)
        binding = result["task_input_binding"]
        self.assertEqual(binding["source"], str(spec_path))
        self.assertEqual(binding["binding"], "legacy_case_spec_task_projection")
        self.assertEqual(Path(binding["snapshot"]).read_text(), spec["task_input"])

    def test_legacy_case_scoped_file_remains_supported(self):
        directory, _, spec, record = self.case(layout="legacy")
        task = directory / "case-work/task_input.md"
        (directory / "task_input.md").write_bytes(task.read_bytes())
        shutil.rmtree(directory / "case-work")
        result = self.judge("dev_001", record)
        self.assertEqual(result["task_input_binding"]["binding"], "legacy_case_scoped_task")

    def test_snapshot_is_exclusive_and_existing_matching_bytes_are_unchanged(self):
        directory, _, _, _ = self.case()
        snapshot, binding = finalizer.result_task_input(self.run, directory, "dev_001")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in [snapshot, directory / "result_task_input_binding.json"]}
        again, _ = finalizer.result_task_input(self.run, directory, "dev_001")
        self.assertEqual(snapshot, again)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in [snapshot, directory / "result_task_input_binding.json"]})
        snapshot.chmod(0o644); snapshot.write_text("existing unrelated snapshot\n"); snapshot.chmod(0o444)
        with self.assertRaisesRegex(ValueError, "existing Result task snapshot differs"):
            finalizer.result_task_input(self.run, directory, "dev_001")
        self.assertEqual(snapshot.read_text(), "existing unrelated snapshot\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
