# Repository discovery and qualified premise use

Work on the pinned Lean project in `assets/repository`. Read all Lean modules before editing: the project contains a recursive warehouse datatype, several useful laws, and a deliberately misleading `Shadow.mass` declaration.

Complete `Training.Warehouse.Shipping.shipped_mass` in `Main.lean`. You may edit only `Main.lean`; preserve every import and declaration statement byte-for-byte. The proof must reference the existing fully qualified premise `Training.Warehouse.Batch.mass_merge`, may contain at most four noncomment nonblank lines after `:= by`, and must not duplicate any definition or theorem.

The final patch must pass a clean `lake build` under the pinned toolchain. Do not use `sorry`, `admit`, `unsafe`, `axiom`, `opaque`, `native_decide`, `aesop`, or tactic-suggestion placeholders. Write `solution.patch`, `proof_report.json`, and `run_report.json` using the shared contract, list the one target, and report the exact compiler command and observed exit code.
