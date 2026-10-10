"""Runner modules, selected by task.json `runner`. Each exposes run(), status(), result(), stop()."""
from __future__ import annotations

import importlib


def load(name: str):
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        raise SystemExit(f"runner {name!r} is not available yet") from exc
