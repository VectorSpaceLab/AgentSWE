from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentloop.evaluator.broker_observation import (
    ExchangeObservation, ObservationStore, load_observations, MAX_BYTES,
)


def sse(*events, done=False):
    raw = b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events)
    return raw + (b'data: [DONE]\n\n' if done else b'')


def terminal(output=None, status='completed'):
    return {'type': 'response.' + status, 'response': {'id': 'resp_1', 'status': status, 'output': output or []}}


def call_item(arguments='{"cmd":"ls"}'):
    return {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'shell', 'arguments': arguments}


def observe(raw, *, request=b'{}', streaming=True, limit=MAX_BYTES):
    return ExchangeObservation(context_id='ctx', request=request, limit=limit).finish(
        status=200, raw=raw, content_type='text/event-stream' if streaming else 'application/json')


class BrokerObservationTests(unittest.TestCase):
    def test_responses_full_fragment_sequence_has_one_call(self):
        item = call_item()
        value = observe(sse(
            {'type': 'response.output_item.added', 'item': call_item('')},
            {'type': 'response.function_call_arguments.delta', 'item_id': 'fc_1', 'delta': '{"cmd":'},
            {'type': 'response.function_call_arguments.delta', 'item_id': 'fc_1', 'delta': '"ls"}'},
            {'type': 'response.function_call_arguments.done', 'item_id': 'fc_1', 'arguments': item['arguments']},
            {'type': 'response.output_item.done', 'item': item}, terminal([item])))
        self.assertTrue(value['complete'], value['errors'])
        self.assertEqual(len(value['model_tool_calls']), 1)
        call = value['model_tool_calls'][0]
        self.assertEqual((call['call_id'], call['name'], call['arguments']), ('call_1', 'shell', item['arguments']))
        self.assertTrue(call['argument_complete'])

    def test_nonstream_responses_extracts_calls_and_final_text(self):
        message = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '{"result":'}, {'type': 'output_text', 'text': '"actual"}'}]}
        value = observe(json.dumps({'id': 'resp_1', 'status': 'completed', 'output': [call_item(), message]}).encode(), streaming=False)
        self.assertTrue(value['complete'], value['errors'])
        self.assertEqual(value['model_tool_calls'][0]['call_id'], 'call_1')
        self.assertEqual(value['assistant_messages'][0]['text'], '{"result":"actual"}')
        self.assertFalse(value['assistant_messages'][0]['proof_of_tool_execution'])

    def test_candidate_tool_outputs_stay_untrusted(self):
        request = {'messages': [{'role': 'tool', 'tool_call_id': 'call_1', 'content': 'I searched'}],
            'input': [{'type': 'function_call_output', 'call_id': 'call_2', 'output': 'I executed'}]}
        value = observe(sse(terminal()), request=json.dumps(request).encode())
        self.assertTrue(value['complete'])
        self.assertEqual(len(value['candidate_reported_tool_outputs']), 2)
        self.assertTrue(all(item['untrusted'] and item['source'] == 'candidate_request' for item in value['candidate_reported_tool_outputs']))
        self.assertEqual(value['model_tool_calls'], [])

    def test_done_failed_and_null_cannot_forge_completed_response(self):
        for raw in (sse(done=True), sse(terminal(status='failed'), done=True),
                    sse({'type': 'response.completed', 'response': None}),
                    sse({'type': 'response.completed', 'response': {'status': 'completed'}})):
            with self.subTest(raw=raw):
                self.assertFalse(observe(raw)['complete'])
        failed = observe(sse(terminal(status='failed')))
        self.assertTrue(failed['capture_complete'])
        self.assertEqual(failed['model_status'], 'failed')

    def test_terminal_requires_response_id_and_rejects_late_payload(self):
        missing_id = {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}
        self.assertFalse(observe(sse(missing_id))['complete'])
        value = observe(sse(terminal([call_item()]), {'type': 'response.output_item.done', 'item': call_item('late replacement')}))
        self.assertFalse(value['complete'])
        self.assertIn('responses_payload_after_terminal', value['errors'])
        self.assertEqual(value['model_tool_calls'][0]['arguments'], '{"cmd":"ls"}')

    def test_duplicate_json_keys_cannot_overwrite_failure_status(self):
        raw = b'{"id":"resp_1","status":"failed","status":"completed","output":[]}'
        self.assertFalse(observe(raw, streaming=False)['complete'])
        sse_raw = b'data: {"type":"response.completed","response":' + raw + b'}\n\n'
        self.assertFalse(observe(sse_raw)['complete'])
        self.assertFalse(observe(sse(terminal()), request=b'{"input":[],"input":[]}')['complete'])

    def test_chat_done_without_choice_finish_is_incomplete(self):
        value = observe(sse({'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 0, 'id': 'c', 'function': {'name': 'shell', 'arguments': '{"cmd":'}}]}, 'finish_reason': None}]}, done=True))
        self.assertFalse(value['complete'])
        self.assertFalse(value['model_tool_calls'][0]['argument_complete'])

    def test_chat_choice_and_tool_indices_are_independent(self):
        raw = sse({'choices': [
            {'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'c0', 'function': {'name': 'f0', 'arguments': 'A'}}]}},
            {'index': 1, 'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'f1', 'arguments': 'B'}}]}}]},
            {'choices': [
                {'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'C'}}]}, 'finish_reason': 'tool_calls'},
                {'index': 1, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'D'}}]}, 'finish_reason': 'tool_calls'}]}, done=True)
        value = observe(raw)
        self.assertTrue(value['complete'], value['errors'])
        self.assertEqual({call['call_id']: call['arguments'] for call in value['model_tool_calls']}, {'c0': 'AC', 'c1': 'BD'})

    def test_chat_length_is_captured_terminal_not_transport_failure(self):
        value = observe(sse({'choices': [{'index': 0, 'delta': {'content': 'actual partial answer', 'tool_calls': [
            {'index': 0, 'id': 'c', 'function': {'name': 'shell', 'arguments': '{not valid JSON'}}]}, 'finish_reason': 'length'}]}, done=True))
        self.assertTrue(value['capture_complete'], value['errors'])
        self.assertTrue(value['complete'])
        self.assertEqual(value['choice_finish_reasons'], {'0': 'length'})
        self.assertEqual(value['assistant_messages'][0]['text'], 'actual partial answer')
        self.assertFalse(value['model_tool_calls'][0]['argument_complete'])
        self.assertEqual(value['model_tool_calls'][0]['arguments'], '{not valid JSON')

    def test_chat_nonstream_final_text_and_invalid_arguments_remain_actual(self):
        value = observe(json.dumps({'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'final',
            'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'f', 'arguments': 'bad json'}}]}, 'finish_reason': 'tool_calls'}]}).encode(), streaming=False)
        self.assertTrue(value['complete'], value['errors'])
        self.assertEqual(value['assistant_messages'][0]['text'], 'final')
        self.assertEqual(value['model_tool_calls'][0]['arguments'], 'bad json')

    def test_malformed_shapes_never_escape_parser(self):
        for value in ({'choices': None}, {'choices': {}}, {'choices': [None]},
                      {'choices': [{'delta': None, 'finish_reason': 'stop'}]},
                      {'status': 'completed', 'output': None}):
            with self.subTest(value=value):
                result = observe(json.dumps(value).encode(), streaming=False)
                self.assertFalse(result['complete'])
                self.assertTrue(result['errors'])

    def test_multiline_sse_and_arbitrary_chunk_boundaries(self):
        raw = b'event: response.completed\r\ndata: {"type":"response.completed",\r\ndata: "response":{"id":"resp_1","status":"completed","output":[]}}\r\n\r\n'
        exchange = ExchangeObservation(context_id='ctx', request=b'{}')
        for offset in range(0, len(raw), 3):
            exchange.feed_response(raw[offset:offset + 3])
        result = exchange.finish(status=200, content_type='text/event-stream')
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual(result['response_sha256'], hashlib.sha256(raw).hexdigest())

    def test_unterminated_sse_frame_is_incomplete(self):
        value = observe(sse(terminal())[:-1])
        self.assertFalse(value['complete'])
        self.assertIn('unterminated_sse_frame', value['errors'])

    def test_response_limit_counts_and_hashes_all_bytes(self):
        raw = sse(terminal()) + b':' + b'x' * 10000 + b'\n\n'
        exchange = ExchangeObservation(context_id='ctx', request=b'{}', limit=4096)
        for offset in range(0, len(raw), 17):
            exchange.feed_response(raw[offset:offset + 17])
        self.assertLessEqual(len(exchange.response_buffer), 4096)
        value = exchange.finish(status=200, content_type='text/event-stream')
        self.assertFalse(value['complete'])
        self.assertEqual(value['response_bytes'], len(raw))
        self.assertEqual(value['response_sha256'], hashlib.sha256(raw).hexdigest())

    def test_request_outputs_share_exchange_budget(self):
        request = json.dumps({'input': [{'type': 'function_call_output', 'call_id': str(i), 'output': 'x' * 3000} for i in range(2)]}).encode()
        value = observe(sse(terminal()), request=request, limit=4096)
        self.assertFalse(value['complete'])
        self.assertIn('request_bytes_limit_exceeded', value['errors'])
        self.assertLessEqual(len(json.dumps(value).encode()), 4096)

    def test_store_case_count_and_byte_budgets(self):
        store = ObservationStore(None, 'ctx', max_records=2, max_bytes=8192, max_exchange_bytes=4096)
        for _ in range(3):
            store.record(b'{}', json.dumps({'id': 'resp_1', 'status': 'completed', 'output': []}).encode(), status=200, content_type='application/json')
        summary = store.close()
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['stored_records'], 2)
        self.assertEqual(summary['discarded'], 1)
        self.assertLessEqual(summary['stored_bytes'], 8192)
        byte_store = ObservationStore(None, 'ctx', max_bytes=4096, max_exchange_bytes=4096)
        first, second = byte_store.begin(b'{}'), byte_store.begin(b'{}')
        self.assertEqual(second.limit, 0)
        for exchange in (first, second):
            byte_store.record(exchange, sse(terminal()), status=200, content_type='text/event-stream')
        self.assertFalse(byte_store.close()['complete'])

    def test_store_record_io_failure_is_incomplete_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ObservationStore(Path(directory), 'ctx')
            with patch('agentloop.evaluator.broker_observation.os.open', side_effect=OSError('ENOSPC')):
                value = store.record(b'{}', sse(terminal()), status=200, content_type='text/event-stream')
            self.assertFalse(value['complete'])
            self.assertIn('observer_record_error:OSError', value['errors'])
            self.assertFalse(store.close()['complete'])

    def test_partial_storage_failure_keeps_full_case_budget_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ObservationStore(Path(directory), 'ctx', max_bytes=8192, max_exchange_bytes=4096)
            original_fdopen = os.fdopen
            class PartialWrite:
                def __init__(self, fd, mode):
                    self.stream = original_fdopen(fd, mode)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.stream.close()
                def write(self, raw):
                    self.stream.write(raw[:len(raw) // 2])
                    self.stream.flush()
                    raise OSError('partial disk failure')
            with patch('agentloop.evaluator.broker_observation.os.fdopen', side_effect=PartialWrite):
                for _ in range(3):
                    result = store.record(b'{}', sse(terminal()), status=200, content_type='text/event-stream')
                    self.assertFalse(result['complete'])
            summary = store.close()
            self.assertEqual(summary['stored_bytes'], 8192)
            self.assertEqual(summary['discarded'], 1)
            self.assertEqual(len(list(Path(directory).glob('broker-observation-*.json'))), 2)
            self.assertFalse(summary['complete'])

    def test_existing_record_path_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broker-observation-0001.json'
            path.write_bytes(b'original')
            store = ObservationStore(Path(directory), 'ctx')
            value = store.record(b'{}', sse(terminal()), status=200, content_type='text/event-stream')
            self.assertFalse(value['complete'])
            self.assertEqual(path.read_bytes(), b'original')

    def test_close_pending_is_bounded_and_late_record_cannot_change_snapshot(self):
        store = ObservationStore(None, 'ctx')
        exchange = store.begin(b'{}')
        start = time.monotonic()
        summary = store.close(timeout=0.01)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(summary['sealed'])
        self.assertEqual(summary['pending'], 1)
        self.assertFalse(summary['complete'])
        store.record(exchange, sse(terminal()), status=200, content_type='text/event-stream')
        self.assertEqual(summary['records'], [])
        self.assertEqual(store.records, [])

    def test_close_waits_for_already_admitted_exchange(self):
        store = ObservationStore(None, 'ctx')
        exchange = store.begin(b'{}')
        results = []
        worker = threading.Thread(target=lambda: results.append(store.close(timeout=1)))
        worker.start()
        deadline = time.monotonic() + 1
        while not store.closing and time.monotonic() < deadline:
            time.sleep(0.001)
        store.record(exchange, sse(terminal()), status=200, content_type='text/event-stream')
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]['sealed'])
        self.assertEqual(results[0]['stored_records'], 1)
        self.assertTrue(results[0]['complete'], results[0]['errors'])

    def test_close_does_not_block_on_observer_storage_io(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ObservationStore(Path(directory), 'ctx')
            exchange = store.begin(b'{}')
            entered, release = threading.Event(), threading.Event()
            original_open = os.open
            def paused_open(*args):
                entered.set()
                if not release.wait(timeout=1):
                    raise OSError('test storage timeout')
                return original_open(*args)
            with patch('agentloop.evaluator.broker_observation.os.open', side_effect=paused_open):
                worker = threading.Thread(target=lambda: store.record(exchange, sse(terminal()), status=200, content_type='text/event-stream'))
                worker.start()
                self.assertTrue(entered.wait(timeout=1))
                summary = store.close(timeout=0.01)
                self.assertFalse(summary['sealed'])
                self.assertFalse(summary['complete'])
                release.set()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
            self.assertEqual(summary['records'], [])

    def test_exchange_cannot_be_recorded_under_another_context(self):
        first, second = ObservationStore(None, 'ctx1'), ObservationStore(None, 'ctx2')
        exchange = first.begin(b'{}')
        result = second.record(exchange, sse(terminal()), status=200, content_type='text/event-stream')
        self.assertFalse(result['complete'])
        self.assertEqual(second.records, [])
        first.record(exchange, sse(terminal()), status=200, content_type='text/event-stream')
        self.assertTrue(first.close()['complete'])

    def test_hashed_summary_reload_and_tamper_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            store = ObservationStore(output, 'ctx')
            store.record(b'{}', sse(terminal()), status=200, content_type='text/event-stream')
            summary = store.close()
            summary_path = output / 'native-broker-observation.json'
            summary_path.write_text(json.dumps(summary))
            self.assertTrue(load_observations(output, 'ctx')['complete'])
            with self.assertRaises(ValueError):
                load_observations(output, 'other-context')
            path = output / 'broker-observation-0001.json'
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaises(ValueError):
                load_observations(output, 'ctx')
            path.write_bytes(original)
            summary_path.write_text(json.dumps(dict(summary, issued=2, counters={**summary['counters'], 'issued': 2})))
            with self.assertRaisesRegex(ValueError, 'completeness mismatch'):
                load_observations(output, 'ctx')
            summary_path.write_text(json.dumps(dict(summary, stored_bytes=summary['stored_bytes'] + 1,
                counters={**summary['counters'], 'stored_bytes': summary['stored_bytes'] + 1})))
            with self.assertRaisesRegex(ValueError, 'byte accounting mismatch'):
                load_observations(output, 'ctx')
            changed = dict(summary, observer_source={'path': '/wrong', 'sha256': '0' * 64})
            summary_path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                load_observations(output, 'ctx')

    def test_complete_summary_cannot_skip_sequence_one(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            store = ObservationStore(output, 'ctx')
            store.record(b'{}', sse(terminal()), status=200, content_type='text/event-stream')
            summary = store.close()
            record = summary['records'][0]
            record['sequence'] = 2
            record['path'] = str(output / 'broker-observation-0002.json')
            body = {k: v for k, v in record.items() if k not in {'path', 'sha256'}}
            raw = json.dumps(body, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
            Path(record['path']).write_bytes(raw)
            record['sha256'] = hashlib.sha256(raw).hexdigest()
            summary['record_references'] = [{k: record[k] for k in ('sequence', 'path', 'sha256', 'complete')}]
            (output / 'native-broker-observation.json').write_text(json.dumps(summary))
            with self.assertRaisesRegex(ValueError, 'sequence or byte accounting mismatch'):
                load_observations(output, 'ctx')


if __name__ == '__main__':
    unittest.main()
