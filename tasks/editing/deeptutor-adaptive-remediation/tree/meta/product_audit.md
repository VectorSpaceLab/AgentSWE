# DeepTutor product audit

| Surface | Audited product seam | Agent-loop use |
|---|---|---|
| CLI | `deeptutor = deeptutor_cli.main:main` | Broad application entry; not used as the narrow lower case entry. |
| Mastery capability | `deeptutor.capabilities.mastery.capability.MasteryPathCapability.run` | Authoritative lower entry. |
| Agent loop | `deeptutor.agents.chat.agentic_pipeline.AgenticChatPipeline` and `agents/chat/agent_loop.py` | Model planning, function calling, tool dispatch, final answer. |
| Model client | `core.agentic.client.build_openai_client` | Candidate-facing Chat interface adapted to evaluator Responses. |
| Tool registry | `runtime.registry.tool_registry` and `tools.builtin.BUILTIN_TOOL_TYPES` | Registers `MASTERY_TOOL_TYPES`; Candidate changes become model-visible tools. |
| Path state | `learning.storage.LearningStore` through `services.path_service.PathService` | Fresh per-case persistent learner state. |
| Native benchmark | `dev_cases/scenario_driver.py`, `harness_support.py` | Build/seed/oracle support only; not Result executor. |
| Candidate patch locus observed in old formal | mastery tools, remediation module, path service, mastery API router, learning models | Evidence that the Edit mechanism is reachable through the selected lower entry. |

The old frozen GPT Candidate changed five files and added the remediation,
review, event, handoff, policy, snapshot, attestation, witness, verify, and
chain-audit tools to the product registry. That historical run is evidence of
feasibility, not a required implementation or answer.
