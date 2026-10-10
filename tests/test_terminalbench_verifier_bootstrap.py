"""A Terminal-Bench verifier that never reached its tests is an infrastructure failure, not a 0 (stdlib unittest).

34 of the 40 held-out and 5 of the 10 dev cases keep their upstream test.sh, which installs curl (apt), uv (astral's
installer, archive from github.com) and pytest (PyPI) when the verifier runs. When the container cannot reach these,
no test runs and reward.txt is 0. The controller records an infrastructure failure when the verifier's
test-stdout.txt shows no pytest output at all and a known bootstrap error;
any output where the tests ran, and any unknown output, keeps the official reward. The fixtures are excerpts of real
test-stdout.txt files. run_once_main runs here with Harbor's two
commands (`task migrate`, `run`) replaced by fakes that write what Harbor writes, so no container runs.

AGENTSWE_TERMINALBENCH_GITHUB_DOWNLOAD_BASE reaches the verifier as UV_INSTALLER_GITHUB_BASE_URL through the job's
`verifier.env`; the migrated task and its tests stay as they are.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config  # noqa: E402
from agentswe.runners.optimization_native_v1 import task_config_env  # noqa: E402

TASK = ROOT / "tasks" / "optimization" / "terminalbench" / "task.json"
BENCHMARK = ROOT / "tasks" / "optimization" / "terminalbench" / "benchmark"
NATIVE = BENCHMARK / "evaluator" / "native_tasks"
CONTROLLER = ROOT / "runners" / "optimization" / "optimization_native" / "terminalbench_controller.py"
SETTING = "AGENTSWE_TERMINALBENCH_GITHUB_DOWNLOAD_BASE"

APT = (
    "Get:1 http://archive.ubuntu.com/ubuntu noble InRelease [256 kB]\n"
    "Get:2 http://security.ubuntu.com/ubuntu noble-security InRelease [126 kB]\n"
    "Fetched 33.5 MB in 12s (2740 kB/s)\n"
    "Reading package lists...\n"
    "The following NEW packages will be installed:\n"
    "  curl krb5-locales libbrotli1 libcurl4t64 libgssapi-krb5-2 libk5crypto3\n"
    "0 upgraded, 19 newly installed, 0 to remove and 61 not upgraded.\n"
    "Setting up curl (8.5.0-2ubuntu10.15) ...\r\n"
    "Processing triggers for libc-bin (2.39-0ubuntu8.4) ...\r\n"
)
# regex-log, a0 and candidate of the 2026-10-09 smoke (agentswe-tb-regex-log-e27a13934d8e, -764ec07a8881)
DOWNLOAD_FAILED = APT + (
    "downloading uv 0.7.13 x86_64-unknown-linux-gnu\n"
    "curl: (56) Failure when receiving data from the peer\n"
    "failed to download https://github.com/astral-sh/uv/releases/download/0.7.13/uv-x86_64-unknown-linux-gnu.tar.gz\n"
    "this may be a standard network error, but it may also indicate\n"
    "that uv's release process is not working. When in doubt\n"
    "please feel free to open an issue!\n"
    "/tests/test.sh: line 10: /root/.local/bin/env: No such file or directory\n"
    "/tests/test.sh: line 18: uv: command not found\n"
    "/tests/test.sh: line 20: .tbench-testing/bin/activate: No such file or directory\n"
    "/tests/test.sh: line 21: uv: command not found\n"
    "/tests/test.sh: line 23: uv: command not found\n"
)
# regex-log, a0 of the 2026-10-02 smoke (agentswe-tb-regex-log-ebfebe0b21d3)
CONNECT_FAILED = APT + (
    "downloading uv 0.7.13 x86_64-unknown-linux-gnu\n"
    "curl: (28) Failed to connect to github.com port 443 after 130405 ms: Couldn't connect to server\n"
    "failed to download https://github.com/astral-sh/uv/releases/download/0.7.13/uv-x86_64-unknown-linux-gnu.tar.gz\n"
    "/tests/test.sh: line 10: /root/.local/bin/env: No such file or directory\n"
    "/tests/test.sh: line 18: uv: command not found\n"
)
# regex-log, candidate of the 2026-10-02 smoke (agentswe-tb-regex-log-f591724dda61): uv installed, tests passed
UV_BOOTSTRAP = APT + (
    "downloading uv 0.7.13 x86_64-unknown-linux-gnu\n"
    "no checksums to verify\n"
    "installing to /root/.local/bin\n"
    "  uv\n"
    "  uvx\n"
    "everything's installed!\n"
    "Using CPython 3.12.3 interpreter at: /usr/bin/python3\n"
    "Creating virtual environment at: .tbench-testing\n"
    "Resolved 5 packages in 2.68s\n"
    "Installed 5 packages in 51ms\n"
    " + pytest==8.4.1\n"
)
UV_PASSED = UV_BOOTSTRAP + (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.12.3, pytest-8.4.1, pluggy-1.6.0\n"
    "rootdir: /tests\n"
    "collected 1 item\n"
    "\n"
    "../tests/test_outputs.py .                                               [100%]\n"
    "\n"
    "==================================== PASSES ====================================\n"
    "=========================== short test summary info ============================\n"
    "PASSED ../tests/test_outputs.py::test_regex_matches_dates\n"
    "============================== 1 passed in 0.01s ===============================\n"
)
# cancel-async-tasks, a0 of the 2026-10-09 smoke (agentswe-tb-cancel-async-tasks-1d16a28d9316): tests ran, one failed
PYTEST_FAILED = (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.13.1, pytest-8.4.1, pluggy-1.6.0\n"
    "rootdir: /tests\n"
    "collected 6 items\n"
    "\n"
    "../tests/test_outputs.py .....F                                          [100%]\n"
    "\n"
    "=================================== FAILURES ===================================\n"
    "____________________ test_tasks_cancel_above_max_concurrent ____________________\n"
    ">       assert stdout.count(\"Cleaned up.\") == 2\n"
    "E       AssertionError: assert 0 == 2\n"
    "/tests/test_outputs.py:172: AssertionError\n"
    "=========================== short test summary info ============================\n"
    "FAILED ../tests/test_outputs.py::test_tasks_cancel_above_max_concurrent - Ass...\n"
    "========================= 1 failed, 5 passed in 12.76s =========================\n"
)
# cancel-async-tasks, candidate of the 2026-10-09 smoke (agentswe-tb-cancel-async-tasks-27641975adf9)
PYTEST_PASSED = (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.13.1, pytest-8.4.1, pluggy-1.6.0\n"
    "rootdir: /tests\n"
    "collected 6 items\n"
    "\n"
    "../tests/test_outputs.py ......                                          [100%]\n"
    "============================== 6 passed in 13.75s ==============================\n"
)
# regex-log of the 2026-10-09 smoke, cut while the download stalled (agentswe-tb-regex-log-d126c5d90902): no error
STALLED = APT + "downloading uv 0.7.13 x86_64-unknown-linux-gnu\n"


def controller(**env: str):
    """A fresh import of the controller with exactly these AGENTSWE_TERMINALBENCH_* settings."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_TERMINALBENCH_")}
    with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
        spec = importlib.util.spec_from_file_location(f"tb_controller_bootstrap_{len(env)}_{id(env)}", CONTROLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def payload_digest(tests: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(tests.rglob("*"), key=lambda p: p.relative_to(tests).as_posix()):
        if path.is_file() and path.name != "test.sh":
            h.update(b"F" + path.relative_to(tests).as_posix().encode() + path.read_bytes())
    return h.hexdigest()


class Evaluation:
    """run_once_main for one regex-log case, with Harbor's `task migrate` and `run` replaced by fakes."""

    def __init__(self, module, root: Path, test_stdout: str, reward: float, agent_metadata: dict | None = None):
        self.module, self.root = module, root
        self.test_stdout, self.reward, self.agent_metadata = test_stdout, reward, agent_metadata or {}
        self.native = root / "native" / "regex-log"
        self.native.mkdir(parents=True)
        for name in ("Dockerfile", "run-tests.sh", "task.yaml"):
            (self.native / name).write_bytes((NATIVE / "regex-log" / name).read_bytes())
        (self.native / "tests").mkdir()
        for path in (NATIVE / "regex-log" / "tests").iterdir():
            (self.native / "tests" / path.name).write_bytes(path.read_bytes())
        self.predictions = root / "predictions.jsonl"
        self.predictions.write_text(json.dumps({"id": "test_032", "commands": ["true"]}) + "\n")
        self.output, self.jobs = root / "out", root / "jobs"
        self.migrated_test_sh = ""

    def fake_run(self, cmd, *args, **kwargs):
        if cmd[1:3] == ["task", "migrate"]:
            task = Path(cmd[cmd.index("-o") + 1]) / self.native.name
            (task / "environment").mkdir(parents=True)
            (task / "tests").mkdir()
            (task / "task.toml").write_text('[verifier]\ntimeout_sec = 180.0\n\n[verifier.env]\n')
            (task / "instruction.md").write_text("instruction\n")
            (task / "environment" / "Dockerfile").write_bytes((self.native / "Dockerfile").read_bytes())
            self.migrated_test_sh = (self.native / "run-tests.sh").read_text().replace("$TEST_DIR", "/tests")
            (task / "tests" / "test.sh").write_text(self.migrated_test_sh)
            for path in (self.native / "tests").iterdir():
                (task / "tests" / path.name).write_bytes(path.read_bytes())
            return subprocess.CompletedProcess(cmd, 0, "migrated\n", None)
        if cmd[1] == "run":
            job = json.loads(Path(cmd[3]).read_text())
            job_dir = Path(job["jobs_dir"]) / job["job_name"]
            trial = job_dir / "regex-log__fixture"
            (trial / "verifier").mkdir(parents=True)
            (trial / "verifier" / "test-stdout.txt").write_text(self.test_stdout)
            (trial / "verifier" / "reward.txt").write_text(f"{int(self.reward)}\n")
            (trial / "result.json").write_text(json.dumps({
                "verifier_result": {"rewards": {"reward": self.reward}},
                "agent_result": {"metadata": self.agent_metadata}}))
            (job_dir / "result.json").write_text(json.dumps({"stats": {"evals": {"static__adhoc": {
                "n_trials": 1, "n_errors": 0, "metrics": [{"mean": self.reward}], "exception_stats": {}}}}}))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")

    def run(self) -> dict:
        argv = ["terminalbench_controller.py", "--case-id", "test_032", "--predictions", str(self.predictions),
                "--native-task", str(self.native), "--expected-task-digest", self.module.digest_tree(self.native),
                "--output-dir", str(self.output), "--jobs-dir", str(self.jobs)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.module.subprocess, "run", side_effect=self.fake_run):
            returncode = self.module.run_once_main()
        if returncode != 0:
            raise AssertionError(f"run_once_main returned {returncode}")
        return json.loads((self.output / "native_result.json").read_text())

    def harbor_job(self) -> dict:
        return json.loads((self.output / "harbor_job.json").read_text())


class VerifierBootstrapClassification(unittest.TestCase):
    def evaluate(self, test_stdout: str, reward: float, module=None, **kwargs) -> tuple[dict, Evaluation]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        evaluation = Evaluation(module or controller(), Path(tmp.name), test_stdout, reward, **kwargs)
        return evaluation.run(), evaluation

    def assertOfficial(self, result: dict, score: int) -> None:
        self.assertIs(result["official_evaluation"], True)
        self.assertIs(result["infrastructure_failure"], False)
        self.assertIs(result["validity_gate"], True)
        self.assertEqual(result["score"], score)

    def test_download_failure_is_infrastructure(self):
        module = controller()
        for name, stdout in (("download_failed", DOWNLOAD_FAILED), ("connect_failed", CONNECT_FAILED)):
            with self.subTest(name):
                result, _ = self.evaluate(stdout, 0.0, module)
                self.assertIs(result["infrastructure_failure"], True)
                self.assertIs(result["official_evaluation"], False)
                self.assertIs(result["validity_gate"], False)
                self.assertEqual(result["score"], 0)
                self.assertEqual(result["errors"],
                                 ["terminalbench_verifier_bootstrap_failed:download_failed,command_not_found"])
                self.assertTrue(result["verifier_bootstrap_failure"]["test_stdout"].endswith(
                    "verifier/test-stdout.txt"))
                # the controller's retry loop and the phase resume treat it like every other infrastructure failure
                self.assertFalse(module.controller_result_is_terminal(result))

    def test_failed_tests_stay_candidate_zero(self):
        for name, stdout in (("pinned wheelhouse", PYTEST_FAILED),
                             ("uv bootstrap", UV_BOOTSTRAP + PYTEST_FAILED),
                             # a test that itself prints download errors still ran
                             ("errors inside tests", PYTEST_FAILED.replace(
                                 "E       AssertionError", "curl: (6) Could not resolve host: github.com\n"
                                 "/app/run.sh: line 3: uv: command not found\nE       AssertionError"))):
            with self.subTest(name):
                result, _ = self.evaluate(stdout, 0.0)
                self.assertOfficial(result, 0)
                self.assertNotIn("verifier_bootstrap_failure", result)

    def test_passing_run_scores(self):
        module = controller()
        for name, stdout in (("pinned wheelhouse", PYTEST_PASSED), ("uv bootstrap", UV_PASSED)):
            with self.subTest(name):
                result, _ = self.evaluate(stdout, 1.0, module)
                self.assertOfficial(result, 100)

    def test_unknown_output_stays_candidate_zero(self):
        module = controller()
        for name, stdout in (("stalled download", STALLED), ("empty", ""),
                             ("conftest import error", UV_BOOTSTRAP + "ImportError while loading conftest "
                              "'/tests/conftest.py'.\nModuleNotFoundError: No module named 'numpy'\n")):
            with self.subTest(name):
                result, _ = self.evaluate(stdout, 0.0, module)
                self.assertOfficial(result, 0)

    def test_agent_timeout_decides_before_verifier(self):
        result, _ = self.evaluate(DOWNLOAD_FAILED, 0.0, agent_metadata={"candidate_timed_out": True})
        self.assertOfficial(result, 0)
        self.assertIs(result["candidate_timed_out"], True)

    def test_signatures(self):
        module = controller()
        cases = {
            "sh: 1: uvx: not found\n": ["command_not_found"],
            "bash: pytest: command not found\n": ["command_not_found"],
            "/usr/local/bin/python: No module named pytest\n": ["module_not_found"],
            "E: Unable to locate package curl\n": ["apt_failed"],
            "E: Failed to fetch http://archive.ubuntu.com/ubuntu/pool/main/c/curl/curl_8.5.0.deb  503\n": ["apt_failed"],
            "ERROR: Could not find a version that satisfies the requirement pytest==8.4.1 (from versions: none)\n"
            "ERROR: No matching distribution found for pytest==8.4.1\n": ["pip_failed"],
            "error: Failed to fetch: `https://pypi.org/simple/pytest/`\n  Caused by: Request failed after 3 retries\n":
                ["uv_pip_failed"],
        }
        for stdout, signatures in cases.items():
            with self.subTest(stdout):
                self.assertEqual(module.verifier_bootstrap_failure(APT + stdout)["signatures"], signatures)
        quiet = "F\nuv: command not found\n1 failed in 0.12s\n"  # pytest -q prints no "===" line here
        for stdout in (PYTEST_FAILED, PYTEST_PASSED, UV_PASSED, STALLED, "", quiet,
                       "W: Some index files failed to download\n"):
            self.assertIsNone(module.verifier_bootstrap_failure(stdout))


class GithubDownloadMirror(unittest.TestCase):
    def evaluate(self, **env: str) -> tuple[dict, Evaluation]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        evaluation = Evaluation(controller(**env), Path(tmp.name), UV_PASSED, 1.0)
        return evaluation.run(), evaluation

    def test_reaches_verifier_env(self):
        result, evaluation = self.evaluate(**{SETTING: "http://mirror.example/github/"})
        env = {"UV_INSTALLER_GITHUB_BASE_URL": "http://mirror.example/github"}
        self.assertEqual(evaluation.harbor_job()["verifier"], {"env": env})
        self.assertEqual(result["verifier_env"], env)
        self.assertEqual(result["score"], 100)

    def test_task_and_payload_unchanged(self):
        result, evaluation = self.evaluate(**{SETTING: "http://mirror.example/github"})
        task = evaluation.output / "migrated" / "regex-log"
        self.assertEqual((task / "tests" / "test.sh").read_text(), evaluation.migrated_test_sh)
        self.assertIn("curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh", evaluation.migrated_test_sh)
        self.assertEqual(result["official_test_payload_digest"], payload_digest(evaluation.native / "tests"))
        self.assertNotIn("UV_INSTALLER", (task / "task.toml").read_text())

    def test_unset_leaves_job_config(self):
        result, evaluation = self.evaluate()
        self.assertNotIn("verifier", evaluation.harbor_job())
        self.assertNotIn("verifier_env", result)

    def test_declared_documented_and_exported(self):
        self.assertIn(f'os.environ.get("{SETTING}"', CONTROLLER.read_text())
        self.assertIn(SETTING, json.loads(TASK.read_text())["runner_config"]["config_env"])
        self.assertIn(SETTING, (ROOT / "docs" / "ENV.md").read_text())
        self.assertIn(SETTING, (ROOT / ".env.example").read_text())
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("AGENTSWE_")}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(f"{SETTING}=http://mirror.example/github\n")
                got = task_config_env(config.load(env_file), json.loads(TASK.read_text())["runner_config"])
        finally:
            os.environ.update(saved)
        self.assertEqual(got[SETTING], "http://mirror.example/github")


class DocumentedCounts(unittest.TestCase):
    def test_cases_installing_uv(self):
        """The counts the docs give: cases whose kept upstream verifier installs uv with astral's installer."""
        module = controller()
        kept = module.STABILIZED_TEST_TASKS | module.QEMU_TASKS
        splits = json.loads((BENCHMARK / "task_contract.json").read_text())["splits"]
        counts = {}
        for split, value in splits.items():
            ids = [case["upstream_id"] for case in value["cases"]]
            counts[split] = (len(ids), sum(
                1 for name in ids if name not in kept and re.search(
                    r"curl -LsSf https://astral\.sh/uv/(?:[\d.]+/)?install\.sh \| sh",
                    (NATIVE / name / "run-tests.sh").read_text())))
        self.assertEqual(counts, {"hidden": (40, 34), "dev": (10, 5)})
        docs = (ROOT / "docs" / "ENV.md").read_text()
        self.assertEqual(len(re.findall(r"34 of the 40 held-out\s+and 5 of the 10 dev", docs)), 2)


if __name__ == "__main__":
    unittest.main()
