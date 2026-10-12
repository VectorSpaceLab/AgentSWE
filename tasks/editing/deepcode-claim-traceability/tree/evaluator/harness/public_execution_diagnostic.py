"""Read a bounded public diagnostic from controller-owned terminal evidence.

The controller supplies all paths; no Candidate field selects an evidence file.
This is not a signature against an evaluator-host writer. Its trust boundary is
the run-owned parent directory which is excluded from the Candidate sandbox.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json
import hashlib
import math
import os
from pathlib import Path
import re
import stat


@dataclass(frozen=True)
class PublicExecutionDiagnostic:
    case_id: str
    case_total_budget_seconds: int
    work_deadline_seconds: int
    cleanup_reserve_seconds: int
    elapsed_seconds: float
    scope_timed_out: bool
    cleanup_complete: bool
    final_artifact_present: bool
    execution_observation_present: bool
    last_completed_phase: None = None
    phase_timing_available: bool = False

    def public_dict(self):
        value = asdict(self)
        case_id = value.pop('case_id')
        return {'case_id': case_id, 'public_execution_diagnostic': value}


def _safe(path: Path, *, missing=False) -> bool:
    if not path.is_absolute() or '..' in path.parts:
        return False
    for part in (path, *path.parents):
        try:
            if stat.S_ISLNK(part.lstat().st_mode):
                return False
        except FileNotFoundError:
            if not missing:
                return False
    return True


def _read(path: Path):
    if not _safe(path):
        raise ValueError('unsafe evidence path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise ValueError('invalid evidence file')
        return json.loads(handle.read(65537))


def _present(path: Path):
    if not _safe(path, missing=True):
        raise ValueError('unsafe output path')
    if not path.exists():
        return False
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('invalid output file')
    return True


def collect_public_diagnostic(*, run_dir: Path, evaluation_root: Path,
                              repository: Path, case_root: Path, case_id: str, candidate_digest: str):
    """Return None on absent/invalid evidence; never change execution semantics."""
    try:
        if type(candidate_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', candidate_digest):
            return None
        if case_id not in ('dev_001', 'dev_002'):
            return None
        if not _safe(run_dir) or not _safe(evaluation_root):
            return None
        if evaluation_root.parent != run_dir / 'evaluations':
            return None
        if not re.fullmatch(r'candidate_[0-9]{3}_attempt_[0-9]+', evaluation_root.name):
            return None
        if repository.parent != run_dir / 'candidates' or not _safe(repository, missing=True):
            return None
        if not re.fullmatch(r'candidate_[0-9]{3}', repository.name):
            return None
        if not _safe(case_root) or case_root.name != case_id or case_root.parent.name != 'dev_cases':
            return None
        output = evaluation_root / case_id
        if not _safe(output):
            return None
        request = _read(evaluation_root / (case_id + '-case-request.json'))
        if not isinstance(request, dict) or any(request.get(k) != str(v) for k, v in
                [('repository', repository), ('case', case_root), ('output', output)]):
            return None
        if request.get('case_id') != case_id:
            return None
        if 'candidate_digest' in request:
            if request['candidate_digest'] != candidate_digest:
                return None
        else:
            # Earlier controller requests lack this field. Require the existing
            # launcher-owned pre-execution context; never rewrite historical bytes.
            context = _read(output / 'logical-context.json')
            if (not isinstance(context, dict) or context.get('case_id') != case_id
                    or context.get('candidate_digest_before') != candidate_digest):
                return None
            if not _safe(case_root / 'input.md'):
                return None
            if context.get('task_sha256') != hashlib.sha256((case_root / 'input.md').read_bytes()).hexdigest():
                return None
        deadline = request.get('deadline')
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0:
            return None
        resource_dir = evaluation_root / (case_id + '-case-resources')
        att = _read(resource_dir / 'resource-attestation.json')
        ownership = _read(resource_dir / 'scope-ownership.json')
        ready = _read(resource_dir / 'scope-ready.json')
        if not isinstance(att, dict) or att.get('schema_version') != 'agentswe-owned-case-resources/v1':
            return None
        if att.get('valid') is not True or att.get('configuration_delta') != []:
            return None
        unit = att.get('unit')
        if not isinstance(unit, str) or not re.fullmatch(r'agentswe-edit-owner-c-[0-9a-f]{32}\.scope', unit):
            return None
        token = unit.removeprefix('agentswe-edit-owner-c-').removesuffix('.scope')
        cgroup = '/system.slice/' + unit
        if att.get('cgroup') != cgroup or att.get('description') != 'AgentSWE case owner ' + token:
            return None
        if not isinstance(ownership, dict) or any(ownership.get(k) != att.get(k)
                for k in ('unit', 'description', 'cgroup', 'memory_bytes')):
            return None
        if type(ownership.get('memory_bytes')) is not int:
            return None
        for key, expected in [('timeout_seconds', 600), ('independent_scope_deadline_seconds', 590),
                              ('cleanup_reserve_seconds', 10), ('memory_bytes', 4294967296)]:
            if type(att.get(key)) is not int or att[key] != expected:
                return None
        elapsed = att.get('elapsed_seconds')
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 605:
            return None
        if type(att.get('timed_out')) is not bool:
            return None
        cleanup = att.get('cleanup')
        if not isinstance(cleanup, dict) or type(cleanup.get('complete')) is not bool:
            return None
        if cleanup['complete']:
            verified_stop = (cleanup.get('unit_state') in (None, 'inactive', 'failed')
                and cleanup.get('identity_verified_before_stop') is True)
            absent = (cleanup.get('unit_absent') is True
                and cleanup.get('identity_verified_before_stop') is False)
            events = cleanup.get('cgroup_events')
            if not (verified_stop or absent) or type(events) is not str or events.strip() != 'populated 0':
                return None
        observed, props = att.get('observed'), att.get('systemd_properties')
        if not isinstance(observed, dict) or not isinstance(props, dict):
            return None
        if not isinstance(ready, dict) or ready != observed:
            return None
        if type(observed.get('pid')) is not int or observed['pid'] <= 0 or observed.get('cgroup') != cgroup:
            return None
        if observed.get('memory_max') != '4294967296' or observed.get('memory_swap_max') != '0':
            return None
        if props.get('MemoryMax') not in (None, '4294967296') or props.get('MemorySwapMax') not in (None, '0'):
            return None
        if props.get('ControlGroup') != cgroup or props.get('Id') != unit or props.get('Description') != att['description']:
            return None
        return PublicExecutionDiagnostic(case_id, 600, 590, 10, float(elapsed), att['timed_out'],
            cleanup['complete'], _present(output / 'workspace' / 'agent_result.json'),
            _present(output / 'execution_observation.json'))
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
