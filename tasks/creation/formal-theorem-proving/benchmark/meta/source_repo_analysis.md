# Source Repository Analysis

## Pinned local source

- Source type: local benchmark repository used as design evidence.
- Read-only path: `${AGENTSWE_HOME}/benchmark/formal-theorem-proving-agent-v3`.
- Source file count at construction start: 62 regular files.
- Source byte count: 217,270 bytes (`du -sb`).
- Deterministic current source-tree digest: SHA-256 `aa27b29da764c5b3361bc1ee490ee829b0774a1d087deba307e8f074fee228aa`, computed over sorted repository-relative UTF-8 paths, a NUL separator, and each file's raw SHA-256 digest bytes.
- Parent checkout: branch `main`, commit `59594bd037733d9bfdd66459093e41ec9069d327` (`2026-08-05T15:30:54+08:00`). The v3 directory itself is untracked in that checkout, so the parent commit does not pin its contents.
- Latest observed v3 file mtime at audit: `2026-08-24T23:41:17+08:00` (`evaluator/code_rubric.md`).
- License/provenance: the benchmark fixtures are synthetic. The v3 metadata identified LeanCopilot as upstream capability inspiration and MIT-licensed at its then-pinned snapshot; v4 copies no LeanCopilot source or assets.

The earlier recorded digest `d68c9a...f0cb` was not reproducible from the current 62-file tree under the documented manifest description, so it is not retained as verified evidence. This audit read v3 only to establish provenance and cross-suite conventions, did not edit it, and records the reproducible current digest above. Because v3 is untracked, this metadata should be treated as a local-source snapshot description rather than a Git-pinned source commit.

## v3 system and retained capability

V3 evaluated an agent that accepts a Markdown request plus a small pinned Lean/Lake project and returns a unified patch with proof and run reports. Its useful observable contract was repository premise retrieval, compiler-checked proof repair, limited dependency handling, statement/import preservation, and an honest finite counterexample path. Runtime depended on Lean/Lake and optionally an LLM endpoint; case data itself used only core/Std and no external packages.

The main v3 weakness was fixture scale and interaction depth. Most projects consisted of one target theorem and one tiny helper module, so filename/name matching or single-shot proof generation could succeed without credible project indexing. The evaluator checked paths, imports, declaration lines, forbidden tokens, and builds, but it did not robustly mask target bodies, enforce per-target proof lengths/required references/dependency use, or type-link the reported counterexample declaration to the exact original target.

## v4 abstraction and boundaries

V4 retains the implementation-independent user value: build an agent that safely inspects an unfamiliar pinned Lean project and produces a minimal kernel-accepted patch or an exact-target refutation. It excludes source-repository prompts, architecture, editor integration, model-specific workflow, UI behavior, remote premise services, and network retrieval.

V4 differs materially from v3:

- all eight fixtures are synthetic multi-module projects with 51–91 noncomment Lean SLOC;
- hidden cases combine namespace/typeclass ambiguity, cross-file dependencies, constrained induction/rewriting, polymorphism, and specification selection rather than changing names or numerals;
- evaluator policy records constraints but no proof text or reference output;
- source integrity is checked by replacing only authorized proof bodies with masks and comparing every other byte;
- the unprovable case requires a separately compiled declaration imported by an evaluator checker at type `Not Hidden.Workflow.Target.conjecture`.

## Runtime, inputs, outputs, and security observations

The created agent receives one active `input.md` and its `assets/repository`, and writes only `solution.patch`, `proof_report.json`, `run_report.json`, and temporary work beneath the requested output directory. Lean projects pin `leanprover/lean4:v4.19.0`; Lake manifests contain no package dependency. The evaluator operates on an isolated copy and treats requests, Lean comments/strings, diagnostics, patches, and model output as untrusted.

No private data, credentials, copied upstream code, unstable API response, or copyrighted non-code corpus appears in the cases. Network use is prohibited during case execution. The only construction download was the official Lean 4.19.0 Linux release archive used for validation; it is not retained in the final benchmark.
