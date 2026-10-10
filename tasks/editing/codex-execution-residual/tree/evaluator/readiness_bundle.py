"""Export current-run Codex evidence; coordinator supplies terminal cleanup proof.

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


def export(run_dir, destination, *, cleanup_receipt, trusted_binding, broker_normalizers):
    from harbor.formal_one_stop import tree_digest, product_source_digest
    from harbor.native_builder_evidence import events, readiness_segment_streams, verify_native
    from evaluator.formal_finalize import execution_verdict
    run_dir = Path(run_dir).resolve()
    cleanup_path = Path(cleanup_receipt)
    cleanup = read(cleanup_path)
    if cleanup.get('run_id') != run_dir.name or cleanup.get('owner') != 'evaluator' or any(
            cleanup.get(key) != 'terminal' for key in ('unit_state','harbor_state','builder_state')):
        raise ValueError('terminal coordinator cleanup required')
    state_path, freeze_path = run_dir/'dev_lifecycle.json', run_dir/'freeze_manifest.json'
    records, frozen = read(state_path), read(freeze_path)
    if (len(records) != 2 or frozen.get('readiness_profile') != PROFILE or
            frozen.get('current_binding') != trusted_binding or frozen.get('source_submission') != 2):
        raise ValueError('fresh two-round readiness freeze missing')
    if tree_digest(Path(frozen['path'])) != frozen['candidate_digest'] or frozen['candidate_digest'] != records[-1]['candidate_digest']:
        raise ValueError('frozen delivery changed')
    converted, builds = [], []
    for number, record in enumerate(records, 1):
        path = run_dir/f'evaluations/submission_{number:03d}/build/build_result.json'
        build = read(path)
        repository = path.parent/'worktree'
        if (build.get('cargo_build', {}).get('exit_code') != 0 or
                build.get('product_source_digest_stage') != 'after_patch_apply_before_compilation' or
                tree_digest(repository) != build['candidate_repo_digest'] or product_source_digest(repository) != build['product_source_digest']):
            raise ValueError('materialized public source changed')
        builds.append(build)
        converted.append({**record, 'feedback_path': record['feedback'], 'feedback_format': 'text', 'build': build})
    native = verify_native(run_dir, converted, read(run_dir/'native_feedback_deliveries.json'), read(run_dir/'native_thread_observations.json'))
    if not native['valid'] or not native.get('native_turn_completed') or not _reconnects_recovered(native):
        raise ValueError('native single-turn feedback chain is invalid or unrecovered')
    native_path = run_dir/'native_builder_attestation.json'
    if read(native_path) != native:
        raise ValueError('terminal native evidence differs from current revalidation')
    segment_streams = readiness_segment_streams(run_dir, native)
    stream = segment_streams[-1]
    turns = [event['usage'] for item in segment_streams for event in events(item.read_bytes())
             if event.get('type') == 'turn.completed']
    if len(turns) != 1:
        raise ValueError('one uninterrupted native turn required')
    w = Writer(run_dir, Path(destination))
    session = native['native_thread_id']
    native_ref = w.raw(stream)
    e = {'profile': PROFILE, 'run_id': run_dir.name, 'current_binding': trusted_binding,
        'public_rounds': [], 'judges': {}, 'usage': {}}
    segment_refs = {'native_logs': [w.raw(v) for v in segment_streams[:-1]] + [native_ref],
                    'builder_segments': w.raw(run_dir / 'builder_segments.json')} \
        if len(segment_streams) > 1 else {}
    e['builder_native'] = w.receipt('builder_native', builder_session_id=session, model='deepseek-flash',
        effort='max', state='terminal', thread_ids=[session], native_log=native_ref,
        native_log_format='codex-harbor-mixed-jsonl-v1', source_evidence=w.raw(native_path), **segment_refs)
    e['usage']['builder'] = w.receipt('usage/builder', role='builder', transport='native_codex_direct',
        builder_broker_started=False, native_completed_turns=1, native_reported_usage=turns,
        actual_upstream_requests=None, complete_provider_billing_claimed=False, native_usage_complete=True,
        known_tokens=sum(v['input_tokens']+v['output_tokens'] for v in turns), builder_session_id=session, native_log=native_ref)
    role_ids = {}
    def access_json(reference):
        path = w.root/reference['path']
        if sha(path) != reference['sha256']:
            raise ValueError('bundled artifact changed')
        return read(path)
    def access_directory(reference):
        path = w.root/reference['path']
        if tree_digest(path) != reference['tree_digest']:
            raise ValueError('bundled receipt tree changed')
        return path
    access = {'run_id': run_dir.name, 'directory': access_directory, 'read_json': access_json}
    # Public-round attribution: only requests bound to the two accepted submissions are
    # accounted as public usage; traffic of infrastructure attempts / rejected deliveries
    # stays in the raw receipt tree and is listed explicitly as unattributed.
    attributed_public = set()
    for number in range(1, len(records) + 1):
        binding_path = run_dir/f'evaluations/submission_{number:03d}/readiness_request_binding.json'
        if binding_path.is_file():
            attributed_public.update(read(binding_path).get('request_ids', []))
    for role, broker_role in [('public_lower','public'),('hidden_lower','hidden')]:
        origin = run_dir/f'brokers/{broker_role}-lower-transport/lower_requests'
        if origin.is_symlink() or any(p.is_symlink() or (not p.is_dir() and not p.is_file()) for p in origin.rglob('*')):
            raise ValueError('raw request receipts contain non-regular entries')
        receipt_directory = w.root/'raw'/origin.relative_to(run_dir)
        receipt_directory.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(origin, receipt_directory)
        tree = tree_digest(origin)
        if tree_digest(receipt_directory) != tree:
            raise ValueError('original receipt directory changed during export')
        cid = (run_dir/f'brokers/{broker_role}.cid').read_text().strip()
        if cid not in cleanup['owned_resources']:
            raise ValueError('receipt broker absent from actual coordinator cleanup')
        terminal = w.receipt('usage/'+role+'_terminal', state='terminal', process_reaped=True,
            receipt_tree_digest=tree, container_id=cid, source_evidence=w.raw(cleanup_path))
        index = {'schema_version': 'agentswe-evaluator-receipt-index/v1', 'task': 'codex',
            'owner': 'evaluator', 'run_id': run_dir.name,
            'receipt_directory': {'path': receipt_directory.relative_to(w.root).as_posix(), 'tree_digest': tree},
            'broker_terminal': terminal}
        index_ref = w.receipt('usage/'+role+'_index', **index)
        ids_all = sorted(p.name for p in origin.iterdir() if p.is_dir())
        unattributed = [] if role != 'public_lower' else [rid for rid in ids_all if rid not in attributed_public]
        ids = [rid for rid in ids_all if rid not in set(unattributed)]
        # Release fix (RC; differs from the paper's admission behaviour): an unattributed receipt that is failed,
        # recovered or incomplete -- e.g. a provider RemoteDisconnected during an infrastructure attempt -- stays in
        # the raw receipt tree (the shared ReceiptNormalizer no longer lets it block other requests) and is listed
        # here with the sha256 of each of its files. Attributed requests are normalized one by one and still block.
        complete_names = {'intent.json', 'upstream_started.json', 'completed.json', 'response.bin'}
        excluded_records = []
        for rid in unattributed:
            present = {f.name for f in (origin/rid).iterdir()}
            if not (complete_names <= present and present <= complete_names | {'upstream.raw'}):
                excluded_records.append({'request_id': rid,
                    'path': (receipt_directory/rid).relative_to(w.root).as_posix(),
                    'files': {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((origin/rid).iterdir())},
                    'reason': 'failed, recovered or incomplete receipt of an unattributed attempt'})
        rows = [{**broker_normalizers[role].normalize_with_artifacts(index, rid, access),
                 'provider_record': index_ref} for rid in ids]
        role_ids[role] = ids
        if not rows:
            raise ValueError('lower role has no original request receipts')
        e['usage'][role] = w.receipt('usage/'+role, role=role, calls=len(rows), actual_upstream_attempts=len(rows),
            successes=len(rows), failures=0, known_tokens=sum(row['known_tokens'] for row in rows),
            unknown_usage=0, in_flight=0, requests=rows,
            unattributed_requests={'count': len(unattributed), 'request_ids': unattributed,
                'reason': 'receipts of infrastructure attempts or rejected deliveries; not bound to an accepted round; '
                          'raw receipts retained in receipt_directory'},
            excluded_failed_requests={'count': len(excluded_records), 'requests': excluded_records,
                'reason': 'release fix: failed/recovered/incomplete receipts of unattributed attempts are listed and '
                          'no longer block the bundle'})
    for role,path in [('result_judge',run_dir/'brokers/judge-judge-transport/broker_stats.json')]:
        raw = read(path)
        ids = [row['request_id' if role == 'result_judge' else 'response_id'] for row in raw['attempts']]
        rows = [{**broker_normalizers[role](raw,rid), 'provider_record':w.raw(path)} for rid in ids]
        e['usage'][role] = w.receipt('usage/'+role, role=role, calls=len(rows), actual_upstream_attempts=sum(r['upstream_attempts'] for r in rows),
            successes=len(rows), failures=0, known_tokens=sum(r['known_tokens'] for r in rows), unknown_usage=0, in_flight=0, requests=rows)
    previous = feedback = None
    for number,(record,build) in enumerate(zip(records,builds),1):
        submission = Path(record['candidate'])
        if tree_digest(submission) != record['candidate_digest'] or set(p.name for p in submission.iterdir()) != set(FILES):
            raise ValueError('accepted delivery changed')
        for name in FILES:
            w.raw(submission/name)
        execution_path = run_dir/f'evaluations/submission_{number:03d}/dev_001/result.json'
        execution = read(execution_path)
        verdict,_ = execution_verdict(execution,'dev_001',record['candidate_digest'])
        if verdict.get('classification') not in {'scoreable','candidate_zero'} or record.get('build_valid') is not True:
            raise ValueError('public infrastructure or build invalid')
        request_binding_path = run_dir/f'evaluations/submission_{number:03d}/readiness_request_binding.json'
        request_binding = read(request_binding_path)
        if request_binding.get('candidate_digest') != record['candidate_digest'] or set(request_binding['request_ids']) != set(request_binding['after'])-set(request_binding['before']):
            raise ValueError('public lower request binding invalid')
        feedback_ref = {**w.raw(Path(record['feedback'])), 'digest_algorithm': 'sha256-bytes-v1'}
        metadata = {'builder_session_id':session,'submission_number':number,'revision_of_candidate_digest':previous,'feedback_digest':feedback}
        result_ref = w.receipt(f'public/{number}/result',case='dev_001',candidate_digest=record['candidate_digest'],
            state='terminal',classification='execution_valid',source_evidence=w.raw(execution_path))
        details = {**metadata,'candidate_digest':record['candidate_digest'],'case':'dev_001','state':'terminal',
            'classification':'execution_valid','current_binding':trusted_binding,'transport':'complete','build_exit_code':0,
            'materialized_source_digest_before_build':build['product_source_digest'],
            'materialized_source_digest_after_build':build['product_source_digest'],
            'materialized_repository_digest':build['candidate_repo_digest'],'result':result_ref,
            'feedback_sha256':feedback_ref['sha256'],'request_ids':request_binding['request_ids'],
            'source_execution':w.raw(execution_path),'source_request_binding':w.raw(request_binding_path)}
        if number == 2:
            details['consumed_feedback'] = e['public_rounds'][0]['feedback']
            details['consumption_native_evidence'] = w.raw(native_path)
        e['public_rounds'].append({**metadata,'case':'dev_001','candidate_digest':record['candidate_digest'],
            'submission_dir':'raw/'+submission.relative_to(run_dir).as_posix(),
            'submission_sha256':{name:sha(submission/name) for name in FILES},
            'materialized_source_digest':build['product_source_digest'],'materialized_repository_digest':build['candidate_repo_digest'],
            'feedback':feedback_ref,'execution':w.receipt(f'public/{number}/execution',**details)})
        previous, feedback = record['candidate_digest'],record['feedback_digest']
    e['freeze'] = w.receipt('freeze',candidate_digest=builds[-1]['candidate_repo_digest'],delivery_candidate_digest=previous,
        builder_session_id=session,submission_sha256=frozen['submission_sha256'],current_binding=trusted_binding,
        source_evidence=w.raw(freeze_path))
    hidden_path = run_dir/'readiness_hidden_attestation.json'
    hidden = read(hidden_path)
    hidden_intent = read(run_dir/'readiness_hidden_intent.json')
    if (hidden_intent.get('case') != 'test_001' or hidden_intent.get('freeze_sha256') != sha(freeze_path) or
            hidden_intent.get('candidate_digest') != previous or
            hidden_intent.get('started_at', '') < frozen.get('frozen_at', '') or
            sha(Path(frozen['binary'])) != frozen['binary_sha256']):
        raise ValueError('hidden execution is not bound after this freeze')
    verdict,_ = execution_verdict(hidden['result'],'test_001',previous)
    if hidden.get('case') != 'test_001' or hidden.get('freeze_digest_stable') is not True or verdict.get('classification') not in {'scoreable','candidate_zero'}:
        raise ValueError('hidden smoke invalid')
    hidden_result = w.receipt('hidden/result',case='test_001',state='terminal',candidate_digest=builds[-1]['candidate_repo_digest'],
        source_evidence=w.raw(hidden_path))
    e['hidden_smoke'] = w.receipt('hidden',case='test_001',state='terminal',classification='execution_valid',transport='complete',
        readiness_only=True,builder_access=False,freeze_sha256=e['freeze']['sha256'],result=hidden_result,
        request_ids=role_ids['hidden_lower'],source_evidence=w.raw(hidden_path))
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
        path = run_dir/'readiness_scoring'/(role+'_observation.json')
        observed = read(path)
        inp,out = w.references(read(observed['input']['path'])),w.references(read(observed['output']['path']))
        # The shared judge adds `ceiling_assessments` when a score-cap contract is supplied;
        # the shared bundle validator checks the payload against a fixed key set, so the
        # ceilings live beside the payload (the raw judge output is the source evidence).
        _payload = out.get('payload') if isinstance(out.get('payload'), dict) else None
        if _payload is not None and 'ceiling_assessments' in _payload:
            out['ceiling_assessments'] = _payload.pop('ceiling_assessments')
        inp['freeze_sha256'] = e['freeze']['sha256']
        observed.update(input=w.receipt('judges/'+role+'_input',**inp),output=w.receipt('judges/'+role+'_output',**out),
            freeze_sha256=e['freeze']['sha256'],source_evidence=w.raw(path))
        e['judges'][role] = w.receipt('judges/'+role,**observed)
    for key in ('before','after'):
        reference = cleanup[key]
        value = read(reference['path'])
        if sha(reference['path']) != reference['sha256']:
            raise ValueError('cleanup original bytes changed')
        cleanup[key] = w.receipt('cleanup/'+key, **value, source_evidence=w.raw(Path(reference['path'])))
    for cid, reference in cleanup['stats'].items():
        value = read(reference['path'])
        if sha(reference['path']) != reference['sha256']:
            raise ValueError('cleanup stats changed')
        cleanup['stats'][cid] = w.receipt('cleanup/'+cid, **value, source_evidence=w.raw(Path(reference['path'])))
    e['cleanup'] = w.receipt('cleanup',**cleanup,source_evidence=w.raw(cleanup_path))
    manifest = w.root/'manifest.json'
    manifest.write_text(json.dumps(e,sort_keys=True,indent=2)+'\n')
    return {'bundle_root':str(w.root),'manifest_sha256':sha(manifest),'pipeline_ready':False,'admission_required':True}
