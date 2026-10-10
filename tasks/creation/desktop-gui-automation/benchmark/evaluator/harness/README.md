# Evaluator Harness 2.1

Run one active case with:

```bash
python evaluator/harness/run_case.py \
  --case-dir test_cases/test_001 \
  --submission-dir /absolute/path/to/submission \
  --python /absolute/path/to/dedicated-prefix/bin/python \
  --output /absolute/path/to/output/test_001 \
  --evidence-dir /absolute/path/to/evidence/test_001 \
  --dotenv /opt/agentswe/benchmark/envs/.env
```

The output and evidence directories must be new or otherwise disjoint. The
candidate receives a staged `input.md` and the proxy URL only. Fixture Python,
HTML, the backend URL, and the evaluator token are not included in its command
or environment.

The evidence directory contains:

- `execution.json`: command identity, timing, RSS, limits, fixture hashes, and candidate-declared provider use.
- `active_case_manifest.json`: source/staged request hashes and excluded fixture assets.
- `audit/initial_state.json` and `audit/final_state.json`: sanitized application-owned state and trace.
- `audit/requests.jsonl` and `audit/request_summary.json`: fixture-origin request audit and protected-route attempts.
- `artifact_validation.json`: presence, hashes, JSON parsing, and PNG dimensions.
- `partial_state_diagnostic.json`: state deltas and workflow progress for diagnosis only.
- `process/`: candidate and fixture stdout/stderr.

`partial_state_diagnostic.json` never changes the formal validity gates or the
100-point rubric. A missing `automation_result.json` or final screenshot remains
a zero under `evaluator/rubric.md` even when partial state shows useful progress.

The proxy observes only fixture-origin traffic. Provider counts remain
candidate-declared unless the outer execution platform supplies network
telemetry. Active-case staging removes ordinary relative filesystem access to
fixture source, but it is not a substitute for an outer filesystem sandbox that
prevents arbitrary reads of known host paths.

Run fixture and staging smoke tests with:

```bash
python -m evaluator.harness.self_test
```
