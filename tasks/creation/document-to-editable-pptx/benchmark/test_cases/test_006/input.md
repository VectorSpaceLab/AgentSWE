# Presentation Request: Cobalt Works Automated Inspection Decision

Create an 11-slide, 16:9 board decision deck for Cobalt Works, a fictional precision-components manufacturer. The board must select at most one automated visual-inspection vendor for a two-line deployment, or defer. This is a closed-corpus case: use all supplied sources, make zero web calls, and treat the sources as a noisy diligence room rather than a coherent brief.

## Sources

- [board_question.md](assets/board_question.md)
- [source_priority.md](assets/source_priority.md)
- [signed_quotes.csv](assets/signed_quotes.csv)
- [pilot_weekly.csv](assets/pilot_weekly.csv)
- [quarterly_cashflows.csv](assets/quarterly_cashflows.csv)
- [risk_register.csv](assets/risk_register.csv)
- [executive_emails.md](assets/executive_emails.md)

Follow `source_priority.md` exactly. Surface material conflicts and show why a superseded or informal value was rejected. Do not average or blend conflicting values merely to create a clean answer.

## Required Analysis

- Aggregate pilot detection and false-reject rates from counts, not by averaging weekly percentages. Preserve sample sizes.
- Calculate uptime from scheduled and unavailable hours.
- Recompute each option's three-year quarterly NPV at the board's 9.0% annual discount rate using the cash-flow timing rule. Include initial capex from the signed quote at time zero.
- Recompute payback quarter from cumulative undiscounted net cash flow, or state that payback is outside the modeled horizon.
- Run a savings sensitivity at -15% and +15% while recurring and implementation costs remain unchanged.
- Apply the board's weighted operating score from `board_question.md` using normalized metric definitions in `source_priority.md`. Show both raw metrics and weighted results.
- Make a defensible recommendation or recommend deferral. Explain why the score is not mathematically dispositive.

Record formulas, inputs, unrounded results, rounding, and source IDs in `source_manifest.json`.

## Required Story

1. Decision title with recommendation status, not a generic agenda.
2. One-slide board answer: recommended action, maximum commitment, conditions, and the principal reason not to proceed.
3. Decision criteria, hard gates, and source precedence.
4. Pilot performance with sample sizes in native charts; distinguish detection from false rejects.
5. Throughput and uptime; explain why a dual-axis chart is or is not appropriate.
6. Native table comparing current commercial terms and identifying the superseded number from email.
7. NPV, payback, and sensitivity in editable analytical graphics.
8. Weighted operating score with raw inputs still accessible; label the score as decision support.
9. Risk register summarized by decision impact, owner, mitigation, and trigger; do not imply open risks are closed.
10. Implementation gates and 90-day verification plan with a stop condition.
11. Exact resolution for the board to approve, plus two dissent questions.

## Board Design Constraints

- Use an off-white `#F7F7F4` canvas, graphite `#222426`, electric blue `#1769E0`, signal red `#D63C32`, and acid green `#A6CE39` only for confirmed pass conditions. Use labels and symbols alongside color.
- Use a strict 0.6-inch margin, a 1.25-inch title zone, and a 0.35-inch footer zone. Put `CONFIDENTIAL - BOARD` at bottom left and slide number at bottom right on every slide.
- No nested cards, gradients, photos, vendor logos, trophy/race metaphors, or unqualified green recommendation banners.
- Use native charts, tables, shapes, and connectors; do not paste CSV tables or screenshots.
- Keep body text at least 16 pt and sources at least 9 pt. Extract decision-relevant detail from long tables instead of shrinking it.
- Put calculation and conflict-resolution notes in actual speaker notes on slides 4 through 9.
- Add meaningful alt text to charts, tables, score diagrams, and risk structures; ensure logical reading order.
- Cite all factual and calculated content with `[S#]` and map claims, source conflicts, and calculations in `source_manifest.json`.
- Do not invent layoffs, quality savings, customer penalties, tax effects, depreciation, financing, residual value, or implementation benefits absent from the corpus.

The deck, source manifest, and run report must satisfy the global delivery contract.
