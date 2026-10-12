# Marketplace settlement and net GMV rules

Analysis as-of timestamp: 2026-07-31T23:59:59Z.

Use order timestamps in half-open UTC periods: 2025 H2 is `[2025-07-01T00:00:00Z, 2026-01-01T00:00:00Z)` and 2026 H1 is `[2026-01-01T00:00:00Z, 2026-07-01T00:00:00Z)`.

For each `seller_region_versions.region_version_key`, retain the greatest revision approved by the analysis as-of timestamp, then use the retained seller country whose effective interval contains the order timestamp. Do not use seller names or seller ID modulo patterns as geography.

For each `payment_event_versions.payment_event_key`, retain the greatest revision recorded by the as-of timestamp. At order grain, net successful settlement is succeeded `capture` amount minus succeeded `refund` amount; failed retained events contribute zero. An order qualifies only when `order_status = 'fulfilled'`, `is_test = 0`, and net successful settlement is at least `required_settlement_minor`. Qualify one row per order before joining items.

For each `order_item_versions.item_key`, retain the greatest revision. `extended_net_minor` is already quantity-extended. For each `item_refund_versions.item_refund_key`, retain the greatest revision; subtract only retained `posted` item refunds. Failed/reversed retained refunds contribute zero. Settled net GMV is qualified latest-item value less posted item refunds associated with those items. Payment refunds determine settlement eligibility; item refunds determine merchandise-value reduction, so do not subtract either twice.

All monetary values for an order use the order currency. Convert with `fx_rates.rate_set = 'board_2026_07_locked'` after dividing by `10 ^ minor_digits`; `latest` is a decoy. Aggregate unrounded USD, display two decimals. Change is 2026 H1 minus 2025 H2; negative is decline. Missing-period family values are zero.

All content is synthetic.
