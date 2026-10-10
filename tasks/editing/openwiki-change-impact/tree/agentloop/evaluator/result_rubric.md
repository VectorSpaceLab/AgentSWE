# OpenWiki semantic Result rubric — 100 points

Judge the actual modified OpenWiki agent's case outcome against the exact
executed request, private expected facts, physical document/publication/search
state and raw CLI/model output. The same rubric applies to public dev and
hidden cases. The independent Code axis is not used here.

This task is change-impact analysis for a wiki. The product exists so that,
given one source change, a maintainer learns **every** page that change
affects, **only** the pages it affects, **why** each one is affected, and can
then hand a reader a site and an index that describe exactly the documentation
that was installed. Weight verified cross-cutting behaviour, not the presence
of an artifact, a field or a filename.

| Dimension | Maximum | Meaning |
|---|---:|---|
| change_impact_evidence | 24 | Identify the real source change; report exactly the affected pages, separating direct from transitive, each with concise provenance and no page the change does not affect; repair stale facts, examples, diagrams and links while preserving unaffected and handwritten bytes. |
| durable_recovery_correctness | 20 | Achieve the requested idempotency, ownership/fencing, recovery, conflict and read-only refusal behaviour using real durable state and observed product operations, and leave no abandoned claim, lock, staging tree or temporary file. |
| static_publication_correctness | 16 | Publish the actual corrected corpus as immutable, offline-openable, self-contained release bytes with verified manifest, file and page hashes and working navigation. |
| search_index_correctness | 16 | Build a persisted, normalized index of exactly the allowed corpus that answers the case's queries against the pages that should answer them and no longer carries text this generation removed. |
| cross_surface_convergence | 16 | Documentation, receipt, active site and active index move together on one generation and one payload digest; a reader observes a complete generation or the previous one, never a hybrid; failures close and preserve the last complete generation. |
| evidence_faithfulness_and_safety | 8 | Report only supported outcomes; preserve scope boundaries, reject unsafe and untrusted inputs, distinguish limitations and missing behaviour honestly. |

Do not assign points merely for an LLM call, a JSON schema, populated fields,
an ID, a filename, an evaluator-labelled CLI event or an asserted digest.
Oracle booleans are factual evidence, not independent point-bearing tests or
an additive field-presence formula. Validate claimed receipt/index/document
hashes against actual bytes. Final state alone does not prove concurrency,
replay or fencing; absence of real action evidence must be reflected in the
corresponding semantic dimension. A truthful partial result can receive
appropriate partial credit; missing required behaviour is still missing.

Editing documentation files by hand is not the same as running the production
impact path. If no `impact-report.json` and `documentation.diff` were produced
by the compiled CLI, the change-impact behaviour this task measures did not
happen, whatever the final Markdown looks like.

The oracle is observational only: it never performs the requested repair or
publication workflow. Its unexecuted example/query checks must not be mistaken
for successful product operations. Use exact case expectations and genuine
product evidence. Infrastructure-invalid execution has no numerical Result
score; an attributable fatal Candidate execution gate is a separate zero
contract, not a fictitious semantic judge response.

## Evidence-bound ceilings

The evaluator issues a per-case `agentswe-result-score-caps/v1` contract bound
to these exact rubric, native-evidence and oracle bytes. The same contract is
issued for the public dev cases, so a dev score and a formal score for the same
behaviour carry the same ceiling. Each condition below
restates a published obligation. Score the dimensions on their own merits
first; the ceiling then applies to the total.

Conditions the **evaluator** decides from the product's own persisted
workspace bytes (`deterministic_surface_assertions` in the oracle summary):

| Id | Ceiling | Condition |
|---|---:|---|
| `c1_no_complete_active_generation` | 25 | A selected publication or search surface has no complete, hash-verified active generation. |
| `c2_impact_evidence_unsound` | 40 | The impact report is missing or incomplete, claims an unaffected page, misclassifies direct versus transitive, states an affected page with no reasons, or disagrees with the documentation actually changed. |
| `c3_no_durable_committed_receipt` | 35 | No immutable committed receipt binds the requested identity to the documentation hashes actually installed, or the tenant state never advanced. |
| `c4_cross_surface_divergence` | 45 | Receipt, active site and active index do not agree on one generation and one payload digest, or abandoned coordination state survives. |
| `c5_index_stale_or_unanswerable` | 45 | The persisted index cannot answer a query the case names against the page that should answer it, or still carries text this generation removed. |
| `c6_artifact_contract_violation` | 55 | The result artifact is not in the documented agent-loop result contract. |
| `c7_scope_or_offline_violation` | 30 | A write landed outside the manifest-authorized roots, or the published site carries executable or remote content. |
| `c8_surface_family_coverage` | graded | `16 + 4 x families` for the five published surface families (change impact, static publication, full-text search, durable convergence, production safety); full coverage lifts the ceiling entirely. |

Conditions **you** decide, from the trajectory, and report in
`ceiling_assessments`. Look for the behaviour in the raw trajectory before
declaring a violation; a truthful account of work that was actually done is not
a violation, and neither is a boundary the case did not ask for.

| Id | Ceiling | Violated when |
|---|---:|---|
| `c9_exact_retry_idempotency_not_demonstrated` | 35 | The case asked for a lost response, restart or duplicate of the same logical identity and the trajectory does not show the product replaying it from the persisted receipt with an empty diff and byte-stable receipt/site/index state. |
| `c10_fail_closed_boundary_not_demonstrated` | 35 | The case's named fail-closed boundary was not shown to be refused with a non-zero exit, a concise schema-valid failure report and unchanged durable state. |
| `c11_production_cli_not_the_actor` | 30 | The documentation, receipt, publication or index bytes were produced by ad-hoc shell commands, hand-written files or a throwaway script instead of the compiled production OpenWiki entry point. |
