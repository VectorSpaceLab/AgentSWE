# Reference Baseline

## Validation environment

The host initially had no `lean`, `lake`, or `elan` executable and no `python` command; construction scripts therefore used `python3`. On August 24, 2026, the official `lean-4.19.0-linux.tar.zst` release archive was downloaded only for construction validation. Its observed size was 343,842,845 bytes and SHA-256 was `6fe3ce97a58f44e2b3567d455b994eacec5bfe9ae7774f2a573444480ba813fe`. The extracted binaries reported:

- `Lean (version 4.19.0, x86_64-unknown-linux-gnu, commit 6caaee842e94, Release)`;
- `Lake version 5.0.0-6caaee8 (Lean version 4.19.0)`.

The temporary archive, extraction, reference copies, outputs, results, and all caches lived beneath `formal-theorem-proving-agent-v4/.construction/`. That entire directory is removed before handoff. Evaluators must provide the pinned toolchain externally; no toolchain binary or archive is part of the benchmark artifact.

## Reference-completed project checks

Construction-only copies were patched with one compliant solution per case. No reference proof or answer file is retained. From a clean `.lake` state, every reference project passed `lake build`; the false-case `Counterexample.lean` also passed `lake env lean Counterexample.lean`. Final observed clean-build resources on this host were:

| Case | Seconds | Peak RSS KiB |
|---|---:|---:|
| dev_001 | 0.64 | 302,948 |
| dev_002 | 0.54 | 299,648 |
| test_001 | 0.44 | 294,604 |
| test_002 | 0.54 | 295,472 |
| test_003 | 0.64 | 303,620 |
| test_004 | 0.64 | 310,372 |
| test_005 | 0.54 | 302,388 |
| test_006 | 0.54 | 343,104 |
| test_006 counterexample | 0.17 | 276,812 |

These measurements are feasibility evidence, not expected agent times or mandatory proof forms. They leave substantial room inside the 600-second/4-GiB execution envelope for repository indexing, model calls, iterative diagnostics, and a final clean build.

## Pristine-fixture and evaluator checks

Fresh construction-only copies of pristine `dev_001`, `dev_002`, and `test_001` through `test_005` failed `lake build` with the intended unsolved/failing target diagnostics. Pristine `test_006` passed because its false conjecture is represented as an unchanged `Prop` definition rather than a proof hole.

Generated unified patches and schema-1.1 reports for all eight construction-only reference solutions were passed through `evaluator/harness/evaluate_case.py`. All eight validity gates passed, including exact changed paths, source masking, proof-length and token checks, required references, dependency constraints, clean builds, separate counterexample compilation, module artifact compilation, and the evaluator-generated exact-negation import checker.

On August 24, 2026, this baseline was independently rechecked without retained construction answers. The official archive was downloaded to `/tmp`, and its observed size, SHA-256, Lean version, and Lake version exactly matched the values above. Fresh disposable completions were derived from the supplied statements and compiled for all eight cases; all eight generated patches/reports passed the repaired evaluator, including elaborated theorem-premise retention and counterexample semantic linkage. An adversarial `test_002` mutation that merely assigned each prerequisite to an unused local name and closed by definitional `rfl` compiled and passed the old textual checker; the repaired Lean-level dependency checker rejects that mutation after removing unused `let_fun` bindings.

## Limitations

No model-based reference agent was run, so this baseline does not estimate proof-search success rate or model-call consumption. The declared dedicated `/share/...` environment and credentials were absent on the audit host, so provider reachability and model-call accounting were not tested. Resource timing was measured on one host and is not a performance guarantee. The harness uses a deliberately conservative textual proof-boundary parser matched to the benchmark's public source style; it is not intended as a general Lean parser for arbitrary formatting. Lean 4.19.0 must be available externally at evaluation time.
