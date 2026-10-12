# Polymorphic typeclass reasoning across two inductive containers

Inspect the complete pinned project. Complete `Hidden.Codec.Target.list_roundTrip` and `Hidden.Codec.Target.tree_roundTrip` in `Main.lean` while retaining the arbitrary type parameter and inferred `Hidden.Codec.Codec` instance. A legacy namespace contains a same-named but Nat-specific `roundTrip_eq` distractor.

Both proofs must use structural induction and reference `Hidden.Codec.Laws.roundTrip_eq`; the tree proof must recurse over both branch hypotheses rather than reducing to the concrete sample. Each proof body may contain at most twelve noncomment nonblank lines. The final bodies must not use `aesop`, `omega`, `native_decide`, `simp_all`, or tactic-suggestion placeholders.

Edit only `Main.lean`, preserve imports and declaration statements byte-for-byte, and leave the valid concrete downstream theorems unchanged. Run a clean `lake build`. Reports must list both fully qualified targets and the real validation evidence. Do not use `sorry`, `admit`, `unsafe`, new `axiom`, or `opaque`.
