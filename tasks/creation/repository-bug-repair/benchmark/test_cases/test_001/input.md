# Repair request: concurrent delivery retries duplicate state and audit rows

Repository: `assets/repository`

`DispatchQueue.record_once(delivery_id, worker, payload)` is documented as linearizable and idempotent for threads sharing one queue. Under synchronized retries, multiple calls return `True`, state contains duplicate IDs, audit replay contains duplicate rows, and sequence numbers can become inconsistent. Sequential tests pass. Distinct delivery IDs must continue making progress concurrently; do not hold one global lock across validation, serialization, or journal I/O.

Repair the state/service/journal boundary. Exactly one duplicate caller returns `True`; state and audit remain in identical insertion order; committed sequences are contiguous; snapshot payloads are defensive; replay reconstructs an equivalent queue; a failed journal append must release any reservation without publishing partial state; no spin, sleep, probabilistic retry, or dependency is allowed. Run repeated concurrent probes and the public suite.

Only `src/dispatchqueue/` may change.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/dispatchqueue/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
