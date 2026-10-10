"""Install the evaluator-owned Chat Completions -> Responses adapter.

This module is loaded by Python before the real DeepTutor CLI because the
launcher places ``agentloop/`` first on ``PYTHONPATH``.  It patches only the
client factory seam; DeepTutor still owns the actual mastery capability,
agent loop, tool registry, state changes, and final response.
"""

from __future__ import annotations

import os
from pathlib import Path
import json


def _install() -> None:
    endpoint = os.environ.get("AGENTSWE_RESPONSES_BASE_URL", "").strip()
    if not endpoint or os.environ.get("AGENTSWE_RESPONSES_ADAPTER") != "1":
        return
    from deeptutor.core.agentic import client as client_module
    from responses_client_adapter import ResponsesChatAdapter, responses_text_stream

    def build_openai_client(config):  # type: ignore[no-untyped-def]
        token = os.environ.get("OPENAI_API_KEY", "broker-only-placeholder")
        return ResponsesChatAdapter(endpoint=endpoint, token=token)

    # Patch both the public factory and its lower-level implementation.  The
    # pipeline imports the public symbol later, while tests may call either
    # seam directly.
    client_module.build_openai_client = build_openai_client
    client_module._build_openai_client = build_openai_client

    async def evaluator_responses_stream(
        prompt: str = "",
        system_prompt: str | None = None,
        **kwargs,
    ):
        """Route services-layer auxiliary LLM calls through Responses too."""
        token = os.environ.get("OPENAI_API_KEY", "broker-only-placeholder")
        async for content in responses_text_stream(
            endpoint=endpoint,
            token=token,
            prompt=prompt,
            system_prompt=system_prompt,
            **kwargs,
        ):
            yield content

    # Post-turn title generation imports ``deeptutor.services.llm.stream`` at
    # call time.  Patch both the package export and the factory definition so
    # no auxiliary product request bypasses the locked Responses transport.
    from deeptutor.services import llm as llm_module
    from deeptutor.services.llm import factory as llm_factory

    llm_module.stream = evaluator_responses_stream
    llm_factory.stream = evaluator_responses_stream
    os.environ["AGENTSWE_CHAT_TO_RESPONSES_ADAPTER"] = "installed"
    marker = os.environ.get("AGENTSWE_ADAPTER_MARKER", "").strip()
    if marker:
        path = Path(marker)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "agentswe-deeptutor-chat-responses-adapter/v1",
                    "installed": True,
                    "endpoint": endpoint,
                    "transport": "chat-completions-to-responses",
                    "services_stream_transport": "responses",
                    "candidate_token": "broker-only-placeholder",
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


try:
    _install()
except Exception as exc:  # pragma: no cover - surfaced by the launcher probe
    os.environ["AGENTSWE_CHAT_TO_RESPONSES_ADAPTER_ERROR"] = type(exc).__name__
