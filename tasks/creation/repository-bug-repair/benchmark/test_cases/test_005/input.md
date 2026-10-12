# Repair request: tenant-scoped profile writes leave positive and negative cache entries stale

Repository: `assets/repository`

ProfileDirectory combines a revisioned repository, positive/negative cache, event bus, and service. After a warm read, `update_user` returns the new value but `get_user` can return the old profile. A cached miss can also remain `None` after `create_user`, and same-named users in different tenants make the failure inconsistent.

Repair coherence across model, repository, cache, event, service, and batch boundaries. Invalidation is exact to `(tenant_id,user_id)`; same-named users in other tenants remain warm and unchanged; cached misses are invalidated by create; returned nested values are defensive; unchanged reads still hit cache; revisions remain monotonic. `update_many` must be all-or-nothing for one tenant when any requested user or validation fails, and successful batch invalidation must occur only after commit. Do not disable caching or clear the whole cache.

Only `src/profiledirectory/` may change. Run the public suite plus warm positive, warm negative, tenant isolation, defensive copy, rollback, and successful batch probes.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/profiledirectory/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
