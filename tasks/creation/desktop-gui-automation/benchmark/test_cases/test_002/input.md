# Reschedule a Sterilization Block

## Case

- Case ID: `test_002`
- Application: [Halcyon Procedure Calendar](assets/serve.py)
- Viewport: 1390 x 840 pixels
- Corpus: closed; use the rendered week calendar and dialogs.

## Goal

Move `Sterilizer B validation` from Tuesday 09:00 to Thursday at 14:30 by dragging its 90-minute block into the correct calendar slot. Keep its duration, owner `Priya Shah`, and checklist unchanged. The first drop triggers a cleaning-buffer conflict. In that modal choose `Start after buffer at 14:45`, then change room to `Suite 2` and add note `QA observer confirmed.`

Open the event review. Verify the adjusted time is `14:45-16:15`, then confirm exactly once.

## Completion

The Thursday calendar and event details must show 14:45-16:15, Suite 2, Priya Shah, the original three checklist items, and the exact note. Leave every other block at its original coordinates and values; do not dismiss the conflict by reverting.
