# Final-Artifact Rubric

Total: **100 points**.

Score only the current case's `report.md`, `sources.json`, `evidence_graph.json`, `run_report.json`, case input/assets, harness observations, and evaluator-retrieved public evidence. Do not inspect or score implementation code, prompts, architecture, model choice, framework, internal tool sequence, private reasoning, diagnostics, or similarity to any reference answer.

## Execution and Validity Gates

Assign **0/100 for the case** when any of these conditions is true:

- The agent cannot launch through the required command.
- The run exceeds 600 seconds, exceeds 4 GiB, or violates the declared credential, network, API, environment, or filesystem policy.
- The command exits nonzero without complete, valid target artifacts.
- `report.md` is missing, empty, not UTF-8, not Markdown, or is not the requested research report.
- `sources.json` is missing, not parseable JSON, or lacks a parseable nonempty `sources` array.
- `evidence_graph.json` is missing, not parseable JSON, lacks the four required arrays, or is so structurally invalid that claims cannot be connected to sources and passages.
- The target is a different artifact type, a tool transcript, or an unrendered data dump rather than the requested report and evidence records.

A valid but poor artifact receives ordinary dimension scores. A missing or malformed `run_report.json` does not by itself force zero; score it under compliance and consistency. A few broken links, missing optional diagnostics, or a poor conclusion also do not automatically force zero.

## 1. Request Compliance and Decision Utility - 12 points

**Evaluate:** Direct answer, audience, cutoff date, requested structure, minimum evidence mix and depth, explicit exclusions, tables/calculations, and requested decision form.

- **Full credit (11-12):** Directly answers the actual decision; fulfills all material deliverables and evidence-depth conditions; uses the specified date, audience, scope, and decision vocabulary; required tables and next steps are easy to locate.
- **Middle (6-10):** The intended report is useful but one material requirement or several minor constraints are incomplete; source mix or body count is slightly short without changing the core answer.
- **Low (0-5):** Wrong question/entity/date, multiple required components absent, evidence minimum substantially missed, or the requested decision is replaced by generic advice.
- **Typical severe errors:** Never resolving the initial clue; omitting a required candidate or CVE; using post-cutoff evidence as if available at the cutoff; skipping the rounding bounds; failing the explicit body/PDF minimum; or giving legal certainty where the case asks for operational analysis.
- **Do not penalize:** Different headings, ordering, prose style, or a different supportable conclusion when every stated requirement remains clear.

## 2. Factual, Entity, Temporal, and Quantitative Fidelity - 22 points

**Evaluate:** Accuracy of identities, versions, dates, policy status, clauses, source values, units, definitions, quotations, calculations, and qualifications.

- **Full credit (20-22):** Material facts match inspected evidence; entity and artifact boundaries are explicit; publication/effective/update dates are not conflated; superseded material is labeled; numbers and conversions reproduce; reported uncertainty means what the report says it means.
- **Middle (11-19):** Mostly accurate with limited transcription, version, date, unit, rounding, or qualification errors that do not reverse the main decision.
- **Low (0-10):** Material identity error, temporal mixing, invented value, wrong denominator, false quotation, unsupported version boundary, or arithmetic that changes the result.
- **Typical severe errors:** Treating Llama code and weights as one licensed object; assigning a CVE to the wrong platform; saying a superseded NIH page controls in 2026; converting withdrawals to consumption; selecting 1945 without support; fabricating a PDF page number; or reporting a normalized metric that cannot be recomputed.
- **Do not penalize:** Accurate derived results, clearly labeled estimates, consistent reasonable display rounding, or correction of an inaccurate premise in the request.

## 3. Retrieval Depth, Source Selection, and Independent Verification - 18 points

**Evaluate:** Whether material claims rely on inspected bodies, the requested source classes are represented, search/refinement and fallback produce adequate coverage, and corroboration is genuinely independent.

- **Full credit (17-18):** Meets the body/PDF/source-publisher requirements; material claims use authoritative or primary evidence appropriate to them; relevant independent evidence tests interpretations; access depth is truthful; blocked or dynamic sources trigger a useful alternate route or browser attempt and honest disclosure.
- **Middle (9-16):** Adequate research with one weak source class, limited contrary searching, an underexplored alternate path, or a few claims relying on shallow/partial material despite stronger inspected evidence elsewhere.
- **Low (0-8):** Snippets carry material claims, source mix is promotional or repetitive, body minimum is far short, no recovery follows a blocked preferred source, or copied pages are counted as independent confirmation.
- **Mandatory severe deduction:** A material claim supported only by a `search_snippet` receives no credit as verified evidence in this and the graph dimension. Systematic snippet-only research generally scores 0-3 here.
- **Typical severe errors:** Citing a search result as a policy; relying only on project READMEs for comparative performance; treating four articles copied from one announcement as corroboration; or claiming full-page review after a block page.
- **Do not penalize:** A relevant source unavailable despite proportionate alternate-path effort, a smaller source count above the case minimum, or use of a different authoritative URL discovered through a valid chain.

## 4. Evidence-Graph Auditability, Conflict, and Provenance - 24 points

**Evaluate:** Report-to-claim citations, atomic claim quality, exact claim-to-passage links, real locators/quotes, contradictions, source relations, independence groups, calculation links, and bidirectional artifact consistency.

- **Full credit (22-24):** Nearly every material claim has a resolving `[C#]`; claim text matches report use; support and contradiction links resolve to exact inspected passages; sources own their cited passages; material conflicts and dependencies are represented; calculations link to exact inputs; the graph permits efficient independent audit.
- **Middle (12-21):** Core decisions are traceable, but some material claims are bundled, citations are remote, a few links/locations mismatch, contradiction or dependence coverage is incomplete, or source metadata has localized errors.
- **Low (0-11):** Citations are decorative; chains commonly break; passages are vague paraphrases; material contradictions are omitted; dependence is hidden; calculations lack source-linked inputs; or locators cannot be verified.
- **Mandatory severe deduction:** A fabricated quotation, source locator, PDF page, repository path, or passage materially supporting the decision generally caps this dimension at 5 and must also affect fidelity/integrity where applicable.
- **Typical severe errors:** Claiming two sources support a fact when both passages derive from one third source; omitting the earlier contrary policy; attaching a quote to the wrong document; listing contradictions only in prose but not the graph; or using a passage from a `search_snippet` source.
- **Do not penalize:** Different valid ID ordering, short exact excerpts, a supportable choice to split or combine closely related claims, or an empty `contradicts` array for a genuinely uncontested claim.

## 5. Analysis, Conflict Resolution, and Research Integrity - 14 points

**Evaluate:** Cross-source reasoning, treatment of alternatives and negative evidence, calibrated resolution of conflicts, independence-aware provenance, causal and legal restraint, access honesty, and recommendation logic.

- **Full credit (13-14):** Synthesizes rather than serially summarizes; explains why sources conflict; tests plausible alternatives; distinguishes evidence, inference, and unknowns; preserves unresolved disagreement; and ties the recommendation and conditions to explicit evidence and risk.
- **Middle (7-12):** Generally defensible but one important conflict, source limitation, transferability issue, or alternative explanation receives shallow treatment; recommendation conditions are only partly operational.
- **Low (0-6):** Cherry-picks one source, converts ambiguity to certainty, hides contrary evidence, confuses correlation with causation, treats a score or benchmark as universally comparable, or recommends action unrelated to findings.
- **Typical severe errors:** Declaring `first` from a later museum caption; equating lower pavement surface temperature with improved pedestrian comfort; offering a legal holding; treating lack of evidence as absence; or ignoring a later correction within the cutoff.
- **Do not penalize:** A cautious or inconclusive result, a different evidence-weighting judgment, or explicit referral to counsel/testing when the final conclusion remains useful and grounded.

## 6. Reproducibility, Communication, and Artifact Consistency - 10 points

**Evaluate:** Calculation transparency, report organization, table usability, prose clarity, terminology, source/claim navigation, and consistency with runtime counts/status.

- **Full credit (9-10):** Formulas, units, conversions, inputs, intermediate precision, ranges, and sensitivity choices are transparent where relevant; the report is concise and scannable; tables render cleanly; terminology fits the audience; JSON counts and artifact status agree with observable outputs.
- **Middle (5-8):** Usable overall but has a dense section, awkward table, unexplained term, missing intermediate, weak bound explanation, or minor run-report/count inconsistency.
- **Low (0-4):** Arithmetic cannot be reproduced, tables obscure provenance or units, report is a source dump, Markdown is hard to navigate, or runtime reporting is absent/fabricated and materially inconsistent.
- **Typical severe errors:** No formula for a headline metric; interval endpoints cannot be recreated; raw tool output replaces synthesis; or `run_report.json` asserts full-page counts plainly contradicted by source metadata.
- **Do not penalize:** Plain styling, absence of graphics, compact appendices, or a nonquantitative case having an empty calculations array when the report makes no derived quantitative claim.
