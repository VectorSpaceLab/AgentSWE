# Inventory recovery and valuation rules

Reporting date: 2026-06-30.

Use `curated_inventory` only when `snapshot_control` for `source_name = 'curated_inventory'` and the reporting date has `load_status = 'COMPLETE'` and `actual_rows = expected_rows`. Otherwise reject the entire curated reporting-date snapshot; do not fill its gaps from raw and do not roll forward a prior date.

The authorized fallback is `raw_inventory_versions` for the reporting date. For each `inventory_key`, retain the greatest revision. Build scope from every active warehouse crossed with every active product, then left join retained raw rows. An absent raw row means zero on hand.

For each `demand_plan_versions.demand_key`, retain the greatest revision approved for the reporting date. Join at report-date/warehouse/SKU grain. Units short are `max(demand_units - on_hand_units, 0)`.

Unit replacement-cost hierarchy:

1. If `products.standard_cost_minor` is positive and its `cost_currency` is present, convert that minor-unit cost.
2. Otherwise use `fallback_costs.csv` for the SKU, selecting the latest `effective_on` not after the reporting date. The CSV value is in major units and its currency is explicit.
3. If a short SKU still lacks an authorized cost, do not invent one; disclose the unresolved exposure and do not claim a complete ranking.

Convert with `fx_rates.rate_set = 'board_2026_07_locked'`; `latest` is a decoy. For database minor-unit costs divide by `10 ^ minor_digits`; CSV fallback values are already major currency units. Exposure is units short times USD unit cost. Aggregate unrounded, display USD to two decimals. Contribution percent is category exposure divided by warehouse exposure, displayed to one decimal; use null if warehouse exposure is zero.

All content is synthetic.
