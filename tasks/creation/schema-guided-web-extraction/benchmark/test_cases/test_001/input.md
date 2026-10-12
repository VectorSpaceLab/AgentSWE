# Retained quality lots with raster-only labels

Use only `assets/site`. In a DOM-capable browser open `index.html`, activate **Include retained inspection lots**, then activate **Load next batch** four times through page 5/5. The all-lots view contains 32 distinct `record_ref` identities and one stale duplicate card. Visit every distinct detail link. Detail pages use four DOM templates.

Transcribe `lot_code` and `net_weight_g` from each linked PNG inspection seal using OCR or visual reading. The PNGs are genuine raster images: their text is not duplicated in HTML, JSON, alt text, filenames, or another runtime source. Record OCR evidence with the exact image locator and confidence/uncertainty. Detail pages are authoritative for name and material; use the newest card revision only to resolve the stale duplicate display, and preserve the discarded stale card as an identity conflict.

For price, a `certified.json` row with `signature_status: valid` outranks the distributor even when older or numerically higher/lower. If certification is expired, use the valid distributor row. Preserve both observations and the winner reason for every price conflict.

Return exactly 32 records conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["record_ref","name","price_usd","material","lot_code","net_weight_g"],"properties":{"record_ref":{"type":"string","pattern":"^QL-[0-9]{4}$"},"name":{"type":"string"},"price_usd":{"type":"number"},"material":{"type":"string"},"lot_code":{"type":"string"},"net_weight_g":{"type":"integer","minimum":1}},"additionalProperties":false}
```

Use trace state IDs `mode:current`, `mode:all/page:1`, `mode:all/page:2`, `mode:all/page:3`, `mode:all/page:4`, and `mode:all/page:5`. The ordered trace must show the mode transition, four load-more transitions, all 32 detail visits, and at least six screenshots covering those six states. Use at most 120 combined case-local file reads and loopback browser navigations. Only local loopback serving is allowed; public search/retrieval counts must be zero.
