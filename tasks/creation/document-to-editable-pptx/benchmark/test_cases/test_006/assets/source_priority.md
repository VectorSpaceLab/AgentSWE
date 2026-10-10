# Source Priority and Normalization Rules

## Precedence

1. `board_question.md` controls decision gates, weights, horizon, formulas, and constraints.
2. `signed_quotes.csv` controls current commercial terms, capex, and contracted schedule. Email quote values are superseded.
3. `pilot_weekly.csv` controls measured pilot performance. Aggregate from raw counts and hours; executive email percentages are informal summaries.
4. `quarterly_cashflows.csv` controls modeled savings and costs, except time-zero capex comes from the signed quote.
5. `risk_register.csv` controls risk status, rating, owner, mitigation, and trigger.
6. `executive_emails.md` supplies concerns and diligence questions only. It cannot override higher-priority values or convert a proposal into a commitment.

Show material conflicts between high- and low-priority sources and explain the applied rule.

## Metric Calculations

- Detection rate = `defects_detected / known_defects` across all pilot weeks.
- False-reject rate = `good_parts_rejected / known_good_parts` across all pilot weeks.
- Throughput = `parts_inspected / run_hours` across all pilot weeks.
- Uptime = `(scheduled_hours - unavailable_hours) / scheduled_hours` across all pilot weeks.

Do not average weekly percentages. Do not drop a weak week as an outlier.

## Score Normalization

Clamp each component to 0-100 before applying its weight:

- Detection score = `(aggregate detection rate - 0.94) / (0.99 - 0.94) * 100`.
- False-reject score = `(0.04 - aggregate false-reject rate) / (0.04 - 0.01) * 100`.
- Throughput score = `(aggregate parts per hour - 700) / (1000 - 700) * 100`.
- Uptime score = `(aggregate uptime - 0.95) / (0.995 - 0.95) * 100`.
- NPV score = `(base-case NPV - minimum eligible NPV) / (maximum eligible NPV - minimum eligible NPV) * 100`. If only one option is eligible, its NPV score is 50 and the deck must disclose that convention.

Weighted operating score = sum of each clamped component score multiplied by its board weight. Display component and total scores to one decimal place.
