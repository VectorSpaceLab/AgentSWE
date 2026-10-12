"""Harbor Codex agent for any Responses-compatible provider (configured by AgentSWE).

The provider key is inherited from the Harbor process and exposed only to the
remote ``codex exec`` process.  It is never serialized into a Harbor config or
written into the trial artifacts.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths


# Release fix (session time limit, RC; differs from the paper path): Harbor's AgentTimeoutError cancels the wait for
# ``codex exec`` but leaves the agent's processes running inside the Builder container, so they could keep editing
# /workspace/submission while the Builder verifier and the controller's freeze read it ("Builder modified
# /workspace/submission after the controller froze it"). The session ends at its limit: every process in the
# container except the init keepalive and this exec's own chain is stopped, then killed, until none is left; the counts are
# written to @@OUT@@. The signal sweep uses shell builtins so it never targets a helper it started.
SESSION_STOP_SCRIPT = r"""
# Preserve the control shell ancestry and Harbor's exact init keepalive only.
# Orphaned Builder processes are reparented to PID 1; preserving its entire
# descendant tree would leave those processes free to mutate the submission.
keep=" 1 "
p=$$
while [ -n "$p" ] && [ "$p" != 0 ]; do
  keep="$keep$p "
  { read -r line < "/proc/$p/stat"; } 2>/dev/null || break
  rest=${line##*) }
  set -f; set -- $rest; set +f
  p=$2
done
init_command=$(tr '\000' ' ' < /proc/1/cmdline)
case "$init_command" in
  "sh -c sleep infinity "|"/bin/sh -c sleep infinity ")
    for d in /proc/[0-9]*; do
      { read -r line < "$d/stat"; } 2>/dev/null || continue
      rest=${line##*) }
      set -f; set -- $rest; set +f
      [ "$2" = 1 ] || continue
      command=$(tr '\000' ' ' < "$d/cmdline" 2>/dev/null)
      [ "$command" = "sleep infinity " ] || continue
      keep="$keep${d#/proc/} "
    done
    ;;
esac
live=0
sweep() {
  sig=$1
  live=0
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    case "$keep" in *" $pid "*) continue ;; esac
    { read -r line < "$d/stat"; } 2>/dev/null || continue
    rest=${line##*) }
    set -f; set -- $rest; set +f
    [ "$1" = Z ] && continue
    live=$((live + 1))
    [ "$sig" = none ] || kill -s "$sig" "$pid" 2>/dev/null
  done
}
sweep none
before=$live
rounds=0
while [ "$rounds" -lt 20 ]; do
  sweep STOP
  [ "$live" -eq 0 ] && break
  sweep KILL
  rounds=$((rounds + 1))
  sleep 0.5
done
sweep none
mkdir -p "$(dirname @@OUT@@)"
printf '{"schema_version": "agentswe-builder-session-stop/v1", "reason": "session_time_limit", "live_before": %d, "live_after": %d, "kill_rounds": %d}\n' "$before" "$live" "$rounds" > @@OUT@@
cat @@OUT@@
"""


class ProviderCodex(Codex):
    """Run the stock Codex CLI/session pipeline against a custom provider."""

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        escaped_instruction = shlex.quote(instruction)

        if not self.model_name:
            raise ValueError("Model name is required")

        model = self.model_name.split("/")[-1]
        provider_config = Path(
            self._get_env("CODEX_CONFIG_TOML_PATH")
            or Path(__file__).resolve().with_name("provider.toml")
        )
        if not provider_config.is_file():
            raise ValueError(f"Codex provider config is missing: {provider_config}")
        model_catalog = Path(
            self._get_env("CODEX_MODEL_CATALOG_PATH")
            or Path(__file__).resolve().with_name("model_catalog.json")
        )
        if not model_catalog.is_file():
            raise ValueError(f"Codex model catalog is missing: {model_catalog}")

        api_key_env_name = (
            self._get_env("CODEX_PROVIDER_API_KEY_ENV") or "AGENTSWE_BUILDER_API_KEY"
        )
        api_key = self._get_env(api_key_env_name)
        if not api_key:
            raise ValueError(
                f"Codex custom-provider credential is missing: {api_key_env_name}"
            )

        cli_flags = self.build_cli_flags()
        cli_flags_arg = (cli_flags + " ") if cli_flags else ""

        remote_codex_home = self._REMOTE_CODEX_HOME.as_posix()
        remote_secrets_dir = self._REMOTE_CODEX_SECRETS_DIR.as_posix()
        remote_config_path = (self._REMOTE_CODEX_HOME / "config.toml").as_posix()
        remote_model_catalog_path = (
            self._REMOTE_CODEX_HOME / "models.json"
        ).as_posix()
        agent_sessions_dir = (EnvironmentPaths.agent_dir / "sessions").as_posix()

        env: dict[str, str] = {
            "CODEX_HOME": remote_codex_home,
            "CODEX_PROVIDER_API_KEY_ENV": api_key_env_name,
            api_key_env_name: api_key,
        }

        await self.exec_as_agent(
            environment,
            command=(
                f'mkdir -p "$CODEX_HOME" {shlex.quote(remote_secrets_dir)} '
                f"{shlex.quote(EnvironmentPaths.agent_dir.as_posix())}"
            ),
            env=env,
        )
        await environment.upload_file(provider_config, remote_config_path)
        await environment.upload_file(model_catalog, remote_model_catalog_path)
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {environment.default_user} "
                    f"{shlex.quote(remote_config_path)} "
                    f"{shlex.quote(remote_model_catalog_path)}"
                ),
            )

        setup_command = f"""
cat >{shlex.quote(remote_secrets_dir)}/provider-auth.sh <<'SH'
#!/usr/bin/env sh
set -eu
key_name="${{CODEX_PROVIDER_API_KEY_ENV:-AGENTSWE_BUILDER_API_KEY}}"
printenv "$key_name"
SH
chmod 700 {shlex.quote(remote_secrets_dir)}/provider-auth.sh
"""

        skills_command = self._build_register_skills_command()
        if skills_command:
            setup_command += f"\n{skills_command}"

        mcp_command = self._build_register_mcp_servers_command()
        if mcp_command:
            setup_command += f"\n{mcp_command}"

        if self._resume:
            setup_command += (
                f"\nif [ ! -d {shlex.quote(agent_sessions_dir)} ]; then\n"
                '  echo "Cannot resume Codex: no previous session logs found" >&2\n'
                "  exit 1\n"
                "fi\n"
                'rm -rf "$CODEX_HOME/sessions"\n'
                f"cp -R {shlex.quote(agent_sessions_dir)} "
                '"$CODEX_HOME/sessions"'
            )

        await self.exec_as_agent(environment, command=setup_command, env=env)
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    f"codex exec {'resume --last ' if self._resume else ''}"
                    "--dangerously-bypass-approvals-and-sandbox "
                    "--skip-git-repo-check "
                    f"--model {shlex.quote(model)} "
                    "--json "
                    f"{cli_flags_arg}"
                    "-- "
                    f"{escaped_instruction} "
                    f"2>&1 </dev/null | tee {EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME}"
                ),
                env=env,
            )
        except asyncio.CancelledError:
            # Harbor's session time limit (AgentTimeoutError) cancels this wait; Harbor waits for this handler
            # before it runs the Builder verifier, so the session is over before anything reads the submission.
            await self._stop_session_processes(environment)
            raise
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"mkdir -p {EnvironmentPaths.agent_dir.as_posix()}\n"
                        'if [ -d "$CODEX_HOME/sessions" ]; then\n'
                        f"  rm -rf {(EnvironmentPaths.agent_dir / 'sessions').as_posix()}\n"
                        f'  cp -R "$CODEX_HOME/sessions" '
                        f"{(EnvironmentPaths.agent_dir / 'sessions').as_posix()}\n"
                        "fi"
                    ),
                    env=env,
                )
            except Exception:
                pass
            try:
                await self.exec_as_agent(
                    environment,
                    command=f'rm -rf {shlex.quote(remote_secrets_dir)} "$CODEX_HOME"',
                    env=env,
                )
            except Exception:
                pass

    async def _stop_session_processes(self, environment: BaseEnvironment) -> None:
        """Stop every Builder process left in the container when the session limit cancels the run."""
        evidence = shlex.quote((EnvironmentPaths.agent_dir / "session_limit_stop.json").as_posix())
        try:
            await self.exec_as_root(
                environment, command=SESSION_STOP_SCRIPT.replace("@@OUT@@", evidence), timeout_sec=120
            )
        except Exception:
            # The controller's freeze check stays the backstop: a submission that still changed after the
            # limit is rejected as before.
            self.logger.warning("session-limit process stop failed", exc_info=True)
