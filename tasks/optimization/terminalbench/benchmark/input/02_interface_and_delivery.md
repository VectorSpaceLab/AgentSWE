# Interface and Delivery

Implement one executable `run_harness.py` in the submission root. The evaluator invokes:

```bash
python run_harness.py --input <input.jsonl> --output <predictions.jsonl> --run-dir <run_dir>
```

`--input` is UTF-8 JSONL. Every input row contains a stable `id` and task-specific fields. Emit exactly one JSON object per input row to `--output`, preserving the same `id`. The prediction may contain a benchmark-native agent specification and a compact `usage` object, but the evaluator owns authoritative traces, rewards, and resource measurements. Create `run_report.json` under `--run-dir` with `status`, artifact paths, errors, and basic usage counters. Exit 0 only after writing all rows; exit nonzero for an unrecoverable protocol error. Repeated runs must overwrite only the designated output/run directory.

Rows use the frozen terminal-task envelope and preserve `id`. Emit an evaluator-executable live agent specification with `agent.kind=terminus2_live`, reusable `policy`, JSON parser selection, and bounded `max_turns`. The evaluator fixes model/provider/base URL and translates the specification to Harbor/TerminalBench; these fields are not candidate-selectable.
