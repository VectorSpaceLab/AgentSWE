import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import code_judge_entry as entry
import result_judge as transport


class JudgeTransportError(RuntimeError):
    def __init__(self, message, attempts, endpoints):
        super().__init__(message)


class SingleLogicalCodeRequest(unittest.TestCase):
    def create(self):
        return SimpleNamespace(ENDPOINTS=('http://test.invalid/v1/responses',),
            MODEL='deepseek-flash', REASONING_EFFORT='max', MAX_ATTEMPTS=4,
            JudgeTransportError=JudgeTransportError)

    def test_completed_invalid_output_cannot_trigger_second_provider_request(self):
        with tempfile.TemporaryDirectory() as directory:
            caller = Mock(return_value=('not valid score JSON', 1, 'endpoint', ['endpoint'],
                                         {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}))
            create = self.create()
            ledger = entry.install_transport(create, Path(directory), caller=caller)
            self.assertEqual(create.call_judge('prompt', 'placeholder', 900)[0], 'not valid score JSON')
            with self.assertRaisesRegex(RuntimeError, 'second logical'):
                create.call_judge('format repair', 'placeholder', 900)
            self.assertEqual(caller.call_count, 1)
            self.assertEqual(ledger['completed_responses'], 1)
            self.assertEqual(ledger['total_tokens'], 30)

    def test_uncertain_delivery_cannot_be_reissued_by_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            caller = Mock(side_effect=transport.TransportFailure('read timeout', 1, ['endpoint']))
            create = self.create()
            entry.install_transport(create, Path(directory), caller=caller)
            with self.assertRaises(JudgeTransportError):
                create.call_judge('prompt', 'placeholder', 900)
            ledger = json.loads((Path(directory) / 'code_transport_ledger.json').read_text())
            self.assertIsNone(ledger['total_tokens'])
            self.assertEqual(ledger['unknown_usage_attempts'], 1)
            self.assertFalse(ledger['usage_complete'])
            restarted = self.create()
            entry.install_transport(restarted, Path(directory), caller=caller)
            with self.assertRaises(FileExistsError):
                restarted.call_judge('prompt', 'placeholder', 900)
            self.assertEqual(caller.call_count, 1)

    def test_invalid_delivered_envelope_keeps_actual_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            body = {'status': 'completed', 'usage': {'input_tokens': 8, 'output_tokens': 2, 'total_tokens': 10}}
            caller = Mock(side_effect=transport.ReceivedResponseFailure('wrong model', 1, ['endpoint'], body))
            create = self.create()
            entry.install_transport(create, Path(directory), caller=caller)
            with self.assertRaises(JudgeTransportError):
                create.call_judge('prompt', 'placeholder', 900)
            ledger = json.loads((Path(directory) / 'code_transport_ledger.json').read_text())
            self.assertEqual(ledger['completed_responses'], 1)
            self.assertEqual(ledger['total_tokens'], 10)

    def test_crash_after_call_intent_leaves_unknown_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            create = self.create()
            entry.install_transport(create, Path(directory), caller=Mock(side_effect=RuntimeError('unexpected failure')))
            with self.assertRaisesRegex(RuntimeError, 'unexpected failure'):
                create.call_judge('prompt', 'placeholder', 900)
            ledger = json.loads((Path(directory) / 'code_transport_ledger.json').read_text())
            self.assertIsNone(ledger['total_tokens'])
            self.assertIsNone(ledger['transport_attempts'])
            self.assertIsNone(ledger['unknown_usage_attempts'])


if __name__ == '__main__':
    unittest.main()
