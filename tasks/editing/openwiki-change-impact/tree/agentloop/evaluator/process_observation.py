"""Bounded, evaluator-owned OS evidence from outside the product namespace."""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import tempfile

CAPTURE_BYTES = 1024 * 1024
LINE_BYTES = 2 * 1024 * 1024
OUTPUT_BYTES = 1024 * 1024
HEADER = re.compile(rb'^(\d+)\s+\d+\.\d+\s+(.*)')
WRITE = re.compile(rb'^write(?:v)?\((\d+),')
EXIT = re.compile(rb'^\+\+\+ (?:exited with \d+|killed by \w+(?: \(core dumped\))?) \+\+\+$')
PRODUCT_START_NAME = 'native-product-start.json'


def _c_string(raw, offset):
    """Decode one complete strace C string; abbreviations are not strings."""
    if raw[offset:offset + 1] != b'"':
        raise ValueError('expected a complete strace string')
    result = bytearray()
    offset += 1
    escapes = {ord('a'): 7, ord('b'): 8, ord('t'): 9, ord('n'): 10,
        ord('v'): 11, ord('f'): 12, ord('r'): 13, ord('"'): 34, ord('\\'): 92}
    while offset < len(raw):
        char = raw[offset]
        offset += 1
        if char == 34:
            return bytes(result), offset
        if char == 92:
            if offset >= len(raw):
                raise ValueError('unterminated strace escape')
            escape = raw[offset]
            offset += 1
            if escape in escapes:
                result.append(escapes[escape])
            elif 48 <= escape <= 55:
                digits = bytes([escape])
                while len(digits) < 3 and offset < len(raw) and 48 <= raw[offset] <= 55:
                    digits += raw[offset:offset + 1]
                    offset += 1
                value = int(digits, 8)
                if value > 255:
                    raise ValueError('strace octal escape exceeds byte')
                result.append(value)
            elif escape == ord('x') and re.fullmatch(rb'[0-9a-fA-F]{2}', raw[offset:offset + 2]):
                result.append(int(raw[offset:offset + 2], 16))
                offset += 2
            else:
                raise ValueError('unsupported strace escape')
        elif char < 32 or char == 127:
            raise ValueError('unescaped control character in strace string')
        else:
            result.append(char)
    raise ValueError('unterminated strace string')


def _successful_product_exec(line, product_executable, product_entry):
    """Accept a full successful execve, including two exact initial argv values."""
    if not line.endswith(b'\n') or len(line) > LINE_BYTES:
        return None
    match = HEADER.fullmatch(line[:-1])
    if not match:
        return None
    pid, body = match.groups()
    if int(pid) <= 0 or not body.startswith(b'execve('):
        return None
    try:
        executable, offset = _c_string(body, len(b'execve('))
        if body[offset:offset + 3] != b', [':
            return None
        offset += 3
        argv = []
        while body[offset:offset + 1] != b']':
            argument, offset = _c_string(body, offset)
            argv.append(argument)
            if body[offset:offset + 2] == b', ':
                offset += 2
                if body[offset:offset + 1] != b'"':
                    return None
            elif body[offset:offset + 1] != b']':
                return None
        # The environment is represented by strace's pointer/count, not its
        # values. Argument abbreviations and unfinished/resumed records fail.
        if not re.fullmatch(rb'\], (?:0x[0-9a-fA-F]+|NULL)(?: /\* \d+ vars \*/)?\)\s+= 0', body[offset:]):
            return None
        expected_executable, expected_entry = os.fsencode(product_executable), os.fsencode(product_entry)
        if executable != expected_executable or len(argv) < 2 or argv[:2] != [expected_executable, expected_entry]:
            return None
        if any(b'\0' in value for value in [executable, *argv]):
            return None
        return {'host_pid': int(pid), 'executable': os.fsdecode(executable),
            'argv': [os.fsdecode(value) for value in argv]}
    except (ValueError, IndexError):
        return None


def _successful_product_exec_records(records, product_executable, product_entry):
    """Validate either one full line or an exact same-PID unfinished/resumed pair.

    The joined bytes below are only parser input. Evidence always retains the
    two original strace records, including their separate host timestamps.
    """
    if len(records) == 1:
        return _successful_product_exec(records[0], product_executable, product_entry)
    if len(records) != 2 or any(not line.endswith(b'\n') or len(line) > LINE_BYTES for line in records):
        return None
    first, last = (HEADER.fullmatch(line[:-1]) for line in records)
    if first is None or last is None or first.group(1) != last.group(1):
        return None
    suffix = b' <unfinished ...>\n'
    if not records[0].endswith(suffix) or last.group(2) != b'<... execve resumed>) = 0':
        return None
    parser_input = records[0][:-len(suffix)] + b') = 0\n'
    return _successful_product_exec(parser_input, product_executable, product_entry)


def file_ref(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('observation reference is not a regular file')
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class ProcessTrace:
    """Drain strace through a private pipe without limiting Candidate writes.

    Only process syscalls and writes to stdout/stderr are retained. Other file
    descriptors include the lower relay socket and are deliberately excluded.
    Storage overflow is explicit; draining continues so tracing cannot deadlock.
    """
    def __init__(self, output, context, *, max_bytes=CAPTURE_BYTES,
                 product_entry=None, product_executable=None):
        if (product_entry is None) != (product_executable is None):
            raise ValueError('both product entry and executable are required for a start witness')
        if product_entry is not None and not all(Path(p).is_absolute() for p in (product_entry, product_executable)):
            raise ValueError('product start witness requires absolute paths')
        self.output = Path(output)
        self.context = context
        self.max_bytes = max_bytes
        self.product_entry = str(product_entry) if product_entry is not None else None
        self.product_executable = str(product_executable) if product_executable is not None else None
        self.product_start = None
        self.product_start_attempted = False
        self.pending_product_exec = {}
        self.pending_product_exec_bytes = 0
        self.path = self.output / 'native-process.trace'
        # Reserve the evaluator-owned trace before any Candidate process is
        # started.  A storage/setup failure must fail before spawn, rather than
        # closing a live strace pipe after the Candidate has begun.
        self.dest_fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self.read_fd, self.write_fd = os.pipe()
        except Exception:
            os.close(self.dest_fd)
            raise
        self.stored = 0
        self.observed = 0
        self.dropped = 0
        self.errors = []
        self.pids = set()
        self.terminated = set()
        self.exec_count = 0
        self.eof = False
        self.pending_write = {}
        self.storage_failed = False
        self._stop_drain = threading.Event()
        self._write_lock = threading.Lock()
        self.ready = threading.Event()
        self.ready_error = None
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def argv(self, command):
        return ['/usr/bin/strace', '-f', '-ttt', '--decode-pids=pidns',
                '-s', '65536', '-e', 'trace=process,execve,write,writev',
                '-o', '/dev/fd/' + str(self.write_fd), '--', *command]

    def _retain(self, line):
        match = HEADER.match(line.rstrip(b'\n'))
        if not match:
            if 'unparsed trace record' not in self.errors:
                self.errors.append('unparsed trace record')
            return True
        pid, body = match.groups()
        self.pids.add(pid)
        if EXIT.fullmatch(body):
            self.terminated.add(pid)
        if body.startswith(b'execve(') and body.endswith(b'= 0'):
            self.exec_count += 1
        write = WRITE.match(body)
        if write:
            retain = write.group(1) in (b'1', b'2')
            if body.endswith(b'<unfinished ...>'):
                self.pending_write[pid] = retain
            return retain
        if body.startswith((b'<... write resumed>', b'<... writev resumed>')):
            return self.pending_write.pop(pid, False)
        return True

    def _observe_product_start(self, line):
        if self.product_entry is None or self.product_start_attempted or self._stop_drain.is_set():
            return
        match = HEADER.fullmatch(line[:-1]) if line.endswith(b'\n') else None
        if match is None:
            return
        pid = match.group(1)
        records = [line]
        previous = self.pending_product_exec.pop(pid, None)
        if previous is not None:
            self.pending_product_exec_bytes -= len(previous)
            records = [previous, line]
        observed = _successful_product_exec_records(records, self.product_executable, self.product_entry)
        if observed is None:
            # Cache only a complete argument/environment prefix which could
            # describe the exact target exec. Never accept the prefix alone.
            suffix = b' <unfinished ...>\n'
            if line.endswith(suffix) and _successful_product_exec(line[:-len(suffix)] + b') = 0\n',
                    self.product_executable, self.product_entry) is not None:
                if self.pending_product_exec_bytes + len(line) <= LINE_BYTES and len(self.pending_product_exec) < 32:
                    self.pending_product_exec[pid] = line
                    self.pending_product_exec_bytes += len(line)
                else:
                    self.errors.append('product_start_pending_capture_limit_exceeded')
            return
        self.product_start_attempted = True
        temporary = None
        try:
            witness = {'schema_version': 'openwiki-native-product-start/v1',
                'producer': 'evaluator strace outside bwrap', 'candidate_writable': False,
                'context': self.context, 'observer_source': file_ref(Path(__file__)),
                'product_entry': self.product_entry, 'product_executable': self.product_executable,
                'successful_execve': observed,
                'raw_exec_records_base64': [base64.b64encode(record).decode('ascii') for record in records],
                'raw_exec_records_sha256': [hashlib.sha256(record).hexdigest() for record in records],
                'interpretation': 'This complete OS execve record proves that Node started with the specified CLI entry. It does not prove artifact authorship, tool success, or task completion.'}
            encoded = (json.dumps(witness, sort_keys=True, indent=2) + '\n').encode()
            fd, temporary = tempfile.mkstemp(prefix='.native-product-start-', dir=self.output)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            path = self.output / PRODUCT_START_NAME
            # link is atomic and refuses replacement. A killed writer leaves
            # either a fully fsynced witness or only an unpublished temp file.
            os.link(temporary, path)
            self.product_start = {'path': str(path), 'sha256': hashlib.sha256(encoded).hexdigest()}
        except Exception as exc:
            self.errors.append('product_start_witness_error:' + type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    self.errors.append('product_start_witness_temp_cleanup_error')

    def _drain(self):
        dest = None
        try:
            with os.fdopen(self.read_fd, 'rb') as stream:
                dest = os.fdopen(self.dest_fd, 'wb')
                self.ready.set()
                while True:
                    line = stream.readline(LINE_BYTES + 1)
                    if not line:
                        self.eof = True
                        break
                    retain = self._retain(line)
                    oversized = not line.endswith(b'\n')
                    length = len(line)
                    while oversized:
                        rest = stream.readline(LINE_BYTES + 1)
                        length += len(rest)
                        if not rest or rest.endswith(b'\n'):
                            break
                    if not oversized:
                        self._observe_product_start(line)
                    if not retain:
                        continue
                    self.observed += length
                    if oversized or self.stored + len(line) > self.max_bytes:
                        self.dropped += length
                        continue
                    if self.storage_failed or self._stop_drain.is_set():
                        self.storage_failed = self.storage_failed or self._stop_drain.is_set()
                        self.dropped += length
                        continue
                    try:
                        with self._write_lock:
                            if self._stop_drain.is_set():
                                self.storage_failed = True
                                self.dropped += length
                                continue
                            dest.write(line)
                            dest.flush()
                            self.stored += len(line)
                    except (OSError, ValueError) as exc:
                        # Keep draining to prevent strace (and therefore the
                        # Candidate) from blocking on a full evidence pipe.
                        self.storage_failed = True
                        self.errors.append(type(exc).__name__ + ': ' + str(exc))
                        self.dropped += length
                        try:
                            dest.close()
                        except (OSError, ValueError):
                            pass
                        dest = None
                if not self.storage_failed and not self._stop_drain.is_set() and dest is not None:
                    try:
                        os.fsync(dest.fileno())
                    except (OSError, ValueError) as exc:
                        self.storage_failed = True
                        self.errors.append(type(exc).__name__ + ': ' + str(exc))
        except (OSError, ValueError) as exc:
            self.ready_error = type(exc).__name__ + ': ' + str(exc)
            self.ready.set()
            self.errors.append(type(exc).__name__ + ': ' + str(exc))
        finally:
            if not self.ready.is_set():
                self.ready.set()
            if dest is not None:
                try:
                    dest.close()
                except (OSError, ValueError):
                    pass

    def finish(self, *, returncode=None, interrupted=False, output_truncated=False,
               output_incomplete=False, stdout_bytes=0, stderr_bytes=0):
        write_fd, self.write_fd = self.write_fd, None
        if write_fd is not None:
            try:
                os.close(write_fd)
            except OSError:
                pass
        self.thread.join(timeout=3)
        forced_seal = False
        if self.thread.is_alive():
            forced_seal = True
            self.errors.append('trace pipe did not reach EOF within cleanup reserve')
            # Stop writing before sealing/hash calculation.  The drainer keeps
            # consuming and discarding until its pipe reaches EOF; no
            # post-seal writes are allowed to race the reference hash.
            self._stop_drain.set()
            self.thread.join(timeout=1)
            # The stop flag prevents any further destination writes; taking
            # the lock also waits out a write that was already in progress.
            with self._write_lock:
                pass
        complete = (self.eof and bool(self.pids) and self.pids <= self.terminated
                    and not self.errors and not self.dropped and not interrupted
                    and not output_truncated and not output_incomplete)
        evidence = {
            'schema_version': 'openwiki-native-process-observation/v1',
            'producer': 'evaluator strace outside bwrap', 'candidate_writable': False,
            'context': self.context, 'trace': file_ref(self.path),
            'observer_source': file_ref(Path(__file__)),
            'complete': complete, 'capture_eof': self.eof,
            'trace_sealed': not self.thread.is_alive(), 'trace_forced_seal': forced_seal,
            'interrupted': interrupted, 'tracer_returncode': returncode,
            'observed_bytes': self.observed, 'stored_bytes': self.stored,
            'dropped_bytes': self.dropped, 'capture_limit_bytes': self.max_bytes,
            'stdout_bytes': stdout_bytes, 'stderr_bytes': stderr_bytes,
            'output_capture_limit_bytes': OUTPUT_BYTES,
            'stdout_truncated': bool(output_truncated and stdout_bytes >= OUTPUT_BYTES),
            'stderr_truncated': bool(output_truncated and stderr_bytes >= OUTPUT_BYTES),
            'output_truncated': bool(output_truncated),
            'output_capture_incomplete': bool(output_incomplete),
            'string_limit_bytes': 65536,
            'string_limit_policy': 'strace marks abbreviated strings explicitly; no omitted bytes are inferred',
            'host_pids': sorted(int(p) for p in self.pids),
            'missing_terminal_pids': sorted(int(p) for p in self.pids - self.terminated),
            'successful_execve_records': self.exec_count, 'errors': self.errors,
            'product_start_witness': self.product_start,
            'interpretation': 'Raw OS evidence, not a semantic success verdict. PID fields use the host namespace; decoded syscall PIDs also identify namespace translations. An execve alone does not prove command success. Pair the process terminal and actual writes; absence/refusal never implies execution.',
        }
        path = self.output / 'native-process-observation.json'
        path.write_text(json.dumps(evidence, indent=2) + '\n')
        return evidence


class _BoundedCapture:
    def __init__(self, stream, limit=OUTPUT_BYTES):
        self.stream, self.limit = stream, limit
        self.data = bytearray()
        self.total = 0
        self.truncated = False
        self.error = None

    def drain(self):
        try:
            while True:
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    return
                self.total += len(chunk)
                if len(self.data) < self.limit:
                    self.data.extend(chunk[:self.limit - len(self.data)])
                if self.total > self.limit:
                    self.truncated = True
        except (OSError, ValueError) as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)
        finally:
            close = getattr(self.stream, 'close', None)
            if close is not None:
                try:
                    close()
                except (OSError, ValueError) as exc:
                    self.error = self.error or type(exc).__name__ + ': ' + str(exc)

    def text(self):
        return bytes(self.data).decode('utf-8', errors='replace')


def run_observed(command, *, output, context, cwd, env, timeout,
                 product_entry=None, product_executable=None):
    trace = ProcessTrace(output, context, product_entry=product_entry, product_executable=product_executable)
    process = None
    pending = None
    captures = []
    output_incomplete = False
    try:
        if not trace.ready.wait(timeout=1):
            raise RuntimeError('trace collector did not become ready before spawn')
        if trace.ready_error:
            raise RuntimeError('trace collector setup failed: ' + trace.ready_error)
        process = subprocess.Popen(trace.argv(command), cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
            pass_fds=(trace.write_fd,))
        # The parent must not keep strace's output FD open, or the drainer can
        # never observe EOF after strace exits.
        write_fd, trace.write_fd = trace.write_fd, None
        if write_fd is not None:
            try:
                os.close(write_fd)
            except OSError:
                pass
        captures = [_BoundedCapture(process.stdout), _BoundedCapture(process.stderr)]
        readers = [threading.Thread(target=c.drain, daemon=True) for c in captures]
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            for reader in readers:
                reader.join(timeout=3)
            output_incomplete = any(reader.is_alive() for reader in readers) or any(c.error for c in captures)
            output_bytes = bytes(captures[0].data)
            stderr_bytes = bytes(captures[1].data)
            raise subprocess.TimeoutExpired(command, timeout, output=output_bytes,
                stderr=stderr_bytes) from exc
        for reader in readers:
            reader.join(timeout=3)
        output_incomplete = any(reader.is_alive() for reader in readers) or any(c.error for c in captures)
        result = subprocess.CompletedProcess(command, returncode,
            captures[0].text(), captures[1].text())
        result.stdout_truncated = captures[0].truncated
        result.stderr_truncated = captures[1].truncated
        result.output_truncated = any(c.truncated for c in captures)
        result.stdout_bytes = captures[0].total
        result.stderr_bytes = captures[1].total
        result.capture_errors = [c.error for c in captures if c.error]
        return result
    except BaseException as exc:
        pending = exc
        raise
    finally:
        try:
            trace.finish(returncode=process.returncode if process and process.poll() is not None else None,
                interrupted=process is None or pending is not None,
                output_truncated=any(c.truncated for c in captures),
                output_incomplete=output_incomplete,
                stdout_bytes=captures[0].total if captures else 0,
                stderr_bytes=captures[1].total if captures else 0)
        except Exception:
            # Never replace the Candidate/timeout exception with an observer
            # sealing error.  A normal run still surfaces the observer error.
            if pending is None:
                raise


def load_observation(output, expected_context):
    output = Path(output)
    path = output / 'native-process-observation.json'
    value = json.loads(path.read_text())
    if value.get('context') != expected_context:
        raise ValueError('process observation context mismatch')
    if value.get('trace') != file_ref(output / 'native-process.trace'):
        raise ValueError('process trace changed after capture')
    if value.get('observer_source') != file_ref(Path(__file__)):
        raise ValueError('process observation source changed')
    return value


def load_product_start(output, expected_context, *, product_entry, product_executable):
    """Revalidate the independent start witness even when final capture was killed."""
    path = Path(output) / PRODUCT_START_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * LINE_BYTES:
            raise ValueError('product start witness is not a bounded regular file')
        raw = path.read_bytes()
        def unique_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate product witness key')
                value[key] = item
            return value
        witness = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(witness, dict) or witness.get('schema_version') != 'openwiki-native-product-start/v1':
            raise ValueError('product start witness schema mismatch')
        if witness.get('context') != expected_context:
            raise ValueError('product start witness context mismatch')
        if witness.get('observer_source') != file_ref(Path(__file__)):
            raise ValueError('product start observer source changed')
        if (witness.get('product_entry') != str(product_entry) or witness.get('product_executable') != str(product_executable)
                or witness.get('candidate_writable') is not False or witness.get('producer') != 'evaluator strace outside bwrap'):
            raise ValueError('product start witness identity mismatch')
        encoded_records = witness['raw_exec_records_base64']
        if not isinstance(encoded_records, list) or not 1 <= len(encoded_records) <= 2:
            raise ValueError('product start witness raw record inventory invalid')
        records = [base64.b64decode(value, validate=True) for value in encoded_records]
        observed = _successful_product_exec_records(records, str(product_executable), str(product_entry))
        if (observed is None or witness.get('successful_execve') != observed
                or witness.get('raw_exec_records_sha256') != [hashlib.sha256(record).hexdigest() for record in records]):
            raise ValueError('product start witness does not contain the required successful execve')
        return {**witness, 'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    except (OSError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError('invalid product start witness: ' + type(exc).__name__) from exc
