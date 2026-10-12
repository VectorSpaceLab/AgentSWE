# Reconcile a Virtualized Payout Ledger

## Case

- Case ID: `test_005`
- Application: [Mariner Payout Reconciliation](assets/serve.py)
- Viewport: 1420 x 820 pixels
- Corpus: closed; use the virtualized local ledger.

## Goal

Filter to `APAC`, status `Needs review`, and settlement dates `2026-07-20` through `2026-07-24`. Within the virtualized results, select exactly rows whose visible variance is at least `$25.00` and whose memo contains `FX`, excluding rows marked `Legal hold`. The qualifying payout IDs are not listed in this request; derive them from rendered rows while scrolling.

Use scoped bulk action `Attach FX evidence`, choose evidence bundle `ECB-2026-W30`, then set status `Ready for approval`. The first apply attempt should reveal that one selected row has a hidden prerequisite; open that row, acknowledge its reconciliation note, return without losing the correct selection, and retry through the bulk review.

## Completion

Every and only qualifying non-held row must have bundle ECB-2026-W30 and status Ready for approval. Preserve all other rows, selection-excluded legal holds, amounts, memos, and regions. Capture decisive evidence showing the reviewed ID list and count before the successful apply.
