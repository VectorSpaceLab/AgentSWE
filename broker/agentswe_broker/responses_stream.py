"""Bounded Responses SSE decoding; never construct an answer from text deltas.

Protocol reference: https://developers.openai.com/api/docs/guides/streaming-responses
No networking, credentials, retries, model selection, or scoring in this module.
The caller owns its absolute deadline and must preserve uncertain delivery.
"""
from __future__ import annotations

import json
import http.client
import os
from functools import partial
import ssl
import urllib.request
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
TERMINAL = {'response.completed': 'completed', 'response.incomplete': 'incomplete',
            'response.failed': 'failed'}


class SafeConnectTimeout(TimeoutError):
    """Connection/TLS setup failed before any HTTP request could be sent."""


class _ConnectionBudget:
    def __init__(self, *args, on_request_start=None, **kwargs):
        self._on_request_start = on_request_start
        super().__init__(*args, **kwargs)

    def connect(self):
        budget = self.timeout
        self.timeout = min(30, budget)
        self._connection_setup_in_progress = True
        try:
            super().connect()
        except TimeoutError as exc:
            raise SafeConnectTimeout('connection establishment timed out') from exc
        finally:
            self.timeout = budget
            self._connection_setup_in_progress = False
        self.sock.settimeout(budget)

    def send(self, data):
        # Establish TCP/TLS before recording a possible HTTP submission. Once
        # sendall may start, failures are conservatively delivery-unknown.
        if self.sock is None:
            if not self.auto_open:
                raise http.client.NotConnected()
            self.connect()
        # HTTPS proxy CONNECT and TLS are connection setup, not a model POST.
        if self._on_request_start is not None and not getattr(self, '_connection_setup_in_progress', False):
            callback, self._on_request_start = self._on_request_start, None
            callback()
        return super().send(data)


class BudgetHTTPConnection(_ConnectionBudget, http.client.HTTPConnection):
    pass


class BudgetHTTPSConnection(_ConnectionBudget, http.client.HTTPSConnection):
    pass


class _HTTP(urllib.request.HTTPHandler):
    def __init__(self, on_request_start=None):
        super().__init__()
        self.on_request_start = on_request_start

    def http_open(self, req):
        return self.do_open(partial(BudgetHTTPConnection,
            on_request_start=self.on_request_start), req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, on_request_start=None):
        # SSLContext owns certificate and hostname verification on all pinned
        # Python versions. Python 3.12 removed HTTPSHandler._check_hostname.
        super().__init__(context=ssl.create_default_context())
        self.on_request_start = on_request_start

    def https_open(self, req):
        return self.do_open(partial(BudgetHTTPSConnection,
            on_request_start=self.on_request_start), req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def evaluator_proxy_url(value=None):
    """Only an explicit evaluator-owned loopback HTTP proxy is accepted."""
    value = os.environ.get('AGENTSWE_EVALUATOR_PROXY_URL', '') if value is None else value
    if not value:
        return ''
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1')
            or parsed.username or parsed.password or not parsed.port
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Invalid evaluator-owned loopback proxy URL')
    return value.rstrip('/')


def direct_opener(*, on_request_start=None, proxy_url=None):
    """Explicit HTTPS proxy only; no ambient proxy, redirect or automatic retry."""
    proxy = evaluator_proxy_url(proxy_url)
    return urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy} if proxy else {}), _NoRedirect(),
        _HTTP(on_request_start), _HTTPS(on_request_start))


class StreamProtocolError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StreamProtocolError('duplicate JSON key')
        result[key] = value
    return result


def strict_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(StreamProtocolError('non-finite JSON')))


class ResponseEvents:
    def __init__(self, *, max_bytes=MAX_RESPONSE_BYTES):
        self.max_bytes, self.bytes_seen = max_bytes, 0
        self.pending = bytearray()
        self.data, self.event = [], None
        self.response_id, self.sequence = None, None
        self.response, self.terminal_type = None, None
        self.event_count = 0
        self.done_marker = False
        self.first_line = True

    def feed(self, chunk):
        self.bytes_seen += len(chunk)
        if self.bytes_seen > self.max_bytes:
            raise StreamProtocolError('response byte limit')
        self.pending.extend(chunk)
        # SSE accepts LF, CRLF and CR. Keep a trailing CR until the next chunk
        # so CRLF split across reads cannot create a spurious empty event.
        while self.pending:
            lf, cr = self.pending.find(b'\n'), self.pending.find(b'\r')
            positions = [p for p in (lf, cr) if p >= 0]
            if not positions:
                break
            pos = min(positions)
            if self.pending[pos] == 13 and pos == len(self.pending)-1:
                break
            size = 2 if self.pending[pos:pos+2] == b'\r\n' else 1
            line = bytes(self.pending[:pos])
            del self.pending[:pos+size]
            self._line(line)

    def _line(self, raw):
        try:
            line = raw.decode('utf-8')
        except UnicodeError as exc:
            raise StreamProtocolError('invalid UTF-8 event') from exc
        if self.first_line:
            line = line.removeprefix('\ufeff')
            self.first_line = False
        if not line:
            self._dispatch()
            return
        if line.startswith(':'):
            return
        field, _, value = line.partition(':')
        if value.startswith(' '):
            value = value[1:]
        if field == 'data':
            self.data.append(value)
        elif field == 'event':
            if self.event is not None:
                raise StreamProtocolError('duplicate SSE event field')
            self.event = value
        # id/retry/unknown SSE fields do not contain an answer.

    def _dispatch(self):
        data, declared = '\n'.join(self.data), self.event
        self.data, self.event = [], None
        if not data:
            return
        if data == '[DONE]':
            if self.response is None or self.done_marker:
                raise StreamProtocolError('missing terminal or duplicate DONE')
            self.done_marker = True
            return
        if self.response is not None:
            raise StreamProtocolError('event after terminal response')
        try:
            value = strict_json(data)
        except (ValueError, UnicodeError) as exc:
            raise StreamProtocolError('invalid JSON event') from exc
        if not isinstance(value, dict) or not isinstance(value.get('type'), str):
            raise StreamProtocolError('missing typed event')
        kind = value['type']
        if declared is not None and declared != kind:
            raise StreamProtocolError('SSE name and event type disagree')
        if 'sequence_number' in value:
            sequence = value['sequence_number']
            if type(sequence) is not int or sequence < 0 or (self.sequence is not None and sequence <= self.sequence):
                raise StreamProtocolError('invalid event sequence')
            self.sequence = sequence
        self.event_count += 1
        if kind == 'error':
            raise StreamProtocolError('provider error event')
        if kind not in TERMINAL and kind not in ('response.created', 'response.in_progress'):
            return  # Neither deltas nor output_text.done establish completion.
        response = value.get('response')
        if not isinstance(response, dict) or not isinstance(response.get('id'), str) or not response['id']:
            raise StreamProtocolError('missing response identity')
        if self.response_id is not None and response['id'] != self.response_id:
            raise StreamProtocolError('response identity changed')
        self.response_id = response['id']
        if kind in TERMINAL:
            if response.get('status') != TERMINAL[kind]:
                raise StreamProtocolError('terminal type/status mismatch')
            self.response, self.terminal_type = response, kind

    def finish(self):
        # A final CR is a real SSE line terminator, not an unterminated line.
        if self.pending.endswith(b'\r'):
            line = bytes(self.pending[:-1])
            self.pending.clear()
            self._line(line)
        if self.pending.strip() or self.data or self.event is not None:
            raise StreamProtocolError('unterminated event')
        if self.response is None:
            raise StreamProtocolError('no terminal response; delivery and usage uncertain')
        return self.response


def read_response(read_chunk, *, content_type, is_success, capture=None,
                  max_bytes=MAX_RESPONSE_BYTES):
    """Read until a complete typed terminal or HTTP EOF, within caller deadline.

    `read_chunk` must deliver available bytes without waiting for the complete
    HTTP body (e.g. HTTPResponse.read1). `capture` receives raw bytes before
    parsing, including malformed or incomplete events. It must redact secrets.
    JSON fallback is explicit for compatible gateways returning final JSON.
    """
    stream = is_success and content_type.split(';', 1)[0].strip().lower() == 'text/event-stream'
    decoder = ResponseEvents(max_bytes=max_bytes) if stream else None
    raw = bytearray()
    while True:
        chunk = read_chunk(65536)
        if not chunk:
            break
        if capture is not None:
            capture(chunk)
        if len(raw) + len(chunk) > max_bytes:
            raise StreamProtocolError('response byte limit')
        raw.extend(chunk)
        if decoder is not None:
            decoder.feed(chunk)
            if decoder.response is not None:
                # Completion, not a socket close, defines the model response.
                # A partial trailing comment/[DONE] may share the final read.
                # We do not wait for a trailer once the terminal is complete.
                return bytes(raw), decoder.response
    return bytes(raw), decoder.finish() if decoder is not None else None


class RedactedCapture:
    """Persist raw bytes without allowing a key split across chunks to escape."""
    def __init__(self, handle, key):
        self.handle, self.key, self.pending = handle, key.encode(), b''
        if not self.key:
            raise ValueError('empty redaction key')

    def write(self, chunk):
        self.pending += chunk
        # Keep the suffix that could start a key in the next chunk. Replace
        # complete keys before selecting a safe flush boundary.
        self.pending = self.pending.replace(self.key, b'[REDACTED]')
        keep = min(len(self.key)-1, len(self.pending))
        boundary = len(self.pending)-keep
        self.handle.write(self.pending[:boundary]); self.handle.flush()
        self.pending = self.pending[boundary:]

    def finish(self):
        self.handle.write(self.pending.replace(self.key, b'[REDACTED]'))
        self.handle.flush(); self.pending = b''
