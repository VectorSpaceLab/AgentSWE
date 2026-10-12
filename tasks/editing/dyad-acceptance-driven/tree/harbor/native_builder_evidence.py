"""Observe the pinned Codex exec stream and rollout; never invent a thread ID.

These are source-bound native records, not a substitute for product or semantic
evaluation. The evaluator saves stream prefixes outside the Builder mounts.
No provider request, retry, resume, or credential loading occurs here.
"""
from __future__ import annotations

import hashlib
import json
import re
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


def events(data: bytes, *, live: bool = False) -> list[dict]:
    if not live and data and not data.endswith(b'\n'):
        raise NativeEvidenceError('native evidence has an incomplete final line')
    lines = data.splitlines()
    if live and data and not data.endswith(b'\n'):
        lines = lines[:-1]
    result = []
    # The Builder stream uses type-first JSONL, while Codex rollout files use
    # timestamp-first envelopes such as {"timestamp":...,"type":...}.
    # Accept both exact typed forms, retain the existing stderr-excerpt guard,
    # and fail closed for every other brace-starting line.
    frame_start = re.compile(rb'^\s*\{\s*"type"\s*:')
    stderr_header = re.compile(rb'^\S+\s+ERROR\s+.*apply_patch verification failed:')
    in_stderr_excerpt = False
    for line in lines:
        if stderr_header.match(line):
            in_stderr_excerpt = True
            continue
        stripped = line.lstrip()
        if not stripped.startswith(b'{'):
            if in_stderr_excerpt:
                continue
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            if in_stderr_excerpt:
                continue
            raise NativeEvidenceError('malformed native JSON event') from exc
        if not isinstance(value, dict) or not isinstance(value.get('type'), str):
            if in_stderr_excerpt:
                continue
            raise NativeEvidenceError('unrecognized or malformed native JSON frame')
        # A valid typed boundary ends an adapter stderr excerpt. This also
        # admits timestamp-first rollout envelopes without weakening identity
        # or tool-output validation performed by the caller.
        in_stderr_excerpt = False
        result.append(value)
    if in_stderr_excerpt:
        raise NativeEvidenceError('apply_patch stderr excerpt has no subsequent native frame boundary')
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
    cut = bool(len(starts) == 1 and not completions and
        not any(v['event'].get('type') == 'thread.failed' for v in fatal) and
        (any(r['retry_index'] == r['retry_limit'] for r in reconnects) or not unrecognized))
    successful = bool(len(starts) == len(completions) == 1 and not fatal and not unrecognized
        and starts[0] < completions[0][0]
        and all(starts[0] < r['stream_event_index'] < completions[0][0] for r in reconnects))
    return {'schema_version': 'agentswe-native-termination/v1', 'successful_terminal': successful,
        'infrastructure_cut': cut and not successful,
        'native_turn_start_count': len(starts), 'native_turn_completed_count': len(completions),
        'error_events': errors, 'reconnect_events': reconnects,
        'unrecognized_error_events': unrecognized, 'fatal_events': fatal,
        'recovered_in_same_turn': bool(reconnects and successful),
        'native_retry_announcements': len(reconnects),
        'retry_related_usage_unknown': bool(reconnects),
        'actual_upstream_requests': None, 'provider_total_usage': None,
        'complete_provider_billing_claimed': False,
        'native_reported_turn_usage': [v.get('usage') for _, v in completions]}


# `/usr/local/bin/submit_dev_candidate` is a thin client the evaluator writes
# itself: it sends {"token": ..., "action": "submit", "feedback_digest": ...}
# over $AGENTSWE_DEV_CONTROLLER_SOCKET, and that socket and token are exported
# into the Builder container.  A Builder that inlines the same request instead
# of invoking the name has taken the same authenticated action, so recognising
# only the name recognises one shell habit rather than the action.  Quotes may
# be backslash-escaped because the tool-call arguments are themselves JSON.
SUBMIT_QUOTE = r'''(?:\\?["\'])'''
SUBMIT_REQUEST = re.compile(SUBMIT_QUOTE + 'action' + SUBMIT_QUOTE + r'\s*:\s*'
                            + SUBMIT_QUOTE + 'submit' + SUBMIT_QUOTE)
SUBMIT_CONTROLLER = ('AGENTSWE_DEV_CONTROLLER_SOCKET', 'AGENTSWE_DEV_CONTROLLER_TOKEN', '.sock')


def submit_action(command: str) -> bool:
    """True when one tool call's own text is a submit against the dev controller.

    Either by the wrapper's name, or by the wrapper's protocol: an `action:
    submit` request addressed to the controller socket.  Nothing else about the
    revision rule moves -- the caller still demands that this same command be
    model-authored, sit after the preceding round, and carry that round's exact
    feedback digest.
    """
    if 'submit_dev_candidate' in command:
        return True
    return (any(hint in command for hint in SUBMIT_CONTROLLER)
            and bool(SUBMIT_REQUEST.search(command)))


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
        native = events(rollout_data)
        meta = [v.get('payload', {}) for v in native if v.get('type') == 'session_meta']
        if len(meta) != 1 or meta[0].get('id') != current['thread_id']:
            raise NativeEvidenceError('rollout identity disagrees with thread.started')
        if meta[0].get('cli_version') != '0.144.1' or meta[0].get('source') != 'exec':
            raise NativeEvidenceError('Builder is not the pinned native Codex exec version')
        if meta[0].get('cwd') != '/workspace':
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
            if number > 1:
                ack = records[number - 2]['feedback_digest']
                if (matches and value.get('feedback_digest_ack') != ack) or record.get('feedback_digest_ack') != ack:
                    raise NativeEvidenceError('native revision does not acknowledge preceding feedback')
                # An actual new model-authored submit action must follow feedback.
                if not any(action_index > previous_index and (not matches or action_index < index)
                           and submit_action(command)
                           and ack in command for action_index, _, command in actions):
                    raise NativeEvidenceError('no model-authored feedback-bound revision action')
                if record.get('build', {}).get('candidate_repo_digest') == records[number - 2].get('build', {}).get('candidate_repo_digest'):
                    raise NativeEvidenceError('revision did not change product source')
            previous_index = index
            proof['feedback_received'].append({'submission_number': number, 'feedback_digest': record['feedback_digest'],
                                               'rollout_event_index': index, 'call_id': call_id,
                                               'payload_echoed_in_native_output': bool(matches)})
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
