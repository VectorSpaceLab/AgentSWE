"""Provider-free negative controls for trusted CLI startup evidence."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentloop.evaluator.process_observation import (
    ProcessTrace, PRODUCT_START_NAME, _successful_product_exec, _successful_product_exec_records, load_product_start, run_observed,
)

NODE = '/runtime/bin/node'
ENTRY = '/private/product/dist/cli.js'
CONTEXT = {'case_id': 'dev_001', 'candidate_digest': 'candidate', 'context_id': 'context', 'resource_cgroup': '0::/test'}


def exec_line(executable=NODE, argv=None, result='0', pid=123):
    argv = argv if argv is not None else [NODE, ENTRY, '--print', 'task with "quotes" and \\slashes']
    return f'{pid} 100.123456 execve({json.dumps(executable)}, [{", ".join(json.dumps(a) for a in argv)}], 0xabcdef /* 12 vars */) = {result}\n'.encode()


class ProductStartWitnessTests(unittest.TestCase):
    def test_success_requires_exact_executable_and_first_two_arguments(self):
        expected = _successful_product_exec(exec_line(), NODE, ENTRY)
        self.assertEqual(expected['host_pid'], 123)
        self.assertEqual(expected['argv'][0:2], [NODE, ENTRY])
        for line in (exec_line('/different/node'), exec_line(argv=[NODE, '/different/cli.js']),
                     exec_line(argv=['node', ENTRY]), exec_line(result='-1 ENOENT (No such file)'),
                     exec_line(argv=[NODE])):
            self.assertIsNone(_successful_product_exec(line, NODE, ENTRY))

    def test_escaped_paths_are_decoded_without_eval(self):
        line = exec_line().replace(b'"/runtime/bin/node"', b'"/runtime/bin/\\156ode"').replace(b'"/private/product/dist/cli.js"', b'"/private/product/dist/cli\\x2ejs"')
        self.assertIsNotNone(_successful_product_exec(line, NODE, ENTRY))
        self.assertIsNone(_successful_product_exec(line.replace(b'\\156', b'\\q'), NODE, ENTRY))

    def test_truncated_unfinished_resumed_and_missing_newline_are_rejected(self):
        raw = exec_line()
        variants = [raw[:-1], raw.replace(b'"--print"', b'"--print"...'),
            raw.replace(b'"--print"', b'...'), raw.replace(b') = 0', b' <unfinished ...>'),
            b'123 100.123456 <... execve resumed>) = 0\n', raw.replace(b'"--print"', b'"\\q"')]
        for line in variants:
            with self.subTest(line=line):
                self.assertIsNone(_successful_product_exec(line, NODE, ENTRY))

    def test_successful_exec_pair_preserves_both_actual_records(self):
        first = exec_line().replace(b') = 0\n', b' <unfinished ...>\n')
        last = b'123 100.456789 <... execve resumed>) = 0\n'
        self.assertIsNotNone(_successful_product_exec_records([first, last], NODE, ENTRY))
        for pair in ([first], [last], [first, last.replace(b'123 ', b'124 ')],
                     [first, last.replace(b'= 0', b'= -1 ENOENT')],
                     [first.replace(b'"--print"', b'...'), last]):
            self.assertIsNone(_successful_product_exec_records(pair, NODE, ENTRY))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            trace = ProcessTrace(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
            try:
                trace._observe_product_start(first)
                self.assertFalse((output / PRODUCT_START_NAME).exists())
                trace._observe_product_start(last)
                witness = load_product_start(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
                self.assertEqual([base64.b64decode(value) for value in witness['raw_exec_records_base64']], [first, last])
            finally:
                trace.finish()

    def test_witness_is_available_before_trace_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            trace = ProcessTrace(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
            try:
                # The drain hook is invoked on a complete OS line, independently
                # of a later timeout/final trace summary.
                trace._observe_product_start(exec_line())
                witness = load_product_start(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
                self.assertEqual(witness['successful_execve']['host_pid'], 123)
                self.assertFalse((output / 'native-process-observation.json').exists())
                self.assertEqual([base64.b64decode(value) for value in witness['raw_exec_records_base64']], [exec_line()])
                self.assertEqual(witness['raw_exec_records_sha256'], [hashlib.sha256(exec_line()).hexdigest()])
            finally:
                trace.finish(interrupted=True)
            self.assertEqual(load_product_start(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)['context'], CONTEXT)

    def test_only_first_match_is_published_and_preexisting_witness_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            trace = ProcessTrace(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
            try:
                trace._observe_product_start(exec_line(pid=123))
                trace._observe_product_start(exec_line(pid=456))
                self.assertEqual(load_product_start(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)['successful_execve']['host_pid'], 123)
            finally:
                trace.finish()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            target = output / PRODUCT_START_NAME
            target.write_bytes(b'original evidence')
            trace = ProcessTrace(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
            try:
                trace._observe_product_start(exec_line())
                self.assertEqual(target.read_bytes(), b'original evidence')
                self.assertIn('product_start_witness_error:FileExistsError', trace.errors)
            finally:
                trace.finish()

    def test_witness_write_failure_keeps_draining_candidate_process(self):
        line = exec_line()
        def fake_tracer(trace, command):
            program = ('import os,sys; fd=int(sys.argv[1]); line=bytes.fromhex(sys.argv[2]); '
                'os.write(fd,line); '
                '[os.write(fd,b"123 100.200000 write(1, \\\"x\\\", 1) = 1\\n") for _ in range(20000)]; '
                'os.write(fd,b"123 100.300000 +++ exited with 0 +++\\n"); print("candidate completed")')
            return [sys.executable, '-c', program, str(trace.write_fd), line.hex()]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ProcessTrace, 'argv', fake_tracer), patch('agentloop.evaluator.process_observation.tempfile.mkstemp', side_effect=OSError('ENOSPC')):
                result = run_observed([], output=Path(directory), context=CONTEXT, cwd='/', env=os.environ.copy(), timeout=5,
                    product_entry=ENTRY, product_executable=NODE)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'candidate completed')
            observation = json.loads((Path(directory) / 'native-process-observation.json').read_text())
            self.assertFalse(observation['complete'])
            self.assertTrue(observation['capture_eof'])
            self.assertIn('product_start_witness_error:OSError', observation['errors'])
            self.assertFalse((Path(directory) / PRODUCT_START_NAME).exists())

    def test_loader_rejects_context_source_pid_hash_and_symlink_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            trace = ProcessTrace(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
            trace._observe_product_start(exec_line())
            trace.finish()
            path = output / PRODUCT_START_NAME
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                load_product_start(output, {'context_id': 'other'}, product_entry=ENTRY, product_executable=NODE)
            with self.assertRaises(ValueError):
                load_product_start(output, CONTEXT, product_entry='/wrong.js', product_executable=NODE)
            for field, value in [('raw_exec_records_sha256', ['0' * 64]), ('observer_source', {}),
                                 ('successful_execve', {'host_pid': 999}), ('raw_exec_records_base64', ['not base64'])]:
                changed = json.loads(original)
                changed[field] = value
                path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_product_start(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)
            path.unlink()
            other = output / 'another.json'
            other.write_bytes(original)
            path.symlink_to(other)
            with self.assertRaises(ValueError):
                load_product_start(output, CONTEXT, product_entry=ENTRY, product_executable=NODE)

    def test_tracer_spawn_without_matching_exec_does_not_prove_product(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ProcessTrace, 'argv', lambda self, command: [sys.executable, '-c', 'print("tracer only")']):
                result = run_observed([], output=Path(directory), context=CONTEXT, cwd='/', env=os.environ.copy(), timeout=5,
                    product_entry=ENTRY, product_executable=NODE)
            self.assertEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / PRODUCT_START_NAME).exists())
            with self.assertRaises(ValueError):
                load_product_start(directory, CONTEXT, product_entry=ENTRY, product_executable=NODE)

    def test_old_run_observed_signature_still_works_without_witness(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ProcessTrace, 'argv', lambda self, command: [sys.executable, '-c', 'print("compatible")']):
                result = run_observed([], output=Path(directory), context={}, cwd='/', env=os.environ.copy(), timeout=5)
            self.assertEqual(result.stdout.strip(), 'compatible')
            self.assertFalse((Path(directory) / PRODUCT_START_NAME).exists())

    def test_actual_bwrap_node_exec_produces_private_witness(self):
        node = Path(__file__).resolve().parents[3] / '.runtime/node-v22.12.0-linux-x64/bin/node'
        if not node.is_file() or not Path('/usr/bin/strace').is_file() or not shutil.which('bwrap'):
            self.skipTest('requires Linux task Node, strace and bwrap')
        from agentloop.evaluator.transport_sandbox import sandbox_command
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, output = root / 'fixture', root / 'private-observation'
            fixture.mkdir()
            output.mkdir()
            entry = fixture / 'cli.js'
            entry.write_text('console.log("real node CLI started")')
            command = sandbox_command([str(node), str(entry)], readonly=(fixture, node.parent.parent), cwd=fixture)
            result = run_observed(command, output=output, context=CONTEXT, cwd=fixture,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, timeout=10,
                product_entry=entry, product_executable=node)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('real node CLI started', result.stdout)
            witness = load_product_start(output, CONTEXT, product_entry=entry, product_executable=node)
            self.assertEqual(witness['successful_execve']['argv'], [str(node), str(entry)])


if __name__ == '__main__':
    unittest.main()
