# Interface and delivery

## Submission

Submit exactly these top-level files:

- `solution.patch`: a non-empty UTF-8 unified Git patch relative to the
  supplied repository that applies with `git apply` exactly once.
- `edit_report.json`: a JSON object with `schema_version: "1.0"`,
  `feature_summary`, `changed_paths`, `commands_run`, `compatibility_notes`,
  and `limitations`.
- `run_report.json`: a JSON object with `schema_version: "1.0"`, `status`,
  `artifact_paths`, `errors`, `runtime_seconds`, `peak_memory_mb`, and
  non-negative integer counts `deepseek`, `gateway`, `gateway_image`,
  `serper`, and `web_retrieval`.

Reports are evidence, not assertions of correctness. They must be factual,
contain no credentials or fixture secrets, and list every changed path.

## Frozen TypeScript adapter

Create `src/api/recovery/recovery-evaluator-adapter.ts`. Export all of the
names below. The evaluator uses this stable adapter for deterministic
discovery and observes delegation separately through the production
services, store, event stream, and UI. Private organization is
unconstrained.

```ts
export type EffectKind =
  | "read_only"
  | "local_write"
  | "commit"
  | "remote_create"
  | "migration"
  | "non_retryable";
export type EffectStatus =
  | "queued"
  | "claimed"
  | "applied"
  | "completed"
  | "uncertain"
  | "blocked"
  | "cancelled";
export type RetryClass =
  | "replay_safe"
  | "already_completed"
  | "reconcile"
  | "blocked";
export type TerminalState = "active" | "paused" | "cancelled" | "completed";
export type RecoveryBoundary =
  | "none"
  | "ready"
  | "queued"
  | "executing"
  | "replay"
  | "reconcile"
  | "blocked"
  | "conflict"
  | "paused"
  | "cancelled"
  | "completed"
  | "corrupt"
  | "forbidden";
export type RecoveryCrashBoundary =
  | "after_enqueue"
  | "after_claim"
  | "after_effect"
  | "after_effect_persisted"
  | "after_settle";
export type InspectionLevel = "status" | "support";
export type InspectionAccess = "internal" | "status" | "support" | "denied";

export interface RecoveryScope {
  backendId: string;
  conversationId: string;
}

export interface RecoveryAdapterOptions {
  storage: Storage;
  now: () => number;
  instanceId: string;
  maxSerializedBytes?: number;
  maxRetainedRevisions?: number;
  defaultClaimTtlMs?: number;
  crashAfter?: RecoveryCrashBoundary | null;
}

export function createRecoveryEvaluatorAdapter(
  options: RecoveryAdapterOptions,
): {
  createCheckpoint(input: CreateCheckpointInput): RecoveryInspection;
  enqueueEffect(input: EnqueueEffectInput): RecoveryInspection;
  recordEffect(input: RecordEffectInput): RecoveryInspection;
  acquireRecoveryLease(input: AcquireLeaseInput): RecoveryInspection;
  recoverConversation(input: RecoverInput): RecoveryInspection;
  pause(input: OwnerTransitionInput): RecoveryInspection;
  resume(input: OwnerTransitionInput): RecoveryInspection;
  cancel(input: OwnerTransitionInput): RecoveryInspection;
  issueInspectionGrant(input: IssueInspectionGrantInput): IssuedInspectionGrant;
  revokeInspectionGrant(input: RevokeInspectionGrantInput): RecoveryInspection;
  inspectRecovery(
    scope: RecoveryScope,
    limits?: RecoveryInspectionLimits,
  ): RecoveryInspection;
  inspectRecoveryAuthorized(
    input: AuthorizedInspectionInput,
  ): RecoveryInspection;
  ingestRuntimeEvent(input: RuntimeRecoveryEvent): RecoveryInspection;
  executeEffect<T>(
    input: ExecuteEffectInput,
    execute: () => Promise<T>,
    reconcile?: (idempotencyKey: string) => Promise<T | undefined>,
  ): Promise<ExecuteEffectResult<T>>;
};

export function recoveryStorageKey(
  backendId: string,
  conversationId: string,
  revision: number,
): string;
export function legacyRecoveryStorageKey(
  conversationId: string,
  revision: number,
): string;
```

The inputs and outputs specified below are exact. Additional bounded fields
are allowed, but the listed fields and enumeration values keep these
meanings.

```ts
export interface CreateCheckpointInput extends RecoveryScope {
  eventCursor: number;
  workspace: { digest: string; version: string | null };
  expectedRevision?: number | null;
  progress?: { phase: string; summary?: string };
  now?: number;
}
export interface EffectClaim {
  token: string;
  deliveryId: string;
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  attempt: number;
  claimedAt: number;
  expiresAt: number;
}
export interface RecoveryEffect {
  deliveryId: string;
  effectId: string;
  idempotencyKey: string;
  toolName: string;
  kind: EffectKind;
  status: EffectStatus;
  retryClass: RetryClass;
  attempt: number;
  runGeneration: number;
  enqueueRevision: number;
  claim?: EffectClaim | null;
  appliedAt?: number | null;
  resultMetadata?: unknown;
}
export interface EnqueueEffectInput extends RecoveryScope {
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  expectedRevision: number;
  eventCursor: number;
  effect: Omit<
    RecoveryEffect,
    | "status"
    | "attempt"
    | "runGeneration"
    | "enqueueRevision"
    | "claim"
    | "appliedAt"
  >;
  now?: number;
}
export interface RecordEffectInput extends RecoveryScope {
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  expectedRevision: number;
  eventCursor: number;
  effect: RecoveryEffect;
  workspaceDigest?: string;
  now?: number;
}
export interface AcquireLeaseInput extends RecoveryScope {
  ownerId: string;
  expectedRevision: number;
  ttlMs: number;
  now?: number;
}
export interface RecoverInput extends RecoveryScope {
  ownerId: string;
  expectedRevision: number;
  workspace: { digest: string; version: string | null };
  ttlMs: number;
  now?: number;
}
export interface OwnerTransitionInput extends RecoveryScope {
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  expectedRevision: number;
  now?: number;
}
export interface RuntimeRecoveryEvent extends RecoveryScope {
  eventId: string;
  eventCursor: number;
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  expectedRevision: number;
  type:
    | "effect_completed"
    | "effect_failed"
    | "workspace_reconciled"
    | "paused"
    | "resumed"
    | "cancelled";
  effect?: RecoveryEffect;
  now?: number;
}
export interface ExecuteEffectInput extends EnqueueEffectInput {
  claimTtlMs?: number;
}
export interface ExecuteEffectResult<T> {
  executed: boolean;
  reconciled: boolean;
  suppressed: boolean;
  result?: T;
  deliveryId: string;
  claimToken?: string;
  inspection: RecoveryInspection;
}
export interface RecoveryCompaction {
  generation: number;
  compactedThroughRevision: number;
  compactedThroughEventCursor: number;
  anchorDigest: string | null;
}
export type RecoveryAuditType =
  | "enqueued"
  | "claimed"
  | "effect_applied"
  | "settled"
  | "claim_expired"
  | "stale_settlement"
  | "duplicate_delivery"
  | "duplicate_event"
  | "reordered_event"
  | "late_terminal"
  | "grant_issued"
  | "grant_revoked"
  | "grant_denied"
  | "migrated"
  | "compacted"
  | "conflict";
export interface RecoveryAuditEntry {
  type: RecoveryAuditType;
  at: number;
  instanceId?: string;
  ownerId?: string;
  leaseEpoch?: number;
  runGeneration?: number;
  deliveryId?: string;
  effectId?: string;
  eventId?: string;
}
export interface StoredInspectionGrant {
  grantId: string;
  tokenDigest: string;
  backendId: string;
  conversationId: string;
  audienceTabId: string;
  level: InspectionLevel;
  expiresAt: number;
  issuedRunGeneration: number;
  maxEffects: number;
  maxAuditEntries: number;
  revokedAt: number | null;
}
export interface IssueInspectionGrantInput extends RecoveryScope {
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  expectedRevision: number;
  audienceTabId: string;
  level: InspectionLevel;
  ttlMs: number;
  maxEffects: number;
  maxAuditEntries: number;
  now?: number;
}
export interface IssuedInspectionGrant {
  token: string;
  grant: Omit<StoredInspectionGrant, "tokenDigest">;
  inspection: RecoveryInspection;
}
export interface RevokeInspectionGrantInput extends RecoveryScope {
  ownerId: string;
  leaseEpoch: number;
  fencingToken: string;
  runGeneration: number;
  expectedRevision: number;
  grantId: string;
  now?: number;
}
export interface AuthorizedInspectionInput extends RecoveryScope {
  tabId: string;
  grantToken: string;
  limits?: RecoveryInspectionLimits;
  now?: number;
}
export interface RecoveryCheckpoint {
  schemaVersion: 3;
  backendId: string;
  conversationId: string;
  checkpointId: string;
  revision: number;
  eventCursor: number;
  workspace: { digest: string; version: string | null };
  lease: {
    ownerId: string;
    epoch: number;
    fencingToken: string;
    expiresAt: number;
  } | null;
  runGeneration: number;
  terminalState: TerminalState;
  cancellationRevision: number | null;
  completedEffects: RecoveryEffect[];
  pendingEffects: RecoveryEffect[];
  recentEventIds: string[];
  audit: RecoveryAuditEntry[];
  inspectionGrants: StoredInspectionGrant[];
  compaction: RecoveryCompaction;
  integrity: { algorithm: "sha256"; digest: string };
}
export interface RecoveryStatusSummary extends RecoveryScope {
  revision: number;
  boundary: RecoveryBoundary;
  terminalState: TerminalState;
  runGeneration: number;
  leaseState: "none" | "held" | "expired";
  queuedCount: number;
  inFlightCount: number;
  reconcileCount: number;
  completedCount: number;
}
export interface RecoveryInspectionLimits {
  maxEffects?: number;
  maxAuditEntries?: number;
}
export interface RecoveryInspection {
  access: InspectionAccess;
  boundary: RecoveryBoundary;
  summary: RecoveryStatusSummary | null;
  checkpoint: RecoveryCheckpoint | null;
  ignoredInvalidRecords: number;
  visibleMessage: string | null;
  effectsTruncated: boolean;
  auditTruncated: boolean;
}
```

## Recovery-aware conversation event dispatcher

Create `src/api/recovery/conversation-event-dispatcher.ts` and export the
exact public contract below. The dispatcher is the production seam between
REST history preload/WebSocket reconnect traffic and the recovery-aware
store, terminal, and UI side effects.

```ts
import type {
  RecoveryInspection,
  RecoveryScope,
  RuntimeRecoveryEvent,
} from "./recovery-evaluator-adapter";

export interface ConversationEventSession extends RecoveryScope {
  sessionId: string;
  generation: number;
  sinceEventCursor: number | null;
}
export interface TerminalOutputChunk {
  streamId: string;
  chunkId: string;
  text: string;
}
export interface ConversationEventDispatchInput {
  session: ConversationEventSession;
  recoveryEvent: RuntimeRecoveryEvent;
  ordinaryEvent?: unknown;
  terminalOutput?: TerminalOutputChunk;
}
export interface ConversationEventDispatchResult {
  accepted: boolean;
  suppressed: boolean;
  staleSession: boolean;
  inspection: RecoveryInspection;
}
export interface ConversationEventDispatcherDependencies {
  inspectRecovery(scope: RecoveryScope): RecoveryInspection;
  ingestRuntimeEvent(input: RuntimeRecoveryEvent): RecoveryInspection;
  emitOrdinaryEvent(event: unknown): void;
  appendTerminalOutput?(chunk: TerminalOutputChunk & RecoveryScope): void;
}
export interface ConversationEventDispatcher {
  openSession(
    input: RecoveryScope & { sessionId: string },
  ): ConversationEventSession;
  dispatch(
    input: ConversationEventDispatchInput,
  ): ConversationEventDispatchResult;
  closeSession(session: ConversationEventSession): void;
}
export function createConversationEventDispatcher(
  dependencies: ConversationEventDispatcherDependencies,
): ConversationEventDispatcher;
```

`openSession` reads the exact backend/conversation checkpoint without
mutating and returns its current durable `eventCursor` as
`sinceEventCursor`, or `null` when no checkpoint exists. Every call
atomically replaces one active session and increments the
dispatcher-local generation. Session IDs may be reused, so the identity is
the complete returned handle rather than caller text.

`dispatch` accepts only the current handle and an exactly matching recovery
scope. A replaced, closed, or wrong-scope session returns a non-throwing
`{ accepted: false, suppressed: true, staleSession: true }` observation with
a fresh inspection, and invokes no ledger or side-effect dependency. For a
live session, inspect immediately before calling `ingestRuntimeEvent`. The
recovery commit happens first. It is accepted only when it advances the
verified checkpoint revision for the supplied event; duplicate, reordered,
stale, or late input is suppressed. Call `emitOrdinaryEvent` and
`appendTerminalOutput` only after an accepted commit and at most once, in
that order. A replayed history/socket event with a different ordinary
wrapper must not repeat any side effect. A throwing ledger commit
propagates without invoking side effects. Closing a stale handle cannot
close its replacement.

`ConversationService.createRecoveryEventDispatcher(sinks)` exposes the same
dispatcher with `inspectRecovery` and `ingestRuntimeEvent` wired to the
production browser ledger. Its exact signature is:

```ts
createRecoveryEventDispatcher(
  sinks: Pick<
    ConversationEventDispatcherDependencies,
    "emitOrdinaryEvent" | "appendTerminalOutput"
  >,
): ConversationEventDispatcher;
```

Callers cannot substitute the ledger operations. This gives reconnect code
a cursor-based resume point and one tested owner of history and socket side
effects without a live server.

## Durable cross-tab recovery sync coordinator

Create `src/api/recovery/recovery-sync-coordinator.ts` and export the exact
contract below. This coordinator owns browser-tab recovery transport
leadership; it does not replace the ledger lease or the event dispatcher.

```ts
import type { RecoveryScope } from "./recovery-evaluator-adapter";
import type {
  ConversationEventDispatcher,
  ConversationEventDispatcherDependencies,
  ConversationEventDispatchInput,
} from "./conversation-event-dispatcher";

export type RecoverySyncRole = "leader" | "follower" | "frozen" | "stopped";
export type RecoverySyncCrashBoundary =
  | "after_leader_claim"
  | "after_transport_response"
  | "after_event_commit";
export type RecoverySyncNoticeKind =
  | "leader"
  | "progress"
  | "frozen"
  | "stopped";

export interface RecoverySyncPullRequest extends RecoveryScope {
  term: number;
  tabId: string;
  incarnationId: string;
  sinceEventCursor: number | null;
}
export interface RecoverySyncPullResponse {
  events: Omit<ConversationEventDispatchInput, "session">[];
  remoteEventCursor: number | null;
}
export interface RecoverySyncTransport {
  pull(input: RecoverySyncPullRequest): Promise<RecoverySyncPullResponse>;
}
export interface RecoverySyncNotice extends RecoveryScope {
  kind: RecoverySyncNoticeKind;
  term: number;
  revision: number;
  durableEventCursor: number | null;
}
export interface RecoverySyncChannel {
  postMessage(notice: RecoverySyncNotice): void;
  subscribe(listener: (notice: RecoverySyncNotice) => void): () => void;
}
export interface RecoverySyncCoordinatorOptions {
  storage: Storage;
  now: () => number;
  tabId: string;
  incarnationId: string;
  leaseTtlMs: number;
  channel: RecoverySyncChannel;
  transport: RecoverySyncTransport;
  dispatcher: ConversationEventDispatcher;
  crashAfter?: RecoverySyncCrashBoundary | null;
}
export interface RecoverySyncRecord extends RecoveryScope {
  schemaVersion: 1;
  revision: number;
  term: number;
  leaderTabId: string | null;
  leaderIncarnationId: string | null;
  leaseExpiresAt: number | null;
  durableEventCursor: number | null;
  status: "active" | "released";
  lastError: "transport" | "crash" | null;
}
export interface RecoverySyncProjection extends RecoveryScope {
  role: RecoverySyncRole;
  term: number;
  revision: number;
  leaderTabId: string | null;
  leaderIncarnationId: string | null;
  leaseExpiresAt: number | null;
  durableEventCursor: number | null;
  inFlight: boolean;
  lastError: "transport" | "crash" | null;
}
export interface RecoverySyncResult {
  acceptedCount: number;
  suppressedCount: number;
  staleLeader: boolean;
  responseLost: boolean;
  projection: RecoverySyncProjection;
}
export interface RecoverySyncCoordinator {
  start(scope: RecoveryScope): RecoverySyncProjection;
  heartbeat(): RecoverySyncProjection;
  sync(): Promise<RecoverySyncResult>;
  freeze(): void;
  resume(): RecoverySyncProjection;
  stop(): void;
  inspect(): RecoverySyncProjection;
}
export interface ProductionRecoverySyncCoordinatorOptions extends Omit<
  RecoverySyncCoordinatorOptions,
  "storage" | "dispatcher"
> {
  sinks: Pick<
    ConversationEventDispatcherDependencies,
    "emitOrdinaryEvent" | "appendTerminalOutput"
  >;
}
export function recoverySyncStorageKey(
  backendId: string,
  conversationId: string,
  revision: number,
): string;
export function createRecoverySyncCoordinator(
  options: RecoverySyncCoordinatorOptions,
): RecoverySyncCoordinator;
```

`recoverySyncStorageKey` returns
`openhands:recovery-sync:v1:${encodeURIComponent(backendId)}:${encodeURIComponent(conversationId)}:${revision}`.
Coordination records are immutable positive revisions and contain only the
scope, revision, term, leader tab/incarnation identities, lease expiry,
durable cursor, and bounded status/error codes. They never contain
transported ordinary events, terminal text, ledger/checkpoint bodies, or any
token/credential.

`start` subscribes to the injected channel, reads the exact scope's latest
valid coordination record and the dispatcher's durable cursor, and becomes
leader only when no unexpired leader exists. Takeover increments `term` and
binds authority to `tabId` and `incarnationId`; reusing a tab ID after a
restart does not revive old callbacks. Same-revision contenders must
converge on one readable term winner. `heartbeat` extends only the caller's
current term. `freeze` immediately suppresses local work but does not forge
an early expiry; after the durable lease expires, another tab may take
over. `resume` re-scans durable state and returns leader only when the exact
term/incarnation is still valid or it legitimately claims an expired term.
`stop` releases only the exact current leadership and unsubscribes. Calls
before `start` fail clearly.

Only the current unfrozen leader may call `transport.pull`. Its request
cursor comes from the current verified recovery checkpoint, never solely
from a channel notice, remote cursor, or cached projection. Before the call
and after its promise resolves, `sync` re-validates the scope, term, tab,
incarnation, expiry, and durable cursor. A rejected pull is a non-throwing
response-loss result; it advances no cursor, dispatches no event, and
retains no exception text. A later retry or a successor pulls again from the
same durable cursor.

For a valid response, dispatch the envelopes in response order through the
current dispatcher session. The dispatcher/ledger commit of each accepted
event is the only authority that advances the durable cursor.
`remoteEventCursor` is an advisory upper bound and cannot skip missing,
rejected, reordered, duplicate, or wrong-scope envelopes. Re-validate the
coordinator authority before every envelope. If the tab freezes, restarts,
stops, expires, or loses leadership while a pull is pending, the late
response is a non-throwing `staleLeader: true` result and invokes neither
the dispatcher nor the sinks. A response becomes stale after one accepted
commit stops; its successor starts from the fresh durable cursor.
Concurrent `sync` calls from one coordinator coalesce or suppress so that
only one transport pull is active.

Injected channel delivery may be dropped, duplicated, reordered, delayed, or
self-echoed. Notices trigger a fresh exactly scoped durable read; they never
grant authority, and wrong-scope or older notices change nothing. Notice
fields are exactly the allowed projection and must remain secret-free.

`crashAfter` applies once per coordinator. The named state is observable
before the throw: the leader claim is durable, the transport response has
caused no dispatch, and the event commit has gone through only for that
event. Rebuilding uses the durable ledger cursor and term record. This
deterministic injection is for process-boundary tests; no live timer or
socket is required.

`ConversationService.createRecoverySyncCoordinator(options)` wires production
`localStorage` and the production recovery event dispatcher while leaving
time, tab/incarnation identity, channel, transport, lease TTL, sinks, and
the optional crash boundary injectable. Its signature is:

```ts
createRecoverySyncCoordinator(
  options: ProductionRecoverySyncCoordinatorOptions,
): RecoverySyncCoordinator;
```

Production callers supply a stable per-tab `tabId` from `sessionStorage` and
a fresh `incarnationId` per page lifecycle; the factory keeps both from
letting callers substitute ledger inspection/ingestion. Production code
opens no live transport in this benchmark; the injected transport is the
testable browser/backend boundary.

## Transactional workspace recovery reconciler

Create `src/api/recovery/workspace-recovery-reconciler.ts` and export the
exact contract below. This is a product surface separate from event
transport: it reconciles workspace bytes after reconnection, then publishes
one recovery cursor. Private implementation is unconstrained.

```ts
import type {
  ExecuteEffectInput,
  ExecuteEffectResult,
  RecoveryInspection,
  RecoveryScope,
  RuntimeRecoveryEvent,
} from "./recovery-evaluator-adapter";

export type WorkspaceRecoveryPhase =
  | "idle"
  | "planned"
  | "transferring"
  | "committing_remote"
  | "applying_local"
  | "publishing_cursor"
  | "completed"
  | "conflict"
  | "stale"
  | "corrupt";
export type WorkspaceRecoveryCrashBoundary =
  | "after_plan"
  | "after_chunk_transfer"
  | "after_remote_commit"
  | "after_local_apply"
  | "after_cursor_commit";
export type WorkspacePlanAction =
  | "keep"
  | "upload"
  | "download"
  | "delete_local"
  | "delete_remote"
  | "conflict";

export interface WorkspaceManifestEntry {
  path: string;
  kind: "file" | "symlink";
  mode: number;
  size: number;
  contentDigest: string;
  chunkDigests: readonly string[];
}
export interface WorkspaceManifest extends RecoveryScope {
  schemaVersion: 1;
  revision: string;
  baseRevision: string | null;
  rootDigest: string;
  entries: readonly WorkspaceManifestEntry[];
}
export interface WorkspaceRecoveryFence {
  term: number;
  tabId: string;
  incarnationId: string;
  runGeneration: number;
}
export interface WorkspacePlanOperation {
  path: string;
  action: WorkspacePlanAction;
  baseDigest: string | null;
  localDigest: string | null;
  remoteDigest: string | null;
}
export interface WorkspaceRecoveryPlan extends RecoveryScope {
  transactionId: string;
  planDigest: string;
  baseRevision: string;
  localRevision: string;
  remoteRevision: string;
  targetManifest: WorkspaceManifest | null;
  operations: readonly WorkspacePlanOperation[];
  uploadChunkDigests: readonly string[];
  downloadChunkDigests: readonly string[];
  conflictCount: number;
}
export interface WorkspaceRecoveryProjection extends RecoveryScope {
  transactionId: string | null;
  phase: WorkspaceRecoveryPhase;
  recordRevision: number;
  fileCount: number;
  conflictCount: number;
  uploadedChunkCount: number;
  downloadedChunkCount: number;
  remoteCommitted: boolean;
  localApplied: boolean;
  cursorCommitted: boolean;
  errorCode:
    | "conflict"
    | "stale_authority"
    | "invalid_manifest"
    | "corrupt_chunk"
    | "response_lost"
    | null;
}
export interface WorkspaceRecoveryRecord extends RecoveryScope {
  schemaVersion: 1;
  transactionId: string;
  revision: number;
  phase: WorkspaceRecoveryPhase;
  fence: WorkspaceRecoveryFence;
  baseRevision: string;
  localRevision: string;
  remoteRevision: string;
  planDigest: string;
  targetRootDigest: string | null;
  uploadedChunkDigests: readonly string[];
  downloadedChunkDigests: readonly string[];
  conflictCount: number;
  remoteCommitted: boolean;
  localApplied: boolean;
  cursorCommitted: boolean;
  errorCode: WorkspaceRecoveryProjection["errorCode"];
}
export interface WorkspaceRecoveryAuthority {
  current(scope: RecoveryScope): WorkspaceRecoveryFence | null;
}
export interface WorkspaceLocalReplica {
  capture(scope: RecoveryScope): Promise<WorkspaceManifest>;
  readChunk(input: RecoveryScope & { digest: string }): Promise<Uint8Array>;
  missingStagedChunks(
    input: RecoveryScope & {
      transactionId: string;
      digests: readonly string[];
    },
  ): Promise<readonly string[]>;
  stageChunk(
    input: RecoveryScope & {
      transactionId: string;
      digest: string;
      bytes: Uint8Array;
    },
  ): Promise<void>;
  apply(
    input: RecoveryScope & {
      transactionId: string;
      expectedRevision: string;
      targetManifest: WorkspaceManifest;
    },
  ): Promise<WorkspaceManifest>;
}
export interface WorkspaceRemoteCommitRequest extends RecoveryScope {
  transactionId: string;
  expectedRevision: string;
  planDigest: string;
  targetManifest: WorkspaceManifest;
  fence: WorkspaceRecoveryFence;
}
export interface WorkspaceRemoteCommitReceipt extends RecoveryScope {
  transactionId: string;
  revision: string;
  rootDigest: string;
}
export interface WorkspaceRecoveryTransport {
  readHead(scope: RecoveryScope): Promise<WorkspaceManifest>;
  missingChunks(
    input: RecoveryScope & {
      digests: readonly string[];
    },
  ): Promise<readonly string[]>;
  readChunk(input: RecoveryScope & { digest: string }): Promise<Uint8Array>;
  writeChunk(
    input: RecoveryScope & {
      digest: string;
      bytes: Uint8Array;
      fence: WorkspaceRecoveryFence;
    },
  ): Promise<void>;
  commit(
    input: WorkspaceRemoteCommitRequest,
  ): Promise<WorkspaceRemoteCommitReceipt>;
  reconcileCommit(
    input: WorkspaceRemoteCommitRequest,
  ): Promise<WorkspaceRemoteCommitReceipt | undefined>;
}
export interface WorkspaceRecoveryLedger {
  inspectRecovery(scope: RecoveryScope): RecoveryInspection;
  executeEffect<T>(
    input: ExecuteEffectInput,
    execute: () => Promise<T>,
    reconcile?: (idempotencyKey: string) => Promise<T | undefined>,
  ): Promise<ExecuteEffectResult<T>>;
  ingestRuntimeEvent(input: RuntimeRecoveryEvent): RecoveryInspection;
}
export interface WorkspaceRecoveryReconcilerOptions {
  storage: Storage;
  now: () => number;
  instanceId: string;
  authority: WorkspaceRecoveryAuthority;
  local: WorkspaceLocalReplica;
  transport: WorkspaceRecoveryTransport;
  ledger: WorkspaceRecoveryLedger;
  crashAfter?: WorkspaceRecoveryCrashBoundary | null;
}
export interface ProductionWorkspaceRecoveryReconcilerOptions extends Omit<
  WorkspaceRecoveryReconcilerOptions,
  "storage" | "ledger"
> {}
export interface WorkspaceRecoveryInput extends RecoveryScope {
  transactionId: string;
  baseManifest: WorkspaceManifest;
  ledgerAuthority: {
    ownerId: string;
    leaseEpoch: number;
    fencingToken: string;
    runGeneration: number;
    expectedRevision: number;
    eventCursor: number;
  };
  completionEventId: string;
  completionEventCursor: number;
  now?: number;
}
export interface WorkspaceRecoveryResult {
  completed: boolean;
  suppressed: boolean;
  staleAuthority: boolean;
  responseLost: boolean;
  plan: WorkspaceRecoveryPlan | null;
  projection: WorkspaceRecoveryProjection;
  inspection: RecoveryInspection | null;
}
export interface WorkspaceRecoveryReconciler {
  reconcile(input: WorkspaceRecoveryInput): Promise<WorkspaceRecoveryResult>;
  inspect(scope: RecoveryScope): WorkspaceRecoveryProjection;
}
export function workspaceRecoveryStorageKey(
  backendId: string,
  conversationId: string,
  transactionId: string,
  revision: number,
): string;
export function createWorkspaceRecoveryReconciler(
  options: WorkspaceRecoveryReconcilerOptions,
): WorkspaceRecoveryReconciler;
```

Manifest paths are normalized relative POSIX paths: no empty, absolute,
dot-segment, backslash, NUL, duplicate, or prefix-colliding file/symlink
path is valid. Entries are sorted by path. Modes are non-negative integers,
sizes are non-negative safe integers, and all content/chunk/root/plan
digests are lowercase `sha256:<64 hex>`. Chunk lists are ordered,
duplicate-free, and limited to 256 chunks per file and 4096 entries per
manifest. A file's content digest commits to its ordered chunks; a symlink
uses one verified chunk containing its target. `rootDigest` commits to the
scope, revision, base revision, and canonical entries. The reconciler treats
every manifest object as immutable and rejects a changed object observed
under an already recorded revision.

`reconcile` captures the local manifest and reads the remote head, then
requires both to name the supplied base manifest revision as their common
base (or to be the base revision itself). It performs a deterministic
path-ordered three-way merge. Identical local/remote entries are kept. A
side equal to the base receives the other side's change, including
deletion. If both sides differ from the base and from each other, the path
is a `conflict`. Any conflict makes the whole plan unapplicable: no chunk,
ledger callback, local apply, or cursor publication is allowed. Never guess
an ancestor, pick the newest timestamp, or resolve file/symlink collisions
automatically.

The target revision derives from the transaction and plan digest, not from
the wall clock. The plan and every subsequent phase are append-only
positive workspace record revisions under the exact encoded scope and
transaction. Same-revision writers use write-then-compare-and-set
semantics. A restart with the same input resumes the newest valid matching
transaction; a different base, plan, scope, or immutable manifest under the
same transaction ID is a conflict.

Transfer only the digests the plan requires. Ask each destination which
chunks are missing, read only those chunks, verify `sha256` against the
returned bytes, then write/stage by digest. A lost write response does not
mark the chunk absent or advance a phase; a restart asks the destination
again and may safely resend the same content-addressed chunk. No bytes or
raw error text are retained. A corrupt chunk makes the transaction
`corrupt` before commit or apply.

Capture the exact `WorkspaceRecoveryFence` before planning. Re-validate the
scope, sync term, tab ID, incarnation ID, and ledger run generation before
and after every awaited dependency and before every state write, chunk,
remote commit, local apply, and cursor commit. Changed or expired authority
returns a non-throwing stale/suppressed result. A late old response may
leave an unreferenced content-addressed chunk or an uncertain remote
commit, but it may not apply locally, publish a cursor, or overwrite
successor state.

After verified chunks exist at both destinations, call
`ledger.executeEffect` with a stable effect built by the reconciler: the
delivery/effect identity is the transaction ID, the idempotency key is
`workspace-recovery:<transactionId>`, the tool is
`workspace_recovery_commit`, the kind is `remote_create`, and the retry
class is `reconcile`. The execute callback calls `transport.commit` with the
exact pre-image revision, plan digest, target manifest, and captured fence.
The reconcile callback calls `transport.reconcileCommit` with the same
request. The remote commit must be an exact CAS or an idempotent receipt for
the same target; a divergent head is a conflict. A rejected or
response-lost commit stays honestly uncertain in the ledger and returns
`responseLost: true`; a restart reconciles before any re-execution. Before
every effect attempt and cursor publication, refresh the exact scope
through `ledger.inspectRecovery`; the caller's `ledgerAuthority` is the
initial authority, not permission to reuse an old checkpoint revision after
earlier phases wrote durable ledger state.

Only a completed/same-target remote receipt permits `local.apply`. The
local apply is an atomic exact CAS against the captured local revision and
must return the target root; a changed local revision is a conflict, not an
overwrite. Only after the remote receipt and the local target match may the
reconciler call `ledger.ingestRuntimeEvent` with the supplied completion
ID/cursor, type `workspace_reconciled`, and the authority/revision from the
completed ledger inspection. That cursor commit is last. Dispatcher
sessions or sync pulls opened before convergence therefore see the old
cursor, and those opened after convergence see the completion cursor. A
replay after `after_cursor_commit` is suppressed by the ledger event ID and
does not re-apply either workspace.

`crashAfter` applies once per instance. Every named phase is durably
observable before the throw. At `after_remote_commit`, throw after the
transport has returned but before the ledger callback returns, leaving a
reconciliation that must prove whether the remote CAS completed.
Restart/takeover uses the immutable records, content-addressed destination
probes, the ledger effect, and the final event ID rather than process
memory.

`inspect` returns only the `WorkspaceRecoveryProjection`. It must not
contain file paths, manifest/root/plan/chunk/content digests, bytes,
transaction bodies, remote errors, credentials, fencing/grant/claim tokens,
or tab/incarnation identities. Invalid, corrupt, future-version,
wrong-scope, and oversized records do not reveal their contents. Keep at
most 24 valid record revisions per exact scope/transaction, compacting only
after a newer valid record is readable.

`ConversationService.createWorkspaceRecoveryReconciler(options)` has the
exact signature below. It wires browser `localStorage` and the production
recovery ledger's execute/event operations; callers cannot substitute these
either. Time, instance identity, the authority reader, the local replica,
the transport, and the crash boundary remain injected. No live filesystem or
remote service is required.

```ts
createWorkspaceRecoveryReconciler(
  options: ProductionWorkspaceRecoveryReconcilerOptions,
): WorkspaceRecoveryReconciler;
```

## Persistence, integrity, and migration

`recoveryStorageKey` returns
`openhands:recovery:v3:${encodeURIComponent(backendId)}:${encodeURIComponent(conversationId)}:${revision}`.
Every value is one immutable checkpoint JSON object; revisions are positive
base-10 integers. Scope components must be compared after decoding, not by
prefix alone.

`legacyRecoveryStorageKey` returns the previous
`openhands:recovery:v2:${encodeURIComponent(conversationId)}:${revision}`
key. Only `default-local` requests may import a complete valid v2 chain. The
first v3 mutation writes exactly one `migrated` v3 revision before removing
any legacy record. A restart/retry observes the existing v3 revision and
does not migrate again. Map the v2 owner epoch to a new fencing token,
initialize `runGeneration` to at least `1`, map pending effects to `queued`
or `uncertain`, and preserve completed keys, terminal/cancellation state,
workspace, cursor, recent event IDs, audit, and compaction anchors.

Migration participates in the same revision compare-and-set protocol as
every v3 mutation. Two adapters may both observe the same latest legacy
revision and attempt the first `default-local` recovery. Exactly one may
publish the migrated lineage; the loser must report a revision conflict
without writing a second migration record or adopting its proposed owner. A
retry with the stale pre-migration `expectedRevision` still conflicts after
restart. An independent same-conversation v3 lineage on another backend
neither wins nor blocks this default-local race.

The integrity digest is lowercase `sha256:<64 hex>` over the RFC 8785 JSON
Canonicalization Scheme serialization of every checkpoint field except
`integrity`. Unknown versions are never interpreted as current state.
Inspection returns only verified records. Every mutation re-scans durable
state and compares `expectedRevision` before writing its immutable next
revision.

## Delivery and crash semantics

- `enqueueEffect` persists `queued` before returning. A duplicate
  `deliveryId` or completed `idempotencyKey` is suppressed with no new
  callback.
- `executeEffect` may enqueue an absent delivery, then persists `claimed`
  before invoking either callback. Another adapter must honor that claim
  while the callback is pending. A live claim yields a non-throwing
  suppressed result.
- After a callback returns, persist `applied` with bounded redacted result
  metadata, then persist `completed`. Completion moves the entry to
  `completedEffects`; a duplicate call never invokes either callback.
- Every delivery owns an independent claim and delivery attempt. Two
  callbacks may be live under one lease; their claim tokens differ, and a
  takeover or late result for either delivery cannot replace, complete, or
  drop the other delivery.
- If `execute` rejects after its durable claim, persist the released
  recovery outcome before propagating the error: unsafe work is
  `uncertain`, replay-safe work stays replayable, and non-retryable work is
  `blocked`. If `reconcile` rejects, persist `uncertain` with no live
  recovery claim before propagating. A fresh instance may then make one
  fresh attempt. Error text is bounded and redacted and is never treated as
  result metadata.
- `crashAfter` throws immediately after the named observable boundary. At
  `after_effect` it throws after the callback returns but before `applied`
  is written. At the other boundaries the named state is durably readable
  before the throw. The option applies once per adapter instance.
- An expired read-only replay-safe claim may be claimed with an incremented
  attempt and executed. An expired or crashed uncertain side-effecting call
  is reconciled through the stable idempotency key before any retry. A
  successful reconciliation settles through `applied` and `completed`; an
  undefined reconciliation durably keeps `uncertain`, releases or lets
  expire its recovery claim, and does not execute. A later restart may
  reconcile again under a fresh attempt. While one reconcile callback is
  live, another competing fresh adapter must honor its claim and suppress
  without invoking a callback. An `applied` state settles without invoking
  any callback. Non-retryable work is blocked.
- Settlement validates the exact claim token, delivery ID, attempt, owner,
  lease epoch, fencing token, run generation, revision lineage, active
  terminal state, and workspace. A stale result is a bounded audit-only
  observation. The original `executeEffect` promise resolves with
  `suppressed: true`; a stale settlement is not reported as an unhandled
  callback rejection.
- A successor may take over an expired lease and settle an old uncertain
  delivery to `completed` before the pending original callback returns. The
  late original result must not replace the completed metadata, authority,
  or generation, and its promise still settles suppressed. The same
  suppression applies to a late rejection, which must not surface as an
  unhandled promise rejection after the authority transfer.

## Lease, ABA, event, and compaction semantics

An unexpired lease cannot be stolen. Expiry takeover increments the lease
epoch, creates a new fencing token, and increments `runGeneration`. A
same-revision race has one writer; a losing recovery observes the winner
and cannot execute. Pause, resume, and cancel each compare the full
authority and increment `runGeneration`. Resume is valid only from paused;
cancel is terminal. An old claim never obtains a new generation by matching
owner text or reusing a revision. Runtime events require the same authority
and are deduplicated by ID before any store/UI side effect; a reordered
cursor cannot advance state.

Keep at most `maxRetainedRevisions`, default `24`, clamped to `8..32`.
Compact after the new record is readable and valid. Remove the smallest
deterministic batch of revisions; the highest removed valid revision/cursor
and its integrity digest become the anchor once compaction advances.
Preserve the terminal state, the current lease/fencing/generation, active
delivery claims, completed key proofs, grants, and event deduplication.
Compaction enumerates and removes only exactly decoded `(backendId,
conversationId)` scope keys. A migrated `default-local` lineage cannot
remove legacy records or v3 revisions of a same-named conversation on
another backend, including percent-encoded/prefix-like scope names. Because
publication precedes cleanup, a `removeItem` failure during compaction is
non-fatal for the committed mutation: return the newest valid inspection,
leave unrelated records untouched, and retry the exact-scope cleanup on a
later mutation. The retention count is a target that may temporarily be
exceeded after an injected browser storage cleanup failure; never claim an
anchor for a record that was not removed.

Keep at most 64 event IDs, 64 audit entries, 100 completed effects, 100
pending effects, and 16 grants. The default internal inspection returns at
most 50 effects and 20 audit entries. Callers clamp limits to `0..100`
effects and `0..64` audit entries. Grant maxima may only lower those
limits.

## Least-privilege inspection and production seams

The grant token is returned once; only the `sha256:<64 hex>` token digest is
persisted. Grant issue/revoke require the current unexpired lease and the
full generation. Any lease takeover or pause/resume/cancel generation
change invalidates all prior grants. A denied inspection returns
`access: "denied"`, `boundary: "forbidden"`, safe visible text, and a null
summary/checkpoint, without revealing whether a foreign scope exists.
`status` returns the summary only. `support` may return the bounded
projection, but with all token/fencing values replaced by redacted
placeholders and its integrity field describing the durable record rather
than asserting that the projection itself is canonical.

- `ConversationService` in the existing session service module exposes all
  adapter operations, including enqueue, grant issue/revoke, and authorized
  inspection. It uses the default browser `localStorage` and a stable
  per-tab `sessionStorage` identity. It also exposes the production
  `createRecoveryEventDispatcher(sinks)`, `createRecoverySyncCoordinator`,
  and `createWorkspaceRecoveryReconciler` factories defined above.
- `AgentServerRuntimeService.executeRecoverableEffect(input, execute,
  reconcile?)` has the exact generic result contract and crosses every
  durable delivery stage around the real callback.
- `src/stores/recovery-store.ts` exports `useRecoveryStore`. The state
  includes `scope`, `tabId`, `grantToken`, `access`, `boundary`, `summary`,
  `visibleMessage`, the two truncation flags, `refresh(request)`,
  `watch(request)`, and `disconnect()`. `watch` installs a browser `storage`
  listener, refreshes only for matching v3 scope keys, re-validates the
  grant, and removes the listener on disconnect/replacement. Replacement is
  atomic from the store's perspective: a late matching event processed for
  the previous request cannot deny, roll back, or overwrite the replacement
  request's projection.
- The existing `useEventStore` state adds
  `ingestRecoveryEvent(input: RuntimeRecoveryEvent, event?: OHEvent):
  RecoveryInspection`. It commits the runtime recovery event through the
  same production ledger before applying the optional ordinary UI event.
  Duplicate, reordered, stale-authority, or late-terminal recovery input
  never repeats the optional event-store/UI side effect. Duplicate event ID
  suppression happens before the revision conflict check, so a reconnect
  replay is non-throwing. That suppression is decided by the durable
  recovery `eventId`, not by the ID of the optional ordinary event object;
  a replay still contained in a different UI event does not cause a second
  side effect.
- `src/components/features/conversation/recovery-status.tsx` exports
  `RecoveryStatus`. It accepts `AuthorizedInspectionInput` without `limits`
  and renders `data-testid="recovery-status"` for a granted non-`none`
  status. It renders only summary/visible status text, not raw checkpoint
  data. It renders no status element for neutral or denied states.
- Runtime recovery events update the same ledger before dispatching
  recovery side effects and do not weaken the existing `useEventStore`
  behavior.
- REST preload and live socket recovery events enter through one dispatcher
  session. Opening a replacement session fences late old callbacks and
  returns the durable cursor for incremental reconnection; the dispatcher
  never stores or requests unbounded event history.
- Browser tabs enter recovery transport through the durable sync
  coordinator. Its term lease is independent of the runtime/effect lease,
  its channel is notification-only, and every late transport response is
  fenced before dispatcher/store/terminal/UI effects. The production
  factory wires localStorage and the production dispatcher while keeping
  the transport injected.
- Workspace reconnection enters through the workspace reconciler. Its
  production factory wires the same localStorage ledger used by runtime
  effects, while the local replica and remote manifest/chunk transport stay
  injected. The final workspace event advances the same cursor used by the
  dispatcher and sync.

## Error contract for deterministic crash injection

`RecoveryCrashBoundary`, `RecoverySyncCrashBoundary`, and
`WorkspaceRecoveryCrashBoundary` are in-process deterministic test hooks.
For each of them:

- the named state must already be durably written and readable by a new
  instance before the throw;
- the thrown `Error.message` must contain that boundary code string itself
  verbatim, for example `after_remote_commit`, so that the evaluator can
  confirm that the crash indeed happened at the declared place;
- it takes effect only once per coordinator/reconciler instance, and a
  rebuilt instance must continue from the durable record without repeating
  a commit, repeating an apply, or advancing the cursor;
- any `reconcile` attempt (including one that ends in a conflict) must
  first write a scope-exact `WorkspaceRecoveryRecord` with `revision >= 1`,
  and after a restart `inspect(scope)` must reproduce its `phase` and the
  three phase flags as is.

### 0920: observable contract for runtime events and grant caps

- The ruling order of `ingestRecoveryEvent` / `ingestRuntimeEvent` is fixed
  and observable: first rule on authority by
  `(expectedRevision, runGeneration, leaseEpoch, fencingToken)`, then rule
  on duplication by `eventId`, and only then commit the ledger and advance
  `eventCursor`. When the authority does not match, suppress (either by
  throwing or by returning an observation) and **do not** advance
  `eventCursor`; when the `eventId` is a duplicate, do not advance the
  cursor again and do not append the effect again. When both the authority
  and the `eventId` hold, `eventCursor` must become the value the event
  itself carries. The external supervisor reads the results of these three
  rules only through `inspectRecovery(scope).checkpoint`.
- `recentEventIds` is deduplication evidence: it must contain no
  duplicates, and its length is bounded by the retention cap (≤ 64).
- The `input.limits` of `inspectRecoveryAuthorized(input)` is the caller's
  request, not authorization. The returned `effectsTruncated` /
  `auditTruncated` describe whether truncation happened relative to the
  **durable state**, not relative to the caller's request.
- When a workspace conflict is resolved upstream, the remote manifest
  appears with a **new `revision`**; `transport.commit` still requires
  `expectedRevision === the remote's current revision`, and a commit with
  a cached old head is refused.
- `enqueueEffect(input: ExecuteEffectInput)` must persist that effect as a
  `pendingEffects` entry (its shape is in the public
  `dev_cases/recovery-contract.type-test.ts`). A subsequent
  `type: "effect_completed"` runtime event settles it by
  `effect.deliveryId` / `effect.idempotencyKey`; the deduplication key is
  those two, not `eventId`. The external supervisor reads the result only
  through the `pendingEffects` / `completedEffects` / `eventCursor` of
  `inspectRecovery(scope).checkpoint`.
