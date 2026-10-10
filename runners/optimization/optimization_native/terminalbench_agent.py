"""Harbor agent that executes a frozen evaluator-approved command plan."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class FrozenCommandPlanAgent(BaseAgent):
    """Execute candidate commands only inside the evaluator-owned task container."""

    @staticmethod
    def name() -> str:
        return "agentswe-frozen-command-plan"

    def version(self) -> str:
        return "1.2.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        prediction_path = (
            self.extra_env.get("AGENTSWE_TB_PREDICTION_PATH")
            or os.environ.get("AGENTSWE_TB_PREDICTION_PATH", "")
        )
        if not prediction_path:
            raise RuntimeError("AGENTSWE_TB_PREDICTION_PATH is required")
        prediction = json.loads(Path(prediction_path).read_text(encoding="utf-8"))
        commands = prediction.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ValueError("frozen prediction must contain a non-empty commands list")
        if len(commands) > 40 or not all(isinstance(item, str) for item in commands):
            raise ValueError("command plan must contain at most 40 strings")
        traces: list[dict[str, Any]] = []
        started = time.monotonic()
        total_budget_seconds = 900.0
        per_command_cap_seconds = 300
        candidate_timed_out = False
        for index, command in enumerate(commands):
            if not command.strip() or len(command.encode("utf-8")) > 131072:
                raise ValueError(f"invalid command at index {index}")
            remaining = total_budget_seconds - (time.monotonic() - started)
            if remaining <= 0:
                candidate_timed_out = True
                traces.append({
                    "index": index,
                    "command_digest": hashlib.sha256(command.encode()).hexdigest(),
                    "timed_out": True,
                    "timeout_kind": "total_command_budget",
                })
                break
            command_timeout = max(1, min(per_command_cap_seconds, int(remaining)))
            command_started = time.monotonic()
            try:
                result = await environment.exec(
                    "timeout --signal=TERM --kill-after=5s "
                    f"{command_timeout}s bash -lc {shlex.quote(command)}",
                    timeout_sec=command_timeout + 15,
                    user="root",
                )
            except RuntimeError as exc:
                if "Command timed out after" not in str(exc):
                    raise
                candidate_timed_out = True
                traces.append({
                    "index": index,
                    "command_digest": hashlib.sha256(command.encode()).hexdigest(),
                    "timed_out": True,
                    "timeout_kind": (
                        "total_command_budget"
                        if command_timeout < per_command_cap_seconds
                        else "per_command_cap"
                    ),
                    "timeout_seconds": command_timeout,
                    "elapsed_seconds": round(time.monotonic() - command_started, 3),
                })
                break
            if result.return_code in {124, 137}:
                candidate_timed_out = True
                traces.append({
                    "index": index,
                    "command_digest": hashlib.sha256(command.encode()).hexdigest(),
                    "timed_out": True,
                    "timeout_kind": (
                        "total_command_budget"
                        if command_timeout < per_command_cap_seconds
                        else "per_command_cap"
                    ),
                    "timeout_seconds": command_timeout,
                    "elapsed_seconds": round(time.monotonic() - command_started, 3),
                    "timeout_enforcement": "container_coreutils",
                })
                break
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            traces.append(
                {
                    "index": index,
                    "command_digest": hashlib.sha256(command.encode()).hexdigest(),
                    "return_code": result.return_code,
                    "stdout_digest": hashlib.sha256(stdout.encode()).hexdigest(),
                    "stderr_digest": hashlib.sha256(stderr.encode()).hexdigest(),
                    "elapsed_seconds": round(time.monotonic() - command_started, 3),
                }
            )
        context.metadata = {
            "mode": "frozen_command_plan",
            "command_count": len(commands),
            "commands_executed": len(traces),
            "candidate_timed_out": candidate_timed_out,
            "total_budget_seconds": total_budget_seconds,
            "per_command_cap_seconds": per_command_cap_seconds,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "commands": traces,
        }


class LiveTerminus2Agent(BaseAgent):
    """Thin evaluator-owned live Terminus-2 delegate.

    The submitted harness controls only the reusable policy text.  Harbor's
    official Terminus2 owns the tmux session, observation parser, model calls,
    summarization and terminal recovery loop.
    """

    @staticmethod
    def name() -> str:
        return "agentswe-live-terminus2"

    def version(self) -> str:
        return "2.0.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._delegate = None
        self._policy = str((self.extra_env or {}).get("AGENTSWE_TB_POLICY", ""))

    async def setup(self, environment: BaseEnvironment) -> None:
        from harbor.agents.terminus_2 import Terminus2

        # Harbor's LiteLLM client is constructed in this host agent process and
        # reads credentials from os.environ.  `extra_env` is also forwarded to
        # the task terminal, but that alone does not configure the host client.
        # The controller supplies only a runtime placeholder key; the broker
        # remains the sole holder of the real provider credential.
        runtime_env = self.extra_env or {}
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            value = runtime_env.get(name)
            if isinstance(value, str) and value:
                os.environ[name] = value

        kwargs = {
            "logs_dir": self.logs_dir,
            "model_name": "openai/gpt-5.6-sol",
            "api_base": str((self.extra_env or {}).get("AGENTSWE_TB_API_BASE", "")) or None,
            "max_turns": int((self.extra_env or {}).get("AGENTSWE_TB_MAX_TURNS", "20")),
            "parser_name": "json",
            "enable_summarize": True,
            "record_terminal_session": True,
            "extra_env": self.extra_env,
        }
        self._delegate = Terminus2(**kwargs)
        await self._delegate.setup(environment)

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if self._delegate is None:
            raise RuntimeError("live Terminus2 delegate was not setup")
        prompt = instruction
        if self._policy:
            prompt += "\n\nReusable agent policy:\n" + self._policy
        await self._delegate.run(prompt, environment, context)
