"""Real isolated product prepare/cancel -> immutable science -> cap diagnostics.

No model, Builder, Result judge or Code judge request is made. Deterministic
operations here prove evaluator plumbing only; they are not lower-agent work.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agentloop'))
import lower_agent_launcher as launcher
from protocol import canonical_json, tree_digest, write_json
from immutable_replay import cleanup_owned_runtime
from case_evidence import scientific_spec, scientific_reference, write_immutable_evidence
from scientific_audit import collect_scientific_audit

IMAGE = 'sha256:10b0f65061629ca8edab1b33444f49661f1c47e98109fda74072839287630336'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--expected-candidate-digest', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    candidate, output = args.candidate.resolve(), args.output.resolve()
    assert tree_digest(candidate) == args.expected_candidate_digest, 'Candidate changed'
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    owner = 'science-capture-' + uuid.uuid4().hex[:16]
    os.environ['AGENTSWE_RUNTIME_OWNER_ID'] = owner
    events, audit, cleanup, error = [], None, None, None
    report = {'schema_version': 'agentswe-real-product-scientific-capture-diagnostic/v1',
              'case_id': 'test_001', 'candidate': str(candidate),
              'candidate_digest': args.expected_candidate_digest,
              'model_calls': 0, 'builder_calls': 0, 'judge_calls': 0,
              'operations_selected_by': 'evaluator-only plumbing diagnostic, not a lower agent',
              'score': None, 'formal_result_publishable': False, 'acceptance_result_publishable': False,
              'image': IMAGE, 'max_wall_seconds': 600, 'memory': '4GiB',
              'isolation': 'network none, selective product/source/workspace mounts; no actual credential or private oracle in product'}
    write_json(output / 'diagnostic_plan.json', report)
    try:
        workspace = output / 'fresh_workspace'; workspace.mkdir()
        case_file = ROOT / 'agentloop/cases/test_001/case_input.json'
        launcher.copy_candidate_visible_assets(launcher.case_assets_root(case_file, case_file.parent), workspace)
        context = json.loads((workspace / 'transaction_context.json').read_text())
        context['request_id'] += '-' + owner
        context['verification_id'] += '-' + owner
        for sequence, operation in enumerate(('prepare', 'cancel'), 1):
            remaining = 600 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('600-second diagnostic case budget exhausted')
            event = launcher._run_product_action(candidate_repo=candidate, workspace=workspace,
                output_dir=output, context=context, endpoint='http://unreachable.invalid/v1/responses',
                timeout=min(180, remaining), operation=operation, sequence=sequence,
                decision={'rationale': 'evaluator-only product plumbing diagnostic; no model decision',
                          'response_text_sha256': ''}, image=IMAGE, dependency_overlay=None)
            event.pop('model_rationale', None); event.pop('model_response_sha256', None)
            event['evaluator_owned_diagnostic_action'] = True
            events.append(event)
            write_json(output / 'diagnostic_product_events.json', {'not_lower_agent_trajectory': True, 'events': events})
            phase_cleanup = cleanup_owned_runtime(owner)
            write_json(output / f'action_{sequence:03d}_runtime_cleanup.json', phase_cleanup)
            if not phase_cleanup['all_absent']:
                raise RuntimeError('Product runtime cleanup unconfirmed; no next operation permitted')
            print(json.dumps({'operation': operation, 'exit_code': event['exit_code'], 'capture': event['scientific_capture']}), flush=True)
        audit = collect_scientific_audit(base=output, events=events, spec=scientific_spec('test_001'))
        native = {'case_id': 'test_001', 'scientific_audit_bundles': audit['bundles'],
                  'evaluator_diagnostic_not_agent_evidence': True}
        oracle = {'case_id': 'test_001', 'scientific_reference': scientific_reference('test_001'),
                  'result_score_caps': audit['result_score_caps'], 'automatic_score': None}
        rubric = ROOT / 'agentloop/result_rubric.md'
        cap = {'schema_version': 'agentswe-result-score-caps/v1', 'case_id': 'test_001',
               'rubric_sha256': sha(rubric),
               'native_evidence_sha256': hashlib.sha256(canonical_json(native)).hexdigest(),
               'oracle_summary_sha256': hashlib.sha256(canonical_json(oracle)).hexdigest(),
               'entries': audit['result_score_caps']}
        inputs_dir = output / 'diagnostic_inputs'
        write_immutable_evidence(inputs_dir, {'native_evidence.json': native,
            'oracle_summary.json': oracle, 'score_cap_contract.json': cap})
        shared = Path('@@AGENTSWE_EDITING_CONTROL@@/result_judge.py')
        spec = importlib.util.spec_from_file_location('agentswe_ai_real_capture_cap_integration', shared)
        judge = importlib.util.module_from_spec(spec); spec.loader.exec_module(judge)
        inputs = {'rubric': rubric, 'native_evidence': inputs_dir / 'native_evidence.json',
                  'oracle_summary': inputs_dir / 'oracle_summary.json'}
        ceiling, verified = judge.load_score_caps(inputs_dir / 'score_cap_contract.json', 'test_001', inputs)
        negative = output / 'negative_snapshot_copy'; negative.mkdir()
        shutil.copytree(output / 'scientific_snapshots', negative / 'scientific_snapshots')
        altered = negative / 'scientific_snapshots/action_001/product/claim_ledger.json'
        if not altered.is_file():
            raise RuntimeError('Real prepare did not expose claim ledger: no positive capture proof')
        altered.write_bytes(altered.read_bytes() + b'\n')
        rejected = False
        try:
            collect_scientific_audit(base=negative, events=events, spec=scientific_spec('test_001'))
        except ValueError as exc:
            rejected = 'changed' in str(exc)
        report.update(shared_result_judge_sha256=sha(shared), production_cap_binding_valid=verified == cap,
            deterministic_ceiling=ceiling, score_caps=audit['result_score_caps'],
            capture_summaries=[{'sequence': item['sequence'], 'operation': item['operation'],
                'source_kind': item['capture']['source_kind'], 'complete': item['complete_scientific_bundle'],
                'cap_applicable': item['cap_applicable'], 'scientific_decision': item.get('scientific_decision'),
                'gate': item['gate'], 'contract_status': item['contract']['status']} for item in audit['bundles']],
            changed_snapshot_rejected=rejected,
            positive_real_stage_capture=bool(audit['bundles']) and audit['bundles'][0]['complete_scientific_bundle']
                and audit['bundles'][0]['capture']['source_kind'] == 'receipt_referenced_stage')
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        cleanup = cleanup_owned_runtime(owner)
        report.update(error=error, cleanup=cleanup, runtime_seconds=round(time.monotonic() - started, 3),
                      candidate_unchanged=tree_digest(candidate) == args.expected_candidate_digest)
        write_json(output / 'diagnostic_report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not error and report.get('positive_real_stage_capture') and report.get('production_cap_binding_valid') and report.get('changed_snapshot_rejected') and report['candidate_unchanged'] and cleanup['all_absent'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
