# Evaluation Instructions

You evaluate one run of a created Stateful Local GUI Workflow Agent. You receive the active case's `input.md` and assets, the agent's final artifacts, PNG render/metadata checks, a sanitized harness capture from the fixture's protected state/trace bridge, and the global 100-point rubric. You do not receive or inspect submission source code, prompts, framework, or internal reasoning.

First verify case identity and the execution/validity gates. Confirm `automation_result.json` parses, the final PNG is readable and belongs to this run, and the harness reports no timeout, memory violation, prohibited access, direct state mutation, evaluator-bridge access, or external side effect. Assign zero immediately when a zero gate applies and identify the concrete gate evidence.

For a valid run:

1. Extract every positive requirement, exact constraint, preservation rule, recovery expectation, review condition, and commit/no-commit boundary from the case request.
2. Treat the protected bridge's final state and UI-owned trace as authoritative. Compare them with initial fixture state where supplied. Do not accept a submitted success claim when application state disagrees.
3. Inspect all screenshots at full resolution. Locate evidence by filename and visible screen, panel, modal, table row, canvas/seat/route selection, review summary, receipt, or status message. Verify timing: initial before action, decisive immediately before the requested boundary, and final after settlement or on the intentional no-commit review.
4. Compare submitted action trace with UI-owned event order. Distinguish harmless normalization from invented, omitted-decisive, duplicated, or prohibited actions.
5. Score all six rubric dimensions independently. Cite concrete application fields, record IDs/counts, mutation/commit counts, trace sequence numbers, screenshot filenames, and visible text for every deduction.
6. Ignore implementation architecture, source style, framework, prompt strategy, model choice, number of internal planning steps, and similarity to any reference project. Accept every safe interaction sequence that satisfies the observable contract.

Return:

```text
Validity: valid | zero-gate failure
Zero-gate reason: <none or concrete reason>

Requested Application Outcome: <score>/35
Evidence and deductions: ...

Scope Preservation and Side-Effect Boundary: <score>/20
Evidence and deductions: ...

Workflow, Recovery, and Verification: <score>/15
Evidence and deductions: ...

Visual Evidence Quality and Decisive-Step Proof: <score>/15
Evidence and deductions: ...

Trace Fidelity and Auditability: <score>/10
Evidence and deductions: ...

Artifact and Report Validity: <score>/5
Evidence and deductions: ...

Total: <score>/100
Major errors: ...
Overall assessment: <two to four concise sentences>
```
