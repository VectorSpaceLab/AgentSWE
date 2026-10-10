# Interface and Delivery Contract

## Installation and Launch

Create all dependencies in the dedicated Conda prefix from `04_resources.md`. The submission root must contain `run_agent.py` and run without confirmation as:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

The harness starts the case fixture and an evaluator-owned browser/control service before this command. It sets `GUI_CONTROL_URL` and `GUI_CONTROL_TOKEN` to the service's private loopback origin and capability token. `GUI_FIXTURE_URL` is retained as a logical compatibility alias for the control URL; it is not the fixture server and must not be treated as an application HTTP origin. Do not require other arguments or a particular current directory.

## Input

`--input` is one UTF-8 Markdown request. Resolve relative links against its directory. The request identifies the case, required viewport, workflow, completion conditions, and boundaries. Supporting assets may be Markdown, text, CSV, JSON, PNG, JPEG, or SVG. Treat the entire case directory as read-only and do not inspect sibling cases.

Use only the evaluator-owned browser through `GUI_CONTROL_URL` and the documented `/v1/observe` and `/v1/action` API. The evaluator opens the active fixture in a fresh isolated browser context and waits until the page's root element has `data-benchmark-ready="true"`. The service returns fresh rendered PNGs, visible text and control handles; send ordinary click, type/fill, key, select, scroll, drag, wait, and screenshot operations. The application may navigate among same-origin routes. Do not open the fixture origin directly, read fixture source files, call page functions/evaluate/CDP, submit raw application requests, or modify cookies/storage/files to derive or set an action plan.

## Required Output

`--output` is the only writable run directory. Create it if absent. On a repeated run replace these five agent-owned files without deleting unrelated files:

- `automation_result.json`
- `initial_state.png`
- `decisive_step.png`
- `final_state.png`
- `run_report.json`

Screenshots must be valid full-viewport PNGs at the requested dimensions. Capture the initial screenshot after readiness and before interaction. Capture `decisive_step.png` after all prerequisites and review checks but immediately before the decisive final confirmation or scoped mutation. Capture the final screenshot after the application settles and visible success can be verified.

`automation_result.json` must be UTF-8 JSON:

```json
{
  "schema_version": "2.0",
  "case_id": "dev_001",
  "task_status": "completed",
  "task_summary": "Completed the requested local workflow.",
  "viewport": {"width": 1366, "height": 820},
  "observed_final_state": {},
  "action_trace": [
    {"sequence": 1, "action": "click", "target": "visible label", "value": null, "outcome": "ok"}
  ],
  "evidence": {
    "initial": "initial_state.png",
    "decisive": "decisive_step.png",
    "final": "final_state.png"
  },
  "errors": []
}
```

The trace is a concise, honest record of ordinary rendered interactions. `observed_final_state` contains values verified from visible UI, not values obtained from the evaluator bridge or fixture internals.

`run_report.json` contains only `status`, `artifacts`, `errors`, and `usage`. `usage` must include `elapsed_seconds`, `action_count`, `deepseek`, `gateway`, `gateway_image`, `serper`, and `web_retrieval`, with actual integer provider counts. Do not include secrets, source dumps, hidden data, screenshots as base64, or private reasoning.

## Errors and Exit Codes

Exit zero only when all five files are complete and parseable. Exit nonzero for invalid input, absent `GUI_CONTROL_URL`/`GUI_CONTROL_TOKEN`, control-service failure, unsafe or impossible workflow, or invalid output. When possible write an error-form `run_report.json` with `status: "error"`, valid artifact paths if any, and concise actionable errors. Missing optional files may be handled honestly; multiple referenced files are read in the priority stated by the case. Never overwrite case assets.

# Evaluator-Owned Browser Protocol

The launch command and the five output files in `02_interface_and_delivery.md`
are unchanged. The evaluator owns the browser and supplies its viewport from
the active request. Do not launch or attach Playwright, Selenium, CDP, or a
separate browser to the application. Use a normal HTTP client to control the
provided browser. This change applies only to the copied refined benchmark;
historical Candidates that launch their own browser require adaptation.

Use `GUI_CONTROL_URL` as the base URL and send `X-GUI-Control-Token` with the
value of `GUI_CONTROL_TOKEN` on every request. Do not print or save the token.
`GUI_FIXTURE_URL` is a compatibility alias for that same control-service URL,
not a raw application origin. The fixture backend, source, browser cookies,
browser debugging pipe and evaluator state endpoint are unavailable.

`GET /health` returns protocol/readiness metadata. `GET /v1/observe` returns a
fresh observation after readiness. `POST /v1/action` takes one JSON object.
Successful input operations return `accepted`, `action_sequence`, and a fresh
`observation`. Screenshot operations return the captured observation directly.
Every fresh observation replaces prior control handles. Use only handles in
the most recent response, and observe again after an error or stale handle.

An observation includes `protocol`, `observation_id`, `timestamp_utc`, `viewport`,
`text`, `controls`, `regions`, `png_base64`, `png_sha256`, and `png_bytes`.
`controls` have opaque handles, tag/role/label, rendered bounds, visible native
options, checked/disabled status, and visible values. `regions` describe visible
text regions and scrollable viewport regions. Neither includes source scripts,
hidden values, application state objects, canvas answer annotations or an
arbitrary selector/evaluation capability. Image/canvas interpretation must use
the returned pixels. Coordinates are viewport CSS pixels at device scale 1.

Supported JSON commands:

```json
{"operation":"click","handle":"<latest visible handle>"}
{"operation":"click","x":240,"y":310}
{"operation":"fill","handle":"<visible input/textarea>","text":"replacement text"}
{"operation":"type","handle":"<visible input/textarea>","text":"appended text"}
{"operation":"key","key":"Tab"}
{"operation":"select","handle":"<visible native select>","label":"Visible option"}
{"operation":"scroll","x":400,"y":500,"dx":0,"dy":500}
{"operation":"drag","points":[{"x":200,"y":240},{"x":750,"y":520}]}
{"operation":"wait","seconds":1}
{"operation":"wait","seconds":3,"visible_text":"Validation passed"}
{"operation":"screenshot","phase":"initial"}
{"operation":"screenshot","phase":"decisive"}
{"operation":"screenshot","phase":"final"}
```

`fill` uses the browser's native input operation; use `Tab` or ordinary focus
changes where an application commits a field on change. `select` matches a
visible native option label. `drag` accepts 2 to 100 bounded viewport points.
Scroll deltas are bounded to 2,000 pixels per request. Waits are bounded to
10 seconds per request. Text input accepts at most 2,000 characters per request.
These per-command safety bounds do not reduce the registered 600-second run
limit, and commands may be repeated normally.

Supported keys are printable single characters and `Enter`, `Tab`, `Escape`,
`Backspace`, `Delete`, `Space`, `ArrowUp`, `ArrowDown`, `ArrowLeft`, `ArrowRight`,
`Home`, `End`, `PageUp`, `PageDown`, `Control+A`, `Control+C`, `Control+V`,
`Control+Home`, `Control+End`, and `Shift+Tab`. Browser developer tools, arbitrary
navigation, raw HTTP, evaluate, cookie/storage mutation, filesystem/process
operations, extension loading and service shutdown are not commands.

The initial capture is made automatically after fixture readiness and before
Candidate input. Requesting phase `initial` returns those exact bytes, even if
requested later. Save the decoded PNG under `initial_state.png`. Capture
`decisive` immediately before the relevant confirmation or scoped mutation and
save its exact PNG as `decisive_step.png`. Capture `final` after checking visible
completion and save its exact PNG as `final_state.png`. For a no-commit task,
both decisive and final may show the same reviewed draft. PNGs must not be
regenerated, annotated or recompressed. The evaluator matches their hashes to
protected capture receipts; fabricated/cross-run final screenshots fail the
existing wrong-run core-artifact gate. Missing or mistimed non-core phase
evidence remains a rubric quality issue.

The token permits only these rendered commands. It does not authenticate
Candidate claims of state, trace or screenshots. Protected records are produced
by the evaluator-owned fixture/browser and joined to the frozen execution and
output identity by an offline verifier. Submitted action traces remain honest
summaries of actual visible input; do not copy CAPTCHA/code secrets into traces.
