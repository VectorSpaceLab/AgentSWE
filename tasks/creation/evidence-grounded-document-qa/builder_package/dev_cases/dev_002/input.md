# Request: Larkspur cold-chain release

Audit qualification run LC-204 using only `release_protocol.pdf`, `release_log.docx`, and `calibration_register.xlsx` in `assets/`.

For each pallet, apply the signed calibration offset that was valid on the run date to every recorded temperature. Report the six corrected readings, the corrected mean to one decimal place, the maximum corrected reading, and the release/hold decision under protocol v3.1. Explain why a passing mean cannot override an excursion. Confirm that each probe certificate was valid on the test date before relying on its offset.

Then answer whether this packet supports any product shelf-life extension. Abstain explicitly if it does not. Ignore the warehouse distractor, expired probes, packaging color, and courier-route details.

Produce a concise `answer.md` with one table per pallet or one combined comparison table, formulas and units, the final disposition, and an evidence-limits section. `claims_and_citations.json` must contain atomic claims for calibration validity, corrected inputs, means, maxima, decisions, the decision rule, and shelf-life answerability. Use exact PDF page rectangles, DOCX body-paragraph or table-cell coordinates, and XLSX sheet/cell coordinates. Each claim/evidence record must link exactly to `review_manifest.json`.

The self-contained `review.html` must render or expose the original bundled PDF, DOCX, and XLSX bytes offline and provide reciprocal claim/evidence navigation through `window.__reviewTestApi`. Selecting a table-cell claim must identify the precise DOCX/XLSX cell, and selecting a rule must identify a PDF rectangle overlapping the extracted rule text. Closed corpus: no search, scrape, browser-network, followed-link, or other external retrieval. The declared model API remains allowed and must be counted separately.
