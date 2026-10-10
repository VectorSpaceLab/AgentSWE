# Board Question and Decision Rules

## Resolution Under Consideration

Authorize management to contract with at most one automated visual-inspection vendor for two production lines, subject to a maximum initial capital commitment of **$4.0 million**, production go-live no later than **31 March 2027**, and the verification gates in the approved resolution. The board may defer.

## Hard Gates

- Signed initial capex at or below $4.0 million.
- Contracted implementation plan supports production go-live by 31 March 2027.
- Aggregate pilot detection rate at least 96.0%.
- Aggregate pilot false-reject rate no more than 3.0%.
- Aggregate pilot uptime at least 97.0%.
- No open risk rated Critical in the risk register.

Passing the gates does not require selection; it establishes eligibility for comparative review.

## Weighted Operating Score

For eligible options, calculate a 0-100 decision-support score:

- Detection rate: 35% weight.
- False-reject performance: 20% weight.
- Throughput: 15% weight.
- Uptime: 15% weight.
- Base-case NPV: 15% weight.

Use the normalization rules in `source_priority.md`. Do not add subjective points or change weights. The board recognizes that model risk, implementation risk, and downside asymmetry may outweigh a small score difference.

## Financial Rules

- Annual discount rate: 9.0%.
- Evaluation horizon: 12 quarters, beginning 2027 Q1.
- Initial capex occurs at time zero.
- Quarterly net cash flow = gross savings - recurring cost - implementation cost.
- Quarterly discount rate = `(1 + annual rate)^(1/4) - 1`.
- Payback uses cumulative undiscounted cash flow including time-zero capex.
- Sensitivity changes gross savings by -15% and +15%; other cash-flow fields stay unchanged.
- Exclude tax, depreciation, financing, residual value, working capital, and head-count savings.

Display currency in millions to one decimal place in the deck; preserve whole-dollar inputs and unrounded results in the manifest.
