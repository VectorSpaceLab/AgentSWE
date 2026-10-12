# Repair request: interrupted SQLite migration advertises the wrong schema

Repository: `assets/repository`

TenantConfig uses SQLite `PRAGMA user_version=1` with `tenant_settings(tenant_id, timeout_seconds, retries, payload_json)` and version 3 with integer `timeout_ms`, `label`, `extension_json`, `revision`, plus migration state. The current migration advances the version before durable replacement and rejects decimal seconds. An interruption can leave `user_version=3` with only the renamed v1 table.

Implement a transactionally safe, idempotent v1-to-v3 migration and executable recovery/rollback probe using the actual SQLite format. Legacy timeouts accept finite nonnegative integer, float, or decimal string seconds and round half-up to milliseconds; booleans, negative/nonfinite/malformed values fail with `ValueError`. Preserve tenant rows, retries, label, all other payload keys, compatibility reads during rolling deployment, and a rollback path to a readable v1 database. Recover all plausible partially migrated table/version combinations without data loss, support repeated/concurrent invocation, and leave `PRAGMA integrity_check` clean.

Only `src/tenantconfig/` may change. Run the public suite and migration, injected-interruption, retry, concurrent invocation, compatibility, integrity, and rollback probes. Deliver the conditional migration report and executable probe artifact.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/tenantconfig/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"required","recovery_format":"sqlite-tenantconfig-v1-v3","max_patch_bytes":2000000}
```
