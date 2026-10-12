# Multi-format registry identity reconciliation

Use only `assets/site`. Open `people.html`, activate **Load another batch** three times through batch 4/4, and follow all 38 listed current-or-stale profile observations. The directory resolves to 30 current people after stale selection. Profiles use JSON, HTML, and vCard formats; several cards omit the registry hint.

Resolve identity from the profile `registry_id`/`data-registry-id`/vCard `UID`, never from a short name or shared alias. Choose the newest profile and directory role by their embedded timestamps, not filename, page, or DOM order. Keep all homonyms separate, including people sharing `M. Chen`. Preserve stale profile and role values in conflict evidence.

Return exactly 30 current records conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["person_id","canonical_name","role","city","affiliation","aliases"],"properties":{"person_id":{"type":"string","pattern":"^PX-[0-9]{4}$"},"canonical_name":{"type":"string"},"role":{"type":"string"},"city":{"type":"string"},"affiliation":{"type":"string"},"aliases":{"type":"array","items":{"type":"string"},"uniqueItems":true}},"additionalProperties":false}
```

Every field must have source/locator/action evidence. Use trace state IDs `batch:1`, `batch:2`, `batch:3`, and `batch:4`. The trace must show four directory states, all unique current and stale profile visits, and at least four state screenshots. Missing hints and stale duplicates must be explained in identity/conflict evidence. Use at most 100 combined case-local file reads and loopback browser navigations. No source outside the case assets is authorized.
