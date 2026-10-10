# Shipment SLA rules

An eligible shipment has `shipment_status = 'delivered'` and `is_test = 0`. Exclude cancelled, returned, and test shipments. Assign June or July by shifting `accepted_at_utc` by `origin_utc_offset_minutes`; use half-open local calendar months.

Shipment weight is the sum of all package weights after converting `lb * 0.45359237` to kg. `kg` values need no conversion. Weight band is `light` when total kg is below 25 and `heavy` otherwise.

For each `commitment_rule_versions.rule_key`, retain the greatest revision approved before the shipment's accepted time. Select the retained carrier/service/weight-band rule whose local effective interval contains the origin-local acceptance date. Promised delivery is accepted UTC plus `promised_hours` elapsed hours.

For each `milestone_event_versions.event_key`, retain the greatest revision and use the retained `delivered` timestamp. A breach is actual delivery strictly later than promised delivery. Positive delay hours are `(actual - promised)` in elapsed hours; on-time/early shipments contribute zero.

For each `exception_event_versions.exception_key`, retain the greatest revision and discard retained rows with `is_void = 1`. A breached shipment's primary cause is the remaining exception with greatest severity; break ties by earliest `occurred_at_utc`, then cause code ascending. If none remains, use `uncoded`. Attribute each breached shipment's full positive delay hours to exactly one primary cause.

Rates use eligible shipments as denominator and are displayed to one decimal place. Delay hours aggregate unrounded and display to two decimals. Pair change is July breach rate minus June breach rate in percentage points.

All content is synthetic.
