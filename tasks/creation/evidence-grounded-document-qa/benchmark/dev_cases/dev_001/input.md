# Request: Alder Creek Trial B acceptance

Use only the three files in `assets/`. Decide whether Alder Creek storage Trial B passed commissioning, and provide a compact audit table plus a short conclusion.

Answer all of the following:

1. From `commissioning_report.html`, apply the issue-2 inclusion rule and acceptance threshold.
2. From `hourly_dispatch.csv`, calculate Trial B round-trip efficiency from the sum of valid charging and delivered-energy rows. Show included totals, the division, percentage, and pass/fail comparison.
3. Calculate coincident peak-grid-import reduction using the report's definition and the correct CSV row.
4. Use the plotted point and caption in `inverter_map.svg` to identify the operating condition associated with the field efficiency and explain the loss mechanism.
5. State whether annual capacity fade is answerable from this commissioning packet. Do not infer it from efficiency.

Treat warm-up and maintenance rows, Trial A, Alder Ridge, and draft thresholds as distractors. Round energy totals to two decimals, efficiency to one decimal percentage point, and peak reduction to one decimal MW.

Deliver `answer.md` with a direct decision, an evidence/calculation table, and an explicit abstention. In `claims_and_citations.json`, use atomic claim IDs and separate evidence IDs for every input, derived result, decision, explanation, and unanswerable claim. Every locator must be source-native: HTML element ID plus exact byte range, CSV row/column plus exact field byte range, or SVG element ID plus normalized bounding box. Include supporting and contradicting evidence where a superseded or excluded observation matters.

Also deliver the complete offline synchronized review package. Claim selection and evidence selection must visibly update reciprocal highlights, the exact source target, status/confidence, source hash, native locator, and URL fragment through `window.__reviewTestApi`. Bundle the exact bytes of every cited source. Closed corpus: make zero search, scrape, browser-network, followed-link, or other external-retrieval calls. The declared model API remains allowed and must be counted separately.
