"""Evaluator-owned Codex adapter: a persistent CODEX_HOME and an explicit resume.

Harbor's installed codex agent
(``harbor/agents/installed/codex.py``, codex CLI 0.144.1) is used unchanged for
everything it does.  This subclass changes exactly three things, all of them
evaluator-side, and none of them touching the provider request, the retry
ladder, the credentials or the flag set the Builder is measured under:

  1. ``_REMOTE_CODEX_HOME`` (codex.py:37) points at a directory the evaluator
     bind-mounts from the host, so the session rollout survives the container.
     Without this, a container that dies mid-turn takes its rollout with it and
     there is nothing to resume.
  2. the adapter's own cleanup (codex.py:1173,
     ``rm -rf <secrets> "$CODEX_HOME"``) no longer removes that directory.  The
     secrets directory is still removed and the ``auth.json`` symlink inside
     ``$CODEX_HOME`` is still dropped, so nothing credential-shaped is left on
     the host.  The sessions copy-out at codex.py:1156-1161 is untouched and
     keeps writing ``<trial>/agent/sessions``.
  3. when the evaluator declares a resume -- and only then -- ``codex exec``
     becomes ``codex exec resume <session id>`` with the identical flag set,
     and the instruction becomes the ORIGINAL Builder instruction, verbatim and
     first, followed by the evaluator's continuation note.

The resume is declared by the evaluator through the agent's ``env`` in the
Harbor job config; the Builder cannot set it.  With no such declaration this
class behaves exactly like the stock adapter apart from where ``$CODEX_HOME``
lives.

No provider request, retry, resume decision or credential loading happens here:
this file only rewrites two shell commands and one instruction string.
"""
from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from harbor.agents.installed.codex import Codex

# The container-side path the evaluator bind-mounts <run>/codex_home onto.
# Deliberately not Harbor's /tmp/codex-home, so a stock adapter can never write
# into the evaluator's mount by accident.
REMOTE_CODEX_HOME = "/tmp/agentswe-codex-home"

RESUME_SESSION_ENV = "AGENTSWE_CODEX_RESUME_SESSION_ID"
RESUME_NOTE_ENV = "AGENTSWE_CODEX_RESUME_NOTE"

# Evaluator-authored, and appended after the Builder's own, unmodified task
# instruction.  No case ids, no scores, no oracle, no score-cap contract, no
# hidden information.
DEFAULT_RESUME_NOTE = (
    "The task above is your original assignment, repeated here verbatim. Your "
    "previous session was interrupted by an evaluator-side infrastructure "
    "failure, not by anything you did. The workspace, the controller socket "
    "and your remaining budget are unchanged, and this is the same session: "
    "whatever work it already records still stands, so continue from there "
    "instead of redoing it. If it records no work at all, the interruption hit "
    "before you had started, and you should simply begin the task above."
)

# The separator between the Builder's instruction and the continuation note.
RESUME_JOIN = "\n\n"

_EXEC_PREFIX = "codex exec "
_RESUME_PREFIX = "codex exec resume "
_CLEANUP_HOME = ' "$CODEX_HOME"'
_DROP_AUTH = '\nrm -f "$CODEX_HOME/auth.json"'


class CodexResume(Codex):
    """Stock Codex, with an evaluator-owned CODEX_HOME and an explicit resume."""

    _REMOTE_CODEX_HOME = PurePosixPath(REMOTE_CODEX_HOME)

    @property
    def resume_session_id(self) -> str | None:
        value = (self._get_env(RESUME_SESSION_ENV) or "").strip()
        return value or None

    @property
    def resume_note(self) -> str:
        return (self._get_env(RESUME_NOTE_ENV) or "").strip() or DEFAULT_RESUME_NOTE

    def resume_instruction(self, instruction: str) -> str:
        """The Builder's own instruction, verbatim and first, then the note.

        The note alone is not enough.  A segment can be cut *before the model
        emits a single token* -- a 503 storm that eats the whole reconnect
        ladder leaves a rollout holding nothing but the untouched task prompt
        (for example: 9 rollout rows, 0 function
        calls, 0 assistant messages).  Resuming such a session with only
        "continue where you left off" told the model to continue nothing: it
        called ``get_goal``, got ``{"goal": null}``, answered "No active goal
        or pending task was found.", completed the turn, and the run ended with
        no submission.  Re-sending the instruction unconditionally costs a
        cached prompt and removes that failure mode entirely; it cannot cost
        the Builder its work, because the note tells it the recorded work
        stands.
        """
        return f"{instruction}{RESUME_JOIN}{self.resume_note}"

    def rewrite_command(self, command):
        """Rewrite exactly the two adapter commands this class owns."""
        if not isinstance(command, str):
            return command
        stripped = command.lstrip()
        if stripped.startswith("rm -rf ") and command.endswith(_CLEANUP_HOME):
            # codex.py:1173.  Keep the secrets removal, keep the rollout, and
            # still drop the credential symlink the setup step created.
            return command[: -len(_CLEANUP_HOME)] + _DROP_AUTH
        session = self.resume_session_id
        if session and _EXEC_PREFIX in command and _RESUME_PREFIX not in command:
            # codex.py:1131-1149.  Same flags, same tee, same instruction slot.
            return command.replace(
                _EXEC_PREFIX, _RESUME_PREFIX + shlex.quote(session) + " ", 1
            )
        return command

    async def exec_as_agent(
        self, environment, command, env=None, cwd=None, timeout_sec=None
    ):
        return await super().exec_as_agent(
            environment,
            self.rewrite_command(command),
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

    async def run(self, instruction: str, environment, context) -> None:
        if self.resume_session_id:
            # A resumed segment continues a conversation that may hold no more
            # than the untouched task prompt, so the task travels with it.
            instruction = self.resume_instruction(instruction)
        return await super().run(instruction, environment, context)
