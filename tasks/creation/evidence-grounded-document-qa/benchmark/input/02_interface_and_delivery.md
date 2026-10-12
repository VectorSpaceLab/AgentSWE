# Interface and Delivery

## Installation and launch

From the candidate root, install declared dependencies non-interactively with:

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` is required and may be empty when the implementation needs no third-party packages. Launch every case with:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

The input is UTF-8 Markdown. Relative asset paths resolve from the input file's directory. Read only the active input and its referenced same-case assets. Support multiple files and duplicate basenames only when their relative paths remain distinct.

## Output transaction and exit behavior

Create or atomically replace only these six files inside `<output_dir>`:

1. `answer.md`
2. `claims_and_citations.json`
3. `review.html`
4. `review_manifest.json`
5. `source_bundle.json`
6. `run_report.json`

Do not leave stale owned artifacts from a prior run or write beside the designated output directory. Exit `0` only after all six artifacts and their cross-links validate. On input, parsing, model, serialization, resource, or postcondition failure, exit nonzero and still write `run_report.json` with `status: "error"`, the stage/source-safe errors, and only paths actually produced. Repeated runs may overwrite the six owned files; unrelated pre-existing files under the output directory must not be deleted.

## Claims contract

`claims_and_citations.json` is a UTF-8 JSON object with `schema_version: "4.0"` and a `claims` array. Every claim has:

```json
{
  "claim_id": "stable unique ID",
  "claim": "one atomic material assertion",
  "status": "supported | partially_supported | contradicted | unanswerable",
  "confidence": "high | medium | low",
  "supporting_evidence": [],
  "contradicting_evidence": []
}
```

Every evidence object has unique `evidence_id`, `source`, `locator`, `quote_or_observation`, and `relation`. `relation` is `supports` in the supporting array and `contradicts` in the contradicting array. A calculation claim cites every source input as separate evidence or clearly linked subclaims. Unanswerable claims cite explicit scope/absence evidence when supplied and must not use empty confidence theater.

## Source bundle and manifest

`source_bundle.json` has `schema_version: "4.0"` and a `sources` array. Include every cited source exactly once with its case-relative `source_id`, MIME type, lowercase SHA-256, `encoding: "base64"`, and exact `bytes_base64`. Paths, URLs, excerpts, normalized Office XML, OCR transcripts, and reconstructed quote cards are not substitutes for original bytes.

`review_manifest.json` has `schema_version: "4.0"`, `claim_targets`, and `evidence_targets` objects. Its claim IDs and evidence IDs must exactly equal those in `claims_and_citations.json`. A claim target records `dom_id`, status, confidence, and reciprocal evidence IDs. An evidence target records `dom_id`, `source_dom_id`, `source_id`, `source_sha256`, relation, reciprocal claim IDs, and `native_locator`. The evidence `locator` object in the claims file must equal the manifest `native_locator` object exactly.

Supported native locator kinds are:

- `byte_range`: `byte_start`, `byte_end` into exact UTF-8/text bytes.
- `html_element`: `element_id`, `byte_start`, `byte_end` covering that source element.
- `csv_cell`: one-based `row` and `column`, `header`, `byte_start`, `byte_end` covering the exact field bytes.
- `xlsx_cell`: `sheet` and A1 `cell` (or contiguous `range`).
- `pdf_rect`: one-based `page` and normalized top-left `x`, `y`, `width`, `height`; text evidence must overlap extracted positioned text containing the quote, while visual-region evidence must overlap an actual rendered object.
- `docx_paragraph`: zero-based body `paragraph`.
- `docx_cell`: zero-based `table`, `row`, and `column`.
- `svg_element`: `element_id` plus normalized `x`, `y`, `width`, `height` consistent with the identified element in the SVG viewBox.
- `image_rect`: normalized `x`, `y`, `width`, `height` over the original raster dimensions.

Use the narrowest reproducible native target. Whole-document, whole-page, whole-sheet, or full-image targets are invalid when the evidence is more specific.

## Offline review interaction

`review.html` must be a self-contained offline viewer with no remote scripts, styles, fonts, URLs, or network dependency. It must embed the source bundle or equivalent exact-byte data URIs so it still works after the case directory is removed. Render source content safely; never execute source HTML, SVG scripts, Office active content, PDF actions, or embedded links.

Expose:

```text
window.__reviewTestApi.selectClaim(claimId)
window.__reviewTestApi.selectEvidence(evidenceId)
window.__reviewTestApi.getState()
```

Each selection must visibly update the selected claim, selected evidence, reciprocal highlight set, source pane and native target, status/confidence, evidence relation, source hash, native locator, and URL fragment without network access. `getState()` returns at least `selected_claim_id`, `selected_evidence_id`, `highlighted_claim_ids`, `highlighted_evidence_ids`, `status`, `confidence`, `relation`, `source_id`, `source_sha256`, `native_locator`, and `url_fragment`, all matching the claims graph and manifest. Claim and evidence DOM elements must use the manifest's `dom_id` values and expose selected state with `aria-selected="true"` or `data-selected="true"`. Each source-target element must use its manifest `source_dom_id`, carry matching `data-source-id`, `data-source-sha256`, and JSON `data-native-locator` attributes, and contain a visible control marked `data-source-download="true"` whose data or Blob URL resolves to the exact bundled bytes. Selected claim, evidence, and source-target elements must be genuinely visible, not hidden zero-size test shims. The URL fragment must identify the active claim and evidence. All reciprocal IDs returned by `getState()` must be complete, not merely a representative subset.

## Run report

`run_report.json` contains only `status`, `artifact_paths`, `errors`, `llm_requests`, `gateway_image_requests`, `serper_requests`, `web_retrieval`, `elapsed_seconds`, and optional `peak_rss_mb`/`cpu_seconds`. On success, `artifact_paths` lists all six owned artifacts exactly once and `errors` is an empty string array. `llm_requests` counts every attempted model request, including image-bearing requests; `gateway_image_requests` is the image-bearing subset. Never include credentials, full provider payloads, image data URLs, hidden reasoning, or source content not already required in the output package.
