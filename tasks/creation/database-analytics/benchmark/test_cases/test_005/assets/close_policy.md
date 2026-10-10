# January 2026 consolidated close policy

Analysis as-of: 2026-01-31T23:59:59Z. Use rate set `close_2026_01_approved`; `latest` is not approved.

For each `ledger_entry_versions.entry_key`, retain the greatest revision recorded by the as-of timestamp. For each `account_mapping_versions.mapping_key`, first exclude revisions approved after the analysis as-of timestamp, then retain the greatest remaining revision effective on the ledger posting date. Include mapped `revenue` and signed `contra_revenue` amounts in product-family net revenue; exclude `exclude_tax`. The signed ledger amount is authoritative—do not invert a negative contra amount again.

Convert each amount from its `currency_code` minor unit using the approved rate set's `minor_digits` and `usd_per_major`. Aggregate without early rounding.

`rebate_documents` are additional contra-revenue adjustments and have no product code. Each rebate must be allocated across product families using `rebate_allocation_versions` rows approved no later than the analysis as-of timestamp: retain the greatest remaining revision for each allocation key, then use the rows for the same `allocation_group` whose effective interval contains the rebate posting date. The applicable weights for a group/date must cover the product families uniquely and sum to 1.0. Subtract the allocated rebate after currency conversion.

There is no authorized fallback for a used allocation group/date that lacks one complete approved weight set. Such a rebate may not be dropped, assigned to an arbitrary family, inferred from other entries, or spread evenly. A product-family report is publishable only when every material included ledger and rebate amount can be assigned under this policy.

Display USD to two decimals and growth to one decimal after aggregation. If a prior-year family value is zero, growth is null rather than infinite.

All content is synthetic.
