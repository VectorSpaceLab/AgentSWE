"""Observe the pinned Codex exec stream and rollout; never invent a thread ID.

These are source-bound native records, not a substitute for product or semantic
evaluation. The evaluator saves stream prefixes outside the Builder mounts.
No provider request, retry, resume, or credential loading occurs here.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
import time
import uuid


class NativeEvidenceError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular(path: Path, root: Path) -> bytes:
    if not path.is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents)):
        raise NativeEvidenceError('native evidence escaped its run or contains a symlink')
    if not path.is_file():
        raise NativeEvidenceError('native evidence file is missing: ' + str(path))
    return path.read_bytes()


def events(data: bytes, *, live: bool = False, rollout: bool = False) -> list[dict]:
    if not live and data and not data.endswith(b'\n'):
        raise NativeEvidenceError('native evidence has an incomplete final line')
    lines = data.splitlines()
    if live and data and not data.endswith(b'\n'):
        lines = lines[:-1]
    result = []
    for line in lines:
        # The installed Harbor adapter merges stderr into codex.txt. Native
        # protocol frames are compact JSON objects with `type` first; pretty
        # JSON, indented tool output, and untyped objects are merged text, not
        # native protocol evidence.
        # The separate rollout is pure JSONL and writes timestamp before type,
        # so every rollout line must decode as a typed event, never be skipped.
        if not rollout and not line.startswith(b'{"type":'):
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise NativeEvidenceError('malformed native JSON event') from exc
        if not isinstance(value, dict) or not isinstance(value.get('type'), str):
            raise NativeEvidenceError('native JSON event lacks a string type')
        result.append(value)
    return result


def native_streams(run: Path) -> list[Path]:
    config = json.loads(regular(run / 'builder_job_config.json', run))
    if Path(config.get('jobs_dir', '')) != run / 'jobs':
        raise NativeEvidenceError('native job directory differs from the run')
    job = config.get('job_name')
    if not isinstance(job, str) or Path(job).name != job:
        raise NativeEvidenceError('invalid native job identity')
    base = run / 'jobs' / job
    found = list(base.glob('*/agent/codex.txt'))
    order = segment_ledger(run)
    if not order:
        # No evaluator segment ledger: the pre-resume rule, unchanged.
        if len(found) != 1:
            raise NativeEvidenceError('expected exactly one native Builder stream')
        regular(found[0], run)
        return [found[0]]
    paths = [base / trial / 'agent' / 'codex.txt' for trial in order]
    if sorted(str(p) for p in paths) != sorted(str(p) for p in found):
        raise NativeEvidenceError('native Builder streams disagree with the segment ledger')
    for item in paths:
        regular(item, run)
    return paths


# A resumed Builder segment is admissible only when the evaluator itself wrote
# the ledger that declares it.  The ledger lives in the run root, outside every
# Builder mount, so a second stream that no evaluator recorded is still refused.
RESUME_CAP = 2
SEGMENT_LEDGER = 'builder_segments.json'


def segment_ledger(run: Path) -> list[str]:
    """Evaluator-declared segment order; an empty list means one segment."""
    path = run / SEGMENT_LEDGER
    if not path.is_file():
        return []
    value = json.loads(regular(path, run))
    if not isinstance(value, dict) or value.get('schema_version') != 'agentswe-builder-segments/v1':
        raise NativeEvidenceError('unknown Builder segment ledger')
    segments = value.get('segments')
    if not isinstance(segments, list) or not segments:
        raise NativeEvidenceError('invalid Builder segment ledger')
    cap = value.get('resume_cap')
    if type(cap) is not int or not 0 <= cap <= RESUME_CAP:
        raise NativeEvidenceError('invalid Builder resume cap')
    if len(segments) > cap + 1:
        raise NativeEvidenceError('native Builder segments exceed the resume cap')
    deadline = value.get('builder_deadline_epoch')
    if not isinstance(deadline, (int, float)):
        raise NativeEvidenceError('Builder segment ledger has no budget deadline')
    trials = []
    for index, row in enumerate(segments, 1):
        if not isinstance(row, dict) or row.get('segment_index') != index:
            raise NativeEvidenceError('invalid Builder segment ledger')
        trial = row.get('trial')
        if not isinstance(trial, str) or Path(trial).name != trial or not trial:
            raise NativeEvidenceError('invalid Builder segment ledger')
        started = row.get('started_at_epoch')
        if not isinstance(started, (int, float)) or started > deadline:
            raise NativeEvidenceError('native Builder segment started after the budget deadline')
        if index > 1 and row.get('resume_of_session_id') != segments[0].get('session_id'):
            raise NativeEvidenceError('native Builder segment does not resume the first session')
        trials.append(trial)
    if len(set(trials)) != len(trials):
        raise NativeEvidenceError('invalid Builder segment ledger')
    return trials


def native_stream(run: Path) -> Path:
    """The segment currently being written; the only one before any resume."""
    return native_streams(run)[-1]


def readiness_segment_streams(run: Path, native: dict) -> list[Path]:
    """Ordered Builder segment streams behind a native proof, terminal last.

    A proof with one segment -- every run that never resumed, and every proof
    written before the segment keys existed -- returns exactly
    ``[source_files[0]]``, which is what the readiness bundle read before.
    More than one segment is admissible only when this run's evaluator-written
    ``builder_segments.json`` declares them in the same order, and only when
    every non-final segment is a recorded infrastructure cut that completed no
    turn.  The Builder cannot write that ledger: it lives in the run root,
    outside every Builder mount.
    """
    refs = native.get('source_files')
    if not isinstance(refs, list) or not refs:
        raise NativeEvidenceError('native Builder stream reference missing')
    rows = native.get('native_segments')
    if not isinstance(rows, list) or not rows:
        return [Path(refs[0]['path'])]
    if native.get('native_segment_count') != len(rows):
        raise NativeEvidenceError('native Builder segment count disagrees with its proof')
    paths = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get('segment_index') != index:
            raise NativeEvidenceError('invalid native Builder segment proof')
        if index < len(rows):
            if row.get('infrastructure_cut') is not True or row.get('native_turn_completed_count'):
                raise NativeEvidenceError('a resumed native Builder segment did not follow an infrastructure cut')
        elif not row.get('native_turn_completed_count'):
            raise NativeEvidenceError('the terminal native Builder segment completed no turn')
        paths.append(Path(row['path']))
    if [str(v) for v in paths] != [str(v.get('path')) for v in refs[:len(paths)]]:
        raise NativeEvidenceError('native Builder segment proof disagrees with its source files')
    if len(paths) > 1:
        # segment_ledger() is the evaluator gate: schema, dense index, cap <= 2,
        # budget deadline, and every resumed row bound to segment 1's session.
        if [v.parent.parent.name for v in paths] != segment_ledger(run):
            raise NativeEvidenceError('native Builder streams disagree with the segment ledger')
    return paths



def settled_read(path: Path, root: Path, *, attempts: int = 12, pause: float = 0.5) -> bytes:
    """Return the file's bytes once two consecutive reads agree.

    An outside copy still in flight yields a short or torn read; waiting for it
    to stop changing removes that without relaxing anything checked afterwards.
    """
    previous = regular(path, root)
    for _ in range(attempts):
        time.sleep(pause)
        current = regular(path, root)
        if current == previous:
            return current
        previous = current
    raise NativeEvidenceError('native evidence did not settle while being read')



BUILDER_AUTH_PATH = re.compile(r'/dev/shm/agentswe-builder-auth-[A-Za-z0-9_]+/auth\.json')


def redacted(value):
    """Put an event into the redaction state the finished stream will have.

    Harbor replaces the Builder credential path with [REDACTED] when it
    re-materialises the stream, so the same event observed live and observed at
    the end differ by exactly that substitution. Applying it to both sides
    compares events rather than redaction timing. Only this one path -- created
    by the evaluator itself -- is normalised.
    """
    if isinstance(value, str):
        return BUILDER_AUTH_PATH.sub('[REDACTED]', value)
    if isinstance(value, dict):
        return {key: redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def event_digest(value: dict) -> str:
    """Stable identity for one typed event, independent of its byte framing."""
    return sha(json.dumps(redacted(value), sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False).encode('utf-8'))


def observe_thread(run: Path, prior: list[dict]) -> dict:
    streams = native_streams(run)
    known = {str(item) for item in streams}
    path = streams[-1]
    data = regular(path, run)
    # A later append may complete the last line; preserve only complete bytes.
    data = data[:data.rfind(b'\n') + 1]
    current = events(data, live=True)
    observed = [event_digest(value) for value in current]
    for ref in prior:
        if ref['path'] not in known:
            raise NativeEvidenceError('native stream was replaced or truncated after observation')
        if ref['path'] == str(path):
            ref_data, ref_observed = data, observed
        else:
            # An earlier segment is closed, but it must still be intact: read it
            # back and hold it to exactly the rule its own observation recorded.
            ref_data = regular(Path(ref['path']), run)
            ref_data = ref_data[:ref_data.rfind(b'\n') + 1]
            ref_observed = [event_digest(value) for value in events(ref_data, live=True)]
        recorded = ref.get('events')
        if recorded is None:
            # Pre-existing observation with only a byte hash; keep the old rule.
            if sha(ref_data[:ref['size']]) != ref['sha256']:
                raise NativeEvidenceError('native stream was replaced or truncated after observation')
            continue
        # Harbor re-materialises the merged stream when the job finishes, so the
        # bytes of an earlier prefix are not stable while its events are. What
        # must hold is that every event observed earlier is still here, in order
        # and unaltered: a removal breaks the prefix, an edit breaks a digest.
        if ref_observed[:len(recorded)] != recorded:
            raise NativeEvidenceError('native stream was replaced or truncated after observation')
    identities = []
    for item in streams:
        body = data if item == path else regular(item, run)
        body = body[:body.rfind(b'\n') + 1]
        starts = [v for v in events(body, live=True) if v.get('type') == 'thread.started']
        # One thread.started per segment: `codex exec resume` announces the
        # session it re-entered, with the id it was given.
        if len(starts) != 1:
            raise NativeEvidenceError('expected exactly one native thread.started event')
        identities.append(starts[0].get('thread_id'))
    if len(set(identities)) != 1:
        raise NativeEvidenceError('native Builder segments do not share one session identity')
    thread_id = identities[0]
    try:
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise NativeEvidenceError('invalid native thread identity') from exc
    return {'thread_id': thread_id, 'path': str(path), 'size': len(data), 'sha256': sha(data),
            'events': observed}


def text_blocks(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [part for item in value for part in text_blocks(item)]
    if isinstance(value, dict):
        return text_blocks(value.get('text', value.get('output', [])))
    return []


def response_objects(value):
    """Decode actual tool-output JSON, including code-mode text envelopes."""
    decoder = json.JSONDecoder()
    for text in text_blocks(value):
        index = 0
        while index < len(text):
            index = text.find('{', index)
            if index < 0:
                break
            try:
                obj, length = decoder.raw_decode(text[index:])
            except ValueError:
                index += 1
                continue
            index += length
            if isinstance(obj, dict) and obj.get('status') == 200 and isinstance(obj.get('payload'), dict):
                yield obj['payload']
            elif isinstance(obj, dict) and isinstance(obj.get('output'), str):
                # A code-mode chunk wraps the real tool output as a JSON string in
                # ``output``. Decoding the wrapper alone advances past the envelope
                # inside it, which made issued feedback look absent from the stream.
                yield from response_objects(obj['output'])


def classify_native_termination(stream_events: list[dict]) -> dict:
    """Classify pinned CLI events; a retry announcement is not a final failure.

    Only the CLI's exact Reconnecting n/N shape is recoverable. Success still
    requires one turn start and one later completion, no fatal event, and no
    unrecognized error. This never reconstructs a response or authorizes retry.
    Announcements do not establish actual upstream attempts or their billing.
    """
    starts = [i for i, v in enumerate(stream_events) if v.get('type') == 'turn.started']
    completions = [(i, v) for i, v in enumerate(stream_events) if v.get('type') == 'turn.completed']
    errors, reconnects, unrecognized, fatal = [], [], [], []
    # Native HTTP errors can include a multiline response body inside the
    # parentheses. Match the whole pinned CLI announcement, retaining it raw.
    pattern = re.compile(r'Reconnecting\.\.\. ([1-9][0-9]*)/([1-9][0-9]*) \((.+)\)', re.DOTALL)
    for index, event in enumerate(stream_events):
        if event.get('type') in {'turn.failed', 'turn.incomplete', 'thread.failed', 'fatal'}:
            fatal.append({'stream_event_index': index, 'event': event})
        if event.get('type') != 'error':
            continue
        row = {'stream_event_index': index, 'event': event}
        errors.append(row)
        message = event.get('message')
        match = pattern.fullmatch(message) if isinstance(message, str) else None
        if match and int(match[1]) <= int(match[2]):
            reconnects.append({**row, 'retry_index': int(match[1]), 'retry_limit': int(match[2])})
        else:
            unrecognized.append(row)
    threads = [i for i, v in enumerate(stream_events) if v.get('type') == 'thread.started']
    cut = bool(len(threads) == 1 and len(starts) == 1 and not completions and
        not any(v['event'].get('type') == 'thread.failed' for v in fatal) and
        (any(r['retry_index'] == r['retry_limit'] for r in reconnects) or not unrecognized))
    successful = bool(len(threads) == 1 and len(starts) == len(completions) == 1 and not fatal and not unrecognized
        and threads[0] < starts[0] < completions[0][0] == len(stream_events) - 1
        and all(starts[0] < r['stream_event_index'] < completions[0][0] for r in reconnects))
    return {'schema_version': 'agentswe-native-termination/v1', 'successful_terminal': successful,
        'infrastructure_cut': cut and not successful,
        'native_thread_start_count': len(threads), 'native_turn_start_count': len(starts), 'native_turn_completed_count': len(completions),
        'error_events': errors, 'reconnect_events': reconnects,
        'unrecognized_error_events': unrecognized, 'fatal_events': fatal,
        'recovered_in_same_turn': bool(reconnects and successful),
        'native_retry_announcements': len(reconnects),
        'retry_related_usage_unknown': bool(reconnects),
        'actual_upstream_requests': None, 'provider_total_usage': None,
        'complete_provider_billing_claimed': False,
        'native_reported_turn_usage': [v.get('usage') for _, v in completions]}


# A bare POSIX environment-assignment word (`PATH=/opt/...:$PATH`) that may precede the
# executable; only this shape is skipped, nothing is expanded or interpreted.
_ASSIGNMENT_WORD = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_INTERPRETERS = frozenset({'python3', 'python', '/usr/bin/python3', '/usr/bin/python',
                          '/usr/local/bin/python3', '/usr/local/bin/python'})


def _is_duration(token: str) -> bool:
    """A bare timeout duration: digits, optional fraction, optional s/m/h/d."""
    return bool(re.fullmatch(r'\d+(?:\.\d+)?[smhd]?', token))


def submission_commands(shell: str) -> list[str]:
    """Recognize simple shell invocations without evaluating model text.

    Non-POSIX tokenization retains quote delimiters, so a quoted newline or
    semicolon is never interpreted as a command separator. Compound control,
    substitutions, and heredocs are deliberately opaque. The documented bare
    command and the actual mounted absolute executable are supported, including
    their use after a normal ``cd ... &&`` command.
    """
    if '$(' in shell or '`' in shell:
        return []
    lexer = shlex.shlex(shell, posix=False, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    controls = {'if', 'then', 'elif', 'else', 'fi', 'for', 'while', 'until',
                'case', 'esac', 'do', 'done', 'function', '{', '}', '(', ')'}
    if any(token in controls or token.startswith('<<') for token in tokens):
        return []
    segments, current = [], []
    for token in tokens:
        if token and all(char in ';&|\n' for char in token):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    result = []
    for segment in segments:
        # Only an executable in command position counts; quoted arguments of
        # printf/echo/cat and diagnostic searches cannot establish provenance.
        # A `timeout <duration>` wrapper is transparent for provenance: the
        # executable still stands in command position after it, and the Builder
        # needs the wrapper because its own tool call is killed while the
        # evaluator runs the dev case. Exactly one bare duration is skipped --
        # no options are interpreted and nothing else is stepped over.
        # A leading environment assignment (`PATH=/opt/agentswe-openhands/bin:$PATH
        # submit_dev_candidate --wait`, formal 0920-fh-001 round 1) is transparent for
        # provenance: the executable still stands in command position after the
        # assignment words. Only bare `NAME=value` words are skipped.
        while segment and _ASSIGNMENT_WORD.match(segment[0]):
            segment = segment[1:]
        if not segment:
            continue
        head = segment
        try:
            leading = shlex.split(segment[0], posix=True)
        except ValueError:
            leading = []
        if (len(leading) == 1 and leading[0] in {'timeout', '/usr/bin/timeout'}
                and len(segment) > 2 and _is_duration(segment[1])):
            head = segment[2:]
        # `python3 /usr/local/bin/submit_dev_candidate …` runs the same mounted
        # script: the interpreter is in command position and the executable is
        # named by absolute path. The bare name is not accepted here, because it
        # would not name the mounted program.
        try:
            interpreter = shlex.split(head[0], posix=True) if head else []
        except ValueError:
            interpreter = []
        if (len(interpreter) == 1 and interpreter[0] in _INTERPRETERS
                and len(head) > 1 and head[1] == '/usr/local/bin/submit_dev_candidate'):
            head = head[1:]
        try:
            first = shlex.split(head[0], posix=True) if head else []
        except ValueError:
            continue
        if len(first) == 1 and first[0] in {
                'submit_dev_candidate', '/usr/local/bin/submit_dev_candidate'}:
            result.append(' '.join(head))
    return result


# The wrapper is one client of the dev controller socket, not the action itself.
# formal_one_stop.py writes /usr/local/bin/submit_dev_candidate into the Builder
# container (:1037-1053, :1098-1102) as a thin client that sends
# {"token": ..., "action": "submit"[, "feedback_digest": ...]} to
# $AGENTSWE_DEV_CONTROLLER_SOCKET, and the evaluator exports that socket and
# token into the same container (:1343-1344). A Builder that writes the request
# itself has taken the same authenticated action against the same controller, so
# provenance recognizes the action rather than one file name. Nothing else is
# relaxed: the request must still stand in command position, carried by an
# interpreter that runs it, so an `echo`, a `cat` of the wrapper's own source, a
# heredoc fed to `cat`, or a search hit still establishes nothing. Quote
# characters may be backslash-escaped because the tool-call arguments containing
# this text are themselves JSON.
CONTROLLER_ADDRESS = re.compile(r'AGENTSWE_DEV_CONTROLLER_(?:SOCKET|TOKEN)'
                                r'|/[\w./-]*\.sock\b')
_SUBMIT_QUOTE = r'''(?:\\?["\'])'''
_SUBMIT_REQUEST = re.compile(_SUBMIT_QUOTE + 'action' + _SUBMIT_QUOTE + r'\s*:\s*'
                             + _SUBMIT_QUOTE + 'submit' + _SUBMIT_QUOTE)
_HEREDOC_OPERATOR = re.compile(r'<<-?')


def _shell_words(line: str) -> list[str] | None:
    """Tokenize one line exactly as submission_commands tokenizes a command."""
    lexer = shlex.shlex(line, posix=False, punctuation_chars=';&|()<>\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _one_word(token: str) -> str | None:
    """The single unquoted word a token stands for, or None."""
    try:
        words = shlex.split(token, posix=True)
    except ValueError:
        return None
    return words[0] if len(words) == 1 else None


def interpreter_scripts(shell: str) -> list[str]:
    """Inline scripts run by an interpreter standing in command position.

    A heredoc body is data supplied to the command that opened it and is never
    command text of its own, so it is attributed to that one command and to no
    other. Lines are tokenized non-POSIX like submission_commands, so a quoted
    `<<` or `;` inside an argument is one token and never a redirection or a
    separator. Nothing is expanded, substituted, or executed; the model's text
    is read, and unknown syntax yields no script rather than a guess.
    """
    controls = {'if', 'then', 'elif', 'else', 'fi', 'for', 'while', 'until',
                'case', 'esac', 'do', 'done', 'function', '{', '}', '(', ')'}
    scripts = []
    lines = shell.split('\n')
    position = 0
    while position < len(lines):
        tokens = _shell_words(lines[position])
        position += 1
        if tokens is None or any(token in controls for token in tokens):
            continue
        body, opened = '', None
        for offset, token in enumerate(tokens):
            if not _HEREDOC_OPERATOR.fullmatch(token) or offset + 1 >= len(tokens):
                continue
            delimiter = _one_word(tokens[offset + 1])
            if delimiter is None:
                break
            stripped = token.endswith('-')
            collected = []
            while position < len(lines):
                line = lines[position]
                position += 1
                if (line.strip() if stripped else line.rstrip()) == delimiter:
                    break
                collected.append(line)
            body, opened = '\n'.join(collected), offset
            break
        segments, current, first = [], [], 0
        for offset, token in enumerate(tokens + [';']):
            if token and all(char in ';&|\n' for char in token):
                segments.append((current, first, offset))
                current, first = [], offset + 1
            else:
                current.append(token)
        for words, start, end in segments:
            # Only an interpreter in command position runs the script it is
            # given; a leading environment assignment and one `timeout
            # <duration>` wrapper are transparent, exactly as for the wrapper.
            while words and _ASSIGNMENT_WORD.match(words[0]):
                words = words[1:]
            if not words:
                continue
            leading = _one_word(words[0])
            if (leading in {'timeout', '/usr/bin/timeout'} and len(words) > 2
                    and _is_duration(words[1])):
                words = words[2:]
            if not words or _one_word(words[0]) not in _INTERPRETERS:
                continue
            carried = body if opened is not None and start <= opened < end else ''
            scripts.append(' '.join(words) + ('\n' + carried if carried else ''))
    return scripts


def controller_submit_commands(shell: str) -> list[str]:
    """Recognize a submit addressed straight to the dev controller socket.

    The same action the wrapper performs, by the wrapper's own protocol: an
    `action: submit` request against the controller socket or token, run by an
    interpreter in command position. The returned text is the invocation and its
    inline script, so the caller's existing acknowledgement test reads the exact
    feedback digest out of the command that carried it.
    """
    return [script for script in interpreter_scripts(shell)
            if CONTROLLER_ADDRESS.search(script) and _SUBMIT_REQUEST.search(script)]


def pair_native_tools(native: list[dict]) -> dict:
    """Bind each native output to its call, including asynchronous submit waits.

    A polling call inherits a submit origin only from the exact session/cell
    handle returned by its earlier tool output. A diagnostic mentioning the
    submit command does not establish an invocation. All raw evidence remains
    in the source-bound rollout; these records are an index into that evidence.
    """
    calls, handles = {}, {}
    output_count = 0
    call_types = {'function_call': 'function_call_output',
                  'custom_tool_call': 'custom_tool_call_output'}
    # Both JSON function arguments and code-mode JavaScript contain quoted cmd
    # values. Decode only double-quoted string literals; unknown syntax fails
    # closed for submission provenance instead of executing model text.
    command_pattern = re.compile(r'(?:"cmd"|\bcmd)\s*:\s*("(?:\\.|[^"\\])*")')
    handle_pattern = re.compile(r'(?<![A-Za-z0-9_])(?:"?(session_id|cell_id)"?)\s*:\s*"?([A-Za-z0-9_-]+)')
    for index, event in enumerate(native):
        if event.get('type') != 'response_item':
            continue
        payload = event.get('payload', {})
        kind, call_id = payload.get('type'), payload.get('call_id')
        if kind not in (*call_types, *call_types.values()):
            continue
        if not isinstance(call_id, str) or not call_id:
            raise NativeEvidenceError('native tool event has no call identity')
        if kind in call_types:
            if call_id in calls:
                raise NativeEvidenceError('duplicate native tool call identity')
            command = str(payload.get('arguments', payload.get('input', '')))
            origins, shells = [], []
            for match in command_pattern.finditer(command):
                try:
                    shell = json.loads(match[1])
                except ValueError:
                    continue
                shells.append(shell)
                for submission in submission_commands(shell):
                    origins.append({'call_id': call_id, 'event_index': index, 'command': submission})
            if not origins:
                # The same submit, spoken to the controller socket the wrapper
                # itself speaks to. Consulted only where the wrapper is absent,
                # so a call that already has a named invocation keeps exactly
                # the origin, and the ambiguity rule, it has today.
                for shell in shells:
                    for submission in controller_submit_commands(shell):
                        origins.append({'call_id': call_id, 'event_index': index, 'command': submission})
            if not origins:
                for key, value in handle_pattern.findall(command):
                    origin = handles.get((key, value))
                    if origin is not None and origin not in origins:
                        origins.append(origin)
            if len(origins) > 1:
                raise NativeEvidenceError('ambiguous native submission provenance')
            calls[call_id] = {'event_index': index, 'expected_output': call_types[kind],
                             'submission_origin': origins[0] if origins else None,
                             'output_event_index': None}
        else:
            call = calls.get(call_id)
            if call is None or call['expected_output'] != kind:
                raise NativeEvidenceError('native tool output has no earlier matching call')
            if call['output_event_index'] is not None:
                raise NativeEvidenceError('duplicate native tool output identity')
            call['output_event_index'] = index
            output_count += 1
            origin = call['submission_origin']
            if origin is not None:
                text = '\n'.join(text_blocks(payload.get('output')))
                returned = handle_pattern.findall(text)
                returned += [('cell_id', m) for m in re.findall(r'Script running with cell ID ([A-Za-z0-9_-]+)', text)]
                returned += [('session_id', m) for m in re.findall(r'Process running with session ID ([A-Za-z0-9_-]+)', text)]
                for key, value in returned:
                    previous = handles.get((key, value))
                    if previous is not None and previous != origin:
                        raise NativeEvidenceError('native asynchronous handle changed submission origin')
                    handles[(key, value)] = origin
    if any(call['output_event_index'] is None for call in calls.values()):
        raise NativeEvidenceError('native tool call has no matching output')
    return {'valid': True, 'call_count': len(calls), 'output_count': output_count,
            'submission_wait_handle_count': len(handles), 'by_call': calls}


def verify_native(run: Path, records: list[dict], deliveries: list[dict],
                  observations: list[dict], *, allow_interrupted: bool = False) -> dict:
    proof = {'schema_version': 'agentswe-native-builder-evidence/v1', 'valid': False,
             'errors': [], 'feedback_received': [], 'revision_observed': False,
             'native_segments': [], 'native_segment_count': 1, 'native_completed_turns': 0}
    try:
        streams = native_streams(run)
        stream = streams[-1]
        data = regular(stream, run)
        proof['source_files'] = [{'path': str(stream), 'sha256': sha(data)}]
        stream_events = events(data)
        termination = classify_native_termination(stream_events)
        proof['native_termination'] = termination
        current = observe_thread(run, observations)
        proof['native_thread_id'] = current['thread_id']
        rollouts = list((stream.parent / 'sessions').glob('**/rollout-*.jsonl'))
        if len(rollouts) != 1:
            raise NativeEvidenceError('expected exactly one native rollout')
        # `codex exec resume` appends to the session's own rollout rather than
        # opening a new one (probed on 0.144.1), so the last segment's rollout
        # is the whole session.  Each earlier segment's copy must therefore be a
        # byte prefix of it: that is what proves continuation, not replacement.
        for earlier in streams[:-1]:
            previous = list((earlier.parent / 'sessions').glob('**/rollout-*.jsonl'))
            if len(previous) > 1:
                raise NativeEvidenceError('expected exactly one native rollout')
            if previous and not regular(rollouts[0], run).startswith(regular(previous[0], run)):
                raise NativeEvidenceError('resumed rollout is not a continuation of the cut segment')
        rollout = rollouts[0]
        rollout_data = settled_read(rollout, run)
        proof['source_files'].append({'path': str(rollout), 'sha256': sha(rollout_data)})
        native = events(rollout_data, rollout=True)
        meta = [v.get('payload', {}) for v in native if v.get('type') == 'session_meta']
        if len(meta) != 1 or meta[0].get('id') != current['thread_id']:
            raise NativeEvidenceError('rollout identity disagrees with thread.started')
        if meta[0].get('cli_version') != '0.144.1' or meta[0].get('source') != 'exec':
            raise NativeEvidenceError('Builder is not the pinned native Codex exec version')
        if meta[0].get('cwd') != '/workspace/worktree':
            raise NativeEvidenceError('native Builder workspace differs from configured workspace')
        contexts = [v.get('payload', {}) for v in native if v.get('type') == 'turn_context']
        if not contexts or any(v.get('model') != 'deepseek-flash' or v.get('effort') != 'max' for v in contexts):
            raise NativeEvidenceError('native Builder model/effort not established')
        segments = []
        for index, item in enumerate(streams, 1):
            body = data if item == stream else regular(item, run)
            per = events(body)
            state = classify_native_termination(per)
            if sum(v.get('type') == 'turn.started' for v in per) != 1:
                raise NativeEvidenceError('native stream does not prove one continuous invocation')
            if index < len(streams) and not state['infrastructure_cut']:
                raise NativeEvidenceError('a resumed native Builder segment did not follow an infrastructure cut')
            if index == len(streams):
                if state['fatal_events'] or state['unrecognized_error_events']:
                    raise NativeEvidenceError('native Builder has a fatal or unrecognized error event')
                if not allow_interrupted and not state['successful_terminal']:
                    raise NativeEvidenceError('native Builder has no successful terminal event')
            segments.append({'segment_index': index, 'path': str(item), 'sha256': sha(body),
                             'native_turn_start_count': state['native_turn_start_count'],
                             'native_turn_completed_count': state['native_turn_completed_count'],
                             'infrastructure_cut': state['infrastructure_cut'],
                             'successful_terminal': state['successful_terminal'],
                             'error_events': state['error_events'],
                             'reconnect_events': state['reconnect_events']})
        proof['native_segments'] = segments
        proof['native_segment_count'] = len(segments)
        native_completed_turns = sum(v['native_turn_completed_count'] for v in segments)
        proof['native_completed_turns'] = native_completed_turns
        completed = native_completed_turns >= 1
        pairing = pair_native_tools(native)
        proof['native_tool_pairing'] = {k: v for k, v in pairing.items() if k != 'by_call'}
        outputs = []
        actions = []
        for index, event in enumerate(native):
            p = event.get('payload', {})
            if event.get('type') != 'response_item':
                continue
            if p.get('type') in ('function_call', 'custom_tool_call'):
                actions.append((index, p.get('call_id'), str(p.get('arguments', p.get('input', '')))))
            if p.get('type') in ('function_call_output', 'custom_tool_call_output'):
                outputs.extend((index, p.get('call_id'), value) for value in response_objects(p.get('output')))
        previous_index = -1
        for number, record in enumerate(records, 1):
            if record.get('builder_session_id') != current['thread_id']:
                raise NativeEvidenceError('accepted delivery is not bound to native thread')
            feedback_path = Path(record['feedback_path'])
            feedback_data = regular(feedback_path, run)
            if sha(feedback_data) != record.get('feedback_digest'):
                raise NativeEvidenceError('authoritative feedback bytes changed')
            feedback = json.loads(feedback_data)
            sent = [v for v in deliveries if v.get('candidate_number') == number and
                    v.get('feedback_digest') == record['feedback_digest'] and
                    v.get('builder_session_id') == current['thread_id']]
            if not sent:
                raise NativeEvidenceError('feedback response has no successful socket-write receipt')
            matches = [(index, call_id, value) for index, call_id, value in outputs if index > previous_index
                       and value.get('submission_number') == number
                       and value.get('builder_session_id') == current['thread_id']
                       and value.get('candidate_digest') == record.get('candidate_digest')
                       and value.get('feedback_digest') == record['feedback_digest']
                       and value.get('feedback') == feedback
                       and any(v.get('payload_sha256') == sha(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()) for v in sent)]
            # The echo is recorded when the Builder's stream carries it and is no
            # longer required: submit blocks for minutes while the evaluator runs
            # the candidate, which does not fit a tool-output window, so demanding
            # it made `submit_dev_candidate --wait` unable to leave admissible
            # evidence. The create benchmark these diverged from says the opposite:
            # "If your shell tool imposes a timeout, use repeated --status calls
            # instead." Delivery is proven by the socket-write receipt above and
            # consumption by the acknowledgement contract validate_delivery enforces
            # at submission time.
            index, call_id, value = matches[0] if matches else (previous_index, None, {})
            origin = pairing['by_call'][call_id].get('submission_origin') if matches else None
            if matches and (origin is None or not previous_index < origin['event_index'] < index):
                raise NativeEvidenceError('authoritative feedback has no paired native submit source')
            if number > 1:
                ack = records[number - 2]['feedback_digest']
                if (matches and value.get('feedback_digest_ack') != ack) or record.get('feedback_digest_ack') != ack:
                    raise NativeEvidenceError('native revision does not acknowledge preceding feedback')
                # An actual new model-authored submit action must follow feedback.
                # A submission that outlives the Builder shell tool's yield
                # window is continued with a fresh `submit_dev_candidate
                # --await-only`, and pair_native_tools makes that continuation
                # the paired origin because it is itself a submit invocation.
                # The continuation never repeats --feedback-digest, so the ack
                # sits on the earlier model-authored submit of the same round.
                # Accept it only from an action strictly after the previous
                # round's echo and no later than the paired origin: outside
                # that window it is not this revision's action, and that window
                # is tighter than the unwindowed scan used when no origin
                # exists at all.
                if not any(ack in command for _, _, command in actions) if origin is None \
                        else (ack not in origin['command'] and not any(
                            ack in command for position, _, command in actions
                            if previous_index < position <= origin['event_index'])):
                    raise NativeEvidenceError('no model-authored feedback-bound revision action')
                if record.get('build', {}).get('candidate_repo_digest') == records[number - 2].get('build', {}).get('candidate_repo_digest'):
                    raise NativeEvidenceError('revision did not change product source')
            previous_index = index
            proof['feedback_received'].append({'submission_number': number, 'feedback_digest': record['feedback_digest'],
                                               'rollout_event_index': index, 'call_id': call_id,
                                               'submission_call_id': origin['call_id'] if origin else None,
                                               'payload_echoed_in_native_output': bool(matches),
                                               'submission_event_index': origin['event_index'] if origin else None})
        if not records:
            raise NativeEvidenceError('no accepted submissions')
        seen = [v['feedback_digest'] for v in records]
        if len(set(seen)) != len(seen):
            raise NativeEvidenceError('a native Builder segment replayed an acknowledged feedback digest')
        proof.update(valid=True, revision_observed=len(records) > 1, native_turn_completed=completed,
                     source_files=[{'path': v['path'], 'sha256': v['sha256']} for v in segments]
                                  + [{'path': str(rollout), 'sha256': sha(rollout_data)}],
                     observed_stream_prefixes=observations)
    except (NativeEvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        proof['errors'].append(str(exc))
    return proof
