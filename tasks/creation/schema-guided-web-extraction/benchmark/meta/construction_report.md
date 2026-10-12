# Construction report

## Parameters and design decisions

Case slug is `schema-guided-web-extraction-agent-hard-v4`. This is a Create benchmark with exactly four Builder input documents, two public development cases, and six hidden test cases. The uniform command is `python run_agent.py --input <input.md> --output <output_dir>`. Per-case limits are 600 seconds and 4 GiB, with stricter local-read/navigation/model limits stated in every individual request.

The final artifact contract is `records.json`, `evidence.json`, `interaction_trace.json`, `session_summary.json`, and `run_report.json`, plus referenced PNG screenshots for browser/visual cases. Result quality is a six-dimension final-artifact-only 100-point rubric. Implementation quality is a separate eight-dimension 100-point code rubric using the shared suite IDs and maxima.

## Coverage rationale

| Case | Expected records | State/source scale | Main unseen mechanisms |
| --- | ---: | --- | --- |
| `dev_001` | 24 | four catalog states, 24 details, four feeds/shards | three detail templates, timestamp/file-order disagreement, official/reseller and inventory conflicts |
| `dev_002` | 20 | four directory states, 21 cards, 20 profiles | three profile templates, missing card IDs, homonyms/shared aliases, late stale role |
| `test_001` | 32 | mode transition, five pages, 32 details/PNGs | raster-only lot and weight, four templates, stale duplicate, validity-priority price conflicts in both numeric directions |
| `test_002` | 30 | four batches, 38 current/stale profiles | JSON/HTML/vCard, missing hints, homonyms, stale filenames/page order versus timestamps |
| `test_003` | 25 | initial quarter, all-period mode, four pages, 25 details | alternate DOM structures, kW/MW normalization, reversed amendment sequence |
| `test_004` | 38 | five shuffled snapshots plus identity/certification files | 40 known identities, legacy/serial resolution, two tombstones, two unresolved rows, bidirectional reading conflicts |
| `test_005` | 24 | manifest, three primary shards, fallback, checksum, journal | malformed shard, declared degraded route, out-of-order patches, prohibited mirror, tight reads/calls |
| `test_006` | 36 | four primary pages, annex state, 36 details, five joined sources | legacy crosswalk, stale card, four templates, 12 raster-only marks, condition/inventory/value joins |

Development cases expose the common contract and core mechanisms without revealing hidden combinations. Hidden cases differ structurally and semantically; they are not name/number substitutions of public fixtures.

## Synthetic data provenance

All record names, identifiers, organizations, locations, values, timestamps, HTML/JavaScript/vCard content, recovery bytes, and raster graphics are synthetic benchmark material. No personal/private data or third-party text, images, or source code is embedded.

The PNG labels are metadata-free RGB raster images drawn from a locally authored bitmap alphabet with deterministic paper noise. For OCR-scored fields, required text is absent from runtime HTML, JSON, vCard, filenames, alt text, and PNG metadata. Evaluator-only expected values and image hashes live outside case directories.

## Oracle and package boundary

Every case directory contains only `input.md` and runtime `assets/`. There are no case-local expected outputs, judge notes, rubrics, answer lists, hidden requirements, or construction tools. Every scorable requirement—entity count, schema, identity, workflow states/actions, priority/timestamp/sequence rules, exclusions, OCR, recovery, authorization, and budgets—is stated in that case's `input.md`.

`evaluator/oracles.json` contains expected records, schemas, workflow minima, and OCR image hashes. `evaluator/case_truth.py` independently derives accepted source/action links, semantic conflicts, state order, exclusions, and recovery ordering from actual assets. Both are excluded from the Builder package together with hidden tests, evaluator files, and metadata. Evaluator-only oracle/reference tools cannot enter Builder submissions or runtime packages.

## Feasibility

The corpus is a few megabytes. Individual cases contain 20–38 output records and at most 38 profile/detail observations, 32 raster images, five pagination pages, and a 140-action/read envelope. A competent agent can parse sources locally, use one browser session per browser case, batch OCR or use local OCR/model calls, and finish inside 600 seconds and 4 GiB. No case requires external search, public retrieval, authentication, a live retry server, recursive crawling, unbounded scrolling, large media, PDF rendering, or open-web ambiguity.

The visual labels are intentionally clean enough for reliable local or multimodal OCR; difficulty comes from raster-only availability, per-image source/action/region evidence, and combination with state traversal and reconciliation. The degraded-resource case requires a real malformed parse, fallback checksum verification, ordered journal application, and pre-contact prohibited-target blocking.

## Deterministic evaluator tooling

- `evaluator/audit_benchmark.py`: construction, provenance, source-reference, leakage, truth-coverage, rubric, and hygiene audit.
- `evaluator/case_truth.py`: source-grounded evaluator truth derivation from immutable case assets.
- `evaluator/make_reference_bundle.py`: disposable source-grounded bundle builder for regression tests.
- `evaluator/validate_case.py`: exact record/schema plus semantic source/evidence/action/state/conflict/OCR/report validator.
- `evaluator/test_validator.py`: all-case positive checks, compatibility checks, and adversarial mutations.

## Known limitations

The upstream implementation was not executed. The generated-looking industrial/archive labels are clean scans rather than natural photographs. Deterministic screenshot validation can prove path safety, PNG integrity, uniqueness, source-image non-copying, required state/action linkage, and basic information content, but cannot by itself prove temporal capture; the final-artifact evaluator must visually compare screenshots with active assets/states and use harness process/browser observations. Likewise, reported network/counter truth ultimately depends on harness observations in addition to internal cross-file consistency.
