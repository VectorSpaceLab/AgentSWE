# Active-organization definition history

## Active organization v1

This definition applied before 2026-05-01 and required three UTC activity dates. It is superseded for the requested report.

## Active organization v2

Effective for reports dated 2026-05-01 onward.

An eligible organization has `organizations.lifecycle_status = 'customer'`, `organizations.is_test = 0`, and a subscription active on 2026-06-30. For each `subscription_versions.subscription_key`, retain the greatest revision. The retained row is active when `subscription_status = 'active'`, `effective_from <= '2026-06-30'`, and `effective_to` is null or after 2026-06-30. Count an organization once in the retained subscription's plan.

For each `activity_event_versions.event_uuid`, retain the greatest revision. An eligible organization is active when, during 2026-05-17 through 2026-06-30 inclusive, it has qualifying activity on at least four distinct `activity_date_local` values and across both feature families `analysis` and `publishing`. Qualifying events are non-automated `analysis_run` and `dashboard_publish`. Exclude `heartbeat`, automated rows, duplicate rows on the same local date, and use neither `loaded_at_utc` nor its calendar date for membership.

Rate is `100 * active organizations / eligible organizations`, displayed to one decimal place. Data and identities are synthetic.
