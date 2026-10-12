from __future__ import annotations
import json
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import control_runtime as runtime
import formal_commands
import launch_formal
import validate_formal_config as validator


class HostControlTests(unittest.TestCase):
    def test_queued_dispatch_rejects_source_input_and_shared_drift(self):
        for target in ('source', 'input', 'shared'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as raw:
                root=Path(raw);sibling=root/'task';sibling.mkdir()
                source=sibling/'program.py';source.write_text('source')
                shared=root/'gate.json';shared.write_text('{}')
                ref={'path':str(shared),'resolved_path':str(shared.resolve()),'sha256':launch_formal.sha256(shared)}
                branch={'task':'task','sibling':str(sibling),'run_dir':str(root/'absent-run'),
                        'sibling_digest':launch_formal.tree_digest(sibling),'input_evidence':{'version':1}}
                current={'version':1}
                if target=='source':source.write_text('changed')
                elif target=='shared':shared.write_text('{"changed":true}')
                else:current={'version':2}
                with patch.object(launch_formal,'branch_input_evidence',return_value=current), \
                     self.assertRaises(ValueError):
                    launch_formal.verify_dispatch_bindings(branch,[ref])
                self.assertFalse((root/'absent-run').exists())

    def test_task_specific_formal_parameters_and_template_agree(self):
        for task in ('claude','aider','openhands','openclaw','codex','ai-scientist',
                     'deepcode','deeptutor','dyad','openwiki'):
            with self.subTest(task=task):
                command=launch_formal.branch_command(task,Path('/task'),Path('<fresh-run-dir>'),
                                                     Path('/credential'),Path('/usr/bin/python3'))
                self.assertEqual(command,validator.command_template(task,Path('/task'),
                                                                    Path('/credential'),Path('/usr/bin/python3')))
                self.assertEqual(command[command.index('--builder-timeout')+1],
                                 '28920' if task=='ai-scientist' else '28800')
                if task in ('claude','dyad'):
                    self.assertIn('--hidden-cases-dir',command)
                if task=='deepcode':self.assertIn('--python',command)
                if task=='openclaw':self.assertIn('--run-hidden',command)

    def test_missing_external_bundle_is_rejected_before_branch_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);harbor=root/'harbor';harbor.write_text('offline executable fixture')
            with patch.object(formal_commands,'HARBOR',harbor), \
                 patch.object(formal_commands,'ISSUED_ROOT',root/'issued'), \
                 self.assertRaisesRegex(ValueError,'missing formal prerequisite'):
                formal_commands.branch_input_evidence('claude',root/'task',root/'run')

    def test_launcher_logs_preserve_fresh_one_stop_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = root / 'runs' / 'deepcode' / 'fresh-run'
            stdout, stderr = launch_formal.branch_log_paths(root / 'launch_control', 'deepcode')
            stdout.write_text('controller log')
            stderr.write_text('')
            self.assertFalse(run.exists())
            self.assertTrue(stdout.is_file())
            with self.assertRaises(FileExistsError):
                launch_formal.branch_log_paths(root / 'launch_control', 'deepcode')

    def test_durable_wait_scheduler_dispatches_all_ten_fresh_directories(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            credential = root / 'credential-fixture'; credential.write_text('not a credential'); credential.chmod(0o600)
            reference = root / 'reference'; reference.write_text('offline reference')
            tasks = {}
            for index in range(10):
                sibling = root / f'task{index}'; (sibling / 'harbor').mkdir(parents=True)
                (sibling / 'harbor/formal_one_stop.py').write_text(
                    'import pathlib,sys\n'
                    'p=pathlib.Path(sys.argv[sys.argv.index("--run-dir")+1])\n'
                    'assert not p.exists(), "one-stop requires a fresh run directory"\n'
                    'p.mkdir(parents=True)\n'
                    '(p/"offline-controller-fixture.json").write_text("{}")\n')
                tasks[f'task{index}'] = sibling
            cfg = SimpleNamespace(CREDENTIAL_FILE=credential, CONTROL_PYTHON=Path(sys.executable),
                FORMAL_ROOT=root/'formal', TASKS=tasks, ALIGNMENT_SNAPSHOT=reference,
                RESULT_JUDGE=reference, CREATE_CODE_JUDGE=reference)
            ready = {'selected_profiles':['codex_xhigh'], 'expected_branch_count':10,
                     'formal_launch_authorized':True, 'formal_evaluation_started':False,
                     'tasks':[{'task':task,'sibling_digest':launch_formal.tree_digest(sibling)}
                              for task,sibling in tasks.items()]}
            with patch.object(launch_formal,'load_config',return_value=cfg), \
                 patch.object(launch_formal,'validate_ready',return_value=ready), \
                 patch.object(launch_formal,'branch_input_evidence',return_value={'offline_fixture':True}), \
                 patch('sys.argv',['launch_formal','--attempt-label','fixture','--batch-size','3','--wait']), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launch_formal.main(),0)
            result=json.loads((cfg.FORMAL_ROOT/'launch_manifest_fixture.json').read_text())
            self.assertEqual(len(result['branches']),10)
            self.assertTrue(all(branch['state']=='completed' for branch in result['branches']))
            self.assertTrue(result['formal_evaluation_started'])
            self.assertEqual(result['failure_count'],0)
            self.assertEqual(result['launch_mode'],'scheduled_wait')
            for branch in result['branches']:
                self.assertTrue((Path(branch['run_dir'])/'offline-controller-fixture.json').is_file())
                self.assertTrue(Path(branch['stdout']).is_relative_to(Path(result['control_dir'])))

    def test_environment_removes_python_injection_not_candidate_environment(self):
        env = {'PYTHONPATH': '/foreign/harbor', 'PYTHONHOME': '/foreign',
               'PYTHONUSERBASE': '/foreign', 'PYTHONSTARTUP': '/foreign',
               'PATH': '/product/runtime/bin', 'MODEL_SETTING': 'unchanged'}
        result = runtime.control_environment(env)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP'):
            self.assertNotIn(key, result)
            self.assertIn(key, env)
        self.assertEqual(result['PATH'], env['PATH'])
        self.assertEqual(result['MODEL_SETTING'], env['MODEL_SETTING'])
        self.assertEqual(result['PYTHONNOUSERSITE'], '1')

    def test_explicit_command_cannot_inherit_launcher_python(self):
        with patch.object(launch_formal.sys, 'executable', '/foreign/venv/bin/python'):
            command = launch_formal.branch_command('openhands', Path('/task'), Path('/run'),
                                                   Path('/secret/env'), Path('/usr/bin/python3'))
        self.assertEqual(command[:4], ['/usr/bin/python3', '-E', '-s', '-B'])
        self.assertIn('--run-formal', command)
        self.assertEqual(command[command.index('--max-dev-rounds') + 1], '10')
        self.assertEqual(command[command.index('--n-concurrent') + 1], '1')

    def test_manifest_template_matches_actual_codex_command(self):
        command = launch_formal.branch_command('codex', Path('/task'), Path('<fresh-run-dir>'),
                                               Path('/secret'), Path('/usr/bin/python3'))
        template = validator.command_template('codex', Path('/task'), Path('/secret'), Path('/usr/bin/python3'))
        self.assertEqual(command, template)
        self.assertNotIn('--run-formal', command)
        self.assertEqual(command[command.index('--benchmark') + 1], '/task')

    def test_relative_python_rejected(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            runtime.control_command(Path('python3'), Path('/task'))

    def test_readiness_commands_are_pinned_and_environment_sanitized(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / 'manifest.json'
            manifest.write_text(json.dumps({'validation': {'valid': True}, 'formal_launch_authorized': True}))
            with patch.object(launch_formal.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')) as run:
                launch_formal.validate_ready(manifest, Path('/usr/bin/python3'))
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.args[0][:4], ['/usr/bin/python3', '-E', '-s', '-B'])
                self.assertEqual(call.kwargs['env']['PYTHONNOUSERSITE'], '1')

    def test_failed_validator_cannot_launch(self):
        with patch.object(launch_formal.subprocess, 'run', side_effect=[
            subprocess.CompletedProcess([], 0, ''), subprocess.CompletedProcess([], 2, 'REPAIR')]):
            with self.assertRaisesRegex(RuntimeError, 'validation failed'):
                launch_formal.validate_ready(Path('/never-created'), Path('/usr/bin/python3'))

    def probe_fixture(self, origin=None, platform='linux'):
        return {'executable': '/test-python', 'version': [3, 10, 12], 'platform': platform,
                'requests_version': 'test', 'harbor_origin': origin, 'harbor_locations': [],
                'isolated_python_environment': True}

    def test_installed_harbor_collision_rejected_before_task_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / 'python'; python.write_text('# executable fixture'); python.chmod(0o700)
            with patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(self.probe_fixture('/site-packages/harbor/__init__.py')), '')) as run:
                with self.assertRaisesRegex(ValueError, 'conflicts'):
                    runtime.probe_control_runtime(python, {'task': Path('/never-run')})
                self.assertEqual(run.call_count, 1)

    def test_actual_task_help_failure_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); python = root / 'python'; python.write_text('# fixture'); python.chmod(0o700)
            task = root / 'task'; (task / 'harbor').mkdir(parents=True)
            (task / 'harbor/formal_one_stop.py').write_text('# source fixture')
            with patch.object(runtime.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(self.probe_fixture()), ''),
                subprocess.CompletedProcess([], 1, '', 'ModuleNotFoundError')]):
                with self.assertRaisesRegex(ValueError, 'actual formal --help'):
                    runtime.probe_control_runtime(python, {'task': task})

    def test_probe_success_is_only_control_plane_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); python = root / 'python'; python.write_text('# fixture'); python.chmod(0o700)
            task = root / 'task'; (task / 'harbor').mkdir(parents=True)
            (task / 'harbor/formal_one_stop.py').write_text('# source fixture')
            with patch.object(runtime.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(self.probe_fixture()), ''),
                subprocess.CompletedProcess([], 0, 'usage: task --help', '')]):
                value = runtime.probe_control_runtime(python, {'task': task})
            self.assertTrue(value['valid'])
            self.assertFalse(value['candidate_environment_validated'])
            self.assertEqual(value['provider_calls'], 0)
            self.assertEqual(len(value['task_cli_checks']), 1)


if __name__ == '__main__':
    unittest.main()
