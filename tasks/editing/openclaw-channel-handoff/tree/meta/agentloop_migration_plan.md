# Agent-loop migration plan

## Product audit

- Product: OpenClaw multi-channel AI gateway, package version `2026.7.2`.
- Launcher: `openclaw.mjs` performs runtime/bootstrap checks and loads the
  compiled `dist/entry.js` or `dist/entry.mjs`; the production command is
  `node openclaw.mjs gateway`.
- Lower-agent entry: the Gateway's production `agent` RPC is registered by
  `src/gateway/server-methods/agent.ts`, routed through
  `agent-run-handler.ts` and `agent-run-dispatch.ts`, and executed through
  `agentCommandFromGatewayIngress` into OpenClaw's embedded agent runner.
  `agent.wait` observes the same run. `sessions.send`/`chat.send` preserve the
  user-facing channel/session route when a case needs a send or follow-up.
- Model call surface: `src/llm/stream.ts` and the OpenAI provider wrappers,
  including `openai-responses`, create the model stream. The evaluator will
  configure an OpenAI-compatible provider whose base URL is the evaluator
  broker; no Candidate receives the upstream credential.
- State: `OPENCLAW_STATE_DIR` isolates Gateway state; session keys encode agent
  and route identity, while transcript/session stores, dedupe entries, delivery
  queue, and channel/thread metadata provide the observable state boundary.
- Artifact: the lower agent must write a case-bound `agent_result.json` under
  the supplied output directory. It must contain conclusions and evidence
  references, not hidden oracle values or raw credential material.

## Behavioral design

Public cases cover distinct combinations: (1) route handoff after owner
rotation with a text delivery and callback; (2) crash/retry recovery with a
thread route, migration/compaction, and a multi-block attachment. Hidden cases
vary concurrency, stale ownership, route type, visibility, delayed receipts,
restart timing, and proof repair. They do not reuse the old byte-stream cases.

The case service creates nonce-bearing route, lease, receipt, provider-session,
attachment, and callback facts. It exposes only narrow actions and redacted
projections. Expected conclusions remain evaluator-owned. The lower agent is
scored on whether it uses real OpenClaw RPC/tool paths, maintains exact route
identity, avoids duplicate side effects, distinguishes accepted from verified,
binds claims to the current rollout/state, and reports partial/unknown results
honestly.

## Isolation and build

Builder, Candidate, public dev, hidden cases, and each case state directory are
physically separate. Candidate mounts include only the frozen product/build,
fresh state, fresh workspace, case client, and placeholder credential. They do
not include evaluator source, hidden fixtures, oracle, other cases, historical
runs, or upstream credentials. Candidate materialization performs source/digest
and secret checks and can optionally run the pinned offline install/build; this
turn deliberately did not execute that option.

## Lifecycle and cost

`controller/builder_session_controller.py` enforces one continuous Builder
session with up to ten distinct accepted submissions, both dev cases and fresh
feedback after each acceptance, latest-accepted freeze, and hidden only after
freeze. The retained `two_round_controller.py` is compatibility/probe code.
Infrastructure-invalid runs do not consume a round. A simple pilot would need
one Gateway build plus four public dev lower-agent calls and one hidden smoke
case; a complete pilot/formal would require a fresh build, two dev evaluations
per accepted digest, and six hidden cases. Resource estimates are recorded in
`meta/construction_report.md`.

## Evaluator-owned execution

`harbor/formal_one_stop.py` is the only formal-capable public lifecycle. It
starts separate evaluator-owned Builder/lower brokers, exposes a per-run Unix
socket to one Builder process, runs both dev cases after each accepted
submission, persists redacted feedback, requires an explicit same-session
feedback acknowledgement, and freezes the latest accepted digest on Builder
exit or the ten-round limit.
`controller/builder_session_controller.py` records session identity, delivery
digests, feedback digest/order, public evidence, marker absence, and the
Builder attestation. The old `evaluator/public_runner.py` is probe-only unless
explicitly invoked with `--legacy-probe` and is not formal evidence.

`evaluator/hidden_executor.py` is the post-freeze six-case executor. In formal
mode it requires the Builder attestation and Candidate runtime manifest; it
creates dynamic private facts outside the Candidate, exposes only a redacted
projection in a fresh case workspace, and invokes `lower_agent/launcher.py`.
The launcher starts `node openclaw.mjs gateway`, calls the production `agent`
RPC, and waits on the same run with `agent.wait`. Every case records broker
before/after/delta, Gateway log, launcher output, artifact contract, frozen
digest before/after, trajectory, result, and an explicit failure
classification. Formal mode rejects `PROBE_ONLY_CANDIDATE2_MARKER`, requires
the evaluator-owned `gpt-5.6-sol`/`medium` broker protocol, and requires all
six case records before declaring evidence valid. The executor never authors
`agent_result.json` and never computes a formal score.
