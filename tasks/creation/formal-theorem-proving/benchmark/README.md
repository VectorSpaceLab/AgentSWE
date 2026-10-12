# Formal Theorem Proving Agent v4

This benchmark measures whether a created agent can repair and complete Lean 4 proofs in unfamiliar, pinned, multi-module Lake projects while preserving kernel-visible integrity. The projects are deliberately large enough to require repository discovery rather than single-buffer completion: every case contains roughly 50–250 noncomment Lean source lines, useful and distracting declarations, and interacting namespace, typeclass, dependency, induction, rewriting, or specification constraints.

Give the agent builder exactly the four documents in `input/` as its task manual. The builder must produce one executable `run_agent.py`; its internal architecture is unrestricted. Invoke it uniformly with:

```bash
python run_agent.py --input <case>/input.md --output <run-output>
```

Each case contains a read-only repository at `assets/repository/`. A successful run writes `solution.patch`, `proof_report.json`, and `run_report.json` beneath the designated output directory. The evaluator copies the pristine repository, applies the patch once, enforces changed-path and source-integrity rules, checks exact report types plus public proof-length/tactic/premise/dependency constraints, removes stale Lake caches, and compiles with the pinned Lean toolchain under a credential-free subprocess environment. Required theorem premises are checked again against elaborated proof terms after unused local bindings are eliminated, so a dead textual mention cannot satisfy a dependency chain. The false-conjecture case additionally compiles the submitted counterexample and generated semantic checkers for required-premise retention and exact type `Not <the original target>`.

Only `dev_cases/` may be provided during agent development. Keep `test_cases/` and evaluator-owned case policy isolated. Public development cases teach the interface, repository traversal, compiler-guided repair, and dependency reporting; they do not expose the hidden theorem structures. Hidden scores are the arithmetic mean of six case scores. A validity-gate failure scores zero for that case; otherwise use `evaluator/rubric.md` and cite final-artifact evidence for every deduction.

Run the benchmark self-checks with:

```bash
python3 evaluator/harness/validate_benchmark.py --benchmark-root .
python3 -m unittest discover -s evaluator/harness/tests -v
```

For a completed agent output, run:

```bash
python3 evaluator/harness/evaluate_case.py \
  --case-dir <case> \
  --output-dir <run-output> \
  --result <result.json>
```

Provide the active case input and assets, generated artifacts, harness result, and global rubric to the evaluator prompt. Do not expose evaluator policy or hidden cases to the builder. All fixture statements and prose are synthetic and use only Lean core/Std. Candidate model use is limited to the documented GATEWAY endpoint; theorem search, browsing, retrieval, and package downloads are prohibited. The per-case envelope is 600 seconds and 4 GiB.
