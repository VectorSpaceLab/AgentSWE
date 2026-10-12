# Dyad acceptance-driven Agent-loop migration plan

Status: `PARTIAL` at stage A. This directory contains protocol and runner
infrastructure only; no formal Builder run and no successful evaluator broker
call has been observed yet.

## Six-entry audit

1. **Real product entry.** The edited product is Dyad, not a generic coding
   agent. Its production path is `npm start` -> `scripts/start-supervisor.mjs`
   -> Electron Forge -> the main process. The lower task enters through the
   typed `chat:stream` IPC contract in acceptance mode. Acceptance handlers are
   registered by `src/ipc/ipc_host.ts`; the acceptance state, tests isolation,
   Preview, restart/recovery and Attestation surfaces are main-owned. The
   source hybrid harness is a useful deterministic seam, but its fake LLM and
   fake test runner are not themselves the lower Agent Result.

2. **Lower-agent isolation.** A Candidate is materialized into a fresh
   repository copy, with a fresh user-data/database root and case workspace.
   Builder public material contains only `input/` and `dev_cases/`. Hidden
   fixtures, evaluator source, oracle values, other cases, old runs and real
   credentials stay evaluator-owned. The lower launcher exposes only a task
   file, the frozen Candidate product, a fresh workspace and a broker URL.
   Candidate credentials are a placeholder and the broker is the only model
   network route.

3. **Dynamic oracle.** Each evaluator case owns fresh IDs, workspace revision
   markers, test fingerprints, terminal status/order, and expected linkage
   facts. The lower Agent may observe product-visible state, IPC responses and
   its own persisted receipt/attestation artifacts. It may not read case JSON,
   evaluator code, hidden fixtures or expected decisions. The oracle compares
   declarations with the fresh runtime state and the recorded rollout, rather
   than accepting non-empty invented IDs.

4. **Result unit.** Result is the edited Dyad lower-agent behavior: model calls,
   `chat:stream`/tool trajectory, dynamic acceptance facts, durable session and
   attestation provenance, final case artifact, honest recovery/cancellation,
   and safety/privacy. Native Dyad tests remain a build/compatibility gate or
   supporting oracle; they are not substituted for Agent-loop Result.

5. **Cost and resource risks.** A simple pilot is planned for one Candidate
   lower-agent dev case plus one frozen hidden smoke, not a full Builder run.
   Budget estimate: 3-8 successful lower-model calls per case, at most 2 dev
   cases per Candidate round, one revision, and one hidden smoke; 8-16 GiB
   temporary disk if npm dependencies are prepared once, 4 CPU and 4 GiB RSS
   for a lower process, and 10 minutes per product case. The full protocol is
   materially more expensive because Electron/npm setup and Playwright can
   dominate model cost. Do not start it during stage A.

6. **Failure/authority boundary.** Invalid patch, failed type/build gate,
   missing product artifact, or a lower Agent that does not call the broker is
   Candidate failure/low Result. Broker/provider/credential/mount/Docker/
   evaluator faults are `infrastructure-invalid` and do not consume a round.
   A static self-test proves only protocol logic. It cannot establish model
   behavior, a Result, or READY status.

## Candidate lifecycle

The controller accepts one through ten distinct snapshots in one continuous
Builder session. Every accepted submission runs both `dev_001` and `dev_002`,
and fresh per-case feedback is returned before a later submission. Builder exit
or the round limit freezes the latest accepted snapshot; a passing dev mean is
feedback only and does not trigger automatic freeze. Hidden cases can be
dispatched only after the freeze manifest is written and its digest is
rechecked. Infrastructure-invalid evaluation is removed from the accepted-
round count.

## Expected stage-B pilot

Run one prepared baseline/frozen Candidate through `dev_001` and one hidden
smoke only after freeze. Require broker stats with model `gpt-5.6-sol`,
reasoning `medium`, successful calls > 0, immutable Candidate/freeze digests,
and a case artifact tied to a real Dyad rollout. Until those facts exist the
status remains `PARTIAL` or `BLOCKED`, never `READY`.
