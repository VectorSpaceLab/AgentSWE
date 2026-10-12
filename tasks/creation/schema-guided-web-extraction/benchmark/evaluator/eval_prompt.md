# Evaluator Instructions

Evaluate one Schema-Guided Web Extraction v4 case from final artifacts and observable runtime evidence only. You receive the active case's `input.md` and assets, candidate final bundle, any necessary parse/render/process observations, and the global final-artifact rubric. Do not inspect candidate source, prompts, private reasoning, other cases, evaluator oracle, or reference bundles.

## Procedure

1. Read the complete case request and enumerate every explicit schema, identity, entity-count, state/action, detail traversal, source-priority, timestamp/sequence, conflict, OCR, recovery, exclusion, authorization, and budget requirement.
2. Apply the validity gate first. Independently parse required artifacts. A parseable but incomplete bundle receives ordinary dimension scores; use zero only under the rubric's failure rules.
3. Parse the requested JSON Schema with a standards-compliant validator. Validate every record, identity uniqueness, exact requested entity coverage, and excluded/extra observations.
4. Reconstruct ground facts from the supplied runtime corpus rather than trusting candidate evidence. Complete browser states when supplied; inspect every relevant source for machine comparison. For raster-only fields, view the actual PNG pixels and verify that candidate evidence points to that image.
5. Compare records by stable identity. Check every field when the case has at most 100 records. Compute entity precision/recall and exact-or-normalized field correctness.
6. Verify identity, temporal, source-priority, amendment, tombstone, conflict, checksum, fallback, patch-order, and prohibited-target decisions against the request and source bodies. Values appearing later in a file or DOM are not automatically newer.
7. Resolve each field-evidence source ID, content hash, locator, method, access depth, record ID, and action ID. Confirm the named source and locator—not merely some source in the case—supports the exact submitted value. Conflict IDs are candidate-chosen; judge conflicts by record, output field, complete competing observations, selected value, deciding action, and reason. Reject generic `record=<id>; field=<name>` locators when they do not identify the supporting row/element/region.
8. Inspect `interaction_trace.json` in sequence. Confirm required action targets, canonical named states, distinct detail visits, source/recovery steps, OCR actions, screenshots, and cumulative counters. A structurally consistent trace is not proof that an action occurred: compare locators, state changes, controls, reached IDs, screenshots, and available process/browser/network observations. Cross-check `session_summary.json` reached IDs and states. Static cases must not invent browser events; browser cases must not replace state transitions with static assumptions.
9. Inspect `run_report.json` and available process/network evidence. Enforce closed-corpus zero public retrieval/search, case read/navigation/model/retry limits, prohibited target blocks, elapsed time, and truthful status.
10. Inspect every referenced screenshot visually. It must depict the actual linked page state or displayed raster source and agree with the case assets and trace event. Blank, repeated, decorative, noise/gradient, post-hoc generated placeholder, raw source-image copy presented as a browser capture, or unrelated screenshots receive no workflow-evidence credit even if a deterministic structural validator passed them.
11. Score all six dimensions independently. Cite concrete record IDs, JSON pointers, source paths/locators, trace action IDs/states, screenshots, and process observations for every deduction and every full-credit conclusion. A deterministic validator pass is only a consistency aid, never automatic rubric credit. Avoid duplicate deductions unless one defect causes distinct observable harms.

## Required output

```markdown
# Evaluation: <case id>

## Validity
- Status: valid | execution failure
- Evidence: <launch, time/memory, parse, authorization, and target-artifact facts>

## Metrics
- Entity precision: ...
- Entity recall: ...
- Correct fields / evaluated fields: ...
- Schema, identity, duplicate, and exclusion checks: ...
- Required/observed action counts and states: ...
- Authorization, budget, retry, and recovery checks: ...

## Dimension Scores
| Dimension | Score | Maximum | Evidence |
| --- | ---: | ---: | --- |
| Schema, identity, and entity coverage | ... | 20 | ... |
| Field correctness and normalization | ... | 25 | ... |
| Temporal, source-conflict, and recovery decisions | ... | 20 | ... |
| Field-level evidence integrity | ... | 15 | ... |
| Workflow state and action evidence | ... | 15 | ... |
| Operational reporting, safety, and budget truth | ... | 5 | ... |

## Total
- Raw: <integer>/100
- Workflow ceiling: none | 20
- Final: <integer>/100

## Major Errors
- <material defects, or `None observed`>

## Overall Assessment
<two to four evidence-based sentences>
```
