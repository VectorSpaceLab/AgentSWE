# Omnichannel reporting definition history

## Legacy recognized sales v1

This version applied only to analyses before 2026-07-01. It used UTC order dates and did not incorporate corrected line revisions. It is historical and does not control the requested as-of date.

## Recognized net sales v2

Effective for analyses as of 2026-07-01 onward.

Use the greatest `revision` for each `order_line_versions.line_key`; a revision is a full replacement, not an increment. Include its order only when `orders.order_status` is `paid` or `fulfilled` and `orders.is_test = 0`. Exclude `cancelled`, `pending`, and test orders even when lines or refunds exist.

Assign an order to a reporting day by shifting `orders.placed_at_utc` by that order's store `utc_offset_minutes`. Q2 2025 is local dates from 2025-04-01 through 2025-06-30 inclusive; Q2 2026 is local dates from 2026-04-01 through 2026-06-30 inclusive. Do not use the UTC calendar date or the current analyst timezone.

For each `refund_versions.refund_key`, retain the greatest revision recorded by the analysis as-of timestamp. Subtract a refund only when that retained row has `refund_status = 'posted'`, its `line_key` belongs to an eligible latest line, and `occurred_at_utc <= 2026-07-15T23:59:59Z`. `failed` and `reversed` retained refunds contribute zero. Associate refunds to the product category and market of their logical line/order. Multiple refund keys can belong to one line.

`net_amount_minor` and `refund_amount_minor` are already quantity-extended in their currency's minor unit. Do not multiply by `quantity`. Convert each latest line and posted refund with `fx_rates.rate_set = 'board_2026_07_locked'`: divide by `10 ^ minor_digits`, then multiply by `usd_per_major`. The `latest` set is a decoy. Aggregate unrounded USD and round displayed money to two decimals only at the end.

Recognized net sales are eligible latest-line USD less eligible posted-refund USD. Change is Q2 2026 minus Q2 2025; a decline is negative. A category absent in one period has zero for that period.

All records and definitions are benchmark-authored synthetic data.
