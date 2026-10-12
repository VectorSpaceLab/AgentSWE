# Construction Report

## Parameters and decisions

- Case slug/path: `formal-theorem-proving-agent-v4`.
- Source evidence: local read-only `formal-theorem-proving-agent-v3` plus its inherited upstream analysis.
- Task type: Create Agent benchmark.
- Uniform command: `python run_agent.py --input <input.md> --output <output_dir>`.
- Final artifacts: `solution.patch`, `proof_report.json`, and `run_report.json`.
- Case counts: exactly two public development cases and six hidden test cases.
- Toolchain: `leanprover/lean4:v4.19.0`; no external Lake package.
- Per-case budget: 600 seconds, 4 GiB, at most 180 GATEWAY text calls and 20 image-bearing calls; closed-corpus retrieval counts must be zero.

The requested 2+6 split overrides the skill default. Markdown remains the single case-input format. This audit confined all benchmark edits to v4 and did not modify v3, Harbor, or another sibling. The local v3 directory is untracked by the parent checkout; current source provenance is therefore recorded with a reproducible content digest plus the parent commit/branch rather than an unsupported claim that Git pins v3.

## Coverage and concrete difficulty improvements

- `dev_001` (77 noncomment Lean SLOC, three modules): teaches full-project discovery with a recursive datatype, a misleading same-domain `Shadow.mass`, and a mandatory fully qualified cross-module premise under a four-line proof cap.
- `dev_002` (54 SLOC, three modules): combines a realistic incomplete proof with a later dependent theorem, requires the repaired earlier declaration by name, and leaves valid downstream users that catch signature drift.
- `test_001` (72 SLOC, three modules): imports two namespaces that each define `Action`, `apply`, and `unit_act`; both targets require the correct fully qualified class theorem and instance resolution.
- `test_002` (51 SLOC, three modules): creates a four-target dependency chain spanning `Proof/Stage.lean` and `Main.lean`; each later proof must reference its repaired predecessor and both writable files must change exactly.
- `test_003` (71 SLOC, three modules): requires structural list induction with two named mirror lemmas, controlled rewrites, a strict ten-line cap, and explicit bans on simplifiers/automation in the target body.
- `test_004` (78 SLOC, three modules): preserves an arbitrary `Codec` instance while proving list and custom-tree round trips; a Nat-specific legacy law is a same-named distractor and both independent inductions are required.
- `test_005` (80 SLOC plus `SPEC.md`, three modules): asks the agent to select exactly two matching formal declarations from valid, false/stronger, and unrelated candidates, then use different required premises and explain the mapping.
- `test_006` (91 SLOC plus `SPEC.md`, three modules): presents a finite workflow transition conjecture that fails specifically at `Phase.running`; only `Counterexample.lean` may be added, and the reported declaration is imported and typechecked as the exact target negation.

The cases are not entity substitutions. Their declaration-structure hashes differ from both development cases and from one another, and no development/hidden Lean module is byte-identical.

## Harness and scoring design

The evaluator is configuration-driven. Case policy contains target names and public constraints but no proof text, expected patch, or witness proof. The harness:

1. validates exact JSON field sets, non-Boolean scalar types, finite nonnegative resource values, ordered artifact paths, validation-entry shapes, and closed-corpus provider counters;
2. rejects unsafe, binary, traversal, mode, rename, copy, symlink, or oversized patches;
3. applies the patch once to an isolated Git fixture and requires the exact public changed-path set;
4. masks only authorized target proof bodies and compares every other byte of baseline Lean source;
5. extracts final proof bodies to enforce noncomment line caps, required references, required/forbidden method tokens, and dependency order;
6. performs a comment-aware global trust-bypass scan;
7. deletes fixture caches and runs a clean pinned `lake build` with a minimal credential-free environment;
8. imports the compiled declarations into an evaluator-generated Lean checker, eliminates unused local `let_fun` bindings from proof terms, and verifies that required theorem premises remain as actual dependencies rather than dead textual mentions; and
9. for the false case, separately compiles the submitted file, checks premise retention in the reported refutation declaration, emits an importable `.olean`, and compiles an evaluator-generated theorem assigning that declaration to `Not Hidden.Workflow.Target.conjecture`.

The final-result rubric has five minimally overlapping dimensions weighted 35+20+20+15+10 = 100 and scores only final artifacts. `code_rubric.md` remains an independent implementation axis using the exact shared IDs/maxima: `interface_lifecycle` 15, `requirement_mechanism_coverage` 20, `analysis_evidence_integrity` 15, `safety_privacy_side_effects` 15, `recovery_honest_failure` 10, `testability_observability` 10, `maintainability_generalization` 10, and `resource_discipline` 5.

## Data provenance and licensing

All theorem statements, namespaces, datatypes, specifications, examples, distractors, and prose were authored synthetically for this benchmark. They use Lean core/Std only. No private data, personal data, secrets, remote API output, copied repository source, or external text corpus is embedded. Lean itself is used under its upstream license only as an external validation toolchain; no Lean binaries are retained.

## Consistency, leakage, and validation checks

Checks actually run during construction include:

- complete reading of the required skill and artifact specification plus an independent v4 file/case/source audit;
- independent current v3 inventory of 62 files and 217,270 bytes, parent checkout `main` at `59594bd...d327`, and reproducible content digest `aa27b29d...28aa`; the earlier `d68c9a...f0cb` claim was not reproducible and was removed;
- `python3 -m py_compile evaluator/harness/evaluate_case.py evaluator/harness/validate_benchmark.py`;
- `python3 -m unittest discover -s evaluator/harness/tests -v`;
- independent clean `lake build` for eight disposable audit completions under the official Lean 4.19.0 archive;
- `lake env lean Counterexample.lean` for the false-case reference;
- pristine-build outcome checks for all eight cases;
- end-to-end evaluator validity runs for all eight independently generated reference patches;
- negative mutation proving that a dead prerequisite mention plus definitional `rfl` compiled and passed the old harness, followed by a Lean-level regression test requiring the fixed checker to reject it;
- `python3 evaluator/harness/validate_benchmark.py --benchmark-root .` after final runtime hashes were recorded;
- final searches for `.construction`, `__pycache__`, `.lake`, `.olean`, `.ilean`, object, archive, and downloaded-toolchain artifacts.

`meta/runtime_asset_hashes.sha256` pins every runtime case input and asset. The validator checks those hashes, exact case counts and shape, 50–250 Lean SLOC bounds, at least two Lean modules per case, empty Lake dependency lists, case-tree hash uniqueness, declaration-structure divergence, absence of public/hidden identical Lean hashes, absence of hidden fully qualified target names in builder/public development material, both rubric totals, exact shared code-rubric IDs/maxima in prose and JSON, resource/interface contract details, policy phrases/method tokens in each case input, absence of per-case oracle-like files, and absence of caches/toolchain/construction artifacts.

## Feasibility and limitations

Reference builds used at most 343,104 KiB peak RSS and 0.64 seconds for a clean project build on the construction host; the separate counterexample check used 276,812 KiB and 0.17 seconds. The dominant 600-second cost for a competent built agent is therefore proof search/model interaction, not kernel checking. Projects remain small enough to index fully while large and varied enough to defeat direct one-line memorization.

Known limitations: the evaluator's proof-body boundary extraction intentionally assumes the top-level declaration formatting used by the fixtures; it is stricter than Lean's full grammar. The elaborated dependency checker eliminates direct unused `let_fun` bindings and confirms retained theorem constants, but it is not a general proof-relevance theorem and cannot rule out every adversarially opaque detour. The benchmark tests finite counterexample handling, not general independence proofs. No model reference run was performed. The declared `/share/...` prefix and credentials were unavailable on this audit host, so GATEWAY invocation was not exercised; external evaluation must provide that environment. Lean/Lake 4.19.0 feasibility was independently verified from the official pinned archive.

## Final inventory

The final tree contains one root README; exactly four builder inputs; 14 public development-case files; 44 hidden test-case files; three evaluator prose/rubric files; six evaluator harness/test files; and five metadata files, for 77 regular files total. Construction/redesign notes are consolidated into this report rather than published as an extra root document. There are exactly two public case directories and six hidden case directories. Cases contain only `input.md` and runtime `assets/`; no case contains an oracle, solution, expected output, case rubric, or evaluator policy.
