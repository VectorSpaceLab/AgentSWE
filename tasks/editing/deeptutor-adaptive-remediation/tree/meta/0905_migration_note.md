# 0905 Edit repair migration note — DeepTutor

Date: 2026-09-07

The case contract readiness declaration was corrected on September 6, 2026
from stale `READY` to `REPAIR`, with the missing current six-case hidden smoke
recorded as the blocker. This is metadata only; it does not promote any
historical smoke score or change the case behavior.

The established DeepTutor topics were retained. This sibling remains `REPAIR`;
formal execution is fail-closed. A real 0905 lower smoke was run for
`test_001` and `test_003`: both entered the DeepTutor mastery loop with the
locked `gpt-5.6-sol/medium` lower broker, producing 12 successful lower calls
in total. `test_001` received one completed independent xhigh Result judgment
(17/100). `test_003` received no completed Result response after 524/502
infrastructure attempts, so no publishable smoke manifest was created.

Implemented the exact 2+6 case contract, common bounded formal CLI, uniform
one-stop summary, a generalized accepted-submission ledger, strict
infrastructure/candidate separation, independent shared Result-judge calls,
and one independent Create Code-judge call. The formal one-stop now creates
and owns its fresh xhigh Result-judge broker after scoreable hidden execution;
external formal judge endpoints are rejected. Deterministic scoring cannot
publish and there is no heuristic fallback.

The same one-stop records remove-plus-inspect cleanup evidence for the
Result-judge, hidden, public, and Builder broker containers, with explicit
`all_started_containers_absent` and unrelated-container ownership fields.
Container ownership is captured through Docker cidfiles; cleanup uses those
container IDs, treats only explicit `No such object/container` responses as
absence, continues after an individual cleanup error, and retains startup-
failure cleanup receipts. Result and Code use separate error sets through the
evaluator-owned shared formal-axes implementation, so either axis can remain
publishable when the other is unavailable.

The formal DeepTutor bridge now permits 1..10 distinct accepted submissions in
one continuous Builder session. Each accepted submission executes `dev_001`
and `dev_002`; infrastructure-invalid attempts do not consume a slot; the
dev mean is recorded as `dev_passed` without automatic freeze; and freeze is
performed by the outer evaluator after Builder exit or the maximum accepted
round. Hidden execution accepts the generalized latest-submission freeze.

Provider-free verification:

- `python3 -m unittest evaluator/tests/test_0905_repair.py`: 8 tests passed.
- `python3 -m unittest tests/test_agentloop_runtime.py`: 4 tests passed,
  including the three-accepted-submission ledger lifecycle.
- Python compile checks passed.
- Static/default one-stop passed with zero provider calls and emitted
  `one_stop_summary.json`.
- Shared readiness audit found only the intentionally absent 0905 paid hidden
  smoke for this sibling.

Remaining blockers are publishable smoke coverage for all six remaining
siblings and a completed independent xhigh Result response for DeepTutor
`test_003`. DeepTutor itself still has no current 0905 smoke manifest, so
readiness remains `REPAIR`; no formal branch has been started.

## September 7 continuation

The lower artifact boundary was tightened after inspecting the actual product
source. The evaluator launcher now appends a case-local terminal protocol to
the real mastery task, passes `DEEPTUTOR_CASE_ID` and the runtime nonce into
the product process, and captures `agent_result.json` only after the product
exits. Hidden classification and shared formal provenance validation require a
case-bound JSON object with `schema_version`, `case_id`, `status`, `summary`,
and object-valued `artifacts`; missing, malformed, wrong-case, and evaluator-
synthesized artifacts are rejected.

The Builder/public contract now explicitly requires this writer at DeepTutor's
common agent-loop final-result seam, and the current `delivery/solution.patch`
now includes that product-side implementation. The untouched authoritative
source remains unchanged and does not contain the writer; the materialized
Candidate tree does. The next provider-backed smoke must prove this path
through the real model-selected mastery loop before the sibling can become
READY.

The current provider-free evidence is 13 evaluator contract tests, 4 runtime
tests, the DeepTutor self-test, 6 shared formal-contract tests, successful
Candidate materialization/patch application, targeted materialized-product
compilation, and a direct product-writer contract probe in the task-local
Python 3.12/Pydantic 2.13.5 environment. The probe proves valid exact-byte
write, malformed-response rejection, and wrong-case rejection; it does not
prove a real lower model reached the final-result seam. The formal gate remains
closed at 0/10.

## September 7 continuation — public mastery surface repair

The latest real lower-model pilot proved a concrete Candidate capability gap:
Candidate 2 exposed only the original five mastery tools, while the public and
hidden adaptive-remediation contract requires remediation, review, event,
session, policy, snapshot, witness, verification, and chain-audit surfaces.

The current sibling now adds the Builder-visible
`input/05_mastery_tool_contract.md`, makes the complete named interface an
explicit public requirement, updates both public dev tasks, and tells the
Builder to verify the full registered name/type surface before submission. This
is a contract/pacing repair within the current sibling; it does not synthesize
product actions, expose hidden oracle values, or modify the authoritative
DeepTutor source. A focused offline regression covers the public contract.

A fresh provider-backed smoke remains required after this repair. The prior
pilot remains non-publishable evidence and is not promoted.

## September 7 continuation — Builder dependency/pace guidance

The `0907-smoke-current-004` trace reached the real xhigh Builder and recorded
25 successful responses before the 900-second timeout. During a Builder-side
registry probe, the product worktree reported `ModuleNotFoundError: yaml`.
That dependency is available to the evaluator-owned lower runtime, but is not
guaranteed in the Builder image. The current `harbor/formal_one_stop.py`
instruction now tells the Builder to record such optional import failures and
continue with Python 3 syntax/compile checks, registry-shape checks, and
delivery validation rather than spending the first submission window on broad
environment setup. It explicitly preserves the requirement that the product
implementation itself be complete and that dependency-complete lower execution
remain evaluator-owned.

The focused repair regression now checks this guidance. A new provider-backed
smoke is required to verify that the Builder submits a Candidate and reaches
the real lower `gpt-5.6-sol/medium` path; no readiness promotion is implied by
the Builder-only evidence.

## September 8 continuation — delivery and interrupted-run evidence

The `0908-smoke-current-003` Builder reached delivery validation but generated
a repository-relative `diff -ruN` patch. The Candidate parser intentionally
accepts Git-style `diff --git a/... b/...` and `+++ b/...` paths, so that
attempt was rejected before consuming an accepted-submission slot. The
Builder instruction now explicitly requires Git-compatible headers and runs a
parser-compatible path assertion before invoking `validate_dev_candidate`.

The next bounded attempt, `0908-smoke-current-004`, proved the repair at the
Builder level: the Builder generated a six-file Git patch, the path assertion
passed, and deterministic postmortem materialization, `git apply --check`,
application, and Python compilation all passed. That delivery is not an
accepted Candidate. The evaluator orchestration parent terminated at the
external tool-session boundary before the Builder completed, leaving a stale
controller socket; no dev lower run, freeze, hidden run, Result judge, or Code
judge occurred. Ownership-scoped cleanup removed the orphaned Harbor Compose
containers and both broker containers without touching unrelated containers.

Because the interrupted run also showed that the xhigh Builder broker could be
terminated before an end-of-run stats collection, the shared broker now writes
an atomic run-local stats snapshot at startup and after every reservation,
completion, and delivery-failure transition. The DeepTutor launcher now passes
that stats-file path explicitly. This is evaluator evidence hardening only; it
does not change the Builder model, Candidate behavior, lower model, or scoring.
DeepTutor remains `REPAIR` pending a fully accepted Candidate and post-freeze
hidden chain.

## September 8 continuation — current-007 lower transport and retry repair

The bounded `0908-smoke-current-007` run proved that the delivery-mechanics
repairs work: the real xhigh Builder produced valid delivery bundles, the
evaluator materialized two Builder-authored revisions, the dependency-complete
runtime exposed all 26 public mastery tools, and the first revision ran the
real DeepTutor mastery loop to a product-authored `agent_result.json` through
seven successful `gpt-5.6-sol/medium` calls. That public attempt remained
infrastructure-invalid because post-turn session-title generation bypassed the
task-local adapter and called `/v1/chat/completions`, which the locked Responses
broker correctly rejected.

The evaluator adapter now patches both DeepTutor's agentic client factory and
the services-layer `stream` export/factory, so auxiliary product LLM calls use
the same evaluator-owned `/v1/responses` transport. A fresh diagnostic replay
of the latest Builder-authored revision completed 10 of 10 medium broker calls
with zero provider, protocol, or delivery failures, persisted a model/product-
authored terminal artifact, and emitted the post-turn title event. This replay
was deliberately outside the accepted-round ledger: it did not freeze a
Candidate, execute hidden cases, or produce a formal Result or Code score.

The same current-007 trace exposed a separate retry-ledger defect. The first
infrastructure-invalid attempt correctly did not consume Candidate round 1,
but the revised submission reused `public/candidate_001/dev_001` and failed
with `FileExistsError`. Public lower outputs are now keyed by both capability
round and submission-attempt number. A provider-free functional regression
proves that an infrastructure-invalid attempt and its same-round retry preserve
distinct evidence directories while only the successful retry consumes the
round.

Current verification is 14 focused evaluator tests, 4 runtime tests, the
provider-free self-test, an actual dependency-runtime adapter import probe, and
the real-medium transport replay. DeepTutor remains `REPAIR`: current-007 had
zero accepted submissions, no freeze, no hidden execution, and no independent
Result or Code judge completion.

## September 8 continuation — current-009 split-registry audit repair and pilot

The `0908-smoke-current-009` pilot exposed a sibling-local evaluator false
negative. The patched product kept its complete 26-tool mastery registry in a
split layout: `tools.py` imported the concrete extension names from
`extended_tools.py`. The lower launcher’s source audit read only `tools.py`,
so it reported the original five tools as present and classified an otherwise
valid product run as `candidate_capability_gap`. The dependency-complete runtime
probe simultaneously observed all 26 registered names, and the product authored
the required terminal artifact.

The repair makes the source audit read all product-owned Python modules in the
mastery capability directory and records those source files in its evidence.
It does not execute hidden code, synthesize tool results, or change the product
registry. A focused regression covers the split-registry layout.

After the repair, the same bounded pilot completed with two accepted Builder
revisions. Both public `dev_001` executions were `candidate_valid` with score
100 and 10/10 successful evaluator-owned `gpt-5.6-sol/medium` calls. The
Candidate 2 freeze remained stable, and post-freeze hidden `test_001` completed
with 12/12 successful medium calls, a product-authored case-bound artifact,
and no provider, protocol, or delivery failures. The pilot Result measurement
was 20/100 and is smoke evidence only; no formal Result/Code score was claimed.
All Builder, public, and hidden containers were removed with
`unrelated_containers_touched=false`.

This is still not the six-hidden-case current smoke required by the central
formal-readiness gate, so DeepTutor remains `REPAIR` and formal launch remains
unauthorized.
