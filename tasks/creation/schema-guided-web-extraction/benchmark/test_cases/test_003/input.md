# Stateful all-period permit ledger

Use only `assets/site`. `permits.html` initially shows a seven-row current-quarter view. In a DOM-capable browser activate **All filing periods**, then activate **Continue ledger** three times through all-period page 4/4. Extract 25 distinct permits and visit every filing detail. The row markup and three detail templates intentionally differ from the public catalog fixtures.

Join by filing/permit id. Normalize kW detail values to MW. Apply `amendments/ledger.json` in numeric `sequence` order even though file order is reversed; a later amendment replaces status, capacity, and filed date for its filing. Preserve the superseded detail observation as a conflict.

Return exactly 25 records conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["permit_id","operator","district","status","capacity_mw","filed_at"],"properties":{"permit_id":{"type":"string","pattern":"^PM-[0-9]{4}$"},"operator":{"type":"string"},"district":{"type":"string"},"status":{"type":"string","enum":["approved","pending","suspended"]},"capacity_mw":{"type":"number"},"filed_at":{"type":"string","format":"date"}},"additionalProperties":false}
```

Use trace state IDs `quarter:current`, `periods:all/page:1`, `periods:all/page:2`, `periods:all/page:3`, and `periods:all/page:4`. Trace the current-quarter state, all-period mode transition, all four ledger pages, 25 detail visits, the amendment read, and all ten amendment applications in ascending numeric `sequence` order. Include at least five screenshots covering those five states. Use at most 85 combined case-local file reads and loopback browser navigations. Only case-local sources are authorized.
