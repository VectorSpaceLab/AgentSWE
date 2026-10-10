# Interface and delivery contract

## Repository workflow

Work in a writable copy of `input/repository/`. Use Node.js 24 and the
pinned lockfile:

```bash
npm ci
npm ci --prefix testing/fake-llm-server
npm run ts
```

Do not invoke `tsc` directly. The public cases run end to end through
`python dev_cases/run_dev_case.py`; there is no detached Acceptance
executable.

## Preserved acceptance contract

Keep the Cycle 001 production contracts and semantics:

```text
acceptance:get-run
acceptance:get-latest-run
acceptance:run-updated
acceptance:control-run
acceptance:get-control-receipt
```

`AcceptanceRunSnapshot` remains schema version 1 with durable requirements,
evidence, test runs, changed paths, unresolved and integrity facts, the
repair budget, `generation`, and `controlSequence`. `resume`/`cancel` use
immutable receipts, caller-stable operation identities, same-ID conflict
detection, transactional sequence/generation changes, restart recovery,
ownership checks, and stale-generation fencing. All Cycle 001 field shapes
and transition rules stay as documented in the repository task material.

## Preview session surface

Define trusted typed contracts and the corresponding typed clients:

```text
acceptance:start-preview
  input: AcceptancePreviewCommand
  output: AcceptancePreviewSession

acceptance:get-preview
  input: { appId, chatId, sessionId }
  output: AcceptancePreviewSession | null

acceptance:preview-updated
  event payload: AcceptancePreviewSession
```

`AcceptancePreviewCommand` has exactly:

```json
{
  "appId": 1,
  "chatId": 1,
  "runId": "run-id",
  "operationId": "caller-stable-start-identity",
  "expectedGeneration": 0,
  "target": {"testFile": "e2e-tests/example.spec.ts", "grep": null},
  "presentation": "preview"
}
```

IDs are non-empty strings, numeric IDs are positive integers, the
generation is a non-negative safe integer, and `testFile` is a normalized
repository-relative Playwright spec path. `grep` is a string or null. Reject
extra command fields.

`AcceptancePreviewSession` has exactly:

```json
{
  "schemaVersion": 1,
  "sessionId": "stable-session-id",
  "operationId": "caller-stable-start-identity",
  "appId": 1,
  "chatId": 1,
  "runId": "run-id",
  "generation": 0,
  "target": {"testFile": "e2e-tests/example.spec.ts", "grep": null},
  "presentation": "preview",
  "status": "queued | setup | running | passed | failed | infrastructure | cancelled | invalidated",
  "startedRevision": "64 lowercase hexadecimal characters",
  "currentRevision": "64 lowercase hexadecimal characters",
  "testFingerprint": "64 lowercase hexadecimal characters",
  "lastEventSequence": 0,
  "createdAt": 1,
  "updatedAt": 1,
  "result": null,
  "infrastructureError": null,
  "invalidationReason": null
}
```

`result` is null until a product test result exists, otherwise exactly
`{"passed":0,"failed":0,"skipped":0}` with non-negative integers. The error
and invalidation fields are null or bounded factual strings. Times are
finite non-negative integer Unix milliseconds. Arrays or prose cannot
substitute for the typed session.

Persist the start record as `queued` before acknowledging, then drive the
real isolated test runner with source `agent`, the exact target, and the
headed/preview presentation. Emit durable `setup`, `running`, and terminal
changes with a strictly increasing `lastEventSequence`; renderer events are
notifications, not authority. At most one non-terminal preview session owns
a run generation.

The identical complete start command returns the identical session,
including after a lost response and database/main re-initialization, and
never runs the target twice. Reusing an operation ID with different content
is a classified conflict. A different concurrent start for the same run
generation is classified as busy or conflicting without starting a second
test process. A stale generation is rejected without a session.
App/chat/run ownership is verified before mutation or disclosure.

The existing `tests:stop` path must cancel the owning test process and
persist `cancelled`. On startup, reconcile any durable `queued`, `setup`, or
`running` session left without a live owner into terminal `infrastructure`
with the actual interruption reason; never resume it automatically.

## Workspace revisions

Both revision fields use SHA-256 over one canonical byte stream. Start from
the current Git `HEAD` (`HEAD\0<40-lowercase-hex>\n` or `HEAD\0UNBORN\n`).
Then enumerate, in raw UTF-8 byte order, the tracked files plus the untracked
non-ignored files under `src/`, `e2e-tests/`, and the root package/lock
manifests. For every regular file append `<path>\0<sha256(file-bytes)>\n`;
for every deleted tracked file append `<path>\0DELETED\n`. Paths use
normalized `/` separators. Exclude `.git`, dependency trees, build output,
caches, screenshots, test reports, and evaluator files. Hash that byte
stream to obtain the 64-hex revision.

`testFingerprint` is the SHA-256 over the exact target spec bytes at
admission. Missing, symlinked, traversing, non-canonical, or unreadable
targets are rejected.

## Durable attestation surface

Define and register:

```text
acceptance:get-attestation
  input: { appId, chatId, attestationId }
  output: AcceptanceTestAttestation | null

acceptance:attestation-updated
  event payload: AcceptanceTestAttestation
```

`AcceptanceTestAttestation` has exactly:

```json
{
  "schemaVersion": 1,
  "attestationId": "stable-attestation-id",
  "sessionId": "stable-session-id",
  "appId": 1,
  "chatId": 1,
  "runId": "run-id",
  "generation": 0,
  "startedRevision": "64 lowercase hexadecimal characters",
  "finishedRevision": "64 lowercase hexadecimal characters",
  "testFingerprint": "64 lowercase hexadecimal characters",
  "outcome": "passed | failed | infrastructure | cancelled | invalidated",
  "completedSequence": 3,
  "resultDigest": "64 lowercase hexadecimal characters",
  "createdAt": 1
}
```

Create one immutable attestation in the same durable transaction as the
session's first terminal update. `resultDigest` is the SHA-256 over
deterministic JSON of the normalized terminal counts/status/target, with
object keys sorted, UTF-8 encoded, and without insignificant whitespace.
Duplicate terminal callbacks return/recover the same attestation.
Conflicting, malformed, partial, or post-terminal callbacks cannot replace
it.

Classify the outcome as `passed` only when the started and finished
revisions match, the target fingerprint still matches, the run and
generation still own the race, no stop won the race, and the result contains
at least one executed test. Otherwise classify honestly. Revision or target
drift yields `invalidated`; a setup/runner failure yields `infrastructure`;
a stop yields `cancelled`; an execution failure yields `failed`. Only an
attestation whose outcome is `passed` may add current focused/regression
passing evidence or help a run become `passed`. Invalidation is monotonic:
restoring old bytes later does not restore an invalidated attestation.
Delayed output from an old session/generation is diagnostic only.

The Tests panel shows a **Run in preview** command for the current
acceptance target, shows setup/running/terminal progress using the existing
Tests/Preview conventions, navigates to the preview on entry, and returns
to Tests on completion only if the user has not navigated elsewhere.
Ordinary panel runs, the agent `run_tests`, headless Playwright, app preview,
and non-acceptance modes keep their current behavior.

## Acceptance target identity (normative)

The product must let callers obtain the current acceptance target without
guessing. The `acceptance:get-run` and `acceptance:get-latest-run` snapshots
expose `currentTarget`, whose shape is exactly that of
`AcceptancePreviewCommand.target` (`{"testFile": <normalized repository-relative
Playwright spec path>, "grep": <string or null>}`). When there is no current
target, `currentTarget` is null.

`AcceptancePreviewSession.target` must equal the `target` of the accepted
command field by field; do not fill in defaults, do not normalize `grep` to
null, and do not rewrite `testFile`.

`testFingerprint` is the SHA-256 of the target file's **raw bytes** at the
moment of admission: no encoding conversion, no line-ending normalization,
no trimming, no whitespace ignored, 64 lowercase hexadecimal characters. The
product must not substitute a path, timestamp, random value, or canonical
content digest for it.

## Content addressability of workspace revisions (normative)

`startedRevision`, `currentRevision`, and the attestation's
`finishedRevision` are all derived by the byte-stream algorithm of the
"Workspace revisions" section, therefore:

- A run in which no protected bytes changed: `startedRevision`,
  `currentRevision`, and `finishedRevision` are all equal.
- Protected bytes changed during the run: `finishedRevision` must differ
  from `startedRevision`, and recomputing after the bytes are restored
  returns to the original value.

Constants, random values, timestamps, or auto-incrementing counters are not
revisions.

Reads themselves do not change state: repeated reads of
`acceptance:get-run`, `acceptance:get-latest-run`, `acceptance:get-preview`,
or `acceptance:get-attestation` for the same state must return byte-stable
results. Snapshot fields change only because of real product events
(control commands, session progress, terminal writes, reconciliation).

## Denial ledger (normative)

`AcceptanceRunSnapshot` gains one append-only field:

```text
deniedRequests: DeniedRequest[]
```

`DeniedRequest` has exactly:

```json
{"classification": "stale_generation", "reason": "bounded factual reason",
 "controlSequence": 0, "at": 1}
```

`classification` is a bounded enumeration string; `reason` is a bounded
factual string of at most 200 characters without absolute paths;
`controlSequence` and `at` are non-negative integers. The ledger is appended
in ascending time order and keeps at most 50 entries.

A denied request may only append to `deniedRequests`: no other snapshot
field (`generation`, `controlSequence`, `status`, `requirements`,
`evidence`, `testRuns`, unresolved items, repair budget, attestation
identifier) may change because of one denied request.

Stale-generation control, foreign-owner control, and unauthorized reads are
all denied requests: each denied request appends **exactly one** ledger
entry, with no merging, deduplication, or batching. Ledger entries must not
echo the `runId`, `sessionId`, or `attestationId` string values carried by
the denied request. Neither a denied control receipt nor a denied read may
return another session/run/attestation's `requirements`, `evidence`,
`testRuns`, `sessionId`, `attestationId`, or `deniedRequests`. An
unauthorized read returns a typed null or a typed denial.

## Operation identity conflicts (normative)

When the same `operationId` arrives again:

- Command content byte-identical: return the same session and do not start
  a second test process.
- Command content different (including a different `target`): must refuse
  with a typed conflict (`ok:false` or `applied:false`/`status:"conflict"`),
  must not start a second test process, must not change the existing
  session's `sessionId`, `status`, or `lastEventSequence`, and must not
  create a second attestation.

## Attestation uniqueness and terminal-state monotonicity (normative)

At most one attestation per session. Duplicate terminal callbacks, late
callbacks after cancellation, duplicate Stops, and reconciliation after
restart must all return the same `attestationId`, with `outcome` agreeing
with the session's terminal state. `lastEventSequence` must not decrease in
any read sequence.

## Restart reconciliation (normative)

After `main`/database re-initialization, any durable `queued`, `setup`, or
`running` session must be reconciled into one of `infrastructure`,
`cancelled`, `invalidated`, or `failed` with a bounded reason; it must not
remain non-terminal, must not re-run the target automatically, and must not
be marked `passed`. The `runId` of the same run is unchanged across the
restart, and `generation` and `controlSequence` are monotonically
non-decreasing. Ordinary Build/Ask/Agent chat and Tests list reads must not
change any byte of an Acceptance run snapshot.

## Acceptance target identity (normative)

The product must let callers obtain the current acceptance target without
guessing. The `acceptance:get-run` and `acceptance:get-latest-run` snapshots
expose `currentTarget`, whose shape is exactly that of
`AcceptancePreviewCommand.target` (`{"testFile": <normalized repository-relative
Playwright spec path>, "grep": <string or null>}`). When there is no current
target, `currentTarget` is null.

`AcceptancePreviewSession.target` must equal the `target` of the accepted
command field by field; do not fill in defaults, do not normalize `grep` to
null, and do not rewrite `testFile`.

`testFingerprint` is the SHA-256 of the target file's **raw bytes** at the
moment of admission: no encoding conversion, no line-ending normalization,
no trimming, no whitespace ignored, 64 lowercase hexadecimal characters. The
product must not substitute a path, timestamp, random value, or canonical
content digest for it.

## Content addressability of workspace revisions (normative)

`startedRevision`, `currentRevision`, and the attestation's
`finishedRevision` are all derived by the byte-stream algorithm of the
"Workspace revisions" section, therefore:

- A run in which no protected bytes changed: `startedRevision`,
  `currentRevision`, and `finishedRevision` are all equal.
- Protected bytes changed during the run: `finishedRevision` must differ
  from `startedRevision`, and recomputing after the bytes are restored
  returns to the original value.

Constants, random values, timestamps, or auto-incrementing counters are not
revisions.

## Denial ledger (normative)

`AcceptanceRunSnapshot` gains one append-only field:

```text
deniedRequests: DeniedRequest[]
```

`DeniedRequest` has exactly:

```json
{"classification": "stale_generation", "reason": "bounded factual reason",
 "controlSequence": 0, "at": 1}
```

`classification` is a bounded enumeration string; `reason` is a bounded
factual string of at most 200 characters without absolute paths;
`controlSequence` and `at` are non-negative integers. The ledger is appended
in ascending time order and keeps at most 50 entries.

A denied request may only append to `deniedRequests`: no other snapshot
field (`generation`, `controlSequence`, `status`, `requirements`,
`evidence`, `testRuns`, unresolved items, repair budget, attestation
identifier) may change because of one denied request.

Ledger entries must not echo the `runId`, `sessionId`, or `attestationId`
string values carried by the denied request. Neither a denied control
receipt nor a denied read may return another session/run/attestation's
`requirements`, `evidence`, `testRuns`, `sessionId`, `attestationId`, or
`deniedRequests`. An unauthorized read returns a typed null or a typed
denial.

## Operation identity conflicts (normative)

When the same `operationId` arrives again:

- Command content byte-identical: return the same session and do not start
  a second test process.
- Command content different (including a different `target`): must refuse
  with a typed conflict (`ok:false` or `applied:false`/`status:"conflict"`),
  must not start a second test process, must not change the existing
  session's `sessionId`, `status`, or `lastEventSequence`, and must not
  create a second attestation.

## Attestation uniqueness and terminal-state monotonicity (normative)

At most one attestation per session. Duplicate terminal callbacks, late
callbacks after cancellation, duplicate Stops, and reconciliation after
restart must all return the same `attestationId`, with `outcome` agreeing
with the session's terminal state. `lastEventSequence` must not decrease in
any read sequence.

## Restart reconciliation (normative)

After `main`/database re-initialization, any durable `queued`, `setup`, or
`running` session must be reconciled into one of `infrastructure`,
`cancelled`, `invalidated`, or `failed` with a bounded reason; it must not
remain non-terminal, must not re-run the target automatically, and must not
be marked `passed`. The `runId` of the same run is unchanged across the
restart, and `generation` and `controlSequence` are monotonically
non-decreasing. Ordinary Build/Ask/Agent chat and Tests list reads must not
change any byte of an Acceptance run snapshot.

## What to submit

Write three regular files into the designated output directory:

1. `solution.patch`: a non-empty UTF-8 unified Git patch relative to the
   supplied repository that applies cleanly once.
2. `edit_report.json`: `schema_version: 1`, a non-empty `feature_summary`,
   the exact sorted `changed_paths`, factual `commands` (`command`,
   `exit_code`, `result`), `compatibility_notes`, and `limitations`.
3. `run_report.json`: schema `1.0` with the required top-level resource
   fields shown below.

```json
{
  "schema_version": "1.0",
  "status": "success",
  "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"],
  "errors": [],
  "runtime_seconds": 1.0,
  "peak_memory_bytes": 1,
  "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0}
}
```

`status` is a non-empty string; `errors` is a string array; the runtime is a
non-negative number; peak memory and all three API counts are non-negative
integers. A nested runtime object, `peak_memory_mb`, `resource_usage`, or
any other field cannot substitute for these top-level fields.
`artifact_paths` must equal the ordered array shown. A repeated delivery
atomically replaces the three files. Exit 0 means a complete or honestly
partial valid submission; exit non-zero only when no valid submission
exists.

## Independently verifiable product contract (normative)

The following seven items are obligations the whole Acceptance surface owes
to **every** case, not the adversarial setup of one particular case. They
are all decided from the product's own return values: the evaluator only
re-reads bytes the product has already given, looks at no narrative, and
consumes no additional lower-model turns.

1. **The current acceptance target is obtainable.** The `currentTarget`
   exposed by the `acceptance:get-run` and `acceptance:get-latest-run`
   snapshots must be **equal field by field** to the run's Acceptance
   target. `grep` is the value the product actually uses to filter that
   target: when the target spec file declares only one `test(<title>)`,
   `grep` must be that title itself; `grep` is null only when the product
   truly applies no filtering. Passing off a real filter value as null is
   the same as making the caller guess.
2. **The accepted target enters the session verbatim.**
   `AcceptancePreviewSession.target` equals the accepted command's `target`
   field by field: no defaults filled in, `grep` not normalized to null,
   `testFile` not rewritten.
3. **The fingerprint is the admitted bytes.** Any `testFingerprint` exposed
   by a session or attestation must equal the SHA-256 (64 lowercase hex) of
   the target file's **raw bytes** at the moment of admission. Paths,
   timestamps, random values, and digests of normalized content are not
   fingerprints.
4. **The event sequence does not go backwards.** `lastEventSequence` of the
   same session must not decrease in any read sequence.
5. **Reads are byte-stable.** Two consecutive reads of the same state must
   return exactly the same bytes. An attestation is an immutable record and
   repeated reads at any time are byte-identical; a preview session that
   has entered a terminal state (`passed`, `failed`, `infrastructure`,
   `cancelled`, `invalidated`) must also be byte-identical on repeated
   reads. Reads must not refresh `updatedAt`, must not recompute and write
   back revisions, and must not change array order.
6. **`resultDigest` is a recomputable canonical digest.** The definition is
   fixed as:

   ```text
   resultDigest = SHA-256( UTF-8( canonical_json({
       "result": <the terminal-state result object, or null when there is no product test result>,
       "status": <the terminal-state session status string>,
       "target": <the session's target object>
   }) ) )
   ```

   `canonical_json` is JSON text with object keys sorted in ascending code
   point order, without any insignificant whitespace, and without escaping
   non-ASCII characters. Equivalent implementations: in JavaScript, calling
   `JSON.stringify` on an object whose keys are already sorted; in Python,
   `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
   The evaluator recomputes with the same formula from the observed
   terminal-state session fields and compares character by character. A
   custom digest, a UUID, a timestamp, or a hash of the whole session object
   does not satisfy this item.
7. **The acceptance gate fails closed.** An attestation whose outcome is not
   `passed` must not make the run `passed`, and must not appear in the run
   snapshot as "passing evidence". A green result returned by the runner,
   the existence of an event, the existence of a field, or a process exit
   code is not acceptance passing.

These seven items are checked in the two public cases as well and are
returned item by item in the public-round feedback for calibration during
implementation.
