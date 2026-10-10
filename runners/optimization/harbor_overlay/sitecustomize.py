"""Install the optimization-only Harbor trial overlay.

Harbor is installed as a regular Python package, so putting a second
``harbor`` directory earlier on ``PYTHONPATH`` is not sufficient to shadow
``harbor.trial.single_step``.  This sitecustomize hook prepends the overlay's
``harbor/trial`` directory to the already imported package path.  The hook is
enabled only when the resumable optimization runner explicitly opts in.
"""

from __future__ import annotations

import os
from pathlib import Path


if os.environ.get("OPTIMIZATION_INFRA_RESUME", "").lower() in {"1", "true", "yes"}:
    import harbor.trial as harbor_trial

    overlay_root = Path(
        os.environ.get("OPTIMIZATION_HARBOR_OVERLAY", Path(__file__).resolve().parent)
    ).resolve()
    overlay_trial = overlay_root / "harbor" / "trial"
    if not overlay_trial.is_dir():
        raise RuntimeError(
            "OPTIMIZATION_INFRA_RESUME is enabled but the Harbor overlay is "
            f"missing: {overlay_trial}"
        )
    path = str(overlay_trial)
    current = list(harbor_trial.__path__)
    if path not in current:
        # ``harbor.trial`` is a namespace package in the installed Harbor
        # build, whose ``_NamespacePath`` has no ``insert``.
        harbor_trial.__path__ = [path, *current]
