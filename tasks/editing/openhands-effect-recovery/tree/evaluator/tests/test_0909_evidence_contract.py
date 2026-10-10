import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evaluator'))
sys.path.insert(0, str(ROOT / 'lower_agent'))
import case_evidence
import isolated_runtime
import openhands_lower_agent as lower


class EvidenceContractTests(unittest.TestCase):
    def test_changed_inputs_rejected_before_any_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_evidence.immutable_bundle(root, {'native.json': {'old': True}, 'oracle.json': {'old': True}})
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with self.assertRaisesRegex(ValueError, 'new score directory'):
                case_evidence.immutable_bundle(root, {'missing.json': {}, 'native.json': {'old': True}, 'oracle.json': {'new': True}})
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)

    def test_completed_invalid_model_json_is_captured_without_resampling(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp) / 'raw.txt'
            response = io.BytesIO(json.dumps({'id': 'synthetic-completed-invalid-json', 'status': 'completed', 'output_text': 'invalid final JSON'}).encode())
            with patch('urllib.request.urlopen', return_value=response) as transport:
                with self.assertRaises(json.JSONDecodeError):
                    isolated_runtime.model_json(lower, 'http://localhost.invalid', 'fixture', 'test_001', 'artifact', lambda: 10, capture)
            self.assertEqual(capture.read_text(), 'invalid final JSON')
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(json.loads(Path(str(capture) + '.request.json').read_text())['state'], 'completed')
            with patch('urllib.request.urlopen') as retried:
                with self.assertRaises(FileExistsError):
                    isolated_runtime.model_json(lower, 'http://localhost.invalid', 'fixture', 'test_001', 'artifact', lambda: 10, capture)
                retried.assert_not_called()

    def test_failed_cleanup_query_cannot_claim_absence(self):
        from subprocess import CompletedProcess
        with patch('subprocess.run', return_value=CompletedProcess([], 1, '', 'daemon unavailable')):
            self.assertFalse(isolated_runtime.cleanup_container('agentswe-0909-oh-synthetic')['container_absent'])

    def test_budget_greater_than_create_rejected_before_execution(self):
        with patch('sys.argv', ['test', '--repository', '/unused', '--case', '/unused', '--broker-endpoint', 'http://unused', '--output', '/unused', '--timeout', '601']):
            with self.assertRaisesRegex(ValueError, '600'):
                isolated_runtime.main(lower)

    def test_dev_world_uses_actual_scenario_bytes(self):
        source = (ROOT / 'lower_agent/isolated_runtime.py').read_text()
        self.assertIn('fixture_path = case / "assets/scenario.json"', source)
        for case in ('dev_001', 'dev_002'):
            self.assertTrue((ROOT / 'dev_cases' / case / 'assets/scenario.json').is_file())
            self.assertNotIn('solution.patch', (ROOT / 'dev_cases' / case / 'natural_task.md').read_text())


if __name__ == '__main__': unittest.main()
