# Aurora instrument catalog (development)

Use only `assets/site`. Serve it on an ephemeral loopback port if your browser requires HTTP; no external network is authorized.

Open `index.html` in a DOM-capable browser. The initial page is state 1 of 4. Activate **Load next six** exactly three times until the control disappears, yielding 24 distinct instruments. Visit every linked detail page; the 24 pages deliberately use three different DOM templates and may express mass in grams or kilograms.

Join by `asset_id`. Choose the newest `observed_at` row within each feed regardless of file order. For price, an active official row outranks the reseller feed; when no active official row exists, use the verified reseller row. For inventory, choose the newest observation across both inventory shards. Preserve all losing price and stale inventory observations in `evidence.json.conflicts`.

Return exactly 24 records in `records.json`, each conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["asset_id","name","price_usd","material","mass_g","stock","warehouse"],"properties":{"asset_id":{"type":"string","pattern":"^AU-[0-9]{4}$"},"name":{"type":"string"},"price_usd":{"type":"number"},"material":{"type":"string"},"mass_g":{"type":"integer"},"stock":{"type":"integer","minimum":0},"warehouse":{"type":"string"}},"additionalProperties":false}
```

Use `asset_id` as record identity. Supply field evidence for every non-null field, linked to an ordered trace action. Use trace state IDs `catalog:1`, `catalog:2`, `catalog:3`, and `catalog:4`. The browser trace must show opening state 1, each of the three load actions and resulting states 2–4, and all 24 detail visits. Include at least four screenshots covering states 1–4. Use at most 80 combined case-local file reads and loopback browser navigations. Report actual local reads and zero public retrieval/search calls.
