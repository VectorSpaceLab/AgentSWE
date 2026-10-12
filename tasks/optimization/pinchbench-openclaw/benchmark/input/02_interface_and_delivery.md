# Interface and Delivery

Implement one executable `run_harness.py` in the submission root. The evaluator invokes:

```bash
python run_harness.py --input <input.jsonl> --output <predictions.jsonl> --run-dir <run_dir>
```

`--input` is UTF-8 JSONL. Every input row contains a stable `id` and task-specific fields. Emit exactly one JSON object per input row to `--output`, preserving the same `id`. The prediction may contain a benchmark-native agent specification and a compact `usage` object, but the evaluator owns authoritative traces, rewards, and resource measurements. Create `run_report.json` under `--run-dir` with `status`, artifact paths, errors, and basic usage counters. Exit 0 only after writing all rows; exit nonzero for an unrecoverable protocol error. Repeated runs must overwrite only the designated output/run directory.

Each row contains `id`, a task instruction, and a workspace asset set. Emit one row preserving `id` with an evaluator-executable OpenClaw agent specification:

```json
{
  "id": "dev_001",
  "agent": {
    "kind": "openclaw",
    "instructions": "reusable system-level working policy",
    "planning_guidance": "optional task planning policy",
    "recovery_guidance": "optional failure recovery policy",
    "verification_guidance": "optional artifact verification policy",
    "tool_profile": "coding"
  }
}
```

`tool_profile` is `coding` or `minimal`; `coding` is the normal productivity-agent surface. The combined specification is limited to 60,000 characters. The evaluator locks the OpenClaw/model/provider versions, task prompt, workspace fixtures, timeout, model-call/token budget, transcript collection, official grader and hybrid judge. It ignores self-reported success or rewards.
