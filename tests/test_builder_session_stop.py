#!/usr/bin/env python3
"""ProviderCodex ends the Builder session at Harbor's time limit (needs Harbor importable; no docker, no provider).

    <harbor venv>/bin/python -B tests/test_builder_session_stop.py

Runs ProviderCodex.run against a fake environment whose ``codex exec`` never returns, under asyncio.wait_for as
Harbor's trial does (AgentTimeoutError). Checks that, before wait_for gives up, the stop script ran as root after the
codex exec and before the session-log copy and secret removal; and that the agent without this fix (the paper
behaviour, given with --paper, e.g. from git show of an earlier main) runs no such stop.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = ROOT / "builders" / "codex"


class FakeEnvironment:
    default_user = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
        kind = ("codex" if "codex exec" in command else
                "stop" if "agentswe-builder-session-stop/v1" in command else
                "sessions" if "sessions" in command and "cp -R" in command else
                "secrets" if "rm -rf" in command else "setup")
        self.calls.append((kind, str(user)))
        if kind == "codex":
            await asyncio.sleep(3600)
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    async def upload_file(self, source, target):
        self.calls.append(("upload", "-"))


def load_agent(path: Path, name: str):
    sys.path.insert(0, str(CODEX_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProviderCodex


async def drive(agent_cls, tmp: Path) -> list[tuple[str, str]]:
    from harbor.models.agent.context import AgentContext

    config = tmp / "provider.toml"
    config.write_text('model = "deepseek-flash"\n')
    catalog = tmp / "models.json"
    catalog.write_text("{}\n")
    agent = agent_cls(logs_dir=tmp / "logs", model_name="deepseek-flash", version="0.144.1",
                      extra_env={"CODEX_CONFIG_TOML_PATH": str(config), "CODEX_MODEL_CATALOG_PATH": str(catalog),
                                 "AGENTSWE_BUILDER_API_KEY": "test-placeholder"})
    environment = FakeEnvironment()
    try:
        await asyncio.wait_for(agent.run(instruction="build", environment=environment, context=AgentContext()),
                               timeout=0.5)
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError("the fake codex exec returned")
    return environment.calls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", type=Path, help="paper codex_provider_agent.py, for the contrast check")
    args = parser.parse_args()
    try:
        import harbor  # noqa: F401
    except ImportError:
        print("SKIP: harbor is not importable; run with the Harbor venv python")
        return 0
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        calls = asyncio.run(drive(load_agent(CODEX_DIR / "codex_provider_agent.py", "release_agent"), Path(tmp)))
        kinds = [k for k, _ in calls]
        checks["release: stop ran"] = "stop" in kinds
        checks["release: stop ran as root"] = ("stop", "root") in calls
        checks["release: stop after the codex exec"] = "stop" in kinds and kinds.index("stop") > kinds.index("codex")
        checks["release: stop before the session-log copy and secret removal"] = (
            "stop" in kinds and all(kinds.index("stop") < kinds.index(k) for k in ("sessions", "secrets") if k in kinds))
        if args.paper:
            paper_tmp = Path(tmp) / "paper"
            paper_tmp.mkdir()
            paper_calls = asyncio.run(drive(load_agent(args.paper, "paper_agent"), paper_tmp))
            checks["paper: no stop at the limit"] = "stop" not in [k for k, _ in paper_calls]
        print(json.dumps({"release_calls": calls, "checks": checks}, indent=2))
    failed = [k for k, v in checks.items() if not v]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
