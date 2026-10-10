"""Evaluator-private immutable checkpoints for dev lower and scoring stages.

An interrupted/unknown lower invocation is never restarted automatically.
Scoring recovery re-enters the same task adapter/output directory, whose shared
scoring_intent owns the single logical provider request. This module does not
delete intents, change request IDs, infer retries from aggregate token counts,
or turn unresolved evidence into an accepted capability round.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

try:
    from .protocol import canonical_json, sha256_file, tree_digest
except ImportError:
    from protocol import canonical_json, sha256_file, tree_digest


class RecoveryError(RuntimeError):
    pass


def read(path: Path):
    if path.is_symlink() or not path.is_file():
        raise RecoveryError('checkpoint missing or symlinked: ' + path.name)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RecoveryError('checkpoint is unreadable: ' + path.name) from exc


def immutable_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(value)
    try:
        with path.open('xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != payload:
            raise RecoveryError('checkpoint already exists with different contents: ' + path.name)


def evidence_inventory(root: Path, *, lower=False):
    """Byte/link identity, excluding only evaluator-added downstream outputs."""
    result = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if lower and (rel.parts[0] == 'semantic_result' or rel.as_posix() == 'controller_observation.json'):
            continue
        if path.is_symlink():
            result[rel.as_posix()] = {'link': os.readlink(path)}
        elif path.is_file():
            result[rel.as_posix()] = {'sha256': sha256_file(path)}
        elif not path.is_dir():
            raise RecoveryError('unquiesced/non-regular execution evidence: ' + rel.as_posix())
    return result


def execution_identity(controller, candidate_digest: str):
    """Bind execution semantics, not a replaceable broker port or test code."""
    source = {}
    for directory in ('agentloop', 'evaluator'):
        for path in sorted((controller.benchmark / directory).rglob('*')):
            relative = path.relative_to(controller.benchmark)
            if '__pycache__' in relative.parts or 'tests' in relative.parts or path.suffix == '.pyc':
                continue
            if path.is_file() and not path.is_symlink():
                source[relative.as_posix()] = sha256_file(path)
    shared = Path('@@AGENTSWE_EDITING_CONTROL@@')
    for name in ('execution_scoring.py', 'execution_contract.py', 'result_judge.py',
                 'responses_stream.py', 'judge_broker_runtime.py', 'judge_broker_xhigh.py'):
        path = shared / name
        if not path.is_file():
            raise RecoveryError('shared execution source is unavailable: ' + name)
        source['shared/' + name] = sha256_file(path)
    return {'schema_version': 'agentswe-ai-stage-identity-v1',
            'candidate_digest': candidate_digest, 'runtime_sources': source,
            'image': controller.image,
            'dependency_overlay_digest': tree_digest(controller.dependency_overlay) if controller.dependency_overlay else None,
            'run_kind': controller.run_kind, 'dry_run': controller.dry_run,
            'public_case_ids': list(controller.public_case_ids),
            'max_dev_rounds': controller.max_dev_rounds, 'n_concurrent': controller.n_concurrent,
            'lower_model': 'deepseek-flash', 'lower_effort': 'high',
            'judge_model': 'deepseek-flash', 'judge_effort': 'max'}


class CaseStages:
    def __init__(self, output: Path, identity: dict):
        self.output = output
        # A sibling, never inside the lower process's writable output mount.
        self.root = output.parent / ('.controller-stage-' + output.name)
        if self.root.is_symlink():
            raise RecoveryError('controller checkpoint directory is symlinked')
        identity_path = self.root / 'identity.json'
        if identity_path.exists():
            if read(identity_path) != identity:
                raise RecoveryError('execution identity changed; previous stages cannot be resampled')
        else:
            if evidence_inventory(output):
                raise RecoveryError('legacy/unbound output requires an independent recovery audit')
            immutable_json(identity_path, identity)

    def lower(self, execute):
        attempts = sorted(self.root.glob('lower_[0-9][0-9][0-9]'))
        if attempts:
            last = attempts[-1]
            receipt = last / 'receipt.json'
            if not receipt.exists():
                raise RecoveryError('lower invocation is in-flight or unknown; automatic replay forbidden')
            saved = read(receipt)
            if evidence_inventory(self.output, lower=True) != saved['evidence']:
                raise RecoveryError('lower evidence changed after its completion checkpoint')
            if saved['state'] == 'finished':
                return copy.deepcopy(saved['result'])
            if saved['state'] != 'not_started' or saved['evidence'] or len(attempts) >= 3:
                raise RecoveryError('lower retry lacks verified pre-launch failure or exceeds retry bound')
        number = len(attempts) + 1
        attempt = self.root / f'lower_{number:03d}'
        # mkdir is the exclusive ownership reservation, including across processes.
        try:
            attempt.mkdir()
        except FileExistsError as exc:
            raise RecoveryError('another evaluator owns the lower stage') from exc
        immutable_json(attempt / 'intent.json', {'output': str(self.output), 'invocation': number})
        result = execute()
        state = 'not_started' if result.get('dispatch_not_started') is True else 'finished'
        immutable_json(attempt / 'receipt.json', {'state': state, 'result': result,
            'evidence': evidence_inventory(self.output, lower=True)})
        return result

    def score(self, evaluate):
        completed = self.root / 'score_completed.json'
        output = self.output / 'semantic_result'
        if completed.exists():
            receipt = read(completed)
            if evidence_inventory(output) != receipt['evidence']:
                raise RecoveryError('completed scoring evidence changed; no replacement judgment allowed')
            return copy.deepcopy(receipt['evaluation'])
        # Never change this output path: the shared scorer's durable intent
        # prevents a second logical provider request after an unknown failure.
        evaluation = evaluate()
        if evaluation.get('contract_valid') is True and evaluation.get('round_consumed') is True:
            immutable_json(completed, {'evaluation': evaluation, 'evidence': evidence_inventory(output)})
        else:
            immutable_json(self.root / 'scoring_observations' / (uuid.uuid4().hex + '.json'), evaluation)
        return evaluation

    def observe(self, result):
        path = self.root / 'observations' / (uuid.uuid4().hex + '.json')
        immutable_json(path, result)
        return path
