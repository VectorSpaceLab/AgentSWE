# Rewrite-constrained structural induction

Inspect all modules in the pinned project and complete only `Hidden.Sequence.Target.mirror_involutive` in `Main.lean`. A distractor namespace contains a theorem with the same short name as a useful law.

The proof must perform structural induction on `xs`, reference both `Hidden.Sequence.mirror_append` and `Hidden.Sequence.mirror_singleton`, and close the inductive step with controlled `rw`/`rfl` reasoning. Its body may contain at most ten noncomment nonblank lines. The final proof body must not contain the tactic tokens `simp`, `simpa`, `simp_all`, `aesop`, `omega`, `decide`, `native_decide`, `exact?`, or `apply?`.

Edit only `Main.lean`; preserve imports and declaration statements byte-for-byte and leave the downstream uses of `mirror_involutive` unchanged. Validate the entire project with a clean `lake build`, and report the exact target, changed path, command, and observed result. No `sorry`, `admit`, `unsafe`, new `axiom`, or `opaque` is permitted.
