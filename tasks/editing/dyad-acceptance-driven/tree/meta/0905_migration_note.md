# 0905 Edit repair migration note — Dyad

Date: 2026-09-05

## September 6, 2026 integration review correction

Private-oracle isolation is now a filesystem property, not merely a path
omission. `prepare_scenario` constructs the current run's private oracle as an
evaluator-parent in-memory object and deterministic digest. It removes any
stale `private_oracle.json` at the reused destination and does not write the
current oracle before launch. `run_lower_agent_case.py` asserts the path is
absent immediately before `subprocess.run`; only after the lower product
process returns or times out does the parent persist the oracle, verify its
digest, and create the comparison/final evidence. A provider-free regression
substitutes the launcher, asserts the oracle path does not exist and is not in
the launch command during `subprocess.run`, then verifies the oracle and
comparison exist afterward.

Required action names and order no longer prove scenario completion by
themselves. Every required action is matched to its ordered trajectory record
and must have `dispatch_status=completed` plus a case-appropriate usable
observation. Required Acceptance reads need successful typed envelopes with
non-null values; preview start/retry needs a typed session, except that
`test_002`'s first deliberately lost delivery must prove the product accepted
the start before the session response was withheld. Mutations, restoration,
restart, ordinary-chat compatibility, Tests stop and final artifact capture
have explicit observation rules. Dispatcher validation errors, missing
handlers, absent required read results, missing attestation actions and
untyped placeholders fail closed.

`test_003` remains able to prove intentional isolation behavior, but only from
real typed product responses: stale/foreign controls require a typed rejected
envelope or typed rejected value, and the foreign read requires typed rejection
or typed successful null nondisclosure. Dispatcher-side `action_error`,
`missingHandler`, absent values and untyped prose/status objects receive no
credit.

The public scenario file now has a closed key set: case/scenario identity,
dynamic target, operation identity, exact task identity, and explicitly
`fixture_only` fault injection. It excludes the scenario axis, private required
actions, required checks, oracle fields and expected product terminal fields.
Provider-free tests validate the actual file and inject each forbidden class
to confirm rejection. Fault injection may script evaluator runner returns and
timing, but is not represented as expected product terminal truth.

The amended provider-free suite contains 31 passing tests and the TypeScript
scenario driver parses with esbuild. Both sibling self-tests and JSON metadata
validation also pass. No provider call, paid smoke, Docker workload, hidden
runtime execution, Result judge, Code judge or formal evaluation was run; no
live scenario success is claimed and readiness remains `REPAIR`.

## September 6, 2026 distinct model-driven scenario repair

The six hidden cases no longer share the old evaluator-driven post-chat
start/read/attest sequence. `evaluator/scenario_contract.py` defines six
distinct run-local scenarios and private oracle contracts:

- `test_001`: failed-first test observation, focused repair, passing rerun and
  durable preview/attestation readback;
- `test_002`: a first preview response withheld after product acceptance,
  byte-equivalent model-selected retry, one preview runner invocation and
  same-session recovery;
- `test_003`: stale-generation control, foreign control and foreign read
  rejection without mutation of the authorized snapshot;
- `test_004`: model-selected target mutation while preview work is active,
  invalidation and no resurrection after restoration;
- `test_005`: model-selected Tests stop, duplicate stop, delayed successful
  runner callback and monotonic `cancelled` state;
- `test_006`: model-selected main/database restart during nonterminal work,
  durable reads, no automatic preview rerun and a separately recorded ordinary
  chat compatibility check.

For each run, the evaluator copies the evaluator-issued natural task into a
new `executed_task.md`, appends dynamic public fixture facts and one generic
product-action interface, and records its SHA-256. The model works through
Dyad's production typed chat and local-agent tools. One product action is
executed only when a single action marker is parsed from a newly persisted
assistant message. Every action record contains that message's ID and hash,
the exact model request and request hash, returned typed envelope/events, and
`evaluator_defaulted=false`. Missing, malformed or unsupported actions stop
the trajectory; there is no case-generic success fallback.

The run-local fixture controls runner outcomes and adversarial timing, but it
does not choose the action sequence. The evaluator's generic dispatcher only
invokes the one action the model selected. The agent artifact is the object in
the model's `finish` action and is captured verbatim as
`dyad-lower-agent-artifact-v3`; setup failures and incomplete work write only
separate evaluator/native evidence and never synthesize an agent artifact.

After execution, `run_lower_agent_case.py` compares that model artifact with
the native evidence and the evaluator-only private oracle. Publication fails
closed unless case/scenario identity, exact executed-task hash, ordered
model-selected actions, persisted-message provenance, the actual
app/chat/run/session/revision/target-fingerprint tuple, artifact action list,
and all scenario-specific checks agree. The private oracle file is never
passed to the Candidate process.

Formal Result preparation now creates a run-local input overlay. Each Result
judge invocation receives the exact `executed_task.md` used by the model and
the matching run-local private comparison. Task and comparison bytes are
re-hashed before routing; the static sibling `test_cases/*/input.md` files are
not a fallback. The hidden attestation filename was aligned with the shared
formal reader. The shared Result and Code judges were not modified, and their
publication/error states remain independent.

Provider-free verification on September 6, 2026 includes 28 evaluator tests,
both sibling self-tests, Python compilation and an esbuild parse of the
TypeScript scenario driver. No provider call, Docker workload, paid smoke,
native Electron run, hidden runtime run, Result judge call, Code judge call or
formal evaluation was performed. These tests establish dispatch/provenance and
fail-closed contracts only; they do not claim that a real model and Candidate
have successfully completed any of the six live scenarios. Readiness remains
`REPAIR`.

## September 6, 2026 audit contract repair

This bounded repair addresses the read-only audit's deterministic contract
defects only. The lower-case emitter and `schemas/result.schema.json` now use
the same `dyad-agentloop-case-result-v2` envelope, including a schema-valid
broker-unavailable result. The formal shared orchestrator's existing local
lookup now resolves to `evaluator/code_rubric.md`, which mirrors the locked
eight Code dimensions and provides Dyad-specific evidence anchors; no shared
Code judge or Harbor runtime file was changed.

Successful case binding now requires `app_id`, `chat_id`, `run_id`,
`session_id`, workspace `revision`, and `target_fingerprint`. Positive numeric
and non-empty string app/chat IDs are accepted, booleans and non-positive
numbers are rejected, and the lower classifier independently checks the tuple
instead of trusting only the artifact's completion flag. The headless artifact
records the product-observed preview session ID. Its target fingerprint must
come from product-observed run, preview-session/readback, or attestation state;
the synthetic hash-of-target fallback was removed. Missing binding remains a
scoreable Candidate failure and cannot be promoted to `valid`.

Provider-free regression coverage was added in
`evaluator/tests/test_contract_repairs.py`. On September 6, 2026, all 22 tests
under `evaluator/tests` passed, `smoke/self_test.py` passed, and
`harbor/formal_one_stop.py --self-test` reported `network_calls=0`,
`docker_started=false`, and `formal_execution_started=false`.

This does not claim full hidden-scenario repair or runtime Acceptance success.
No paid smoke, provider call, Docker workload, native Electron run, hidden
six-case run, Result judge invocation, Code judge invocation, or formal
evaluation was performed. Readiness remains `REPAIR` pending authorized real
runtime evidence for the scenario-specific idempotency, ownership, drift,
cancellation, restart, and terminal-attestation behaviors.

On September 6, 2026, the live preflight was corrected to avoid an
architecture-specific false negative: the established reference used
`acceptance_store.ts`, while a real Builder Candidate used the equivalent
`acceptance_service.ts`. Preflight now requires the typed Acceptance contract
and handlers plus one of those service implementations, records which service
path was found, and still fails closed when neither exists. This preserves the
product-behavior requirement without prescribing a filename.

This sibling remains `REPAIR`; formal execution is fail-closed. No paid API or
heavy Electron/container run was started.

Implemented the exact 2+6 contract, common formal CLI, accepted-submission
ledger, uniform summary, shared Result judge and Create Code judge. Dyad’s
scoreable lower path is explicitly the typed
`chat:stream -> SQLite -> Git -> Acceptance IPC` path; native Electron process
exit remains smoke-only. Every hidden prompt and successful artifact must bind
to the case-local `app_id`, `chat_id`, `run_id`, `session_id`, workspace
`revision`, and `target_fingerprint`. The classifier rejects incomplete
binding as a Candidate failure.

The formal one-stop now creates its own fresh `gpt-5.6-sol`/`xhigh` Result-judge
broker only after hidden execution and before shared semantic scoring. Formal
mode rejects an externally supplied Result-judge endpoint. The one-stop checks
the new broker's zero-call/model/effort state, passes its loopback endpoint and
the fixed shared Result/Code judge entries to the finalizer, saves final broker
stats, then removes and inspects the run-local broker container. Cleanup records
per-role startup/stats/remove/inspect evidence and declares
`unrelated_containers_touched=false`. Pilot does not start a Result-judge broker
and continues to publish neither Result nor Code.

Formal axis orchestration now loads the evaluator-owned shared implementation
at `@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py` instead of
importing mutable code from the DeepCode sibling. Result and Code retain
separate error sets and publication states; `combined_score` stays null.

The bounded 2026-09-05 static hardening additionally removed preemptive
name-based Docker deletion. Broker startup records the current-run container
ID, cleanup targets only that ID, and Docker inspect treats only an explicit
`No such object`/`No such container` response as absence. Cleanup records errors
per role and continues to subsequent roles. Mixed provider/broker/client
failure counters are infrastructure-invalid even when another call succeeded.
The lifecycle and one-stop contract wrappers now point to evaluator-owned
repair-dir shared copies rather than the mutable DeepCode sibling.

Deterministic/native scorers remain non-formal, and the existing lower-case
classifier still separates provider/infrastructure failure from scoreable
Candidate failure.

Provider-free verification:

- `python3 -m unittest evaluator/tests/test_0905_repair.py`: 7 tests passed.
- `python3 -m unittest -v evaluator/tests/test_0905_result_judge_ownership.py`:
  8 tests passed, covering formal CLI ownership, fresh xhigh/zero-call gating,
  post-hidden startup ordering, fixed judge arguments, remove+inspect cleanup,
  and Candidate/provider classification without Docker or network.
- `python3 smoke/self_test.py`: passed with the current accepted-submission
  freeze schema and zero broker calls.
- `python3 harbor/formal_one_stop.py --self-test`: passed with
  `network_calls=0`, `docker_started=false`, and
  `formal_execution_started=false`.
- `python3 -m compileall -q harbor evaluator smoke`: passed with bytecode
  redirected outside the sibling during this repair.

No provider, Docker, formal, or real smoke was started. Readiness therefore
remains `REPAIR` until an authorized real typed-Acceptance six-hidden run
observes provider usage, independent Result/Code judge contracts, and the new
runtime cleanup attestation.

## September 8, 2026 public-result artifact gate repair

The formal public-result gate and the active compatibility controller now
separate product execution evidence from model-result artifact validity. A
`valid` result is accepted only when it carries the captured
`dyad-lower-agent-artifact-v3` object with the expected model-finish provenance
(`model_via_dyad_typed_chat`, persisted typed-chat finish capture, and no
evaluator synthesis). `artifact_present` alone is no longer sufficient.

A real `candidate_failure` remains scoreable feedback when product entry was
observed and the lower broker recorded both a call and a successful response,
even if the model artifact is absent or malformed. Provider, broker, launcher,
product-entry, and transport failures remain infrastructure-invalid and do not
consume an accepted-submission slot.

Provider-free regression coverage in
`evaluator/tests/test_acceptance_classification_repair.py` now covers positive
model-finish provenance, absent/malformed/evaluator-owned valid artifacts,
artifactless and invalid-artifact Candidate failures, live evidence
prerequisites, and infrastructure non-consumption. The focused suite has 7
passing tests. `smoke/self_test.py`, `harbor/formal_one_stop.py --self-test`,
and Python compilation of the changed Python files also pass with no provider
or Docker execution. Full evaluator discovery reports one out-of-scope shared
formal-axes private-oracle fixture dependency in
`test_0905_repair.py`; no shared file was changed to work around it. Readiness
remains `REPAIR`.
