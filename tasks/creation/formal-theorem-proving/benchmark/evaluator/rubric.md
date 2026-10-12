# Final Artifact Rubric (100 points)

Score only the created agent's final `solution.patch`, `proof_report.json`, and `run_report.json`, the patched case repository, and evaluator-observed parse/compile results. Do not score implementation architecture, source code, prompts, framework choice, model/tool selection, hidden reasoning, intermediate candidates, or similarity to any reference implementation.

Apply the evaluator harness before ordinary scoring. Assign **0 for the entire case** if the agent cannot launch, exceeds the case timeout, produces no target artifacts, emits a corrupt/unparsable or unsafe patch, changes unauthorized paths, changes imports/declaration statements/non-target source, uses a prohibited trust bypass, fails the required clean Lean build, omits a requested target, or fails the exact semantic counterexample-link check. A valid but weak artifact receives an ordinary rubric score. A missing nonessential explanation or harmless formatting difference does not automatically force zero.

## 1. Kernel validity and complete target closure — 35 points

**Observable object:** the isolated patched repository, all requested declarations, the counterexample path when applicable, and evaluator-owned Lean/Lake output.

- **Full credit (32–35):** every requested proof is kernel accepted under Lean 4.19.0 in one clean project build; the false case also compiles the submitted file and evaluator-generated exact-negation checker; no target is duplicated, shadowed, or accidentally accepted through stale artifacts.
- **Middle (18–31):** the validity gate passes, but evidence shows a minor fragility such as redundant local machinery, avoidable elaboration dependence, or a nonsemantic warning that does not threaten the requested declarations.
- **Low (1–17):** reserved for a still-valid artifact with materially incomplete validation evidence or unusually brittle proof construction; any actual unsolved target or failed required compile is a case-level zero under the gate.
- **Typical severe errors:** `sorry`/`admit`, an added axiom, unsafe/opaque bypass, theorem replacement, stale-cache-only success, missing dependent target, or a counterexample declaration not having the exact target-negation type.
- **Do not penalize:** equivalent proof terms, different accepted tactics, explicit versus inferred arguments, harmless qualification, or formatting within the public line limits.

## 2. Explicit constraint and source-integrity compliance — 20 points

**Observable object:** the applied diff, changed paths, masked-source integrity comparison, proof-body metrics, imports, and forbidden-token scans.

- **Full credit (18–20):** exactly the authorized paths change; existing imports, declaration headers, and non-target source remain byte-identical; all proof-length, forbidden-tactic, allowed-file, and output-format requirements hold.
- **Middle (10–17):** all validity rules hold but the patch includes small unnecessary edits inside an authorized target body or minimally untidy diff context that does not alter semantics or violate a stated limit.
- **Low (1–9):** a valid artifact narrowly satisfies the gate but is needlessly broad inside replaceable proof bodies or reports constraints imprecisely.
- **Typical severe errors:** out-of-scope files, statement/import edits, changes to `lean-toolchain`/Lake files, edits to valid distractor declarations, mode/symlink/binary patches, exceeded proof limits, or forbidden final tactics.
- **Do not penalize:** whitespace or comments changed wholly inside an authorized target proof body, provided line counting and all explicit constraints still pass.

## 3. Premise selection, dependency use, and project fidelity — 20 points

**Observable object:** final target proof bodies, evaluator-generated elaborated-proof dependency checks, and their references to the publicly required project declarations.

- **Full credit (18–20):** every proof retains the required fully qualified theorem premise in its elaborated proof after unused local bindings are eliminated; ambiguous namespaces/typeclasses resolve correctly; dependent targets genuinely use the repaired prerequisites in the requested order; no supplied concept is copied into a duplicate answer declaration.
- **Middle (10–17):** required premises and dependencies are present, but the proof also unfolds or repeats more project detail than necessary, making it somewhat fragile without violating the case input.
- **Low (1–9):** valid proof bodies use the required names only superficially or rely heavily on accidental simplification, unnecessary local restatements, or avoidable concrete-instance reasoning.
- **Typical severe errors:** selecting the wrong same-named lemma, bypassing a required dependency with an unrelated proof, omitting a requested target, or adding a duplicate helper whose statement is the answer.
- **Do not penalize:** additional legitimate supporting lemmas already present in the repository or a different valid order of independent tactic steps.

## 4. Generalization, induction, and semantic specification fidelity — 15 points

**Observable object:** preserved theorem signatures, inductive proof structure, typeclass parameters, and correspondence between `SPEC.md` and selected formal targets.

- **Full credit (13–15):** polymorphic/typeclass targets remain fully general; required structural inductions recurse over every constructor; rewrite-constrained proofs obey the requested method; specification-driven cases select exactly the matching declarations and leave stronger, false, or unrelated candidates untouched.
- **Middle (7–12):** the full statements compile and the required method is present, but the proof contains avoidable instance-specific detours or redundant case work.
- **Low (1–6):** the final result is valid only through fragile elaboration or concrete examples that obscure, but do not actually narrow, the preserved theorem statement.
- **Typical severe errors:** specializing an arbitrary type to Nat/Bool, replacing induction with a forbidden solver, selecting a mismatching candidate, or changing a proposition to fit a proof.
- **Do not penalize:** different constructor names introduced by Lean's induction tactic, explicit universe/type arguments, or alternative controlled rewrite sequences.

## 5. Honest reports and semantic unprovable handling — 10 points

**Observable object:** both JSON reports compared with the applied patch and evaluator-observed commands/results.

- **Full credit (9–10):** schema/status/targets/files/artifacts are exact; diagnostics are concise and evidence based; successful commands and exit codes match reality; provider counts are typed and closed-corpus retrieval counts are zero; the false case reports the exact target, concrete witness, file, and declaration accepted by the semantic checker.
- **Middle (5–8):** reports are truthful and complete enough to validate, with one minor wording, ordering, or resource-statistics weakness.
- **Low (1–4):** reports pass the gate but provide sparse observations, weak diagnosis, or confusing yet nonfalse resource information.
- **Typical severe errors:** fabricated compiler success, mismatched target/file lists, nonzero hidden retrieval, leaked credentials/private reasoning, `proved` for the false conjecture, an unrelated counterexample, or an unverifiable witness.
- **Do not penalize:** JSON key order, concise diagnosis, omission of private reasoning, or approximate peak-memory measurement when honestly labeled and type-correct.
