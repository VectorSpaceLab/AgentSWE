"""Complete broker exchanges can still exceed the shared judge input limit."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agentloop.evaluator.broker_observation import ObservationStore
from agentloop.evaluator.execution_evidence import broker_trajectory, judge_input_sizes


class JudgeInputLimitTests(unittest.TestCase):
    def test_complete_exchanges_remain_intact_when_combined_trajectory_is_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            store = ObservationStore(output, 'case-context')
            for index in range(6):
                exchange = store.begin(json.dumps({'input': 'task-' + str(index)}).encode())
                response = {'id': 'response-' + str(index), 'object': 'response',
                    'status': 'completed', 'output': [{'type': 'function_call',
                        'id': 'item-' + str(index), 'call_id': 'call-' + str(index),
                        'name': 'execute', 'arguments': json.dumps({'command': str(index) * (512 * 1024)})}]}
                exchange.feed_response(json.dumps(response).encode())
                record = store.record(exchange, status=200, content_type='application/json')
                self.assertTrue(record['complete'], record.get('errors'))
            summary = store.close()
            self.assertTrue(summary['complete'], summary.get('errors'))
            trajectory = output / 'observed_trajectory.json'
            trajectory.write_text(json.dumps(broker_trajectory(summary)))
            original = trajectory.read_bytes()
            original_hash = hashlib.sha256(original).hexdigest()
            self.assertGreater(len(original), 2_500_000)
            sizes = judge_input_sizes(output)
            self.assertFalse(sizes['valid'])
            self.assertFalse(sizes['inputs']['trajectory']['within_limit'])
            self.assertEqual(sizes['inputs']['trajectory']['bytes'], len(original))
            self.assertFalse(sizes['evidence_truncated'])
            self.assertEqual(hashlib.sha256(trajectory.read_bytes()).hexdigest(), original_hash)
            self.assertEqual(len(json.loads(trajectory.read_text())['exchanges']), 6)


if __name__ == '__main__':
    unittest.main()
