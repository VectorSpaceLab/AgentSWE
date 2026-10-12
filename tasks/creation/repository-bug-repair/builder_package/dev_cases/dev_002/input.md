# Repair request: rolling session migration rejects JSON string versions

Repository: `assets/repository`

SessionArchive stores one JSON object per line. Version 1 records may omit `version` or use integer `1` or JSON string `"1"`; `expires` may be an integer, decimal number, or decimal string in seconds. Version 2 uses nonnegative integer `expires_at_ms`. A rolling upgrade now rejects string-version rows, and the existing whole-file migration can destroy the only usable copy if interrupted.

Repair reads and implement an idempotent migration/recovery artifact grounded in this JSONL format. It must preserve unknown fields and metadata, retain v2 records byte-for-byte as Python dictionaries during repeated record migration, atomically install a complete migrated archive, retain an executable rollback copy, recover deterministically from stale or complete temporary state, and allow safe retry. Unsupported versions and malformed/nonfinite/negative expiry values raise `ValueError`. `SessionArchive.load_all()` keeps its public output shape.

Run the public suite and focused mixed-generation, interruption, retry, and rollback probes. Only `src/sessionarchive/` may change. Deliver `migration_report.json` and a Python artifact beneath that prefix. The command must use the required `{python}`, `{artifact}`, and `{work}` placeholders and print the recovery-probe JSON contract described in the builder interface.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/sessionarchive/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"required","recovery_format":"sessionarchive-jsonl-v1-v2","max_patch_bytes":2000000}
```
