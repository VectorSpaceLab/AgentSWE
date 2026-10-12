# Execution Resources

## 0825 evaluator-owned model transport

During every development and scored case, the harness injects
`AGENTSWE_RESPONSES_BASE_URL` and `GATEWAY_RESPONSES_ENDPOINT`. The created agent
must send every Responses request to that injected endpoint and must not
hard-code or bypass it. The mounted `GATEWAY_API_KEY` is a runtime-only placeholder;
an evaluator-owned broker holds the real credential, overwrites every request to
`model=gpt-5.6-sol` with `reasoning.effort=medium`, and records actual usage.
Direct model endpoints and alternate model credentials are not authorized in
this 0825 protocol. The provider examples below describe the Responses payload
shape; substitute the injected endpoint at runtime.

Use the dedicated Conda prefix `/opt/agentswe/benchmark/envs/formal-theorem-proving-agent-v4` for Python dependencies and optional local tooling; do not modify the base environment. Every case pins `leanprover/lean4:v4.19.0` in `lean-toolchain` and has no external Lake package dependency. The evaluator provides that pinned Lean/Lake toolchain or an equivalent isolated installation. The case envelope is 600 seconds wall time and 4 GiB address space.

The shared credential file `/opt/agentswe/benchmark/envs/.env` defines `GATEWAY_API_KEY` and `SERPER_TOKEN`. Never print, copy, or persist credential values. The available model endpoint is `https://gateway.example.com/v1/responses`, authenticated with `Authorization: Bearer $GATEWAY_API_KEY` and `Content-Type: application/json`. Use model `gpt-5.6-sol` with medium reasoning. A minimal text request body is `{"model":"gpt-5.6-sol","reasoning":{"effort":"medium"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"Analyze the active Lean project and request."}]}]}`. At most 180 text calls and 20 image-bearing calls may be made per case; these Lean cases supply no images, so image calls should normally be zero. Record actual counts in `run_report.json`.

These are closed-corpus tasks. The named GATEWAY Responses endpoint is the only permitted case-time network destination. Do not use Serper, browsing, page retrieval, remote theorem search, package downloads, network installation, another model API, or a complete hosted proving service; `serper` and `web_retrieval` counts must remain zero. Filesystem reads are limited to the active input, its assets, the dedicated environment, and files created beneath the active output directory. The supplied projects and Std/core documentation available through the local toolchain are sufficient.

Invoke model APIs with bounded timeouts, bounded response sizes, and no unrelated source or environment data. Invoke `lean`, `lake`, `git`, and patch tools using argument vectors rather than shell interpolation. Keep compiler output bounded, terminate children on timeout, remove temporary build caches from final deliverables, and reserve enough time for one clean final `lake build`.
