# Repair request: stream merge is slow and folds distinct event IDs

Repository: `assets/repository`

EventMerge combines individually timestamp-ordered finite iterables. Output is globally ordered by `(timestamp_ms, source_index, source_offset)`, retains the first occurrence of each exact case-sensitive `event_id`, and consumes each source once. Correctness tests miss case variants, and a recent large-fan-in workload regressed from interactive latency to tens of seconds with excessive memory use.

Repair planning, identity, and merge orchestration without materializing source iterables or sharing state across calls. On this environment, eight generators totaling 80,000 unique events must finish in under 2.0 seconds and peak below 120 MiB. Preserve stable ties, arbitrary Unicode IDs and payloads, empty source lists, input validation, and detection of an out-of-order source. Do not use probabilistic membership or dependencies. Run the public suite and a measured focused benchmark.

Only `src/eventmerge/` may change.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/eventmerge/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
