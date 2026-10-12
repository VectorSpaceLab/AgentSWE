# Agent-loop Result rubric (100 points per case)

The score is attached to the frozen Candidate's real DeepCode lower-agent
rollout. A native test pass, a source symbol, a plan file or a narrated
scenario is never sufficient: a boundary earns points only when an actual
product command produced an observable response and the persisted product
state or the raw trajectory corroborates it.

| axis | points | evidence |
|---|---:|---|
| scientific_capsule_correctness | 20 | Case-specific numeric facts, seeds/data/configs, explicit scientific gaps and a justified complete/partial/blocked status agree with the generated artifacts, **and the published capsule's own `paper_spec.json`/`traceability_graph.json` carry the case's claim identifiers as graph nodes and claim-to-code mappings.** |
| durable_operation_correctness | 20 | The requested concurrency, lease, scope, corruption, atomicity or blocked-publication boundary on the preserved `traceability`/`traceability_runs` surfaces works through actual product operations and persisted receipts. |
| revision_review_promotion_correctness | 25 | Immutable registration with deduplication and changed-payload conflict, canonical semantic diff, policy-authorized review with monotonic review generations that do not advance on exact retry, and promotion as an atomic compare-and-swap that refuses early, stale, concurrent and unproven attempts. |
| resumable_execution_and_audit_correctness | 20 | Plans bound to an exact revision digest and review generation; one manifest command per ordered checkpoint with response-loss idempotency; pause/resume/cancel generation and token fencing that stale owners cannot cross; attestations bound to the immutable digest; and an append-only, tenant/project-scoped, authorization-gated audit chain that reports `chain_valid` and fails closed on tampering, cross-scope access and unauthorized actors. Quarantine/restore fencing scores here when the case requires it. |
| evidence_and_report_integrity | 15 | Claims bind to real code/config/data/tests/commands/artifacts and to inspectable, consistent publication, generation, digest and operation receipts; the artifact honestly reports achieved results, unresolved gaps, refused and unattempted steps and safe next actions, without invented completion or cross-scope disclosure; and the artifact is written in the contract the patched product documents. |

## What counts as product behaviour

The driver writes files of its own inside the case workspace: captured stdout,
request bodies, summaries. Those are the Candidate's narration. The private
oracle separates them (`product_store_paths` and `product_store_count` versus
`agent_captured_file_count`) and every deterministic check below reads only the
product's own stores. A captured response file is not proof that the product
behaved that way; a persisted receipt, ledger event, snapshot or checkpoint is.
When the two disagree, the store wins and the discrepancy is a
`evidence_and_report_integrity` deduction.

A boundary is demonstrated only when the refusal is visible in the product's own
record: an exit code and a stable `error.code`, a persisted refusal receipt, and
the targeted state shown unchanged. A refusal that exists only in the driver's
transcript earns nothing.

## Scoring discipline

Score every dimension out of its maximum and do not redistribute weights. An
operation the rollout never executed scores zero in its dimension, however
well it is described. A refusal only counts when the exit code and a stable
`error.code` were observed **and** the state that had to stay unchanged is
shown to be unchanged. Reasonable equivalent JSON ordering, identifiers,
provenance labels and local storage choices lose no points; the benchmark
prescribes no database, file layout, lock or class design.

## Evidence-bound ceilings

The private oracle carries `required_product_operations` for this case and
evaluator-computed `surface_assertion_comparisons` over the persisted product
state. A `false` comparison means the evaluator could not observe that evidence
in persisted state; before applying a ceiling, look for the same behaviour in
the actual product responses in the trajectory. If you can find it there,
record that and do not apply the ceiling. If you cannot, the ceiling applies.
These ceilings restate published requirements; they are not new criteria and
they are never enabled by something the Candidate merely claims. Apply the
lowest applicable ceiling to the **sum** of the dimension scores, and allocate
the remaining points semantically inside the original maxima rather than
reporting an uncapped sum next to a clamped total.

| id | condition | total must not exceed |
|---|---|---:|
| C1 | The rollout produced no observable revision-store **and** no observable execution-store behaviour for this case: the new surfaces were never exercised. | 20 |
| C2 | Any operation in `required_product_operations` was never executed as a real product command with an observable response. | 28 |
| C3 | Any fail-closed boundary this case names (promotion before review, stale generation or token, stale head compare-and-swap, unauthorized actor, quarantine block, corrupted snapshot, tampered ledger) was not demonstrated to be refused with a stable `error.code` and unchanged state. | 30 |
| C7 | The product's own stores persist no refusal receipt. A refused operation that leaves no receipt cannot detect a later reuse of its `operation_id` with a different body. | 28 |
| C9 | A recorded promotion disagrees with its bound plan's digest or review generation, precedes the revision's own generation, or stands while a required role's decision is a rejection. | 25 |
| C10 | No plan carries an ordered, duplicate-free checkpoint list where every checkpoint records a command identity, an exit code and an output digest consistent with the completion proof. | 30 |
| C11 | The append-only ledger does not cover every accepted lifecycle mutation, logs a refused operation, or repeats an event hash. | 35 |
| C12 | Fewer than all of this case's reserved adversarial probes produced a product-persisted refusal receipt. The ceiling is `20 + 5 x covered`; full coverage lifts it entirely. | graded |
| C4 | The published capsule's own graph does not carry this case's claim identifiers, **or** the audit export does not present an ordered, linked, `chain_valid` event chain. | 45 |
| C5 | The result artifact does not conform to the agent-loop result contract the product documents (see `artifact_validation.quality_schema_findings` in the native evidence). | 55 |
| C6 | Cross-tenant or cross-project disclosure, a post-cancel or post-invalidation execution, or a publication by a fenced owner was observed. | 35 |

Record per-axis points, maximums, evidence, broker deltas, applied ceilings and
validity. A broker/provider/mount/evaluator fault is `infrastructure-invalid`,
not a zero. Model calls, API names, matching IDs and JSON fields are
validity/provenance checks, never semantic points. An honest incomplete
product-authored artifact remains judgeable. Code is independent and acceptance
does not require high Result.
