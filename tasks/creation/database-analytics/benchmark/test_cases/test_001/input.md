# Marketplace settlement coverage and refund burden

Use only the synthetic local assets; make zero external search or retrieval calls.

- Database: [assets/marketplace.sqlite](assets/marketplace.sqlite)
- Authoritative rules: [assets/settlement_rules.md](assets/settlement_rules.md)

For 2025 H2 and 2026 H1, build a seller-country settlement and merchandise-refund panel. Start from fulfilled, non-test orders in each period and the seller country effective at each order timestamp. For every seller country with at least 100 such eligible orders in both periods, report:

- eligible orders;
- orders whose net successful payment settlement meets the order's required settlement amount;
- settlement rate;
- gross item GMV in USD for those settled orders;
- posted item refunds in USD for those settled orders;
- settled net GMV in USD; and
- merchandise refund burden as posted item refunds divided by gross item GMV.

Then identify the country with the largest settlement-rate deterioration in percentage points and, independently, the country with the largest increase in merchandise refund burden. Preserve one coherent order, seller-version, payment, item/refund revision, period, currency, and test scope across both comparisons.

The database contains corrected seller regions, full-replacement item revisions, partial captures, failed/revised and post-as-of payment events, posted and reversed item refunds, multiple child rows per order, mixed minor digits, and a decoy FX set. Show numeric/cardinality checks that prove one seller country and one item amount per applicable logical key, order-grain settlement qualification before item joins, no payment/refund fan-out, correct refund sign/status, locked conversion, exclusions, and `gross item GMV - posted item refunds = settled net GMV` within USD 0.01.

`result.csv` columns: `seller_country`, `period`, `eligible_orders`, `settled_orders`, `settlement_rate_percent`, `gross_item_gmv_usd`, `posted_item_refunds_usd`, `settled_net_gmv_usd`, `refund_burden_percent`. Include both periods for every qualifying country; sort by seller country, then `2025 H2` before `2026 H1`. Display rates to one decimal place and USD to two decimals, after using unrounded values for comparisons. `chart.json` must be a grouped `bar` using every row, seller country on x, `settlement_rate_percent` on y from zero, and period as series.

`answer.json` must name the largest settlement-rate deterioration and largest refund-burden increase, with both periods' displayed values and changes. `dashboard.html` must be fully offline with working controls `id="country-select"` and `id="metric-view"`; the metric control must switch the visible comparison among settlement rate, refund burden, and settled net GMV while keeping rows/summary synchronized. Provide provenance from a selected country/metric to declared query IDs and local result CSVs. Complete `decision.json`, `lineage.json`, query evidence, and runtime reporting are required.

Role `marketplace_finance_global` has no additional row restriction.
