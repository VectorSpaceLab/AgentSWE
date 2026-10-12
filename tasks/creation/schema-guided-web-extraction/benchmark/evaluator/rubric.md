# Final-Artifact Rubric — 100 Points

Score only the active case request/assets, observable runtime/process evidence supplied by the harness, and the candidate's final output bundle. Do not score implementation, prompts, framework, code style, intermediate reasoning, or similarity to any reference repository.

## Validity and failure rules

- The agent cannot launch through the required command: **0**.
- The run exceeds 600 seconds or 4 GiB: **0**.
- No `records.json` is produced: **0**.
- `records.json` or another indispensable artifact is corrupt/unparseable so the target dataset cannot be evaluated: **0**.
- A material authorization/budget violation makes the result untrustworthy: **0**.
- A valid but low-quality or incomplete bundle receives an ordinary rubric score.
- A missing nonessential diagnostic does not automatically force zero.

Every case requires `interaction_trace.json` and `session_summary.json`. Browser/visual cases additionally require usable referenced PNG screenshots; static cases require deterministic local-source/merge/recovery evidence and must not fabricate browser activity. If records are parseable but this case-applicable workflow evidence is absent or nonfunctional, score dimensions normally and cap the final total at **20/100**.

## 1. Schema, identity, and entity coverage — 20 points

**Object:** parsed `records.json` against the case schema and explicitly requested entity set.

- **Full (18–20):** every requested entity appears exactly once under the correct stable identity; every record satisfies required fields, types, patterns, enums, formats, nullability, and additional-property rules; excluded/tombstoned/unresolved observations do not become records.
- **Middle (9–17):** the majority of entities and fields are valid, with isolated missing/extra/duplicate records or schema defects.
- **Low (0–8):** broad coverage loss, duplicate identities, short-name/title/location merges, many schema violations, fabricated entities, or failure to complete pagination/state exposure.
- **Severe errors:** missing later-state entities, treating stale duplicates as new entities, emitting unresolved rows, or replacing stable IDs with display text.
- **Do not penalize:** JSON key order, record order unless the request requires it, or equivalent numeric serialization.

## 2. Field correctness and normalization — 25 points

**Object:** every submitted field compared with actual supplied HTML/JSON/NDJSON/vCard/raster content and declared normalization rules.

- **Full (23–25):** values are exact after required unit/date/type normalization; raster-only text is correctly transcribed; amendments/patches are applied in declared sequence; no unsupported value is invented.
- **Middle (12–22):** central extraction is correct with limited transcription, unit, date, or field-source errors.
- **Low (0–11):** systematic stale values, wrong units, unprocessed patches, fabricated OCR, incorrect formats, or widespread field mismatches.
- **Severe errors:** reading OCR truth from non-image surrogates, treating a corrupt shard prefix as valid records, or selecting values by file/DOM order against the request.
- **Do not penalize:** harmless whitespace/case normalization where semantic equality is clear and the schema permits it.

## 3. Temporal, source-conflict, and recovery decisions — 20 points

**Object:** selected observations plus `evidence.json.conflicts`, exclusions, and recovery decisions.

- **Full (18–20):** stable-key/crosswalk decisions are correct; embedded timestamps, validity, source priority, sequence, tombstones, and fallback checksums are applied exactly; every requested losing observation is retained with source/value/time or validity, selected value, and a concrete reason.
- **Middle (9–17):** final values are mostly correct but some conflicts, stale values, missing IDs, exclusions, or recovery steps are thinly represented.
- **Low (0–8):** silent last-write-wins behavior, filename-order selection, homonym merge, ignored tombstone, accepted rejected/expired source, skipped checksum, fabricated retry, or unauthorized mirror use.
- **Severe errors:** the wrong identity receives another entity's fields, or a recovery decision is asserted without the observed malformed primary and declared local route.
- **Do not penalize:** different but unambiguous conflict JSON organization or concise equivalent reason wording.

## 4. Field-level evidence integrity — 15 points

**Object:** `evidence.json` sources, field evidence, records, conflicts, and uncertainties resolved against supplied assets and trace actions.

- **Full (14–15):** every non-null field has exact value-matching evidence with a real source hash, precise locator, truthful access depth/method, correct record identity, and valid action linkage; OCR fields point to the actual raster; join/inference evidence names supporting observations.
- **Middle (7–13):** evidence is broadly usable but some locators are coarse, conflicts lack one supporting edge, or a small fraction of fields lacks direct action linkage.
- **Low (0–6):** many unsupported fields, invented sources/hashes/locators, wrong record IDs, self-referential evidence, non-image evidence for raster-only truth, or missing provenance arrays.
- **Severe errors:** evidence points to another case, an unauthorized path, a failed resource as fact, or a nonexistent trace action.
- **Do not penalize:** CSS/XPath/semantic/JSON-pointer locator differences that identify the same real source element.

## 5. Workflow state and action evidence — 15 points

**Object:** ordered trace, session summary, screenshot references, completed states, and reached IDs.

- **Full (14–15):** sequence/action IDs are ordered and unique; every required mode/load/detail/OCR/read/merge/recovery/policy action names the real control/source/record target and occurs at least as often as requested; required states and exact reached IDs are closed; screenshots visibly depict the linked browser state or displayed raster source; static cases contain no invented browser evidence.
- **Middle (7–13):** workflow is substantially complete but one state, screenshot, source read, or summary field is missing or weakly linked.
- **Low (0–6):** action trace is generic, unordered, uses placeholder locators, is inconsistent with supplied controls, omits later states/details, fabricates screenshots/browser/retry events, reuses blank/decorative/noise/raw-source images as captures, or summary reached IDs disagree with records.
- **Severe errors:** records claim entities that no trace state/source exposed, or required mode/annex/recovery transitions are absent.
- **Do not penalize:** additional safe diagnostic actions or different redacted locator syntax.

## 6. Operational reporting, safety, and budget truth — 5 points

**Object:** `run_report.json`, counters, errors/limitations, artifact paths, and observable authorization behavior.

- **Full (5):** truthful `ok` status, complete artifact paths, zero hidden errors, accurate model/search/retrieval/local/browser/retry counts, elapsed time within bounds, case budgets respected, prohibited targets blocked, and no secret/raw-token leakage.
- **Middle (2–4):** successful and safe with one minor reporting/counter/path defect.
- **Low (0–1):** misleading status/counters, unjustified retries, omitted prohibited-target decision, fabricated zero usage, over-budget work, or unsafe/unredacted output.
- **Severe errors:** public retrieval in a closed case, contacting the named prohibited mirror, or reporting success after a required postcondition failed.
- **Do not penalize:** zero LLM/OCR-provider calls when reliable local parsing/OCR produced correct evidence.

After scoring all dimensions, apply the 20-point workflow-evidence ceiling if applicable. Report the raw total, any ceiling, and final integer total.
