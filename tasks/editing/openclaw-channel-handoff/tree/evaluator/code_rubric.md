# Independent Code rubric — 100 points

Score the frozen submitted source only. Do not add this score to Agent-loop
Result or average the axes.

| dimension | points | full-credit anchor | low-credit anchor |
|---|---:|---|---|
| interface_lifecycle | 15 | exact Gateway RPC lifecycle, schema compatibility, ownership and freeze-safe transitions | missing methods, ad-hoc side channel, or broken lifecycle |
| requirement_mechanism_coverage | 20 | mechanisms cover grants, leases, logical clocks, outbox, media, delivery, interactions, migration and repair | requirements are prompt-only, partial, or hard-coded |
| analysis_evidence_integrity | 15 | claims are tied to durable facts, receipts, digests and observable states; no fabricated evidence | non-authoritative logs or random tokens treated as proof |
| safety_privacy_side_effects | 15 | route/visibility/auth boundaries, loopback validation, secret isolation and idempotent effects | credential/oracle leakage, foreign route, duplicate side effect |
| recovery_honest_failure | 10 | restart/takeover/ambiguous provider outcomes converge and remain honest | retries duplicate, resurrect, or overclaim |
| testability_observability | 10 | focused tests and bounded redacted projections expose the important invariants | tests only cover happy paths or expose sensitive internals |
| maintainability_generalization | 10 | clear modular implementation generalizes across direct/group/thread and schema versions | case-specific branches, duplicated state logic, brittle constants |
| resource_discipline | 5 | bounded state, I/O and runtime work with no unapproved services | unbounded growth, heavy or unrelated service dependency |

For each dimension record concrete file/symbol evidence. Reasonable choices of
TypeScript structure, database helper, RPC serialization, and test framework do
not lose points when behavior is equivalent.

