"""Provider-free real Docker/Git controls for the materializer runtime."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

FILE = Path(__file__).with_name("materialize_candidate.py")
spec = importlib.util.spec_from_file_location("materializer", FILE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
VALID = '''raise RuntimeError("Candidate must never be imported")
try:
    pass
except* ValueError:
    pass
VALUE = 1
'''


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], env=m.CLEAN_ENV,
                          check=True, text=True, capture_output=True, timeout=15).stdout


def fixture(root, *, baseline=VALID, candidate=None):
    source, delivery = root / "source", root / "delivery"
    (source / "aider").mkdir(parents=True)
    (source / "tests").mkdir()
    delivery.mkdir()
    entry = source / "aider/worktree_plan_adapter.py"
    test = source / "tests/test_adapter.py"
    entry.write_text(baseline)
    test.write_text("TEST = 1\n")
    git(source, "init", "-q")
    git(source, "add", ".")
    git(source, "-c", "user.name=Syntax control", "-c", "user.email=syntax@invalid", "commit", "-qm", "baseline")
    entry.write_text(candidate if candidate is not None else VALID.replace("VALUE = 1", "VALUE = 2"))
    test.write_text("TEST = 2\n")
    patch = git(source, "diff", "--binary")
    git(source, "checkout", "--", ".")
    (delivery / "solution.patch").write_text(patch)
    (delivery / "edit_report.json").write_text(json.dumps({"schema_version": 1, "changed_paths": ["aider/worktree_plan_adapter.py", "tests/test_adapter.py"], "summary": "control", "tests": []}))
    (delivery / "run_report.json").write_text(json.dumps({"schema_version": 1, "status": "control", "commands": [], "duration_seconds": 0, "errors": [], **dict.fromkeys(("deepseek", "gateway", "gateway_image", "serper", "web_retrieval"), 0)}))
    return source, delivery


class ActualMaterializerRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aider-syntax-control-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, baseline=VALID, candidate=None):
        source, delivery = fixture(self.root, baseline=baseline, candidate=candidate)
        before = m.digest(source)
        output = self.root / "materialized"
        done = subprocess.run([sys.executable, "-I", "-B", str(FILE), "--source", str(source),
                    "--candidate", str(delivery), "--patch", str(delivery / "solution.patch"), "--output", str(output)],
                    env=m.CLEAN_ENV, text=True, capture_output=True, timeout=100)
        self.assertTrue((output / "build_result.json").is_file(), done.stderr[-1500:])
        result = json.loads((output / "build_result.json").read_text())
        self.assertEqual(before, m.digest(source))
        self.assertTrue(result["syntax_cleanup"]["complete"], result)
        attestation = json.loads(Path(result["syntax_resource_attestation"]).read_text())
        self.assertTrue(attestation["valid"], attestation)
        self.assertEqual(attestation["memory_bytes"], 4 * 1024 ** 3)
        self.assertLessEqual(attestation["timeout_seconds"], 600)
        self.assertTrue(attestation["cleanup"]["complete"])
        self.assertTrue(attestation["aggregate_cleanup"]["complete"])
        for record in result["syntax_cleanup"]["containers"]:
            absent = subprocess.run(["docker", "inspect", record["name"]], env=m.CLEAN_ENV, capture_output=True, timeout=5)
            self.assertNotEqual(absent.returncode, 0)
        self.assertFalse(list(output.rglob("*.pyc")))
        return done, result

    def test_real_git_python311_without_import_and_system_path(self):
        done, result = self.invoke()
        self.assertEqual(done.returncode, 0, result)
        self.assertEqual(result["classification"], "ready_for_lower")
        self.assertTrue(result["environment_preflight"]["valid"])
        observed = result["environment_preflight"]["baseline_compile"]
        self.assertEqual(observed["observed"]["python_version"][:2], [3, 11])
        self.assertEqual(observed["resource_contract"]["cpus"], 4)
        self.assertEqual(observed["resource_contract"]["network"], "none")
        self.assertEqual(observed["observed"]["kernel_resources"], {"memory.max": "4294967296", "memory.swap.max": "0", "cpu.max": "400000 100000"})
        self.assertEqual(observed["image_id"], result["checks"][-1]["image_id"])

    def test_real_candidate_syntax_error_is_candidate_failure(self):
        done, result = self.invoke(candidate="def broken(:\n")
        self.assertEqual(done.returncode, 2, result)
        self.assertEqual(result["classification"], "candidate_build_failure")
        self.assertTrue(result["environment_preflight"]["valid"])
        self.assertEqual(result["checks"][-1]["observed"]["error_type"], "SyntaxError")

    def test_real_candidate_indentation_error_is_candidate_failure(self):
        done, result = self.invoke(candidate="def broken():\npass\n")
        self.assertEqual(done.returncode, 2, result)
        self.assertEqual(result["classification"], "candidate_build_failure")
        self.assertTrue(result["environment_preflight"]["valid"])
        self.assertEqual(result["checks"][-1]["observed"]["error_type"], "IndentationError")

    def test_real_unhealthy_baseline_is_infrastructure(self):
        done, result = self.invoke(baseline="def broken(:\n", candidate=VALID)
        self.assertEqual(done.returncode, 3, result)
        self.assertEqual(result["classification"], "evaluator_infrastructure_failure")
        self.assertFalse(result["environment_preflight"]["valid"])
        self.assertNotIn("checks", result)

    def test_existing_output_preserved(self):
        source, delivery = fixture(self.root)
        output = self.root / "materialized"
        output.mkdir()
        marker = output / "build_result.json"
        marker.write_bytes(b"old evidence\n")
        done = subprocess.run([sys.executable, "-I", "-B", str(FILE), "--source", str(source),
                    "--candidate", str(delivery), "--patch", str(delivery / "solution.patch"), "--output", str(output)],
                    env=m.CLEAN_ENV, capture_output=True, timeout=10)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(marker.read_bytes(), b"old evidence\n")

    def test_real_running_container_timeout_owned_cleanup_and_inherited_deadline(self):
        source, _ = fixture(self.root)
        evidence = self.root / "timeout"
        evidence.mkdir()
        done, attestation = m.resources_module().run_owned(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--timeout-control", str(source), str(evidence)],
            cwd="/", env=m.CLEAN_ENV, output=self.root / "scope", timeout=25)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads((evidence / "control.json").read_text())
        self.assertEqual(result["exception"], "TimeoutExpired")
        self.assertTrue(result["cleanup"]["complete"])
        self.assertEqual(len(result["cleanup"]["containers"]), 1)
        self.assertLess(result["inherited_remaining"], 25)
        self.assertGreater(result["inherited_remaining"], 0)
        self.assertTrue(attestation["cleanup"]["complete"])
        self.assertTrue(attestation["aggregate_cleanup"]["complete"])

    def test_oom_and_non_syntax_failure_never_misclassified_as_candidate(self):
        outcome = {"valid": False, "image_id": "fixed", "container_state": {"OOMKilled": True},
                   "observed": {"error_type": "SyntaxError"}}
        self.assertEqual(m.syntax_classification(outcome, "fixed"), "evaluator_infrastructure_failure")
        outcome["container_state"]["OOMKilled"] = False
        for error in ("MemoryError", "MissingCompileEvidence", "PermissionError"):
            outcome["observed"]["error_type"] = error
            self.assertEqual(m.syntax_classification(outcome, "fixed"), "evaluator_infrastructure_failure")
        outcome["observed"]["error_type"] = "SyntaxError"
        self.assertEqual(m.syntax_classification(outcome, "changed"), "evaluator_infrastructure_failure")


def timeout_control():
    source, evidence = map(Path, sys.argv[2:])
    inherited = m.envelope_timeout()
    m.COMPILE_PROGRAM = "import time; time.sleep(300)"
    exception = None
    try:
        m.syntax_check(source, evidence, "timeout", time.monotonic() + 5)
    except Exception as exc:
        exception = type(exc).__name__
    finally:
        cleanup = m.cleanup_containers(evidence)
    (evidence / "control.json").write_text(json.dumps({"exception": exception, "cleanup": cleanup, "inherited_remaining": inherited}))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--timeout-control"]:
        raise SystemExit(timeout_control())
    unittest.main()
