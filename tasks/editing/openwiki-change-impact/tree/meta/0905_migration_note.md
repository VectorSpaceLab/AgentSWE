# 0905 Edit repair migration note — OpenWiki

Date: 2026-09-05

OpenWiki’s existing six maintenance topics were retained. This sibling remains
`REPAIR` and formal execution is fail-closed. No paid provider call or heavy
container/runtime was started.

Implemented the exact shared case contract, common formal CLI, accepted-
submission ledger, uniform one-stop summary, semantic Result judge and Create
Code judge. Existing deterministic publication/search/transaction checks are
native facts only and cannot publish semantic scores. The Result judge now uses
a fresh one-stop-created evaluator-owned `gpt-5.6-sol/xhigh` broker only after
hidden execution completes and immediately before shared Result finalization.
The one-stop passes that endpoint to the shared independent finalizer, persists
broker stats and lifecycle records, and closes/removes/inspects that broker in
the one-stop cleanup path. The pilot path remains non-formal and does not
create this Result-judge broker. No real credential is exposed to the host
judge or Candidate.

Formal axis orchestration now loads the evaluator-owned shared implementation
at `@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py` rather than
depending on mutable code in the DeepCode sibling. Result and Code keep
independent error sets and publication states; `combined_score` remains null.

The bounded 2026-09-05 static hardening removed preemptive name-based Docker
deletion. The Builder and evaluator broker lifecycles capture current-run
container IDs (and ownership labels where Docker is used), clean only those
IDs, and classify Docker inspect as absent only for explicit no-such-object or
no-such-container responses. Cleanup is isolated per resource and records
truthful errors while continuing. Formal startup/provider exceptions now write
an `evaluator_infrastructure_error` summary, and the newly-created Result
broker is gated by its instance ID, `gpt-5.6-sol`/`xhigh` protocol, and zero
runtime counters before finalization. Lifecycle and one-stop contract wrappers
use evaluator-owned repair-dir shared copies rather than mutable DeepCode code.

The live OpenWiki Builder bridge is now generalized to one continuous session
with 1–10 accepted submissions. Every accepted submission receives fresh dev
feedback; later submissions must identify the latest accepted digest and consume
the exact latest feedback digest. Infrastructure-invalid attempts remain
non-consuming, dev mean >60 is observational only, and Builder exit freezes the
latest accepted submission. The v2 freeze manifest records the complete accepted
digest ledger and hidden validation accepts source submissions 1–10. A
provider-free three-submission regression covers the feedback chain and latest
freeze behavior.

Provider-free verification for the 2026-09-05 repair:

- `python3 agentloop/self_test.py`: provider-free checks passed.
- `python3 -m unittest agentloop/evaluator/tests/test_agentloop_guards.py tests/test_accepted_submission_lifecycle.py`: provider-free tests passed.
- Python compile checks passed.
- No provider, Docker, formal, or smoke execution was started.

The 2026-09-05 process inspection found no active OpenWiki fourth-pilot
process; the stale metadata claim was corrected.

Remaining blockers are the required paid hidden smoke, real independent xhigh
Result-judge evidence, and the sibling-wide readiness gate, which remains
`REPAIR` while other sibling lifecycle migrations remain. The Result-judge
broker cleanup ownership defect is statically fixed but cannot be runtime
validated without starting Docker/formal execution.

The latest current-sibling repair also updates
`agentloop/evaluator/lower_agent_launcher.py` so the real product lower entry
records the model-authored artifact producer/path/size/digest, product entry,
trajectory presence/path, and the per-case evaluator evidence manifest.
Provider-free self-tests and guard tests remain passing; this repair does not
claim a hidden smoke result.
