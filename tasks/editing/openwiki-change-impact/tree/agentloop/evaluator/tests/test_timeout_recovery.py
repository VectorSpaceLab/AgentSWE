"""Provider-free controls for conservative aggregate-timeout attribution."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentloop.evaluator import execution_evidence, lower_agent_launcher as launcher
from agentloop.evaluator.broker_observation import ObservationStore
from agentloop.evaluator.process_observation import ProcessTrace, load_product_start
from agentloop.evaluator.transport_sandbox import FixedLowerRelay, UnixConnection
from agentloop.protocol import LOWER_EFFORT, LOWER_MODEL, file_sha256, tree_digest


ROOT = Path(__file__).resolve().parents[3]
SHARED = Path('@@AGENTSWE_EDITING_CONTROL@@')
if not SHARED.is_dir():
    SHARED = ROOT.parent / 'shared-reference'
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))
from validate_formal_config import tree_digest as task_tree_digest


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True), encoding='utf-8')


def snapshot(**changes):
    value = dict.fromkeys((
        'calls', 'successful_calls', 'failures', 'broker_failures',
        'provider_failures', 'credential_failures', 'protocol_failures',
        'unknown_calls', 'pending_calls', 'unknown_usage_calls',
        'reserved_not_submitted_count', 'prompt_tokens', 'completion_tokens',
        'total_tokens', 'forced_overrides'), 0)
    value.update(schema_version=2, model=LOWER_MODEL, reasoning_effort=LOWER_EFFORT,
        broker_instance_id='provider-free-timeout-test', credential_value_recorded=False)
    value.update(changes)
    return value


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='timeout-recovery-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.repo, self.output = root / 'candidate', root / 'private'
        self.repo.joinpath('dist').mkdir(parents=True)
        self.repo.joinpath('dist/cli.js').write_text('console.log("fixture");\n')
        self.output.joinpath('product/dist').mkdir(parents=True)
        self.output.joinpath('product/dist/cli.js').write_bytes(self.repo.joinpath('dist/cli.js').read_bytes())
        self.output.joinpath('workspace').mkdir()
        self.output.joinpath('case_resources').mkdir()
        self.request = root / 'dev_001.md'
        self.request.write_text('Inspect the repository, then report "actual" state.\n')
        self.node = root / 'runtime/bin/node'
        self.node.parent.mkdir(parents=True)
        self.node.write_text('provider-free executable identity fixture\n')
        self.entry = self.output / 'product/dist/cli.js'
        # Exercise the real argv builder with an isolated executable fixture.
        self.addCleanup(patch.stopall)
        patch.object(launcher, 'TASK_NODE', self.node).start()
        patch.object(launcher.command, '__defaults__', (self.node,)).start()
        self.endpoint = 'http://127.0.0.1:18443/v1/responses'
        self.resources = {
            'schema_version': 'agentswe-owned-case-resources/v1',
            'valid': True, 'timed_out': True, 'timeout_seconds': 600,
            'unit': 'agentswe-edit-owner-c-timeout-test.scope',
            'description': 'AgentSWE case owner timeout-test',
            'cgroup': '/system.slice/agentswe-edit-owner-c-timeout-test.scope',
            'memory_bytes': 4096 * 1024 * 1024,
            'cleanup': {'complete': True, 'cgroup_events': 'populated 0\nfrozen 0\n'},
        }
        self.resources['observed'] = {'cgroup': self.resources['cgroup']}
        self.resources['systemd_properties'] = {
            'Id': self.resources['unit'], 'Description': self.resources['description'],
            'ControlGroup': self.resources['cgroup'],
            'MemoryMax': str(self.resources['memory_bytes']), 'MemorySwapMax': '0',
        }
        ownership = {key: self.resources[key] for key in ('unit', 'description', 'cgroup', 'memory_bytes')}
        write_json(self.output / 'case_resources/scope-ownership.json', ownership)
        self.context = {
            'candidate_digest': tree_digest(self.repo),
            'task_source_digest': task_tree_digest(ROOT),
            'product_entry_sha256': file_sha256(self.repo / 'dist/cli.js'),
            'request_sha256': file_sha256(self.request), 'case_id': 'dev_001',
            'output_path': str(self.output), 'workspace_path': str(self.output / 'workspace'),
            'resource_cgroup': '0::' + self.resources['cgroup'] + '\n',
            'remaining_case_timeout_seconds': 500,
        }
        self.save_context()
        self.publish_start()
        write_json(self.output / 'runtime-preflight.json', {'valid': True})
        write_json(self.output / 'broker_before.json', snapshot())
        write_json(self.output / 'transport_preflight.json', {'valid': True, 'endpoint_mapping_errors': []})
        self.after = snapshot()
        self.run = {'product_started': False, 'case_resource_contract': self.resources}
        self.relay = FixedLowerRelay(self.endpoint, context_id=self.context['context_id'], observation_dir=self.output).start()
        self.addCleanup(self.relay.close)
        connection = UnixConnection(self.relay.socket_path, timeout=3)
        try:
            connection.request('GET', '/transport-health')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())['valid'])
        finally:
            connection.close()
        self.seal_transport()

    def save_context(self):
        fields = {key: value for key, value in self.context.items() if key != 'context_id'}
        self.context['context_id'] = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        write_json(self.output / 'logical-context.json', self.context)

    def publish_start(self, argv=None):
        path = self.output / 'native-product-start.json'
        path.unlink(missing_ok=True)
        (self.output / 'native-process.trace').unlink(missing_ok=True)
        argv = launcher.command(self.output / 'product', self.request) if argv is None else argv
        raw = ('123 100.123456 execve(' + json.dumps(str(self.node)) + ', ['
            + ', '.join(json.dumps(arg) for arg in argv) + '], 0xabcdef /* 12 vars */) = 0\n').encode()
        trace = ProcessTrace(self.output, self.context, product_entry=self.entry, product_executable=self.node)
        try:
            trace._observe_product_start(raw)
            self.assertEqual(load_product_start(self.output, self.context,
                product_entry=self.entry, product_executable=self.node)['successful_execve']['argv'], argv)
        finally:
            trace.finish(interrupted=True)

    def seal_transport(self):
        summary = self.relay.close()
        self.assertTrue(summary['complete'], summary.get('errors'))
        write_json(self.output / 'native-broker-observation.json', summary)
        self.run['native_broker_observation'] = execution_evidence.broker_observation_reference(self.output, summary)

    def record_deliveries(self, count):
        store = ObservationStore(self.output, self.context['context_id'])
        for sequence in range(count):
            response = json.dumps({'id': 'resp_' + str(sequence), 'status': 'completed', 'output': []}).encode()
            record = store.record(b'{}', response, status=200, content_type='application/json')
            self.assertTrue(record['complete'], record.get('errors'))
        summary = store.close()
        self.assertTrue(summary['complete'], summary.get('errors'))
        write_json(self.output / 'native-broker-observation.json', summary)
        self.run['native_broker_observation'] = execution_evidence.broker_observation_reference(self.output, summary)

    def recover(self, **changes):
        arguments = dict(repo=self.repo, request=self.request, output=self.output,
            endpoint=self.endpoint, resources=self.resources)
        arguments.update(changes)
        with patch.object(launcher, 'read_broker_stats', return_value=self.after):
            return launcher._recover_owned_timeout(copy.deepcopy(self.run), **arguments)

    def assert_infrastructure(self, result):
        self.assertTrue(result['infrastructure_invalid'], result)
        self.assertNotEqual(result.get('classification'), 'candidate_timeout', result)
        self.assertIsNone(result.get('candidate_classification'), result)
        self.assertFalse(result.get('timeout_recovery', {}).get('verified'), result)

    def replace_summary(self, mutate, *, renew_reference=False):
        path = self.output / 'native-broker-observation.json'
        summary = json.loads(path.read_text())
        mutate(summary)
        write_json(path, summary)
        if renew_reference:
            self.run['native_broker_observation'] = execution_evidence.broker_observation_reference(self.output, summary)

    def test_no_call_recovers_only_with_complete_sealed_transport(self):
        result = self.recover()
        self.assertEqual(result['classification'], 'candidate_timeout', result)
        self.assertFalse(result['infrastructure_invalid'])
        self.assertTrue(result['timeout_start_recovered'])
        self.assertTrue(result['product_start_observed'])
        self.assertTrue(result['timeout_recovery']['delivery_complete'])
        self.assertEqual(result['broker_calls'], 0)
        record = execution_evidence.attest(result, case_id='dev_001',
            candidate_digest=self.context['candidate_digest'], output=self.output)
        self.assertEqual(record['classification'], 'candidate_timeout', record)
        self.assertEqual(record['failure_attribution']['party'], 'candidate')
        self.assertTrue(record['failure_attribution']['fatal'])

    def test_existing_infrastructure_has_precedence_and_never_reads_stats(self):
        for classification in ('provider_failure', 'credential_mount_failure', 'broker_failure',
                               'launcher_failure', 'native_binding_failure', 'evaluator_failure'):
            with self.subTest(classification=classification):
                value = dict(self.run, classification=classification,
                    infrastructure_invalid=True, failure='earlier observed failure')
                with patch.object(launcher, 'read_broker_stats', side_effect=AssertionError('must preserve prior failure')):
                    result = launcher._recover_owned_timeout(value, repo=self.repo, request=self.request,
                        output=self.output, endpoint=self.endpoint, resources=self.resources)
                self.assertEqual(result['classification'], classification)
                self.assertEqual(result['failure'], 'earlier observed failure')
                self.assertFalse(result['timeout_start_recovered'])

    def test_unknown_pending_and_reserved_ledger_states_are_rejected(self):
        for key in ('unknown_calls', 'pending_calls', 'unknown_usage_calls', 'reserved_not_submitted_count'):
            with self.subTest(key=key):
                self.after = snapshot(**{key: 1})
                self.assert_infrastructure(self.recover())

    def test_missing_null_boolean_and_negative_broker_counters_are_rejected(self):
        for location in ('before', 'after'):
            for key in snapshot():
                if type(snapshot()[key]) is not int or key == 'schema_version':
                    continue
                for value in (None, True, -1, '0', 'missing'):
                    with self.subTest(location=location, key=key, value=value):
                        damaged = snapshot(**{key: value})
                        if value == 'missing':
                            damaged.pop(key)
                        self.after = damaged if location == 'after' else snapshot()
                        write_json(self.output / 'broker_before.json', damaged if location == 'before' else snapshot())
                        self.assert_infrastructure(self.recover())

    def test_broker_protocol_and_instance_mismatch_are_rejected(self):
        for key, value in (('schema_version', 1), ('model', 'other'), ('reasoning_effort', 'other'),
                           ('broker_instance_id', ''), ('broker_instance_id', 'other-instance'),
                           ('credential_value_recorded', True)):
            with self.subTest(key=key, value=value):
                self.after = snapshot(**{key: value})
                self.assert_infrastructure(self.recover())

    def test_provider_failure_and_success_count_mismatch_are_rejected(self):
        for changes in ({'calls': 1}, {'provider_failures': 1}, {'broker_failures': 1},
                        {'credential_failures': 1}, {'failures': 1}, {'protocol_failures': 1}):
            with self.subTest(changes=changes):
                self.after = snapshot(**changes)
                self.assert_infrastructure(self.recover())

    def test_no_call_without_sealed_reference_is_rejected(self):
        self.run.pop('native_broker_observation')
        self.assert_infrastructure(self.recover())

    def test_completed_call_with_sealed_delivery_recovers(self):
        self.record_deliveries(1)
        self.after = snapshot(calls=1, successful_calls=1)
        result = self.recover()
        self.assertEqual(result['classification'], 'candidate_timeout', result)
        self.assertEqual(result['broker_successful_calls'], 1)

    def test_fewer_delivery_records_than_upstream_calls_is_rejected(self):
        self.record_deliveries(1)
        self.after = snapshot(calls=2, successful_calls=2)
        self.assert_infrastructure(self.recover())

    def test_cached_delivery_may_exceed_upstream_calls(self):
        self.record_deliveries(2)
        self.after = snapshot(calls=1, successful_calls=1)
        result = self.recover()
        self.assertEqual(result['classification'], 'candidate_timeout', result)

    def test_actual_unfinished_delivery_remains_infrastructure(self):
        store = ObservationStore(self.output, self.context['context_id'])
        exchange = store.begin(b'{}')
        exchange.feed_response(b'data: {"type":"response.created"}\n\n')
        summary = store.close(timeout=0)
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['pending'], 1)
        write_json(self.output / 'native-broker-observation.json', summary)
        self.run['native_broker_observation'] = execution_evidence.broker_observation_reference(self.output, summary)
        self.assert_infrastructure(self.recover())

    def test_no_call_without_summary_is_rejected(self):
        (self.output / 'native-broker-observation.json').unlink()
        self.assert_infrastructure(self.recover())

    def test_no_call_unsealed_summary_is_rejected_even_with_current_reference(self):
        self.replace_summary(lambda summary: summary.update(sealed=False), renew_reference=True)
        self.assert_infrastructure(self.recover())

    def test_changed_summary_is_rejected_against_launcher_reference(self):
        self.replace_summary(lambda summary: summary.update(unexpected_field='changed'))
        self.assert_infrastructure(self.recover())

    def test_transport_preflight_and_mapping_errors_are_rejected(self):
        path = self.output / 'transport_preflight.json'
        for value in ({'valid': False}, {'valid': True, 'endpoint_mapping_errors': ['unmapped_product_endpoint']}):
            with self.subTest(value=value):
                write_json(path, value)
                self.assert_infrastructure(self.recover())
        path.unlink()
        self.assert_infrastructure(self.recover())

    def test_recorded_relay_errors_are_not_erased_by_no_call(self):
        relay = FixedLowerRelay(self.endpoint, context_id=self.context['context_id'], observation_dir=self.output).start()
        connection = UnixConnection(relay.socket_path, timeout=3)
        try:
            connection.request('POST', '/unmapped', b'{}', {'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()
            summary = relay.close()
        self.assertTrue(summary['complete'])
        write_json(self.output / 'native-broker-observation.json', summary)
        self.run['native_broker_observation'] = execution_evidence.broker_observation_reference(self.output, summary)
        self.run['transport_errors'] = list(relay.errors)
        self.assertTrue(any(error.get('error') == 'unmapped_product_endpoint' for error in relay.errors))
        self.assert_infrastructure(self.recover())

    def test_health_endpoint_source_and_context_tampering_are_rejected(self):
        self.assert_infrastructure(self.recover(endpoint='http://127.0.0.1:18444/v1/responses'))
        path = self.output / 'native-transport-health.json'
        original = path.read_bytes()
        for key, value in (('context_id', 'other'), ('valid', False), ('health_request_observed', False),
                           ('fixed_loopback_host', 'example.invalid'), ('observer_source', {})):
            with self.subTest(key=key):
                changed = json.loads(original)
                changed[key] = value
                write_json(path, changed)
                self.assert_infrastructure(self.recover())
        path.unlink()
        self.assert_infrastructure(self.recover())

    def test_runtime_preflight_is_required(self):
        write_json(self.output / 'runtime-preflight.json', {'valid': False})
        self.assert_infrastructure(self.recover())

    def test_context_hash_and_request_identity_tampering_are_rejected(self):
        path = self.output / 'logical-context.json'
        changed = dict(self.context, case_id='other')
        write_json(path, changed)
        self.assert_infrastructure(self.recover())
        self.context['request_sha256'] = '0' * 64
        self.save_context()
        self.assert_infrastructure(self.recover())

    def test_rehashed_context_must_match_actual_source_candidate_request_and_scope(self):
        original = dict(self.context)
        for key, value in (('candidate_digest', '0' * 64), ('task_source_digest', '0' * 64),
                           ('product_entry_sha256', '0' * 64), ('request_sha256', '0' * 64),
                           ('case_id', 'other'), ('output_path', '/other'), ('workspace_path', '/other'),
                           ('resource_cgroup', '0::/other\n'), ('remaining_case_timeout_seconds', 601)):
            with self.subTest(key=key):
                self.context = dict(original, **{key: value})
                self.save_context()
                result = self.recover()
                self.assert_infrastructure(result)
                self.assertNotIn('product start', result['failure'])

    def test_full_product_argv_is_required(self):
        expected = launcher.command(self.output / 'product', self.request)
        for argv in (expected[:-1] + ['different request'], expected[:-2] + ['other-model', expected[-1]],
                     expected[:2] + ['--print', expected[-1]], expected + ['--unexpected']):
            with self.subTest(argv=argv):
                self.publish_start(argv)
                self.assert_infrastructure(self.recover())

    def test_product_start_witness_is_required(self):
        (self.output / 'native-product-start.json').unlink()
        self.assert_infrastructure(self.recover())

    def test_cleanup_memory_and_observed_cgroup_evidence_are_required(self):
        variants = [dict(self.resources, valid=False), dict(self.resources, timed_out=False),
            dict(self.resources, memory_bytes=1024), dict(self.resources, cleanup={'complete': False}),
            dict(self.resources, cleanup={'complete': True, 'cgroup_events': 'populated 1\n'}),
            dict(self.resources, observed={'cgroup': '/other'})]
        for resources in variants:
            with self.subTest(resources=resources):
                self.assert_infrastructure(self.recover(resources=resources))

    def test_scope_ownership_and_systemd_identity_tampering_are_rejected(self):
        for key, value in (('Id', 'unrelated.scope'), ('Description', 'unrelated'),
                           ('ControlGroup', '/unrelated'), ('MemoryMax', '1024'), ('MemorySwapMax', 'max')):
            resources = copy.deepcopy(self.resources)
            resources['systemd_properties'][key] = value
            with self.subTest(key=key):
                self.assert_infrastructure(self.recover(resources=resources))
        resources = dict(self.resources, description='changed owner')
        self.assert_infrastructure(self.recover(resources=resources))

    def test_malformed_resource_objects_fail_closed(self):
        for key in ('cleanup', 'observed', 'systemd_properties'):
            for value in (None, [], 'invalid'):
                with self.subTest(key=key, value=value):
                    self.assert_infrastructure(self.recover(resources=dict(self.resources, **{key: value})))

    def test_substantive_artifact_with_incomplete_authorship_remains_infrastructure(self):
        write_json(self.output / 'workspace/agent_result.json', {'observations': ['substantive partial evidence']})
        (self.output / 'stdout.log').write_text('process interrupted before terminal result\n')
        result = self.recover()
        record = execution_evidence.attest(result, case_id='dev_001',
            candidate_digest=self.context['candidate_digest'], output=self.output)
        self.assertTrue(record['artifact_validation']['substantive_native_artifact'])
        self.assertEqual(record['classification'], 'infrastructure_invalid', record)
        self.assertTrue(record['infrastructure_invalid'])

    def test_substantive_artifact_without_stdout_cannot_become_fatal_zero(self):
        write_json(self.output / 'workspace/agent_result.json', {'observations': ['substantive partial evidence']})
        result = self.recover()
        record = execution_evidence.attest(result, case_id='dev_001',
            candidate_digest=self.context['candidate_digest'], output=self.output)
        self.assertEqual(record['classification'], 'infrastructure_invalid', record)
        self.assertTrue(record['infrastructure_invalid'])

    def test_substantive_authored_artifact_without_budgeted_semantics_remains_infrastructure(self):
        self.record_deliveries(1)
        self.after = snapshot(calls=1, successful_calls=1)
        artifact = {'schema_version': 'openwiki-agent-result/v1', 'case_id': 'dev_001',
            'observations': ['actual evidence'], 'integrity': {}, 'decision': {'completion_claim': 'partial'}}
        write_json(self.output / 'workspace/agent_result.json', artifact)
        (self.output / 'stdout.log').write_text(json.dumps(artifact))
        result = self.recover()
        record = execution_evidence.attest(result, case_id='dev_001',
            candidate_digest=self.context['candidate_digest'], output=self.output)
        self.assertTrue(record['artifact_validation']['valid'])
        self.assertTrue(record['real_execution'])
        self.assertEqual(record['classification'], 'infrastructure_invalid', record)
        self.assertTrue(record['infrastructure_invalid'])


if __name__ == '__main__':
    unittest.main()
