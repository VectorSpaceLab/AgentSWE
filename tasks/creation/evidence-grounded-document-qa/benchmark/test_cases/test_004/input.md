# Request: Mesa Reservoir sediment estimate

Use `bathymetry_report.pdf`, `survey_points.csv`, `field_correction.html`, and `transect_intensity.png` to estimate deposited sediment for Mesa Reservoir.

Compute each reach volume with the report's average-end-area method using consecutive accepted transects and chainage differences. Sum total volume, apply the controlling dry bulk density, calculate dry mass, and compare it with the management threshold. Show all reach lengths, endpoint areas, formulas, units, rounding, and the final planning decision.

Use `transect_intensity.png` only for the requested qualitative question: identify which left-to-right transect panel shows the highest deposition-intensity class under the key in `field_correction.html`. Do not replace quantitative CSV inputs with pixel estimates. Explain the density revision conflict and state whether contaminant concentration or toxicity can be concluded.

`answer.md` must include a reach calculation table, total volume and mass, threshold decision, qualitative raster observation, conflict note, and justified abstention. `claims_and_citations.json` must separately support every input and derived result. Use PDF rectangles, CSV row/column byte ranges, HTML element IDs plus byte ranges, and a normalized raster region tight enough to identify the relevant panel.

Deliver the complete offline viewer/bundle/manifest with exact source bytes and reciprocal selection. Selecting the density claim must expose both the superseded PDF text and controlling signed correction with correct relations. Closed corpus: zero search, scrape, browser-network, followed-link, or other external retrieval. The declared model API remains allowed and must be counted separately.
