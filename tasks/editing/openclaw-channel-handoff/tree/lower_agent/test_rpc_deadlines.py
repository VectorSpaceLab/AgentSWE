from __future__ import annotations
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from lower_agent import launcher as lower
from evaluator import hidden_executor as hidden


class RpcDeadlineTests(unittest.TestCase):
    def call(self, method, params, remaining, limit=None):
        with patch.object(lower.time, 'monotonic', return_value=100), patch.object(lower, 'run_rpc', return_value={'status': 'ok'}) as rpc:
            result = lower.deadline_rpc(Path('/node'), Path('/product'), 1234, 'synthetic', method,
                                        params, {}, 100 + remaining, limit)
        return result, rpc

    def test_health_uses_seconds_in_python_and_milliseconds_in_cli(self):
        result, rpc = self.call('health', {}, 120, 15)
        command, _, _, seconds = rpc.call_args.args
        self.assertEqual(seconds, 15)
        self.assertEqual(command[command.index('--timeout') + 1], '15000')
        self.assertEqual(result['timeout_seconds'], 15)

    def test_health_is_clamped_to_startup_remaining_budget(self):
        _, rpc = self.call('health', {}, 2.75, 15)
        command, _, _, seconds = rpc.call_args.args
        self.assertEqual(seconds, 2.75)
        self.assertEqual(command[command.index('--timeout') + 1], '2750')

    def test_agent_wait_and_transport_share_remaining_budget_not_300s_default(self):
        params = {'runId': 'synthetic'}
        _, rpc = self.call('agent.wait', params, 432.5)
        command, _, _, seconds = rpc.call_args.args
        supplied = json.loads(command[command.index('--params') + 1])
        self.assertEqual(supplied['timeoutMs'], 432500)
        self.assertEqual(command[command.index('--timeout') + 1], '432500')
        self.assertEqual(seconds, 432.5)
        self.assertEqual(params, {'runId': 'synthetic'})

    def test_existing_smaller_operation_budget_is_not_expanded(self):
        _, rpc = self.call('agent', {'timeout': 180}, 500)
        command = rpc.call_args.args[0]
        self.assertEqual(json.loads(command[command.index('--params') + 1])['timeout'], 180)
        _, rpc = self.call('agent', {'timeout': 600}, 45.5)
        command = rpc.call_args.args[0]
        self.assertEqual(json.loads(command[command.index('--params') + 1])['timeout'], 45)

    def test_no_rpc_after_deadline(self):
        result, rpc = self.call('agent.wait', {}, -1)
        rpc.assert_not_called()
        self.assertFalse(result['dispatched'])
        self.assertEqual(result['status'], 'timeout')

    def test_ready_marker_delay_is_charged_to_same_120s_budget(self):
        clock = [0.0]
        gateway = Mock(); gateway.poll.return_value = None
        log = Mock()
        def delayed_ready(**_kwargs):
            clock[0] += 112
            return '[gateway] ready'
        log.read_text.side_effect = delayed_ready
        budgets = []
        def rpc(_cmd, _cwd, _env, timeout):
            budgets.append(timeout); clock[0] += timeout
            return {'status': 'timeout'}
        with patch.object(lower.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(lower.time, 'sleep', side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
             patch.object(lower, 'run_rpc', side_effect=rpc):
            lower.wait_gateway_health(gateway, log, Path('/node'), Path('/product'), 1, 'synthetic', {}, 590)
        self.assertEqual(budgets, [8])
        self.assertEqual(clock[0], 120)

    def test_no_health_rpc_if_gateway_never_ready(self):
        clock = [0.0]
        gateway = Mock(); gateway.poll.return_value = None
        log = Mock(); log.read_text.return_value = 'still starting'
        with patch.object(lower.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(lower.time, 'sleep', side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
             patch.object(lower, 'run_rpc') as rpc:
            self.assertIsNone(lower.wait_gateway_health(gateway, log, Path('/node'), Path('/product'), 1, 'synthetic', {}, 25))
        self.assertEqual(clock[0], 25)
        rpc.assert_not_called()

    def test_actual_stalled_cli_is_timed_out_and_reaped(self):
        command = [sys.executable, '-u', '-c', 'import os,time; print(os.getpid(), flush=True); time.sleep(30)']
        started = time.monotonic()
        result = lower.run_rpc(command, Path.cwd(), os.environ.copy(), 0.25)
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(time.monotonic() - started, 2)
        pid = int(re.search(r'\d+', result['stdout_tail']).group())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_outer_executor_caps_legacy_1200s_and_passes_same_absolute_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); case = root / 'test_001'; case.mkdir()
            (case / 'input.md').write_text('synthetic unit request')
            candidate = root / 'candidate'; candidate.mkdir()
            process = subprocess.CompletedProcess([], 1, '', '')
            resource = {'valid': True, 'timed_out': False, 'cleanup': {'complete': True}}
            with patch.object(hidden, 'tree_digest', return_value='fixed'), \
                 patch.object(hidden, 'safe_stats', return_value=({'runtime': {}}, None)), \
                 patch.object(hidden, 'run_owned', return_value=(process, resource)) as run:
                result = hidden.launch_case(case_id='test_001', hidden_case=case, frozen_candidate=candidate,
                    output=root / 'output', broker_endpoint='http://127.0.0.1:9/v1/responses', runtime=None,
                    private_file=root / 'private.json', view={}, timeout_seconds=1200)
            contract = result['timing_contract']
            # 930 = 300s evaluator setup allowance + the agent's 600s clock + 30s reserve.
            self.assertEqual(contract['effective_timeout_seconds'], 930)
            self.assertEqual(contract['requested_timeout_seconds'], 1200)
            self.assertEqual(contract['agent_clock_seconds'], 600)
            command = run.call_args.args[0]
            self.assertEqual(float(command[command.index('--case-deadline-monotonic') + 1]), contract['absolute_case_deadline_monotonic'])
            self.assertLessEqual(run.call_args.kwargs['timeout'], 930)
            self.assertEqual(run.call_args.kwargs['memory_bytes'], 24 * 1024**3)
            self.assertTrue(contract['aggregate_scope_valid'])
            self.assertFalse(result['formal_result_claimed'])
            self.assertEqual(result['classification'], 'launcher_infrastructure_error')

    def test_nonfinite_deadline_rejected_before_filesystem_work(self):
        for value in ('nan', 'inf'):
            with patch.object(sys, 'argv', ['launcher', '--product', '/not-read', '--case', '/not-read',
                 '--broker-endpoint', 'http://127.0.0.1:9/v1/responses', '--output', '/not-created',
                 '--case-deadline-monotonic', value]):
                with self.assertRaisesRegex(SystemExit, 'finite'):
                    lower.main()

    def test_scope_failure_is_infrastructure_without_unscoped_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); case = root / 'test_001'; case.mkdir()
            (case / 'input.md').write_text('synthetic scope failure')
            candidate = root / 'candidate'; candidate.mkdir()
            with patch.object(hidden, 'tree_digest', return_value='fixed'), \
                 patch.object(hidden, 'safe_stats', return_value=({'runtime': {}}, None)), \
                 patch.object(hidden, 'run_owned', side_effect=RuntimeError('scope ownership unverified')) as run, \
                 patch.object(hidden.subprocess, 'Popen') as unscoped:
                value = hidden.launch_case(case_id='test_001', hidden_case=case, frozen_candidate=candidate,
                    output=root / 'output', broker_endpoint='http://127.0.0.1:9/v1/responses', runtime=None,
                    private_file=root / 'private.json', view={}, timeout_seconds=600)
            self.assertEqual(run.call_count, 1)
            unscoped.assert_not_called()
            self.assertEqual(value['classification'], 'launcher_infrastructure_error')
            self.assertIn('scope ownership unverified', value['classification_reason'])
            self.assertFalse(value['timing_contract']['aggregate_scope_valid'])
            self.assertFalse(value['formal_result_claimed'])

    def test_actual_sigterm_reaps_separate_session_gateway_process(self):
        """Real sandbox/processes/signals; synthetic Python Gateway, zero API."""
        if not Path('/usr/bin/bwrap').exists():
            self.skipTest('requires the Linux task sandbox host')
        children = []
        original_popen = subprocess.Popen
        previous_handler = signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); product = root / 'product'; product.mkdir()
            (product / 'openclaw.mjs').write_text('import time\nprint("[gateway] ready", flush=True)\ntime.sleep(30)\n')
            case = root / 'test_001'; case.mkdir(); (case / 'input.md').write_text('synthetic case')
            runtime_root = root / 'runtime'
            runtime_node = runtime_root / 'node-test' / 'bin' / 'node'
            runtime_node.parent.mkdir(parents=True)
            (runtime_root / 'bin').mkdir(); (runtime_root / 'lib').mkdir()
            shutil.copy2(sys.executable, runtime_node)
            (runtime_root / 'bin' / 'node').symlink_to(runtime_node)
            def popen(*args, **kwargs):
                process = original_popen(*args, **kwargs); children.append(process); return process
            def rpc(_node, _product, _port, _token, method, *_args, **_kwargs):
                if method == 'health':
                    return {'status': 'ok'}
                raise AssertionError('initial task must not be seeded before native context')
            def interrupt_native():
                os.kill(os.getpid(), signal.SIGTERM)
                raise AssertionError('SIGTERM handler did not interrupt')
            try:
                with patch.object(sys, 'argv', ['launcher', '--product', str(product), '--case', str(case),
                     '--broker-endpoint', 'http://127.0.0.1:9/v1/responses', '--output', str(root / 'output')]), \
                     patch.object(lower, 'read_broker_stats', return_value={'runtime': {}}), \
                     patch.object(lower, 'resolve', return_value={'node': str(runtime_root / 'bin' / 'node'), 'root': str(runtime_root)}), \
                     patch.object(lower, 'materialize_product', return_value=product), \
                     patch.object(lower.subprocess, 'Popen', side_effect=popen), \
                     patch.object(lower, 'deadline_rpc', side_effect=rpc), \
                     patch('lower_agent.embedded_agent.EmbeddedAgent.wait', side_effect=interrupt_native):
                    with self.assertRaises(SystemExit) as caught:
                        lower.main()
                self.assertEqual(caught.exception.code, 143)
                # Both fault-target Gateways and the native reasoning process.
                self.assertEqual(len(children), 3)
                for child in children:
                    self.assertIsNotNone(child.poll())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child.pid, 0)
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
            finally:
                for process in children:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=2)
                signal.signal(signal.SIGTERM, previous_handler)

    def test_sandbox_start_failure_is_infrastructure_without_host_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); product = root / 'product'; product.mkdir()
            case = root / 'test_001'; case.mkdir(); (case / 'input.md').write_text('synthetic case')
            output = root / 'output'
            with patch.object(sys, 'argv', ['launcher', '--product', str(product), '--case', str(case),
                 '--broker-endpoint', 'http://127.0.0.1:9/v1/responses', '--output', str(output)]), \
                 patch.object(lower, 'read_broker_stats', return_value={'runtime': {}}), \
                 patch.object(lower, 'resolve', return_value={'node': sys.executable, 'root': str(root)}), \
                 patch.object(lower, 'materialize_product', return_value=product), \
                 patch('lower_agent.gateway_cluster.ProductSandbox', side_effect=RuntimeError('namespace preflight failed')) as sandbox, \
                 patch.object(lower.subprocess, 'Popen') as unscoped, \
                 patch('builtins.print'):
                self.assertEqual(lower.main(), 1)
            sandbox.assert_called_once(); unscoped.assert_not_called()
            report = json.loads((output / 'run_report.json').read_text())
            self.assertEqual(report['classification'], 'launcher_infrastructure_error')
            self.assertFalse(report['formal_result_claimed'])
            self.assertEqual(report['artifact_contract']['artifact_dir'], str(output / 'workspace'))

    def test_artifact_symlink_cannot_make_host_validator_read_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); workspace = root / 'workspace'; workspace.mkdir()
            private = root / 'private.json'; private.write_text('{}')
            result = workspace / 'agent_result.json'; result.symlink_to(private)
            report = workspace / 'run_report.json'; report.symlink_to(private)
            for valid, reason in (lower.validate_agent_result(result, 'test_001'),
                                  lower.validate_agent_run_report(report)):
                self.assertFalse(valid)
                self.assertIn('outside the Agent artifact workspace', reason)

    def test_sandbox_identity_work_is_charged_to_original_rpc_budget(self):
        from contextlib import contextmanager
        clock = [100.0]
        class Owned:
            @contextmanager
            def rpc_command(self, command):
                clock[0] += 4
                yield ['synthetic-wrapper', *command], (123,)
        with patch.object(lower.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(lower, 'run_rpc', return_value={'status': 'ok'}) as rpc:
            lower.deadline_rpc(Path('/node'), Path('/product'), 1, 'synthetic', 'health', {},
                               {}, 220, 15, sandbox=Owned())
        self.assertEqual(rpc.call_args.args[3], 11)
        self.assertEqual(rpc.call_args.kwargs['pass_fds'], (123,))

    def test_unresolved_broker_request_is_infrastructure_even_with_valid_artifacts(self):
        delta = lower.broker_stats_delta({'runtime': {}}, {'runtime': {'calls': 1, 'successful_calls': 1}})
        for field in ('broker_in_flight_before', 'broker_in_flight_after'):
            report = {'status': 'completed', 'health': {'status': 'ok'}, field: 1}
            classification, _ = lower.classify_execution(report, delta, True, None)
            self.assertEqual(classification, 'broker_infrastructure_error')

    def test_known_candidate_request_error_is_not_reclassified_as_provider_outage(self):
        delta = lower.broker_stats_delta({'runtime': {}}, {'runtime': {
            'calls': 1, 'failures': 1, 'successful_calls': 0, 'client_failures': 1, 'provider_failures': 0}})
        report = {'status': 'partial', 'health': {'status': 'ok'}, 'errors': ['Responses request invalid']}
        self.assertEqual(lower.classify_execution(report, delta, False, 'missing required artifact')[0],
                         'candidate_product_failure')
        # A valid partial artifact must reach ordinary semantic scoring. A
        # caller-owned bad request is not a reason to discard that artifact.
        self.assertEqual(lower.classify_execution(report, delta, True, None)[0], 'candidate_behavior_observed')
        self.assertEqual(lower.classify_execution({**report, 'status': 'failed'}, delta, True, None)[0],
                         'candidate_behavior_failure')
        delta['client_failures'] = 0; delta['provider_failures'] = 1
        self.assertEqual(lower.classify_execution(report, delta, True, None)[0], 'broker_infrastructure_error')

    def test_unavailable_or_inflight_initial_stats_prevent_new_lower_dispatch(self):
        for initial in (OSError('synthetic unavailable stats'), {'runtime': {'in_flight_calls': 1}}):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); product = root / 'product'; product.mkdir()
                case = root / 'test_001'; case.mkdir(); (case / 'input.md').write_text('synthetic case')
                output = root / 'output'
                with patch.object(sys, 'argv', ['launcher', '--product', str(product), '--case', str(case),
                     '--broker-endpoint', 'http://127.0.0.1:9/v1/responses', '--output', str(output)]), \
                     patch.object(lower, 'read_broker_stats', side_effect=[initial, {'runtime': {}}]), \
                     patch.object(lower, 'resolve', return_value={'node': sys.executable, 'root': str(root)}), \
                     patch.object(lower, 'materialize_product', return_value=product), \
                     patch.object(lower, 'ProductSandbox') as sandbox, patch('builtins.print'):
                    self.assertEqual(lower.main(), 1)
                sandbox.assert_not_called()
                report = json.loads((output / 'run_report.json').read_text())
                self.assertEqual(report['classification'], 'broker_infrastructure_error')


if __name__ == '__main__':
    unittest.main()
