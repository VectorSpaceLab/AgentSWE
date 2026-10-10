# Cross-file dependency chain

Repair every marked target in the pinned project under `assets/repository`. The declarations form a required chain across `Proof/Stage.lean` and `Main.lean`; solve and report them in source order:

1. `Hidden.Dependency.clear_pad` in `Proof/Stage.lean`, explicitly referencing `Hidden.Dependency.pad`;
2. `Hidden.Dependency.one_pass` in `Main.lean`, referencing `Hidden.Dependency.clear_pad`;
3. `Hidden.Dependency.two_pass`, referencing the repaired `Hidden.Dependency.one_pass`; and
4. `Hidden.Dependency.two_pass_cost`, referencing `Hidden.Dependency.two_pass`.

Only `Proof/Stage.lean` and `Main.lean` may change. Preserve all imports and declaration statements byte-for-byte. Each target proof may use at most seven noncomment nonblank lines. Do not alter the already valid declarations between targets, duplicate a target, specialize to sample numerals, or bypass the dependency chain by proving the later results solely from unfolded definitions.

Run a clean `lake build`. The patch must use no `sorry`, `admit`, `unsafe`, new `axiom`/`opaque`, `aesop`, `native_decide`, or suggestion placeholders. Reports must list all four targets in source/dependency order and both actual changed files.
