"""0921 case-clock regressions: the 600s belongs to the agent, not to setup.

No model, no container and no real case execution. Every number here is either a
published constant or arithmetic over one; the evidence behind the choices is in
14-openclaw/openclaw.ANALYSIS.md.
"""
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from broker import responses_broker
from evaluator import case_budget, suite_runtime
from evaluator.candidate_outcome import terminal_cause, terminal_major_errors, TERMINAL_CAUSES
from evaluator.case_budget import (AGENT_CLOCK_SECONDS, CASE_SECONDS, FINALIZATION_SECONDS,
                                   PRODUCT_CLEANUP_SECONDS, SETUP_ALLOWANCE_SECONDS, CaseBudget)
from evaluator.suite_runtime import SuiteClock, SUITE_SECONDS
from lower_agent import owned_resources, sandbox_transport
from lower_agent.embedded_agent import EmbeddedAgent
from lower_agent.launcher import CASE_TIMEOUT_SECONDS

ROOT = Path(__file__).resolve().parents[2]


class EnvelopeArithmeticTests(unittest.TestCase):
    def test_case_envelope_is_setup_allowance_plus_agent_clock_plus_reserve(self):
        self.assertEqual(AGENT_CLOCK_SECONDS, 600)
        self.assertEqual(SETUP_ALLOWANCE_SECONDS, 300)
        self.assertEqual(CASE_SECONDS, SETUP_ALLOWANCE_SECONDS + AGENT_CLOCK_SECONDS
                         + PRODUCT_CLEANUP_SECONDS + FINALIZATION_SECONDS)
        self.assertEqual(CASE_SECONDS, 930)
        self.assertEqual(CASE_TIMEOUT_SECONDS, CASE_SECONDS)

    def test_measured_0919_setup_fits_inside_the_allowance(self):
        # hidden/test_00N/run_report.json#timing_contract on 0905-...-0919-fw-001.
        for seconds in (214.7, 217.8, 220.6, 232.4, 236.1, 249.7):
            self.assertLess(seconds, SETUP_ALLOWANCE_SECONDS)

    def test_owned_scope_ceilings_track_the_published_envelopes(self):
        # owned_resources keeps literals because it also runs under `python3 -I`.
        self.assertEqual(owned_resources.DEADLINE_SECONDS, CASE_SECONDS)
        self.assertEqual(owned_resources.SUITE_DEADLINE_SECONDS, SUITE_SECONDS)
        with self.assertRaises(ValueError):
            owned_resources.run_owned(['/bin/true'], cwd=ROOT, env={},
                                      output=ROOT / 'never-created', timeout=CASE_SECONDS + 1)

    def test_suite_runtime_uses_one_case_constant(self):
        self.assertIs(suite_runtime.CASE_SECONDS, case_budget.CASE_SECONDS)

    def test_suite_envelope_admits_six_full_cases_after_a_real_cold_build(self):
        # 0919 formal suite: build admission 42.5s, cold build done 226.2s,
        # first case preparation 250.7s.
        current = [1000.0]
        clock = SuiteClock(started=1000.0, deadline=1000.0 + SUITE_SECONDS,
                           case_ids=tuple('test_%03d' % i for i in range(1, 7)),
                           clock=lambda: current[0])
        current[0] = 1042.5
        self.assertGreater(clock.build_deadline(), current[0] + 600)
        current[0] = 1250.7
        for case in clock.case_ids:
            started, end = clock.begin_case(case)
            self.assertAlmostEqual(end - started, CASE_SECONDS, places=6)
            current[0] = end
        self.assertEqual(clock.snapshot()['admitted_cases'], 6)
        self.assertTrue(clock.snapshot()['within_total_budget'])

    def test_case_budget_snapshot_publishes_the_split(self):
        value = CaseBudget(1000.0, 1000.0 + CASE_SECONDS).snapshot()
        self.assertEqual(value['maximum_case_seconds'], CASE_SECONDS)
        self.assertEqual(value['agent_clock_seconds'], AGENT_CLOCK_SECONDS)
        self.assertEqual(value['setup_allowance_seconds'], SETUP_ALLOWANCE_SECONDS)
        self.assertIn('sandbox', value['agent_clock_anchor'])
        self.assertEqual(value['product_deadline'] if 'product_deadline' in value
                         else value['product_deadline_monotonic'], 1000.0 + CASE_SECONDS - 30)


class AgentClockAnchorTests(unittest.TestCase):
    """The agent's 600s starts at its own sandbox, after evaluator setup."""

    def cluster(self, tmp, case_deadline):
        cluster = SimpleNamespace(output=tmp / 'output', workspace=tmp / 'workspace',
            product=tmp / 'product', state=tmp / 'state', runtime={}, env={},
            endpoint='http://127.0.0.1:9/v1/responses', bridge=19876,
            case_socket=None, node=Path('/pinned/bin/node'), deadline=case_deadline,
            product_readonly=True, sandboxes={})
        cluster.output.mkdir(parents=True); cluster.workspace.mkdir(parents=True)
        return cluster

    def build(self, tmp, *, setup_seconds):
        now = time.monotonic()
        # The launcher started `setup_seconds` ago; the case envelope runs from there.
        case_deadline = now - setup_seconds + CASE_SECONDS - PRODUCT_CLEANUP_SECONDS - FINALIZATION_SECONDS
        sandbox = Mock(namespace={'net_inode': 3}, native_relays={})
        with patch('lower_agent.embedded_agent.ProductSandbox', return_value=sandbox):
            agent = EmbeddedAgent(self.cluster(tmp, case_deadline),
                                  message='A real task.', session_key='agent:main:c')
        return agent, now

    def test_setup_is_not_charged_to_the_agent(self):
        import tempfile
        for setup in (0.0, 214.7, 249.7, 299.0):
            with tempfile.TemporaryDirectory() as directory:
                agent, now = self.build(Path(directory), setup_seconds=setup)
                window = agent.deadline - agent.clock_started
                self.assertAlmostEqual(window, AGENT_CLOCK_SECONDS, delta=2.0,
                                       msg='setup=%.1fs stole %.1fs' % (setup, AGENT_CLOCK_SECONDS - window))
                self.assertTrue(agent.evidence['agent_clock_full'])
                self.assertEqual(agent.evidence['agent_clock_seconds'], AGENT_CLOCK_SECONDS)

    def test_setup_overrun_shortens_the_clock_instead_of_overrunning_the_case(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            agent, _ = self.build(Path(directory), setup_seconds=400.0)
            self.assertLess(agent.deadline - agent.clock_started, AGENT_CLOCK_SECONDS)
            self.assertFalse(agent.evidence['agent_clock_full'])


class DispatchReserveTests(unittest.TestCase):
    def test_reserve_is_sized_from_the_ledgers_not_a_policy_floor(self):
        # 0919 hidden lower ledger: 226 settled attempts, max elapsed 14.38s;
        # public 47 attempts, max 11.10s; widest recorded in this tree 19.6s.
        self.assertEqual(sandbox_transport.LOWER_DISPATCH_RESERVE_SECONDS, 25.0)
        self.assertGreater(sandbox_transport.LOWER_DISPATCH_RESERVE_SECONDS, 19.6)
        self.assertLess(sandbox_transport.LOWER_DISPATCH_RESERVE_SECONDS, 60.0)

    def test_the_guard_belongs_to_the_host_relay_whose_evidence_is_collected(self):
        self.assertEqual(sandbox_transport.BRIDGE_DISPATCH_RESERVE_SECONDS, 0.0)
        for is_host, expected in ((True, 25.0), (False, 0.0)):
            server = SimpleNamespace()
            sandbox_transport.configure_server(server, time.monotonic() + 60,
                                               lambda timeout: None, is_host=is_host)
            self.assertEqual(server.dispatch_reserve, expected)

    def test_the_broker_still_refuses_a_deadline_beyond_the_agent_clock(self):
        self.assertEqual(responses_broker.LOWER_MAX_SECONDS, float(AGENT_CLOCK_SECONDS))


class TerminalCauseTests(unittest.TestCase):
    """missing_core must name which of the three evidenced causes it was."""

    def agent(self, **overrides):
        value = {'status': 'completed', 'exit_code': 0, 'started_monotonic': 100.0,
                 'ended_monotonic': 340.0, 'agent_clock_seconds': AGENT_CLOCK_SECONDS,
                 'agent_clock_full': True, 'observed_stop_reason': 'stop',
                 'sandbox': {'transport': {'dispatch_guard': {'refused_requests': 0,
                                                              'turns_completed': 23}}}}
        value.update(overrides)
        return value

    def test_budget_refusal_is_named_as_the_evaluators_own_clock(self):
        agent = self.agent(status='candidate_process_error', exit_code=1,
                           observed_stop_reason=None,
                           sandbox={'transport': {'dispatch_guard': {'refused_requests': 4,
                                                                     'turns_completed': 50}}})
        cause, detail = terminal_cause(agent, 'missing_core')
        self.assertEqual(cause, 'evaluator_case_budget_refused_further_model_calls')
        self.assertIn('case clock', TERMINAL_CAUSES[cause])
        self.assertEqual(detail['budget_guard_refusals'], 4)
        self.assertEqual(detail['model_turns_completed'], 50)
        errors = terminal_major_errors(cause, detail, 'missing_core')
        self.assertEqual(len(errors), 2)
        self.assertIn('refused 4 further model request', errors[1])
        self.assertIn('first product response', errors[1])

    def test_a_loop_that_stopped_by_itself_is_not_called_a_budget_refusal(self):
        cause, detail = terminal_cause(self.agent(observed_stop_reason='toolUse'), 'missing_core')
        self.assertEqual(cause, 'agent_loop_ended_without_core_delivery')
        self.assertEqual(detail['product_reported_stop_reason'], 'toolUse')
        self.assertTrue(detail['stop_reason_is_product_self_report'])
        self.assertIn('stopReason=toolUse', terminal_major_errors(cause, detail, 'missing_core')[1])

    def test_a_process_error_without_a_refusal_is_its_own_cause(self):
        cause, _ = terminal_cause(self.agent(status='candidate_process_error', exit_code=1,
                                             observed_stop_reason=None), 'missing_core')
        self.assertEqual(cause, 'agent_process_error_before_core_delivery')

    def test_the_three_causes_are_distinct_and_all_explained(self):
        self.assertEqual(len(TERMINAL_CAUSES), 3)
        self.assertEqual(len(set(TERMINAL_CAUSES.values())), 3)

    def test_other_fatal_reasons_keep_their_own_identity(self):
        cause, _ = terminal_cause(self.agent(), 'non_json_core')
        self.assertEqual(cause, 'core_output_non_json_core')

    def test_no_hidden_or_oracle_field_reaches_the_detail(self):
        detail = terminal_cause(self.agent(), 'missing_core')[1]
        self.assertEqual(set(detail), {'native_agent_status', 'agent_exit_code',
            'model_turns_completed', 'budget_guard_refusals', 'agent_clock_seconds',
            'agent_clock_full', 'agent_elapsed_seconds', 'product_reported_stop_reason',
            'stop_reason_is_product_self_report'})


class PublishedContractTests(unittest.TestCase):
    def test_the_driver_asks_for_an_incremental_artifact(self):
        text = (ROOT / 'lower_agent/launcher.py').read_text(encoding='utf-8')
        self.assertNotIn('if you can complete this case', text)
        self.assertIn('Runtime delivery contract (incremental', text)
        self.assertIn('decision_state', text)
        self.assertIn('first response from the product', text)

    def test_the_resources_document_publishes_the_same_numbers(self):
        text = ' '.join((ROOT / 'input/04_resources.md').read_text(encoding='utf-8').split())
        for token in ('6,900 seconds of wall clock', '930-second', '300 s', '600 s',
                      'lower_dispatch_refused_by_case_budget_guard', '25 seconds'):
            self.assertIn(token, text)
        self.assertIn('pkill -f openclaw', text)

    def test_the_requirements_document_agrees(self):
        text = ' '.join((ROOT / 'input/03_requirements_and_constraints.md').read_text(encoding='utf-8').split())
        self.assertIn('6,900-second suite budget', text)
        self.assertIn('930-second envelope', text)
        self.assertIn('decision_state', text)


if __name__ == '__main__':
    unittest.main()
