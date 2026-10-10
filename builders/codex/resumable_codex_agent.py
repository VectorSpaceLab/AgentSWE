"""Codex Builder wrapper that enforces dev-loop liveness in one Trial.

The stock Codex process is allowed to return after emitting a final answer even
when an asynchronous dev evaluation is still running.  In the formal
optimization protocol that is an incomplete Builder session, not success.  The
wrapper waits for the controller's active evaluation and then invokes Codex's
native ``resume --last`` in the same Harbor Trial/container/session until the
controller freezes the required number of rounds.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, override

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths


class ResumableCodex(Codex):
    """Keep one native Codex session alive across normal early returns."""

    SUPPORTS_RESUME = True

    @staticmethod
    def _poll_seconds() -> float:
        try:
            return max(1.0, float(os.environ.get("AGENTSWE_BUILDER_CONTINUATION_POLL_SEC", "60")))
        except ValueError:
            return 60.0

    @staticmethod
    def _max_continuations() -> int:
        try:
            return max(0, int(os.environ.get("AGENTSWE_BUILDER_CONTINUATION_MAX_ATTEMPTS", "0")))
        except ValueError:
            return 0

    async def _progress(self, environment: BaseEnvironment) -> dict[str, Any]:
        result = await environment.exec(command="submit_dev_candidate --progress")
        if result.return_code != 0:
            raise RuntimeError(
                "Builder continuation progress query failed: "
                f"{result.stderr or result.stdout}"
            )
        lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("Builder continuation progress query returned no JSON")
        raw_stdout = result.stdout or ""
        candidates: list[object] = []
        try:
            candidates.append(json.loads(raw_stdout))
        except json.JSONDecodeError:
            pass
        for line in reversed(lines):
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        decoder = json.JSONDecoder()
        for offset, character in enumerate(raw_stdout):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(raw_stdout[offset:])
            except json.JSONDecodeError:
                continue
            candidates.append(value)
        value = next(
            (
                candidate
                for candidate in candidates
                if isinstance(candidate, dict) and "http_status" in candidate
            ),
            None,
        )
        if value is None:
            raise RuntimeError(
                "Builder continuation progress query returned invalid JSON: "
                f"{raw_stdout!r}"
            )
        if not isinstance(value, dict):
            raise RuntimeError("Builder continuation progress is not an object")
        if value.get("http_status") != 200:
            raise RuntimeError(f"Builder continuation progress HTTP error: {value}")
        return value

    async def _write_state(self, environment: BaseEnvironment, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        # The state contains only controller counters and timestamps.  Encode
        # it through base64 so the shell command cannot be altered by JSON
        # punctuation or a future diagnostic string.
        import base64

        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        state_path = EnvironmentPaths.agent_dir / "builder_continuation_state.json"
        await environment.exec(
            command=(
                f"mkdir -p {EnvironmentPaths.agent_dir.as_posix()} && "
                f"printf '%s' '{encoded}' | base64 -d > {state_path.as_posix()}"
            )
        )

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        max_rounds = int(os.environ.get("AGENTSWE_DEV_MAX_ROUNDS", "5"))
        count = 0
        state: dict[str, Any] = {
            "schema_version": "1.0",
            "status": "watching",
            "continuation_count": 0,
            "max_rounds": max_rounds,
            "events": [],
        }
        await self._write_state(environment, state)

        # Every invocation is a normal Codex turn.  On the first turn this
        # starts the session; on later turns ``self._resume`` makes the stock
        # implementation seed the same session logs and issue native
        # ``codex exec resume --last`` inside the same Harbor Trial/container.
        await super().run(instruction, environment, context)

        while True:
            progress = await self._progress(environment)
            accepted = int(progress.get("accepted_rounds", 0) or 0)
            if progress.get("frozen") is True or accepted >= max_rounds:
                state.update({"status": "completed", "accepted_rounds": accepted})
                await self._write_state(environment, state)
                return
            if progress.get("environment_repair_required") is True:
                state.update({
                    "status": "blocked_environment_repair",
                    "accepted_rounds": accepted,
                    "error": progress.get("environment_repair_error"),
                })
                await self._write_state(environment, state)
                # Return cleanly so the host controller can classify this as a
                # blocked infrastructure state instead of a generic Builder
                # process failure.  No score is produced from this state.
                return

            if progress.get("active_submission_id") is not None:
                state.update({"status": "waiting_for_evaluator", "accepted_rounds": accepted})
                await self._write_state(environment, state)
                await asyncio.sleep(self._poll_seconds())
                continue

            count += 1
            limit = self._max_continuations()
            if limit > 0 and count > limit:
                state.update({"status": "continuation_exhausted", "accepted_rounds": accepted})
                await self._write_state(environment, state)
                raise RuntimeError(
                    f"Builder returned before required dev rounds; continuation attempts exhausted at {count - 1}"
                )

            event = {
                "continuation": count,
                "accepted_rounds_before": accepted,
                "resumed_at": datetime.now(timezone.utc).isoformat(),
            }
            state["events"].append(event)
            state.update({"status": "resuming", "continuation_count": count})
            await self._write_state(environment, state)
            self._resume = True
            try:
                await super().run(instruction, environment, context)
            finally:
                self._resume = False
