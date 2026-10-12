# Voluntary attrition definition

For year Y, the denominator is allowed employees who were employed at 00:00 UTC on January 1 of Y and whose historical assignment effective at that instant belongs to the reported division/job family. Exclude `worker_type = 'contractor'` and `is_test = 1`. Employment begins when `hire_date < Y-01-01`. A worker is no longer employed if the latest effective termination event is before Y-01-01.

For each `assignment_versions.assignment_key`, retain the greatest revision. Select the retained assignment whose half-open interval `[effective_from, effective_to)` contains January 1. Do not use `worker_current.current_division` or `current_job_family` as historical assignment authority.

For each `employment_event_versions.event_key`, retain the greatest revision. The numerator is denominator members whose first retained effective `termination` event during Y has `reason_code` equal to `resignation` or `retirement`. `layoff`, `transfer`, and `end_of_contract` are not voluntary. Use `effective_date`; `recorded_at_utc` is correction metadata.

Rate is `100 * voluntary departures / start population`, displayed to one decimal place. Division totals use the same worker and event rules before grouping.

All data is synthetic.
