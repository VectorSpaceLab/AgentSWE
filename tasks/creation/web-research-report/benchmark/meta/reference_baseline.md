# Reference Baseline

## Execution Status

The underlying MiroFlow implementation was **not executed**. The assigned reference was a read-only benchmark directory rather than a runnable pinned checkout, and the underlying framework requires a broad Hydra/tool/provider configuration that does not implement V2's four-artifact contract. Running a floating checkout would not produce a meaningful or comparable baseline and would add external-service and side-effect risk.

The original benchmark itself contains task documents and evaluator materials, not a reference agent. It was therefore analyzed but not invoked as software. No existing submission or prior evaluation was treated as an oracle.

## Inspection Performed

Construction inspected all original benchmark-facing documents, all eleven original case requests, the global rubric and evaluator prompt, the source-repository analysis, reference baseline, construction report, file inventory, and resource contract. A SHA-256 manifest covering all 53 original files was created before V2 edits for the read-only preservation check.

The original source analysis had already pinned and reviewed the repository README, license, dependency metadata, entry points, pipeline/orchestrator code, prompts/configurations, tool documentation, Git tree, issue metadata, public documentation, product material, and arXiv metadata.

## Observed Baseline Contract

The original benchmark required:

- `python run_agent.py --input <input.md> --output <output_dir>`
- `report.md`, `sources.json`, and `run_report.json`
- Markdown `[S#]` citations to a source manifest
- Access-depth disclosure for the two live-web cases
- 180 seconds and 2 GiB per case

Most cases were synthetic and closed-corpus. The live cases required several official source bodies, but no case required a machine-inspectable claim graph, passage quotations, contradiction edges, provenance relations, source-linked calculations, or systematic independent evaluator retrieval.

## Baseline Limitations

- No reference run, latency, provider count, or quality score was collected.
- The underlying reference output is a logged summary/answer/trace rather than the V2 artifact set.
- Reference framework features depend on credentials and services outside the canonical V2 allowlist.
- The original benchmark's 180-second envelope and mostly closed cases do not estimate V2 performance.
- No conclusion, source list, query sequence, or case-specific answer from prior work was copied into V2.

The baseline is descriptive only. It establishes task ancestry and the capability increase, not a mandatory solution or score target.
