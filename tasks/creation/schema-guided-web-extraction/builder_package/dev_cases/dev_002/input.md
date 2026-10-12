# Regional contributor directory (development)

Use only `assets/site`. Open `directory.html` in a DOM-capable browser and activate **More contributors** three times, recording all four directory states. There are 20 distinct people plus one stale duplicate directory observation. Visit all profile links; profiles use three DOM templates. Some directory cards omit `data-person`, so take the stable `person_id` from the linked profile rather than guessing from a name.

Keep homonyms and shared aliases separate. Select the newest directory role by `data-as-of` timestamp, not DOM or file position. Profile pages are authoritative for `person_id`, canonical name, city, affiliation, and aliases. The alias `M. Chen` is not an identity key. Preserve the discarded stale role observation as a conflict.

Return exactly 20 records conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["person_id","canonical_name","role","city","affiliation","aliases"],"properties":{"person_id":{"type":"string","pattern":"^RG-[0-9]{3}$"},"canonical_name":{"type":"string"},"role":{"type":"string"},"city":{"type":"string"},"affiliation":{"type":"string"},"aliases":{"type":"array","items":{"type":"string"},"uniqueItems":true}},"additionalProperties":false}
```

Provide evidence for every field and link it to ordered workflow actions. Use trace state IDs `directory:1`, `directory:2`, `directory:3`, and `directory:4`. The trace must include one open, three load-more actions, 20 distinct profile visits, and at least four screenshots covering directory states 1–4. Use at most 70 combined case-local file reads and loopback browser navigations. All public retrieval/search/provider counts must be truthful; only case-local files are authorized.
