# Final-Artifact Result Rubric — 100 points

## Validity and execution gates

- The created agent cannot launch, exceeds 600 seconds or 4 GiB, or exits success without both `solution.patch` and `repair_report.json`: **0 total**.
- Either target artifact is empty, non-UTF-8, malformed, corrupt, or unparsable: **0 total**.
- The patch is oversized for the active contract, unsafe, binary, creates a symlink, violates allowed paths, fails `git apply --check`, or cannot be applied once to the pristine repository: **0 total**.
- A valid but low-quality patch receives an ordinary score. Missing or malformed auxiliary metadata does not by itself force zero unless it makes a target artifact invalid.

For a case whose contract requires recovery, failure to provide a valid `migration_report.json` and evaluator-executed recovery artifact caps an otherwise valid result at **20/100**. Apply the cap after dimension scoring. Recovery is not required for `recovery: none` cases.

Score only final artifacts and observable behavior in the isolated patched repository. Do not inspect or score the agent implementation, prompts, framework, model choice, architecture, or similarity to a reference repair.

## 1. Required repair behavior and boundary coverage — 55 points

**Object:** evaluator-owned correctness, concurrency, migration, performance, protocol, tenant-coherence, and torn-state checks explicitly required by the active input.

- **55:** Every independently named behavior and threshold passes, including repeated/adverse states and interaction boundaries; no fixture-specific bypass is observable.
- **28:** The primary symptom is fixed and at least half of the named checks pass, but one substantial boundary, retry, state interaction, or threshold fails.
- **8:** Relevant behavior changes, but fewer than half pass, the solution is flaky/quadratic/non-idempotent, or only the issue example works.
- **0:** No repair check passes, the original symptom remains, or the public API is disabled.

Severe errors include duplicate durable effects, data loss, wrong migration generation, stale cross-tenant data, partial frame emission, accepting explicitly invalid protocol input, probabilistic correctness, or missed time/memory limits. Equivalent algorithms and synchronization designs that satisfy the observable contract receive full credit.

## 2. Public regression and compatibility — 20 points

**Object:** complete original public suite plus observable signatures, return shapes, ordering, errors, old/new formats, and unchanged paths identified by the input.

- **20:** Public suite passes and all published compatibility commitments hold.
- **10:** Most public behavior works, but one noncentral regression or compatibility detail fails.
- **2:** Multiple regressions occur, though part of the repaired path remains usable.
- **0:** Public tests cannot run or a central API/format is broken.

Internal refactoring is not penalized when external behavior is equivalent.

## 3. Patch scope, integrity, and portability — 15 points

**Object:** patch text, actual changed-path inventory, and clean application.

- **15:** Patch is focused, changes only authorized production paths, includes every necessary new artifact, creates no unsafe type, changes no tests/spec/build/dependency files, and applies without manual steps.
- **8:** Patch is safe and in scope but contains avoidable authorized-file churn.
- **2:** Patch barely remains in scope while making broad unrelated changes.
- **0:** Any prohibited path, dependency/test/spec weakening, hard-coded fixture bypass, traversal, unsafe file, or disabled validation is present.

Do not penalize a multi-file repair when the root cause crosses modules.

## 4. Reporting and executable evidence — 10 points

**Object:** `repair_report.json`, `run_report.json`, conditional `migration_report.json`, patch inventory consistency, and evaluator execution of the recovery artifact.

- **10:** Schemas are exact; changed files match the patch; reproduction/validation claims are specific and observed; runtime/memory and all provider counts are truthful; conditional recovery format, command, and six probe phases execute successfully and agree with the report.
- **5:** Reports parse and identify the repair but contain vague evidence, incomplete metadata, or a noncentral inconsistency.
- **1:** Reports are mostly boilerplate or contain material unsupported claims while retaining a few accurate facts.
- **0:** Reports are unusable, deceptive, leak credentials, conceal failure, or the required recovery command is fabricated.

**Arithmetic:** 55 + 20 + 15 + 10 = **100**.

