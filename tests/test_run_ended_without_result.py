"""`agentswe result` for a Creation or Optimization run whose one_stop ended without its summary (stdlib unittest).

A Creation run can end without one_stop_summary.json: a web-research-report smoke
failed its hidden phase on a Harbor infrastructure exception and
its one_stop exited 1. `result` printed only "run has not finished" while `status` said alive false. It now reports
that the run ended without a result, the exit status the one_stop wrapper logged, the last infrastructure error the
run recorded and the last 20 lines of its one_stop log (the counterpart of Editing's orchestrator_log_tail), and the
command still exits 1 because there is no result. The fixtures follow that run's files.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import creation_harbor_v1 as creation  # noqa: E402
from agentswe.runners import optimization_native_v1 as opt  # noqa: E402

RUN_ID = "c-web-codex-deepseek-flash-s1-smoke-20260101t000000z"
HIDDEN_ERROR = (f"RuntimeError: {RUN_ID}-hidden-attempt-001 failed; see controller_logs/{RUN_ID}-hidden-attempt-001/"
                "stderr.log; stderr tail: adapter error: Create runtime infrastructure failure; score withheld: "
                '[{"error": "Harbor trial infrastructure exception", "exception_type": "RewardFileNotFoundError"}]')
LOG_END = [
    "Traceback (most recent call last):",
    '  File "runners/creation/runtime_contract.py", line 308, in assert_runtime_health',
    '    raise RuntimeError("Create runtime infrastructure failure; score withheld: " + json.dumps(failures))',
    "RuntimeError: Create runtime infrastructure failure; score withheld: [...]",
    "",
    "===== 2026-10-09T17:52:10Z one_stop exit 1; broker and key files removed",
]


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))


def exited_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class Cfg:
    home = Path("/nonexistent")


class CreationEndedWithoutResult(unittest.TestCase):
    family, run_id = "creation", RUN_ID

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / "runs" / self.family
        self.run_dir = self.root / self.run_id
        self.run_dir.mkdir(parents=True)
        self.log = self.root / f"{self.run_id}.one_stop.log"
        self.launch = {"run_id": self.run_id, "task": "web-research-report", "family": self.family, "mode": "smoke",
                       "comparable": False, "builder": {"profile": "codex"}, "host": socket.gethostname(),
                       "pid": exited_pid(), "run_dir": str(self.run_dir), "log": str(self.log),
                       "protocol": {"heldout_cases": ["test_001"]}}
        write(self.root / f"{self.run_id}.launch.json", self.launch)
        write(self.log, "\n".join([f"===== 2026-10-09T16:31:16Z one_stop.py --run-id {self.run_id}"]
                                  + [f"progress line {n}" for n in range(30)] + LOG_END) + "\n")
        write(self.run_dir / "freeze_manifest.json", {"digest": "2abe"})
        write(self.run_dir / "infrastructure_events.json", [{
            "submission_id": "dev-r001-a001-e5a77a15d6e0", "round": 1, "state": "infrastructure_error",
            "infrastructure_error": f"RuntimeError: {RUN_ID}-dev-r001-a001 produced missing or invalid Eval score contracts",
            "finished_at": "2026-10-09T17:00:41.295256+00:00"}])
        write(self.run_dir / "hidden_infrastructure_events.json", [{
            "phase": "hidden_eval", "phase_id": f"{RUN_ID}-hidden-attempt-001", "exception_class": "RuntimeError",
            "error": HIDDEN_ERROR, "attempt": 1, "state": "infrastructure_error",
            "recorded_at": "2026-10-09T17:51:43.527459+00:00"}])

    def result(self):
        return creation.result(Cfg(), self.launch)

    def test_dead_run_reports_exit_error_and_log_tail(self):
        res = self.result()
        self.assertIs(res["ended_without_result"], True)
        self.assertEqual((res["valid"], res["score"]), (False, None))
        self.assertEqual((res["exit_status"], res["exit_logged_at"]), (1, "2026-10-09T17:52:10Z"))
        error = res["last_infrastructure_error"]
        self.assertEqual((error["source"], error["phase"], error["state"], error["error"]),
                         ("hidden_infrastructure_events.json", "hidden_eval", "infrastructure_error", HIDDEN_ERROR))
        self.assertEqual(len(res["one_stop_log_tail"]), 20)
        self.assertEqual(res["one_stop_log_tail"][-len(LOG_END):], LOG_END)
        self.assertEqual(res["one_stop_log"], str(self.log))

    def test_dev_ledger_when_the_hidden_phase_never_failed(self):
        (self.run_dir / "hidden_infrastructure_events.json").unlink()
        error = self.result()["last_infrastructure_error"]
        self.assertEqual((error["source"], error["phase"], error["phase_id"], error["at"]),
                         ("infrastructure_events.json", "dev_evaluation", "dev-r001-a001-e5a77a15d6e0",
                          "2026-10-09T17:00:41.295256+00:00"))

    def test_hidden_evaluation_failure_without_replay_ledger(self):
        # repository-bug-repair has no hidden replay: its hidden evaluation records the failure only in
        # evaluations/<run>-hidden/runtime_infrastructure_failure.json, which must win over an older dev event.
        (self.run_dir / "hidden_infrastructure_events.json").unlink()
        write(self.run_dir / "evaluations" / f"{RUN_ID}-hidden" / "runtime_infrastructure_failure.json", [{
            "path": "jobs/x-hidden/test_006__abc/result.json", "error": "Harbor trial infrastructure exception",
            "exception_type": "RewardFileNotFoundError"}])
        error = self.result()["last_infrastructure_error"]
        self.assertEqual((error["source"], error["phase"], error["phase_id"], error["state"]),
                         (f"evaluations/{RUN_ID}-hidden/runtime_infrastructure_failure.json", "hidden_eval",
                          f"{RUN_ID}-hidden", "infrastructure_error"))
        self.assertEqual(error["error"], "Harbor trial infrastructure exception (RewardFileNotFoundError)")

    def test_hidden_judge_job_failure(self):
        # The held-out judge (Eval) job records its failure one level down, in eval-run/; it is newer than the
        # candidate job's own record and must be the one reported.
        (self.run_dir / "hidden_infrastructure_events.json").unlink()
        hidden = self.run_dir / "evaluations" / f"{RUN_ID}-hidden"
        write(hidden / "eval-run" / "runtime_infrastructure_failure.json", [{
            "path": "jobs/x-eval-hidden/test_002__abc/result.json", "error": "Harbor trial infrastructure exception",
            "exception_type": "RewardFileNotFoundError"}])
        error = self.result()["last_infrastructure_error"]
        self.assertEqual((error["source"], error["phase_id"]),
                         (f"evaluations/{RUN_ID}-hidden/eval-run/runtime_infrastructure_failure.json",
                          f"{RUN_ID}-hidden/eval-run"))

    def test_no_recorded_error_and_no_wrapper_line(self):
        # A killed process group never reaches the wrapper's exit line.
        for name in ("hidden_infrastructure_events.json", "infrastructure_events.json"):
            (self.run_dir / name).unlink()
        write(self.log, "one line\n")
        res = self.result()
        self.assertEqual((res["exit_status"], res["last_infrastructure_error"], res["one_stop_log_tail"]),
                         (None, None, ["one line"]))

    def test_live_run_has_not_finished(self):
        self.launch["pid"] = os.getpid()
        self.assertIsNone(self.result())

    def test_run_launched_on_another_host_is_not_judged_here(self):
        self.launch["host"] = "another-host.invalid"
        self.assertIsNone(self.result())

    def test_cli_prints_the_diagnostics_and_exits_one(self):
        from agentswe import cli
        saved = os.environ.get("AGENTSWE_HOME")
        os.environ["AGENTSWE_HOME"] = str(self.home)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = cli.main(["result", self.run_id])
        finally:
            if saved is None:
                os.environ.pop("AGENTSWE_HOME", None)
            else:
                os.environ["AGENTSWE_HOME"] = saved
        self.assertEqual(rc, 1)
        printed = json.loads(out.getvalue())
        self.assertIs(printed["ended_without_result"], True)
        self.assertEqual(printed["exit_status"], 1)


class OptimizationEndedWithoutResult(unittest.TestCase):
    RUN = "o-tau3-codex-deepseek-flash-s1-smoke-20260101t000000z"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "runs" / "optimization"
        self.run_dir = root / self.RUN
        log = root / f"{self.RUN}.one_stop.log"
        self.launch = {"run_id": self.RUN, "task": "tau3", "family": "optimization", "mode": "smoke",
                       "comparable": False, "builder": {"profile": "codex"}, "host": socket.gethostname(),
                       "pid": exited_pid(), "run_dir": str(self.run_dir), "log": str(log)}
        write(log, "RuntimeError: Candidate Harbor infrastructure failure\n"
                   "===== 2026-10-09T15:02:44Z one_stop exit 1; broker and key files removed\n")
        phase = self.run_dir / "controller_logs" / f"{self.RUN}-initial-test"
        write(phase / opt.RESUME_STATE, {"phase_id": f"{self.RUN}-initial-test", "status": "infrastructure_error",
                                         "resume_count": 1, "events": [{
                                             "attempt": 1, "status": "paused_infrastructure",
                                             "error_type": "RuntimeError", "at": "2026-10-09T15:01:00Z",
                                             "error_message": "Candidate Harbor infrastructure failure"}]})
        write(phase / "attempt_001" / "stderr.log", "RuntimeError: Candidate Harbor infrastructure failure\n")

    def test_dead_run_reports_exit_error_and_log_tail(self):
        res = opt.result(Cfg(), self.launch)
        self.assertIs(res["ended_without_result"], True)
        self.assertEqual(res["exit_status"], 1)
        error = res["last_infrastructure_error"]
        self.assertEqual((error["source"], error["phase"], error["type"], error["message"]),
                         ("controller_logs", "initial-test", "RuntimeError",
                          "RuntimeError: Candidate Harbor infrastructure failure"))
        self.assertEqual(res["one_stop_log_tail"][-1],
                         "===== 2026-10-09T15:02:44Z one_stop exit 1; broker and key files removed")

    def test_live_run_has_not_finished(self):
        self.launch["pid"] = os.getpid()
        self.assertIsNone(opt.result(Cfg(), self.launch))


if __name__ == "__main__":
    unittest.main()
