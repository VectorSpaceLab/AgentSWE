# Q2 recognized net sales decline by market

You are the `commercial_review_analyst` for a synthetic omnichannel retailer. Use only the closed local evidence below. Do not search the web or retrieve any external page.

## Assets and authority

- Database: [assets/commerce.sqlite](assets/commerce.sqlite)
- Reporting definitions: [assets/reporting_rules.md](assets/reporting_rules.md)

The definition document is authoritative for the reporting-version choice, period boundaries, statuses, revisions, refunds, currency conversion, sign, and rounding. Similar database names are not substitutes for those rules.

## Request

For an analysis as of 2026-07-15, compare recognized net sales in local-calendar Q2 2026 with Q2 2025. First identify the two markets with the largest absolute decline in USD. Then, only for those markets, decompose the change by product category and identify the largest negative category contribution for each market.

This is a dependent analysis: the selected definition version, line and refund revision logic, store-local period, order eligibility, test exclusion, locked FX set, units, and signs must remain identical in ranking and decomposition. Show concrete checks for logical-line deduplication, refund revision/status handling, order-line/refund fan-out, local-date assignment near quarter boundaries, minor-unit conversion, excluded statuses/tests, and category-to-market reconciliation within USD 0.01.

## Output

- `result.csv`: one row per selected market/category, sorted by market total change ascending, then category change ascending, then category. Columns: `market`, `product_category`, `q2_2025_usd`, `q2_2026_usd`, `change_usd`.
- `chart.json`: `stacked_bar` using every result row, `market` on x, `change_usd` in USD on y, and `product_category` as series. Preserve negative signs and a zero baseline.
- `answer.json`: name the two markets, their unrounded-derived displayed changes, and the largest negative category for each. Cite the selected definition version and material checks.
- `dashboard.html`: self-contained and offline. Include a working market selector with `id="market-select"`; changing it must update the visible category rows, selected-market total, and largest negative category from the submitted result data. Include a provenance control with `id="show-lineage"` that reveals declared query IDs, local `results/*.csv` paths, and supplied assets for the selected rows.
- `decision.json` and `lineage.json`: record the selected scope/version and map every result column and displayed decision value to declared query IDs and supplied assets.
- Complete query CSV evidence and `run_report.json` under the global interface contract.

No row or column restrictions apply beyond excluding test orders as the reporting definition requires.
