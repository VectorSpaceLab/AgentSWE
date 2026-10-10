"""Aider readiness fences on the actual delivery, product, feedback and native exit."""
import hashlib
import json
from pathlib import Path

PROFILE='single-dev-two-round-hidden-smoke-v1'
FILES=('solution.patch','edit_report.json','run_report.json')
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


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_bytes())

def validate_delivery(controller, candidate, acknowledgement):
    from harbor.agentloop_controller import digest
    if set(p.name for p in candidate.iterdir()) != set(FILES) or any((candidate/n).is_symlink() or not (candidate/n).is_file() for n in FILES):
        raise ValueError('readiness requires exactly three regular delivery files')
    if len(controller.records)>=2 or controller.frozen:
        raise ValueError('readiness accepts exactly two rounds')
    previous=controller.records[-1] if controller.records else None
    expected={'builder_session_id':getattr(controller,'builder_session_id',None),'submission_number':len(controller.records)+1,
        'revision_of_candidate_digest':previous['candidate_digest'] if previous else None,
        'feedback_digest':previous['feedback_digest'] if previous else None}
    metadata=read(candidate/'run_report.json')
    if not expected['builder_session_id'] or any(k not in metadata or metadata[k]!=v for k,v in expected.items()):
        raise ValueError('readiness metadata mismatch')
    if acknowledgement!=expected['feedback_digest']:
        raise ValueError('readiness feedback acknowledgement mismatch')
    if previous:
        if sha(previous['feedback_path'])!=previous['feedback_digest']:
            raise ValueError('authoritative first feedback changed')
        if sha(candidate/'solution.patch')==previous['patch_sha256'] or digest(candidate)==previous['candidate_digest']:
            raise ValueError('readiness requires distinct product revision')
        response=read(candidate/'edit_report.json').get('feedback_response')
        if not isinstance(response,str) or not response.strip():
            raise ValueError('readiness revision must explain feedback response')

def validate_freeze(controller, reason, exit_evidence):
    from harbor.agentloop_controller import digest
    from harbor.native_builder_evidence import verify_native
    if reason!='builder_exit' or not isinstance(exit_evidence,dict) or exit_evidence.get('exit_code')!=0 or exit_evidence.get('native_valid') is not True:
        raise ValueError('readiness freeze requires successful native exit')
    if len(controller.records)!=2 or any(r.get('accepted') is not True or controller._dev_has_infrastructure_failure(r)[0] for r in controller.records):
        raise ValueError('readiness requires exactly two valid rounds')
    run=controller.run_dir.parent
    records=controller.records
    native=verify_native(run,[{**r,'build':{'candidate_repo_digest':r['native_product_source_digest']}} for r in records],
        read(run/'builder_feedback_deliveries.json'),read(run/'builder_native_observations.json'))
    if not native.get('valid') or not native.get('native_turn_completed') or not _reconnects_recovered(native) or read(run/'native_builder_attestation.json')!=native:
        raise ValueError('invalid native uninterrupted turn attestation')
    if len({r['native_product_source_digest'] for r in records})!=2:
        raise ValueError('two distinct Aider products required')
    previous=None
    for number,r in enumerate(records,1):
        source=Path(r['materialized_path']);delivery=Path(r['candidate_path'])
        if digest(source)!=r['readiness_materialized_digest'] or digest(source/'aider')!=r['native_product_source_digest'] or digest(delivery)!=r['candidate_digest'] or sha(r['build_result_path'])!=r['build_result_sha256'] or sha(r['feedback_path'])!=r['feedback_digest']:
            raise ValueError('accepted Aider source/delivery/build/feedback changed')
        report=read(delivery/'run_report.json')
        expected={'builder_session_id':native['native_thread_id'],'submission_number':number,
            'revision_of_candidate_digest':previous['candidate_digest'] if previous else None,
            'feedback_digest':previous['feedback_digest'] if previous else None}
        if any(k not in report or report[k]!=v for k,v in expected.items()) or r.get('feedback_digest_ack')!=expected['feedback_digest']:
            raise ValueError('accepted metadata/feedback chain changed')
        previous=r
