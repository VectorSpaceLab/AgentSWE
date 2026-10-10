"""Export current-run OpenClaw evidence; coordinator supplies terminal cleanup proof.

This exporter never dispatches, scores, cleans resources, or writes a shared gate.
Raw files are copied byte-for-byte; normalized receipts retain their source refs.

OpenClaw names three things its own way, and the mapping is fixed here:

    contract candidate         <- submission row's delivery_digest (the three files)
    contract materialized repo <- submission row's candidate_digest
    per-round lower ledger     <- that attempt's own rotated broker_after.json

The last one matters: the shared usage normalizer validates a ledger as a whole,
so each accepted round has to be described completely and only by its own file.
The broker rotates per candidate attempt to make that true.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
FILES = ('solution.patch', 'edit_report.json', 'run_report.json')
CASE = 'test_001'

def contract_tree_digest(root):
    """The readiness contract's candidate digest (identical to v2_readiness.tree_digest and
    agentloop.protocol.tree_digest): name-first framing over every entry. The Create-style
    `tree_digest` above frames kind-first and is kept for repository/product digests.
    Verified 2026-09-19 on 0919-ds-002: v2 == this for all three submission dirs."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        elif path.is_dir():
            kind, payload = b"D", b""
        else:
            raise ValueError("special file in tree")
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


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
        if any(part in {'builder_provider.toml', 'builder_direct_provider.toml',
                        'builder_broker_provider.toml', '.env', 'auth.json'} for part in relative.parts):
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


def controlled_build(record):
    """The case's controlled build, read from the runtime manifest it names.

    The manifest is where openclaw records what the candidate source was before
    and after the build, the runtime tree the build produced, and each build
    phase's exit. Immutability is measured here rather than asserted later.
    """
    manifest_path = Path(record['candidate_runtime_manifest'])
    manifest = json.loads(manifest_path.read_bytes())
    before = manifest.get('candidate_source_digest')
    after = manifest.get('candidate_source_digest_after_build')
    if not before or before != after or manifest.get('source_digest_stable') is not True:
        raise ValueError('controlled build changed the materialized product source')
    if manifest.get('candidate_runtime_ready') is not True:
        raise ValueError('controlled build did not produce a ready runtime')
    codes = [phase.get('exit_code') for phase in (manifest.get('build_phases') or [])]
    if not codes or any(type(code) is not int for code in codes):
        raise ValueError('controlled build has no terminal phase exits')
    return {'source_digest': before, 'repository_digest': manifest['runtime_source_digest'],
            'exit_code': max(codes), 'manifest_path': manifest_path}


def checked_case(record, case_id, bound_digest):
    """OpenClaw's case record is the execution record; its classifier decides.

    A dev case and a hidden case carry the same schema but neither names its
    candidate directly, so the binding comes from the controlled build: the
    source the build was handed is the accepted submission for a dev round and
    the frozen candidate for the hidden one.
    """
    from execution_contract import infrastructure_reason
    if record.get('case_id') != case_id:
        raise ValueError('case record identity mismatch: ' + case_id)
    reason = infrastructure_reason(record)
    if reason:
        raise ValueError('raw execution failed task classifier: ' + str(reason))
    if record.get('timed_out') is True:
        raise ValueError('case execution timed out: ' + case_id)
    build = controlled_build(record)
    if build['source_digest'] != bound_digest:
        raise ValueError('case record is not bound to its candidate: ' + case_id)
    return build


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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evaluator.semantic_finalize import tree_digest

    cleanup_path = Path(cleanup_receipt)
    cleanup = read(cleanup_path)
    if cleanup.get('owner') != 'evaluator' or cleanup.get('run_id') != run_dir.name or any(
            cleanup.get(key) != 'terminal' for key in ('unit_state', 'harbor_state', 'builder_state')):
        raise ValueError('coordinator terminal cleanup proof is required before export')

    session_path = run_dir / 'builder_session_attestation.json'
    if not session_path.is_file():
        session_path = run_dir / 'pilot_builder_session_attestation.json'
    attestation = read(session_path)
    native = attestation['native_evidence']
    if native.get('valid') is not True or native.get('native_turn_completed') is not True:
        raise ValueError('native Builder has not completed verified two-round lifecycle')

    freeze_path = run_dir / 'lifecycle/freeze_manifest.json'
    frozen = read(freeze_path)
    if frozen.get('readiness_profile') != PROFILE or frozen.get('current_binding') != trusted_binding:
        raise ValueError('current binding or sealed readiness freeze mismatch')
    records = read(run_dir / 'builder_submissions.json')
    if len(records) != 2 or frozen.get('source_submission') != 2:
        raise ValueError('readiness requires the second accepted round')
    frozen_path = Path(str(frozen.get('frozen_candidate_path', run_dir / 'lifecycle/frozen_candidate')))
    if tree_digest(frozen_path) != frozen['candidate_digest']:
        raise ValueError('frozen repository changed before evidence export')

    from harbor.native_builder_evidence import events, readiness_segment_streams
    segment_streams = readiness_segment_streams(run_dir, native)
    stream = segment_streams[-1]
    if any(sha(item) != ref['sha256'] for item, ref in zip(segment_streams, native['source_files'])):
        raise ValueError('native stream changed after verified terminal')
    parsed = [v for item in segment_streams for v in events(item.read_bytes())]
    terminal = events(stream.read_bytes())
    turns = [v['usage'] for v in parsed if v.get('type') == 'turn.completed']
    # D13 (2026-09-20, readiness 0920-hd-003): a codex-CLI "Reconnecting..." error is a
    # recovered transport reconnect -- the formal path classifies it as such -- and the turn
    # accounting below still requires the one started turn to complete.
    unrecovered = [v for v in parsed if v.get('type') == 'turn.failed' or
                   (v.get('type') == 'error' and not str(v.get('message', '')).startswith('Reconnecting...'))]
    if len(turns) != 1 or unrecovered:
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
        source_evidence=w.raw(session_path), **segment_refs)
    e['usage']['builder'] = w.receipt('usage/builder', role='builder',
        transport='native_codex_direct', builder_broker_started=False,
        native_completed_turns=len(turns), native_reported_usage=turns,
        actual_upstream_requests=None, complete_provider_billing_claimed=False,
        native_usage_complete=True,
        known_tokens=sum(v['input_tokens'] + v['output_tokens'] for v in turns),
        builder_session_id=session, native_log=native_log)

    hidden_record_path = run_dir / 'pilot_hidden' / CASE / 'hidden_case_attestation.json'
    hidden_record = read(hidden_record_path)
    suite = read(run_dir / 'pilot_hidden/hidden_result.json')
    if suite.get(CASE) != hidden_record:
        raise ValueError('suite index differs from the original case attestation')

    # Each accepted attempt owns its rotated ledger; the judges' ledgers are the
    # readiness broker's recorded stats and the Code judge's own attempt file.
    ledger_paths = {
        'public_lower': [run_dir / 'public/by_candidate' / row['candidate_digest'] / 'dev_001/broker_after.json'
                         for row in records],
        'hidden_lower': [run_dir / 'pilot_hidden' / CASE / 'broker_after.json'],
        'result_judge': [run_dir / 'readiness_judge_broker_stats.json'],
    }
    round_ids = []
    for role, paths in ledger_paths.items():
        rows = []
        for path in paths:
            ledger = read(path)
            if role.endswith('lower'):
                ids = [v['request_id'] for v in ledger['attempts']]
            elif role == 'result_judge':
                ids = [v['request_id'] for v in ledger['attempts']]
            else:
                ids = [v['response_id'] for v in ledger['attempts']]
            if role == 'public_lower':
                round_ids.append(list(ids))
            rows.extend({**broker_normalizers[role](ledger, rid), 'provider_record': w.raw(path)}
                        for rid in ids)
        if not rows:
            raise ValueError('required role has no actual requests: ' + role)
        e['usage'][role] = w.receipt('usage/' + role, role=role, calls=len(rows),
            actual_upstream_attempts=sum(r['upstream_attempts'] for r in rows),
            successes=sum(r['state'] == 'success' for r in rows),
            failures=sum(r['state'] == 'failure' for r in rows),
            known_tokens=sum(r['known_tokens'] for r in rows), **_recovered_usage_fields(rows), requests=rows)

    previous = feedback = None
    for number, row in enumerate(records, 1):
        submission = Path(row['delivery_snapshot'])
        if set(p.name for p in submission.iterdir()) != set(FILES) or contract_tree_digest(submission) != row['delivery_digest']:
            raise ValueError('accepted submission changed')
        for name in FILES:
            w.raw(submission / name)
        case = row['dev_results']['dev_001']
        build = checked_case(case, 'dev_001', row['candidate_digest'])
        if (case.get('dev_feedback') or {}).get('valid') is not True:
            raise ValueError('public round did not establish a valid evaluation')
        execution_path = Path(case['evaluator_run_report'])
        feedback_ref = {**w.raw(Path(row['feedback'])), 'digest_algorithm': 'sha256-bytes-v1'}
        meta = {'builder_session_id': session, 'submission_number': number,
                'revision_of_candidate_digest': previous, 'feedback_digest': feedback}
        result = w.receipt(f'public/{number}/result', candidate_digest=row['delivery_digest'],
            case='dev_001', state='terminal', classification='execution_valid',
            source_evidence=w.raw(execution_path))
        receipt = {**meta, 'candidate_digest': row['delivery_digest'], 'case': 'dev_001',
            'state': 'terminal', 'classification': 'execution_valid', 'current_binding': trusted_binding,
            'transport': 'complete', 'build_exit_code': build['exit_code'],
            'materialized_source_digest_before_build': build['source_digest'],
            'materialized_source_digest_after_build': build['source_digest'],
            'materialized_repository_digest': build['repository_digest'],
            'result': result, 'feedback_sha256': feedback_ref['sha256'],
            'request_ids': round_ids[number - 1],
            'source_state': w.raw(run_dir / 'builder_submissions.json')}
        if number == 2:
            receipt['consumed_feedback'] = e['public_rounds'][0]['feedback']
            receipt['consumption_native_evidence'] = w.raw(session_path)
        e['public_rounds'].append({**meta, 'case': 'dev_001', 'candidate_digest': row['delivery_digest'],
            'submission_dir': ('raw/' + submission.relative_to(run_dir).as_posix()),
            'submission_sha256': {n: sha(submission / n) for n in FILES},
            'materialized_source_digest': build['source_digest'],
            'materialized_repository_digest': build['repository_digest'],
            'feedback': feedback_ref, 'execution': w.receipt(f'public/{number}/execution', **receipt)})
        previous, feedback = row['delivery_digest'], row['feedback_digest']

    # The sealed manifest is kept verbatim as source evidence, but the readiness
    # contract's freeze binding is restated here in the contract's own names, the
    # way the sibling agentloop exporters state it:
    #   candidate_digest          <- round 2's materialized repository digest
    #   delivery_candidate_digest <- round 2's accepted three-file delivery digest
    #   submission_sha256         <- those three files' hashes
    #   builder_session_id        <- the one native Builder session
    # OpenClaw's manifest cannot supply them as-is: validate_freeze_manifest
    # requires its candidate_digest to be the frozen product SOURCE digest, it
    # records no session id, and its delivery fields are written by the session
    # layer while the round being frozen has not yet joined that layer's ledger,
    # so they name the previous round.  Nothing is asserted about the manifest's
    # own vocabulary here; the raw manifest stays attached unchanged.
    _last = e['public_rounds'][-1]
    e['freeze'] = w.receipt('freeze', **{**frozen,
        'candidate_digest': _last['materialized_repository_digest'],
        'delivery_candidate_digest': _last['candidate_digest'],
        'submission_sha256': dict(_last['submission_sha256']),
        'builder_session_id': session}, source_evidence=w.raw(freeze_path))
    # The hidden case stays bound to the frozen product source it was built from;
    # only the receipt's contract-named identity follows the freeze receipt.
    checked_case(hidden_record, CASE, frozen['candidate_digest'])
    hr = w.receipt('hidden_result', case=CASE, state='terminal',
        candidate_digest=_last['materialized_repository_digest'],
        source_evidence=w.raw(hidden_record_path))
    e['hidden_smoke'] = w.receipt('hidden', case=CASE, state='terminal', classification='execution_valid',
        transport='complete', readiness_only=True, builder_access=False,
        freeze_sha256=e['freeze']['sha256'], result=hr,
        request_ids=[v['request_id'] for v in read(ledger_paths['hidden_lower'][0])['attempts']],
        source_evidence=w.raw(run_dir / 'pilot_hidden/hidden_result.json'))

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
        # The shared judge adds `ceiling_assessments` when a score-cap contract is supplied;
        # the shared bundle validator checks the payload against a fixed key set, so the
        # ceilings live beside the payload (the raw judge output is the source evidence).
        _payload = out.get('payload') if isinstance(out.get('payload'), dict) else None
        if _payload is not None and 'ceiling_assessments' in _payload:
            out['ceiling_assessments'] = _payload.pop('ceiling_assessments')
        inp['freeze_sha256'] = e['freeze']['sha256']
        input_ref = w.receipt('judges/' + role + '_input', **inp,
                              source_evidence=w.raw(Path(observed['input']['path'])))
        output_ref = w.receipt('judges/' + role + '_output', **out,
                               source_evidence=w.raw(Path(observed['output']['path'])))
        observed.update(freeze_sha256=e['freeze']['sha256'], input=input_ref, output=output_ref,
                        source_evidence=w.raw(path))
        e['judges'][role] = w.receipt('judges/' + role, **observed)

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
