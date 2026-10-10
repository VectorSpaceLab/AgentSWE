# Design

## Task registry

Every task is a directory `tasks/<family>/<id>/` with a `task.json`; the registry is the set of
those files. Fields: `id`, `family`, `short`, `title`, `version`, `tier` (T1 single command, T2
external search key, T3 special host), `lite`, `status` (planned / extracted / runnable / verified),
`runner`, `roles`, `services`, `images`, `envs`, `host` (requirements checked by `doctor`), `protocol`
(the published budget), `smoke` (a cheap, non-comparable budget), `upstream`, `runner_config`.

## Runner interface

`task.json` `runner` names a module in `agentswe/runners/` that implements
`run(cfg, task, builder=, seed=, smoke=, label=)`, `status(cfg, launch)`, `result(cfg, launch)` and
`stop(cfg, launch)`. Each run writes `<run_id>.launch.json` (no secrets) and, when finished,
`<run_id>.result.json` with `score`, `score_kind`, `valid`, `per_case`, `usage` and `provenance`.

## Roles and credentials

`BUILDER`, `RUNTIME` and `JUDGE` are configured independently (Editing runs need one key for all three). Creation
and Optimization runs write keys only to 0600 files under `AGENTSWE_HOME/secrets/<run_id>/`, mounted into the brokers
(Creation: one provider broker per role; Optimization: the builder broker and the task evaluator); Codex, Result
judges and candidates receive placeholder values only, and the key files are deleted when the run's controller
exits. Editing runs write the key to one 0600 file per home, `AGENTSWE_HOME/secrets/editing/credential.env`, removed
by `result` or `stop` when no other Editing run in the home is live; the lower-agent and Result-judge brokers mount
it, edited products and lower agents receive placeholders, and the Builder's Codex receives the key itself in a
tmpfs `auth.json` for the Builder session, as in the paper's Editing protocol (`docs/ENV.md`, "Where the keys are").

## Images and environments

Base images are referenced on docker.io by digest. Local tags live under `agentswe-os/`. Trusted
environments are rebuilt from exact conda specs inside a container, at the same in-container prefix
the published runs used (`/opt/agentswe/benchmark/envs/<name>`), so their scripts' shebangs resolve
unchanged.

## Tests

`python3 -m unittest discover -s tests` runs the repository tests (standard library only; local sockets and
temporary directories, no model or network access). The Editing control plane carries its own tests: run
`python3 -m unittest <test module>` inside `runners/editing/control` (`test_judge_stream_integration` also needs
the `requests` package).

The Editing task trees ship byte for byte as the published runs used them, tests included. Some tests inside them
fail on the release and are left unchanged because they are stale, not because the code they cover is wrong:

| Tree test | Failure | Why it is stale |
|---|---|---|
| `tree/evaluator/tests/test_0905_repair.py` of DeepCode, DeepTutor, Dyad and OpenWiki: `test_contract_inventory_and_entries`, `test_judge_contracts_require_exact_usage` | `'deepseek-flash' != 'gpt-5.6-sol'`; `['wrong Result judge model'] != []` | they assert the Result judge model of an earlier configuration; the rendered control plane requires the configured judge model |
| `tasks/editing/deepcode-claim-traceability/tree/evaluator/tests/test_0905_repair.py`: `test_builder_task_mounts_writable_git_backed_product_worktree` | `AttributeError: ... no attribute 'readiness_profile'` | its stub controller predates the `readiness_profile` attribute the Builder staging reads |
| `tasks/editing/dyad-acceptance-driven/tree/evaluator/tests/test_0910_semantic_execution.py`: `test_live_public_wrapper_routes_execution_to_independent_score`, `test_wrong_claims_are_independently_scored_with_exact_inputs_and_cached` | `None != 17` | the mocked judge answer no longer yields a score on the current scoring path |
| `tasks/editing/dyad-acceptance-driven/tree/evaluator/tests/test_0911_frozen_evidence.py`: `test_unknown_code_request_not_released_by_bridge` | `AssertionError: process forbidden` | the test forbids subprocesses; the code runner now starts one before the check the test expects |
| `tasks/editing/openwiki-change-impact/tree/evaluator/tests/test_0911_frozen_identity.py` (12 tests), `test_0913_hidden_finalizer.py` (7 tests) | `KeyError: 'candidate_path'` | the stub controller's freeze record lacks `candidate_path`, which `freeze()` reads |
