# Reference Baseline

## Status

The public reference implementation was **not executed** during V2 construction.

## Reason and Environment Assumptions

The assigned source was the read-only original benchmark artifact, not a repository checkout. Reproducing the public project would require cloning/installing a separate large translation and layout stack, downloading model/font assets, selecting a provider, and choosing stable versus experimental modes. It would not establish a baseline for V2's required `alignment.json`, stable overlay DOM, locally bundled viewer, bidirectional paragraph scroll, or honest confidence behavior because those are new benchmark deliverables.

The V2 dedicated Conda prefix was created with Python 3.12 and construction-only PDF/browser validation libraries. No live DeepSeek, GATEWAY, Serper, scrape, or reference-provider request was made. The shared credential file was not opened.

## Evidence and V2 Conformance Fixture

Public reference evidence establishes feasibility of layout-aware scientific PDF translation, monolingual/bilingual output, formula preservation, and geometry processing. The original reference baseline also identifies flowchart disorder, overflow, and reference collision as practical risks. It provides no mandatory output or score.

Separately, construction created a temporary conformant local viewer fixture using one new synthetic development PDF as both source and target, local PDF.js 5.3.93 assets, four alignment records, and the benchmark's required DOM. This was not a translation baseline and is not shipped as a case answer. Playwright Chromium 139 loaded eight nonblank PDF canvases, found eight bounded semantic overlays, performed an off-screen source click and reverse target keyboard activation, propagated reciprocal active state, scrolled both counterpart panes by 2462 pixels, survived a 1280 x 800 resize, and emitted no console error or external request.

## Limitations

No reference translation quality, runtime, cost, alignment quality, browser behavior, or benchmark score was measured. The conformance fixture validates that the specified static-serving and browser-inspection procedure is practical; it does not predict a created agent's translation, segmentation, digest, geometry, or uncertainty quality and must not be treated as a reference answer.
