# AgentSWE unified provider broker

One evaluator-owned process per **role** (`builder`, `runtime`, `judge`, `search`) per run. It is
the only process that holds a provider key, and the only place that knows which provider, wire,
model slug and effort spelling a role uses. Everything else speaks the OpenAI Responses wire to it
with a placeholder token.

```
Codex 0.144.1 ──Responses SSE──▶ broker[builder] ──┬─ responses: passthrough ─▶ provider
                                                    └─ chat: Responses⇄Chat ───▶ provider
candidate ─▶ task's evaluator broker (unchanged: budgets, /events, /stats, truncation resample …) ─▶ broker[runtime] ─▶ provider
judge / τ³ user sim / PinchBench grader ─▶ task's judge broker (unchanged) ─▶ broker[judge] ─▶ provider
candidate search call (legacy {query,page,search_type,token}) ─▶ broker[search] ─▶ Serper-compatible API
```

Standard library only (Python ≥ 3.10). `python -m agentswe_broker --help`.

## Why a layer *behind* the existing evaluator brokers

Each family (and each Editing task) already has its own evaluator broker whose counters feed
validity gates: Creation's `/events?evaluation_id=` failure classification, Editing's per-task
lower brokers and the Result-judge accounting gate, Optimization's per-role budgets. Replacing them
would change evidence formats that formal finalization checks byte for byte. Instead, their
hard-coded `UPSTREAM`/`MODEL`/effort constants are pointed at this broker, which makes **exactly one
upstream submission per accepted request** and relays status codes faithfully. Their retry loops,
budgets and counters therefore keep their exact meaning.

## Upstream wires

| Wire | Request | Response |
|---|---|---|
| `responses` | body forwarded; only `model` and `reasoning.effort` pinned (`--model-policy/--effort-policy lock`) | stream bytes relayed unchanged; `: keepalive` comments only at SSE event boundaries; JSON relayed unchanged |
| `chat` | `chat_translate.responses_to_chat` (from the Lite broker): instructions→system, developer→system, function/custom tool calls merged into assistant turns, **reasoning carried back on every assistant turn** (`--reasoning-mode carry`, also for `apply_patch` turns), `apply_patch` freeform tool → function with `{input}`, `max_output_tokens`→`max_tokens` (default 65536 when absent), `text.format`→`response_format`, `input_image`→`image_url` | streaming clients get strict Responses SSE (accepted by the evaluator's own `ResponseEvents` decoder); non-streaming clients get one Responses JSON object; `finish_reason=length` → `status: incomplete`; Qwen's chat-template end-token degeneration → `response.failed` |

Chat clients (`/v1/chat/completions`) are supported too: passthrough with pins on a Chat provider;
on a Responses provider, text-only translation that returns only `message` text (never reasoning
text — DeepSeek can return a reasoning item ahead of the message).

## Accounting

| Role | Stats (`GET /stats`, `Bearer stats-only-placeholder`) | Ledger |
|---|---|---|
| `builder` | `agentswe-builder-broker-stats/v2`, field-for-field as `builder_broker_xhigh` / the Lite chat broker | immutable `builder_requests/<sha256>/` (`RequestLedger`, verbatim), redacted `upstream.raw`, `translation_receipt.json` |
| `runtime`, `judge`, `search` | `agentswe-broker-stats/v1`: `runtime.calls`, `successful_calls`, `failures`, `upstream_attempts`, token sums, `classifications` | `<ledger-dir>/<role>-events.jsonl`, one terminal event per request, no bodies, no keys |

`successful_calls` counts provider HTTP 200 with a terminal response whose `status` is `completed`
— the semantics of the Editing judge broker. The test `test_editing_judge_gate_arithmetic_holds`
replays the gate in `formal_axes_shared.py` (successful-call delta == judged cases; call delta ==
transport attempts) against this broker with a resample after a max_output_tokens truncation and a 503 retry.

## Protocol constants vs configuration

The broker never changes `max_output_tokens`, budgets, retry counts or timeouts that belong to a
task protocol. It pins only what is provider-specific: endpoint, wire, model slug and the effort
spelling. Builder-role defaults that are protocol-relevant and measured: 429 is shown to Codex as
503 (Codex 0.144.1 dies on a bare 429), and `--replay-policy resend` lets Codex's stream reconnect
reach the provider again under a new ledger identity.

## Security

Keys come from `--credential-file` (one `KEY=value` line, 0600, read-only mount; exactly the named
variable, no fallback) or from the role's env variables. Client tokens default to the legacy
placeholders (`broker-only-placeholder`, `runtime-only-placeholder`, `judge-only-placeholder`).
Upstream error bodies are redacted before they are relayed; the CLI ready line and `describe`
report only *which variable* supplied a key.

## Commands

```bash
python -m agentswe_broker describe --env-file .env                  # resolved, non-secret config
python -m agentswe_broker probe --role judge --env-file .env [--image]  # 2-3 tiny calls: echo, JSON schema, vision
python -m agentswe_broker serve --role runtime --credential-file run/tmp/runtime.key \
    --stats-file run/broker/runtime_stats.json --ledger-dir run/broker --bind 0.0.0.0 --port 0
```

## Tests

`python -m unittest discover -s tests -t .` — 84 tests, offline (fake provider on 127.0.0.1):
38 carried over unchanged from the Lite chat broker, plus passthrough, Chat translation end to end,
accounting-gate arithmetic, search shim, configuration, CLI and probe.

## Not done here

* Pointing each evaluator broker's constants at this broker (mapping in `inventory/sites.tsv` and
  `routing_constants.tsv`; done when O/OE extract each task).
* A real-provider smoke (needs approval for a provider key).
* The Editing `--builder-transport broker` route: not needed; the direct route simply uses the
  broker URL as its provider, so `native_codex_direct` evidence stays as it was.
