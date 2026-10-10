# Construction report

Status: `PARTIAL` for formal execution; lifecycle infrastructure integration:
`STATIC_VERIFIED`.

The case is owner-14's OpenClaw channel-handoff Edit migration. The source is
MIT-licensed and its canonical 0830 staging digest is retained in
`meta/provenance_manifest.json`; a separate observed tree digest is recorded
because the source snapshot has pre-existing working-tree modifications.

The real product entry was audited from `openclaw.mjs`, `package.json`,
`src/gateway/server-methods/agent.ts`, `agent-run-handler.ts`,
`agent-run-dispatch.ts`, `src/gateway/openresponses-http.ts`, session/routing
modules, and `src/llm` provider wrappers. The lower launcher therefore starts
the compiled OpenClaw Gateway and drives production RPC rather than invoking
the old benchmark-native Python harness.

Inventory is exactly two public dev cases and six hidden cases. Dynamic
route/lease/grant/receipt/upload/callback facts are evaluator-owned. Hidden
inputs contain task requests only and no oracle, fixture, expected value, or
judge context. Candidate visibility is limited to public input/dev data and a
fresh product/state/case-client mount.

The evaluator-owned broker locks `gpt-5.6-sol` and reasoning `medium`, accepts
only the Candidate placeholder credential, and records calls, failures, input
tokens, output tokens, and total tokens. A bounded simple pilot was run on
September 2, 2026 for `dev_001`: the real Gateway reached ready, production
`health`, `agent`, and `agent.wait` ran, and the broker recorded `calls=3`,
`failures=3`, `successful_calls=0`, and `total_tokens=0`. The exact blocker is
that the environment has no evaluator upstream Responses URL/API credential;
the broker correctly returned `503 provider_unconfigured`, and the lower
agent reported the provider failure as partial/uncertain. This is wiring and
failure evidence, not a formal Result. No freeze or hidden run was started.

The formal-capable entry is `harbor/formal_one_stop.py`.
`BuilderSessionController` exposes evaluator-owned submit/status/feedback-
acknowledgement operations to one Builder session; it enforces two Candidate
digests, both dev cases in every accepted round, feedback acknowledgement before
Candidate 2, Candidate runtime readiness, probe-marker absence, and Candidate-2-
only immutable freeze. `evaluator/formal_gates.py` binds public/hidden records
to the Builder attestation, freeze digest, real Gateway entry, placeholder-only
credentials, and the locked lower broker. Code scoring is a separate
eight-dimension 100-point runner. Legacy native Result, Agent-loop Result, and
Code score remain separate.

Consistency checks completed by inspection: all case IDs match the inventory;
hidden count is six; protocol lock model/effort and freeze flag agree; rubric
weights total 100; no hidden input contains the word `oracle`; source and all
read-only framework directories were not edited. Full install/build, Docker,
and formal runs were not started in this repair turn. The simple pilot and
earlier hidden probe remain historical evidence only; no existing probe
Candidate is promoted. Static tests exercise the new formal gates without
starting a Gateway, broker, or Harbor.

Estimated simple pilot: one isolated Node 24.15/pnpm 11.15 build (roughly
5–10 minutes and 12–14 GiB peak PSS based on the retained baseline), four
public lower-agent case executions plus one hidden smoke (roughly 5–10 model
calls, 10–20 minutes wall time depending on provider latency), and under 1 GiB
case artifacts. It requires a real evaluator broker credential and is not
included in this phase.
