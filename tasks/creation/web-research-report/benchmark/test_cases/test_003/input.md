# Research Request: Recompute a Colorado River State Water-Use Benchmark

Prepare a 1,800-2,300 word Markdown public-data methods report for a regional water-planning consortium. The consortium wants a reproducible **2015** comparison of Arizona, Nevada, and Utah using USGS water-withdrawal estimates and Census population estimates, to decide which comparisons are sufficiently robust for a later conservation-program study. Do not interpret withdrawals as consumption or as proof of efficiency.

Use evidence publicly available through **2026-06-30**, but keep all numerators and denominators on the requested 2015 basis. If a later revision changes a 2015 value, identify the revision rather than mixing vintages silently.

## Required Calculations

For each state, extract from official USGS tables:

- total freshwater withdrawals;
- thermoelectric freshwater withdrawals;
- irrigation freshwater withdrawals; and
- public-supply freshwater withdrawals.

Use each state's **July 1, 2015 resident-population estimate from Census Vintage 2015** as the primary denominator. Recompute:

1. total freshwater gallons per resident per day;
2. non-thermoelectric freshwater gallons per resident per day;
3. irrigation share of freshwater withdrawals; and
4. public-supply gallons per resident per day.

Normalize `Mgal/day`, gallons/day, people, and percentages transparently. Preserve full precision in intermediate calculations and round displayed results consistently.

## Quality and Uncertainty Analysis

- Verify the meaning and table location of every category, geography, unit, year, and denominator against the methodological report, not only a downloadable cell.
- Reconcile state summary pages, national tables, revised files, or Census vintage differences. Show material conflicts instead of selecting convenient values.
- For each calculated metric, produce a mechanical rounding interval by treating every displayed source input as lying within plus or minus one-half unit of its last shown digit and propagating the endpoints. State clearly that this is **not** a sampling-error or model-error confidence interval.
- Add a denominator sensitivity using a separately published official 2015 population concept, preferably the 2015 ACS one-year population estimate or the population figure embedded in the USGS compilation. Explain its reference period, estimate program, and vintage before comparing it with Census Vintage 2015. Do not treat unlike concepts as a correction or invent uncertainty metadata.
- Explain at least five reasons the three-state ranking is not a policy-performance ranking.
- Recommend which metrics, if any, are fit for peer benchmarking and what data should be collected before program decisions.

## Evidence Depth

Inspect at least **9 distinct source bodies or structured official records** from at least **3 authoritative publishers/data programs**. Include the USGS national table workbook or equivalent machine-readable tables, the USGS methods/report PDF, state-level official pages or tables, Census machine-readable data and its documentation, plus at least one independent methodological critique or peer-reviewed paper. Inspect at least two full PDFs. Search snippets cannot provide values.

## Report Format

Lead with a compact result table and a warning about interpretation. Include a source-vintage table, formulas, inputs, unit conversions, unrounded and rounded results, rounding intervals, conflict resolution, sensitivity findings, and limitations. Cite claims using `[C#]`. Every reported number must be reproducible from a `calculations` entry whose inputs link to exact table cells, records, or PDF passages.
