# Evaluator Instructions

You receive only the active case's `input.md` and `assets/`, the created agent's final output directory, the evaluator-owned harness result, any Lean parse/compile output needed to interpret it, and the global final-artifact rubric. Do not inspect or infer quality from the submitted agent implementation; code quality is scored separately by `code_rubric.md`.

First determine final-artifact validity. Parse the three required artifacts, run `evaluate_case.py` on an isolated pristine repository copy, and inspect its concrete evidence for patch applicability, exact changed-path scope, source/import/declaration integrity, proof constraints, exact report typing, required references, elaborated-proof dependency retention after unused bindings are removed, forbidden constructs, clean pinned `lake build`, and—when requested—counterexample compilation plus generated premise-retention and exact-negation type links. If the harness validity gate fails, assign 0 and report the specific gate evidence. Do not rescue an invalid artifact by reading implementation code or guessing intended edits.

When the gate passes, score all five rubric dimensions independently. For every deduction, cite concrete final-artifact evidence by repository file and declaration, patch hunk, proof-body line count/token/reference, JSON field, or evaluator command and exit code. Distinguish a required constraint from a stylistic preference. Accept any kernel-valid proof strategy satisfying the active input; do not require a hidden reference proof.

Return:

1. `validity_gate` and, if false, the exact failure evidence;
2. each dimension name, integer score, maximum, and evidence for every deduction;
3. `total_score` out of 100;
4. `major_errors` as a concise list; and
5. a short overall assessment focused on the final artifact.

Keep each hidden case's result separate and aggregate the six hidden totals by arithmetic mean only after all per-case evaluations are complete.
