"""Evaluator-owned readiness checks; no dispatch or shared state mutation."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
FILES = ('solution.patch', 'edit_report.json', 'run_report.json')
META = ('builder_session_id', 'submission_number', 'revision_of_candidate_digest', 'feedback_digest')
SHARED = Path('@@AGENTSWE_EDITING_CONTROL@@')

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def canonical_feedback(value):
    return hashlib.sha256((json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode()).hexdigest()

def delivery_digest(root):
    # Admission uses its shared tree algorithm, separately from DeepTutor's product hash.
    shared_modules()
    from v2_readiness import tree_digest
    return tree_digest(Path(root))

def shared_modules():
    if str(SHARED) not in sys.path:
        sys.path.insert(0, str(SHARED))
    import presubmit_validator
    return presubmit_validator

def validate_metadata(delivery, expected):
    parser = shared_modules()
    paths = parser.paths(Path(delivery) / 'solution.patch')
    parser.report_ok(Path(delivery), paths, expected)
    if set(p.name for p in Path(delivery).iterdir()) != set(FILES):
        raise ValueError('readiness delivery requires exactly three files')
    if any((Path(delivery)/name).is_symlink() or not (Path(delivery)/name).is_file() for name in FILES):
        raise ValueError('readiness delivery files must be regular')

def validate_binding(value):
    if not isinstance(value, dict) or value.get('task') != 'deeptutor':
        raise ValueError('readiness binding task mismatch')
    for key in ('source_digest','contract_digest','registry_digest'):
        raw=value.get(key)
        if not isinstance(raw,str) or len(raw)!=64 or any(c not in '0123456789abcdef' for c in raw):
            raise ValueError('readiness binding lacks '+key)
    return dict(value)

def execution_valid(record):
    delta=record.get('broker_delta') or {}
    return (record.get('terminal') is True and record.get('infra_valid') is True
        and record.get('lower_agent_executed') is True
        and int(delta.get('successful_calls',0) or 0)>0
        and not any(int(delta.get(k,0) or 0) for k in
            ('failures','provider_failures','upstream_failures','unknown_usage','in_flight')))

def verify_rounds(records, session, binding, exit_evidence):
    validate_binding(binding)
    if not isinstance(exit_evidence,dict) or exit_evidence.get('builder_exit_code') != 0 or exit_evidence.get('valid') is not True:
        raise RuntimeError('readiness freeze requires verified successful Builder exit')
    if len(records)!=2:
        raise RuntimeError('readiness freeze requires exactly two accepted rounds')
    for number, record in enumerate(records,1):
        if record.get('round')!=number or record.get('session_id')!=session or record.get('consumed') is not True:
            raise RuntimeError('readiness accepted round/session mismatch')
        if set(record.get('dev_results',{}))!={'dev_001'} or not execution_valid(record['dev_results']['dev_001']):
            raise RuntimeError('readiness requires complete public lower execution')
        imm=record.get('readiness_build',{})
        if imm.get('source_before')!=imm.get('source_after') or not imm.get('source_before') or imm.get('build_exit_code')!=0:
            raise RuntimeError('readiness controlled build immutability missing')
        delivery=Path(record['delivery_path'])
        if delivery_digest(delivery)!=record['delivery_candidate_digest']:
            raise RuntimeError('accepted delivery changed')
    first,second=records
    if first['candidate_digest']==second['candidate_digest'] or first['readiness_build']['source_before']==second['readiness_build']['source_before']:
        raise RuntimeError('revision has no materialized product change')
    if sha(Path(first['delivery_path'])/'solution.patch')==sha(Path(second['delivery_path'])/'solution.patch'):
        raise RuntimeError('revision has no patch change')
    if second.get('revision_of_candidate_digest')!=first['delivery_candidate_digest']:
        raise RuntimeError('revision delivery identity mismatch')
    if second.get('feedback_digest_consumed')!=first.get('issued_feedback_digest'):
        raise RuntimeError('revision did not consume exact F1')
    for number,record in enumerate(records,1):
        previous=records[number-2] if number>1 else None
        validate_metadata(Path(record['delivery_path']),{'builder_session_id':session,'submission_number':number,
            'revision_of_candidate_digest':previous['delivery_candidate_digest'] if previous else None,
            'feedback_digest':previous['issued_feedback_digest'] if previous else None})
