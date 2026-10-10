# Model and search providers (`.env`)

Every model call in AgentSWE belongs to one **role**. A role is configured with five variables;
anything not set for a role falls back to `AGENTSWE_DEFAULT_*` (connection) or to the AgentSWE-Lite
protocol default (model, effort). The built-in provider is the DeepSeek official API, so with a DeepSeek key one
line is enough:

```dotenv
AGENTSWE_DEFAULT_API_KEY=...
```

Another provider for everything needs three lines (`WIRE` is `responses` or `chat`, see below), plus the model
names that provider uses (`AGENTSWE_<ROLE>_MODEL`):

```dotenv
AGENTSWE_DEFAULT_BASE_URL=https://provider.example.com/v1
AGENTSWE_DEFAULT_API_KEY=...
AGENTSWE_DEFAULT_WIRE=responses
```

Setting a provider or runtime value explicitly is a configuration choice, even when it repeats the built-in value:
with no `AGENTSWE_PROFILE`, any of `AGENTSWE_DEFAULT_BASE_URL`, `AGENTSWE_RUNTIME_{BASE_URL,MODEL,EFFORT}` or
`AGENTSWE_CREATION_RUNTIME_{BASE_URL,MODEL,EFFORT}` turns off the smoke-only Creation runtime effort
(`explicit-none`), and any of `AGENTSWE_DEFAULT_BASE_URL`, `AGENTSWE_RUNTIME_{BASE_URL,MODEL}`,
`AGENTSWE_OPTIMIZATION_RUNTIME_{BASE_URL,MODEL}` or `AGENTSWE_OSWORLD_EFFORT` turns off the smoke-only OSWorld
effort (`profiles/README.md`). So leave `AGENTSWE_DEFAULT_BASE_URL` unset for the DeepSeek official API.

## Roles

| Role | Who calls the model | Reaches the provider through |
|---|---|---|
| `BUILDER` | Codex CLI 0.144.1, the coding agent under test | Creation, Optimization: broker role `builder` (Codex sees a placeholder token only). Editing: the provider directly; Codex holds the real key in its container's `auth.json` for the Builder session (`docs/ENV.md`, "Where the keys are") |
| `RUNTIME` | the built candidate agent, during dev and held-out evaluation; Editing lower agents; Optimization agents under test | the task's evaluator broker (unchanged accounting), which points at broker role `runtime` |
| `JUDGE` | Result/Code judges, τ³ user simulator and NL-assertion judge, PinchBench LLM grader | the task's evaluator/judge broker, which points at broker role `judge` |
| `SEARCH` | candidates of T2 tasks (web research, PPTX sourcing, BrowseComp) | broker role `search` (candidates never see the real key) |

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `AGENTSWE_DEFAULT_BASE_URL` | `https://api.deepseek.com/v1` (built in) | Provider base URL. The broker appends `/responses` or `/chat/completions`; a URL that already ends in the route is used as is. |
| `AGENTSWE_DEFAULT_API_KEY` | — | Provider key. The brokers read it from 0600 files the runner writes; in Editing the Builder's Codex also receives it (`docs/ENV.md`, "Where the keys are"). Never logged; `agentswe doctor` reports only *set / not set*. |
| `AGENTSWE_DEFAULT_WIRE` | `responses` | `responses` (OpenAI Responses API; passthrough) or `chat` (Chat Completions only; the broker translates). |
| `AGENTSWE_<ROLE>_BASE_URL` / `_API_KEY` / `_WIRE` | `DEFAULT_*` | Per-role override (`<ROLE>` = `BUILDER`, `RUNTIME`, `JUDGE`). |
| `AGENTSWE_<ROLE>_MODEL` | `deepseek-flash` | Upstream model slug, exactly as the provider names it. The broker pins it on every request. |
| `AGENTSWE_<ROLE>_EFFORT` | builder `max`, runtime `high`, judge `max` | The provider's **spelling** of the protocol's effort level (see below). `none` sends no effort field. |
| `AGENTSWE_SEARCH_BASE_URL` | — | Serper-compatible search API, e.g. `https://google.serper.dev`. Required only by T2 tasks. |
| `AGENTSWE_SEARCH_API_KEY` | — | Search key (read only by the broker's `search` role). No fallback to the model key. |

### Further settings

| Variable | Default | Why it is needed |
|---|---|---|
| `AGENTSWE_SEARCH_WIRE` | `serper` | The paper's search proxy used its own request shape (`POST {query, page, search_type, token}`), not Serper's (`POST /<type>` with `X-API-KEY`, body `{q, page}`). `serper` translates; `legacy-proxy` forwards the old shape. Candidates keep the old contract either way. |
| `AGENTSWE_BUILDER_CONTEXT_WINDOW` | from `builders/codex/known_models.json` | Needed only when `AGENTSWE_BUILDER_MODEL` is neither a Codex built-in nor in `known_models.json`: Codex needs a catalog entry (else it silently drops `apply_patch`), and the entry needs the prompt window the provider accepts. |
| `AGENTSWE_APT_MIRROR` | — | Optional apt mirror for images that install system packages (TerminalBench task images). Symmetric with `AGENTSWE_PIP_INDEX_URL` / `AGENTSWE_NPM_REGISTRY`. |
| `AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS` | `65536` | The chat translator's completion cap sent to a Chat provider when the client sends none (Codex never does). Lower it for providers with small output limits; keep `BUILDER_CONTEXT_WINDOW + this <= provider context`. |
| `AGENTSWE_EVALUATOR_PROXY_URL` | — | An evaluator-owned **loopback** HTTP proxy for the broker's egress (`http://127.0.0.1:<port>`). Nothing else is accepted. |

## Effort levels are protocol, their spelling is provider-specific

The protocol fixes *which* level each role uses (the paper's Lite runs: builder = maximum, runtime = high,
judge = maximum; the paper's main experiment used runtime medium and judge xhigh on GPT-5.6 Sol).
Providers spell levels differently, so `AGENTSWE_<ROLE>_EFFORT` holds the spelling:

| Provider (as measured) | Levels accepted | Maximum |
|---|---|---|
| DeepSeek official API, `deepseek-flash` (Responses) | `high`, `max` | `max` |
| OpenAI-compatible GPT-5.x gateways | `low` … `xhigh` (GPT-5.6 Sol also `max`) | `xhigh` (paper) |
| DeepInfra Chat, `Qwen/Qwen3.6-35B-A3B` | `none`, `low`, `high`, `max` (default ≈ `high`) | `max` |
| DeepInfra Chat, `deepseek-ai/DeepSeek-V4-Flash-0731` | thinks only when an effort is sent | `high` (measured) |

Codex's own `model_reasoning_effort` must be a level of the model's catalog entry
(`builders/codex/gen_codex_config.py` checks it); the broker then pins the provider spelling.

## Protocol constants (not configurable)

These are part of the measurement and are referenced by evidence/attestation code. They live in
task manifests and evaluator code, never in `.env`:

* Editing judge `max_output_tokens = 64000`, one logical judge request per scoreable held-out case,
  judge inner retries 0 (pinned in five places; changing one breaks the judge broker's validation).
* Creation per-case runtime envelope (600 s, 4 GiB, 300 gateway requests of which ≤ 100 with images),
  judge output caps per task.
* Optimization per-case budgets (τ³: 50 model calls, 150 000 tokens, ≤ 4 tool calls per turn;
  PinchBench: 20 calls, 600 s, 200 000 tokens) and the judge `max_output_tokens ≥ 32000` floor.
* Builder session limits (Creation 8 h / 10 dev rounds; Editing 5 h / 5 accepted submissions;
  Optimization 16 h / 5 rounds) and multi-agent off (both Codex switches).

## What changes scores when you change provider

Any provider or model other than the published configuration produces new numbers, not a
reproduction. In particular: the model itself and its effort semantics; Chat-only providers lose
server-side reasoning state and see `apply_patch` as a JSON-wrapped function; providers without
vision degrade PDF/PPTX/GUI runtimes and judges; providers without strict JSON-schema output make
judges fail; tokenizers move token budgets (τ³ and PinchBench budgets are enforced in tokens);
latency moves the share of time-limited cases; a different search backend changes T2 evidence.
`agentswe probe-roles` sends one short request per role (and one search query) and reports whether the provider
answered under the configured model name; it does not test effort acceptance, JSON-schema output or image input,
which the first smoke run exercises. `agentswe doctor` only checks that each role's base URL is reachable.
