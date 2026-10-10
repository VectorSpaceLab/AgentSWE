# Resources and budgets

## Local environment

- Linux, Node.js 24, npm 11, Python 3, Git, and SQLite build support.
- The pinned lockfile is authoritative. Root and fake-server dependencies
  may be prepared once and reused by public and hidden worktrees.
- Product scenarios use the local fake engine, tests, browser, Git, and
  database boundaries. The runtime is offline except for evaluator-owned
  loopback services.
- Builder access is limited to the repository, the two public cases/assets,
  and the designated three-file output directory.

## Authorized builder assistance

You may call GATEWAY Responses only at
`https://gateway.example.com/v1/responses` with model `gpt-5.6-sol`,
reasoning effort `high`, the bearer credential environment variable
`GATEWAY_API_KEY`, and at most 12 calls.

Optional public documentation search may use only the configured
Serper-compatible service with the bearer credential environment variable
`SERPER_TOKEN`, at most 8 search calls, plus at most 8 direct public
retrievals totaling 20 MiB through the documented proxy. Record actual
calls in `run_report.json.api_calls`. Never send credentials, source dumps,
hidden/evaluator data, delivery reports, or private reasoning.

No alternative model, search provider, remote execution service, or
third-party agent that implements the feature is authorized.

## Execution budget

- The current agent-loop Builder uses 8 CPUs and 16 GiB of aggregate
  container memory; the native session wall-clock cap is 28,800 seconds
  (8 hours), with an outer default of 28,920 seconds including shutdown and
  cleanup headroom.
- Harbor's environment image preparation `build_timeout_sec = 900` and the
  Candidate type check are two stages; the complete budget for each
  baseline or Candidate build is 600 seconds and 4 GiB, including runtime
  preparation, dependency copies, `npm run ts`, and cleanup of the owning
  container.
- Each public/hidden lower-agent product case has 600 seconds and 4 GiB in
  total, covering setup, model, product execution, artifacts, observation,
  and cleanup. The upper and lower stages do not each get a fresh 600
  seconds.
- Builds and the lower agent use a real cgroup aggregate memory cap that
  includes all descendant processes, with swap disabled; it is not a
  single-process RSS or virtual address space limit. Clean up all
  descendants by the exact attribution of this run.
- Acceptance repair defaults to 4 attempts, with a hard maximum of 6
  fix-mutation completions. Every behavior case gets a fresh process,
  database, user-data root, app checkout, fake engine, and observation path.
- The baseline type check of the pinned dependencies and toolchain runs once
  per Builder session. Pre-existing baseline type diagnostics are kept as a
  compatibility reference and the Candidate is not required to fix all old
  errors; newly introduced explicit source compile errors block entry to the
  lower stage. A Candidate that compiles, or that carries only pre-existing
  type diagnostics, is still executed by the real lower agent and its
  functional quality is judged by the independent Result judge.

Timeouts, memory failures, unprepared dependencies, toolchain mismatches,
and unattributable execution faults are all infrastructure invalidity; they
are not recorded as Candidate zeros and are not used as a difficulty
mechanism.
