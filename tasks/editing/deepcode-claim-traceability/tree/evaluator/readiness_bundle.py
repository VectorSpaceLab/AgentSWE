"""Export current-run DeepCode evidence; coordinator supplies terminal cleanup proof.

This exporter never dispatches, scores, cleans resources, or writes a shared gate.
Raw files are copied byte-for-byte; normalized receipts retain their source refs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
FILES = ('solution.patch', 'edit_report.json', 'run_report.json')


def unrecovered_native_errors(events):
    """Fatal native events: turn.failed, or an error that is not a recovered transport reconnect.
    A codex-CLI "Reconnecting..." error is tolerated only because the turn accounting still
    requires every started turn to complete (2026-09-19)."""
    return [v for v in events if v.get("type") == "turn.failed" or
            (v.get("type") == "error" and not str(v.get("message", "")).startswith("Reconnecting..."))]


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Writer:
    def __init__(self, run_dir, destination):
        self.run = run_dir.resolve()
        self.root = destination.resolve()
        self.root.mkdir(parents=True, exist_ok=False)

    def raw(self, path):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.run) or not path.is_file():
            raise ValueError('missing or foreign raw run evidence: ' + str(path))
        relative = path.resolve().relative_to(self.run)
        if any(part in {'builder_direct_provider.toml', 'builder_broker_provider.toml', '.env', 'auth.json'} for part in relative.parts):
            raise ValueError('credential/provider configuration cannot enter evidence bundle')
        destination = self.root / 'raw' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ValueError('raw evidence changed during export')
        shutil.copyfile(path, destination)
        return {'path': destination.relative_to(self.root).as_posix(), 'sha256': sha(destination)}

    def receipt(self, name, **value):
        value = {'run_id': self.run.name, 'owner': 'evaluator', **value}
        path = self.root / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
        return {'path': path.relative_to(self.root).as_posix(), 'sha256': sha(path)}

    def imported_ref(self, reference):
        path = Path(reference['path'])
        if sha(path) != reference['sha256']:
            raise ValueError('evaluator artifact changed before export')
        return self.raw(path)

    def references(self, value):
        if isinstance(value, dict):
            if (isinstance(value.get('path'), str) and Path(value['path']).is_absolute()
                    and isinstance(value.get('sha256'), str)):
                return {**value, **self.imported_ref(value)}
            return {key: self.references(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.references(child) for child in value]
        return value


def checked_execution(path, case_id, repository_digest):
    from execution_contract import classify_candidate_execution
    value = read(path)
    if value.get('infra_valid') is not True:
        raise ValueError('raw execution does not explicitly exclude infrastructure failure')
    terminal = type(value.get('controller_process_exit_code', value.get('lower_agent_exit_code'))) is int or (
        value.get('classification') == 'candidate_timeout' and
        value.get('case_resource_contract', {}).get('timed_out') is True)
    if not terminal:
        raise ValueError('raw execution does not establish terminal product process')
    verdict = classify_candidate_execution(value, case_id=case_id, candidate_digest=repository_digest)
    if verdict.get('classification') not in {'scoreable', 'candidate_zero'}:
        raise ValueError('raw execution failed task classifier: ' + str(verdict.get('reason')))
    return value


def superseded_attempt_request_ids(run_dir, number, ledger, accepted_context):
    """Lower requests of this round's attempts the controller rejected as infrastructure and re-ran.

    A rejected attempt (lifecycle/rejected_evaluations/<id>/record.json: round == number,
    accepted False, round_consumed False, dev[0].infra_valid False) ran under its own lower
    context on the same appended-once public ledger. Its rows belong to this round and to no
    other; an attempt that never reached the broker contributes nothing.
    """
    ids = []
    root = Path(run_dir) / 'lifecycle/rejected_evaluations'
    for record_path in (sorted(root.glob('*/record.json')) if root.is_dir() else []):
        record = read(record_path)
        if record.get('round') != number:
            continue
        if record.get('accepted') is not False or record.get('round_consumed') is not False:
            raise ValueError('rejected round %d attempt is recorded as neither rejected nor consumed' % number)
        attempt = (record.get('dev') or [{}])[0]
        if attempt.get('infra_valid') is not False:
            raise ValueError('rejected round %d attempt did not record an infrastructure classification' % number)
        context = read(Path(attempt['execution_record_path']).parent / 'logical-context.json')['identity']
        if context == accepted_context:
            raise ValueError('rejected round %d attempt shares the accepted lower context' % number)
        ids.extend(rid for rid, value in ledger['logical_requests'].items() if value.get('context_id') == context)
    return ids


def raw_lower_ledger(run, role):
    paths = list(run.glob('lower-broker-deepcode-*-' + role + '-*/stats.json'))
    if len(paths) != 1:
        raise ValueError('expected one durable ' + role + ' broker ledger')
    return paths[0]


def verify_rounds(records, native, binding):
    from evaluator.harness.candidate_adapter import tree_digest
    from v2_readiness import tree_digest as delivery_digest
    if binding.get('task') != 'deepcode' or native.get('builder_exit_code') != 0:
        raise ValueError('DeepCode binding/Builder exit invalid')
    if len(records) != 2:
        raise ValueError('exactly two accepted rounds required')
    for number, record in enumerate(records, 1):
        if record.get('round') != number or record.get('builder_session_id') != native['native_thread_id'] or record.get('accepted') is not True:
            raise ValueError('accepted round/session mismatch')
        if len(record.get('dev', [])) != 1 or record['dev'][0].get('case_id') != 'dev_001':
            raise ValueError('readiness requires dev_001 only')
        before = record['build']['readiness_source_immutability']
        if before.get('unchanged') is not True or before['before'] != before['after'] or record['build']['exit_code'] != 0:
            raise ValueError('controlled build source changed')
        if tree_digest(Path(record['candidate_path'])) != record['candidate_digest']:
            raise ValueError('accepted product changed')
        if delivery_digest(Path(record['delivery_path'])) != record['delivery_candidate_digest']:
            raise ValueError('accepted delivery changed')
    first, second = records
    if first['candidate_digest'] == second['candidate_digest'] or second['revision_of_candidate_digest'] != first['delivery_candidate_digest']:
        raise ValueError('distinct product revision binding invalid')


def _recovered_usage_fields(rows):
    """D13 (2026-09-19): tolerated upstream transport failures, recorded and never summed.

    The shared normalizer marks such a row; its usage stays unknown, so it is reported as
    unknown usage and named by identity rather than folded into known_tokens. A role with
    no tolerated row produces exactly the accounting this exporter produced before.
    """
    recovered = [r for r in rows if r.get('recovered_transport') is True]
    fields = {'unknown_usage': len(recovered), 'in_flight': 0}
    if recovered:
        fields['recovered_transport_failures'] = [{'request_id': r['request_id'], 'error': r['error']}
                                                  for r in recovered]
    return fields


def export(run_dir, destination, *, cleanup_receipt, trusted_binding, broker_normalizers):
    run_dir = Path(run_dir).resolve()
    cleanup_path = Path(cleanup_receipt)
    cleanup = read(cleanup_path)
    if cleanup.get('owner') != 'evaluator' or cleanup.get('run_id') != run_dir.name or any(
            cleanup.get(key) != 'terminal' for key in ('unit_state', 'harbor_state', 'builder_state')):
        raise ValueError('coordinator terminal cleanup proof is required before export')
    native_path = run_dir / 'builder_native_attestation.json'
    native = read(native_path)
    state_path = run_dir / 'controller_state.json'
    if not state_path.is_file(): state_path = run_dir / 'lifecycle/controller_state.json'
    state = read(state_path)
    freeze_path = run_dir / 'freeze_manifest.json'
    if not freeze_path.is_file(): freeze_path = run_dir / 'lifecycle/freeze_manifest.json'
    frozen = read(freeze_path)
    if native.get('valid') is not True or native.get('native_turn_completed') is not True:
        raise ValueError('native Builder has not completed verified two-round lifecycle')
    if frozen.get('readiness_profile') != PROFILE or frozen.get('current_binding') != trusted_binding or state.get('frozen') != frozen:
        raise ValueError('current binding or sealed readiness freeze mismatch')
    records = state['records']
    if len(records) != 2 or frozen.get('source_submission') != 2:
        raise ValueError('readiness requires the second accepted round')
    from harbor.native_builder_evidence import events, readiness_segment_streams, verify_native
    from evaluator.harness.candidate_adapter import tree_digest
    from v2_readiness import tree_digest as delivery_digest
    verify_rounds(records, native, trusted_binding)
    native_records = []
    for record in records:
        number = record['round']
        native_records.append({'builder_session_id': record['builder_session_id'],
            'candidate_digest': record['candidate_digest'],
            'feedback_path': str(state_path.parent / f'builder-feedback-candidate_{number:03d}.json'),
            'feedback_digest': read(state_path.parent / f'builder-feedback-candidate_{number:03d}.json')['feedback_digest'],
            'feedback_digest_ack': record.get('feedback_digest_consumed'),
            'build': {'candidate_repo_digest': record['candidate_digest']}})
    checked_native = verify_native(run_dir, native_records,
        read(run_dir / 'native_feedback_deliveries.json'), read(run_dir / 'native_thread_observations.json'))
    if not checked_native['valid'] or checked_native['source_files'] != native['source_files']:
        raise ValueError('native feedback/turn proof changed: ' + str(checked_native.get('errors')))
    for reference in native['source_files']:
        if sha(reference['path']) != reference['sha256']:
            raise ValueError('native source evidence changed after attestation')
    if tree_digest(Path(frozen['candidate_path'])) != frozen['candidate_digest']:
        raise ValueError('frozen repository changed before evidence export')
    segment_streams = readiness_segment_streams(run_dir, native)
    stream = segment_streams[-1]
    if any(sha(item) != ref['sha256'] for item, ref in zip(segment_streams, native['source_files'])):
        raise ValueError('native stream changed after verified terminal')
    parsed = [v for item in segment_streams for v in events(item.read_bytes())]
    turns = [v['usage'] for v in parsed if v.get('type') == 'turn.completed']
    # Fatal/unrecovered errors are judged on the terminal segment: a cut segment
    # is admissible only because readiness_segment_streams already proved it is
    # an evaluator-ledgered infrastructure cut that completed no turn.
    if len(turns) != 1 or unrecovered_native_errors(events(stream.read_bytes())):
        raise ValueError('native usage includes unknown retry/error or incomplete turns')
    w = Writer(run_dir, Path(destination))
    for reference in native['source_files']:
        w.imported_ref(reference)
    w.raw(run_dir / 'native_feedback_deliveries.json')
    w.raw(run_dir / 'native_thread_observations.json')
    w.raw(run_dir / 'builder_job_config.json')
    native_log = w.raw(stream)
    session = native['native_thread_id']
    e = {'profile': PROFILE, 'run_id': run_dir.name, 'current_binding': trusted_binding,
        'public_rounds': [], 'judges': {}, 'usage': {}}
    segment_refs = {'native_logs': [w.raw(v) for v in segment_streams[:-1]] + [native_log],
                    'builder_segments': w.raw(run_dir / 'builder_segments.json')} \
        if len(segment_streams) > 1 else {}
    e['builder_native'] = w.receipt('builder_native', builder_session_id=session,
        model='deepseek-flash', effort='max', state='terminal', thread_ids=[session],
        native_log=native_log, native_log_format='codex-harbor-mixed-jsonl-v1',
        source_evidence=w.raw(native_path), **segment_refs)
    e['usage']['builder'] = w.receipt('usage/builder', role='builder',
        transport='native_codex_direct', builder_broker_started=False,
        native_completed_turns=len(turns), native_reported_usage=turns,
        actual_upstream_requests=None, complete_provider_billing_claimed=False,
        native_usage_complete=True, known_tokens=sum(v['input_tokens'] + v['output_tokens'] for v in turns),
        builder_session_id=session, native_log=native_log)
    ledger_paths = {
        'public_lower': raw_lower_ledger(run_dir, 'public'),
        'hidden_lower': raw_lower_ledger(run_dir, 'hidden'),
        'result_judge': run_dir / 'brokers/result_judge-judge-transport/broker_stats.json'}
    ledgers = {role: read(path) for role, path in ledger_paths.items()}
    for role, ledger in ledgers.items():
        if role.endswith('lower'):
            ids = [v['request_sha256'] for v in ledger['requests']]
        elif role == 'result_judge':
            ids = [v['request_id'] for v in ledger['attempts']]
        else:
            ids = [v['response_id'] for v in ledger['attempts']]
        rows = [{**broker_normalizers[role](ledger, rid), 'provider_record': w.raw(ledger_paths[role])} for rid in ids]
        if not rows:
            raise ValueError('required role has no actual requests: ' + role)
        e['usage'][role] = w.receipt('usage/' + role, role=role, calls=len(rows),
            actual_upstream_attempts=sum(r['upstream_attempts'] for r in rows),
            successes=sum(r['state'] == 'success' for r in rows), failures=sum(r['state'] == 'failure' for r in rows),
            known_tokens=sum(r['known_tokens'] for r in rows), **_recovered_usage_fields(rows), requests=rows)
    previous = feedback = None
    attributed_public_ids = set()
    for number, record in enumerate(records, 1):
        submission = Path(record['delivery_path'])
        candidate = record['delivery_candidate_digest']
        if set(p.name for p in submission.iterdir()) != set(FILES) or delivery_digest(submission) != candidate:
            raise ValueError('accepted submission changed')
        for name in FILES:
            w.raw(submission / name)
        public = record['dev'][0]
        if record.get('accepted') is not True or public.get('infra_valid') is not True:
            raise ValueError('public record did not establish infrastructure validity')
        source = record['build']['readiness_source_immutability']
        if source['before'] != source['after']:
            raise ValueError('controlled build changed product source')
        execution_path = Path(public['execution_record_path'])
        if read(execution_path) != public:
            raise ValueError('accepted public record differs from raw execution')
        checked_execution(execution_path, 'dev_001', record['candidate_digest'])
        repository = Path(record['candidate_path'])
        if tree_digest(repository) != record['candidate_digest']:
            raise ValueError('accepted public repository changed after execution')
        context = read(execution_path.parent / 'logical-context.json')['identity']
        accepted_ids = [rid for rid, value in ledgers['public_lower']['logical_requests'].items() if value.get('context_id') == context]
        if not accepted_ids:
            raise ValueError('public native context has no matching lower requests')
        superseded_ids = superseded_attempt_request_ids(run_dir, number, ledgers['public_lower'], context)
        ids = superseded_ids + accepted_ids
        if len(set(ids)) != len(ids) or set(ids) & attributed_public_ids:
            raise ValueError('public round %d request attribution overlaps another round' % number)
        attributed_public_ids.update(ids)
        feedback_ref = {**w.raw(state_path.parent / f'builder-feedback-candidate_{number:03d}.json'),
            'digest_algorithm': 'canonical-json-without-feedback-digest-no-newline-v1'}
        meta = {'builder_session_id': session, 'submission_number': number,
            'revision_of_candidate_digest': previous, 'feedback_digest': feedback}
        result = w.receipt(f'public/{number}/result', candidate_digest=candidate,
            case='dev_001', state='terminal', classification='execution_valid',
            source_evidence=w.raw(execution_path))
        receipt = {**meta, 'candidate_digest': candidate, 'case': 'dev_001',
            'state': 'terminal', 'classification': 'execution_valid', 'current_binding': trusted_binding,
            'transport': 'complete', 'build_exit_code': record['build']['exit_code'],
            'materialized_source_digest_before_build': source['before'],
            'materialized_source_digest_after_build': source['after'],
            'materialized_repository_digest': record['candidate_digest'],
            'result': result, 'feedback_sha256': feedback_ref['sha256'], 'request_ids': ids,
            'superseded_attempt_request_ids': superseded_ids, 'accepted_attempt_request_ids': accepted_ids,
            'source_state': w.raw(state_path), 'source_execution': w.raw(execution_path)}
        if number == 2:
            receipt['consumed_feedback'] = e['public_rounds'][0]['feedback']
            receipt['consumption_native_evidence'] = w.raw(native_path)
        e['public_rounds'].append({**meta, 'case': 'dev_001', 'candidate_digest': candidate,
            'submission_dir': ('raw/' + submission.relative_to(run_dir).as_posix()),
            'submission_sha256': {n: sha(submission / n) for n in FILES},
            'materialized_source_digest': source['before'],
            'materialized_repository_digest': record['candidate_digest'],
            'feedback': feedback_ref, 'execution': w.receipt(f'public/{number}/execution', **receipt)})
        previous, feedback = candidate, read(state_path.parent / f'builder-feedback-candidate_{number:03d}.json')['feedback_digest']
    unbound = set(ledgers['public_lower']['logical_requests']) - attributed_public_ids
    if unbound:
        raise ValueError('public lower requests attributed to no round: %d' % len(unbound))
    e['freeze'] = w.receipt('freeze', **frozen, source_evidence=w.raw(freeze_path))
    hidden_path = run_dir / 'lifecycle/hidden-after-freeze-attestation.json'
    hidden = read(hidden_path)
    cases = hidden['cases']
    if len(cases) != 1 or cases[0].get('case_id') != 'test_001':
        raise ValueError('hidden smoke must contain only test_001')
    case = cases[0]
    if (hidden.get('expected_cases') != ['test_001'] or hidden.get('executed_cases') != ['test_001']
            or case.get('infra_valid') is not True
            or hidden.get('all_cases_started_after_freeze') is not True or hidden.get('frozen_digest_stable') is not True):
        raise ValueError('hidden smoke is not a completed valid single-case execution')
    checked_execution(Path(case['execution_record_path']), 'test_001', frozen['candidate_digest'])
    hr = w.receipt('hidden_result', case='test_001', state='terminal',
        candidate_digest=frozen['candidate_digest'], source_evidence=w.raw(Path(case['execution_record_path'])))
    e['hidden_smoke'] = w.receipt('hidden', case='test_001', state='terminal', classification='execution_valid',
        transport='complete', readiness_only=True, builder_access=False, freeze_sha256=e['freeze']['sha256'],
        result=hr, request_ids=list(ledgers['hidden_lower']['logical_requests']), source_evidence=w.raw(hidden_path))
    # Code axis retired 2026-09-19 (Result-only): explicit skip receipts, accepted by v2_readiness.
    _skip_policy = {'id': 'edit-code-axis-retired-2026-09-19', 'evaluation_state': 'skipped_by_policy', 'reason': 'Result-only evaluation; Code judge not dispatched'}
    _skip_source = w.raw(run_dir / 'readiness_scoring/code_observation.json')
    e['usage']['code_judge'] = w.receipt('usage/code_judge', role='code_judge', skipped_by_policy=True,
        policy=_skip_policy, calls=0, actual_upstream_attempts=0, successes=0, failures=0, known_tokens=0,
        unknown_usage=0, in_flight=0, requests=[], source_evidence=_skip_source)
    e['judges']['code'] = w.receipt('judges/code', role='code', state='skipped_by_policy', formal=False,
        judge_session_id=None, request_id=None, freeze_sha256=e['freeze']['sha256'], policy=_skip_policy,
        source_evidence=_skip_source)
    for role in ('result',):
        path = run_dir / 'readiness_scoring' / (role + '_observation.json')
        observed = read(path)
        if observed.get('freeze_sha256') != sha(freeze_path):
            raise ValueError('judge observation is not bound to original freeze bytes')
        for key in ('input', 'output'):
            w.imported_ref(observed[key])
        inp = w.references(read(observed['input']['path']))
        out = w.references(read(observed['output']['path']))
        # The shared judge adds `ceiling_assessments` when a score-cap contract is
        # supplied; the shared bundle validator checks the payload against a fixed
        # key set, so the ceilings live beside the payload (the raw judge output
        # remains the receipt's source evidence).
        _payload = out.get('payload') if isinstance(out.get('payload'), dict) else None
        if _payload is not None and 'ceiling_assessments' in _payload:
            out['ceiling_assessments'] = _payload.pop('ceiling_assessments')
        inp['freeze_sha256'] = e['freeze']['sha256']
        input_ref = w.receipt('judges/' + role + '_input', **inp, source_evidence=w.raw(Path(observed['input']['path'])))
        output_ref = w.receipt('judges/' + role + '_output', **out, source_evidence=w.raw(Path(observed['output']['path'])))
        observed.update(freeze_sha256=e['freeze']['sha256'], input=input_ref, output=output_ref,
            source_evidence=w.raw(path))
        e['judges'][role] = w.receipt('judges/' + role, **observed)
    # The coordinator owns OS/resource observations. Its normalized cleanup
    # receipt and immutable referenced bytes must already be inside this run.
    def copy_cleanup_ref(reference):
        value = read(reference['path'])
        if sha(reference['path']) != reference['sha256']:
            raise ValueError('cleanup source reference changed')
        return w.receipt('cleanup/' + Path(reference['path']).stem, **value,
            source_evidence=w.raw(Path(reference['path'])))
    cleanup['before'] = copy_cleanup_ref(cleanup['before'])
    cleanup['after'] = copy_cleanup_ref(cleanup['after'])
    cleanup['stats'] = {rid: copy_cleanup_ref(ref) for rid, ref in cleanup['stats'].items()}
    e['cleanup'] = w.receipt('cleanup', **cleanup, source_evidence=w.raw(cleanup_path))
    manifest = w.root / 'manifest.json'
    manifest.write_text(json.dumps(e, sort_keys=True, indent=2) + '\n')
    return {'bundle_root': str(w.root), 'manifest_sha256': sha(manifest),
        'pipeline_ready': False, 'admission_required': True}
