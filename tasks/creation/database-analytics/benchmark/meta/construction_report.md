# Construction Report

## Assigned and inferred parameters

- Construction date: 2026-08-24.
- Source evidence: pinned Vanna repository analysis at commit `365d0617c1a4567ffee1b19b40c27feb4206bfcf`.
- Case slug/output: `database-analytics-agent-hard-v4` in the assigned benchmark root.
- Case type: Create Agent.
- Abstracted task: build a CLI agent that turns a closed-local Markdown request, synthetic SQLite/CSV evidence, business-definition history, and access policy into a reproducible privacy-safe analytical bundle.
- Final artifact: JSON/CSV query-evidence bundle with exact result table/chart data, compact offline dashboard, decision record, field lineage, and runtime report.
- Development cases: exactly 2; hidden cases: exactly 6.
- Per-case envelope: 600 seconds, 4 GiB.
- Network: model endpoints allowed under the public resource policy; all authored cases prohibit search and page retrieval.
- Dedicated environment: `envs/database-analytics-agent-hard-v4`.

## V4 design decisions

- Actual analytical state space, not artifact count, is the difficulty lever. The eight-file top-level bundle is unchanged; dashboard weight is small relative to numerical/definition/privacy scoring.
- Every database contains hundreds to several thousand relevant fact/event rows. Dimension and policy tables remain naturally smaller.
- Full-replacement versions are represented by repeated logical keys and revision numbers; cases require revision collapse before joins.
- Fan-out traps contain enough one-to-many rows to materially change totals under a naïve join.
- Effective selection varies by structure: reporting-definition date, seller geography revision, assignment interval, tariff interval/revision, commitment rule date/weight band, approved finance mapping/coverage, and snapshot-control state.
- Permission-before-access is observable because forbidden tables are populated with synthetic protected-looking fields while allowed tables contain sufficient aggregate evidence.
- Public development privacy explicitly calibrates primary and complementary suppression. The hidden workforce case changes the mechanism to paired-year and division-margin protection rather than copying the public matrix.
- The insufficiency case defines a complete calculation policy but does not state which active data keys fail it. Material missing allocation coverage must be inferred from aggregate database inspection.
- `dashboard_replay.json`, case-specific dashboard contracts, hidden action vocabularies, transition transcripts, and replay validators were removed. Public case control IDs plus rendered offline consistency are the entire dashboard contract.
- The generic validator contains no expected values, case IDs, table plans, or answer logic.

## Case coverage and concrete difficulty

### Development cases

- `dev_001` — 1,300 orders, 4,324 line-version rows, and 365 refund-version rows. Combines as-of definition selection, store-local quarter assignment, full-replacement lines/refunds, a post-as-of refund correction decoy, posted/reversed/failed refunds, signed return lines, status/test exclusions, mixed minor digits, locked FX, line/refund fan-out, dependent market selection, and category reconciliation.
- `dev_002` — 464 organizations, 541 subscription-version rows, and 5,198 activity-event-version rows. Combines row/table/column permission before access, materially outcome-changing subscription/event corrections, distinct local days and feature families, exclusions, primary cells, deterministic complementary cells, safe plan margins, and cross-artifact non-disclosure.

### Hidden cases

- `test_001` — 1,500 orders, 4,840 item-version rows, 4,833 payment-event-version rows, and corrected seller/refund histories. It is a two-period country panel of settlement coverage, gross/refund/net merchandise values, and refund burden rather than the public decline-ranking/category-decomposition task. Settlement qualification occurs at order grain before item joins; posted item refunds occur on settled orders; a post-as-of payment revision changes qualification if the cutoff is ignored; geography, items, payments, and refunds all have revision state; currencies include zero- and two-decimal minor units.
- `test_002` — 900 workers, 963 assignment-version rows, 222 employment-event-version rows, and populated forbidden identity/compensation tables. Historical assignment replaces current attributes, an assignment correction changes historical family membership, event corrections affect voluntary classification, and disclosure uses paired-year primary suppression plus a structurally distinct complementary-pair rule for safe division totals.
- `test_003` — 720 sessions and 3,049 meter-reading-version rows across four sites. Per-session offsets cross DST/local-midnight boundaries, stale negative endpoint revisions are corrected by later versions, export readings and estimates are decoys, canceled sessions exist, tariff versions change during the window, and Wh/kWh plus cents/USD must be normalized once.
- `test_004` — 1,600 shipments, 3,184 packages, 3,328 milestone-version rows, and 1,396 exception-version rows. Shipment weight requires summing mixed kg/lb packages before a 25 kg rule band, origin-local month differs from UTC for real rows, commitment rules change by month and revision, delivery corrections and voided exceptions are present, and delay hours must receive one primary cause without fan-out.
- `test_005` — 2,984 ledger-entry-version rows and 288 rebate documents in four currencies. Ledger corrections, 336 signed contra-revenue rows, excluded tax, approved FX, mapping/allocation revision and approval-cutoff decoys, and complete allocation weights are populated. Two materially used allocation groups lack any approved family-weight set; that blocker is present only in key coverage, not copied into the request or rule document as an answer list.
- `test_006` — 4,806 curated rows, 3,483 raw-inventory-version rows, and 3,276 demand-version rows across 18 active warehouses and 178 active products. A stale complete curated snapshot and failed half-loaded reporting-date snapshot force whole-source raw recovery; raw/demand revisions, 103 absent inventory grains, zero-on-missing logic, multi-currency standard costs, effective CSV fallbacks, locked FX, top-three ranking, and category reconciliation all interact.

## Structural distinction and leakage controls

- Exact SHA-256 comparison found no identical file between public development case files and hidden case files.
- All eight SQLite table-name sets are unique. The independently recomputed maximum public-development/hidden table-name Jaccard similarity is 0.20 (`commerce.sqlite` versus `marketplace.sqlite`).
- Public/hidden request character-sequence similarity is low (maximum independently recomputed ratio 0.104 after the `test_001` redesign). Raw bag-of-words cosine is higher because all requests repeat the shared artifact contract; it is not used as the structural-distinctness criterion.
- Public builder inputs, README, and development requests contain no hidden SQLite asset filename.
- Hidden cases differ in data model and core operation: a settlement/refund multi-metric panel, temporal workforce population, cumulative metering, shipment event SLA, financial allocation sufficiency, and snapshot recovery. No hidden case repeats the public period-decline/geography-ranking/category-decomposition task graph.
- Case directories contain only `input.md` and referenced runtime assets. No case contains an oracle, expected result, answer, solution, rubric, judge context, required-content list, forbidden-error list, generator, or temporary output.

## Synthetic provenance

All databases, rule documents, CSV rows, identities, organizations, workers, sellers, drivers, recipients, shipments, amounts, timestamps, events, and policies were generated specifically for this benchmark from deterministic formulas and fixed pseudorandom seeds. Contact-like strings use the reserved `.example` domain or synthetic placeholders. No private, personal, customer, production, or copyrighted dataset is included.

The original construction generator and construction-only check code are not included in the release. During the independent v4 audit, frozen SQLite fixtures were repaired in place to make previously inert revision/sign/as-of traps material. Consequently, the unavailable original generator is not claimed to reproduce the repaired database bytes; the released databases themselves are the versioned synthetic snapshots.

## Validation performed

- Ran `PRAGMA integrity_check` read-only on all eight repaired databases: every result was `ok`.
- Independently profiled every table and reproduced the requested semantics for all eight cases with read-only SQL/Python calculations, including suppression sets, time and effective-version boundaries, fan-out defenses, recovery hierarchy, and the material finance blocker.
- Verified exact 2/6 case counts, four builder inputs, resolved asset links with no extra case files, no exact public/hidden duplicate, no hidden asset basename in public files, and no stale v3 path, generator, cache, temporary bundle, or reference answer in the release tree.
- Compiled and adversarially exercised `evaluator/validate_artifacts.py` against a complete public-contract bundle and malformed, inconsistent, remote, path-escaping, SQL-unsafe, replay-artifact, and query/CSV-mismatch variants.
- Parsed the fallback CSV with `csv.DictReader`; verified effective-row coverage for every active product lacking a positive standard cost and no unresolved positive-exposure cost mapping.
- Checked final-artifact rubric arithmetic (`8 + 36 + 20 + 18 + 12 + 6 = 100`) and the exact shared code-rubric IDs/maxima (`15 + 20 + 15 + 15 + 10 + 10 + 10 + 5 = 100`).
- Recomputed the v3 predecessor SHA-256 inventory before and after repair to verify byte-for-byte preservation; repository status was also reviewed for sibling/Harbor changes.

## Resource feasibility

The combined released assets are small enough for the 4 GiB envelope, while each analytical database is large enough to defeat manual row-by-row shortcutting and naïve joins. The largest SQLite file is below 1 MiB and the representative standard-library/SQLite semantic queries completed in under one second in the construction environment. A competent built agent has ample time for schema inspection, several targeted queries, LLM-assisted planning/review, local artifact validation, and a small offline dashboard within 600 seconds.

The hard part is selecting and combining the right states and authorities, not scanning an oversized database or rendering an elaborate UI.

## Final inventory

- Root: `README.md`.
- `input/`: exactly four builder documents.
- `dev_cases/`: two inputs, two SQLite databases, three Markdown rule/policy assets.
- `test_cases/`: six inputs, six SQLite databases, seven Markdown rule/policy assets, one CSV fallback.
- `evaluator/`: final-artifact rubric, evaluator prompt, code rubric, and generic artifact validator.
- `meta/`: source analysis, reference baseline, and construction report.

## Known limitations and unresolved issues

- SQLite is the only database dialect; live warehouses, PostgreSQL-specific behavior, and production authentication are out of scope.
- Static validator checks cannot prove that every dashboard control changes the correct rendered state; the evaluator must interact with the page offline.
- The generic validator intentionally does not decide semantic answers, privacy correctness, or whether insufficiency is warranted; evaluator-side independent computation remains necessary.
- SQL keyword screening is defense in depth rather than a complete SQL parser; immutable read-only connections and actual re-execution are authoritative.
- Harness observation is still required for peak memory, network/provider attempts, forbidden-table access before output, database immutability, and filesystem boundaries.
- The original asset generator is intentionally absent and cannot be used to regenerate the repaired database bytes; release reproducibility therefore depends on preserving the frozen case assets and their hashes.
- Provider endpoint/model availability and the declared external environment prefix were not exercised during this closed-local artifact audit; the 600-second/4-GiB feasibility conclusion is based on local deterministic execution and asset scale.
- No unresolved correctness or contract issue remains in the released v4 artifacts after the recorded audit.
