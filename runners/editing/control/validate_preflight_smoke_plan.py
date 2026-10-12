#!/usr/bin/env python3
"""Validate the provider-free Edit smoke plan against current matrix and gate."""
from __future__ import annotations
import argparse, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from audit_readiness import tree_digest

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', type=Path, default=Path(__file__).with_name('preflight_smoke_plan.json'))
    ap.add_argument('--matrix', type=Path, default=Path(__file__).with_name('case_coverage_matrix.json'))
    ap.add_argument('--gate', type=Path, default=Path(__file__).with_name('formal_readiness_gate.json'))
    ap.add_argument('--output', type=Path, default=Path(__file__).with_name('preflight_smoke_plan_validation.json'))
    args = ap.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    matrix = json.loads(args.matrix.read_text(encoding='utf-8'))
    gate = json.loads(args.gate.read_text(encoding='utf-8'))
    errors: list[str] = []
    if plan.get('provider_calls_started') is not False:
        errors.append('plan provider_calls_started must be false')
    if plan.get('formal_launch_authorized') is not False:
        errors.append('plan formal_launch_authorized must be false')
    snapshot = plan.get('gate_snapshot', {})
    for key in ('ready_count', 'expected_count', 'formal_ready', 'formal_launch_authorized', 'formal_evaluation_started'):
        if snapshot.get(key) != gate.get(key):
            errors.append(f'gate drift: {key}')
    matrix_tasks = {x['task']: x for x in matrix.get('tasks', [])}
    plan_tasks = {x['task']: x for x in plan.get('tasks', [])}
    if set(matrix_tasks) != set(plan_tasks) or len(plan_tasks) != 10:
        errors.append('task set/count drift')
    for name, task in matrix_tasks.items():
        row = plan_tasks.get(name, {})
        for key in ('sibling_digest', 'real_lower_entry', 'dev_cases', 'hidden_cases', 'smoke_manifest'):
            if row.get(key) != task.get(key):
                errors.append(f'{name}: {key} drift')
        broker = row.get('broker', {})
        if broker.get('model') != task.get('broker_model') or broker.get('effort') != task.get('broker_effort'):
            errors.append(f'{name}: broker drift')
        sibling = Path(str(task.get('sibling', '')))
        if not sibling.is_absolute() or not sibling.is_dir():
            errors.append(f'{name}: actual sibling missing')
        elif tree_digest(sibling) != task.get('sibling_digest'):
            errors.append(f'{name}: actual sibling digest drift')
        contract = Path(str(task.get('contract_path', '')))
        if not contract.is_absolute() or not contract.is_file():
            errors.append(f'{name}: actual contract missing')
        elif sha(contract) != task.get('contract_digest'):
            errors.append(f'{name}: actual contract digest drift')
    receipt = {
        'schema_version': 'agentswe-edit-preflight-smoke-plan-validation-v1',
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'valid': not errors,
        'provider_calls': 0,
        'errors': errors,
        'plan_sha256': sha(args.plan),
        'matrix_sha256': sha(args.matrix),
        'gate_sha256': sha(args.gate),
        'task_count': len(plan_tasks),
    }
    args.output.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if not errors else 1

if __name__ == '__main__':
    raise SystemExit(main())
