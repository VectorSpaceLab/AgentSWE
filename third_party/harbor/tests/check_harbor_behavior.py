#!/usr/bin/env python3
"""Behavioural check of an AgentSWE harbor profile (run with the target interpreter).

    <venv>/bin/python -B check_harbor_behavior.py --expect creation
    <venv>/bin/python -B check_harbor_behavior.py --observe-only        # print observations as JSON

It observes the behaviours the patch series is responsible for, without Docker,
network or a model:

  codex_config_toml      CODEX_CONFIG_TOML_PATH handling (patch 0001)
  codex_resume_mode      `resume --last` vs root-thread resume (0004)
  root_selector          the in-container selector on a fake sessions tree (0004)
  stream_disconnect      exception class for Codex 'stream disconnected' (0003)
  single_step_enabled    fault-injected API failure with AGENTSWE_INFRA_RESUME_ENABLED=1 (0005 / runner overlay)
  single_step_disabled   the same with resume disabled
  tmux_start             how the Terminus-2 tmux session receives env (0002)
  claude_agent_env       ANTHROPIC_MODEL taken from the agent env (0006)
  docker_cpus            resources-override `cpus` for a task compose without / with its own
                         cpu_quota+cpu_period (0007)

The same script is run against a patched venv and against the original
paper-run tree (PYTHONPATH overlay copy) to show that both behave the same.
docker_cpus is the exception for `site`: 0007 is a release fix the paper trees do not have.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

EXPECTED = {
    "pristine": {
        "codex_config_toml": "absent", "codex_resume_mode": "--last", "root_selector": "absent",
        "stream_disconnect": "NonZeroAgentExitCodeError",
        "single_step_enabled": "no-resume:recorded:ApiUsageLimitError",
        "single_step_disabled": "no-resume:recorded:ApiUsageLimitError",
        "tmux_start": "tmux -e", "claude_agent_env": "absent",
        "docker_cpus": "plain:cpus;quota:cpus",
    },
    "site": {
        "codex_config_toml": "ok", "codex_resume_mode": "--last", "root_selector": "absent",
        "stream_disconnect": "NonZeroAgentExitCodeError",
        "single_step_enabled": "no-resume:recorded:ApiUsageLimitError",
        "single_step_disabled": "no-resume:recorded:ApiUsageLimitError",
        "tmux_start": "env-prefix", "claude_agent_env": "absent",
        "docker_cpus": "plain:cpus;quota:no-cpus",
    },
    "creation": {
        "codex_config_toml": "ok", "codex_resume_mode": "root-thread", "root_selector": "picks-root;refuses-two-roots",
        "stream_disconnect": "ApiConnectionClosedError",
        "single_step_enabled": "resumed:completed_after_resume:continuation-note",
        "single_step_disabled": "no-resume:recorded:ApiUsageLimitError",
        "tmux_start": "env-prefix", "claude_agent_env": "absent",
        "docker_cpus": "plain:cpus;quota:cpus",
    },
    "creation-0902": {
        "codex_config_toml": "ok", "codex_resume_mode": "--last", "root_selector": "absent",
        "stream_disconnect": "NonZeroAgentExitCodeError",
        "single_step_enabled": "resumed:completed_after_resume:continuation-note",
        "single_step_disabled": "no-resume:recorded:ApiUsageLimitError",
        "tmux_start": "env-prefix", "claude_agent_env": "absent",
        "docker_cpus": "plain:cpus;quota:cpus",
    },
    "creation-glm-0903": {
        "codex_config_toml": "ok", "codex_resume_mode": "--last", "root_selector": "absent",
        "stream_disconnect": "NonZeroAgentExitCodeError",
        "single_step_enabled": "resumed:completed_after_resume:continuation-note",
        "single_step_disabled": "no-resume:recorded:ApiUsageLimitError",
        "tmux_start": "env-prefix", "claude_agent_env": "agent-env",
        "docker_cpus": "plain:cpus;quota:cpus",
    },
    # site venv + runners/optimization/harbor_overlay with OPTIMIZATION_INFRA_RESUME=1
    "site+optimization-overlay": {
        "codex_config_toml": "ok", "codex_resume_mode": "--last", "root_selector": "absent",
        "stream_disconnect": "NonZeroAgentExitCodeError",
        "single_step_enabled": "resumed:completed_after_resume:original-instruction",
        "single_step_disabled": "resumed:completed_after_resume:original-instruction",
        "tmux_start": "env-prefix", "claude_agent_env": "absent",
        "docker_cpus": "plain:cpus;quota:no-cpus",
    },
}


def observe_codex() -> dict:
    from harbor.agents.installed import codex as codex_mod
    from harbor.agents.installed.codex import Codex

    out = {}
    resolver = getattr(Codex, "_resolve_config_toml_path", None)
    if resolver is None:
        out["codex_config_toml"] = "absent"
    else:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "provider.toml"
            cfg.write_text("[features]\nmulti_agent = false\n")
            ok_unset = resolver(SimpleNamespace(_get_env=lambda k: None)) is None
            ok_set = resolver(SimpleNamespace(_get_env=lambda k: str(cfg))) == cfg
            try:
                resolver(SimpleNamespace(_get_env=lambda k: str(cfg) + ".missing"))
                ok_missing = False
            except ValueError:
                ok_missing = True
        out["codex_config_toml"] = "ok" if (ok_unset and ok_set and ok_missing) else "broken"
    source = inspect.getsource(Codex.run)
    if 'resume "$(cat' in source:
        out["codex_resume_mode"] = "root-thread"
    elif "resume --last" in source:
        out["codex_resume_mode"] = "--last"
    else:
        out["codex_resume_mode"] = "unknown"
    selector = getattr(codex_mod, "_root_session_selector_script", None)
    if selector is None:
        out["root_selector"] = "absent"
    else:
        results = []
        for roots in (1, 2):
            with tempfile.TemporaryDirectory() as tmp:
                sessions = Path(tmp) / "sessions" / "2026" / "10" / "01"
                sessions.mkdir(parents=True)
                for i in range(roots):
                    (sessions / f"rollout-root{i}.jsonl").write_text(json.dumps(
                        {"type": "session_meta", "payload": {"id": f"root-{i}"}}) + "\n")
                (sessions / "rollout-child.jsonl").write_text(json.dumps(
                    {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root-0"}}) + "\n")
                marker = Path(tmp) / "secrets" / "root_session_id"
                script = selector(PurePosixPath(tmp) / "sessions", PurePosixPath(marker))
                proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                if roots == 1:
                    ok = proc.returncode == 0 and marker.read_text().strip() == "root-0"
                    results.append("picks-root" if ok else "wrong-pick")
                else:
                    results.append("refuses-two-roots" if proc.returncode != 0 else "accepts-two-roots")
        out["root_selector"] = ";".join(results)
    return out


def observe_stream_disconnect() -> str:
    from harbor.agents.installed.base import BaseInstalledAgent
    from harbor.agents.installed.codex import Codex

    fake = SimpleNamespace(
        _compiled_error_patterns=[(re.compile(p.pattern, re.IGNORECASE), p.exception) for p in Codex.ERROR_PATTERNS],
        logger=logging.getLogger("check"),
        _truncate_output=lambda text, max_len=1000: text or "",
    )
    result = SimpleNamespace(
        return_code=1,
        stdout="ERROR: stream disconnected before completion: error sending request for url (.../responses)",
        stderr="",
    )
    return type(BaseInstalledAgent._classify_exec_error(fake, "codex exec ...", result)).__name__


class _FakeTrial:
    def __init__(self, trial_dir: Path) -> None:
        self.task = SimpleNamespace(instruction="ORIGINAL BUILDER TASK",
                                    config=SimpleNamespace(agent=SimpleNamespace(user="root")))
        self.agent = SimpleNamespace(SUPPORTS_RESUME=True, session_id="agent-session")
        self.agent_environment = SimpleNamespace(session_id="env-session")
        self.config = SimpleNamespace(trial_name="fake-trial")
        self.paths = SimpleNamespace(trial_dir=trial_dir)
        self._agent_timeout_sec = 3600
        self.result = SimpleNamespace()
        self.logger = logging.getLogger("check")
        self.calls: list[dict] = []
        self.recorded: list[BaseException] = []

    async def _run_agent_phase(self, **kwargs) -> None:
        from harbor.agents.installed.base import ApiUsageLimitError

        self.calls.append({"resume": bool(kwargs.get("resume", False)), "instruction": kwargs.get("instruction")})
        if len(self.calls) == 1:
            raise ApiUsageLimitError("fault-injected HTTP 402 usage limit")

    async def _sync_agent_output(self, result) -> None:
        return None

    def _record_exception(self, exc: BaseException) -> None:
        self.recorded.append(exc)


def observe_single_step(enabled: bool) -> str:
    from harbor.trial.single_step import SingleStepTrial

    saved = {k: os.environ.get(k) for k in ("AGENTSWE_INFRA_RESUME_ENABLED", "AGENTSWE_INFRA_RESUME_MIN_WAIT_SEC",
                                             "AGENTSWE_INFRA_RESUME_MAX_WAIT_SEC", "AGENTSWE_INFRA_RESUME_MAX_ATTEMPTS")}
    os.environ.update({"AGENTSWE_INFRA_RESUME_MIN_WAIT_SEC": "0", "AGENTSWE_INFRA_RESUME_MAX_WAIT_SEC": "0",
                       "AGENTSWE_INFRA_RESUME_MAX_ATTEMPTS": "2"})
    if enabled:
        os.environ["AGENTSWE_INFRA_RESUME_ENABLED"] = "1"
    else:
        os.environ.pop("AGENTSWE_INFRA_RESUME_ENABLED", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FakeTrial(Path(tmp))
            try:
                asyncio.run(SingleStepTrial._run_agent(fake))
            except Exception as exc:  # pragma: no cover - reported, not raised
                return f"raised:{type(exc).__name__}"
            state_path = Path(tmp) / "infrastructure_resume_state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if len(fake.calls) >= 2 and fake.calls[1]["resume"]:
                note = ("original-instruction" if fake.calls[1]["instruction"] == "ORIGINAL BUILDER TASK"
                        else "continuation-note")
                status = state.get("state") or state.get("status")
                return f"resumed:{status}:{note}"
            recorded = ",".join(type(e).__name__ for e in fake.recorded) or "nothing"
            return f"no-resume:recorded:{recorded}"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def observe_tmux() -> str:
    from harbor.agents.terminus_2.tmux_session import TmuxSession

    fake = SimpleNamespace(_extra_env={"AGENTSWE_TB_POLICY": "x y"}, _pane_width=160, _pane_height=40,
                           _session_name="s", _logging_path="/tmp/l.log")
    command = TmuxSession._tmux_start_session.fget(fake)
    if " -e " in command:
        return "tmux -e"
    if command.count("env ") and "tmux new-session -x" in command:
        return "env-prefix"
    return "unknown"


def observe_claude() -> str:
    from harbor.agents.installed import claude_code

    source = inspect.getsource(claude_code)
    return "agent-env" if 'self._get_env("ANTHROPIC_MODEL")' in source else "absent"


def observe_docker_cpus() -> str:
    """The resources override Harbor writes for `main` (cpus=8 in task.toml), for a task compose
    without and with its own cpu_quota/cpu_period. No Docker command runs."""
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    results = []
    for label, main in (("plain", {"environment": {"PROBE": "1"}}),
                        ("quota", {"cpu_quota": 800000, "cpu_period": 100000})):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp) / "environment"
            env_dir.mkdir()
            (env_dir / "docker-compose.yaml").write_text(json.dumps({"services": {"main": main}}))
            env = DockerEnvironment(
                environment_dir=env_dir, environment_name="probe", session_id="probe__env",
                trial_paths=TrialPaths(Path(tmp) / "trial"),
                task_env_config=EnvironmentConfig(docker_image="probe:latest", cpus=8, memory_mb=1024))
            override = json.loads(env._write_resources_compose_file().read_text())["services"]["main"]
            env._cleanup_resources_compose_file()
            results.append(f"{label}:{'cpus' if 'cpus' in override else 'no-cpus'}")
    return ";".join(results)


def observe() -> dict:
    import harbor
    import harbor.trial.single_step as single_step_mod
    import harbor.agents.terminus_2.tmux_session as tmux_mod

    out = {"harbor_file": harbor.__file__, "single_step_file": single_step_mod.__file__,
           "tmux_session_file": tmux_mod.__file__}
    out.update(observe_codex())
    out["stream_disconnect"] = observe_stream_disconnect()
    out["single_step_enabled"] = observe_single_step(True)
    out["single_step_disabled"] = observe_single_step(False)
    out["tmux_start"] = observe_tmux()
    out["claude_agent_env"] = observe_claude()
    out["docker_cpus"] = observe_docker_cpus()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=sorted(EXPECTED))
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    observed = observe()
    print(json.dumps(observed, indent=2, sort_keys=True))
    if args.observe_only or not args.expect:
        return 0
    failures = [f"{k}: expected {v!r}, observed {observed.get(k)!r}"
                for k, v in EXPECTED[args.expect].items() if observed.get(k) != v]
    for line in failures:
        print("FAIL " + line, file=sys.stderr)
    print(("OK" if not failures else "FAILED") + f" expect={args.expect}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
