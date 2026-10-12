"""Export current-run Dyad evidence; coordinator supplies terminal cleanup proof.

This exporter never dispatches, scores, cleans resources, or writes a shared gate.
Raw files are copied byte-for-byte; normalized receipts retain their source refs.

Dyad names three things differently from the shared readiness contract, and the
mapping is fixed here rather than in the contract:

    contract candidate            <- record['delivery_digest']   (the three delivered files)
    contract materialized repo    <- record['candidate_digest']  (the whole materialized tree)
    contract execution record     <- the case result, whose raw bytes are native_evidence_path

Everything else is read from dyad's own evidence under its own names.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

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


# --- D57 (2026-09-21): a case the evaluator's own case budget killed ------------------
# Policy (coordinator 2026-09-21, D14/D23/D54): budget exhaustion is a Candidate outcome
# (candidate_timeout, a hard zero), never an infrastructure fault.  Such a case is killed
# inside scenario_chat_flow.test.ts's beforeAll (:632-866) and therefore never reaches the
# afterAll (:869 -> :1107) that is the one writer of the native evidence file, so
# `real_execution` -- which is `run_lower_agent_case.py:455 bool(native_evidence and
# native_evidence["real_product"] is True)` -- is False by construction.  Inventing it
# would destroy the one field the bundle relies on.  Instead the evaluator's own records
# are required to prove that the product process entered and that the evaluator ended it,
# and the absence of the native record is stated explicitly rather than filled in, exactly
# the way readiness_smoke.py:210-229 already synthesises an absence observation for the
# judge.  Every other gate in this exporter is unchanged.
CANDIDATE_BUDGET_CLASSES = {'candidate_timeout': 'case_budget_exhausted'}
CASE_EVIDENCE_MANIFEST_SCHEMA = 'dyad-lower-case-evidence/v1'
HEADLESS_LAUNCH_SCHEMA = 'dyad-headless-launch-v2'
SETUP_BLOCKER_EXIT = 125          # headless_chat_flow.py write_failure() launch record
DEADLINE_EXIT = 124               # headless_chat_flow.py:864-868 TimeoutExpired branch


def _d57_case_evidence_manifest(record):
    """The case's own evaluator-written evidence index, or None.

    Nothing here is taken from a Candidate byte: run_lower_agent_case.py writes this
    manifest, and every reference in it is re-verified against the file it names.
    """
    try:
        manifest = read(record['evidence_manifest'])
    except (OSError, KeyError, TypeError, ValueError):
        return None
    if (manifest.get('schema_version') != CASE_EVIDENCE_MANIFEST_SCHEMA
            or manifest.get('case_id') != record.get('case_id')):
        return None
    files = manifest.get('files')
    return files if isinstance(files, dict) else None


def _d57_manifest_file(files, name, *, present):
    """A {path, sha256} reference the manifest declares present (and whose bytes still
    hash to what it recorded), or an explicit declaration that the file is absent."""
    entry = files.get(name)
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
        return None
    if entry.get('exists') is not present:
        return None
    if not present:
        return entry.get('sha256') is None and entry
    path = Path(entry['path'])
    if path.is_symlink() or not path.is_file() or sha(path) != entry.get('sha256'):
        return None
    return {'path': entry['path'], 'sha256': entry['sha256']}


def _d57_budget_entry_proof(record):
    """Evaluator-written proof that a case with no native evidence still ran the product
    process and was ended by the evaluator's own case deadline; None if anything is
    missing, so the failure mode is the pre-D57 refusal."""
    from execution_contract import lower_execution_counts
    if CANDIDATE_BUDGET_CLASSES.get(record.get('classification')) != record.get('failure_class'):
        return None
    attribution = record.get('failure_attribution') or {}
    if (attribution.get('party') != 'candidate' or attribution.get('observed_by') != 'evaluator'
            or attribution.get('fatal') is not True or not attribution.get('reason')):
        return None
    if (record.get('environment_preflight') or {}).get('valid') is not True:
        return None
    if record.get('native_evidence_present') is not False or record.get('native_evidence_sha256') is not None:
        return None
    calls, successful = lower_execution_counts(record)
    if calls <= 0 or successful <= 0:
        return None
    files = _d57_case_evidence_manifest(record)
    if files is None:
        return None
    launcher_ref = _d57_manifest_file(files, 'launcher_result', present=True)
    case_result_ref = _d57_manifest_file(files, 'case_result', present=True)
    if not launcher_ref or not case_result_ref:
        return None
    if not _d57_manifest_file(files, 'native_evidence', present=False):
        return None
    try:
        launch = read(launcher_ref['path'])
    except (OSError, ValueError):
        return None
    runtime = launch.get('headless_runtime_record')
    if not isinstance(runtime, dict) or runtime.get('schema_version') != HEADLESS_LAUNCH_SCHEMA:
        return None
    command = runtime.get('command')
    if (launch.get('executed') is not True or not isinstance(command, list) or not command
            or Path(str(command[0])).name != 'bwrap' or '--unshare-net' not in command):
        return None
    if (runtime.get('network_namespace') != 'isolated'
            or runtime.get('provider_credential_mounted') is not False
            or runtime.get('native_evidence_present') is not False
            or runtime.get('exit_code') == SETUP_BLOCKER_EXIT):
        return None
    if not (runtime.get('timed_out') is True or runtime.get('exit_code') == DEADLINE_EXIT
            or launch.get('exit_code') == DEADLINE_EXIT):
        return None
    return {'policy_id': 'edit-case-deadline-candidate-outcome-2026-09-21',
            'sandbox_argv0': str(command[0]), 'network_namespace': 'isolated',
            'unshared_network': True, 'provider_credential_mounted': False,
            'controller_exit_code': runtime.get('exit_code'),
            'controller_timed_out': runtime.get('timed_out'),
            'controller_duration_seconds': runtime.get('duration_seconds'),
            'launcher_exit_code': launch.get('exit_code'),
            'lower_calls': calls, 'successful_lower_calls': successful,
            'native_evidence_present': False,
            'launcher_result': launcher_ref, 'case_result': case_result_ref,
            'evidence_manifest': record['evidence_manifest']}


def _d57_execution_evidence(w, case, receipt_name):
    """The bundle's bytes for a case's terminal execution record.

    A case that delivered its native record exports exactly what it always exported.  A
    proven budget kill exports an explicit evaluator observation of the absence, bound to
    the evaluator's own terminal records -- never a fabricated launch record.
    """
    path = Path(case['native_evidence_path'])
    proof = None if case.get('real_execution') is True else _d57_budget_entry_proof(case)
    if proof is None:
        if sha(path) != case['native_evidence_sha256']:
            raise ValueError('native case evidence changed after execution')
        return {'source_evidence': w.raw(path), 'outcome': {}}
    absence = w.receipt(receipt_name, evidence_kind='evaluator_observed_absence',
        candidate_authored=False, native_evidence_present=False, case=case['case_id'],
        expected_native_evidence_path=str(path),
        classification=case['classification'], failure_class=case['failure_class'],
        reason=(case.get('failure_attribution') or {}).get('reason'),
        product_entry_proof={k: v for k, v in proof.items()
                             if k not in ('launcher_result', 'case_result', 'evidence_manifest')},
        launcher_result=w.imported_ref(proof['launcher_result']),
        case_result=w.imported_ref(proof['case_result']),
        evidence_manifest=w.raw(Path(proof['evidence_manifest'])))
    return {'source_evidence': absence,
            'outcome': {'candidate_outcome': case['classification'],
                        'candidate_failure_class': case['failure_class'],
                        'native_evidence': absence}}
# --- end D57 --------------------------------------------------------------------------


def checked_case(record, case_id, repository_digest):
    """Dyad's case result is the execution record; its classifier decides validity."""
    from execution_contract import infrastructure_reason
    if record.get('case_id') != case_id:
        raise ValueError('case record identity mismatch: ' + case_id)
    if record.get('infra_valid') is not True:
        raise ValueError('raw execution does not explicitly exclude infrastructure failure')
    reason = infrastructure_reason(record)
    if reason:
        raise ValueError('raw execution failed task classifier: ' + str(reason))
    if record.get('candidate_digest') != repository_digest:
        raise ValueError('case record is not bound to the materialized repository')
    if record.get('execution_attempted') is not True:
        raise ValueError('raw execution does not establish a real terminal product process')
    # D57: a case the evaluator's own case budget killed has no native product-entry record
    # and never can (see the block above), so the evaluator's launch/controller records
    # stand in its place. Every other case still needs `real_execution`.
    if record.get('real_execution') is not True and _d57_budget_entry_proof(record) is None:
        raise ValueError('raw execution does not establish a real terminal product process')
    if record.get('launcher_exit_code') is None:
        raise ValueError('raw execution has no terminal launcher exit')
    return record


def round_request_ids(case):
    """Requests this round actually issued: the ledger delta around its execution."""
    before = {row['request_sha256'] for row in case['broker']['before']['requests']}
    ids = [row['request_sha256'] for row in case['broker']['after']['requests']
           if row['request_sha256'] not in before]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('public round has no distinct lower requests of its own')
    return ids


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
    state_path = run_dir / 'lifecycle/dev_lifecycle.json'
    records = read(state_path)
    freeze_path = run_dir / 'lifecycle/freeze_manifest.json'
    frozen = read(freeze_path)
    seal_path = run_dir / 'lifecycle/freeze_manifest.sha256'
    if seal_path.read_text().strip() != sha(freeze_path):
        raise ValueError('sealed freeze manifest does not match its own seal')
    if native.get('valid') is not True or native.get('native_turn_completed') is not True:
        raise ValueError('native Builder has not completed verified two-round lifecycle')
    if frozen.get('readiness_profile') != PROFILE or frozen.get('current_binding') != trusted_binding:
        raise ValueError('current binding or sealed readiness freeze mismatch')
    if len(records) != 2 or frozen.get('source_submission') != 2:
        raise ValueError('readiness requires the second accepted round')
    from harbor.builder_protocol import tree_digest
    from harbor.native_builder_evidence import events, readiness_segment_streams
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
        'public_lower': run_dir / 'public_lower_broker_stats.json',
        'hidden_lower': run_dir / 'hidden_lower_broker_stats.json',
        'result_judge': run_dir / 'result_judge_broker_stats.json'}
    ledgers = {role: read(path) for role, path in ledger_paths.items()}
    # Public-round attribution (0919-ds-004): dyad's public broker keeps one ledger for
    # every attempt, and a request cut short inside a rejected, non-consuming attempt
    # taints the whole-ledger aggregate the shared normalizer checks. The normalizer is
    # handed the accepted rounds' projection -- rows and intents restricted to the two
    # accepted rounds' own request ids, counters recomputed from those rows -- while the
    # untouched raw ledger stays the provider_record and the exclusion is recorded.
    attributed_public = set()
    for record in records:
        for case in (record.get('dev') or []):
            try:
                attributed_public.update(round_request_ids(case))
            except (KeyError, TypeError):
                pass
    ledger_projection = None
    for role, ledger in ledgers.items():
        if role.endswith('lower'):
            ids = [v['request_sha256'] for v in ledger['requests']]
            if role == 'public_lower':
                excluded = [rid for rid in ids if rid not in attributed_public]
                if excluded:
                    rows_kept = [v for v in ledger['requests'] if v['request_sha256'] in attributed_public]
                    tokens = {k: sum(int(v.get(k) or 0) for v in rows_kept) for k in ('input_tokens', 'output_tokens', 'total_tokens')}
                    # D13: a kept row that failed or whose usage is unknown stays one in the
                    # projected counters, and the subtotal is the projection's own, not the
                    # raw ledger's. The shared normalizer decides whether it is tolerable.
                    failed_kept = [v for v in rows_kept if v.get('ok') is not True or v.get('usage_state') != 'known']
                    unknown_kept = [v for v in rows_kept if v.get('usage_state') != 'known']
                    projected = {**ledger, 'requests': rows_kept,
                        'logical_requests': {k: v for k, v in (ledger.get('logical_requests') or {}).items() if k in attributed_public},
                        'runtime': {**ledger.get('runtime', {}), 'calls': len(rows_kept),
                                    'successful_calls': len(rows_kept) - len(failed_kept),
                                    'failures': len(failed_kept), 'unknown_usage_calls': len(unknown_kept),
                                    'pending_calls': 0, 'in_flight_calls': 0, 'known_usage_subtotal': tokens,
                                    **({k: None for k in tokens} if unknown_kept else tokens)}}
                    ledger_projection = {'raw_sha256': sha(ledger_paths[role]), 'excluded_request_ids': excluded,
                        'rule': 'accepted rounds only: requests of rejected/non-consuming attempts excluded; raw ledger retained as provider_record'}
                    ledger = projected
                    ids = [v['request_sha256'] for v in rows_kept]
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
            **({'ledger_projection': ledger_projection} if role == 'public_lower' and ledger_projection else {}))
    previous = feedback = None
    for number, record in enumerate(records, 1):
        # The contract's candidate is the delivered triple, not dyad's whole
        # materialized tree; both digests are carried so neither is lost.
        attempt = Path(record['candidate_path']).parent
        submission = attempt / 'delivery'
        if set(p.name for p in submission.iterdir()) != set(FILES) or tree_digest(submission) != record['delivery_digest']:
            raise ValueError('accepted submission changed')
        for name in FILES:
            w.raw(submission / name)
        repository = Path(record['candidate_path'])
        if tree_digest(repository) != record['candidate_digest']:
            raise ValueError('accepted public repository changed after execution')
        preflight = read(attempt / 'preflight.json')
        immutability = preflight['readiness_source_immutability']
        if immutability.get('unchanged') is not True or immutability['before'] != immutability['after']:
            raise ValueError('controlled build changed product source')
        if record.get('submission_consumed') is not True:
            raise ValueError('public record did not establish infrastructure validity')
        case = checked_case(record['dev'][0], 'dev_001', record['candidate_digest'])
        ids = round_request_ids(case)
        # D57: the native record when the case delivered one, an explicit evaluator
        # observation of its absence when the evaluator's own budget ended the case.
        execution_evidence = _d57_execution_evidence(w, case, f'public/{number}/native_evidence')
        execution_ref = execution_evidence['source_evidence']
        feedback_ref = {**w.raw(Path(record['feedback_path'])), 'digest_algorithm': 'sha256-bytes-v1'}
        meta = {'builder_session_id': session, 'submission_number': number,
            'revision_of_candidate_digest': previous, 'feedback_digest': feedback}
        result = w.receipt(f'public/{number}/result', candidate_digest=record['delivery_digest'],
            case='dev_001', state='terminal', classification='execution_valid',
            source_evidence=execution_ref, **execution_evidence['outcome'])
        # D57: `classification` is the shared contract's statement that the execution RECORD
        # is contract-valid (execution_contract.py:97,107-111 says exactly that of a
        # candidate_zero) and is hard-required by v2_readiness.py:203,215,233-235. The
        # Candidate's own outcome is published beside it, never in place of it.
        receipt = {**meta, 'candidate_digest': record['delivery_digest'], 'case': 'dev_001',
            'state': 'terminal', 'classification': 'execution_valid', 'current_binding': trusted_binding,
            **execution_evidence['outcome'],
            'transport': 'complete', 'build_exit_code': preflight['candidate_typecheck']['exit_code'],
            'materialized_source_digest_before_build': immutability['before']['digest'],
            'materialized_source_digest_after_build': immutability['after']['digest'],
            'materialized_repository_digest': record['candidate_digest'],
            'result': result, 'feedback_sha256': feedback_ref['sha256'], 'request_ids': ids,
            'source_state': w.raw(state_path), 'source_execution': execution_ref}
        if number == 2:
            receipt['consumed_feedback'] = e['public_rounds'][0]['feedback']
            receipt['consumption_native_evidence'] = w.raw(native_path)
        e['public_rounds'].append({**meta, 'case': 'dev_001', 'candidate_digest': record['delivery_digest'],
            'submission_dir': ('raw/' + submission.relative_to(run_dir).as_posix()),
            'submission_sha256': {n: sha(submission / n) for n in FILES},
            'materialized_source_digest': immutability['before']['digest'],
            'materialized_repository_digest': record['candidate_digest'],
            'feedback': feedback_ref, 'execution': w.receipt(f'public/{number}/execution', **receipt)})
        previous, feedback = record['delivery_digest'], record['feedback_digest']
    e['freeze'] = w.receipt('freeze', **frozen, source_evidence=w.raw(freeze_path))
    hidden_path = run_dir / 'hidden-after-freeze-attestation.json'
    hidden = read(hidden_path)
    case = hidden['cases']['test_001']
    if (hidden.get('expected_cases') != ['test_001'] or hidden.get('executed_cases') != ['test_001']
            or set(hidden['cases']) != {'test_001'} or hidden.get('all_cases_started_after_freeze') is not True
            or case.get('executed_after_freeze') is not True
            or case.get('frozen_digest_before') != frozen['candidate_digest']):
        raise ValueError('hidden smoke is not a completed valid single-case execution')
    checked_case(case, 'test_001', frozen['candidate_digest'])
    # D57: same rule as the public rounds -- the native record, or an explicit evaluator
    # observation of its absence for a case the evaluator's own case budget ended.
    hidden_evidence = _d57_execution_evidence(w, case, 'hidden_native_evidence')
    hr = w.receipt('hidden_result', case='test_001', state='terminal',
        candidate_digest=frozen['candidate_digest'],
        source_evidence=hidden_evidence['source_evidence'], **hidden_evidence['outcome'])
    e['hidden_smoke'] = w.receipt('hidden', case='test_001', state='terminal', classification='execution_valid',
        transport='complete', readiness_only=True, builder_access=False, freeze_sha256=e['freeze']['sha256'],
        result=hr, request_ids=[v['request_sha256'] for v in ledgers['hidden_lower']['requests']],
        source_evidence=w.raw(hidden_path), **hidden_evidence['outcome'])
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
