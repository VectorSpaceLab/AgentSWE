# 0905 Edit repair migration note — Claude policy provenance

Date: 2026-09-05

## September 6, 2026 focused-case and evidence-binding repair

Hidden execution now rejects filename-only case JSON. Every evaluator-issued
case must match the canonical executable contract in
`agentloop/evaluator/case_contract.py`: exact primary/scored/excluded axes,
allowed `hook_event` and `inspector_command` action types, a meaningful runtime
nonce, and a canonical contract digest. A paired evaluator-private
`test_NNN.oracle.json` with its own digest is required. The visible case schema
and private-oracle schema are recorded under `agentloop/schemas/`.

The lower `gpt-5.6-sol/medium` model now selects both hook events and bounded
inspector commands from the evaluator-issued catalog. The inspector is no
longer automatically forced to `--audit`. Each product request, response,
receipt, observation, and selected action contributes to a run-local binding;
the model-authored `agent_result.json` must copy the exact case-spec,
case-contract, runtime/execution nonce, product-event, observation, and
trajectory digests. Hidden execution and attestation independently reject a
changed action ID, receipt/event digest, or artifact binding.

The formal finalizer no longer writes placeholder task text or a placeholder
oracle comparison before Result judging. Hidden execution now writes the real
task input, a focused task-local 100-point rubric, sanitized native product
evidence, and a comparison against the evaluator-private oracle. Their paths
and digests are bound into the case record and rechecked before the independent
`gpt-5.6-sol/xhigh` Result judge is called. Excluded axes cannot trigger the
legacy global 35-point cap. The independent Code judge now receives
`evaluator/code_rubric.md`, the sibling-local eight-dimension Code rubric,
instead of the legacy multi-axis Result rubric.

Provider/broker failure attribution was also corrected: an infrastructure
failure remains infrastructure-invalid/N/A even though the trajectory also
contains a generic lower-agent failure event. Candidate product failures remain
Candidate-attributed. On September 6, 2026, nine focused provider-free tests,
36 existing Agent-loop self-checks, and the formal one-stop offline self-test
passed; no provider call, Docker workload, hidden smoke, Result judge, Code
judge, or formal evaluation was started.

Readiness remains `REPAIR`. The external hidden bundles must be reissued under
the v2 schemas, and a real post-freeze six-hidden run must still demonstrate
broker usage, model-selected product actions, independent Result/Code judge
contracts, and ownership-backed cleanup.

This sibling remains `REPAIR`; no paid provider call, Docker workload, or formal
hidden run was started during the repair.

The six hidden prompts now state focused user goals without exposing private
oracles or scoring mechanics. The lifecycle supports up to ten distinct
accepted Candidate submissions in one Builder session, evaluates both dev cases
after each accepted submission, records but does not freeze on `dev mean > 60`,
and executes all six hidden cases only after freeze. Formal Result publication
uses the shared semantic `gpt-5.6-sol/xhigh` judge once per scoreable hidden
case. Code publication routes through the evaluator-owned wrapper around the
unchanged Create Code judge; Result and Code remain independent and
`combined_score` is null.

Provider-free self-tests passed after the follow-up path-alignment repair. The
2026-09-05 static repair closes the
lower-agent artifact provenance gap: only a result JSON returned by the lower
model and passing the result contract is written as `agent_result.json`.
Provider, broker, target-product, and invalid-adjudication failures now leave
the artifact absent, record a bounded `lower_agent_failure`, retain explicit
infrastructure classification where applicable, and return nonzero. Hidden
execution and attestation require the artifact to be present, contract-valid,
and marked `origin=lower_model_final_response` with
`evaluator_synthesized=false` before treating a case as real execution
evidence.

The 2026-09-06 follow-up also binds formal hidden attestation output to
`lifecycle/hidden_after_freeze_attestation.json`, which is the path consumed by
`evaluator/formal_finalize.py`, and corrects the pilot summary to reference its
actual root-level `pilot_hidden_attestation.json` output.

Changed in these static repairs: `agentloop/evaluator/lower_agent_launcher.py`,
`agentloop/evaluator/hidden_executor.py`,
`agentloop/evaluator/hidden_attestation.py`, `agentloop/self_test.py`, and
these migration records. No provider call, Docker workload, or formal
execution was started. Remaining readiness work is a new real post-freeze
six-hidden smoke with evaluator-owned broker credentials, semantic Result
evidence from the shared judge, and a cleanup attestation whose absence state
is verified by the final publication gate.
