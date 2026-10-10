"""Synthetic bytes and real localhost HTTP only. Never contact a provider."""
import contextlib
import http.server
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import code_judge_entry
import result_judge as judge
from responses_stream import ResponseEvents, StreamProtocolError, RedactedCapture, read_response
import test_code_judge_entry as code_fixture


def body(status='completed', **changes):
    return {'id': 'resp_synthetic', 'model': judge.MODEL, 'status': status, 'error': None,
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"中文":1}'}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13}, **changes}


def event(kind, *, response=None, sequence=0, newline='\n', **fields):
    data = {'type': kind, 'sequence_number': sequence, **fields}
    if response is not None:
        data['response'] = response
    return ('event: '+kind+newline+'data: '+json.dumps(data, ensure_ascii=False)+newline+newline).encode()


class EventsTests(unittest.TestCase):
    def test_fragmented_utf8_and_all_line_endings(self):
        for newline in ('\n', '\r\n', '\r'):
            raw = event('response.created', response=body('in_progress'), newline=newline)
            raw += event('response.completed', response=body(), sequence=1, newline=newline)
            for size in (1, 7, 65536):
                with self.subTest(newline=repr(newline), size=size):
                    parser = ResponseEvents()
                    for start in range(0, len(raw), size):
                        parser.feed(raw[start:start+size])
                    self.assertEqual(parser.finish(), body())

    def test_completed_incomplete_failed_are_distinct_terminal_objects(self):
        for status in ('completed', 'incomplete', 'failed'):
            parser = ResponseEvents()
            parser.feed(event('response.'+status, response=body(status)))
            self.assertEqual(parser.finish(), body(status))

    def test_deltas_and_done_marker_cannot_manufacture_completion(self):
        for raw in (event('response.output_text.delta', delta='{"score":100}'), b'data: [DONE]\n\n',
                    event('response.output_text.done', text='{}'), event('response.done', response=body())):
            with self.subTest(raw=raw), self.assertRaises(StreamProtocolError):
                parser = ResponseEvents(); parser.feed(raw); parser.finish()

    def test_identity_status_type_and_sequence_mismatches(self):
        cases = [event('response.completed', response=body('incomplete')),
                 event('response.completed', response=body()).replace(b'event: response.completed', b'event: response.failed'),
                 event('response.created', response=body('in_progress'))+event('response.completed', response=body(id='foreign'), sequence=1),
                 event('response.created', response=body('in_progress'))+event('response.completed', response=body(), sequence=0),
                 event('response.completed', response=body(id='')),
                 event('response.completed', response=body(), sequence=True)]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(StreamProtocolError):
                parser = ResponseEvents(); parser.feed(raw); parser.finish()

    def test_duplicate_or_unterminated_event_and_error(self):
        complete = event('response.completed', response=body())
        for raw in (complete+complete, complete[:-1], event('error', message='synthetic'),
                    b'data: {"type":"response.completed","type":"error"}\n\n',
                    b'data: {"type":"response.completed","response":NaN}\n\n', b'data: \xff\n\n'):
            with self.subTest(raw=raw), self.assertRaises(StreamProtocolError):
                parser = ResponseEvents(); parser.feed(raw); parser.finish()

    def test_comments_bom_multiline_data_and_done(self):
        value = json.dumps({'type': 'response.completed', 'response': body()})
        raw = ('\ufeff: keepalive\r\nretry: 200\r\nid: synthetic\r\n\r\n'
               'data: '+value[:1]+'\r\ndata: '+value[1:]+'\r\n\r\ndata: [DONE]\r\n\r\n').encode()
        parser = ResponseEvents(); parser.feed(raw)
        self.assertEqual(parser.finish(), body())

    def test_limit_and_raw_capture_before_parse_failure(self):
        parser = ResponseEvents(max_bytes=3)
        with self.assertRaises(StreamProtocolError): parser.feed(b'abcd')
        received = []
        raw = b'data: garbage\n\n'
        with self.assertRaises(StreamProtocolError):
            read_response(io.BytesIO(raw).read1, content_type='text/event-stream', is_success=True, capture=received.append)
        self.assertEqual(b''.join(received), raw)

    def test_terminal_does_not_wait_for_eof_or_partial_done_trailer(self):
        read = Mock(side_effect=[event('response.completed', response=body())+b'data: [DO', AssertionError('unnecessary read')])
        _, result = read_response(read, content_type='text/event-stream; charset=utf-8', is_success=True)
        self.assertEqual(result, body()); self.assertEqual(read.call_count, 1)

    def test_redaction_across_every_chunk_boundary(self):
        secret = 'synthetic-only-secret-XYZ'
        raw = ('before '+secret+' between '+secret+' after').encode()
        for size in range(1, len(secret)+2):
            handle = io.BytesIO(); capture = RedactedCapture(handle, secret)
            for start in range(0, len(raw), size): capture.write(raw[start:start+size])
            capture.finish()
            self.assertEqual(handle.getvalue(), raw.replace(secret.encode(), b'[REDACTED]'))


class HTTP(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_): pass
    def do_POST(self):
        self.server.calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        mode = self.server.mode
        try:
            if mode == '524':
                self.send_response(524); self.send_header('Content-Length', '0'); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close'); self.end_headers()
            self.wfile.write(event('response.created', response=body('in_progress'))); self.wfile.flush()
            if mode == 'drop':
                self.connection.shutdown(socket.SHUT_RDWR); return
            if mode == 'trickle':
                until = time.monotonic()+1
                while time.monotonic() < until:
                    self.wfile.write(b': heartbeat\n\n'); self.wfile.flush(); time.sleep(.02)
                return
            response = body('incomplete') if mode == 'incomplete' else body()
            if mode == 'wrong_model': response['model'] = 'wrong'
            if mode == 'echo': response['output'][0]['content'][0]['text'] = 'synthetic-api-key'
            self.wfile.write(event('response.'+response['status'], response=response, sequence=1)); self.wfile.flush()
            if mode == 'terminal_without_eof': time.sleep(1)
        except OSError:
            pass
        finally:
            self.close_connection = True


@contextlib.contextmanager
def server(mode):
    serving = http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTP)
    serving.daemon_threads = True; serving.mode = mode; serving.calls = []
    thread = threading.Thread(target=serving.serve_forever, kwargs={'poll_interval': .01}, daemon=True); thread.start()
    try: yield serving, f'http://127.0.0.1:{serving.server_port}/v1/responses'
    finally: serving.shutdown(); serving.server_close(); thread.join(2)


class DirectHTTPTests(unittest.TestCase):
    def call(self, endpoint, path, timeout=2):
        return judge.call_judge('synthetic', 'synthetic-api-key', timeout, 4,
            endpoint=endpoint, response_path=path, transport_mode='stream')

    def test_stream_wire_flag_terminal_usage_capture_and_no_eof_wait(self):
        with tempfile.TemporaryDirectory() as directory, server('terminal_without_eof') as (http, endpoint):
            path = Path(directory)/'response.json'; started = time.monotonic()
            result = self.call(endpoint, path)
            self.assertLess(time.monotonic()-started, .8)
            self.assertEqual(json.loads(result[0]), {'中文': 1}); self.assertEqual(result[4]['total_tokens'], 13)
            self.assertEqual(len(http.calls), 1); self.assertIs(http.calls[0]['stream'], True)
            self.assertEqual(http.calls[0]['model'], judge.MODEL)
            self.assertEqual(http.calls[0]['reasoning'], {'effort': 'max'})
            self.assertEqual(http.calls[0]['max_output_tokens'], judge.MAX_OUTPUT_TOKENS)
            ledger = json.loads(path.with_name('response-attempts.json').read_text())
            self.assertTrue(ledger['attempts'][0]['usage_known'])
            self.assertIn(b'response.completed', path.with_name('response-attempt-001.raw').read_bytes())

    def test_incomplete_wrong_model_drop_524_and_deadline_never_reissue(self):
        for mode in ('incomplete', 'wrong_model', 'drop', '524', 'trickle'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, server(mode) as (http, endpoint):
                path = Path(directory)/'response.json'
                with self.assertRaises(judge.TransportFailure): self.call(endpoint, path, .2 if mode == 'trickle' else 2)
                self.assertEqual(len(http.calls), 1)
                ledger = json.loads(path.with_name('response-attempts.json').read_text())
                self.assertEqual(len(ledger['attempts']), 1)
                self.assertEqual(ledger['attempts'][0]['state'], 'terminal')
                self.assertEqual(ledger['attempts'][0]['usage_known'], mode in ('incomplete', 'wrong_model'))

    def test_echo_is_redacted_in_raw_and_parsed_outputs(self):
        with tempfile.TemporaryDirectory() as directory, server('echo') as (http, endpoint):
            path = Path(directory)/'response.json'; result = self.call(endpoint, path)
            self.assertNotIn('synthetic-api-key', result[0])
            self.assertTrue(all(b'synthetic-api-key' not in p.read_bytes() for p in Path(directory).iterdir()))

    def test_code_adapter_forwards_selected_mode(self):
        for mode in ('stream', 'nonstream'):
            with tempfile.TemporaryDirectory() as directory:
                create = code_fixture.SingleLogicalCodeRequest().create()
                caller = Mock(return_value=('{}', 1, 'endpoint', ['endpoint'], body()['usage']))
                ledger = code_judge_entry.install_transport(create, Path(directory), caller=caller)
                create.call_judge('synthetic', 'placeholder', 900, transport_mode=mode)
                self.assertEqual(caller.call_args.kwargs['transport_mode'], mode)
                self.assertEqual(ledger['transport_mode'], mode)


if __name__ == '__main__': unittest.main()
