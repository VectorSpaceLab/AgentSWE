# Task Goal

You are building a formal-proof repair agent for Lean 4 developers. A user supplies a pinned, multi-module Lake project and a Markdown request naming or describing one or more proof targets, writable files, and source-level constraints. Your agent must inspect the complete active project, understand imports, namespaces, notation, structures, classes, instances, definitions, existing lemmas, declaration order, and compiler diagnostics, then produce a minimal patch accepted by a fresh Lean kernel.

The core value is dependable repository-level repair. Users should receive a reviewable patch that completes all requested declarations in dependency order and obeys explicit proof-length, tactic, premise-use, import, and changed-path restrictions—not a plausible but unchecked tactic suggestion. Some projects contain similarly named distractors or a natural-language specification that must be matched to the correct formal declarations.

Your agent must also recognize a genuinely false finite conjecture. In that situation it must preserve the target and provide a concrete Lean-checkable refutation whose type is the negation of that exact declaration, rather than manufacture an axiom or silently weaken the statement.

Your final objective is an executable `run_agent.py` that follows the uniform interface, emits the required patch and reports, and generalizes to unseen projects. Do not copy a reference implementation, hard-code case names, declaration names, project hashes, proofs, witnesses, diagnostics, or development answers. Do not require interactive confirmation, expose private reasoning, alter fixture files in place, or implement an unrelated theorem-proving service.
