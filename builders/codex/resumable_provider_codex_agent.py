"""AgentSWE-Lite builder: ResumableCodex liveness wrapper over the custom-provider Codex adapter.

MRO: ResumableDeepSeekCodex -> ResumableCodex -> CatalogDeepSeekCodex -> DeepSeekCodex -> Codex,
so every turn (first and resumed) runs CatalogDeepSeekCodex.run and then DeepSeekCodex.run with
the provider config named by CODEX_CONFIG_TOML_PATH (deepseek-flash, Sol or Qwen in Lite).

Codex 0.144.1 has no built-in metadata for non-GPT slugs, so without a catalog it falls back to
generic metadata: no apply_patch tool and no reasoning field. Lite gives every builder the same
tool surface by installing the shared model catalog (``model_catalog_json`` in the provider
TOML). DeepSeekCodex.run removes the whole CODEX_HOME after every turn, so the catalog is
uploaded again before each turn; a resumed turn otherwise fails with "No such file or directory".
"""
from __future__ import annotations

import shlex
from pathlib import Path

from codex_provider_agent import ProviderCodex as DeepSeekCodex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from resumable_codex_agent import ResumableCodex

CATALOG_FILENAME = "codex_model_catalog_lite.json"
REMOTE_CATALOG_FILENAME = "model_catalog_lite.json"


class CatalogDeepSeekCodex(DeepSeekCodex):
    """Install the Lite model catalog into CODEX_HOME before every Codex turn."""

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        catalog = Path(
            self._get_env("CODEX_MODEL_CATALOG_PATH")
            or Path(__file__).resolve().with_name(CATALOG_FILENAME)
        )
        if not catalog.is_file():
            raise ValueError(f"Codex model catalog is missing: {catalog}")
        remote_home = self._REMOTE_CODEX_HOME.as_posix()
        remote_catalog = (self._REMOTE_CODEX_HOME / REMOTE_CATALOG_FILENAME).as_posix()
        await self.exec_as_agent(
            environment, command=f"mkdir -p {shlex.quote(remote_home)}"
        )
        await environment.upload_file(catalog, remote_catalog)
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=f"chown {environment.default_user} {shlex.quote(remote_catalog)}",
            )
        await super().run(instruction, environment, context)


class ResumableDeepSeekCodex(ResumableCodex, CatalogDeepSeekCodex):
    SUPPORTS_RESUME = True
