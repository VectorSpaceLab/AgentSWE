"""`agentswe status` for an Optimization run shows its infrastructure resumes (stdlib unittest, no docker).

With --infrastructure-resume (always passed) one_stop resumes an evaluation phase's infrastructure failures without
limit, as in the paper. The fixtures follow the public tau3 smoke of 2026-10-09 whose held-out evaluation hit
EnvironmentStartTimeoutError six times: controller_logs/<run_id>-initial-test/infrastructure_resume_state.json with
paused_infrastructure events and attempt_NNN/stderr.log, next to a baseline and a dev phase that passed."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import optimization_native_v1 as opt  # noqa: E402

RUN_ID = "o-tau3-codex-deepseek-flash-s1-smoke-20260101t000000z"
STDERR = """Traceback (most recent call last):
  File "runners/optimization/adapter.py", line 1441, in collect_candidate
    raise RuntimeError(f"Candidate Harbor infrastructure failure: {failures}")
RuntimeError: Candidate Harbor infrastructure failure: [{'case_id': 'test_001', 'trial': 'test_001__78J7U44', \
'exception': 'Environment start timed out after 300.0 seconds'}, {'case_id': 'test_002', 'trial': 'test_002__vUBAFc7', \
'exception': 'Environment start timed out after 300.0 seconds'}]
"""


def write(path: Path, value, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class Cfg:
    home = Path("/nonexistent")


class OptimizationInfrastructureStatus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "runs" / "optimization" / RUN_ID
        self.logs = self.run_dir / "controller_logs"
        self.launch = {"run_id": RUN_ID, "pid": 0, "run_dir": str(self.run_dir), "task": "tau3", "family":
                       "optimization", "mode": "smoke", "comparable": False, "builder": {"profile": "codex"},
                       "log": str(self.run_dir.parent / f"{RUN_ID}.one_stop.log")}
        self.write_phase("baseline", "completed_without_resume", 0, [], mtime=1000)
        self.write_phase("dev-r001-a001", "completed_without_resume", 0, [], mtime=2000)
        write(self.run_dir / "dev_lifecycle.json", [{"round": 1, "state": "completed", "dev_mean": 100.0}])
        write(self.run_dir / "infrastructure_events.json", [])
        write(self.run_dir / "freeze_manifest.json", {"digest": "6a20"})

    def write_phase(self, phase, status, resume_count, events, mtime, last_wait=None):
        phase_id = f"{RUN_ID}-{phase}"
        state = {"candidate_digest": "a56b", "events": events, "phase_id": phase_id, "resume_count": resume_count,
                 "schema_version": "1.0", "status": status}
        if last_wait is not None:
            state["last_wait_sec"] = last_wait
        write(self.logs / phase_id / "infrastructure_resume_state.json", state, mtime)

    def held_out_stuck(self, attempts=6):
        phase_id = f"{RUN_ID}-initial-test"
        events = []
        for n in range(1, attempts + 1):
            attempt_phase = phase_id if n == 1 else f"{phase_id}-infra-a{n:03d}"
            stderr = self.logs / phase_id / f"attempt_{n:03d}" / "stderr.log"
            write(stderr, STDERR)
            events.append({"at": f"2026-10-09T13:{20 + n:02d}:00+00:00", "attempt": n,
                           "error_message": f"{attempt_phase} failed; see /old/host/path/{stderr.name}",
                           "error_type": "HarborProcessError", "phase_id": attempt_phase,
                           "status": "paused_infrastructure"})
        self.write_phase("initial-test", "paused_infrastructure", attempts, events, mtime=3000, last_wait=180.0)

    def status(self):
        with mock.patch.object(opt.util, "pid_alive", return_value=True):
            return opt.status(Cfg(), self.launch)

    def test_a_held_out_phase_stuck_on_infrastructure_shows_count_last_error_and_logs(self):
        self.held_out_stuck()
        st = self.status()
        self.assertEqual(st["phase"], "held-out")  # existing keys unchanged
        infra = st["infrastructure"]
        self.assertEqual(infra["controller_logs"], str(self.logs))
        current = infra["current_phase"]
        self.assertEqual((current["phase"], current["status"], current["resume_count"], current["last_wait_sec"]),
                         ("initial-test", "paused_infrastructure", 6, 180.0))
        self.assertEqual(current["controller_logs"], str(self.logs / f"{RUN_ID}-initial-test"))
        error = current["last_error"]
        self.assertEqual((error["type"], error["attempt"]), ("HarborProcessError", 6))
        self.assertEqual(error["exceptions"], ["Environment start timed out after 300.0 seconds"])
        self.assertTrue(error["message"].startswith("RuntimeError: Candidate Harbor infrastructure failure"))
        self.assertLessEqual(len(error["message"]), opt.SHORT_MESSAGE)
        self.assertEqual(error["stderr"], str(self.logs / f"{RUN_ID}-initial-test" / "attempt_006" / "stderr.log"))
        self.assertNotIn("dev_evaluation_resumes", infra)

    def test_a_clean_run_reports_its_latest_phase_without_errors(self):
        current = self.status()["infrastructure"]["current_phase"]
        self.assertEqual((current["phase"], current["resume_count"], current["last_error"]),
                         ("dev-r001-a001", 0, None))

    def test_dev_controller_resumes_are_reported_while_the_builder_works(self):
        write(self.run_dir / "infrastructure_events.json", [
            {"submission_id": "s1", "round": 2, "state": "paused_infrastructure", "resume_attempt": 3,
             "error_type": "InfrastructureEvaluationError", "error_message": "x" * 1000, "at": "t"}])
        dev = self.status()["infrastructure"]["dev_evaluation_resumes"]
        self.assertEqual((dev["events"], dev["round"], dev["resume_attempt"], dev["last_error"]["type"]),
                         (1, 2, 3, "InfrastructureEvaluationError"))
        self.assertLessEqual(len(dev["last_error"]["message"]), opt.SHORT_MESSAGE)

    def test_a_case_level_infrastructure_event_names_its_cases(self):
        self.write_phase("candidate-test", "resuming", 1, [
            {"attempt": 1, "phase_id": f"{RUN_ID}-candidate-test", "status": "paused_infrastructure",
             "infrastructure_cases": ["test_002"], "invalid_cases": ["test_002"], "at": "t"}], mtime=4000)
        error = self.status()["infrastructure"]["current_phase"]["last_error"]
        self.assertEqual((error["type"], error["message"]), ("paused_infrastructure", "infrastructure cases: test_002"))

    def test_a_run_without_controller_logs_has_no_infrastructure_block(self):
        import shutil
        shutil.rmtree(self.logs)
        (self.run_dir / "infrastructure_events.json").unlink()
        self.assertNotIn("infrastructure", self.status())

    def test_result_lists_each_phase_resume_count(self):
        self.held_out_stuck(attempts=2)
        self.write_phase("initial-test", "completed_after_resume", 2, [], mtime=3000)
        self.write_phase("candidate-test", "completed_without_resume", 0, [], mtime=4000)
        write(self.run_dir / "one_stop_summary.json", {"status": "completed", "hidden_mean": 60.0,
                                                        "initial_test_mean": 50.0})
        res = opt.result(Cfg(), self.launch)
        self.assertEqual(res["infrastructure_resumes"], {"baseline": 0, "dev-r001-a001": 0, "initial-test": 2,
                                                          "candidate-test": 0})
        self.assertEqual(res["score"], 20.0)

    # A dev evaluation that ended without a resume (a public tau3 run, 2026-10-10): the dev controller's record has
    # infrastructure_error ("Type: message") and finished_at, not the error_type / at / error_message of a pause.
    def strict_dev_failure(self):
        phase_id = f"{RUN_ID}-dev-r001-a001"
        stderr = self.logs / phase_id / "attempt_001" / "stderr.log"
        write(stderr, "Traceback (most recent call last):\n  ...\nFileNotFoundError: [Errno 2] No such file or "
                      "directory: '/h/jobs/e/dev_011__TJJ6fdS/verifier/score_contract.json'\n")
        write(self.run_dir / "infrastructure_events.json", [
            {"round": 1, "evaluation_attempt": 1, "submission_id": "dev-r001-a001-69ebed6376cb",
             "state": "infrastructure_error", "finished_at": "2026-10-10T10:44:37+00:00", "accepted_round": False,
             "infrastructure_error": f"RuntimeError: {phase_id} failed; see {stderr}"}])
        return stderr

    def test_a_dev_evaluation_that_ended_without_resume_shows_its_error(self):
        self.strict_dev_failure()
        dev = self.status()["infrastructure"]["dev_evaluation_resumes"]
        self.assertEqual((dev["events"], dev["round"], dev["state"], dev["submission_id"]),
                         (1, 1, "infrastructure_error", "dev-r001-a001-69ebed6376cb"))
        error = dev["last_error"]
        self.assertEqual((error["type"], error["at"]), ("RuntimeError", "2026-10-10T10:44:37+00:00"))
        self.assertTrue(error["message"].startswith(f"RuntimeError: {RUN_ID}-dev-r001-a001 failed; see "))
        self.assertTrue(error["detail"].startswith("FileNotFoundError: [Errno 2] No such file or directory"))
        last = opt.last_infrastructure_error(self.run_dir, RUN_ID)  # dev phases passed: falls back to the dev record
        self.assertEqual((last["source"], last["type"]), ("infrastructure_events.json", "RuntimeError"))

    def test_the_phase_is_baseline_until_the_starter_has_been_evaluated(self):
        (self.run_dir / "freeze_manifest.json").unlink()
        self.assertEqual(self.status()["phase"], "baseline")
        write(self.run_dir / "baseline.json", {"score": 45.0})
        self.assertEqual(self.status()["phase"], "builder/dev")
        with mock.patch.object(opt.util, "pid_alive", return_value=False):
            self.assertEqual(opt.status(Cfg(), self.launch)["phase"], "stopped")

    def test_result_lists_each_phase_final_status(self):
        self.write_phase("dev-r001-a001", "infrastructure_error", 1, [
            {"attempt": 1, "phase_id": f"{RUN_ID}-dev-r001-a001", "status": "paused_infrastructure",
             "error_type": "HarborProcessError", "error_message": "x", "at": "t"}], mtime=2000)
        self.write_phase("dev-r001-a002", "completed_without_resume", 0, [], mtime=2500)
        self.write_phase("initial-test", "completed_without_resume", 0, [], mtime=3000)
        self.write_phase("candidate-test", "completed_without_resume", 0, [], mtime=4000)
        write(self.run_dir / "one_stop_summary.json", {"status": "completed", "hidden_mean": 42.5,
                                                        "initial_test_mean": 50.0})
        res = opt.result(Cfg(), self.launch)
        self.assertEqual(res["evaluation_phase_status"], {
            "baseline": "completed_without_resume", "dev-r001-a001": "infrastructure_error",
            "dev-r001-a002": "completed_without_resume", "initial-test": "completed_without_resume",
            "candidate-test": "completed_without_resume"})
        self.assertEqual(res["infrastructure_resumes"]["dev-r001-a001"], 1)
        self.assertEqual(res["score"], -15.0)


if __name__ == "__main__":
    unittest.main()
