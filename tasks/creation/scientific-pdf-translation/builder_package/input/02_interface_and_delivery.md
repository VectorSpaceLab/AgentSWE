# Interface and Delivery Contract

## Environment Preparation

Create and configure the dedicated Conda prefix described in `04_resources.md`. Install every PDF, OCR, font, browser-asset, and validation dependency needed by your implementation there. Once invoked, setup and case execution must require no confirmation and must not alter unrelated environments.

## Uniform Run Command

The submission root must contain `run_agent.py`. Every case is invoked exactly as:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

Do not require another argument, a special working directory, or interactive input.

## Input Contract

`--input` is one UTF-8 Markdown request. Resolve relative asset links against the input file's directory. A request names at least one `.pdf` and may name multiple PDFs plus UTF-8 terminology or instruction files in `.csv`, `.md`, or `.txt` form. Treat all case files as read-only.

When multiple source PDFs are listed, process them in listed order. The translated PDF and the viewer's normalized source PDF must concatenate their pages in that order without an inserted cover or separator unless the case explicitly requests one. Alignment page numbers are one-based global page numbers in that concatenated sequence; retain each record's original `document_id` as well.

Fail rather than guess if no source PDF is identified, a required asset is missing, the source order is ambiguous, or an asset cannot be read safely.

## Output Directory and Repeated Runs

`--output` is the only directory the run may create or modify. Create it if absent. On a repeated run, atomically replace agent-owned required artifacts and the `viewer/` tree. Do not delete unrelated files already present. Do not leave a mixture of old and new viewer files after success or failure.

## Required Successful Output

A successful run must contain all of these paths:

```text
<output_dir>/translated.pdf
<output_dir>/alignment.json
<output_dir>/run_report.json
<output_dir>/viewer/index.html
<output_dir>/viewer/styles.css
<output_dir>/viewer/app.js
<output_dir>/viewer/documents/source.pdf
<output_dir>/viewer/documents/translated.pdf
<output_dir>/viewer/vendor/...
```

`translated.pdf` is the requested scientific translation. `viewer/documents/translated.pdf` must render the same translated document; it may be a byte-identical copy. `viewer/documents/source.pdf` must be the complete source page sequence, combining multiple sources when needed. Files under `viewer/vendor/` must include every third-party runtime asset needed by the viewer. The browser must make no external network request after the output directory has been generated.

A harness will serve `<output_dir>` over loopback HTTP and open `/viewer/index.html`. The viewer need not work directly from a `file://` URL and need not include its own server.

## PDF Contract

The source and translated PDFs must be ordinary parseable, renderable files. Preserve source page order and, unless the case says otherwise, page count, media-box dimensions, and orientation. Monolingual mode replaces in-scope prose with target-language prose. Bilingual mode presents source and target text together in the translated PDF using the relationship requested by the case. Translated text should remain searchable/selectable wherever reasonable; source scans and scientific imagery may remain raster.

## Alignment Artifact

`alignment.json` must be UTF-8 JSON with this observable structure:

```json
{
  "schema_version": "1.0",
  "coordinate_system": "normalized_top_left",
  "digest_algorithm": "sha256",
  "source_documents": [
    {"document_id": "source-1", "input_name": "paper.pdf", "global_page_start": 1, "page_count": 4}
  ],
  "paragraphs": [
    {
      "id": "p-0001",
      "reading_order": 1,
      "confidence": 0.96,
      "status": "aligned",
      "source": {
        "text_digest": "<64 lowercase hex characters>",
        "anchors": [
          {"document_id": "source-1", "page": 1, "bbox": [0.08, 0.16, 0.38, 0.11]}
        ]
      },
      "target": {
        "text_digest": "<64 lowercase hex characters>",
        "anchors": [
          {"page": 1, "bbox": [0.08, 0.16, 0.38, 0.14]}
        ]
      }
    }
  ]
}
```

Coordinates are `[x, y, width, height]` fractions of the displayed crop/media box after page rotation, measured from the rendered page's top-left. Each value must be finite; `x` and `y` must be in `[0,1]`; width and height must be positive; and the box must remain within the page within a tolerance of `0.002`. Page numbers are one-based.

Use one stable record per semantic paragraph, heading, caption, table cell or coherent table block, footnote, and reference entry that is in translation scope. A paragraph split across columns or pages has multiple anchors under the same side and ID. `reading_order` values must be unique positive integers and reflect source reading order. Compute each digest from the normalized semantic text represented by the side's complete anchor list: Unicode NFC, all whitespace runs collapsed to one ASCII space, leading/trailing whitespace removed, then UTF-8 SHA-256. Do not store full source or target paragraph text in `alignment.json`.

`confidence` is numeric from `0` to `1`. Use `status: "aligned"` when both sides are reliable, `"low_confidence"` when a useful but uncertain correspondence exists, and `"unresolved"` when a defensible counterpart cannot be located. An unresolved side may have an empty anchor list and must use the SHA-256 digest of the normalized empty string only when no text was recovered. Do not invent a precise box or high confidence to make coverage appear complete.

## Viewer Interaction Contract

The first screen must be the reading workspace: source on the left, translation on the right, with independent vertically scrollable panes and a compact status/control area. Both panes must render actual PDF pages at readable scale. Paragraph hit regions must be positioned over the corresponding rendered page boxes from `alignment.json`; a detached transcript, list of links, or static screenshot does not satisfy the contract.

Expose these stable, machine-observable elements:

- Scroll containers `#source-pane` and `#target-pane`.
- Each rendered page as `.pdf-page[data-side="source|target"][data-page-number="N"]`.
- Each hit region as `.paragraph-overlay[data-side="source|target"][data-alignment-id="..."][data-page-number="N"]`.
- Every hit region has `role="button"`, `tabindex="0"`, an informative `aria-label`, and geometry matching its declared anchor on that rendered page.
- Activating a source or target hit region by pointer click, Enter, or Space gives every overlay with that ID the class `is-active` and `aria-current="true"`, removes active state from the previous ID, and scrolls the opposite pane until the first counterpart anchor is substantially visible.
- The viewer exposes `[data-viewer-status]` for load/error state and `[data-alignment-confidence]` for the selected record's confidence/status. Low-confidence and unresolved records must be visibly distinguishable and announced; unresolved records must not pretend that a counterpart scroll occurred.

The interaction must still work after resizing a desktop viewport. Overlay placement must scale with the rendered page. Do not cover PDF text with opaque idle overlays. Provide keyboard-visible focus, sufficient highlight contrast, pane labels, page identity, and usable overflow at 1280 x 800 and 1440 x 900 viewports.

## `run_report.json`

On success, write a report shaped as follows. Counts must be actual, including retries; zero is valid when a provider was not used.

```json
{
  "status": "success",
  "artifacts": ["translated.pdf", "alignment.json", "viewer/index.html"],
  "errors": [],
  "usage": {
    "elapsed_seconds": 82.4,
    "source_file_count": 1,
    "source_page_count": 4,
    "output_page_count": 4,
    "alignment_record_count": 37,
    "low_confidence_record_count": 2,
    "external_api_calls": {
      "deepseek": 3,
      "gateway": 2,
      "gateway_image": 2,
      "serper": 0,
      "web_retrieval": 0
    }
  }
}
```

Limit the report to status, artifact paths, concise errors, and basic elapsed/page/alignment/provider usage statistics. Never include credentials, source content, private reasoning, stack traces with unrelated paths, or fabricated counts.

## Exit Codes and Failure Reports

Exit `0` only after all required artifacts parse and the locally served viewer passes an internal load/interaction smoke check. Exit nonzero for invalid input, missing assets, unreadable required content, generation failure, validation failure, or an incomplete viewer. When possible, write an error-form `run_report.json` with `status: "error"`, an empty or valid-only `artifacts` array, actual usage counts, and actionable strings in `errors` before returning nonzero.
