# Request: Metrovale disputed-fare audit

Audit journeys J-101/J-102, J-201/J-202, and J-301 using only `fare_policy.html`, `zone_map.svg`, `signed_route_directive.docx`, and `tap_events.csv`.

For the adult journeys, derive each journey's zone crossing from `zone_map.svg`, calculate fare due under the base, transfer, and peak rules, apply the signed Route N directive where effective, and calculate any refund as charged amount minus fare due. Treat a pair of journey IDs with the same hundred-series as successive boardings by the same rider; use the prior exit and next boarding timestamps to test transfer eligibility. Report each journey and the total adult refund.

For J-301, determine whether the child fare can be reconstructed. Do not substitute the adult fare. Explain how the signed directive conflicts with or supersedes the general policy. Ignore route S and pre-effective-date distractor events.

`answer.md` must show elapsed-time arithmetic, zone classification, fare components, due amount, charged amount, refund, and a concise policy-priority explanation. `claims_and_citations.json` must use atomic claims and preserve both superseded general rules and controlling exceptions as support/contradiction where appropriate. Use HTML element IDs plus byte ranges, SVG element IDs plus normalized geometry, DOCX paragraph/table-cell coordinates, and CSV row/column byte ranges.

Create the full self-contained review package with exact source bytes and reciprocal claim/evidence interaction. Selecting a fare component must visibly target the specific rule or exception and the relevant tap/station evidence; URL fragments and `getState()` must agree with the manifest. Closed corpus: zero search, scrape, browser-network, followed-link, or other external-retrieval calls. The declared model API remains allowed and must be counted separately.
