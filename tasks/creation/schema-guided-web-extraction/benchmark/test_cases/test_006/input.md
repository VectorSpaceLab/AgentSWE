# Mixed-state collection extraction with legacy identities and visual marks

Use only `assets/site`. Open `collection.html` in a DOM-capable browser. Load primary pages 2–4, then activate **Include annex records** to add six annex objects. The final browser state contains 36 distinct objects plus one stale duplicate card. Visit every distinct detail page; four detail DOM templates are used.

Resolve `OLD-MX-*` card identities through `identity/crosswalk.json`; never use title as identity. Choose the newest card revision. Join conservation and inventory by canonical object id. For value, a valid insurance policy outranks the reviewed curator estimate; a lapsed policy does not. Preserve all competing values and the stale card.

For the 12 detail pages whose seal code appears only in a linked PNG archive mark, use OCR/visual reading and cite the image region. Those codes are not duplicated in HTML, alt text, filenames, or structured runtime data. Other detail templates expose the seal in a meta field.

Return exactly 36 records conforming to:

```json
{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["accession_id","title","category","insured_usd","condition_code","storage_zone","seal_code"],"properties":{"accession_id":{"type":"string","pattern":"^MX-[0-9]{4}$"},"title":{"type":"string"},"category":{"type":"string","enum":["drawing","model","textile","photograph"]},"insured_usd":{"type":"number"},"condition_code":{"type":"string","enum":["A","B","C"]},"storage_zone":{"type":"string"},"seal_code":{"type":"string"}},"additionalProperties":false}
```

Use trace state IDs `primary:1`, `primary:2`, `primary:3`, `primary:4`, and `annex:included`. Trace the four primary page states, annex transition, 36 detail visits, crosswalk reads, operational/feed reads, and 12 OCR actions. Include at least six screenshots: one for each of the five browser states and at least one actual displayed raster-mark view linked to an OCR action. Use at most 140 combined case-local file reads and loopback browser navigations and no public network.
