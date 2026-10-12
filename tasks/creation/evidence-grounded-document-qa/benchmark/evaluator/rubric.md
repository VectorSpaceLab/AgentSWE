# Final-Artifact Rubric — 100 points

Score only the final artifacts and observable parse/render/interaction results for the active case. Do not inspect or reward implementation, prompts, framework, tool choice, intermediate reasoning, or similarity to any reference project.

## Failure rules and hard-feature ceiling

- The agent cannot launch, the run exceeds 600 seconds, or prohibited external retrieval occurs: **0**.
- No `answer.md` or `claims_and_citations.json` is produced: **0**.
- Either target artifact is corrupt or cannot be parsed/read: **0**.
- A valid but weak answer receives an ordinary rubric score.
- A missing nonessential statistic in `run_report.json` does not by itself force zero.
- The source-native review package is central. If `review.html`, `review_manifest.json`, or `source_bundle.json` is missing; exact cited bytes/hashes fail; native locators are fabricated or non-replayable; claim/evidence linkage fails; or reciprocal offline API interaction is absent, cap the otherwise earned score at **20/100**. Apply the lowest relevant rule after dimension scoring and cite the concrete validator/browser evidence.

## 1. Factual, visual, and computational correctness — 28 points

Observable object: the claims, direct answers, calculations, table/figure interpretations, units, revision choices, and final decisions in `answer.md` and the claims JSON.

- Full credit: all material facts are reconstructed from the supplied packet; every multi-hop calculation uses the correct included rows/cells, formulas, units, priorities, time alignment, and requested rounding; visual counts/plot readings are correct; final decisions follow all controlling criteria.
- Middle performance: the main result is correct with one localized arithmetic, rounding, row-selection, qualifier, or visual-count error that does not reverse most conclusions.
- Low performance: several wrong inputs/results, entity or revision confusion, incorrect threshold logic, unsupported visual interpretation, or a materially wrong final decision.
- Severe errors: treating a superseded value as controlling, silently substituting a distractor entity, inventing a missing value, pricing an unsupported rider class, or using pixels where the case requires source numeric data.
- Do not penalize equivalent algebra, table ordering, or sensible extra precision that rounds to the same requested answer.

## 2. Evidence coverage, entailment, and conflict representation — 22 points

Observable object: whether every material claim and calculation input has directly entailing evidence, and whether superseded/contradictory evidence is represented with the correct relation.

- Full credit: all requested sub-questions, source inputs, intermediate results, visual observations, decision rules, and abstentions are covered; evidence actually entails each claim; controlling and discarded evidence are separated accurately.
- Middle performance: one minor input, explanation, or contradiction is weakly supported or omitted, while the major conclusions remain auditable.
- Low performance: sparse or mismatched citations, one citation reused for unrelated claims, missing calculation inputs, or conflict resolution asserted without showing both sides.
- Severe errors: quotes/observations do not occur in or follow from the cited source, fabricated evidence, or a material claim with no source support.
- Concise claims and multiple valid evidence decompositions are acceptable when the complete support graph remains clear.

## 3. Native locator and bundle integrity — 20 points

Observable object: source bytes/hashes, claim/evidence IDs, locator objects, and evaluator validation against active source content.

- Full credit: every cited source is bundled byte-for-byte once with the correct hash; IDs are unique and exactly linked; text byte ranges slice the claimed bytes; HTML IDs exist; CSV/XLSX/DOCX cells contain the observation; PDF rectangles overlap the correct extracted/rendered object; SVG IDs and geometry match; raster rectangles tightly identify the relevant region.
- Middle performance: most locators replay, with one slightly broad but still specific region or one nonmaterial manifest inconsistency.
- Low performance: generic file/page/sheet/image locators, several wrong cells/ranges, missing source hashes, duplicate IDs, or claims and manifest drifting apart.
- Severe errors: synthetic quote cards in place of original bytes, path-only bundles, whole-document targeting, byte/hash mismatch, fabricated PDF/SVG geometry, or a locator pointing at content that cannot support the evidence.
- Equivalent reproducible paragraph numbering or a geometrically tighter overlapping rectangle must not lose points.

## 4. Revision, uncertainty, and justified abstention — 12 points

Observable object: handling of entity identity, signed/current priority, contradictions, partially supported claims, and absent evidence.

- Full credit: all stated priority rules are applied and explained; distractors remain separate; uncertainty and confidence are calibrated; every intentionally missing sub-question is explicitly unanswerable for the right reason.
- Middle performance: recognizes the principal conflict and abstention but misses one nuance or overstates confidence slightly.
- Low performance: silently chooses a source, treats absence as proof of a value, or fails to distinguish unsupported root cause/service life/stability/chemistry/bond questions.
- Severe errors: hallucinating an answer for an explicitly absent measurement or allowing unsigned/older/different-entity material to control.
- Reasonable concise wording and either “unanswerable” or a clearly equivalent calibrated formulation are acceptable.

## 5. Offline review interaction and audit usability — 10 points

Observable object: `review.html` in a fresh offline browser plus `window.__reviewTestApi` state and visible behavior.

- Full credit: every manifest claim and evidence selection works both directions; complete reciprocal sets and genuinely visible claim/evidence/source targets highlight; status, confidence, relation, source hash, locator, and claim+evidence URL fragment update consistently; each source target exposes exact bytes whose hash matches the bundle; all cited modalities remain inspectable after the case directory is unavailable; no external or secondary file request occurs.
- Middle performance: the main reciprocal workflow works with one minor state/display defect or awkward navigation.
- Low performance: mostly static citation list, source shown without exact target, one-way navigation, state inconsistent with the manifest, or poor readability that materially impedes audit.
- Severe errors: remote or relative-file dependency, source content execution, hidden/zero-size test shims, only a cherry-picked pair working, API names present but calls fail, exact-byte download mismatch, or reconstructed snippets instead of source-native review.
- Visual styling, layout, color palette, and framework choice are not scored except where they affect observable audit use.

## 6. Artifact validity, clarity, and requirement compliance — 8 points

Observable object: parseability, required fields/files, directness, organization, and adherence to the active case's requested format and precision.

- Full credit: all required artifacts parse; Markdown directly answers every part with readable tables/calculations; JSON enums and arrays are valid; report status/paths/counters are honest; output is concise enough to use.
- Middle performance: minor schema, organization, formatting, or verbosity issue without material loss.
- Low performance: malformed nonfatal records, omitted requested section, unclear decision, missing units, or difficult-to-follow calculations.
- Severe errors: contradictory answer and claims JSON, false success status, or omitted major requested deliverable.
- Equivalent Markdown layouts, claim ordering, and wording are acceptable.
