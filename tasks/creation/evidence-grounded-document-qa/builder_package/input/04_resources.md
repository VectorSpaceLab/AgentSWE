# Resources

## 0825 evaluator-owned model transport

During every development and scored case, the harness injects
`AGENTSWE_RESPONSES_BASE_URL` and `GATEWAY_RESPONSES_ENDPOINT`. The created agent
must send every Responses request to that injected endpoint and must not
hard-code or bypass it. The mounted `GATEWAY_API_KEY` is a runtime-only placeholder;
an evaluator-owned broker holds the real credential, overwrites every request to
`model=gpt-5.6-sol` with `reasoning.effort=medium`, and records actual usage.
Direct model endpoints and alternate model credentials are not authorized in
this 0825 protocol. Any provider example below describes the Responses payload
shape; substitute the injected endpoint at runtime.

## Runtime envelope

The evaluation runner provides a Linux environment with Python, local filesystem access to the candidate, one active case, and its designated output directory. The created agent may declare Python dependencies in `requirements.txt`; installation must be non-interactive. Do not assume system PDF, Office, OCR, SVG, image, or browser binaries unless your declared dependencies provide the required capability or your implementation detects them and degrades honestly.

Per case: 600 seconds wall time, 4 GiB RAM, at most 300 combined text/image model requests, and at most 100 image-bearing requests. The case assets are small enough to parse locally; arbitrary truncation that omits required pages, rows, sheets, or figures is not acceptable.

## Model API

When provisioned, GATEWAY exposes model `gpt-5.6-sol` with medium reasoning at `https://gateway.example.com/v1/responses`. The credential is `GATEWAY_API_KEY`; load it from the evaluator-provided environment and never print or persist it. A text request uses the Responses-compatible JSON form:

```json
{"model":"gpt-5.6-sol","reasoning":{"effort":"medium"},"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}]}
```

An image request may add a PNG/JPEG data URL as an `input_image` content item. Send only active-case content or necessary cropped/rendered regions. Never store provider payloads, credentials, or image data URLs in logs or final artifacts. If the model credential is unavailable, fail honestly or use a declared local/general mechanism; do not fabricate a successful run.

## Retrieval policy

`SERPER_TOKEN` may exist in the runner for other benchmarks, but every v4 case is explicitly closed-corpus. `serper_requests` must remain `0`, and `web_retrieval` must record zero search, scrape, browser-network, and followed-link operations. Local browser use for offline `review.html` validation is not web retrieval, but it must block all network requests.

## Filesystem and evaluator tools

The agent may read the active case input and its referenced assets and may write only beneath `<output_dir>`. Evaluators use Python validators included with the benchmark and may use a compatible local Chromium-family browser for the offline interaction harness. The source bundle must make review independent of access to the original case directory.
