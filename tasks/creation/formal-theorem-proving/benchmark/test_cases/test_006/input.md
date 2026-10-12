# False finite conjecture with semantic target linkage

Inspect `SPEC.md` and all Lean modules in the pinned project. `Hidden.Workflow.Target.conjecture` is genuinely false. Do not edit `Main.lean`, any `Proof/` module, any import, or the conjecture declaration, and do not claim `proved`.

Add exactly one file, `Counterexample.lean`, importing `Main`. In it, prove a named theorem whose type is exactly `¬ Hidden.Workflow.Target.conjecture`. The proof must give the concrete witness `Hidden.Workflow.Phase.running` and reference the existing premises `Hidden.Workflow.Facts.running_eligible` and `Hidden.Workflow.Facts.tick_running_not_eligible`; do not merely prove an unrelated Boolean computation or restate a different proposition. Keep the refutation proof body to at most ten noncomment nonblank lines.

Report `status: "unprovable"`, `targets: ["Hidden.Workflow.Target.conjecture"]`, and a `counterexample` object naming that exact target, witness, file, and fully qualified refutation declaration. Validate with both `lake build` and `lake env lean Counterexample.lean`. The evaluator will import the submitted file and typecheck the reported declaration specifically as `Not Hidden.Workflow.Target.conjecture`.

No other path may change. Do not use `sorry`, `admit`, `unsafe`, new `axiom`/`opaque`, `native_decide`, or tactic-suggestion placeholders.
