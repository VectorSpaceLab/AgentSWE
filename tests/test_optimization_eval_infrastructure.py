"""An Optimization Eval trial that Harbor ends with an exception before the evaluator writes its score contract is an
infrastructure failure, so the infrastructure resume replays the evaluation (stdlib unittest, Harbor and the adapter
process faked, no docker).

The fixture follows a public tau3 run whose first dev evaluation hit, on a loaded Docker host, a
VerifierTimeoutError (the verifier's compose build, 120 s) in one Eval trial and an EnvironmentStartTimeoutError
(300 s) in another: both trials have no verifier/score_contract.json. collect_eval read the contract unconditionally
and raised FileNotFoundError, which is_infrastructure_error does not match, so the dev controller dropped the
submission instead of replaying it, and the same failure in a held-out phase would have ended the run."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
OPT = ROOT / "runners" / "optimization"
RUN_ID = "o-tau3-codex-deepseek-flash-s1-formal-20260101t000000z"
VERIFIER_TIMEOUT = ("VerifierTimeoutError", "Verifier execution timed out after 120.0 seconds")
START_TIMEOUT = ("EnvironmentStartTimeoutError", "Environment start timed out after 300.0 seconds")


def load_modules():
    """The Optimization adapter and one_stop, one_stop bound to this adapter whatever else is in sys.modules."""
    with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"AGENTSWE_HOME": home}):
        spec = importlib.util.spec_from_file_location("ox_optimization_adapter", OPT / "adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        saved = sys.modules.get("adapter")
        sys.modules["adapter"] = adapter
        try:
            spec = importlib.util.spec_from_file_location("ox_optimization_one_stop", OPT / "one_stop.py")
            one_stop = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(one_stop)
        finally:
            if saved is None:
                sys.modules.pop("adapter", None)
            else:
                sys.modules["adapter"] = saved
    return adapter, one_stop


adapter, one_stop = load_modules()


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))


def contract(score: int) -> dict:
    return {"score": score, "contract_valid": True, "validity_gate": True, "official_evaluation": True,
            "infrastructure_failure": False, "deterministic_infrastructure_failure": False,
            "native_task_available": True}


def make_eval_job(job: Path, *, scored: dict[str, int], errored: dict[str, tuple[str, str]],
                  contract_despite_exception: dict[str, int] | None = None, in_trial_result: bool = True) -> None:
    """A Harbor Eval job: `scored` trials have a contract; `errored` trials ended with a Harbor exception and have
    none; `contract_despite_exception` trials have both (Harbor recorded an exception after the verifier wrote it)."""
    contract_despite_exception = contract_despite_exception or {}
    exceptions: dict[str, list[str]] = {}
    for case_id, score in {**scored, **contract_despite_exception}.items():
        trial = job / f"{case_id}__Abc1234"
        write(trial / "verifier" / "score_contract.json", contract(score))
    for case_id, (kind, message) in errored.items():
        trial = job / f"{case_id}__Err1234"
        (trial / "verifier").mkdir(parents=True, exist_ok=True)
        exceptions.setdefault(kind, []).append(trial.name)
    for trial in sorted(p for p in job.iterdir() if p.is_dir()):
        case_id = trial.name.split("__")[0]
        failure = errored.get(case_id) or (("RuntimeError", "compose down failed")
                                           if case_id in contract_despite_exception else None)
        info = ({"exception_type": failure[0], "exception_message": failure[1], "exception_traceback": "..."}
                if failure and in_trial_result else None)
        write(trial / "result.json", {"trial_name": trial.name, "exception_info": info,
                                      "verifier_result": None if case_id in errored else {"rewards": {"reward": 0.0}}})
    n_errored = len(errored) + len(contract_despite_exception)
    write(job / "result.json", {"n_total_trials": len(scored) + n_errored, "stats": {
        "n_completed_trials": len(scored) + n_errored, "n_errored_trials": n_errored,
        "evals": {"oracle__adhoc": {"n_trials": len(scored), "n_errors": n_errored, "exception_stats": exceptions}}}})


class CollectEval(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job = Path(tmp.name) / "jobs" / f"optimization-eval-tau3-tool-agent-optimization-v1-{RUN_ID}-dev-r001-a001"

    def test_errored_trials_without_a_contract_are_an_infrastructure_failure(self):
        make_eval_job(self.job, scored={"dev_001": 100, "dev_002": 0},
                      errored={"dev_011": VERIFIER_TIMEOUT, "dev_012": START_TIMEOUT})
        with self.assertRaises(adapter.InfrastructureEvaluationError) as caught:
            adapter.collect_eval(self.job, ("dev_001", "dev_002", "dev_011", "dev_012"))
        message = str(caught.exception)
        self.assertTrue(message.startswith("Eval Harbor infrastructure failure: "))
        self.assertIn("'case_id': 'dev_011', 'trial': 'dev_011__Err1234', "
                      "'exception': 'VerifierTimeoutError: Verifier execution timed out after 120.0 seconds'", message)
        self.assertIn("'exception': 'EnvironmentStartTimeoutError: Environment start timed out after 300.0 seconds'",
                      message)
        self.assertNotIn("dev_001", message)
        self.assertTrue(adapter.is_infrastructure_error(caught.exception))
        # one_stop sees only the adapter process's stderr: the traceback text must classify the same way
        text = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                  caught.exception.__traceback__))
        self.assertTrue(adapter.is_infrastructure_error(text))

    def test_a_trial_named_only_in_the_job_exception_stats_counts_as_errored(self):
        make_eval_job(self.job, scored={"dev_001": 100}, errored={"dev_012": START_TIMEOUT}, in_trial_result=False)
        with self.assertRaises(adapter.InfrastructureEvaluationError) as caught:
            adapter.collect_eval(self.job, ("dev_001", "dev_012"))
        self.assertIn("'exception': 'EnvironmentStartTimeoutError'", str(caught.exception))

    def test_completed_trials_are_scored_from_their_contracts_as_before(self):
        # an exception Harbor recorded after the verifier wrote the contract does not change the score
        make_eval_job(self.job, scored={"dev_001": 100, "dev_002": 0}, errored={},
                      contract_despite_exception={"dev_003": 100})
        summary = adapter.collect_eval(self.job, ("dev_001", "dev_002", "dev_003"))
        self.assertEqual([(c["case_id"], c["score"], c["contract_valid"], c["infrastructure_failure"])
                          for c in summary["cases"]],
                         [("dev_001", 100, True, False), ("dev_002", 0, True, False), ("dev_003", 100, True, False)])
        self.assertEqual((summary["total_score"], summary["mean_score"]), (200, 200 / 3))
        self.assertEqual(summary["harbor_stats"]["n_errored_trials"], 1)
        self.assertEqual(summary["cases"][0]["contract"],
                         str(self.job / "dev_001__Abc1234" / "verifier" / "score_contract.json"))

    def test_a_missing_contract_without_a_harbor_exception_still_fails_closed(self):
        make_eval_job(self.job, scored={"dev_001": 100}, errored={})
        (self.job / "dev_001__Abc1234" / "verifier" / "score_contract.json").unlink()
        with self.assertRaises(FileNotFoundError) as caught:
            adapter.collect_eval(self.job, ("dev_001",))
        self.assertFalse(adapter.is_infrastructure_error(caught.exception))


class DevEvaluationReplay(unittest.TestCase):
    """The dev controller replays an evaluation whose Eval job has errored trials (fake adapter process)."""

    CASES = ("dev_001", "dev_011")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.run_dir = self.tmp / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        self.candidate = self.run_dir / "candidates" / "round_001_attempt_001"
        write(self.candidate / "run_harness.py", "print('candidate')\n")
        self.job = self.tmp / "jobs" / "eval-a001"
        make_eval_job(self.job, scored={"dev_001": 100}, errored={"dev_011": VERIFIER_TIMEOUT})
        self.commands: list[str] = []
        env = {"OPTIMIZATION_INFRA_RESUME": "1", "AGENTSWE_EVAL_RESUME_MAX_ATTEMPTS": "3",
               "AGENTSWE_EVAL_RESUME_MIN_WAIT_SEC": "0", "AGENTSWE_EVAL_RESUME_MAX_WAIT_SEC": "0"}
        for patch in (mock.patch.dict(os.environ, env), mock.patch.object(one_stop.time, "sleep"),
                      mock.patch.object(one_stop.subprocess, "run", side_effect=self.fake_adapter_process)):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_adapter_process(self, command, **kw):
        """First attempt: the adapter's collect_eval fails on the errored Eval job (the real function, its traceback
        on stderr); any later attempt scores both cases."""
        phase_id = command[command.index("--run-id") + 1]
        self.commands.append(phase_id)
        if len(self.commands) == 1:
            try:
                adapter.collect_eval(self.job, self.CASES)
            except Exception:
                kw["stderr"].write(traceback.format_exc())
                return subprocess.CompletedProcess(command, 1)
            raise AssertionError("collect_eval scored an Eval job with an errored trial")
        cases = [{"case_id": c, "score": 100, **contract(100)} for c in self.CASES]
        write(Path(command[command.index("--runs-dir") + 1]) / phase_id / "score_summary.json",
              {"cases": cases, "total_score": 200, "mean_score": 100.0})
        return subprocess.CompletedProcess(command, 0)

    def evaluation_runner(self, round_index, attempt_index, candidate):
        return one_stop.run_adapter_phase(
            run_dir=self.run_dir, phase_id=f"{RUN_ID}-dev-r{round_index:03d}-a{attempt_index:03d}",
            candidate=candidate, cases=self.CASES, benchmark=self.tmp / "benchmark", env_prefix=self.tmp / "env",
            credential_file=self.tmp / "evaluator.env", jobs_dir=self.tmp / "jobs", n_concurrent=2,
            max_infrastructure_attempts=1)  # what one_stop passes for dev with --infrastructure-resume

    def controller(self):
        controller = one_stop.DevController(
            run_dir=self.run_dir, run_id=RUN_ID, workspace_submission=self.tmp / "workspace", max_rounds=5,
            evaluation_runner=self.evaluation_runner, infrastructure_resume=True)
        digest = adapter.tree_digest(self.candidate)
        record = {"round": 1, "evaluation_attempt": 1, "submission_id": f"dev-r001-a001-{digest[:12]}",
                  "candidate": str(self.candidate), "candidate_digest": digest, "state": "running"}
        controller._records.append(record)
        controller._record_by_id[record["submission_id"]] = record
        controller._record_by_digest[digest] = record
        controller._attempt_counter = 1
        controller._active_id = record["submission_id"]
        return controller, record

    def test_an_errored_eval_trial_is_replayed_and_the_round_is_accepted(self):
        controller, record = self.controller()
        controller._evaluate(record["submission_id"])
        self.assertEqual(self.commands, [f"{RUN_ID}-dev-r001-a001", f"{RUN_ID}-dev-r001-a002"])
        self.assertEqual((record["state"], record["accepted_round"], record["dev_mean"]), ("completed", True, 100.0))
        self.assertEqual(record["infrastructure_resume_attempts"], 1)
        self.assertEqual(Path(record["candidate"]), self.candidate)  # not moved to infrastructure_attempts/
        events = controller.infrastructure_events
        self.assertEqual([(e["state"], e["error_type"]) for e in events],
                         [("paused_infrastructure", "InfrastructureEvaluationError")])
        self.assertIn("VerifierTimeoutError", events[0]["error_message"])
        first = adapter.read_json(self.run_dir / "controller_logs" / f"{RUN_ID}-dev-r001-a001"
                                  / "infrastructure_resume_state.json")
        self.assertEqual((first["status"], first["resume_count"]), ("infrastructure_error", 1))
        self.assertIn("finished_at", first)
        second = adapter.read_json(self.run_dir / "controller_logs" / f"{RUN_ID}-dev-r001-a002"
                                   / "infrastructure_resume_state.json")
        self.assertEqual(second["status"], "completed_without_resume")

    def test_a_held_out_phase_resumes_instead_of_failing(self):
        summary, phase_dir = one_stop.run_adapter_phase(
            run_dir=self.run_dir, phase_id=f"{RUN_ID}-initial-test", candidate=self.candidate,
            cases=self.CASES, benchmark=self.tmp / "benchmark", env_prefix=self.tmp / "env",
            credential_file=self.tmp / "evaluator.env", jobs_dir=self.tmp / "jobs", n_concurrent=2,
            max_infrastructure_attempts=0)  # held-out with --infrastructure-resume: without limit
        self.assertEqual(self.commands, [f"{RUN_ID}-initial-test", f"{RUN_ID}-initial-test-infra-a002"])
        self.assertEqual(summary["mean_score"], 100.0)
        state = adapter.read_json(self.run_dir / "controller_logs" / f"{RUN_ID}-initial-test"
                                  / "infrastructure_resume_state.json")
        self.assertEqual((state["status"], state["resume_count"]), ("completed_after_resume", 1))

    def test_a_phase_that_fails_otherwise_records_how_it_ended(self):
        def broken(command, **kw):
            kw["stderr"].write("Traceback (most recent call last):\nValueError: unexpected contract layout\n")
            return subprocess.CompletedProcess(command, 1)

        with mock.patch.object(one_stop.subprocess, "run", side_effect=broken), self.assertRaises(RuntimeError) as c:
            self.evaluation_runner(1, 1, self.candidate)
        self.assertNotIsInstance(c.exception, adapter.InfrastructureEvaluationError)
        state = adapter.read_json(self.run_dir / "controller_logs" / f"{RUN_ID}-dev-r001-a001"
                                  / "infrastructure_resume_state.json")
        self.assertEqual(state["status"], "failed")
        self.assertIn("finished_at", state)
        self.assertEqual((state["events"][-1]["attempt"], state["events"][-1]["error_type"]), (1, "RuntimeError"))


if __name__ == "__main__":
    unittest.main()
