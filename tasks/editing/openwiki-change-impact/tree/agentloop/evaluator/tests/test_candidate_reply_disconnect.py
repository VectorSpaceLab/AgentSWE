"""Candidate reply disconnect (2026-10-09), next to 122: the Candidate's own product aborts its model call after the
broker completed it, the relay's reply write fails with BrokenPipe, and the product exits by itself.  The shapes follow
the public Lite smoke of 2026-10-09 (OpenWiki hidden test_001).  Provider-free: no product, broker or model runs."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from agentloop.evaluator import execution_evidence, lower_agent_launcher as launcher
from agentloop.protocol import LOWER_EFFORT, LOWER_MODEL

PATH = '/v1/chat/completions'


def broker_snapshot(**changes):
    value = dict.fromkeys(('calls', 'successful_calls', 'failures', 'broker_failures', 'provider_failures',
                           'credential_failures', 'protocol_failures', 'pending_calls', 'prompt_tokens',
                           'completion_tokens', 'total_tokens', 'forced_overrides'), 0)
    value.update(schema_version=2, model=LOWER_MODEL, reasoning_effort=LOWER_EFFORT, requests=[],
                 broker_instance_id='provider-free-disconnect-test', credential_value_recorded=False)
    value.update(changes)
    return value


COMPLETED_ROW = {'ok': True, 'upstream_completion': 'completed', 'error': None, 'transport_attempts': 1,
                 'request_sha256': 'a' * 64, 'usage_state': 'known'}


class CandidateReplyDisconnect(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='reply-disconnect-')
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name).resolve()
        (self.output / 'workspace').mkdir()
        self.observation_path = str(self.output / 'broker-observation-0001.json')
        self.transport = {'valid': True, 'relay_health': True, 'loopback_connection_valid': True,
                          'schema_version': 'agentswe-isolated-lower-transport/v1'}
        (self.output / 'transport_preflight.json').write_text(json.dumps(self.transport))
        # the D-run shape: one model call, completed upstream (200, finish_reason stop, 34.05 s), reply write failed
        self.relay_errors = [
            {'error': 'BrokenPipeError', 'path': PATH},
            {'error': 'broker_observation_incomplete', 'observation_path': self.observation_path, 'path': PATH,
             'reasons': ['BrokenPipeError']},
            {'error': 'broker_observation_store_incomplete'}]
        self.observation = {
            'complete': False, 'errors': [], 'pending': 0, 'relay_dropped_events': 0, 'relay_pending_handlers': 0,
            'sealed': True, 'records': [{
                'path': self.observation_path, 'sequence': 1, 'complete': False, 'capture_complete': False,
                'response_status': 200, 'model_status': 'terminal', 'errors': ['BrokenPipeError'],
                'choice_finish_reasons': {'0': 'stop'}, 'started_at': 1791549311.5058124,
                'finished_at': 1791549345.5553842}]}
        self.before = broker_snapshot()
        self.after = broker_snapshot(calls=1, successful_calls=1, prompt_tokens=528, completion_tokens=8608,
                                     total_tokens=9136, requests=[dict(COMPLETED_ROW)])
        self.broker = launcher._stats_delta(self.before, self.after)

    def verdict(self, **changes):
        arguments = dict(transport=self.transport, relay_errors=self.relay_errors, observation=self.observation,
                         broker_before=self.before, broker_after=self.after, broker=self.broker)
        arguments.update(copy.deepcopy(changes))
        return launcher._self_exit_transport_verdict(**arguments)

    def launcher_value(self, transport_failure, disconnect):
        """The launcher's self-exit record for the D run: exit 1, no artifact, one completed call."""
        value = {'valid': False, 'exit_code': 1, 'product_started': True, 'artifact_preexisting': False,
                 'candidate_classification': 'candidate_contract_failure', 'broker_calls': 1,
                 'broker_successful_calls': 1, 'broker_failures': 0, 'provider_failures': 0,
                 'elapsed_seconds': 49.262, 'semantic_observer_in_case_budget': True,
                 'classification': transport_failure or 'candidate_contract_failure',
                 'infrastructure_invalid': transport_failure is not None,
                 'transport_errors': [] if disconnect is not None else list(self.relay_errors)}
        if disconnect is not None:
            value['candidate_reply_disconnect'] = disconnect
        (self.output / 'launcher_result.json').write_text(json.dumps(value))
        return value

    def test_candidate_disconnect_shape_is_candidate(self):
        failure, disconnect = self.verdict()
        self.assertIsNone(failure)
        self.assertEqual(disconnect['tolerated_transport_errors'], self.relay_errors)  # recorded, not dropped
        (request,) = disconnect['disconnected_requests']
        self.assertEqual((request['path'], request['error'], request['response_status'], request['upstream_seconds']),
                         (PATH, 'BrokenPipeError', 200, 34.05))
        record = execution_evidence.attest(self.launcher_value(failure, disconnect), case_id='test_001',
                                           candidate_digest='c' * 64, output=self.output)
        self.assertEqual(record['failure_attribution']['party'], 'candidate')
        self.assertEqual(record['classification'], 'candidate_behavior_failure')
        self.assertFalse(record.get('infrastructure_invalid'))
        self.assertTrue(record['environment_preflight']['valid'])

    def test_connection_reset_on_the_reply_write_is_the_same_disconnect(self):
        errors = copy.deepcopy(self.relay_errors)
        errors[0]['error'] = 'ConnectionResetError'
        errors[1]['reasons'] = ['ConnectionResetError']
        observation = copy.deepcopy(self.observation)
        observation['records'][0]['errors'] = ['ConnectionResetError']
        self.assertIsNone(self.verdict(relay_errors=errors, observation=observation)[0])

    def test_relay_error_without_broker_completion_stays_infrastructure(self):
        no_answer = copy.deepcopy(self.observation)
        no_answer['records'][0].update(response_status=0, model_status='unknown')
        failed_row = broker_snapshot(calls=1, successful_calls=1, requests=[dict(COMPLETED_ROW, ok=False,
                                     upstream_completion='unknown_or_failed')])
        pending = broker_snapshot(calls=1, successful_calls=1, pending_calls=1, requests=[dict(COMPLETED_ROW)])
        for changes in ({'observation': no_answer}, {'broker_after': failed_row},
                        {'broker_after': pending},
                        {'broker': dict(self.broker, failures=1, provider_failures=1)},
                        {'broker': dict(self.broker, calls=1, successful_calls=0)},
                        {'broker_after': broker_snapshot(requests=[])}):
            with self.subTest(changes=sorted(changes)):
                self.assertEqual(self.verdict(**changes), ('evaluator_failure', None))
        old = execution_evidence.attest(self.launcher_value('evaluator_failure', None), case_id='test_001',
                                        candidate_digest='c' * 64, output=self.output)
        self.assertEqual((old['classification'], old['failure_attribution']['party']),
                         ('infrastructure_invalid', 'evaluator'))

    def test_anything_more_than_the_disconnect_stays_infrastructure(self):
        extra = self.relay_errors + [{'error': 'broker_http_error', 'path': PATH, 'status': 502}]
        timeout = copy.deepcopy(self.relay_errors)
        timeout[0]['error'] = 'TimeoutError'
        mismatched = copy.deepcopy(self.relay_errors)
        mismatched[1]['reasons'] = ['BrokenPipeError', 'observation_feed_ValueError']
        dropped = dict(copy.deepcopy(self.observation), relay_dropped_events=1)
        store_error = dict(copy.deepcopy(self.observation), errors=['observer_workers_pending_at_close'])
        other_incomplete = copy.deepcopy(self.observation)
        other_incomplete['records'].append({'path': str(self.output / 'broker-observation-0002.json'),
                                            'complete': False, 'response_status': 200, 'errors': []})
        cases = {'other relay error': {'relay_errors': extra}, 'not a disconnect': {'relay_errors': timeout},
                 'observation names more': {'relay_errors': mismatched},
                 'dropped relay events': {'observation': dropped},
                 'observation store error': {'observation': store_error},
                 'another incomplete exchange': {'observation': other_incomplete},
                 'invalid preflight': {'transport': dict(self.transport, valid=False)},
                 'endpoint mapping error': {'transport': dict(self.transport, endpoint_mapping_errors=['x'])},
                 'disconnect without its observation': {'relay_errors': self.relay_errors[:1]}}
        for name, changes in cases.items():
            with self.subTest(name):
                self.assertEqual(self.verdict(**changes), ('evaluator_failure', None))
        self.assertEqual(self.verdict(relay_errors=[]), (None, None))  # a clean case is unchanged

    def test_judge_evidence_accepts_an_observation_incomplete_only_by_the_tolerated_disconnect(self):
        _, disconnect = self.verdict()
        run = {'candidate_reply_disconnect': disconnect}
        sealed = dict(copy.deepcopy(self.observation), sealed=True)
        self.assertTrue(execution_evidence.disconnect_only_incomplete(sealed, run))
        other = copy.deepcopy(sealed)
        other['records'].append({'path': str(self.output / 'broker-observation-0002.json'), 'complete': False})
        for name, observation, record in (('no tolerated disconnect', sealed, {}),
                                          ('another incomplete exchange', other, run),
                                          ('store error', dict(sealed, errors=['x']), run),
                                          ('unsealed', dict(sealed, sealed=False), run)):
            with self.subTest(name):
                self.assertFalse(execution_evidence.disconnect_only_incomplete(observation, record))

    def timeout_value(self, calls, successful):
        return {'classification': 'candidate_timeout', 'candidate_classification': 'candidate_timeout',
                'infrastructure_invalid': False, 'product_started': True, 'broker_calls': calls,
                'broker_successful_calls': successful, 'broker_delta': {'failures': 0}}

    def test_evaluator_deadline_kill_keeps_the_122_behaviour(self):
        tolerated = launcher._d122_deadline_kill_in_flight(self.timeout_value(1, 0), self.relay_errors,
                                                           self.transport, self.observation)
        self.assertEqual(tolerated['package'], '122')
        self.assertEqual(tolerated['classification_kept'], 'candidate_timeout')

    def test_disconnect_followed_by_a_deadline_kill_stays_infrastructure(self):
        second = str(self.output / 'broker-observation-0002.json')
        errors = self.relay_errors[:2] + [
            {'error': 'BrokenPipeError', 'path': PATH},
            {'error': 'broker_observation_incomplete', 'observation_path': second, 'path': PATH,
             'reasons': ['BrokenPipeError']},
            {'error': 'broker_observation_store_incomplete'}]
        self.assertIsNone(launcher._d122_deadline_kill_in_flight(self.timeout_value(2, 1), errors, self.transport,
                                                                 self.observation))
        # the deadline path never consults the self-exit rule: it returns before the verdict
        source = Path(launcher.__file__).read_text(encoding='utf-8')
        run_case = source[source.index('def _run_case('):]
        timeout_branch = run_case[run_case.index('except subprocess.TimeoutExpired'):run_case.index('except OSError')]
        self.assertNotIn('_self_exit_transport_verdict', timeout_branch)
        self.assertNotIn('_self_exit_transport_verdict', run_case[:run_case.index('workspace_digest_after = ')])


if __name__ == '__main__':
    unittest.main()
