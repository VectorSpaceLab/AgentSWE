"""Small, dependency-free helpers for infrastructure-only agent recovery."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from harbor.agents.installed.base import (
    ApiConnectionClosedError,
    ApiInternalServerError,
    ApiOverloadedError,
    ApiRateLimitError,
    ApiResponseStalledError,
    ApiUsageLimitError,
    NetworkConnectionError,
    NonZeroAgentExitCodeError,
)


RECOVERABLE_EXCEPTIONS = (
    ApiRateLimitError,
    ApiUsageLimitError,
    ApiInternalServerError,
    ApiOverloadedError,
    ApiConnectionClosedError,
    ApiResponseStalledError,
    NetworkConnectionError,
)

_RECOVERABLE_TEXT = re.compile(
    # 522 is Cloudflare's connection-timeout status.  It is a provider/
    # transport failure, not a model or task failure, and must be eligible for
    # native Codex continuation in the same Harbor trial.
    r"(?:\b(?:402|429|500|502|503|504|522)\b|"
    r"payment required|insufficient wallet balance|wallet balance|"
    r"rate.?limit|too many requests|usage limit|quota exceeded|"
    r"overloaded|connection closed|stream closed|response stalled|"
    r"could not resolve host|connection refused|connection timed out|"
    r"request timed out|ssl_error_syscall|ssl_connect|network error)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_recoverable_exception(exc: BaseException) -> bool:
    """Return true only for transient provider/network failures.

    Generic agent timeouts, authentication errors, model-not-found errors,
    safety refusals, context-window errors, and output-token errors are not
    retried.  The text fallback is intentionally restricted to Harbor's
    non-zero agent errors so a task-level exception cannot become an infinite
    recovery loop merely because it mentions a network-related word.
    """

    if isinstance(exc, RECOVERABLE_EXCEPTIONS):
        return True
    return isinstance(exc, NonZeroAgentExitCodeError) and bool(
        _RECOVERABLE_TEXT.search(str(exc))
    )


@dataclass(frozen=True)
class ResumePolicy:
    """Backoff and retry limits for one live Harbor Trial."""

    max_attempts: int = 0
    min_wait_sec: float = 60.0
    max_wait_sec: float = 300.0

    @classmethod
    def from_environment(cls) -> "ResumePolicy":
        def number(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = float(raw)
            except ValueError:
                return default
            return max(0.0, value)

        max_attempts = int(number("AGENTSWE_INFRA_RESUME_MAX_ATTEMPTS", 0.0))
        return cls(
            max_attempts=max_attempts,
            min_wait_sec=number("AGENTSWE_INFRA_RESUME_MIN_WAIT_SEC", 60.0),
            max_wait_sec=number("AGENTSWE_INFRA_RESUME_MAX_WAIT_SEC", 300.0),
        )

    def delay_for(self, resume_attempt: int) -> float:
        # resume_attempt is one-based for the first wait.
        return min(
            self.max_wait_sec,
            self.min_wait_sec * (2 ** max(0, resume_attempt - 1)),
        )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def run_with_infrastructure_resume(
    initial_call: Callable[[], Awaitable[None]],
    resume_call: Callable[[], Awaitable[None]],
    *,
    state_path: Path,
    policy: ResumePolicy | None = None,
    can_resume: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run once and resume in-place after transient infrastructure errors.

    The function deliberately does not catch arbitrary exceptions forever:
    only ``is_recoverable_exception`` failures enter the loop.  ``state_path``
    is updated before sleeping and after every resume, so an interrupted
    process still leaves an auditable last state.
    """

    resolved_policy = policy or ResumePolicy.from_environment()
    events: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "running",
        "resume_count": 0,
        "events": events,
        **(identity or {}),
    }
    _write_json_atomic(state_path, state)

    try:
        await initial_call()
    except Exception as exc:
        if not can_resume or not is_recoverable_exception(exc):
            state.update(
                {
                    "status": "failed_nonrecoverable",
                    "finished_at": utc_now(),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            _write_json_atomic(state_path, state)
            raise

        while True:
            next_attempt = int(state["resume_count"]) + 1
            if (
                resolved_policy.max_attempts > 0
                and next_attempt > resolved_policy.max_attempts
            ):
                state.update(
                    {
                        "status": "resume_exhausted",
                        "finished_at": utc_now(),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                _write_json_atomic(state_path, state)
                raise

            delay = resolved_policy.delay_for(next_attempt)
            event = {
                "resume_attempt": next_attempt,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "paused_at": utc_now(),
                "wait_sec": delay,
                "status": "paused_infrastructure",
            }
            events.append(event)
            state.update(
                {
                    "status": "paused_infrastructure",
                    "resume_count": next_attempt - 1,
                    "last_error_type": type(exc).__name__,
                    "last_error_message": str(exc),
                    "paused_at": event["paused_at"],
                    "next_wait_sec": delay,
                }
            )
            _write_json_atomic(state_path, state)
            await sleep(delay)

            state.update(
                {
                    "status": "resuming",
                    "resume_count": next_attempt,
                    "resumed_at": utc_now(),
                }
            )
            _write_json_atomic(state_path, state)
            try:
                await resume_call()
            except Exception as resume_exc:
                if not is_recoverable_exception(resume_exc):
                    state.update(
                        {
                            "status": "failed_nonrecoverable",
                            "finished_at": utc_now(),
                            "error_type": type(resume_exc).__name__,
                            "error_message": str(resume_exc),
                        }
                    )
                    _write_json_atomic(state_path, state)
                    raise
                exc = resume_exc
                continue

            state.update(
                {
                    "status": "completed_after_resume",
                    "finished_at": utc_now(),
                    "resume_count": next_attempt,
                }
            )
            _write_json_atomic(state_path, state)
            return state

    except Exception:
        raise
    else:
        state.update({"status": "completed_without_resume", "finished_at": utc_now()})
        _write_json_atomic(state_path, state)
        return state
