"""Actual pinned product container stdin regression; no provider or Candidate repair."""
import json
import tempfile
import unittest
from pathlib import Path
from agentloop.evaluator.lower_agent_launcher import invoke, invoke_inspector


class ProductStdinTests(unittest.TestCase):
    def test_hook_event_reaches_actual_docker_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin, workspace = root / 'plugin', root / 'workspace'
            (plugin / 'hooks').mkdir(parents=True)
            workspace.mkdir()
            (plugin / 'hooks/policy_hook.py').write_text('import json,sys; v=json.load(sys.stdin); print(json.dumps({"observed_event":v}))\n')
            event = {'hook_event_name': 'PreToolUse', 'tool_name': 'Read',
                     'tool_input': {'file_path': 'src/app.py'}, 'tool_use_id': 'stdin-test'}
            result = invoke(plugin, workspace, workspace / '.agentloop-state', event)
            self.assertEqual(result['observed_event'], {**event, 'cwd': '/workspace'})

    def test_inspector_checkpoint_stdin_reaches_actual_docker_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin, workspace = root / 'plugin', root / 'workspace'
            (plugin / 'bin').mkdir(parents=True)
            workspace.mkdir()
            inspector = plugin / 'bin/policy-ledger-inspect'
            inspector.write_text('#!/usr/bin/env python3\nimport json,sys; print(json.dumps(json.load(sys.stdin)))\n')
            inspector.chmod(0o755)
            value = {'checkpoint': 'task-local-public-transport-fixture'}
            rc, stdout, stderr = invoke_inspector(plugin, workspace, workspace / '.agentloop-state', ['--import'], value)
            self.assertEqual(rc, 0, stderr)
            self.assertEqual(json.loads(stdout), value)


if __name__ == '__main__':
    unittest.main()
