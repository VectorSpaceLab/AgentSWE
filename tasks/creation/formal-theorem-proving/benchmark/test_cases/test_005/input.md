# Select formal targets from a natural-language specification

Read `SPEC.md` and every Lean module in the pinned repository. Complete exactly the two theorem declarations in `Main.lean` whose formal statements match the two numbered acceptance rules. Leave all other theorem/definition declarations, including the deliberately mismatching proposition, untouched.

For the normalization rule, the completed proof must reference `Hidden.Pipeline.Facts.measure_normalize`. For the empty-append checksum rule, it must reference `Hidden.Pipeline.Facts.checksum_append_empty`. Each selected proof may contain at most five noncomment nonblank lines. The `targets` report field must name the two selected declarations in source order and `diagnosis` must briefly identify which numbered rule each target matches.

Edit only `Main.lean`; preserve its imports and all declaration statements byte-for-byte. Run a clean `lake build`. Do not use `sorry`, `admit`, `unsafe`, new `axiom`/`opaque`, `aesop`, `native_decide`, or tactic-suggestion placeholders, and do not strengthen or weaken any candidate statement.
