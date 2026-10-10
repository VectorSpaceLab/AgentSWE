"""Readiness review outside the source tree, bound to immutable run evidence.

This only replaces a task's self-declared readiness flag. The caller must
independently pass all existing smoke, source, isolation and scoring checks.
No command in this module creates or grants an admission.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = "single-dev-two-round-hidden-smoke-v1"
# AGENTSWE_EDITING_FORMAL_GATE value that launch_formal_task.py --integrity-only (a release install's `agentswe run`)
# sets on the formal unit: require_formal_readiness keeps its source checks and needs no readiness admission.
RELEASE_INTEGRITY = "release-integrity"

REQUIRED_CHECKS=(
    'current_source_and_contract_reviewed',
    'real_native_builder_two_dev_acceptance',
    'feedback_and_frozen_candidate_verified',
    'real_medium_product_execution',
    'artifact_trajectory_oracle_provenance',
    'independent_xhigh_result_and_code',
    'owned_cleanup_verified',
    'formal_entry_matches_reviewed_execution',
    'configuration_prerequisites_resolved',
)

def sha(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('invalid evidence file')
    return hashlib.sha256(path.read_bytes()).hexdigest()

def registry_digest(registry_path, task):
    """Digest this task's configuration row, not the whole registry file.

    A whole-file digest made every task's binding depend on every other task's
    configuration: because the binding is recomputed from current bytes at
    admission, one rebind performed for task B retroactively invalidated task
    A's finished bundle even though task A's own source never changed. Scoped
    per row, task A's evidence only decays when task A's own row changes.

    The schema version is digested alongside the row so a registry format
    change is still caught for every task. Top-level prose (scope, status,
    registry_sync_* history) is deliberately outside the digest: nothing reads
    it, and including it would restore exactly the coupling this removes.
    """
    registry = json.loads(_registry_bytes(registry_path))
    if registry.get('schema_version') != 'agentswe-edit-configuration-registry/v1':
        raise ValueError('unexpected configuration registry schema')
    tasks = registry.get('tasks')
    if not isinstance(tasks, dict) or task not in tasks:
        raise ValueError('configuration registry has no row for ' + str(task))
    payload = {'schema_version': registry['schema_version'], 'task': task, 'row': tasks[task]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _registry_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing or symlinked configuration registry: ' + str(path))
    return path.read_bytes()

def check_admission(directory, *, task, sibling_digest, contract_path, smoke_path):
    path=Path(directory)/(task+'.json')
    if not path.exists():return False, None
    try:
        value=json.loads(path.read_text())
        if path.is_symlink() or value.get('schema_version')!='agentswe-edit-readiness-admission/v2':
            raise ValueError('admission schema/path invalid')
        if value.get('profile') != PROFILE:
            raise ValueError('v2 readiness profile required')
        if value.get('task')!=task or value.get('readiness')!='READY':
            raise ValueError('admission task/status mismatch')
        if value.get('sibling_digest')!=sibling_digest:
            raise ValueError('admission source drift')
        for field,expected in (('contract',contract_path),('smoke_manifest',smoke_path)):
            ref=value.get(field,{})
            if ref.get('path')!=str(expected) or ref.get('sha256')!=sha(expected):
                raise ValueError('admission '+field+' drift')
        checks=value.get('review_checks',{})
        if any(checks.get(key) is not True for key in REQUIRED_CHECKS):
            raise ValueError('admission review incomplete')
        reports=value.get('review_reports')
        if not isinstance(reports,list) or not reports:
            raise ValueError('admission review report missing')
        for ref in reports:
            evidence=Path(ref.get('path',''))
            if not evidence.is_absolute() or sha(evidence)!=ref.get('sha256'):
                raise ValueError('admission review report drift')
        # Admission is coordinator-owned; the runner supplies only its bundle.
        # Derive current source/contract/registry from evaluator files, not bundle claims.
        from v2_readiness import load_and_validate
        from readiness_judge_validation import make_judge_output_validators
        from v2_usage_normalizers import make_broker_record_normalizers
        bundle_root = Path(value.get('bundle_root', ''))
        if not bundle_root.is_absolute() or bundle_root.is_symlink():
            raise ValueError('invalid evaluator bundle root')
        bundle_root.resolve(strict=True).relative_to(Path(smoke_path).parent.resolve(strict=True))
        ok, reasons = load_and_validate(smoke_path,
            bundle_root=bundle_root,
            expected_manifest_sha256=value['smoke_manifest']['sha256'],
            trusted_current_binding={'source_digest':sibling_digest,
                'contract_digest':sha(contract_path),
                'registry_digest':registry_digest(ROOT/'configuration_delta_registry.json',task),
                'task':task},
            judge_output_validators=make_judge_output_validators(Path(contract_path).parent.parent,
                code_implementation=Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py')),
            broker_record_normalizers=make_broker_record_normalizers(task))
        if not ok:
            raise ValueError('v2 evidence rejected: ' + '; '.join(reasons))
        return True, {'path':str(path),'sha256':sha(path), 'profile':PROFILE}
    except (OSError,ValueError,TypeError,AttributeError,KeyError) as exc:
        return False, {'path':str(path),'error':str(exc)}

def release_integrity(sibling,task,tree_digest):
    """The source checks of require_formal_readiness without its READY gate row and admission (release install)."""
    digest=tree_digest(sibling)
    snapshot_path=ROOT/'post_repair_tree_snapshot.json'
    snapshot=json.loads(snapshot_path.read_text())
    if snapshot['tasks'][task]['sibling']['digest']!=digest:
        raise ValueError('the rendered task tree differs from the snapshot setup recorded; run `agentswe setup` again')
    baseline=json.loads((ROOT/'pre_repair_tree_snapshot.json').read_text())['tasks'][task]['source']
    if tree_digest(Path(baseline['path']))!=baseline['digest']:
        raise ValueError('the release template tree changed after setup')
    return {'task':task,'sibling_digest':digest,'readiness':'not required (release integrity check)',
        'admission':None,'gate':None,'mode':RELEASE_INTEGRITY,
        'snapshot':{'path':str(snapshot_path),'sha256':sha(snapshot_path)}}

def require_formal_readiness(sibling):
    """Admit a formal task without mutating its executed source contract.

    The shared launcher performs full configuration validation. This task-local
    guard independently requires the ten-task gate and rechecks this task's
    current source, original source, snapshot and hash-bound external review.
    Pilot execution must not call this function.

    With AGENTSWE_EDITING_FORMAL_GATE=release-integrity exactly (the release
    formal launch), only the source checks run: task/source identity, the
    current tree equals the snapshot setup recorded, the original (release
    template) source is unchanged. The READY gate row and the admission are a
    benchmark-construction gate a release install does not have. Any other
    value, or none, keeps the full gate.
    """
    from audit_readiness import tree_digest
    sibling=Path(sibling).resolve(strict=True)
    contract_path=sibling/'meta/0905_case_contract.json'
    contract=json.loads(contract_path.read_text())
    task=contract.get('task')
    spec=importlib.util.spec_from_file_location('admission_formal_config',ROOT/'formal_config.py')
    if spec is None or spec.loader is None:raise ValueError('formal configuration unavailable')
    cfg=importlib.util.module_from_spec(spec);spec.loader.exec_module(cfg)
    if task not in cfg.TASKS or sibling!=Path(cfg.TASKS[task]) or contract.get('sibling')!=str(sibling):
        raise ValueError('formal task/source identity mismatch')
    if os.environ.get('AGENTSWE_EDITING_FORMAL_GATE')==RELEASE_INTEGRITY:
        return release_integrity(sibling,task,tree_digest)
    gate_path=ROOT/'formal_readiness_gate.json'
    gate=json.loads(gate_path.read_text())
    gate_tasks=gate.get('tasks',[])
    # Per-task gate (2026-09-19): formal execution of THIS task requires its own
    # current READY row; other tasks' readiness no longer blocks it.
    row=next((x for x in gate_tasks if x.get('task')==task),None)
    if (gate.get('profile') != PROFILE or row is None or row.get('readiness')!='READY' or row.get('errors')
        or task in (gate.get('source_differences') or [])):
        raise ValueError('formal execution requires a current READY readiness gate row for '+str(task))
    digest=tree_digest(sibling)
    snapshot=json.loads((ROOT/'post_repair_tree_snapshot.json').read_text())
    if snapshot['tasks'][task]['sibling']['digest']!=digest:
        raise ValueError('formal sibling differs from reviewed snapshot')
    baseline=json.loads((ROOT/'pre_repair_tree_snapshot.json').read_text())['tasks'][task]['source']
    if tree_digest(Path(baseline['path']))!=baseline['digest']:
        raise ValueError('authoritative original source changed')
    smoke_path=cfg.SMOKE_ROOT/task/'latest_smoke_manifest.json'
    valid,evidence=check_admission(ROOT/'readiness_admissions',task=task,
        sibling_digest=digest,contract_path=contract_path,smoke_path=smoke_path)
    if not valid:raise ValueError('current evaluator readiness admission missing or invalid: '+str(evidence))
    return {'task':task,'sibling_digest':digest,'readiness':'READY',
        'admission':evidence,'gate':{'path':str(gate_path),'sha256':sha(gate_path)}}
