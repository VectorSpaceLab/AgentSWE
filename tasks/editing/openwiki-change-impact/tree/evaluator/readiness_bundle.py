"""Export current-run OpenWiki evidence; coordinator supplies terminal cleanup proof.

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
    if value.get('infrastructure_invalid') is not False:
        raise ValueError('raw execution does not explicitly exclude infrastructure failure')
    crc = value.get('case_resource_contract') or {}
    cleanup = crc.get('cleanup') or {}
    # D50 (2026-09-22): D49 (execution_evidence.py:286-288) made a launcher-observed
    # `candidate_timeout` a terminal Candidate outcome, but this gate still accepted only
    # the *scope* watchdog flag.  `_inner_case_deadline` (lower_agent_launcher.py:444)
    # deliberately arms the product deadline strictly inside the owned scope deadline, so
    # on the designed path the evaluator kills the product itself and
    # `case_resource_contract.timed_out` stays False -- the branch below was unreachable,
    # and a legitimate timed-out round could never be exported (0921b-gateway-r-023, dev_001
    # of candidate 2: classification candidate_timeout, no `exit_code`, timed_out False).
    # It is accepted only with the owned scope's own proof that nothing survived the kill;
    # a product that leaks a background process still fails this gate.
    killed_at_case_deadline = (
        value.get('classification') == 'candidate_timeout'
        and value.get('candidate_classification') == 'candidate_timeout'
        and value.get('product_started') is True
        and crc.get('valid') is True
        and (crc.get('timed_out') is True or (
            type(crc.get('process_exit_code')) is int
            and cleanup.get('complete') is True
            and cleanup.get('unit_absent') is True
            and str(cleanup.get('cgroup_events') or '').split()[:2] == ['populated', '0'])))
    terminal = type(value.get('exit_code')) is int or killed_at_case_deadline
    if not terminal:
        raise ValueError('raw execution does not establish terminal product process')
    verdict = classify_candidate_execution(value, case_id=case_id, candidate_digest=repository_digest)
    if verdict.get('classification') not in {'scoreable', 'candidate_zero'}:
        raise ValueError('raw execution failed task classifier: ' + str(verdict.get('reason')))
    return value


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
    native_path = run_dir / 'native_builder_evidence.json'
    native = read(native_path)
    state_path = run_dir / 'lifecycle/controller_state.json'
    state = read(state_path)
    freeze_path = run_dir / 'lifecycle/freeze_manifest.json'
    frozen = read(freeze_path)
    if native.get('valid') is not True or native.get('native_turn_completed') is not True:
        raise ValueError('native Builder has not completed verified two-round lifecycle')
    if frozen.get('readiness_profile') != PROFILE or frozen.get('current_binding') != trusted_binding or state.get('frozen') != frozen:
        raise ValueError('current binding or sealed readiness freeze mismatch')
    records = state['records']
    if len(records) != 2 or frozen.get('source_submission') != 2:
        raise ValueError('readiness requires the second accepted round')
    from harbor.native_builder_evidence import events, readiness_segment_streams
    from agentloop.protocol import tree_digest
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
        'public_lower': run_dir / 'public_broker/broker_stats-request-ledger.json',
        'hidden_lower': run_dir / 'pilot_hidden_broker/broker_stats-request-ledger.json',
        'result_judge': run_dir / 'result_judge_broker/container-judge-transport/broker_stats.json'}
    ledgers = {role: read(path) for role, path in ledger_paths.items()}
    # Public-round attribution: only requests made under an accepted round's native
    # context are accounted as public usage; traffic of non-consuming attempts stays in
    # the raw ledger and is listed explicitly as unattributed (0919-ds-001).
    accepted_contexts = set()
    for record in records:
        try:
            exec_path = Path(record['dev_cases']['dev_001']['execution']['execution_record_path'])
            accepted_contexts.add(read(exec_path.parent / 'logical-context.json')['context_id'])
        except (KeyError, OSError, ValueError, TypeError):
            pass
    for role, ledger in ledgers.items():
        unattributed = []
        if role.endswith('lower'):
            ids = [v['request_sha256'] for v in ledger['requests']]
            if role == 'public_lower':
                intents = ledger.get('logical_requests') or {}
                unattributed = [rid for rid in ids if (intents.get(rid) or {}).get('context_id') not in accepted_contexts]
                ids = [rid for rid in ids if rid not in set(unattributed)]
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
            known_tokens=sum(r['known_tokens'] for r in rows), **_recovered_usage_fields(rows), requests=rows,
            unattributed_requests={'count': len(unattributed), 'request_ids': unattributed,
                'reason': 'requests of non-consuming attempts; not bound to an accepted round; raw ledger retained'})
    previous = feedback = None
    for number, record in enumerate(records, 1):
        submission = Path(record['candidate_path'])
        if set(p.name for p in submission.iterdir()) != set(FILES) or tree_digest(submission) != record['candidate_digest']:
            raise ValueError('accepted submission changed')
        for name in FILES:
            w.raw(submission / name)
        public = record['dev_cases']['dev_001']
        if record.get('submission_consumed') is not True or public.get('infrastructure_invalid') is not False:
            raise ValueError('public record did not establish infrastructure validity')
        source = record['build']['readiness_source_immutability']
        if source.get('unchanged') is not True or source['before'] != source['after']:
            raise ValueError('controlled build changed product source')
        execution_path = Path(public['execution']['execution_record_path'])
        checked_execution(execution_path, 'dev_001', source['materialized_repository_digest'])
        repository = Path(record['build']['product_entry']).parent.parent
        if tree_digest(repository) != source['materialized_repository_digest']:
            raise ValueError('accepted public repository changed after execution')
        context = read(execution_path.parent / 'logical-context.json')['context_id']
        ids = [rid for rid, value in ledgers['public_lower']['logical_requests'].items() if value.get('context_id') == context]
        if not ids:
            raise ValueError('public native context has no matching lower requests')
        feedback_ref = {**w.raw(Path(record['feedback']['json'])),
            'digest_algorithm': 'canonical-json-without-feedback-digest-v1'}
        meta = {'builder_session_id': session, 'submission_number': number,
            'revision_of_candidate_digest': previous, 'feedback_digest': feedback}
        result = w.receipt(f'public/{number}/result', candidate_digest=record['candidate_digest'],
            case='dev_001', state='terminal', classification='execution_valid',
            source_evidence=w.raw(execution_path))
        receipt = {**meta, 'candidate_digest': record['candidate_digest'], 'case': 'dev_001',
            'state': 'terminal', 'classification': 'execution_valid', 'current_binding': trusted_binding,
            'transport': 'complete', 'build_exit_code': record['build']['pnpm_build']['exit_code'],
            'materialized_source_digest_before_build': source['before']['digest'],
            'materialized_source_digest_after_build': source['after']['digest'],
            'materialized_repository_digest': source['materialized_repository_digest'],
            'result': result, 'feedback_sha256': feedback_ref['sha256'], 'request_ids': ids,
            'source_state': w.raw(state_path), 'source_execution': w.raw(execution_path)}
        if number == 2:
            receipt['consumed_feedback'] = e['public_rounds'][0]['feedback']
            receipt['consumption_native_evidence'] = w.raw(native_path)
        e['public_rounds'].append({**meta, 'case': 'dev_001', 'candidate_digest': record['candidate_digest'],
            'submission_dir': ('raw/' + submission.relative_to(run_dir).as_posix()),
            'submission_sha256': {n: sha(submission / n) for n in FILES},
            'materialized_source_digest': source['before']['digest'],
            'materialized_repository_digest': source['materialized_repository_digest'],
            'feedback': feedback_ref, 'execution': w.receipt(f'public/{number}/execution', **receipt)})
        previous, feedback = record['candidate_digest'], record['feedback']['feedback_digest']
    e['freeze'] = w.receipt('freeze', **frozen, source_evidence=w.raw(freeze_path))
    hidden_path = run_dir / 'hidden-after-freeze-attestation.json'
    hidden = read(hidden_path)
    case = hidden['cases']['test_001']
    if (hidden.get('expected_cases') != ['test_001'] or hidden.get('executed_cases') != ['test_001']
            or set(hidden['cases']) != {'test_001'} or case.get('infrastructure_invalid') is not False
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
        inp = w.references(read(observed['input']['path']))
        out = w.references(read(observed['output']['path']))
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
