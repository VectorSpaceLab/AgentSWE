"""Finish a task-local readiness bundle after real unit exit, without admission.

The coordinator invokes this once per terminal run. Cleanup precedes source
admission so a failed/unknown run still releases retained Docker resources.
No provider calls, candidate replay, or gate writes occur here.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

import formal_config as cfg
from readiness_binding import verify_binding
from readiness_cleanup import finalize as cleanup, verify_completed_cleanup
from v2_readiness import load_and_validate
from v2_usage_normalizers import make_broker_record_normalizers
from readiness_judge_validation import make_judge_output_validators


def finalize(task, run, unit, binding_file, binding_sha256):
    if task not in cfg.TASKS:
        raise ValueError('unknown task')
    run = Path(run).resolve(strict=True)
    run.relative_to((cfg.SMOKE_ROOT/task).resolve(strict=True))
    binding_file = Path(binding_file)
    if binding_file.is_symlink() or hashlib.sha256(binding_file.read_bytes()).hexdigest() != binding_sha256:
        raise ValueError('coordinator binding file changed')
    binding = json.loads(binding_file.read_bytes())
    if binding.get('task') != task:
        raise ValueError('binding task mismatch')
    cleanup_dir = run/'coordinator_cleanup'
    if (cleanup_dir/'cleanup.json').is_file():
        cleanup_ref = verify_completed_cleanup(run, unit, run/'readiness_retained_resources.json', cleanup_dir)
    else:
        cleanup_ref = cleanup(run, unit, run/'readiness_retained_resources.json', cleanup_dir)
    # Never normalize an unknown/outdated run into a valid current bundle.
    source = Path(cfg.TASKS[task])
    verify_binding(source, binding)
    exporter = source/'evaluator/readiness_bundle.py'
    if not exporter.is_file() or exporter.is_symlink():
        raise ValueError('task exporter has not been deployed')
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location('task_readiness_bundle', exporter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    normalizers = make_broker_record_normalizers(task)
    # Packaging is read-only over current-run raw evidence. Preserve failed
    # export attempts and retry packaging in a fresh directory; no request or
    # Candidate is dispatched again, and completed cleanup is only verified.
    attempt = uuid.uuid4().hex
    result = module.export(run, run/('readiness_bundle_'+attempt), cleanup_receipt=Path(cleanup_ref['path']),
                           trusted_binding=binding, broker_normalizers=normalizers)
    manifest = Path(result['bundle_root'])/'manifest.json'
    valid, errors = load_and_validate(manifest, bundle_root=Path(result['bundle_root']),
        expected_manifest_sha256=result['manifest_sha256'], trusted_current_binding=binding,
        judge_output_validators=make_judge_output_validators(source,
            code_implementation=cfg.AUTHORITATIVE_CREATE_CODE_JUDGE), broker_record_normalizers=normalizers)
    receipt = {'task': task, 'run': str(run), 'cleanup': cleanup_ref, **result,
               'validation_passed': valid, 'errors': errors, 'admission_required': True,
               'pipeline_ready': False, 'provider_calls': 0}
    with (run/('readiness_finalize_receipt_'+attempt+'.json')).open('x') as stream:
        json.dump(receipt, stream, indent=2)
        stream.write('\n')
    if not valid:
        raise ValueError('task bundle validation failed: ' + '; '.join(errors))
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--binding-file', type=Path, required=True)
    parser.add_argument('--binding-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(finalize(args.task, args.run_dir, args.unit, args.binding_file, args.binding_sha256)))


if __name__ == '__main__':
    main()
