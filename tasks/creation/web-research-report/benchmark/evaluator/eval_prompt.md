# Evaluator Instruction

You are evaluating one execution of a created Deep Evidence Research Agent.

You receive the active case's `input.md` and assets, the created agent's `report.md`, `sources.json`, `evidence_graph.json`, and `run_report.json` if produced, harness parse/render/cross-reference/recomputation observations, the global `rubric.md`, and isolated evaluator-side Search/Scrape resources.

Evaluate only observable final artifacts and publicly checkable evidence. Do not inspect the submission implementation, prompts, framework, agent loop, internal tool calls, diagnostics, hidden reasoning, or private chain-of-thought. Do not compare with a reference answer. Different conclusions are acceptable when the report and graph support them.

## Evaluation Procedure

1. Read the complete case request and build a checklist of its question, cutoff date, entities, source/body/PDF minima, required analyses, calculations, format, exclusions, and decision form.
2. Inspect validity first. Apply a zero-score rule exactly when triggered. If zero applies, identify the rule and concrete evidence, assign `0/100`, and stop dimension scoring.
3. Parse all JSON. Cross-reference report `[C#]` IDs, claims, sources, passages, contradictions, relations, calculations, and passage ownership in both directions. Confirm enum values and inspect any harness validation results.
4. Use your own Search/Scrape tools for a risk-based verification sample. At minimum, independently check every claim central to identity, current/superseded status, the recommendation, a material conflict, and each headline calculation, plus at least two other material claims. Retrieve the cited body where possible; do not judge truth from snippets.
5. Verify quoted passages and locators against actual pages, PDFs, repository files, releases, or structured records. Check whether the cited body was available at the claimed depth. A quote that exists elsewhere but not at the cited locator is a locator error.
6. Search for a plausible authoritative contradiction or newer/older controlling source when the report claims consensus, current policy, earliest provenance, no conflict, or complete version scope. Check whether multiple alleged corroborators cite, copy, syndicate, or share one underlying dataset.
7. For quantitative cases, independently extract the consequential inputs and recompute formulas, conversions, rounding intervals, and sensitivity endpoints. For nonquantitative cases, inspect any numerical effect, date interval, benchmark, or version comparison that influences the conclusion.
8. Score all six dimensions independently. Avoid double-deducting one defect unless it has separately observable consequences; explain each consequence.
9. Cite concrete final-artifact and external-verification evidence for every deduction. Locate artifacts by heading, paragraph opening, table/row, claim ID, passage ID, source ID and JSON field, calculation ID, or harness observation. For evaluator research, provide the checked URL/document title and access result.
10. Accept reasonable differences in source selection, organization, interpretation, and conclusion. Do not penalize a report for disagreeing with your initial view when its inspected evidence, graph, and qualifications support the decision.

## Required Output

```markdown
# Evaluation

## Validity
- Status: valid | zero-score failure
- Evidence: ...

## Independent Verification
- Claims checked: ...
- Sources/access depth checked: ...
- Contradictions or dependence checked: ...
- Calculations recomputed: ...

## Dimension Scores
| Dimension | Score | Maximum | Evidence and deductions |
|---|---:|---:|---|
| Request Compliance and Decision Utility | ... | 12 | ... |
| Factual, Entity, Temporal, and Quantitative Fidelity | ... | 22 | ... |
| Retrieval Depth, Source Selection, and Independent Verification | ... | 18 | ... |
| Evidence-Graph Auditability, Conflict, and Provenance | ... | 24 | ... |
| Analysis, Conflict Resolution, and Research Integrity | ... | 14 | ... |
| Reproducibility, Communication, and Artifact Consistency | ... | 10 | ... |

## Total
**.../100**

## Major Errors
- ...

## Overall Assessment
...
```

Use integer scores. For a valid artifact, include evidence for every dimension, including full-credit dimensions. If there are no major errors, write `None observed.` Keep the assessment concise and specific to the current decision.
