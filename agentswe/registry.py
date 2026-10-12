"""Task registry: every tasks/<family>/<id>/task.json."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT

FAMILIES = ("creation", "editing", "optimization")


@dataclass
class Task:
    data: dict
    dir: Path

    def __getattr__(self, key):
        try:
            return self.data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    @property
    def label(self) -> str:
        return f"{self.data['family']}/{self.data['id']}"


def load_all() -> list[Task]:
    tasks = []
    for path in sorted(REPO_ROOT.glob("tasks/*/*/task.json")):
        data = json.loads(path.read_text())
        if path.parent.name != data["id"] or path.parent.parent.name != data["family"]:
            raise ValueError(f"{path}: id/family do not match the directory")
        tasks.append(Task(data=data, dir=path.parent))
    return tasks


def find(name: str) -> Task:
    matches = [t for t in load_all() if name in (t.data["id"], t.data.get("short"), t.label)]
    if not matches:
        raise SystemExit(f"unknown task {name!r}; see `agentswe list`")
    if len(matches) > 1:
        raise SystemExit(f"ambiguous task {name!r}: {[t.label for t in matches]}")
    return matches[0]
