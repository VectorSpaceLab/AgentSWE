"""Offline controls for native terminal and asynchronous feedback provenance."""
import json
from pathlib import Path
import tempfile
import unittest

from harbor.native_builder_evidence import (NativeEvidenceError,
    classify_native_termination, pair_native_tools, sha, submission_commands, verify_native)


THREAD = '01a09696-310f-7872-9a9e-2cf5602c1cdc'


def stream_events(*middle):
    return [{'type': 'thread.started', 'thread_id': THREAD},
            {'type': 'turn.started'}, *middle, {'type': 'turn.completed', 'usage': {}}]


def call(call_id, command):
    return {'type': 'response_item', 'payload': {'type': 'function_call',
            'call_id': call_id, 'name': 'exec_command', 'arguments': json.dumps(command)}}


def output(call_id, result):
    return {'type': 'response_item', 'payload': {'type': 'function_call_output',
            'call_id': call_id, 'output': json.dumps(result)}}


class NativeTerminationTests(unittest.TestCase):
    def test_reconnect_before_completed_is_retained_but_not_fatal(self):
        error = {'type': 'error', 'message': 'Reconnecting... 1/5 (stream disconnected)'}
        proof = classify_native_termination(stream_events(error))
        self.assertTrue(proof['successful_terminal'])
        self.assertEqual(proof['error_events'][0]['event'], error)
        self.assertIsNone(proof['actual_upstream_requests'])
        self.assertFalse(proof['complete_provider_billing_claimed'])

    def test_terminal_unrecognized_and_out_of_turn_errors_fail(self):
        samples = [stream_events({'type': 'turn.failed'}),
            stream_events({'type': 'error', 'message': 'quota exhausted'}),
            stream_events({'type': 'error', 'message': 'Reconnecting... 6/5 (bad)'}),
            stream_events() + [{'type': 'error', 'message': 'Reconnecting... 1/5 (late)'}],
            stream_events()[:-1], stream_events() + [{'type': 'turn.completed'}],
            stream_events()[1:], list(reversed(stream_events()))]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(classify_native_termination(sample)['successful_terminal'])


class NativePairingTests(unittest.TestCase):
    def test_shell_parser_excludes_quoted_multiline_and_heredoc_text(self):
        commands = ["printf 'x\nsubmit_dev_candidate --wait'",
            "printf '\n' 'submit_dev_candidate' '--wait'",
            "printf ';' 'submit_dev_candidate' '--wait'",
            "echo \"x; submit_dev_candidate --wait\"",
            "cat <<'EOF'\nsubmit_dev_candidate --wait\nEOF\n",
            "cat <<EOF\nsubmit_dev_candidate --wait\nEOF\n",
            'echo "$(submit_dev_candidate --wait)"',
            'function unused() { submit_dev_candidate --wait; }']
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(submission_commands(command), [])

    def test_shell_parser_accepts_known_bare_and_absolute_invocations(self):
        for command in ['cd /workspace/worktree && submit_dev_candidate --wait',
            '/usr/local/bin/submit_dev_candidate --wait',
            'export PATH=/opt/agentswe-openhands/bin:$PATH\nsubmit_dev_candidate --wait',
            'printf "diagnostic"; /usr/local/bin/submit_dev_candidate --feedback-digest abc --wait']:
            with self.subTest(command=command):
                self.assertEqual(len(submission_commands(command)), 1)

    def test_async_wait_inherits_only_exact_returned_handle(self):
        native = [call('a', {'cmd': 'submit_dev_candidate --wait'}),
            output('a', {'session_id': 42}), call('b', {'session_id': 42}), output('b', {'status': 200})]
        paired = pair_native_tools(native)
        self.assertEqual(paired['by_call']['b']['submission_origin']['call_id'], 'a')
        native[2] = call('b', {'session_id': 43})
        self.assertIsNone(pair_native_tools(native)['by_call']['b']['submission_origin'])

    def test_diagnostic_reference_is_not_submit_source(self):
        for command in ["ps -ef | rg 'submit_dev_candidate'", "echo 'submit_dev_candidate --wait'", 'cat instructions.md']:
            native = [call('a', {'cmd': command}), output('a', {})]
            self.assertIsNone(pair_native_tools(native)['by_call']['a']['submission_origin'])

    def test_tool_orphan_duplicate_type_and_missing_output_rejected(self):
        paired = [call('a', {'cmd': 'true'}), output('a', {})]
        wrong_type = output('a', {})
        wrong_type['payload']['type'] = 'custom_tool_call_output'
        for native in [paired[:1], paired[1:], list(reversed(paired)),
                       paired + [paired[0]], paired + [paired[1]], [paired[0], wrong_type]]:
            with self.subTest(native=native), self.assertRaises(NativeEvidenceError):
                pair_native_tools(native)


class NativeAcceptedProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name).resolve()
        self.agent = self.run / 'jobs/native/trial/agent'
        self.agent.mkdir(parents=True)
        (self.run / 'builder_job_config.json').write_text(json.dumps({
            'jobs_dir': str(self.run / 'jobs'), 'job_name': 'native'}))
        self.stream = self.agent / 'codex.txt'
        (self.agent / 'sessions').mkdir()
        self.rollout = self.agent / 'sessions/rollout-test.jsonl'
        self.meta = [{'type': 'session_meta', 'payload': {'id': THREAD,
            'cli_version': '0.144.1', 'source': 'exec', 'cwd': '/workspace/worktree'}},
            {'type': 'turn_context', 'payload': {'model': 'deepseek-flash', 'effort': 'max'}}]
        self.feedback = {'dev': {'dev_001': 30, 'dev_002': 40}}
        path = self.run / 'feedback.json'
        data = json.dumps(self.feedback).encode()
        path.write_bytes(data)
        self.record = {'builder_session_id': THREAD, 'feedback_path': str(path),
            'feedback_digest': sha(data), 'candidate_digest': 'product-digest'}
        self.payload = {'submission_number': 1, 'builder_session_id': THREAD,
            'candidate_digest': 'product-digest', 'feedback_digest': sha(data), 'feedback': self.feedback}
        self.delivery = {'candidate_number': 1, 'builder_session_id': THREAD,
            'feedback_digest': sha(data), 'payload_sha256': sha(json.dumps(
                self.payload, sort_keys=True, ensure_ascii=False).encode())}

    def write(self, events, native):
        self.stream.write_text(''.join(json.dumps(v) + '\n' for v in events))
        self.rollout.write_text(''.join(json.dumps(v) + '\n' for v in self.meta + native))

    def verify(self):
        return verify_native(self.run, [self.record], [self.delivery], [])

    def test_recovered_native_turn_requires_real_paired_accepted_feedback(self):
        native = [call('submit', {'cmd': 'submit_dev_candidate --wait'}),
            output('submit', {'session_id': 42}), call('poll', {'session_id': 42}),
            output('poll', {'status': 200, 'payload': self.payload})]
        self.write(stream_events({'type': 'error', 'message': 'Reconnecting... 1/5 (transport)'}), native)
        proof = self.verify()
        self.assertTrue(proof['valid'], proof)
        self.assertEqual(proof['feedback_received'][0]['submission_call_id'], 'submit')
        self.delivery['payload_sha256'] = 'tampered'
        self.assertFalse(self.verify()['valid'])

    def test_completed_turn_does_not_accept_echoed_feedback_or_wrong_thread(self):
        self.write(stream_events(), [call('fake', {'cmd': 'echo evidence'}),
            output('fake', {'status': 200, 'payload': self.payload})])
        self.assertIn('no paired native submit source', self.verify()['errors'][0])
        self.meta[0]['payload']['id'] = 'wrong-thread'
        self.write(stream_events(), [])
        self.assertIn('rollout identity', self.verify()['errors'][0])

    def test_no_accepted_round_remains_invalid_after_terminal_fix(self):
        self.write(stream_events(), [])
        proof = verify_native(self.run, [], [], [])
        self.assertEqual(proof['errors'], ['no accepted submissions'])
        self.assertTrue(proof['native_termination']['successful_terminal'])

    def test_two_round_revision_is_bound_to_prior_feedback_and_new_source(self):
        self.record['build'] = {'candidate_repo_digest': 'product-one'}
        second_path = self.run / 'feedback-two.json'
        second_feedback = {'dev': {'dev_001': 50, 'dev_002': 55}}
        second_path.write_text(json.dumps(second_feedback))
        second_digest = sha(second_path.read_bytes())
        prior_digest = self.record['feedback_digest']
        second_record = {'builder_session_id': THREAD,
            'feedback_path': str(second_path), 'feedback_digest': second_digest,
            'feedback_digest_ack': prior_digest, 'candidate_digest': 'product-two',
            'build': {'candidate_repo_digest': 'product-two'}}
        second_payload = {'submission_number': 2, 'builder_session_id': THREAD,
            'candidate_digest': 'product-two', 'feedback_digest': second_digest,
            'feedback_digest_ack': prior_digest, 'feedback': second_feedback}
        second_delivery = {'candidate_number': 2, 'builder_session_id': THREAD,
            'feedback_digest': second_digest, 'payload_sha256': sha(json.dumps(
                second_payload, sort_keys=True, ensure_ascii=False).encode())}
        native = [call('one', {'cmd': 'submit_dev_candidate --wait'}),
            output('one', {'status': 200, 'payload': self.payload}),
            call('two', {'cmd': 'cd /workspace/worktree && /usr/local/bin/submit_dev_candidate '
                '--feedback-digest ' + prior_digest + ' --wait'}),
            output('two', {'session_id': 42}), call('poll-two', {'session_id': 42}),
            output('poll-two', {'status': 200, 'payload': second_payload})]
        self.write(stream_events(), native)
        verify = lambda: verify_native(self.run, [self.record, second_record],
                                       [self.delivery, second_delivery], [])
        proof = verify()
        self.assertTrue(proof['valid'], proof)
        self.assertTrue(proof['revision_observed'])
        self.assertEqual(proof['feedback_received'][1]['submission_call_id'], 'two')
        second_record['build']['candidate_repo_digest'] = 'product-one'
        self.assertIn('revision did not change product source', verify()['errors'][0])


if __name__ == '__main__':
    unittest.main()
