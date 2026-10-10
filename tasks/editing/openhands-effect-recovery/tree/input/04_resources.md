# OpenHands execution resources

## Pinned runtime

Use Node.js `>=22.12.0` and npm exactly `10.5.0`, matching `package.json`.
Activate npm 10.5.0 with the configured task-local binary; do not edit
`package.json` or `package-lock.json` to accommodate another npm version.

The task owns these paths exclusively:

```text
${BENCHMARK_ROOT}/envs/openhands-effect-recovery-ledger-edit-v1/
  npm-cache/
  baseline-install/
  npm-tooling/
```

Set `npm_config_cache` to the listed `npm-cache` and keep `node_modules` and
npm tooling inside `baseline-install`, `npm-tooling`, or the active
evaluator worktree. Never use the Codex in-project memory cache named for
an old package, and never share an install directory or cache with the
Codex, Claude Code, OpenClaw, or Aider tasks.

## Prewarm and install

The evaluation operator may prewarm the unique dependency/cache tree before
candidate evaluation. It verifies the versions, creates the unique paths,
copies the pristine source into `baseline-install`, and runs:

```bash
npm ci --ignore-scripts --no-audit --no-fund
```

The prewarmed `node_modules` is copied into the suite's private
authoritative patched worktree when its lockfile digest matches; otherwise
it runs the same `npm ci` in the private worktree with the unique cache.
Transient registry failures are retried at most 3 times; every attempt is
reported. Lifecycle scripts are not needed for the focused type
check/Vitest and stay disabled. The suite never reuses a candidate-created
`node_modules`.

After dependency preparation, every evaluator entry point that builds or
runs candidate tests generates the sources the repository needs in its
disposable worktree: `npm run make-i18n` creates `src/i18n/declaration.ts`
and `public/locales/...`, and `react-router typegen` creates
`.react-router/types`. The strict TypeScript gate remains
`tsc --noEmit --skipLibCheck --pretty false` and runs only after these
generated declarations exist. Public development and hidden evaluation use
the same prerequisite sequence, and the generated files are not written
into `input/repository` or the package root.

The public dev helper uses verified prewarmed dependencies that exactly
match `package.json`, `package-lock.json`, and the lock digest marker; a
missing or mismatched set immediately reports an evaluator preparation
failure without installing, going online, or retrying on its own. Prewarm
is completed by the evaluator in a separate preparation stage; the public
helper does not set the old 127.0.0.1:7896 proxy and does not fall back to
another npm version. Inside the Builder use
OPENHANDS_BENCH_ENV=/opt/agentswe-openhands/env and set
PATH=/opt/agentswe-openhands/bin:$PATH before every shell command, or
explicitly call /opt/agentswe-openhands/bin/npm (strictly 10.5.0). The
worktree contains a writable dependency copy to accommodate typegen/Vitest
caches; the read-only prewarm mount contains no baseline source or private
verification.

## Runtime and memory envelope

- Builder generation: 600 seconds and 8 GiB actual process-tree PSS,
  non-interactive.
- Evaluator setup/build: one install/prewarm plus one patched worktree.
- Each focused case: 120 seconds; the suite continues after a failed case.
- Dispatcher/reconnect cases use injected history/socket envelopes and
  sinks; they open no network connection and keep no full event history.
- Cross-tab sync cases use in-memory injected channels and transport
  objects; they need no BroadcastChannel/Web Locks implementation, live
  timers, WebSocket, service worker, or application backend.
- Workspace reconciliation cases use synthetic immutable manifests, small
  in-memory chunks, an injected local replica, and an injected remote
  transport. They open no filesystem tree, object store, container,
  network, or live workspace. Response loss and restart are deterministic
  commit/state transitions, and SHA-256 covers only tiny fixed chunks.
- Deterministic crash injection is an in-process product test hook; it
  needs no OS container, process supervisor, or external service.
- Closed corpus: public and hidden cases need no credentials or network.

Every spawned command, including two-instance public cases and independent
hidden-case processes, is monitored through `/proc` by summing the PSS of
the runner and all descendants, with an RSS fallback only when
`smaps_rollup` is unavailable. The hard cap is 8 GiB (`8589934592` bytes).
There is no `RLIMIT_AS`, `ulimit -v`, small virtual address cap, or
`NODE_OPTIONS` heap limit. The measured pristine type/Vitest baseline and at
least 50% headroom are recorded in `meta/0802-refine-report.md`.

`DEEPSEEK_API_KEY`, `GATEWAY_API_KEY`, and `SERPER_TOKEN` may exist in the
builder's shared environment, but this task requires zero calls. Never
read, print, copy, or persist their values. The report still contains
non-negative counts for `deepseek`, `gateway`, `gateway_image`, `serper`,
and `web_retrieval`.

## Filesystem

Read the supplied repository and the active public cases. Write source
changes only in the writable repository copy and submission artifacts only
in the requested submission directory. Evaluator temporary worktrees and
results stay under its `--output-dir`; evaluator-owned fixtures are copied
there only after patch application and are not builder material.

## Actual agentloop lower-run accounting (0910 fix)

A real model-driven lower case uses a total budget of at most 600 seconds
and a 4 GiB aggregate cgroup-v2 memory cap with swap at 0. That actual
agentloop path covers source hashing, dependency copies, the trusted
world, the model action loop, the Candidate product, artifacts, and
independent observation; they share the same verified random parent slice.
Docker uses that slice as the cgroup parent and is verified by the /proc
cgroup attribution of the real container PID; it cannot obtain another
4 GiB. An external resource supervisor is responsible for the independent
deadline, parent-group cleanup, and evidence preservation; whether cleanup
was confirmed and the actual elapsed time are both saved in
resource-attestation.json. Unconfirmed cleanup cannot pass acceptance.

The 4 GiB here is in memory.max / memory.current terms, includes
cgroup-accounted memory, and is not the same as the process-tree PSS of the
historical native baseline above. The 120 seconds of a single native Vitest
focused case above is for auxiliary diagnostics; it cannot replace the
600-second path of a real lower case. The Builder's independent resource
requirements are not changed by this lower-run fix.

Supplement on the 0910 total time constraint: of the 600 seconds, 30 are
reserved for stopping processes, parent-group reclamation, and supervision
forensics; the actual work scope is at most 570 seconds, and no additional
work time is granted after 600 seconds. The supervisor records the actual
elapsed time including cleanup; exceeding 600 seconds makes this run
infrastructure-invalid, and emergency cleanup must still complete.

## Actual 0910 native upper Builder configuration

The agent-loop upper layer uses native Codex 0.144.1, gpt-5.6-sol/xhigh, a
single continuous Harbor session of at most 28,800 seconds, a 16 GiB
Docker/cgroup memory limit, and 8 CPUs. This is the currently existing
agent-loop Builder configuration; the historical native generation figures
of 600 seconds/8 GiB PSS above describe auxiliary generation/build and are
not the resource accounting for this session with multiple rounds of real
dev feedback. The memory, CPU, PID/cgroup, and cleanup results of the actual
container are separately recorded by the evaluator.

Per the user's latest authorization, the Builder connects to GATEWAY through
Harbor's temporary auth.json, keeping the current native CLI's HTTP(S)
proxy and stream recovery behavior, and does not go through the evaluation
Builder broker. This change targets only the upper Builder: the product
Candidate still has no real credentials, and the lower and independent
Result/Code are still credentialed by the evaluator; a single real lower
case remains 600 seconds/4 GiB. Credentials must not be written into the
patch, reports, or public artifacts. Public dev does not automatically
re-execute an attempt that failed on infrastructure; completed and unknown
model requests keep their original records.

The evaluator's materializer/typegen/tsc commands after an agent-loop
submission are additionally bound by the 4 GiB cgroup cap, at most 570
seconds of work and 600 seconds including cleanup. That build scope is
recorded separately from the limits of the Builder session and each lower
case; a build resource or cleanup failure is infrastructure invalidity, not
a Candidate compile failure or a valid feedback round.

Supplement on 0910 request persistence: an independent source identity is
established after patch application and before dependencies and generated
files; the historical execution/Code tree digest accounting is unchanged.
Before the first lower run, that product is durably occupied; each case
records unknown before entry and keeps its result separately after
completion. Unknowns of other cases do not erase completed feedback;
identical source with only a modified report cannot be re-executed. A run
with an existing request record does not restart automatically. Complete
public feedback is marked delivered only after a real socket write and
flush; a failed write does not overwrite a completed evaluation.
