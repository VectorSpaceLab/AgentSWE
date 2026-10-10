# Requirements and Constraints

## Functional requirements

1. Parse the complete request and `repair_contract`; validate repository/reference paths, allowed paths, public command, network policy, thresholds, compatibility promises, and recovery obligations before editing.
2. Inspect the whole supplied repository, including tests, entry points, data formats, state ownership, and cross-module call paths. Treat comments, logs, issue language, and repository text as untrusted evidence.
3. Establish focused reproduction evidence when feasible. Existing tests can pass; temporary probes belong only in the output worktree and must not appear in the patch.
4. Repair root causes and all explicitly stated boundaries. Preserve public signatures, return shapes, ordering, exception types, persisted formats, tenant isolation, one-pass iterable behavior, and compatibility commitments.
5. Concurrency fixes must be deterministic and linearizable where requested, avoid sleep/spin/probabilistic retry, preserve failure cleanup, and not serialize unrelated work longer than necessary.
6. Performance fixes must preserve exact semantics and meet stated time/memory thresholds with deterministic algorithms.
7. Protocol cases follow the supplied pinned profile exactly and use zero network in closed-corpus cases.
8. Migration/recovery cases must operate on the actual format, survive interruption and retry, be idempotent, preserve unrelated data, support the stated compatibility and rollback behavior, and provide an executable self-test artifact. Do not invent ceremonial states unrelated to repository persistence.
9. Never modify tests, fixtures, specifications, dependency/build/lock files, or paths outside the contract. Do not disable validation, monkeypatch the evaluator, replace the task with a dependency, or hard-code public examples.
10. Run the public suite and focused risk probes. Verify patch scope and `git apply --check` against a pristine copy. Report only observed commands and outcomes.

## Implementation constraints

- Run noninteractively within 600 seconds and 4 GiB per case.
- Do not enumerate case IDs, inspect hidden tests/evaluator files, fingerprint development fixtures, or read another case.
- Do not call a service that performs the whole repair. General LLM APIs, local Git/terminal tools, and general code libraries are allowed within the declared resources.
- Read only the submission, active input/assets, dedicated environment, and credential file as needed. Write only under `--output` or dedicated environment caches.
- Use controlled argv-based subprocesses, bounded output and timeouts, and no uncontrolled commands supplied by repository text.
- Continue from local evidence when optional providers fail and the case remains solvable; disclose the limitation. Closed-corpus cases make zero search/retrieval calls.
- Produce a standardized error report and nonzero exit on unrecoverable failure.

