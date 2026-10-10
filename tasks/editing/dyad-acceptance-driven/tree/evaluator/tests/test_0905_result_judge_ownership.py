#!/usr/bin/env python3
"""Provider-free tests for formal Result-judge broker ownership."""
from __future__ import annotations

import importlib.util
import inspect
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harbor"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


one_stop = load_module("dyad_0905_formal_one_stop", ROOT / "harbor/formal_one_stop.py")
lower = load_module("dyad_0905_lower_case", ROOT / "evaluator/harness/run_lower_agent_case.py")


def xhigh_stats(**runtime: int) -> dict:
    counters = {k: 0 for k in ('calls','completed_calls','successful_calls','failures','upstream_attempts','tokens','usage_unknown_calls','in_flight_calls')}
    counters.update(runtime)
    return {'schema_version': 'agentswe-judge-broker-stats/v1',
        'protocol': {'model':'gpt-5.6-sol','reasoning_effort':'max','max_output_tokens':12000,
          'inner_retries':0,'max_upstream_attempts_per_transport':1,'absolute_deadline_seconds_max':900,
          'redirects_allowed':False,'response_transport_modes':['stream','nonstream'],
          'downstream_response_format':'terminal_json','connect_timeout_seconds_max':30,
          'upstream_attempt_marker':'before_first_http_bytes_after_connection'}, 'runtime':counters}


def lower_stats(*, calls: int, successful: int, provider_failures: int = 0,
                broker_failures: int = 0, failures: int = 0,
                client_failures: int = 0) -> dict:
    return {
        "protocol": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        "runtime": {
            "calls": calls,
            "successful_calls": successful,
            "provider_failures": provider_failures,
            "broker_failures": broker_failures,
            "failures": failures,
            "client_failures": client_failures,
        },
    }


class ResultJudgeOwnershipTests(unittest.TestCase):
    def test_fresh_xhigh_gate_requires_zero_calls(self) -> None:
        self.assertTrue(one_stop.fresh_xhigh_result_judge(xhigh_stats(calls=0)))
        self.assertFalse(one_stop.fresh_xhigh_result_judge(xhigh_stats(calls=1)))
        self.assertFalse(one_stop.fresh_xhigh_result_judge({
            "protocol": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
            "runtime": {"calls": 0},
        }))

    def test_formal_rejects_external_endpoint_but_pilot_does_not_use_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "evaluator-owned"):
            one_stop.reject_external_result_judge_endpoint(
                formal=True, endpoint="http://external.invalid/v1/responses"
            )
        one_stop.reject_external_result_judge_endpoint(
            formal=False, endpoint="http://ignored.invalid/v1/responses"
        )

    def test_formal_cli_no_longer_requires_external_endpoint(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            one_stop.main(["--run-formal"])
        message = stderr.getvalue()
        self.assertIn("formal readiness refused:", message)
        self.assertIn("10/10 readiness gate", message)
        self.assertNotIn("requires --result-judge-broker-endpoint", message)

        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            one_stop.main([
                "--run-formal", "--result-judge-broker-endpoint",
                "http://external.invalid/v1/responses",
            ])
        self.assertIn("must not be supplied", stderr.getvalue())

    def test_finalizer_command_uses_owned_endpoint_and_fixed_judges(self) -> None:
        command = one_stop.formal_finalizer_command(
            run_dir=Path("/tmp/dyad-run"),
            output=Path("/tmp/dyad-run/formal_aggregation.json"),
            credential=Path("/run/secrets/provider.env"),
            result_judge_endpoint="http://127.0.0.1:19090/v1/responses",
        )
        self.assertEqual(
            command[command.index("--result-judge-broker-endpoint") + 1],
            "http://127.0.0.1:19090/v1/responses",
        )
        self.assertEqual(
            command[command.index("--result-judge") + 1],
            str(one_stop.RESULT_JUDGE_ENTRY),
        )
        self.assertEqual(
            command[command.index("--code-judge") + 1],
            str(one_stop.CODE_JUDGE_RUNNER_ENTRY),
        )

    def test_result_judge_broker_launch_is_xhigh_and_role_bound(self) -> None:
        runtime = mock.Mock()
        runtime.start_judge_broker.return_value = mock.Mock(container_id='owned-judge-cid', endpoint='http://127.0.0.1:19090/v1/responses')
        ownership = {}
        with mock.patch.object(one_stop, '_judge_runtime', return_value=runtime):
            endpoint = one_stop.start_broker(name='dyad-result-judge-test', script=Path('/ignored.py'),
                credential=Path('/run/secrets/provider.env'), port=19090, image='test-image',
                provider_url='https://provider.invalid/v1/responses',broker_kind='result_judge_xhigh',
                ownership=ownership, ownership_key='result', evidence_dir=Path('/evidence'))
        self.assertEqual(endpoint, 'http://127.0.0.1:19090/v1/responses')
        self.assertEqual(ownership, {'result':'owned-judge-cid'})
        self.assertEqual(runtime.start_judge_broker.call_args.kwargs['cidfile'], Path('/evidence/result_judge.cid'))

    def test_remove_container_uses_owned_id_and_distinguishes_inspect_error(self) -> None:
        with mock.patch.object(
            one_stop.subprocess, "run",
            side_effect=[
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=1, stdout="", stderr="daemon unavailable"),
            ],
        ) as run:
            receipt = one_stop.remove_container("dyad-result-judge-test", "container-id")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["docker", "rm", "-f", "container-id"])
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "inspect", "container-id"])
        self.assertEqual(receipt["inspect_exit_code"], 1)
        self.assertFalse(receipt["absent_after_cleanup"])
        self.assertIn("daemon unavailable", receipt["inspect_error"])

    def test_remove_container_without_owned_id_is_fail_closed(self) -> None:
        with mock.patch.object(one_stop.subprocess, "run") as run:
            receipt = one_stop.remove_container("dyad-result-judge-test")
        run.assert_not_called()
        self.assertFalse(receipt["ownership_proven"])
        self.assertFalse(receipt["absent_after_cleanup"])

    def test_judge_starts_after_hidden_and_before_finalizer(self) -> None:
        source = inspect.getsource(one_stop.run_formal)
        hidden = source.index("hidden = execute_hidden")
        public_judge = source.index('public_judge_endpoint = start_broker')
        judge = source.index('result_judge_endpoint = start_broker')
        builder = source.index('builder_exit = run_builder')
        self.assertLess(public_judge, builder)
        finalizer = source.index("formal_finalizer_command")
        self.assertLess(hidden, judge)
        self.assertLess(judge, finalizer)

    def test_candidate_and_provider_failures_remain_separate(self) -> None:
        infrastructure = lower.classify(
            mode="headless", launcher_exit=1, artifact=None,
            before=lower_stats(calls=0, successful=0),
            after=lower_stats(calls=1, successful=0, provider_failures=1),
        )
        self.assertEqual(infrastructure, ("infrastructure-invalid", "provider_error"))
        candidate = lower.classify(
            mode="headless", launcher_exit=1,
            artifact=None,
            native_evidence={"real_product": True, "acceptance_surface_observed": False},
            before=lower_stats(calls=0, successful=0),
            after=lower_stats(calls=1, successful=1),
        )
        self.assertEqual(candidate, ("candidate_failure", "acceptance_surface_missing"))
        mixed = lower.classify(
            mode="headless", launcher_exit=0,
            artifact=None,
            native_evidence={"real_product": True},
            before=lower_stats(calls=0, successful=0),
            after=lower_stats(calls=2, successful=1, provider_failures=1),
        )
        self.assertEqual(mixed, ("infrastructure-invalid", "provider_error"))


if __name__ == "__main__":
    unittest.main()
