# OpenClaw execution resources

## Agentloop model and test boundary

This copy evaluates the OpenClaw Agent delivered after the Builder's
modifications. Both dev and hidden cases must launch the product's real
Gateway/Agent execution entry; the lower model configuration is fixed to
`gpt-5.6-sol`, `reasoning_effort=medium`, connected through the
evaluator-owned broker. The Candidate receives only the non-secret
`broker-only-placeholder`; real API credentials do not enter the Candidate
workspace or process environment, and bypassing the broker to reach the
model provider directly is not allowed.

Legacy offline functional regressions, dependency prewarming, fixture
initialization, and the evaluator's independent state checks may run
without calling the model, but those steps cannot substitute for the lower
Agent's execution and cannot be counted as the Agent having completed
recovery, migration, or delivery. The fault environment only provides the
initial state and the observable service boundaries; it neither chooses
recovery actions for the Agent nor writes the final result. Scoring is
based on real product behavior and the Agent's artifacts.

## Pinned toolchain and prefix

The supplied repository pins Node `>=22.22.3<23 || >=24.15.0 <25 ||
>=25.9.0` and pnpm `11.15.1`. The benchmark prefix provides Node `v24.15.0`,
pnpm `11.15.1`, its unique offline store, and the verified pristine
baseline:

```text
${BENCHMARK_ROOT}/envs/openclaw-channel-handoff-ledger-edit-v1
```

Rust and the shared Codex/Rust prefixes are not part of this task. Keep the
core packages, pnpm, npm, the XDG cache/state, and store paths under this
unique prefix.

An administrator runs `evaluator/harness/prewarm.py` once while the package
network is available. Formal/public evaluation then copies the source once,
applies the patch exactly once, and performs one offline frozen install,
one `build:strict-smoke`, and the focused gateway/protocol tests. All cases
reuse the built worktree. No case installs dependencies.

## Runtime envelope

- One formal suite: 6,900 seconds of wall clock. This covers the offline
  install, the strict build, the entry probe, the complete 930-second
  envelope of each of the six new multi-gateway cases, and a 30-second
  final-result reserve. The suite envelope serves only evaluator wrap-up;
  it does not change case behavior, assertions, weights, caps, or the
  public contract.
- Each new behavioral case: at most 930 seconds of wall clock, explicitly
  divided into the following three segments (since 0921):
  1) evaluator-owned preparation of at most 300 seconds: source hash
     verification, product materialization, startup and health checks of
     the two Gateways, native fixture readiness. This is the evaluator's
     work and does not count against the agent budget; the 0919 formal
     runs measured 214.7–249.7 seconds per case.
  2) 600 seconds of the lower agent's own wall clock, timed from when the
     agent's own sandbox is established, not during the preceding
     evaluator preparation stage, and not reset on model calls. The
     product's own node/OpenClaw startup is Candidate work and counts
     against these 600 seconds.
  3) a final 30-second reserve: first at most 10 seconds to clean up product
     processes, then at most 20 seconds for evaluator forensics and
     recording (of which the last 2 seconds are left for recording).
  When the preparation stage exceeds 300 seconds the case is not extended;
  the agent wall clock is shortened accordingly and this is honestly
  recorded in `run_report.json#timing_contract.setup_allowance_exceeded`.
  Finishing early does not require waiting until these endpoints; this is
  an explicit allocation of wrap-up overhead and does not add model
  budget. When forensics cannot complete within budget, record
  infrastructure-invalid/N/A rather than disguising it as a product 0.
- Exhaustion of the case budget is observable: when the remaining budget
  falls below 25 seconds, the relay locally refuses the next model request
  and returns 503 `lower_dispatch_refused_by_case_budget_guard`; that
  refusal is recorded in `dispatch_guard.json` and stated explicitly in
  dev feedback, and is not disguised as a product defect. The 25 seconds
  come from the measured upper bound of a single upstream request in this
  tree (p99 12.5 seconds, measured maximum 19.6 seconds).
- Per-gateway startup acquisition: a designated 120-second budget.
  Multi-gateway cases may start two independent processes; the budget
  applies to each.
- The suite worker, build, case, setup, forensics, and their child
  processes of the same top-level protected run are jointly limited by one
  random and independently verified cgroup-v2 parent slice: 24 GiB
  (25,769,803,776 bytes), swap 0. Nested scopes inherit the same parent
  group and cannot each obtain an additional 24 GiB. An external resource
  supervisor is responsible for budget verification and cleanup by
  attribution. The actual limit uses memory.max / memory.current, and the
  report stores memory.events; this accounting includes cgroup-accounted
  memory and is not the same as the process-tree PSS of the old baseline
  report.
- No `RLIMIT_AS`, `ulimit -v`, virtual address limit, `--jitless`, or
  smaller Node heap limit. `RAYON_NUM_THREADS=16` and `MALLOC_ARENA_MAX=1`
  are evaluator-owned reproducibility controls.
- Non-interactive execution. A product behavior failure in a single case
  does not block subsequent cases. Missing or corrupted evaluator case
  records, unconfirmed cleanup, and unknown broker/provider outcomes are
  all N/A; they cannot be aggregated into a product zero and do not
  consume an accepted dev round. This differs from the Candidate's own
  delivery failure: when the real product has executed, launch/isolation/
  cleanup, the model protocol, the case/frozen-source binding, and the
  complete evidence are all verified, but the core result is missing,
  cannot be parsed as a JSON object, uses an unsafe file type, or leaks
  forbidden private values, a Candidate 0 with causal evidence may be
  recorded by the existing Result rules, and the round counts when both
  dev cases are valid. When the core JSON object parses safely, defects in
  ordinary fields, case claims, evidence quality, or the auxiliary run
  report are left to the independent Result rubric, and the whole case is
  not zeroed uniformly for those defects. The absence of a model call is
  not by itself an infrastructure failure; scoring still follows the
  model-call dimension and the actual artifacts.

The Cycle 1 peak PSS was 13,218,841,600 bytes, below 24 GiB. Memory was
never the cause of the earlier global timeouts.

## Offline network and loopback boundaries

The formal build and lower run must use an isolated network namespace with
loopback enabled. Clear inherited proxy variables and real
model/channel/cloud/search credentials, and set
`NO_PROXY=localhost,127.0.0.1,::1`. Model access is allowed only through
the fixed broker channel supplied by the evaluator; this is a different
restriction from the prohibition on real external channels, remote
databases, browser, or storage access, and the "no model contact" rule of
the old offline regressions must not be applied to the lower Agent.

For effect cases, the evaluator starts an idempotent HTTP sink on loopback
and sets `OPENCLAW_HANDOFF_EFFECT_SINK_URL` in the gateway environment. It
accepts only the frozen POST contract, records the stable effect ID, and
may terminate the gateway in the dispatch/commit crash window after
accepting one effect. The sink is evaluator-owned infrastructure, not a
complete third-party task service. Candidate code must reject any
non-loopback or redirecting sink.

For channel dispatch cases, the evaluator also starts a synthetic loopback
channel connector and sets `OPENCLAW_HANDOFF_CHANNEL_CONNECTOR_URL` plus a
case-local `OPENCLAW_HANDOFF_CHANNEL_CONNECTOR_TOKEN`. It implements only
the frozen `/v1/messages` acceptance and `/v1/receipts` reconciliation
protocol. It records the exact route and stable dispatch ID, may lose the
response after acceptance, and may return unknown, failed, empty-identity,
or verified results. This is a deterministic adapter boundary, not a real
channel or a service that performs the benchmark task.

For media-bearing cases, the evaluator starts a separate synthetic loopback
provider upload adapter and sets `OPENCLAW_HANDOFF_MEDIA_CONNECTOR_URL` plus
a different `OPENCLAW_HANDOFF_MEDIA_CONNECTOR_TOKEN`. It implements only the
frozen init/chunk/finalize/status protocol. It records immutable manifests,
ordered offsets, chunk digests, and stable request bodies; it may accept at
any stage and lose the response. The channel connector may then return a
partial or out-of-order media receipt set before the complete verified set.
Neither adapter provides the candidate's storage, routing, authorization,
retry, ledger, or repair logic, and their credentials are not
interchangeable.

For inbound interaction cases, the evaluator starts a third synthetic
loopback adapter and sets `OPENCLAW_HANDOFF_INTERACTION_CONNECTOR_URL` plus
a different `OPENCLAW_HANDOFF_INTERACTION_CONNECTOR_TOKEN`. It implements
only `/v1/interactions/verify` and `/v1/interactions/status`, records the
exact stable ingest identity and callback body, and may accept a
verification and drop the response. It never provides interaction claims,
source-route authorization, reply correlation, persistence, or completion
logic. Callback tokens are synthetic fixed values and are redacted from
every evidence projection.

The evaluator assigns each independent process an
`OPENCLAW_HANDOFF_GATEWAY_ID`, provides controlled wall-clock values, IDs,
participants, state paths, ports, process signals, and ordering, and
removes environment secrets. The controlled Node preload changes only
`Date.now()` and zero-argument `new Date()`; timers stay monotonic.

## Filesystem

Agentloop lower execution launches the real Gateway through independent
PID, mount, and network namespaces within the same protected scope.
Subsequent real CLI RPCs use the same identity-verified and pinned network
namespace; they do not fall back to the host network. Only the disposable
product tree, fresh Gateway state, the Agent artifact workspace, and the
read-only Node/pnpm toolchain are provided; the whole evaluator output
directory, hidden case directories, the original frozen Candidate, the
runtime verification/baseline directories, and real credentials are not
mounted. When the launcher is invoked separately without an explicit
artifact path, the artifacts live under `workspace/` beneath its output,
and the evaluator report is still at the output root; formal dev/hidden
already use that workspace explicitly.

The fixed loopback model entry inside the namespace connects to this lower
broker through a dedicated Unix socket, exposing only `/v1/responses` and a
channel health check without model calls, and not stats, judge, arbitrary
destination addresses, or forwarded redirects. The transport bridge does
not retry on its own and forwards responses segment by segment; the
upstream broker's lower path also uses a single upstream attempt and
segment-by-segment transport, and the Builder path's protocol is not
affected by this change. The trusted host relay writes the absolute
monotonic-clock deadline of this case into every request; the broker does
not accept a Candidate extending the budget and limits calls to that
remaining time and no more than 600 seconds. On expiry or relay
disconnection it reclaims the precisely owned network workers; it does not
claim that a disconnection proves the remote provider cancelled
generation, and it does not resubmit unknown outcomes. It durably records
the number of upstream attempts, the digest of the original text, the
termination reason, and whether usage is known. Partial model output with
an explicit termination event and network interruption are recorded
separately, and missing usage is not declared as zero consumption.
Incomplete isolation startup, identity verification, cleanup, or model
transport evidence is treated as an evaluation infrastructure failure; it
is not replaced by non-isolated host execution and does not lead to a
Candidate 0.

During candidate work, read the supplied repository and the public
development material. Write implementation files only in the supplied
repository, and submit artifacts only in the requested output directory.
The evaluator writes only to its output, disposable worktrees, fresh/shared
case state, and the unique prefix. Hidden fixtures and evaluator code stay
read-only.

Supplement on the 0910 public-dev aggregate constraint: for every public
evaluation, the source verification, fresh build/cache verification, the
two dev cases, Result input preparation, and the observer run inside the
same 24 GiB parent slice, and the build/case child scopes inherit that
parent group. The external Builder controller only schedules and relays
results back. Of the public evaluation's total supervision budget of 6,900
seconds, 30 seconds are reserved for cleanup; each case is a 930-second
envelope (≤300 s evaluator preparation + 600 s agent wall clock + 30 s
cleanup and forensics), and the public evaluation's own total supervision
budget remains 4,800 seconds.

## Actual native Builder entry (0910)

The Builder uses the pinned Codex 0.144.1 native CLI through the current
GATEWAY proxy and a temporary auth.json; the Candidate/lower obtains no real
credentials, and the existing lower/public aggregate 24 GiB boundary is
unchanged. The native Builder container is 8 CPUs, 16 GiB; the Harbor agent
maximum is 28,800 seconds, the one-stop outer default is 14,400 seconds,
and an explicit --builder-timeout decides the outer limit. Before agent
setup the Harbor healthcheck verifies the actual cgroup, Node 24.15.0, pnpm
11.15.1, and the offline dependencies. Use the task tools in
/opt/agentswe-openclaw/bin, and set
PATH=/opt/agentswe-openclaw/bin:$PATH before every shell command. Warning:
do not use `pkill -f openclaw` or any broad pattern that can match your
own command line — your own harness process command line contains
`openclaw`, and this would SIGTERM yourself and terminate the whole Builder
slot directly; end only the processes you started, precisely by the
recorded PIDs. A node_modules exactly matching the public lockfile is
mounted read-only into the worktree; there is no need to install
dependencies or download runtimes, and no external package network is
opened. Only the dependencies and toolchain are provided; the baseline
source, verification, hidden cases, and credentials are not exposed.
`submit_dev_candidate --submit --wait` returns the complete public feedback
and the real native thread ID; subsequent submissions must explicitly pass
--feedback-digest <the exact digest of the previous one>. The same
completed or unknown submission does not redo dev. Exiting after one valid
submission is still an allowed formal lifecycle.
