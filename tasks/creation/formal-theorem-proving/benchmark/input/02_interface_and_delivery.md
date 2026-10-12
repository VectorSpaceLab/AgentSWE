# Interface And Delivery

Installation is a no-op during evaluation: deliver self-contained Python source that runs with the dedicated benchmark interpreter documented in `04_resources.md`, using only its preinstalled packages or submission-local modules. Do not install, update, or mutate packages in the base environment or during a case. The CLI receives exactly one Markdown input file. Resolve that input path and every referenced attachment or repository path against the input file's directory; multiple Lean/source/specification files are handled only as references beneath that active input's `assets/` tree, never as extra CLI inputs. Do not discover or read sibling cases, hidden cases, evaluator policy, metadata, reference material, or unrelated workspace files.

Launch exactly:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

The output directory is the only writable location. Work on an isolated copy beneath it; never edit the supplied repository. On success, atomically replace stale owned deliverables and exit `0`. On failure, still write `run_report.json` with status `error`, remove or clearly exclude stale success artifacts, and exit nonzero. Preserve unrelated files already present in the output directory.

`solution.patch` must be a nonempty UTF-8 unified Git patch relative to the supplied repository root and applicable exactly once with `git apply`. It must contain no binary payload, absolute or traversal path, symlink, submodule, mode-only change, generated Lake cache, or change outside the request's authorized paths.

Also write `proof_report.json`, a UTF-8 JSON object with:

- `schema_version`: exactly `"1.1"`;
- `status`: `"proved"` or `"unprovable"`;
- `targets`: complete array of fully qualified requested declaration names;
- `files_changed`: complete array of repository-relative changed paths;
- `validation`: array of objects with `command`, integer `exit_code`, and concise `observation`;
- `diagnosis`: short evidence summary without private chain-of-thought; and
- `counterexample`: `null` for proved cases, or an object containing `target`, `witness`, `file`, and fully qualified `declaration` for an unprovable case.

For `unprovable`, the added Lean file must import the original target module and the reported declaration must have type `Not <target>`. The witness must be concrete and finite.

The proof report object contains exactly those seven fields. `targets` and `files_changed` are arrays of unique strings in the requested target order and actual repository-relative changed-path order. `validation` is nonempty; every item contains exactly a nonempty string `command`, a JSON integer `exit_code` (not a Boolean), and a nonempty string `observation`. `diagnosis` is a nonempty string. The `counterexample` object, when present, contains exactly the four named string fields.

`run_report.json` contains exactly `status`, `artifact_paths`, `errors`, `runtime_seconds`, `peak_memory_mb`, and `provider_counts`. `status` is exactly `"success"` after a verified proved/unprovable result or `"error"` on failure. On success, `artifact_paths` is exactly the ordered array `["solution.patch", "proof_report.json", "run_report.json"]`, and `errors` is an empty array. Every error entry on failure is a string. `runtime_seconds` and `peak_memory_mb` are finite nonnegative JSON numbers, not Booleans. `provider_counts` contains exactly the keys `gateway_text`, `gateway_image`, `serper`, and `web_retrieval`, each a nonnegative JSON integer rather than a Boolean, using zero when unused. Repeated runs replace the three owned deliverables rather than appending to them.
