# Task Goal

## Background

Scientific PDFs encode meaning through prose, formulas, identifiers, tables, figures, captions, footnotes, references, page orientation, and spatial reading order. A translation can be linguistically correct yet difficult to trust when a reader cannot locate the source passage that supports it. Static side-by-side pages are also insufficient when paragraphs move, split across pages, or come from OCR.

## Agent to Create

You must create an **Interactive Scientific PDF Translation Agent**. It receives one Markdown request that identifies one or more local scientific PDFs, source and target languages, a translated-PDF display mode, and optional terminology assets. It produces a faithful translated PDF plus a self-contained local reading viewer that displays the source and translation side by side with geometry-based paragraph correspondence.

## Target Users

The users are researchers, students, engineers, librarians, reviewers, and technical translators who must read, audit, and cite scientific material across languages.

## Core Value

The created agent must let a reader:

- Read a scientifically faithful translation without losing formulas, values, identifiers, tables, figures, captions, notes, or references.
- Inspect source and translated PDF pages together in an offline-capable local viewer.
- Click a paragraph over either rendered PDF and see every source and target fragment of that correspondence highlighted.
- Follow the counterpart automatically even when it lies on another page, in another column, or in a paragraph split across pages.
- Audit a machine-readable alignment record containing page geometry, reading order, confidence, and text digests.
- Recognize uncertain OCR or alignment instead of receiving falsely precise links.

## Final Objective

For every valid request, generate a complete output bundle containing a parseable `translated.pdf`, a machine-inspectable `alignment.json`, a locally bundled viewer, normalized source and target PDF copies used by that viewer, and `run_report.json`. The viewer must work when the output directory is served by an ordinary local static HTTP server with external network access disabled.

## Non-Goals

You are not required to:

- Reproduce any particular repository, framework, prompt, model pipeline, or internal architecture.
- Preserve byte-level PDF objects, original fonts, tag trees, layers, or object identifiers.
- Translate mathematical notation, citation keys, code, URLs, or protected scientific tokens.
- Recover truly illegible content by guessing.
- Build a hosted service, editor, collaboration system, terminology database, or general PDF reader.
- Depend on a CDN, remote font, remote script, remote browser, or complete third-party document-translation service.
- Expose private reasoning or a detailed generation trace.
