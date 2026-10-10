# Interface And Delivery

Keep the executable `run_harness.py` in the submission root. The evaluator invokes:

```bash
python run_harness.py --input <input.jsonl> --output <predictions.jsonl> --run-dir <run_dir>
```

Emit exactly one JSONL row for every input row, preserving its `id`. Each row must have exactly:

```json
{
  "id": "dev_001",
  "agent": {
    "kind": "osworld_screenshot_react",
    "instructions": "reusable operating policy",
    "planning_guidance": "reusable planning policy",
    "recovery_guidance": "reusable recovery policy",
    "verification_guidance": "reusable verification policy"
  }
}
```

The four policy fields are plain text. `instructions` must be non-empty; each field is limited to
8,000 characters and the complete agent object to 20,000 characters. No other top-level or agent
field is accepted. In particular, predictions cannot select an import path, model, endpoint, API
key, tool, shell command, plugin, task ID, VM, evaluator, or budget.

Write `run_report.json` in `--run-dir`. The starter demonstrates the required multi-row behavior,
deterministic overwrite semantics, and error reporting. Only `run_harness.py` and
`desktop_policy.py` are editable.
