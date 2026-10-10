"""Codex task-specific readiness admission fences, independent of quality score."""
from pathlib import Path
import hashlib
import json


MAX_NATIVE_RECONNECTS = 40


def _reconnects_recovered(native):
    """Bounded, recorded reconnects are recovery, not a replay.

    A reconnect resumes a stream that disconnected before completion, so no
    Candidate, feedback, hidden result or usage is reused and every attempt stays
    announced in the rollout. The turn must still complete exactly once and the
    recovery must have happened inside that same turn.
    """
    termination = native.get('native_termination') or {}
    count = termination.get('native_retry_announcements', 0)
    if type(count) is not int or count < 0 or count > MAX_NATIVE_RECONNECTS:
        return False
    return not count or termination.get('recovered_in_same_turn') is True


def validate_freeze_inputs(controller, reason, evidence):
    from harbor.formal_one_stop import tree_digest, product_source_digest
    if reason != 'builder_exit' or controller.active_id or len(controller.records) != 2:
        raise RuntimeError('readiness freeze requires two completed rounds after Builder exit')
    if (not isinstance(evidence, dict) or evidence.get('exit_code') != 0
            or evidence.get('native_valid') is not True or evidence.get('builder_session_id') != controller.builder_session_id):
        raise RuntimeError('readiness requires successful native Builder exit evidence')
    native_path = controller.run_dir / 'native_builder_attestation.json'
    native = json.loads(native_path.read_bytes())
    if native.get('valid') is not True or native.get('native_turn_completed') is not True or native.get('native_thread_id') != controller.builder_session_id:
        raise RuntimeError('native session did not complete')
    if not _reconnects_recovered(native):
        raise RuntimeError('native reconnects exceeded the recorded recovery bound')
    previous_source = previous_candidate = previous_feedback = None
    for number, record in enumerate(controller.records, 1):
        snapshot = Path(record['candidate'])
        feedback = Path(record['feedback'])
        build_path = controller.run_dir / f'evaluations/submission_{number:03d}/build/build_result.json'
        build = json.loads(build_path.read_bytes())
        repository = build_path.parent / 'worktree'
        source = build.get('product_source_digest')
        if (record.get('state') != 'completed' or record.get('build_valid') is not True or
                record.get('builder_session_id') != controller.builder_session_id or
                set(record.get('results', {})) != {'dev_001'} or
                record['results']['dev_001'].get('readiness_execution_valid') is not True):
            raise RuntimeError('readiness accepted round is not structure/build/transport valid')
        if (tree_digest(snapshot) != record['candidate_digest'] or record['candidate_digest'] == previous_candidate or
                not source or source == previous_source or product_source_digest(repository) != source or
                tree_digest(repository) != build.get('candidate_repo_digest')):
            raise RuntimeError('readiness Candidate source changed or revision is not distinct')
        if hashlib.sha256(feedback.read_bytes()).hexdigest() != record['feedback_digest']:
            raise RuntimeError('readiness authoritative feedback bytes changed')
        if record.get('feedback_digest_ack') != previous_feedback:
            raise RuntimeError('readiness exact feedback chain mismatch')
        binary = Path(record['binary'])
        if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != record['binary_sha256']:
            raise RuntimeError('readiness built binary changed')
        previous_source, previous_candidate, previous_feedback = source, record['candidate_digest'], record['feedback_digest']
