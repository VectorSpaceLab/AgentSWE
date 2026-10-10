# Hidden task: recover safely after a policy-worker crash

Inspect the post-crash policy state and recover only the still-authorized
lease/continuation. Produce a case-bound artifact that separates the durable
prefix from unverified work and gives a bounded recovery or escalation action.

The primary failure axis is crash recovery with lease expiry. Preserve fencing,
monotonic repair evidence, and privacy boundaries; never replay a stale lease or
accept a corrupted descendant. Dynamic lease and repair facts remain evaluator-
owned except for the sanitized product observations.

## Cycle-10 hidden world: crash recovery, lease expiry, and the ownership fence

A stopped worker holds an expired short lease. Points examined: idempotent reuse on an exact repeat, an independent same-named `tool_use_id` in another session, refusal of a conflicting action with the same identity, a cross-session post without provenance, a matching post advancing the generation, and an unprepared handoff activation and a foreign checkpoint envelope having to fail closed without changing the ownership generation.
