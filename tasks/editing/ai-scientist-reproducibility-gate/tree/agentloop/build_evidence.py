"""Bind a failed, isolated compilation to evaluator-owned Candidate-zero evidence.

No product rollout is fabricated. A compile failure can stop the product before
any lower-model call, provided the actual baseline and resource checks passed.
"""
from pathlib import Path
import sys

try:
    from .protocol import read_json, sha256_file, tree_digest
except ImportError:
    from protocol import read_json, sha256_file, tree_digest


def verified_build_failure(build, build_dir, candidate):
    build_dir, candidate = Path(build_dir).resolve(), Path(candidate).resolve()
    if not (build.get('valid') is False
            and build.get('classification') == 'candidate_build_failure'
            and build.get('failure') == 'python_compileall'
            and build.get('causal_candidate_failure') is True
            and build.get('infrastructure_health', {}).get('valid') is True
            and build.get('infrastructure_health', {}).get('baseline_compile', {}).get('valid') is True
            and str(build.get('infrastructure_health', {}).get('baseline_compile', {}).get('compiler', '')).startswith('3.11.')):
        raise ValueError('No causal Candidate compilation failure')
    manifest = build_dir / 'build_manifest.json'
    if manifest.is_symlink() or read_json(manifest) != build:
        raise ValueError('Build manifest is unavailable or changed')
    resource = Path(build['resource_attestation_path'])
    if (resource.is_symlink() or not resource.is_file()
            or build_dir not in resource.resolve().parents
            or sha256_file(resource) != build.get('resource_attestation_sha256')):
        raise ValueError('Build resource evidence is unavailable or changed')
    attestation = read_json(resource)
    if not (attestation.get('valid') is True and attestation.get('timed_out') is False
            and attestation.get('purpose') == 'build'
            and attestation.get('memory_bytes') == 4 * 1024**3
            and 0 < attestation.get('timeout_seconds', 0) <= 1800
            and attestation.get('cleanup', {}).get('complete') is True
            and (not attestation.get('aggregate_parent', {}).get('created_here')
                 or attestation.get('aggregate_cleanup', {}).get('complete') is True)):
        raise ValueError('Build resource or cleanup contract failed')
    digest = build.get('candidate_repo_digest')
    if (not candidate.is_dir() or tree_digest(candidate) != digest
            or tree_digest(build_dir / 'repository') != digest):
        raise ValueError('Compiled Candidate source is unavailable or changed')
    return {'manifest_path': str(manifest), 'manifest_sha256': sha256_file(manifest),
            'resource_path': str(resource), 'resource_sha256': sha256_file(resource),
            'candidate_repo_digest': digest}


def build_failure_record(*, build, build_dir, candidate, case_id, task):
    proof = verified_build_failure(build, build_dir, candidate)
    return {'case_id': case_id, 'valid': False, 'execution_stage': 'build',
            'classification': 'candidate_build_failure', 'classification_axis': 'candidate',
            'candidate_digest': proof['candidate_repo_digest'],
            'real_execution': False, 'execution_attempted': True,
            'environment_preflight': {'valid': True, 'scope': 'isolated compilation and baseline'},
            'integrity': {'candidate_repo_digest': proof['candidate_repo_digest']},
            'task_sha256': sha256_file(task), 'build_evidence': proof,
            'broker': {'calls_delta': 0, 'failures_delta': 0, 'successful_calls': 0},
            'failure_attribution': {'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                'reason': 'Candidate Python syntax failed compilation after the unmodified baseline compiled successfully in the verified build environment.',
                'evidence_paths': [proof['manifest_path'], proof['resource_path']]}}


def validate_build_record(record, *, candidate, case_id, task):
    proof = record.get('build_evidence', {})
    manifest = Path(proof['manifest_path'])
    if manifest.is_symlink() or sha256_file(manifest) != proof.get('manifest_sha256'):
        raise ValueError('Recorded build manifest changed')
    expected = build_failure_record(build=read_json(manifest), build_dir=manifest.parent,
        candidate=candidate, case_id=case_id, task=task)
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError('Case compilation evidence does not match the verified build')
    return expected


def candidate_zero_contract_valid(record):
    """Validate the actual shared contract rather than a boolean in a ledger."""
    try:
        evaluation = record.get('result_evaluation', {})
        if not (evaluation.get('classification') == 'candidate_zero'
                and evaluation.get('score') == 0
                and evaluation.get('contract_valid') is True
                and evaluation.get('round_consumed') is True):
            return False
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        from execution_contract import classify_candidate_execution, candidate_zero_result_contract
        verdict = classify_candidate_execution(record, case_id=record['case_id'],
            candidate_digest=record['candidate_digest'])
        expected = candidate_zero_result_contract(verdict, case_id=record['case_id'],
            candidate_digest=record['candidate_digest'])
        contract = Path(evaluation['contract_path'])
        return not contract.is_symlink() and read_json(contract) == expected
    except (OSError, ValueError, TypeError, KeyError):
        return False


def candidate_zero_attribution_verified(record):
    """True when the shared contract already sees a causal Candidate zero.

    ``candidate_zero_contract_valid`` can only speak after ``score_case`` wrote
    the contract.  Hidden cases are scored by the formal finalizer, which never
    runs while the completeness gate cannot see the zero, so the gate consults
    the same shared classifier on the same trusted execution record.
    """
    try:
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        from execution_contract import classify_candidate_execution
        verdict = classify_candidate_execution(record, case_id=record['case_id'],
            candidate_digest=record['candidate_digest'])
        return verdict.get('classification') == 'candidate_zero'
    except (OSError, ValueError, TypeError, KeyError):
        return False
