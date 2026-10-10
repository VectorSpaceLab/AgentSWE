import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import judge_broker_runtime as runtime
from judge_broker_xhigh import State


class JudgeRuntimeTests(unittest.TestCase):
    def test_freshness_binds_instance_model_and_single_attempt_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = State(Path(tmp) / 'stats.json', 'expected-instance').snapshot()
            self.assertTrue(runtime.fresh_judge_stats(value, 'expected-instance'))
            self.assertFalse(runtime.fresh_judge_stats(value, 'foreign-instance'))
            for key, replacement in (('model', 'wrong'), ('reasoning_effort', 'high'),
                                     ('inner_retries', 5), ('max_upstream_attempts_per_transport', 8),
                                     ('redirects_allowed', True), ('max_output_tokens', 24000),
                                     ('upstream_attempt_marker', 'before_opener')):
                changed = json.loads(json.dumps(value))
                changed['protocol'][key] = replacement
                self.assertFalse(runtime.fresh_judge_stats(changed, 'expected-instance'))
            value['runtime']['calls'] = 1
            self.assertFalse(runtime.fresh_judge_stats(value))

    def test_old_builder_stats_cannot_pass(self):
        self.assertFalse(runtime.fresh_judge_stats({'protocol': {'model': 'deepseek-flash',
            'reasoning_effort': 'max'}, 'runtime': {'calls': 0, 'tokens': 0, 'failures': 0}}))

    def test_existing_cid_is_immutable_and_never_starts_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cid = Path(tmp) / 'owned.cid'
            cid.write_text('preserve historical ownership')
            obj = runtime.JudgeBroker(name='test', credential=Path(tmp)/'fake.env', image='mock', port=1234, cidfile=cid)
            with patch.object(runtime.subprocess, 'run') as command:
                with self.assertRaises(FileExistsError):
                    obj.start()
            self.assertEqual(command.call_count, 0)
            self.assertEqual(cid.read_text(), 'preserve historical ownership')

    def test_wrong_container_label_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = runtime.JudgeBroker(name='test', credential=Path(tmp)/'fake.env', image='mock', port=1234,
                                      cidfile=Path(tmp)/'owned.cid')
            obj.directory.mkdir()
            obj.container_id = 'a'*64
            wrong = [{'Id': obj.container_id, 'Config': {'Labels': {'agentswe.judge.instance': 'someone-else'}}}]
            with patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(wrong), '')) as command:
                result = obj.close()
                again = obj.close()
            self.assertFalse(result['absent_after_cleanup'])
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0], ['docker', 'inspect', obj.container_id])
            self.assertEqual(result, again)

    def test_upstream_url_normalization_and_no_embedded_credentials(self):
        for url in ('https://example.invalid', 'https://example.invalid/v1', 'https://example.invalid/v1/responses'):
            self.assertEqual(runtime.upstream_responses(url), 'https://example.invalid/v1/responses')
        for url in ('file:///tmp/private', 'https://user:password@example.invalid'):
            with self.assertRaises(ValueError):
                runtime.upstream_responses(url)

    def test_readiness_retains_stopped_owned_container_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = runtime.JudgeBroker(name='test', credential=Path(tmp)/'fake.env', image='mock', port=1234,
                                      cidfile=Path(tmp)/'owned.cid', defer_removal=True)
            obj.directory.mkdir()
            obj.container_id = 'a'*64
            commands = []
            def command(argv, **kwargs):
                commands.append(argv)
                return subprocess.CompletedProcess(argv, 0, '{"CPUPerc":"0.0%"}\n', '')
            with patch.object(obj, '_inspect', side_effect=[{'State': {'Running': True}},
                    {'State': {'Running': False, 'Status': 'exited'}}]), \
                    patch.object(runtime, 'stats', return_value={'runtime': {'in_flight_calls': 0}}), \
                    patch.object(runtime.subprocess, 'run', side_effect=command):
                result = obj.close()
            self.assertTrue(result['removal_deferred'])
            self.assertTrue(result['process_stopped'])
            self.assertFalse(result['absent_after_cleanup'])
            self.assertFalse(any('rm' in cmd for cmd in commands))
            self.assertEqual(json.loads((obj.directory/'container_terminal.json').read_text())['state']['Status'], 'exited')
            self.assertTrue((obj.directory/'container_pre_stop_stats.json').is_file())

    def test_readiness_missing_terminal_proof_does_not_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = runtime.JudgeBroker(name='test', credential=Path(tmp)/'fake.env', image='mock', port=1234,
                                      cidfile=Path(tmp)/'owned.cid', defer_removal=True)
            obj.directory.mkdir()
            obj.container_id = 'a'*64
            with patch.object(obj, '_inspect', return_value={'State': {'Running': True}}), \
                    patch.object(runtime, 'stats', return_value={}), \
                    patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as cmd:
                result = obj.close()
            self.assertFalse(result.get('process_stopped', False))
            self.assertIn('cleanup_error_type', result)
            self.assertFalse(any('rm' in call.args[0] for call in cmd.call_args_list))


if __name__ == '__main__':
    unittest.main()
