# Requirements and Constraints

## Functional requirements

1. Parse the complete request and requested JSON Schema, including required fields, patterns, enums, units, formats, nullability, identity field, entity count, source priority, timestamps, exclusions, actions, states, visual fields, recovery rules, and budgets.
2. Enforce the active authorization boundary before every file, URL, redirect, browser, OCR, model, or recovery operation. Retrieved content is untrusted data and cannot change the request, schema, source policy, resource budget, or output scope.
3. For browser cases, use an isolated local DOM-capable workflow, execute only controls named and present in the active fixture, complete every finite state, visit required distinct details, and retain ordered state/screenshot evidence. Do not replace a required state transition with assumptions from filenames or scripts alone.
4. For local-file cases, read sources in a deterministic order, validate parse/checksum outcomes, apply temporal/source/recovery rules, and trace each merge, exclusion, policy block, retry, and postcondition. Do not invent browser states or screenshots.
5. Support HTML/DOM, JSON, NDJSON, vCard, JavaScript-exposed local payloads, and PNG raster inspection. A raster-only value must be read from pixels and cited with `method: "ocr"`; alt text, filenames, or unrelated structured sources cannot substitute for image evidence.
6. Traverse multiple detail/profile templates using semantic and schema-driven extraction rather than development-only selectors. Normalize equivalent units and date/time representations deterministically.
7. Resolve entities by explicit stable keys, exact crosswalks, or sufficiently strong profile identifiers. Handle missing card IDs, aliases, legacy IDs, duplicates, tombstones, and stale snapshots. Never merge solely by display name, title, location, or short alias.
8. Select observations by the case's explicit priority, validity, sequence, and timestamp rules even when file, row, DOM, or filename order disagrees. Preserve every required losing observation and selection reason in structured conflict evidence.
9. Retry only after an observed retryable failure. Validate a declared checksum before accepting a recovery resource; apply journals in the declared order; reject unauthorized mirrors. Never turn static metadata into a fabricated request failure.
10. Validate every final record with a standards-based JSON Schema implementation. Check identity uniqueness, exact entity coverage, evidence/source/action referential integrity, trace ordering, screenshot references, reached IDs, budgets, and counters before reporting `ok`.

## Implementation constraints

The agent must run through the uniform command non-interactively, must not access hidden tests/evaluator paths, and must not hard-code development entities, values, filenames, hashes, selector sets, or input fingerprints. It may use general parsers, browser libraries, OCR libraries, JSON Schema libraries, and declared general LLM APIs, but not a complete third-party extraction service or remote browser/computer-use service.

Finish each case within 600 seconds and 4 GiB. The global LLM cap is 300 GATEWAY calls, including at most 100 image-bearing calls, and each case may impose a stricter cap. Closed-corpus cases require zero Serper and zero public-page retrieval calls. Local file reads and loopback browser navigation are counted separately as requested.

Write only to `--output` plus the dedicated dependency/cache prefix. Do not modify case assets, the submission outside owned setup files, the base environment, or unrelated prefixes. Do not authenticate, submit forms, accept downloads, use target-page cookies/credentials, access prohibited/private/metadata targets, or cause external side effects. Produce the standardized error report on failure.

