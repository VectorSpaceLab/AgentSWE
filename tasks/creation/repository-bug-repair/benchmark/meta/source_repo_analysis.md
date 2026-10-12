# Source Repository Analysis

## Pin and provenance

- Primary evidence repository: `https://github.com/SWE-agent/SWE-agent`
- Owner: SWE-agent organization
- Pinned branch/commit inherited from v3 analysis: `main` at `3ea751c087f32b16e039a2233dd6eefecef325d5`
- Commit author date: 2026-07-16
- License: MIT
- Supplementary evidence: `https://github.com/SWE-agent/mini-swe-agent`, `main` at `a83fcae82d2a08f0ee0c688f9d137b3566c097f8`, MIT
- v4 construction source: local read-only v3 benchmark at `${AGENTSWE_HOME}/benchmark/repository-bug-repair-agent-hard-v3`

The pinned projects are general coding-agent harnesses whose observable value is issue interpretation, repository inspection, terminal/tool use, editing, test execution, recovery from failed attempts, and extraction of a final patch or answer. Their provider orchestration, prompts, trajectory formats, UIs, batch scheduling, container integrations, and benchmark adapters are implementation choices rather than target capabilities.

## v4 benchmark abstraction

The benchmark asks a builder to create a repository-repair agent with one uniform CLI and portable patch/report output. It does not reproduce SWE-agent internals. The source repositories provide feasibility and capability evidence; v3 provides the prior benchmark contract and demonstrates weaknesses to repair.

V4 replaces v3's tens-of-lines fixtures with eight synthetic multi-module standard-library projects. Hidden repositories are 264–344 physical production lines and intentionally present several plausible fault locations. Cases retain deterministic local execution while increasing diagnosis across normalization/cache boundaries, journal/state synchronization, SQLite schemas and transactions, iterator/heap planning, incremental protocol state, tenant-scoped event invalidation and atomic batch writes, and checksummed segment/manifest recovery.

## Inputs, outputs, and boundaries

Typical input is a behavior report plus one complete local repository and optional pinned local profile. Required outputs are a clean unified patch and structured evidence reports. Recovery cases add an executable artifact that the evaluator runs against an isolated work directory.

Excluded capabilities include live GitHub issue mutation, external repository cloning during a case, SWE-bench container orchestration, provider-specific prompts, trajectory scoring, million-line repositories, GUI/API serving, offensive-security modes, and unstable live-data dependencies. Every case is solvable from local evidence.

## Safety observations

Repository text, issue prose, profiles, command output, and generated code are untrusted. The evaluator parses active-input constraints, copies assets to a temporary repository, initializes a Git baseline, checks and applies text patches, enforces path/symlink restrictions, removes credentials from the runtime environment, bounds processes, and restricts recovery execution to a Python artifact and placeholders. Patched code is still executable and must be evaluated inside a disposable OS sandbox.

All case repositories, requests, profiles, tests, identifiers, and records were authored synthetically for this benchmark. The WireBatch profile is synthetic CC0-1.0 material. No source-repository code, prompts, trajectories, or outputs were copied into builder-facing artifacts.

