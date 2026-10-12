# Request: Tidepool humidity incident timeline

Reconstruct the H-7 and H-8 alarm state around the 2026-07-02 handoff using only `firmware_release.html`, `operator_handoff.docx`, `event_log.csv`, `dashboard.svg`, and `panel_photo.png`.

Apply the firmware correction to logged humidity, apply any device-specific clock adjustment, and use the stated interval convention to identify each device's alarm start, alarm end, and elapsed duration. Determine which device, if any, remained in alarm for at least 10 minutes. Reconcile the computed state at the photograph timestamp with the left/right red/green panel lights using the handoff mapping. Use the SVG as corroborating plotted evidence, not as a replacement for the event-log arithmetic.

Ignore the salinity-station rows and unrelated firmware. Explain any apparent disagreement caused by uncorrected values or clock alignment. State whether the packet establishes battery failure or another physical root cause.

`answer.md` must include a corrected event timeline, duration arithmetic, dashboard/photo reconciliation, final incident conclusion, and justified root-cause abstention. `claims_and_citations.json` must make correction rules, clock mapping, threshold crossings, durations, visual observations, and uncertainty atomic. Use HTML element IDs plus byte ranges, DOCX paragraph/table-cell coordinates, CSV row/column byte ranges, SVG element IDs plus normalized geometry, and tight normalized raster regions for the left/right indicators.

Deliver the exact-byte source bundle and a self-contained synchronized viewer whose reciprocal API state, visible highlights, source hash, locator, and URL fragment match the manifest for representative timeline and image claims. Closed corpus: zero search, scrape, browser-network, followed-link, or other external-retrieval calls. The declared model API remains allowed and must be counted separately.
