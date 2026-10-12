# Aider crash-recoverable repository-setup transaction benchmark

> Agent-loop sibling v1: stage-A migration infrastructure. Static smoke is not a formal paper Result; the current state is `PARTIAL` because no real successful broker calls have been produced.

This sibling preserves the authoritative Aider/Git transaction task and native evaluator copy while adding a real lower-agent protocol. The target lower agent is the Candidate-modified Aider, entered with `python -m aider`; `aider.worktree_plan_adapter` is the product mechanism under edit. Every result must come from Aider's own model/API/tool trajectory, the dynamic evaluator oracle, state/receipt/provenance, and the final `agent_result.json`; an external general-purpose Codex cannot replace it.

Migration audit and protocol files:

- `meta/provenance/source_digest_manifest.json`
- `meta/agentloop_migration_plan.md`
- `meta/hidden_test_inventory.md`
- `protocol_lock.json`
- `meta/agentloop_result_rubric.md` and `meta/code_rubric.md`

The stage-B entry point is `evaluator/harness/run_lower_agent_case.py`. Candidate materialization/build is performed by `evaluator/materialize_candidate.py`. Within one Builder session, `harbor/agentloop_controller.py` permits at most ten accepted-submission lifecycles; every acceptance runs both public dev cases and returns fresh feedback. When the Builder exits or reaches the limit, the latest accepted Candidate is frozen. The broker is `evaluator/broker/responses_broker.py`. The default controller runs only a static build smoke; pass `--run-real` explicitly, after review by the main agent, to start Docker/API work.

The benchmark tests whether a candidate can add schema-v3, epoch-isolated transactions across a local Git superproject and component repositories, while preserving a fixed Python Aider source. Workers create an isolated candidate for each repository; the parent candidate binds child gitlinks. Repository-wide admission accepts disjoint transactions but blocks overlap, and a durable decision lets cross-repository publication recovery roll the same child suffix forward.

The Builder receives the exact canonical public `input/` tree, including both the JSON schema and the raw `input/repository`, plus two independent `dev_cases`. The `evaluator/`, `test_cases/`, `meta/`, and hidden scoring material remain isolated. The fixed product entry point is:

```bash
python -m aider.worktree_plan_adapter --request request.json --response response.json
```

Prepare a unique environment:

```bash
evaluator/harness/prepare_environment.sh input/repository \
  /opt/agentswe/benchmark/envs/aider-worktree-transaction-ledger-edit-v1
```

After installing the candidate source into that prefix, run the public cases:

```bash
dev_cases/dev_001/run.sh /tmp/aider-repository-set-dev-001
dev_cases/dev_002/run.sh /tmp/aider-repository-set-dev-002
```

Public case 001 demonstrates isolated federated preparation followed by a component/root publication whose root gitlink names the candidate component. Public case 002 demonstrates cross-state overlap rejection, disjoint admission, hard death after the first participant makes a durable decision, non-blocking partial state, takeover at the next fence, and suffix roll-forward. The authoritative suite command is in `evaluator/eval_prompt.md`. Six isolated hidden cases combine repository-wide admission with a real concurrent DAG, isolation/worktree/ref/object pruning, post-decision prefix recovery, filters/hooks/nested gitlinks, component conflicts with independent commit transactions, reverse-decision rollback for dirty root/component indexes, and alias/admission/decision/obstruction/status-purity guards plus compatibility. Each case scores `0..100`; the final score is the arithmetic mean. Case-local timeouts/failures do not stop later cases, but missing, malformed, duplicate, evaluator-error, or invalid-manifest results invalidate aggregation.

The tool samples the real process tree PSS from Linux `/proc` under an 8 GiB cap and does not use address-space/JIT limits or artificial memory pressure. For auditability, the evaluator independently reads real refs after every operation, the root tree's `160000` gitlink entries, each participant's `aider/transactions/` admission bytes, `state_dir/ledger.json`, `git worktree list`, and append-only records from evaluator-owned worker/test commands. Publication, exactly-once behavior, read-only `status`, ownership release, and pre-decision isolation are decided from those bytes; response claims are not the oracle. The full inventory is in the “verifiability” section of `input/02_interface_and_delivery.md`.

Run the evaluator integrity check with:

```bash
python3 evaluator/harness/self_test.py
```
