# Interface and Delivery

## Installation and launch

Create the agent in the submission workspace and use only the dedicated environment described in `04_resources.md`. Every case launches exactly:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

The input is one UTF-8 Markdown file. Resolve referenced paths relative to that file. It contains exactly one fenced `repair_contract` JSON object defining the repository path, allowed patch paths, public test argv, network policy, conditional recovery requirement, and patch-size limit. Parse and enforce it; do not discover sibling cases. Input and assets are read-only. All worktrees, probes, and case caches must remain under `--output`.

## Required successful outputs

- `solution.patch`: nonempty UTF-8 unified Git patch, applicable once with `git apply` to the pristine repository. Paths are repository-relative. No absolute/traversal paths, binary patches, symlinks, submodules, or out-of-scope changes.
- `repair_report.json`: exactly `schema_version`, `issue_summary`, `diagnosis`, `files_changed`, `reproduction`, `validation`, `compatibility_notes`, and `limitations`. Schema version is `"1.0"`. `files_changed` exactly matches the patch. `reproduction` and `validation` are arrays of objects with exactly `command` (argv array), `exit_code` (integer), and `observation` (nonempty string). Record only commands actually run.
- `run_report.json`: exactly `status`, `artifact_paths`, `errors`, `runtime_seconds`, `peak_memory_mb`, and `provider_counts`. Status is `success` or `error`; provider counts contain exactly nonnegative integer `gateway_text`, `gateway_image`, `serper`, and `web_retrieval` values, including zeros.

When `repair_contract.recovery` is `required`, also produce `migration_report.json` with exactly `schema_version`, `format`, `artifact`, `command`, `pre_state`, `post_state`, `interruption_observation`, `retry_observation`, `compatibility_observation`, and `rollback_observation`. `format` equals the contract's `recovery_format`. `artifact` is an allowed repository-relative Python file included in the patch. `command` is an argv array beginning `[{python}, {artifact}]`, includes `--self-test` and `{work}`, and may use `{repository}`. The evaluator substitutes placeholders without a shell and executes the artifact from a pristine patched repository.

The recovery self-test must print one final JSON line:

```json
{"schema_version":"1.0","status":"ok","format":"<recovery_format>","checks":{"pre_state":true,"post_state":true,"interruption":true,"retry":true,"compatibility":true,"rollback":true}}
```

These booleans must come from executable probes of the case's actual JSONL, SQLite, segment, checkpoint, or journal format. A hard-coded success object without creating and checking those states is invalid evidence.

Create every recovery fixture beneath `{work}`. The evaluator runs the command with an isolated work directory and audits format-relevant file replacement, SQLite connection, binary-segment, and manifest activity there. A print-only probe, activity outside `{work}`, or ceremonial files unrelated to the named format fails recovery validation even if all six booleans are `true`.

On success, replace stale owned deliverables, list every artifact path, and exit `0`. On failure, exit nonzero and still write `run_report.json` with `status: "error"` and structured errors. Never output credentials, prompts, or private reasoning.
