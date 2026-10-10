# Temporal sensor reconciliation across misleading snapshots

This is a deterministic local-file workflow. Use only `assets/data`; do not invent browser activity. Read all five files under `snapshots/`, `identity/crosswalk.json`, `identity/registry.json`, and `certified-readings.json`.

Resolve `legacy_id` through the crosswalk and use registry serials as an additional exact identity check. Never merge by location. Select each sensor's latest snapshot by embedded `observed_at`, not filename, directory order, or row order. Exclude the two known sensors whose latest snapshot status is `retired`. Also exclude and report the two active-looking rows with neither a resolvable sensor/legacy id nor a registry serial match.

For reading, a certified row with `quality: valid` outranks the latest snapshot even if its value is higher or lower; rejected certification does not. Registry location wins when non-null, otherwise use the latest snapshot location. `observed_at` is the timestamp of the source that supplied the selected reading. Preserve every losing reading and identity/tombstone decision.

Return exactly 38 active records conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["sensor_id","serial","reading","status","location","observed_at"],"properties":{"sensor_id":{"type":"string","pattern":"^SN-[0-9]{4}$"},"serial":{"type":"string"},"reading":{"type":"number"},"status":{"type":"string","const":"active"},"location":{"type":"string"},"observed_at":{"type":"string","format":"date-time"}},"additionalProperties":false}
```

Use trace state IDs `snapshots:read`, `identity:resolved`, `temporal:merged`, and `certification:applied`. The ordered trace must record the eight required local reads, one `merge` decision for each of the 40 known sensor identities, and four `exclude` actions covering the two tombstones and two unresolved rows; merge evidence must expose timestamp, crosswalk, and certification accept/reject decisions. `session_summary.json` must list all 38 reached ids and identify the four excluded observations. Maximum 20 local reads; public network and browser usage are prohibited.
