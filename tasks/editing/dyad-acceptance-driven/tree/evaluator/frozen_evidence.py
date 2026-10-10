"""Pure checks for Dyad's evaluator-owned frozen source and execution times."""
from datetime import datetime
import hashlib
import json
import os
import stat
from pathlib import Path

from harbor.builder_protocol import tree_digest


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('missing execution timestamp')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('execution timestamp lacks timezone')
    return parsed


def inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError) as exc:
        raise ValueError('unresolvable or cyclic evidence path') from exc


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def write_once(path, value):
    """Preserve incomplete/unknown observations; even an empty file blocks."""
    data = value if isinstance(value, str) else json.dumps(value, sort_keys=True) + '\n'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    fsync_directory(Path(path).parent)


def modes_digest(root):
    rows = []
    for path in (Path(root), *Path(root).rglob('*')):
        if path.is_symlink():
            if path.readlink().is_absolute() or not inside(path, root):
                raise ValueError('source symlink escapes its tree')
            continue
        if not (path.is_file() or path.is_dir()):
            raise ValueError('source is not a regular tree')
        rows.append((str(path.relative_to(root)), stat.S_IMODE(path.stat().st_mode)))
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def validate_freeze(freeze, run_dir):
    """Require original v3 manifest, seal, accepted history and immutable bytes.

    Older manifests are left intact. Missing identity/timing evidence must not
    be manufactured by a scoring adapter.
    """
    run = Path(run_dir).resolve()
    owner = run / 'lifecycle'
    document = owner / 'freeze_manifest.json'
    seal = owner / 'freeze_manifest.sha256'
    records_path = owner / 'dev_lifecycle.json'
    frozen = owner / 'frozen_candidate'
    for path in (owner, document, seal, records_path, frozen):
        if path.is_symlink() or not inside(path, run):
            raise ValueError('frozen evidence path escapes its owner or is a symlink')
    if json.loads(document.read_text()) != freeze:
        raise ValueError('freeze differs from original manifest')
    if (freeze.get('schema_version') != 'dyad-agentloop-freeze-v3'
            or freeze.get('readonly') is not True
            or freeze.get('hidden_only_after_freeze') is not True
            or Path(str(freeze.get('candidate_path', ''))).resolve() != frozen.resolve()
            or seal.read_text().strip() != sha256(document)):
        raise ValueError('missing or inconsistent sealed frozen identity')
    for path in (document, seal, records_path, frozen, *frozen.rglob('*')):
        if path.is_symlink():
            if path.readlink().is_absolute() or not inside(path, frozen):
                raise ValueError('frozen symlink escapes its tree')
        elif not (path.is_file() or path.is_dir()) or path.stat().st_mode & 0o222:
            raise ValueError('frozen source or seal is not readonly regular evidence')
    digest = tree_digest(frozen)
    if digest != freeze.get('candidate_digest') or digest != freeze.get('candidate_digest_after_readonly'):
        raise ValueError('frozen lifecycle digest mismatch')
    if sha256(records_path) != freeze.get('accepted_history_sha256'):
        raise ValueError('accepted history bytes differ from sealed freeze')
    records = json.loads(records_path.read_text())
    if (not isinstance(records, list) or not records
            or any(not isinstance(item, dict) for item in records)
            or len(records) != freeze.get('accepted_submission_count')
            or [item.get('submission_number') for item in records] != list(range(1, len(records) + 1))
            or [item.get('candidate_digest') for item in records] != freeze.get('accepted_candidate_digests')
            or freeze.get('source_submission') != len(records)
            or freeze.get('source_submission_id') != f'candidate-{len(records):03d}'
            or freeze.get('feedback_chain_complete') is not True
            or freeze.get('feedback_consumed') is not True
            or not 1 <= len(records) <= freeze.get('max_dev_rounds', 0) <= 10
            or len(set(freeze.get('accepted_candidate_digests', []))) != len(records)):
        raise ValueError('freeze does not match accepted history')
    frozen_at = timestamp(freeze.get('frozen_at'))
    for index, record in enumerate(records):
        feedback = Path(str(record.get('feedback_path', '')))
        if (record.get('session_id') != freeze.get('builder_session_id')
                or record.get('connection_id') != freeze.get('builder_connection_id')
                or record.get('submission_consumed') is not True
                or feedback.is_symlink() or not inside(feedback, run) or not feedback.is_file()
                or sha256(feedback) != record.get('feedback_digest')
                or timestamp(record.get('accepted_at')) > frozen_at
                or (not index and record.get('feedback_digest_ack') is not None)
                or (index and record.get('feedback_digest_ack') != records[index - 1].get('feedback_digest'))):
            raise ValueError('accepted session or feedback chain differs from freeze')
        payload = json.loads(feedback.read_text())
        if (payload.get('builder_session_id') != record['session_id']
                or payload.get('builder_connection_id') != record['connection_id']
                or payload.get('source_candidate_digest') != record['candidate_digest']
                or payload.get('source_submission') != record['submission_number']
                or payload.get('public_inventory') != freeze.get('public_cases')
                or [item.get('case_id') for item in record.get('dev', [])] != freeze.get('public_cases')):
            raise ValueError('feedback payload differs from accepted product/inventory')
    latest = records[-1]
    accepted = Path(str(latest.get('candidate_path', '')))
    if (latest.get('candidate_digest') != digest
            or not inside(accepted, run) or accepted.resolve() == frozen.resolve()
            or accepted.is_symlink() or not accepted.is_dir()
            or tree_digest(accepted) != digest
            or str(accepted.resolve()) != freeze.get('accepted_candidate_path')
            or modes_digest(accepted) != freeze.get('accepted_modes_sha256')
            or freeze.get('feedback_digest') != latest.get('feedback_digest')):
        raise ValueError('frozen source differs from the last accepted submission')
    return document, frozen


def hidden_timing_errors(hidden, freeze, run_dir, selected):
    """Independently check durable per-case observations against sealed freeze."""
    errors = []
    try:
        document, frozen = validate_freeze(freeze, run_dir)
        frozen_at = timestamp(freeze['frozen_at'])
        suite_start = timestamp(hidden.get('hidden_started_at'))
        suite_end = timestamp(hidden.get('hidden_finished_at'))
        intent_path = Path(run_dir) / 'hidden_after_freeze/execution_intent.json'
        intent = json.loads(intent_path.read_text())
        if (hidden.get('schema_version') != 'dyad-agentloop-hidden-after-freeze-attestation-v2'
                or hidden.get('freeze_manifest_sha256') != sha256(document)
                or hidden.get('freeze_manifest') != str(document)
                or not frozen_at <= suite_start <= suite_end
                or intent_path.is_symlink() or intent_path.stat().st_mode & 0o222
                or intent.get('expected_cases') != list(selected)
                or intent.get('freeze_manifest_sha256') != sha256(document)
                or intent.get('candidate_digest') != freeze['candidate_digest']
                or intent.get('started_at') != hidden.get('hidden_started_at')
                or hidden.get('expected_cases') != list(selected)
                or hidden.get('executed_cases') != list(selected)):
            raise ValueError('hidden inventory or freeze binding mismatch')
        cases = hidden.get('cases')
        if not isinstance(cases, dict) or list(cases) != list(selected):
            raise ValueError('hidden case records are missing or unordered')
        for case_id in selected:
            record = cases[case_id]
            path = Path(run_dir) / 'hidden_after_freeze' / case_id / 'execution_timing.json'
            if path.is_symlink() or not inside(path, run_dir) or path.stat().st_mode & 0o222:
                raise ValueError('execution timing escapes its owner')
            timing = json.loads(path.read_text())
            if (record.get('execution_timing_sha256') != sha256(path)
                    or record.get('execution_timing_path') != str(path)
                    or timing.get('case_id') != case_id
                    or timing.get('freeze_manifest_sha256') != sha256(document)
                    or not suite_start <= timestamp(timing.get('started_at')) <= timestamp(timing.get('finished_at')) <= suite_end
                    or timing.get('frozen_digest_before') != freeze['candidate_digest']
                    or timing.get('frozen_digest_after') != freeze['candidate_digest']):
                raise ValueError('hidden case timing or frozen source mismatch: ' + case_id)
            begin_path = path.with_name('execution_started.json')
            runner_path = path.with_name('runner_execution.json')
            for evidence in (begin_path, runner_path):
                if evidence.is_symlink() or not inside(evidence, run_dir) or evidence.stat().st_mode & 0o222:
                    raise ValueError('hidden execution evidence is not immutable and owned')
            begin = json.loads(begin_path.read_text())
            runner = json.loads(runner_path.read_text())
            command = begin.get('command', [])
            if (timing.get('execution_started_sha256') != sha256(begin_path)
                    or timing.get('runner_execution_sha256') != sha256(runner_path)
                    or begin.get('started_at') != timing.get('started_at')
                    or begin.get('case_id') != case_id
                    or begin.get('freeze_manifest_sha256') != sha256(document)
                    or runner != record.get('runner_execution')
                    or runner.get('command') != command
                    or command[command.index('--case-id') + 1] != case_id
                    or Path(command[command.index('--output') + 1]) != path.with_name('result.json')):
                raise ValueError('case timing does not bind the observed runner: ' + case_id)
        if tree_digest(frozen) != freeze['candidate_digest']:
            raise ValueError('frozen source changed after hidden execution')
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, AttributeError) as exc:
        errors.append('Dyad hidden evidence invalid: ' + str(exc))
    return errors
