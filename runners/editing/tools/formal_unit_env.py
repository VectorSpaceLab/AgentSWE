#!/usr/bin/env python3
"""The environment a formal Editing unit runs with (never a credential).

launch_formal_task.py sets these variables on the systemd unit of a formal run and records them in the run's
launch_record.json (`unit_environment`). budget_freeze.py runs a formal run's held-out cases and finalizer after the
unit has ended, with the same variables: from the launch record when it has them, else from this function.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

FORMAL_GATE_RELEASE = 'release-integrity'  # readiness_admission.RELEASE_INTEGRITY
# Task-required environment (same table as launch_readiness.py): evaluator-owned evidence paths plus their digests,
# never credentials. deeptutor refuses to run without its prior-product guard (a launch without it dies at startup).
TASK_ENVIRONMENT = {'deeptutor': {'AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD':
                                  '@@AGENTSWE_EDITING_STATE@@/deeptutor-prior-product-guard.json'}}
TASK_ENVIRONMENT_DIGESTS = {'deeptutor': {'AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD_SHA256':
                                          'AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD'}}


class TaskEnvironmentMissing(FileNotFoundError):
    pass


def unit_environment(task: str, cfg, *, integrity_only: bool) -> dict[str, str]:
    """The --setenv variables of a formal unit of `task` (cfg: the control plane's formal_config module)."""
    env = {'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'AGENTSWE_RESULT_JUDGE': str(cfg.RESULT_JUDGE),
           'AGENTSWE_CODE_JUDGE': str(cfg.CREATE_CODE_JUDGE),
           'AGENTSWE_CREATE_ALIGNMENT_SNAPSHOT': str(cfg.ALIGNMENT_SNAPSHOT),
           'AGENTSWE_EDIT_PROFILE_SCOPE': 'codex_xhigh_only',
           # FORMAL cells only.  A completed judge response whose
           # ANSWER is unusable -- it does not parse even after the deterministic
           # envelope repair, or it stops before the required top-level keys -- gets
           # exactly ONE further logical request with the same prompt.  A complete
           # object the judge merely disagrees with is never resampled.  Readiness
           # pilots run under launch_readiness.py and never see this variable, so
           # their `exactly one broker request` smoke assertions are untouched.
           'AGENTSWE_EDIT_EARLY_STOP_RESAMPLE': '1'}
    if integrity_only:
        env['AGENTSWE_EDITING_FORMAL_GATE'] = FORMAL_GATE_RELEASE
    for key, value in TASK_ENVIRONMENT.get(task, {}).items():
        if not Path(value).is_file():
            raise TaskEnvironmentMissing(value)
        env[key] = value
    for key, source in TASK_ENVIRONMENT_DIGESTS.get(task, {}).items():
        env[key] = hashlib.sha256(Path(env[source]).read_bytes()).hexdigest()
    return env
