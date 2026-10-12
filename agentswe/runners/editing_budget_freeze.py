"""`agentswe freeze <run_id>`: the budget freeze of a formal Editing run.

When Harbor cuts the Builder at its 5 h budget, a formal Editing one-stop stops at its lifecycle gate and the run
has no Result. The Editing protocol freezes the last accepted submission in that case and runs the six held-out
cases against it; this command does that after the run has ended, through the installed
editing/tools/budget_freeze.py (see its --help), which imports the task tree's own code. Stages:
check (read-only, the default) -> freeze -> hidden -> finalize, or all; every stage is a dry run without --apply.

Run it as the owner of the run directory. The hidden and finalize stages need the provider credential: the command
writes the same 0600 file `run` writes (AGENTSWE_HOME/secrets/editing/credential.env, from the .env roles) and removes
it afterwards under the rule `result` and `stop` follow (kept while another Editing run of this home is live).
The command passes the tool what `agentswe result` reports about the cut (editing_agentloop_v1.budget_cut_state); the
freeze stage requires that report and the tool's own trigger to agree.
Supported tasks: editing_agentloop_v1.BUDGET_FREEZE_TASKS (aider, deeptutor, openwiki); every other task refuses.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import util
from ..config import REPO_ROOT, Config
from . import editing_agentloop_v1 as ed

SUPPORTED = tuple(sorted(ed.BUDGET_FREEZE_TASKS))  # control-plane task keys (editing_agentloop_v1.TASK_KEYS values)
STAGES = ("check", "freeze", "hidden", "finalize", "all")
CREDENTIAL_STAGES = ("hidden", "finalize", "all")
TOOL_FILES = ("budget_freeze.py", "formal_unit_env.py")
LIVE = ("active", "activating", "deactivating", "reloading")
CONTROL_PYTHON = "/usr/bin/python3"  # formal_config.CONTROL_PYTHON: the formal units' interpreter


def refuse_unsupported(launch: dict, key: str | None) -> None:
    if launch.get("family") != "editing":
        raise SystemExit(f"agentswe freeze applies to formal Editing runs only; {launch.get('run_id')} is a "
                         f"{launch.get('family')} run")
    if launch.get("mode") != "formal":
        raise SystemExit(f"{launch.get('run_id')} is a {launch.get('mode')} run: a smoke run has no held-out score to "
                         "freeze for; agentswe freeze applies to formal runs only")
    if key not in SUPPORTED:
        raise SystemExit(f"agentswe freeze does not support {launch.get('task')} yet (supported: "
                         f"{', '.join(SUPPORTED)}); its run stays as the formal path ended it")


def tool_path(cfg: Config, tokens: dict[str, str], scratch: Path) -> Path:
    """The installed tools/budget_freeze.py, or (an install whose tools/ predates it) a fresh render in scratch."""
    installed = cfg.home / "editing" / "tools"
    if all((installed / name).is_file() for name in TOOL_FILES):
        return installed / TOOL_FILES[0]
    for name in TOOL_FILES:
        text = (REPO_ROOT / "runners" / "editing" / "tools" / name).read_text(encoding="utf-8")
        for key, value in tokens.items():
            if key.startswith("@@"):
                text = text.replace(key, value)
        (scratch / name).write_text(text, encoding="utf-8")
    util.log("this install's editing/tools/ has no budget_freeze.py; using a fresh render of the repository's copy")
    return scratch / TOOL_FILES[0]


def result_state(run_dir: Path, key: str, unit_state: str) -> dict:
    """What `agentswe result` reports about the budget cut (its budget_exhausted block), for the tool to compare."""
    state = ed.budget_cut_state(run_dir, task=key, unit_state=unit_state)
    if state is None:
        return {"detected": False}
    return {"detected": True, "task": state["task"], "status": state["status"],
            "accepted_submissions": state["accepted_submissions"], "latest_accepted": state["latest_accepted"]}


def freeze(cfg: Config, launch: dict, *, stage: str = "check", apply: bool = False, operator: str | None = None) -> int:
    key = ed.TASK_KEYS.get(str(launch.get("task")))
    refuse_unsupported(launch, key)
    if stage not in STAGES:
        raise SystemExit(f"unknown stage {stage!r} (one of {', '.join(STAGES)})")
    unit_state = ed._unit_state(launch["unit"])
    if unit_state in LIVE:
        raise SystemExit(f"{launch['run_id']} is still running ({launch['unit']}); a budget freeze applies after the "
                         "formal unit has ended")
    run_dir = Path(str(launch.get("run_dir") or ""))
    if not run_dir.is_absolute() or not run_dir.is_dir():
        raise SystemExit(f"{launch['run_id']}: run directory {run_dir} is missing")
    owner = run_dir.stat().st_uid
    if owner != os.geteuid():
        raise SystemExit(f"run agentswe freeze as the owner of {run_dir} (uid {owner}), not as uid {os.geteuid()}")
    scratch = Path(tempfile.mkdtemp(prefix="agentswe-budget-freeze-"))
    credential = apply and stage in CREDENTIAL_STAGES
    try:
        tool = tool_path(cfg, ed.tokens(cfg), scratch)
        argv = [CONTROL_PYTHON, "-E", "-s", "-B", str(tool), "--run-dir", str(run_dir), "--stage", stage,
                "--result-state", json.dumps(result_state(run_dir, key, unit_state), sort_keys=True)]
        if apply:
            argv.append("--apply")
        if operator:
            argv += ["--operator", operator]
        if credential:
            ed.write_credential(cfg)  # the file `run` writes; never printed
        return subprocess.run(argv, check=False).returncode
    finally:
        if credential:
            ed._release_credential(cfg, launch)
        shutil.rmtree(scratch, ignore_errors=True)
