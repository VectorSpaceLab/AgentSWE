# Repair request: normalized route changes reuse a stale quote

Repository: `assets/repository`

ParcelRoute 2.4 is used by a checkout service. In one long-lived process, quoting the same account and parcel for `EU` economy and then `APAC` priority can return the first quote's destination, service level, and amount. Equivalent spelling such as `" eu "` must share the canonical `EU` cache entry, but materially different pricing inputs and policy revisions must never alias. Existing tests cover only repeated identical requests.

Trace request normalization, cache identity, pricing policy, service orchestration, and serialization. Preserve `QuoteService.quote(...)`, `quote_request(...)`, the dictionary keys, arbitrary nonempty destination strings, LRU behavior, and defensive result dictionaries. A policy replacement with a different revision must not reuse an old quote. Run the public suite and focused warm-cache probes.

Only `src/parcelroute/` may change. Do not modify tests or project files and do not add dependencies.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/parcelroute/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
