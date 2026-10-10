# Independent Code rubric — 100 points

Score only the frozen Candidate source against the public Claude
policy-provenance requirements. This axis is independent of hidden Result
scores: do not use Result outcomes, broker counts, or evaluator assertions as
Code credit.

| dimension | points | full-credit evidence | severe weakness |
|---|---:|---|---|
| `interface_lifecycle` | 15 | The fixed hook and inspector entries implement stable, compatible request/response and maintenance lifecycles | Missing entries, ad-hoc side runners, or broken ordinary compatibility |
| `requirement_mechanism_coverage` | 20 | Production mechanisms cover policy pinning, receipts, continuation/repair/handoff/checkpoint operations, integrity fences, and bounded reads | Requirements exist only in prompts/tests or large required mechanisms are absent |
| `analysis_evidence_integrity` | 15 | Decisions and claims derive from durable ledger, receipt, generation, lineage, and integrity evidence with explicit verification | Field presence, logs, or self-authored claims are treated as proof |
| `safety_privacy_side_effects` | 15 | Scope, path, tenant/session, generation, secret redaction, idempotency, and protected-file checks precede mutation or disclosure | Foreign/stale authority succeeds, secrets leak, or retries duplicate unsafe effects |
| `recovery_honest_failure` | 10 | Crash, partial write, stale lease, corrupt tail, altered envelope, and unsupported legacy states converge safely and report uncertainty honestly | Recovery resurrects stale work, hides corruption, or claims unobserved completion |
| `testability_observability` | 10 | Focused deterministic tests and bounded inspector views make concurrency, persistence, tamper, and restart behavior independently checkable | Only happy paths or implementation-private state are observable |
| `maintainability_generalization` | 10 | Cohesive modules and data contracts generalize across policies, sessions, generations, action types, and fresh workspaces | Case-specific constants, duplicated state machines, or brittle filename checks dominate |
| `resource_discipline` | 5 | Locks, files, journals, subprocesses, output, retries, and cleanup are bounded within the declared state/workspace limits | Unbounded work, lock waits, output, or writes outside the allowed roots |

For every dimension, cite concrete Candidate files and symbols. Accept
behaviorally equivalent implementation choices. A passing hidden case, a
schema declaration, or similarity to a reference implementation is not itself
evidence of Code quality.
