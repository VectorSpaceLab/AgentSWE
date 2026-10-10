# Request: North Harbor coating decision

Use only the five assets supplied for this case. Determine the final North Pier coating decision under revision 5.

Required analysis:

1. Read the quantitative and visual acceptance criteria from `inspection_manual.pdf`.
2. Calculate the North Pier area-weighted mean coating loss from `survey_results.xlsx`, replacing any superseded row with the approved value in `signed_amendment.docx`. Show every weighted term, denominator, result in mm, and threshold comparison.
3. Count critical defects in `north_pier.png` using the semantics and counting instructions in `photo_legend.html`. Keep critical markers separate from cosmetic markers.
4. Combine the two criteria into one overall repair/pass decision.
5. Explain the superseded measurement conflict and state whether remaining service life can be concluded.

Ignore South Harbor sheets, cosmetic markers, and marketing forecasts. Round the weighted mean to two decimal places.

`answer.md` must include the direct overall decision, a calculation table, a visual-count table, conflict handling, and a justified abstention. `claims_and_citations.json` must make the formula, each numeric input, corrected value, visual observation, threshold, and final decision separately auditable. Use PDF rectangles that overlap extracted text, XLSX sheet/cell locators, DOCX table-cell locators, HTML element IDs plus byte ranges, and normalized raster-image rectangles for the observed marker regions. Link every claim/evidence ID exactly to the manifest.

The offline viewer must preserve all cited original bytes, display the PNG without network access, and support reciprocal selection for text, table cells, and image regions through `window.__reviewTestApi`. A generic full-image target is not sufficient when a smaller marker region can be identified.
