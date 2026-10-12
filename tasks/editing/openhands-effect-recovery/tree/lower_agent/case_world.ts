/** Stateful external incidents for the six OpenHands lower-agent cases.
 * This supplies input data, unreliable transports, and hostile external
 * callbacks. It never chooses the lower agent's recovery actions or scores.
 */
import { createHash } from "node:crypto";

const canonical = (value: any): any => Array.isArray(value) ? value.map(canonical)
  : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const hash = (value: any) => createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
const bytesHash = (value: Uint8Array) => `sha256:${createHash("sha256").update(value).digest("hex")}`;
const copy = (value: any) => structuredClone(value);

export function createCaseWorld(caseId: string, nonce: string, fixture: any, storage: Storage,
                                legacyStorageKey: (conversation: string, revision: number) => string) {
  fixture = { ...fixture,
    workspaceChunkResponseLoss: fixture.workspaceChunkResponseLoss ?? fixture.workspaceChunkResume ?? false,
    productionWorkspaceCommitRecovery: fixture.productionWorkspaceCommitRecovery ?? fixture.workspaceCommitReconciliation ?? false,
    productionSeams: fixture.productionSeams ?? (fixture.syncResponseLossRetry || fixture.syncFrozenTabTakeover || false),
    productionSyncResponseLossRetry: fixture.productionSyncResponseLossRetry ?? fixture.syncResponseLossRetry ?? false,
    productionSyncLateResponseFence: fixture.productionSyncLateResponseFence ?? fixture.syncFrozenTabTakeover ?? false,
    grantLifecycle: fixture.grantLifecycle ?? (caseId === "dev_002"),
    aba: fixture.aba ?? fixture.lateSettlementTakeover ?? false,
  };
  const scope = { backendId: fixture.schemas ? "default-local" : `backend-${nonce}`, conversationId: `conversation-${nonce}` };
  let clock = 1700000000000;
  let term = 1;
  let incarnation = `incarnation-${nonce}`;
  let restartCount = 0;
  let currentGeneration = 1;
  let currentCheckpoint: any = null;
  let syncPullCount = 0;
  let delayedSync: any = null;
  const syncResponses: any[] = [];
  let delayedAuthority: any = null;
  const pendingEffects: any[] = [];
  const issuedGrants: any[] = [];
  const crashBoundaries: string[] = fixture.workspaceCrashBoundaries ?? [];
  const crashObservations: any[] = [];
  let crashIndex = 0;
  const operations: any[] = [];
  const externalEvents: any[] = [];
  // 0919 hardening: additive, evaluator-only bookkeeping used to compute the
  // per-case required-outcome ledger. None of it is visible to the product.
  const actionLog: any[] = [];
  const workspaceObservations: any[] = [];
  const generationLog: any[] = [];
  const syncPulls: any[] = [];
  const secretStrings = new Set<string>();
  let lateTerminalBurst: any = null;
  // 0920 hardening: evaluator-driven probes. They cost the lower agent no action
  // budget -- the world delivers the traffic itself and reads the product's own
  // durable state back -- so every negative required outcome below now has a
  // positive control next to it and "the product did nothing" stops passing.
  let eventDisciplineDone = false;
  let eventDiscipline: any = null;
  let lastLiveCheckpoint: any = null;
  const durableCursorMarks: any[] = [];
  const deliveredLegitimateCursors: number[] = [];
  let refusalSnapshot: any = null;
  let conflictResolved = false;
  const grantLimitProbes: any[] = [];
  let effectDelivery: any = null;
  let effectDeliveryDone = false;
  let actionSequence = 0;
  const payloads = Object.fromEntries(["base", "local", "remote", "conflict"].map(name => [name, new TextEncoder().encode(`${name}:${nonce}`)]));
  const chunk = (name: string) => bytesHash(payloads[name]);
  const entry = (path: string, name: string) => ({ path, kind: "file", mode: 420, size: payloads[name].byteLength, contentDigest: chunk(name), chunkDigests: [chunk(name)] });
  const manifest = (revision: string, baseRevision: string | null, entries: any[]) => ({
    schemaVersion: 1, ...scope, revision, baseRevision, rootDigest: `sha256:${hash(entries)}`, entries,
  });
  const conflict = fixture.workspaceConflictSafePlan === true;
  // A conflicting path next to independent local-only/remote-only paths: the
  // whole transaction must still be refused, so a partial "merge what we can"
  // reconciler is observably wrong instead of merely unlucky.
  const mixedConflict = conflict && fixture.workspaceMixedConflict === true;
  const baseManifest = manifest(`base-${nonce}`, null, [entry("README.md", "base")]);
  let localManifest = manifest(`local-${nonce}`, baseManifest.revision,
    mixedConflict ? [entry("README.md", "local"), entry(`local-${nonce.slice(0, 6)}.txt`, "local")]
    : conflict ? [entry("README.md", "local")] : [entry("README.md", "base"), entry(`local-${nonce.slice(0, 6)}.txt`, "local")]);
  let remoteManifest = manifest(`remote-${nonce}`, baseManifest.revision,
    mixedConflict ? [entry("README.md", "remote"), entry(`remote-${nonce.slice(0, 6)}.txt`, "remote")]
    : conflict ? [entry("README.md", "remote")] : [entry("README.md", "base"), entry(`remote-${nonce.slice(0, 6)}.txt`, "remote")]);
  const initialLocal = copy(localManifest);
  const initialRemote = copy(remoteManifest);
  const localChunks = new Map<string, Uint8Array>([[chunk("base"), payloads.base], [chunk("local"), payloads.local]]);
  const remoteChunks = new Map<string, Uint8Array>([[chunk("base"), payloads.base], [chunk("remote"), payloads.remote]]);
  const staged = new Map<string, Uint8Array>();
  const committed = new Map<string, any>();
  let loseWrite = fixture.workspaceChunkResponseLoss === true;
  let loseCommit = fixture.workspaceCommitResponseLoss === true || fixture.productionWorkspaceCommitRecovery === true;
  let migrationIncidentsDelivered = false;
  const namespaceSentinels = new Map<string, string>();
  let legacyExpected: any = null;
  if (fixture.schemas) {
    const legacy = {
      schemaVersion: 2, conversationId: scope.conversationId, checkpointId: `legacy-${nonce}`,
      revision: 1, eventCursor: 7, workspace: { digest: `sha256:${hash(nonce)}`, version: `git:${nonce}` },
      lease: { ownerId: `legacy-owner-${nonce}`, epoch: 4, expiresAt: clock - 10 },
      terminalState: "active", cancellationRevision: null,
      completedEffects: [{ effectId: `complete-${nonce}`, idempotencyKey: `complete:${nonce}`, toolName: "local_write", kind: "local_write", status: "completed", retryClass: "already_completed", claim: null, resultMetadata: { digestAlgorithm: "sha256" } }],
      pendingEffects: [{ effectId: `pending-${nonce}`, idempotencyKey: `pending:${nonce}`, toolName: "remote_create", kind: "remote_create", status: "uncertain", retryClass: "reconcile", claim: null }],
      recentEventIds: [`legacy-event-${nonce}`], audit: [],
      compaction: { generation: 0, compactedThroughRevision: 0, compactedThroughEventCursor: 0, anchorDigest: null },
    };
    const key = legacyStorageKey(scope.conversationId, 1);
    legacyExpected = copy(legacy);
    const text = JSON.stringify({ ...legacy, integrity: { algorithm: "sha256", digest: `sha256:${hash(legacy)}` } });
    storage.setItem(key, text);
    namespaceSentinels.set(key, text);
    for (const name of ["sync", "workspace", "foreign-recovery"]) {
      const sentinelKey = name === "sync" ? `openhands:recovery-sync:v1:${encodeURIComponent(scope.backendId)}:${encodeURIComponent(scope.conversationId)}:1`
        : name === "workspace" ? `openhands:workspace-recovery:v1:${encodeURIComponent(scope.backendId)}:${encodeURIComponent(scope.conversationId)}:transaction-a:1`
        : `openhands:recovery:v3:foreign-${nonce}:${encodeURIComponent(scope.conversationId)}:7`;
      const foreign = { schemaVersion: 3, backendId: `foreign-${nonce}`, conversationId: scope.conversationId,
        checkpointId: `foreign-checkpoint-${nonce}`, revision: 7, eventCursor: 4,
        workspace: { digest: `sha256:${hash(`foreign-${nonce}`)}`, version: `git:foreign-${nonce}` },
        lease: null, runGeneration: 1, terminalState: "active", cancellationRevision: null,
        completedEffects: [], pendingEffects: [], recentEventIds: [], audit: [], inspectionGrants: [],
        compaction: { generation: 0, compactedThroughRevision: 0, compactedThroughEventCursor: 0, anchorDigest: null } };
      const sentinelValue = name === "foreign-recovery" ? JSON.stringify({ ...foreign, integrity: { algorithm: "sha256", digest: `sha256:${hash(foreign)}` } })
        : JSON.stringify({ ...scope, schemaVersion: 1, revision: 1, transactionId: "transaction-a", phase: "planned", term: 7, durableEventCursor: 7 });
      storage.setItem(sentinelKey, sentinelValue);
      namespaceSentinels.set(sentinelKey, sentinelValue);
    }
  }
  const visibleNotice = () => ({
    case_id: caseId, scope, clock,
    incident: fixture.schemas ? "A legacy conversation with an expired owner must resume; unrelated backend, sync and workspace records coexist."
      : conflict ? ("Local and remote edits overlap in README.md while other paths changed on only one side; protect every version while restoring a usable recovery state."
          + (fixture.aba ? " A previous browser generation still owes this conversation an unreturned callback." : ""))
      : fixture.aba ? "A previous browser generation may still deliver queued callbacks while the current user pauses, resumes or cancels."
      : fixture.grantLifecycle ? "An owner must provide bounded status/support access to another tab without exposing private recovery state."
      : fixture.productionSeams ? "The live browser event stream has historical echoes, reconnects and an uncertain workspace delivery."
      : "Local-only and remote-only files must converge after intermittent write/commit response loss.",
    workspace: fixture.schemas ? { digest: `sha256:${hash(nonce)}`, version: `git:${nonce}` }
      : { digest: baseManifest.rootDigest, version: baseManifest.revision },
    observer_tab: `viewer-${nonce}`, legacy_revision: fixture.schemas ? 1 : null,
    pending_external_callbacks: pendingEffects.length,
  });
  const log = (operation: string, facts: any) => operations.push({ sequence: operations.length + 1, operation, facts: copy(facts) });
  const exactScope = (input: any) => {
    if (input.backendId !== scope.backendId || input.conversationId !== scope.conversationId) throw new Error("workspace scope mismatch");
  };
  const readChunk = (chunks: Map<string, Uint8Array>, input: any) => {
    exactScope(input);
    const bytes = chunks.get(input.digest);
    if (!bytes) throw new Error("missing content-addressed fixture chunk");
    return bytes.slice();
  };
  const validateChunk = (input: any) => {
    exactScope(input);
    if (bytesHash(input.bytes) !== input.digest) throw new Error("content-addressed chunk digest mismatch");
  };
  const transport = {
    pull: async (input: any) => {
      exactScope(input);
      syncPullCount += 1;
      const durableCursorAtPull = currentCheckpoint ? Number(currentCheckpoint.eventCursor) : null;
      syncPulls.push({ attempt: syncPullCount, request_term: input.term, world_term: term,
        request_cursor: input.sinceEventCursor ?? null, durable_cursor_at_pull: durableCursorAtPull,
        request_tab: input.tabId, request_incarnation: input.incarnationId, world_incarnation: incarnation });
      log("sync.pull", { request: input, attempt: syncPullCount, world_term: term, durable_cursor_at_pull: durableCursorAtPull });
      if (!fixture.productionSeams || !currentCheckpoint?.lease) return { remoteEventCursor: currentCheckpoint?.eventCursor ?? null, events: [] };
      const checkpoint = copy(currentCheckpoint), lease = checkpoint.lease;
      const event = { ...scope, ownerId: lease.ownerId, leaseEpoch: lease.epoch,
        fencingToken: lease.fencingToken, runGeneration: checkpoint.runGeneration,
        expectedRevision: checkpoint.revision, eventId: `history-${nonce}-${syncPullCount}`,
        eventCursor: Number(checkpoint.eventCursor) + 1, type: "resumed", now: clock };
      const response = { remoteEventCursor: event.eventCursor, events: [
        { recoveryEvent: event, ordinaryEvent: { id: `rest-wrapper-${nonce}-${syncPullCount}` }, terminalOutput: { streamId: `stream-${nonce}`, chunkId: `chunk-${event.eventId}`, text: "recovered terminal output" } },
        { recoveryEvent: copy(event), ordinaryEvent: { id: `socket-wrapper-${nonce}-${syncPullCount}` }, terminalOutput: { streamId: `stream-${nonce}`, chunkId: `chunk-${event.eventId}`, text: "recovered terminal output" } },
      ] };
      syncResponses.push({ request: copy(input), response: copy(response) });
      for (const item of response.events) deliveredLegitimateCursors.push(Number(item.recoveryEvent.eventCursor));
      if (syncPullCount === 1 && fixture.productionSyncResponseLossRetry) {
        log("fault.sync_response_lost", { requested_cursor: input.sinceEventCursor, remote_event_cursor: response.remoteEventCursor });
        throw new Error("browser history response lost before delivery");
      }
      if (!delayedSync && syncPullCount === 2 && fixture.productionSyncLateResponseFence) {
        log("fault.sync_response_delayed", { requested_cursor: input.sinceEventCursor, old_term: input.term });
        return await new Promise(resolve => { delayedSync = { resolve, response, oldTerm: input.term }; });
      }
      log("sync.response_delivered", { event_ids: response.events.map(item => item.recoveryEvent.eventId), distinct_ui_wrappers: true });
      return response;
    },
    readHead: async (input: any) => { exactScope(input); log("remote.readHead", { revision: remoteManifest.revision }); return copy(remoteManifest); },
    missingChunks: async (input: any) => { exactScope(input); return input.digests.filter((digest: string) => !remoteChunks.has(digest)); },
    readChunk: async (input: any) => readChunk(remoteChunks, input),
    writeChunk: async (input: any) => {
      validateChunk(input); remoteChunks.set(input.digest, input.bytes.slice()); log("remote.writeChunk", { digest: input.digest });
      if (loseWrite) { loseWrite = false; log("fault.chunk_response_lost", { persisted: true }); throw new Error("chunk response lost after remote persistence"); }
    },
    commit: async (input: any) => {
      exactScope(input);
      if (committed.has(input.transactionId)) return copy(committed.get(input.transactionId));
      if (input.expectedRevision !== remoteManifest.revision) throw new Error("remote revision conflict");
      for (const item of input.targetManifest.entries) for (const digest of item.chunkDigests ?? []) {
        if (!remoteChunks.has(digest)) throw new Error("remote commit references unavailable chunk");
      }
      remoteManifest = copy(input.targetManifest);
      const receipt = { ...scope, transactionId: input.transactionId, revision: remoteManifest.revision, rootDigest: remoteManifest.rootDigest };
      committed.set(input.transactionId, receipt); log("remote.commit", receipt);
      if (loseCommit) { loseCommit = false; log("fault.commit_response_lost", { persisted: true }); throw new Error("commit response lost after durable publication"); }
      return copy(receipt);
    },
    reconcileCommit: async (input: any) => { exactScope(input); log("remote.reconcileCommit", { transactionId: input.transactionId, found: committed.has(input.transactionId) }); return copy(committed.get(input.transactionId)); },
  };
  const local = {
    capture: async (input: any) => { exactScope(input); return copy(localManifest); },
    readChunk: async (input: any) => readChunk(localChunks, input),
    missingStagedChunks: async (input: any) => input.digests.filter((digest: string) => !staged.has(`${input.transactionId}:${digest}`)),
    stageChunk: async (input: any) => { validateChunk(input); staged.set(`${input.transactionId}:${input.digest}`, input.bytes.slice()); log("local.stageChunk", { transactionId: input.transactionId, digest: input.digest }); },
    apply: async (input: any) => {
      exactScope(input);
      if (input.expectedRevision !== localManifest.revision) throw new Error("local revision conflict");
      if (!committed.has(input.transactionId) && ![...committed.values()].some(receipt => receipt.revision === input.targetManifest.revision)) throw new Error("local apply attempted before durable remote commit");
      localManifest = copy(input.targetManifest); log("local.apply", { revision: localManifest.revision }); return copy(localManifest);
    },
  };
  const effectReceipts = new Map<string, any>();
  const executeEffect = async (input: any) => {
    const key = input.effect?.idempotencyKey;
    if (effectReceipts.has(key)) { log("effect.duplicate_execution", { key }); throw new Error("remote side effect executed twice"); }
    const receipt = { id: `receipt-${nonce}-${effectReceipts.size + 1}`, idempotencyKey: key };
    effectReceipts.set(key, receipt); log("effect.execute", receipt);
    if (fixture.aba || fixture.productionSeams) {
      log("fault.effect_callback_delayed", { key, durable_effect_exists: true });
      return await new Promise(resolve => pendingEffects.push({ key, receipt, resolve }));
    }
    return receipt;
  };
  const reconcileEffect = async (key: string) => { log("effect.reconcile", { key, found: effectReceipts.has(key) }); return copy(effectReceipts.get(key)); };
  const harvestSecrets = (checkpoint: any) => {
    const token = checkpoint?.lease?.fencingToken;
    if (typeof token === "string" && token.length >= 8) secretStrings.add(token);
    for (const effect of [...(checkpoint?.pendingEffects ?? []), ...(checkpoint?.completedEffects ?? [])]) {
      const claimToken = effect?.claim?.token;
      if (typeof claimToken === "string" && claimToken.length >= 8) secretStrings.add(claimToken);
    }
  };
  const beforeAction = (_action: string, inspection: any) => {
    const checkpoint = inspection?.checkpoint;
    if (checkpoint) currentCheckpoint = copy(checkpoint);
    harvestSecrets(checkpoint);
    if (Number.isSafeInteger(checkpoint?.runGeneration) && checkpoint.runGeneration > 0) currentGeneration = checkpoint.runGeneration;
    if (!delayedAuthority && checkpoint?.lease) delayedAuthority = copy(checkpoint);
    clock += 1;
  };
  // A Candidate whose inspectRecovery throws must not turn an oracle probe into
  // an evaluator error: the world records the failure and carries on.
  const safeInspect = async (adapter: any) => {
    try { return (await adapter.inspectRecovery(scope))?.checkpoint ?? null; }
    catch { return null; }
  };
  const cursorSnap = (checkpoint: any) => ({ revision: checkpoint?.revision ?? null,
    event_cursor: checkpoint?.eventCursor ?? null,
    completed_effects: checkpoint?.completedEffects?.length ?? null,
    pending_effects: checkpoint?.pendingEffects?.length ?? null,
    terminal_state: checkpoint?.terminalState ?? null,
    recent_event_ids: Array.isArray(checkpoint?.recentEventIds) ? checkpoint.recentEventIds.length : null,
    distinct_recent_event_ids: Array.isArray(checkpoint?.recentEventIds) ? new Set(checkpoint.recentEventIds).size : null });
  const ingest = async (adapter: any, event: any) => {
    try { await adapter.ingestRuntimeEvent(copy(event)); return { threw: false, error_name: null }; }
    catch (error: any) { return { threw: true, error_name: error?.name ?? "Error" }; }
  };
  // 0920: one runtime-event discipline probe per case, driven by the world.
  // It publishes four deliveries against the three rules that input/03 already
  // states (§8 dedup, §21 stale suppression, §25):
  //   1. a fully authorized fresh event must be ACCEPTED and must move the
  //      durable cursor to exactly its own eventCursor  (positive control);
  //   2. the identical eventId re-delivered must change nothing;
  //   3. an event carrying a forged fencing token must be suppressed with the
  //      durable cursor bit-identical;
  //   4. the same for an event carrying a wrong lease epoch.
  // Rules 2-4 are the negatives a "reject everything" product would pass; rule 1
  // is the positive control that such a product now fails.
  const runEventDisciplineProbe = async (adapter: any) => {
    const live = await safeInspect(adapter);
    if (!live?.lease || !Number.isSafeInteger(Number(live.revision))) {
      eventDiscipline = { attempted: false, reason: "no live leased checkpoint existed when the probe fell due" };
      log("probe.runtime_event_discipline_skipped", { reason: "no live leased checkpoint" });
      return;
    }
    const authority = (checkpoint: any) => ({ ownerId: checkpoint.lease.ownerId, leaseEpoch: checkpoint.lease.epoch,
      fencingToken: checkpoint.lease.fencingToken, runGeneration: checkpoint.runGeneration,
      expectedRevision: checkpoint.revision });
    const before = cursorSnap(live);
    const freshCursor = Number(live.eventCursor) + 1;
    const eventId = `discipline-${nonce}`;
    const fresh = { ...scope, ...authority(live), eventId, eventCursor: freshCursor, type: "resumed", now: clock };
    deliveredLegitimateCursors.push(freshCursor);
    const first = await ingest(adapter, fresh);
    const afterFresh = await safeInspect(adapter);
    const replayAuthority = afterFresh?.lease ? authority(afterFresh) : authority(live);
    const replay = { ...scope, ...replayAuthority, eventId, eventCursor: freshCursor, type: "resumed", now: clock + 1 };
    const second = await ingest(adapter, replay);
    const afterReplay = await safeInspect(adapter);
    const forgedAuthority = afterReplay?.lease ? authority(afterReplay) : replayAuthority;
    const forged = { ...scope, ...forgedAuthority, fencingToken: `${String(forgedAuthority.fencingToken)}-forged`,
      eventId: `discipline-forged-${nonce}`, eventCursor: Number(afterReplay?.eventCursor ?? freshCursor) + 50,
      type: "resumed", now: clock + 2 };
    const third = await ingest(adapter, forged);
    const afterForged = await safeInspect(adapter);
    const epochAuthority = afterForged?.lease ? authority(afterForged) : forgedAuthority;
    const wrongEpoch = { ...scope, ...epochAuthority, leaseEpoch: Number(epochAuthority.leaseEpoch) + 7,
      eventId: `discipline-epoch-${nonce}`, eventCursor: Number(afterForged?.eventCursor ?? freshCursor) + 90,
      type: "resumed", now: clock + 3 };
    const fourth = await ingest(adapter, wrongEpoch);
    const afterEpoch = await safeInspect(adapter);
    eventDiscipline = { attempted: true, before, fresh_event_cursor: freshCursor,
      after_fresh: cursorSnap(afterFresh), after_replay: cursorSnap(afterReplay),
      after_forged_fencing: cursorSnap(afterForged), after_wrong_epoch: cursorSnap(afterEpoch),
      fresh_threw: first.threw, replay_threw: second.threw, forged_threw: third.threw, wrong_epoch_threw: fourth.threw,
      fresh_accepted_once: first.threw === false && Number(afterFresh?.eventCursor) === freshCursor,
      replay_changed_nothing: Number(afterReplay?.eventCursor) === Number(afterFresh?.eventCursor)
        && Number(afterReplay?.completedEffects?.length ?? 0) === Number(afterFresh?.completedEffects?.length ?? 0)
        && Number(afterReplay?.pendingEffects?.length ?? 0) === Number(afterFresh?.pendingEffects?.length ?? 0),
      forged_fencing_suppressed: Number(afterForged?.eventCursor) === Number(afterReplay?.eventCursor),
      wrong_epoch_suppressed: Number(afterEpoch?.eventCursor) === Number(afterForged?.eventCursor),
      durable_cursor_after_probe: Number(afterEpoch?.eventCursor ?? afterFresh?.eventCursor ?? 0) };
    log("probe.runtime_event_discipline", { fresh_event_cursor: freshCursor,
      durable_cursor_after_probe: eventDiscipline.durable_cursor_after_probe });
  };
  const eventDisciplineHeld = () => Boolean(eventDiscipline?.attempted)
    && eventDiscipline.fresh_accepted_once === true && eventDiscipline.replay_changed_nothing === true
    && eventDiscipline.forged_fencing_suppressed === true && eventDiscipline.wrong_epoch_suppressed === true;
  // 0920b: the durable outbox, driven by the world through the product's own
  // public adapter surface. input/03 §3 requires the persistent
  // queued -> claimed -> applied -> completed state machine, §5 requires a
  // settlement to be completed from durable evidence without calling back, §6
  // requires a stale settlement to change nothing, and §8 requires duplicate
  // DELIVERY IDS and IDEMPOTENCY KEYS to be suppressed -- not only duplicate
  // event ids. Nothing measured any of it. Four deliveries, read back from the
  // product's own checkpoint each time; the agent spends no action on it.
  const keysOf = (list: any[]) => (list ?? []).map((item: any) => item?.idempotencyKey ?? null);
  const runEffectDeliveryProbe = async (adapter: any) => {
    const live = await safeInspect(adapter);
    if (!live?.lease || typeof adapter.enqueueEffect !== "function") {
      effectDelivery = { attempted: false,
        reason: !live?.lease ? "no live leased checkpoint existed when the probe fell due"
                             : "the Candidate adapter exposes no enqueueEffect" };
      log("probe.effect_delivery_skipped", { reason: effectDelivery.reason });
      return;
    }
    const idempotencyKey = `probe-effect:${nonce}`;
    const deliveryId = `probe-delivery-${nonce}`;
    const effect = { deliveryId, effectId: `probe-effect-${nonce}`, idempotencyKey,
      toolName: "remote_create", kind: "remote_create", retryClass: "reconcile" };
    const authority = (cp: any) => ({ ownerId: cp.lease.ownerId, leaseEpoch: cp.lease.epoch,
      fencingToken: cp.lease.fencingToken, runGeneration: cp.runGeneration, expectedRevision: cp.revision });
    const snap = (cp: any) => ({ revision: cp?.revision ?? null, event_cursor: cp?.eventCursor ?? null,
      pending: (cp?.pendingEffects ?? []).length, completed: (cp?.completedEffects ?? []).length,
      pending_keys: keysOf(cp?.pendingEffects), completed_keys: keysOf(cp?.completedEffects) });
    const before = snap(live);
    let enqueueThrew = false, enqueueError: string | null = null;
    try {
      await adapter.enqueueEffect({ ...scope, ...authority(live), eventCursor: Number(live.eventCursor),
        effect: copy(effect), claimTtlMs: 5000, now: clock });
    } catch (error: any) { enqueueThrew = true; enqueueError = error?.name ?? "Error"; }
    const afterEnqueue = await safeInspect(adapter);
    const settlement = (cp: any, suffix: string) => ({ ...scope, ...authority(cp),
      eventId: `probe-settle-${nonce}-${suffix}`, eventCursor: Number(cp?.eventCursor ?? 0) + 1,
      type: "effect_completed", now: clock + 1,
      effect: { ...copy(effect), status: "completed", attempt: 1,
        runGeneration: cp?.runGeneration, enqueueRevision: cp?.revision } });
    const first = afterEnqueue?.lease ? await ingest(adapter, settlement(afterEnqueue, "a")) : { threw: true, error_name: "no-checkpoint" };
    const afterSettle = await safeInspect(adapter);
    // Same deliveryId and idempotencyKey under a NEW event id: event-id dedup
    // alone must not be enough to make this safe.
    const replay = afterSettle?.lease ? await ingest(adapter, settlement(afterSettle, "b")) : { threw: true, error_name: "no-checkpoint" };
    const afterReplay = await safeInspect(adapter);
    const staleBase = afterReplay?.lease ? afterReplay : afterSettle;
    const staleEvent = staleBase?.lease
      ? { ...settlement(staleBase, "c"), leaseEpoch: Number(staleBase.lease.epoch) + 9 }
      : null;
    const stale = staleEvent ? await ingest(adapter, staleEvent) : { threw: true, error_name: "no-checkpoint" };
    const afterStale = await safeInspect(adapter);
    const completedOnce = (cp: any) => keysOf(cp?.completedEffects).filter((key: any) => key === idempotencyKey).length;
    effectDelivery = { attempted: true, idempotency_key: idempotencyKey, delivery_id: deliveryId,
      before, after_enqueue: snap(afterEnqueue), after_settlement: snap(afterSettle),
      after_duplicate_delivery: snap(afterReplay), after_stale_settlement: snap(afterStale),
      enqueue_threw: enqueueThrew, enqueue_error: enqueueError, settlement_threw: first.threw,
      duplicate_threw: replay.threw, stale_threw: stale.threw,
      enqueued_durably: keysOf(afterEnqueue?.pendingEffects).includes(idempotencyKey)
        && (afterEnqueue?.pendingEffects ?? []).length === before.pending + 1,
      settled_exactly_once: completedOnce(afterSettle) === 1
        && !keysOf(afterSettle?.pendingEffects).includes(idempotencyKey),
      duplicate_delivery_changed_nothing: completedOnce(afterReplay) === completedOnce(afterSettle)
        && (afterReplay?.completedEffects ?? []).length === (afterSettle?.completedEffects ?? []).length
        && (afterReplay?.pendingEffects ?? []).length === (afterSettle?.pendingEffects ?? []).length,
      stale_settlement_changed_nothing: (afterStale?.completedEffects ?? []).length === (afterReplay?.completedEffects ?? []).length
        && Number(afterStale?.eventCursor) === Number(afterReplay?.eventCursor) };
    log("probe.effect_delivery", { idempotency_key: idempotencyKey,
      enqueued_durably: effectDelivery.enqueued_durably, settled_exactly_once: effectDelivery.settled_exactly_once });
  };
  const effectDeliveryHeld = () => Boolean(effectDelivery?.attempted)
    && effectDelivery.enqueued_durably === true && effectDelivery.settled_exactly_once === true
    && effectDelivery.duplicate_delivery_changed_nothing === true
    && effectDelivery.stale_settlement_changed_nothing === true;
  const afterAction = async (action: string, adapter: any, result: any) => {
    actionSequence += 1;
    const observedCheckpoint = result?.checkpoint ?? result?.inspection?.checkpoint ?? null;
    harvestSecrets(observedCheckpoint);
    actionLog.push({ sequence: actionSequence, action, restart_count: restartCount,
      run_generation: observedCheckpoint?.runGeneration ?? null,
      terminal_state: observedCheckpoint?.terminalState ?? null,
      revision: observedCheckpoint?.revision ?? null, event_cursor: observedCheckpoint?.eventCursor ?? null });
    if (observedCheckpoint && ["pause", "resume", "cancel", "recover_conversation", "acquire_recovery_lease"].includes(action)) {
      generationLog.push({ sequence: actionSequence, action, run_generation: observedCheckpoint.runGeneration,
        terminal_state: observedCheckpoint.terminalState, revision: observedCheckpoint.revision,
        cancellation_revision: observedCheckpoint.cancellationRevision ?? null });
    }
    // 0921: a probe is disarmed only once it has actually RUN its own precondition.
    // Both flags used to be set before the call, so a single lease-visibility miss --
    // the world re-inspects through the product and the action's own return said
    // "lease" while inspectRecovery did not (fh-003 test_001: an ownerId=null lease
    // written on action 3) -- forfeited OHR005/OHR007 for the rest of the case even
    // though action 4 produced a healthy lease that could have carried the probe.
    // Re-arming costs the agent no action: the world drives this traffic itself.
    const disciplineDue = fixture.runtimeEventDiscipline === true && !eventDisciplineDone;
    const outboxDue = fixture.effectDeliveryProbe === true && !effectDeliveryDone;
    if ((disciplineDue || outboxDue) && observedCheckpoint?.lease
        && ["acquire_recovery_lease", "recover_conversation"].includes(action)) {
      if (disciplineDue) {
        await runEventDisciplineProbe(adapter);
        eventDisciplineDone = eventDiscipline?.attempted === true;
      }
      if (outboxDue) {
        await runEffectDeliveryProbe(adapter);
        effectDeliveryDone = effectDelivery?.attempted === true;
      }
      // The world, not the agent, produced this traffic. Hand the product's own
      // current state back on the same observation so the agent is not taxed an
      // action from its 12-action budget to rediscover a revision it never moved.
      const refreshed = await safeInspect(adapter);
      if (refreshed) {
        if (result && typeof result === "object" && result.checkpoint) result.checkpoint = copy(refreshed);
        if (result?.inspection && typeof result.inspection === "object" && result.inspection.checkpoint) result.inspection.checkpoint = copy(refreshed);
        currentCheckpoint = copy(refreshed);
      }
    }
    if (["inspect_production_workspace", "reconcile_production_workspace"].includes(action)) {
      const reconcileResult = action === "reconcile_production_workspace" ? (result?.result ?? null) : null;
      const projection = result?.projection ?? reconcileResult?.projection ?? result?.workspace_projection ?? null;
      workspaceObservations.push({ sequence: actionSequence, action, restart_count: restartCount,
        restart_epoch: crashIndex, threw: Boolean(result?.observed_error),
        phase: projection?.phase ?? null, record_revision: projection?.recordRevision ?? null,
        conflict_count: projection?.conflictCount ?? null, error_code: projection?.errorCode ?? null,
        remote_committed: projection?.remoteCommitted ?? null, local_applied: projection?.localApplied ?? null,
        cursor_committed: projection?.cursorCommitted ?? null,
        completed: reconcileResult?.completed ?? null, suppressed: reconcileResult?.suppressed ?? null,
        stale_authority: reconcileResult?.staleAuthority ?? null, response_lost: reconcileResult?.responseLost ?? null,
        projection_keys: projection && typeof projection === "object" ? Object.keys(projection).sort() : null,
        projection_text: projection ? JSON.stringify(projection) : null });
    }
    // 0920: once the refusal has been proved durable across a browser restart,
    // the overlapping edit is resolved upstream -- the remote side reverts
    // README.md to the base content under a NEW manifest revision. The published
    // task says this will happen. A reconciler that only knows how to refuse, or
    // that replans from a cached remote head, cannot converge from here.
    if (fixture.workspaceConflictResolvedAfterRestart === true && !conflictResolved && restartCount >= 1
        && ["inspect_production_workspace", "reconcile_production_workspace"].includes(action)) {
      const latest = workspaceObservations[workspaceObservations.length - 1];
      if (latest && (latest.phase === "conflict" || latest.error_code === "conflict")) {
        conflictResolved = true;
        refusalSnapshot = { at_sequence: actionSequence,
          replicas_untouched: hash(localManifest) === hash(initialLocal) && hash(remoteManifest) === hash(initialRemote),
          write_chunk_calls: operations.filter(item => item.operation === "remote.writeChunk").length,
          stage_chunk_calls: operations.filter(item => item.operation === "local.stageChunk").length,
          local_applies: operations.filter(item => item.operation === "local.apply").length,
          remote_commits: committed.size, side_effects: effectReceipts.size };
        remoteManifest = manifest(`remote-resolved-${nonce}`, baseManifest.revision,
          [entry("README.md", "base"), entry(`remote-${nonce.slice(0, 6)}.txt`, "remote")]);
        log("fault.upstream_conflict_resolved", { resolved_path: "README.md",
          new_remote_revision: remoteManifest.revision, recovery_action_selected_by_environment: false });
      }
    }
    if (fixture.schemas && !migrationIncidentsDelivered && action === "recover_conversation" && result?.checkpoint?.schemaVersion === 3) {
      migrationIncidentsDelivered = true;
      const migrated = copy(result.checkpoint);
      externalEvents.push({ source: "model-completed-migration", checkpoint: migrated });
      // A competing browser retries the same old compare-and-set base. This
      // is external contention, not an evaluator recovery solution.
      if (fixture.sameRevisionMigrationRace && adapter.staleMigrationAttempt) {
        externalEvents.push({ source: "same-revision-migration-competitor", result: copy(await adapter.staleMigrationAttempt({
          ...scope, ownerId: `competing-owner-${nonce}`, expectedRevision: 1,
          workspace: migrated.workspace, ttlMs: 50, now: clock })) });
      }
      if (adapter.armRemovalFailure && fixture.removeFailureAfterPublication) {
        await adapter.armRemovalFailure(scope);
      }
      let current = (await adapter.inspectRecovery(scope))?.checkpoint ?? migrated;
      // The original compound workload contains sixteen normal runtime
      // notifications. Deliver them through the real public runtime boundary;
      // the product, not this world, decides how to persist and compact them.
      for (let index = 0; index < 16 && current?.lease; index++) {
        const event = { ...scope, ownerId: current.lease.ownerId, leaseEpoch: current.lease.epoch,
          fencingToken: current.lease.fencingToken, runGeneration: current.runGeneration,
          expectedRevision: current.revision, eventId: `migration-traffic-${nonce}-${index}`,
          eventCursor: current.eventCursor + 1, type: "resumed", now: clock + index + 1 };
        try {
          const observation = await adapter.ingestRuntimeEvent(event);
          current = observation?.checkpoint ?? current;
          externalEvents.push({ source: "normal-runtime-traffic", index, checkpoint: copy(current) });
        } catch (error: any) { externalEvents.push({ source: "normal-runtime-traffic", index, rejected: true, error_name: error.name }); }
      }
      if (adapter.corruptNewestRecords && fixture.corruption?.length) {
        const injected = await adapter.corruptNewestRecords(scope);
        externalEvents.push({ source: "external-storage-corruption", evidence: copy(injected) });
        log("fault.corrupt_and_future_records", injected);
      }
    }
    if (action === "reconcile_production_workspace" && crashIndex < crashBoundaries.length) {
      const boundary = crashBoundaries[crashIndex];
      const projection = result?.workspace_projection;
      const message = String(result?.observed_error?.message ?? "");
      const actual = {
        durable_record: Number(projection?.recordRevision) > 0,
        product_threw_current_boundary: message.includes(boundary),
        remote_commit_count: committed.size,
        local_apply_count: operations.filter(item => item.operation === "local.apply").length,
        replica_entries_converged: hash(localManifest.entries) === hash(remoteManifest.entries),
        projection: copy(projection ?? null),
      };
      let boundaryObserved = actual.durable_record && actual.product_threw_current_boundary;
      if (boundary === "after_plan") boundaryObserved &&= projection?.phase === "planned" && committed.size === 0 && actual.local_apply_count === 0;
      if (boundary === "after_chunk_transfer") boundaryObserved &&= remoteChunks.has(chunk("local")) && committed.size === 0;
      if (boundary === "after_remote_commit") boundaryObserved &&= projection?.remoteCommitted === true && committed.size === 1 && actual.local_apply_count === 0;
      if (boundary === "after_local_apply") boundaryObserved &&= projection?.localApplied === true && projection?.cursorCommitted === false && actual.local_apply_count === 1 && actual.replica_entries_converged;
      if (boundary === "after_cursor_commit") boundaryObserved &&= projection?.cursorCommitted === true && projection?.phase === "completed" && actual.replica_entries_converged;
      crashObservations.push({ boundary, actual, advanced: boundaryObserved });
      if (boundaryObserved) { crashIndex += 1; log("fault.workspace_process_restart", { after_boundary: boundary, storage_preserved: true, recovery_action_selected_by_environment: false }); }
    }
    const checkpoint = result?.checkpoint ?? result?.inspection?.checkpoint;
    if (fixture.aba && delayedAuthority && ["resume", "cancel", "recover_conversation"].includes(action)) {
      const previous = delayedAuthority;
      const staleCursor = Number(fixture.staleHighCursor ?? 9999);
      const event = { ...scope, ownerId: previous.lease.ownerId, leaseEpoch: previous.lease.epoch, fencingToken: previous.lease.fencingToken,
        runGeneration: previous.runGeneration, expectedRevision: previous.revision, eventId: `delayed-${nonce}-${externalEvents.length}`,
        eventCursor: staleCursor, type: "resumed", now: clock };
      // Whether this delivery is genuinely stale depends on what the agent did
      // first; the ledger below only judges deliveries that really were stale.
      const live = (await safeInspect(adapter)) ?? checkpoint ?? null;
      const probeIsStale = Boolean(live) && (Number(live.revision) !== Number(previous.revision)
        || Number(live.runGeneration) !== Number(previous.runGeneration)
        || Number(live.lease?.epoch ?? -1) !== Number(previous.lease.epoch));
      const before = { revision: live?.revision ?? null, event_cursor: live?.eventCursor ?? null,
        completed_effects: live?.completedEffects?.length ?? null, terminal_state: live?.terminalState ?? null };
      try {
        const observed = copy(await adapter.ingestRuntimeEvent(event));
        const after = await safeInspect(adapter);
        externalEvents.push({ source: "old-generation-callback", probe_is_stale: probeIsStale, stale_event_cursor: staleCursor,
          before, after: { revision: after?.revision ?? null, event_cursor: after?.eventCursor ?? null,
            completed_effects: after?.completedEffects?.length ?? null, terminal_state: after?.terminalState ?? null },
          result: observed });
      } catch (error: any) {
        externalEvents.push({ source: "old-generation-callback", probe_is_stale: probeIsStale, stale_event_cursor: staleCursor,
          before, rejected: true, error_name: error.name });
      }
      log("fault.delayed_callback_delivered", { previous_generation: previous.runGeneration, current_generation: checkpoint?.runGeneration, probe_is_stale: probeIsStale });
    }
    if ((fixture.aba || fixture.productionSeams) && ["resume", "cancel", "recover_conversation"].includes(action)) {
      for (const pending of pendingEffects.splice(0)) {
        pending.resolve(copy(pending.receipt));
        log("fault.effect_callback_released", { key: pending.key, after_model_action: action });
      }
      if (adapter.inspectRecovery) {
        try { externalEvents.push({ source: "after-real-delayed-effect-settlement", inspection: copy(await adapter.inspectRecovery(scope)) }); }
        catch (error: any) { externalEvents.push({ source: "after-real-delayed-effect-settlement", rejected: true, error_name: error?.name ?? "Error" }); }
      }
    }
    if (fixture.aba && action === "cancel" && checkpoint?.lease) {
      let current = checkpoint;
      lateTerminalBurst = { before: { terminal_state: current.terminalState, revision: current.revision,
        event_cursor: current.eventCursor, completed_effects: current.completedEffects?.length ?? 0,
        audit_length: current.audit?.length ?? 0, pending_effects: current.pendingEffects?.length ?? 0 },
        count: Number(fixture.lateTerminalEvents ?? 80), rejected: 0, accepted: 0, after: null };
      for (let index = 0; index < Number(fixture.lateTerminalEvents ?? 80); index++) {
        const event = { ...scope, ownerId: current.lease.ownerId, leaseEpoch: current.lease.epoch,
          fencingToken: current.lease.fencingToken, runGeneration: current.runGeneration, expectedRevision: current.revision,
          eventId: `late-terminal-${nonce}-${index}`, eventCursor: Number(current.eventCursor) + index + 2,
          type: index % 2 ? "effect_failed" : "effect_completed", now: clock + index,
          effect: { deliveryId: `late-delivery-${nonce}-${index}`, effectId: `late-effect-${nonce}-${index}`, idempotencyKey: `late:${nonce}:${index}`,
            toolName: "remote_create", kind: "remote_create", retryClass: "reconcile", status: "completed", attempt: 1,
            runGeneration: current.runGeneration, enqueueRevision: current.revision } };
        try {
          const observed = await adapter.ingestRuntimeEvent(event);
          current = observed?.checkpoint ?? current;
          lateTerminalBurst.accepted += 1;
          externalEvents.push({ source: "late-terminal-event", index, observed_terminal_state: current.terminalState,
            observed_completed_effects: current.completedEffects?.length, observed_audit_length: current.audit?.length });
        } catch (error: any) { lateTerminalBurst.rejected += 1; externalEvents.push({ source: "late-terminal-event", index, rejected: true, error_name: error.name }); }
      }
      const settled = (await safeInspect(adapter)) ?? current;
      lateTerminalBurst.after = { terminal_state: settled?.terminalState ?? null, revision: settled?.revision ?? null,
        event_cursor: settled?.eventCursor ?? null, completed_effects: settled?.completedEffects?.length ?? 0,
        audit_length: settled?.audit?.length ?? 0, pending_effects: settled?.pendingEffects?.length ?? 0,
        retained_valid_revisions: validScopedRecords().length };
      log("fault.late_terminal_burst", { count: Number(fixture.lateTerminalEvents ?? 80) });
    }
    if (fixture.grantLifecycle && action === "issue_inspection_grant" && result?.token) {
      issuedGrants.push({ token: result.token, grant: copy(result.grant) });
      const issuedLevel = result?.grant?.level ?? null;
      for (const [label, input] of [
        ["wrong-tab", { ...scope, tabId: `intruder-${nonce}`, grantToken: result.token, now: clock }],
        ["foreign-backend", { ...scope, backendId: `foreign-${nonce}`, tabId: `viewer-${nonce}`, grantToken: result.token, now: clock }],
        ["wrong-conversation", { ...scope, conversationId: `${scope.conversationId}-neighbour`, tabId: `viewer-${nonce}`, grantToken: result.token, now: clock }],
      ] as const) {
        try { externalEvents.push({ source: label, grant_level: issuedLevel, projection: copy(await adapter.inspectRecoveryAuthorized(input)) }); }
        catch (error: any) { externalEvents.push({ source: label, grant_level: issuedLevel, rejected: true, error_name: error.name }); }
      }
    }
    if (fixture.grantLifecycle && issuedGrants.length && ["inspect_recovery_authorized", "revoke_inspection_grant", "resume", "cancel"].includes(action)) {
      const issued = issuedGrants[issuedGrants.length - 1];
      let reason = action === "revoke_inspection_grant" ? "after-revocation" : "after-generation-change";
      if (action === "inspect_recovery_authorized") {
        const expiresAt = Number(issued.grant?.expiresAt);
        if (!Number.isFinite(expiresAt)) return;
        clock = Math.max(clock, expiresAt + 1); reason = "after-expiry";
      }
      try { externalEvents.push({ source: reason, grant_level: issued.grant?.level ?? null, projection: copy(await adapter.inspectRecoveryAuthorized({ ...scope, tabId: `viewer-${nonce}`, grantToken: issued.token, now: clock })) }); }
      catch (error: any) { externalEvents.push({ source: reason, grant_level: issued.grant?.level ?? null, rejected: true, error_name: error.name }); }
    }
    if (fixture.productionSeams && action === "freeze_production_sync") {
      const expiresAt = Number(result?.projection?.leaseExpiresAt);
      if (Number.isFinite(expiresAt)) {
        clock = Math.max(clock, expiresAt + 1);
        log("fault.frozen_browser_lease_expired", { prior_lease_expires_at: expiresAt, actual_clock: clock });
      }
      term += 1; log("fault.browser_takeover", { current_term: term });
      if (delayedSync?.resolve) {
        delayedSync.resolve(copy(delayedSync.response));
        log("fault.old_sync_response_released", { old_term: delayedSync.oldTerm, current_term: term, after_model_action: action });
        delayedSync.resolve = null;
      }
    }
    if (fixture.grantLifecycle && action === "inspect_recovery_authorized" && ["status", "support"].includes(result?.access)) {
      const issued = issuedGrants[issuedGrants.length - 1];
      externalEvents.push({ source: "authorized-viewer", grant_level: issued?.grant?.level ?? null, projection: copy(result) });
    }
    // 0920: least authority has a positive half. The world presents the freshly
    // issued token from the audience tab with a caller limit far larger than the
    // grant's own maxima. input/03 §11 already requires the grant's maxima to
    // win and the truncation flags to be truthful; this measures it.
    if (fixture.grantLimitProbe === true && action === "issue_inspection_grant" && result?.token) {
      const issued = issuedGrants[issuedGrants.length - 1];
      const live = await safeInspect(adapter);
      const durableEffects = Number(live?.completedEffects?.length ?? 0) + Number(live?.pendingEffects?.length ?? 0);
      const durableAudit = Number(live?.audit?.length ?? 0);
      try {
        const projection = copy(await adapter.inspectRecoveryAuthorized({ ...scope, tabId: `viewer-${nonce}`,
          grantToken: issued.token, now: clock, limits: { maxEffects: 4096, maxAuditEntries: 4096 } }));
        const checkpoint = projection?.checkpoint ?? null;
        const returnedEffects = checkpoint ? Number(checkpoint.completedEffects?.length ?? 0) + Number(checkpoint.pendingEffects?.length ?? 0) : 0;
        const returnedAudit = checkpoint ? Number(checkpoint.audit?.length ?? 0) : 0;
        grantLimitProbes.push({ source: "inflated-caller-limit", grant_level: issued.grant?.level ?? null,
          access: projection?.access ?? null, checkpoint_present: checkpoint !== null,
          summary_present: (projection?.summary ?? null) !== null,
          grant_max_effects: issued.grant?.maxEffects ?? null, grant_max_audit_entries: issued.grant?.maxAuditEntries ?? null,
          bounded_maxima: Number.isFinite(Number(issued.grant?.maxEffects)) && Number.isFinite(Number(issued.grant?.maxAuditEntries)),
          durable_effects: durableEffects, durable_audit_entries: durableAudit,
          returned_effects: returnedEffects, returned_audit_entries: returnedAudit,
          effects_within_grant_maximum: !checkpoint || returnedEffects <= Number(issued.grant?.maxEffects ?? -1),
          audit_within_grant_maximum: !checkpoint || returnedAudit <= Number(issued.grant?.maxAuditEntries ?? -1),
          effects_truncated_flag: projection?.effectsTruncated ?? null,
          audit_truncated_flag: projection?.auditTruncated ?? null,
          effects_truncation_truthful: Boolean(projection?.effectsTruncated) === (durableEffects > Number(issued.grant?.maxEffects ?? Infinity)),
          audit_truncation_truthful: Boolean(projection?.auditTruncated) === (durableAudit > Number(issued.grant?.maxAuditEntries ?? Infinity)),
          leaked_value_count: leakText(projection).length });
      } catch (error: any) {
        grantLimitProbes.push({ source: "inflated-caller-limit", grant_level: issued.grant?.level ?? null,
          rejected: true, error_name: error?.name ?? "Error" });
      }
    }
    if (action === "restart_product") {
      restartCount += 1; incarnation = `incarnation-${nonce}-restart-${restartCount}`;
      log("fault.browser_reopened", { new_incarnation: incarnation, recovery_action_selected_by_environment: false });
    }
    // 0920: a per-action durable-cursor trace read from the product itself. It
    // makes "the cursor never moved" and "the cursor accumulated every replay"
    // both visible facts instead of inferences.
    if (fixture.runtimeEventDiscipline === true || fixture.productionSeams) {
      const live = await safeInspect(adapter);
      if (live) lastLiveCheckpoint = copy(live);
      durableCursorMarks.push({ sequence: actionSequence, action, restart_count: restartCount, ...cursorSnap(live) });
    }
  };
  const validScopedRecords = () => {
    const prefix = `openhands:recovery:v3:${encodeURIComponent(scope.backendId)}:${encodeURIComponent(scope.conversationId)}:`;
    const result: any[] = [];
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      if (!key?.startsWith(prefix)) continue;
      try {
        const value = JSON.parse(storage.getItem(key)!);
        const unsigned = copy(value); delete unsigned.integrity;
        if (value.schemaVersion === 3 && value.backendId === scope.backendId && value.conversationId === scope.conversationId
            && value.integrity?.digest === `sha256:${hash(unsigned)}`) result.push(value);
      } catch { /* invalid records remain in actual storage but are not accepted as evidence of a valid checkpoint */ }
    }
    return result.sort((left, right) => right.revision - left.revision);
  };

  // ---------------------------------------------------------------------------
  // 0919 hardening: the required-outcome ledger.
  //
  // Every entry is derived only from what this trusted world actually observed.
  // `satisfied` is never a judgement about wording; it is a mechanical fact.
  // The semantic Result judge is told (in the rubric) to gate its dimensions on
  // this ledger, so completing one of four required outcomes can no longer read
  // as a completed task.
  // ---------------------------------------------------------------------------
  const opsNamed = (name: string) => operations.filter(item => item.operation === name);
  const opCount = (name: string) => opsNamed(name).length;
  const opFacts = (name: string) => opsNamed(name).map(item => item.facts);
  const ext = (source: string) => externalEvents.filter(item => item.source === source);
  const usedAction = (name: string) => actionLog.some(item => item.action === name);
  const leakText = (text: any) => {
    const value = typeof text === "string" ? text : JSON.stringify(text ?? null);
    return [...secretStrings].filter(secret => value.includes(secret));
  };
  const projectionsLeakFree = () => workspaceObservations.every(item => leakText(item.projection_text).length === 0);
  const boundedWorkspaceProjection = () => workspaceObservations.every(item => !item.projection_keys
    || !item.projection_keys.some((key: string) => /path|digest|content|token|chunkid|tabid|incarnation/i.test(key)));
  const staleCallbackProbes = () => ext("old-generation-callback").filter(item => item.probe_is_stale === true);
  const staleCallbackSuppressed = () => {
    const probes = staleCallbackProbes();
    return probes.length >= 1 && probes.every(item => item.rejected === true
      || (Number(item.after?.event_cursor) !== Number(item.stale_event_cursor)
          && Number(item.after?.event_cursor ?? 0) === Number(item.before?.event_cursor ?? 0)));
  };
  const deniedProbe = (item: any) => item.rejected === true
    || (item.projection && item.projection.access === "denied"
        && (item.projection.checkpoint ?? null) === null && (item.projection.summary ?? null) === null);
  const pathsOf = (value: any) => value.entries.map((item: any) => item.path).sort();
  const expectedUnionPaths = () => [...new Set([...initialLocal.entries, ...initialRemote.entries].map((item: any) => item.path))].sort();
  const convergedOnExpectedPaths = () => hash(localManifest.entries) === hash(remoteManifest.entries)
    && hash(pathsOf(localManifest)) === hash(expectedUnionPaths());
  const replicasUntouched = () => hash(localManifest) === hash(initialLocal) && hash(remoteManifest) === hash(initialRemote);
  const noWorkspaceSideEffect = () => opCount("remote.writeChunk") === 0 && opCount("local.stageChunk") === 0
    && committed.size === 0 && opCount("local.apply") === 0 && effectReceipts.size === 0
    && workspaceObservations.every(item => item.remote_committed !== true && item.local_applied !== true && item.cursor_committed !== true);
  const workspaceAfterRestart = () => workspaceObservations.filter(item => item.restart_count >= 1);
  // 0920 helpers.
  // The refusal is judged at the moment the world resolved the conflict, not at
  // the end of the run: after the upstream resolution the replicas are SUPPOSED
  // to move, and the refusal that came before it must still have been total.
  const refusalHeld = () => refusalSnapshot
    ? refusalSnapshot.replicas_untouched === true && refusalSnapshot.write_chunk_calls === 0
      && refusalSnapshot.stage_chunk_calls === 0 && refusalSnapshot.remote_commits === 0
      && refusalSnapshot.local_applies === 0 && refusalSnapshot.side_effects === 0
    : replicasUntouched() && noWorkspaceSideEffect();
  const staleCallbackSuppressedAboveZero = () => staleCallbackSuppressed()
    && staleCallbackProbes().every(item => Number(item.before?.event_cursor ?? 0) > 0);
  const REQUIRED_WORKSPACE_PROJECTION_KEYS = ["phase", "recordRevision", "conflictCount",
    "remoteCommitted", "localApplied", "cursorCommitted", "uploadedChunkCount", "downloadedChunkCount"];
  const workspaceProjectionComplete = () => workspaceObservations.length >= 1
    && workspaceObservations.every(item => Array.isArray(item.projection_keys)
      && REQUIRED_WORKSPACE_PROJECTION_KEYS.every(key => item.projection_keys.includes(key)));
  const maxDeliveredCursor = () => deliveredLegitimateCursors.length
    ? Math.max(...deliveredLegitimateCursors) : 0;
  const finalDurableCursor = () => Number(lastLiveCheckpoint?.eventCursor ?? -1);
  // The durable cursor is a high-water mark over the events this world
  // delivered, never their sum. `+2` leaves room for the product's own
  // workspace-completion event, which is published on top of the last one.
  const cursorIsHighWaterMark = () => finalDurableCursor() >= 0
    && finalDurableCursor() <= maxDeliveredCursor() + 2;
  const recentEventIdsDistinct = () => !Array.isArray(lastLiveCheckpoint?.recentEventIds)
    || (new Set(lastLiveCheckpoint.recentEventIds).size === lastLiveCheckpoint.recentEventIds.length
        && lastLiveCheckpoint.recentEventIds.length <= 64);
  const pullsUseDurableCursor = () => syncPulls.length >= 1
    && syncPulls.every(item => item.durable_cursor_at_pull === null
        || Number(item.request_cursor) === Number(item.durable_cursor_at_pull));
  const someDurableCursorAboveZero = () => syncPulls.some(item => Number(item.durable_cursor_at_pull ?? 0) > 0);
  const grantLimitsHonoured = () => grantLimitProbes.length >= 1
    && grantLimitProbes.every(item => item.rejected !== true
      && ["status", "support"].includes(item.access)
      && item.bounded_maxima === true && Number(item.leaked_value_count ?? 0) === 0
      && (item.access !== "status" || (item.checkpoint_present === false && item.summary_present === true))
      && (item.access !== "support" || (item.checkpoint_present === true
          && item.effects_within_grant_maximum === true && item.audit_within_grant_maximum === true
          && item.effects_truncation_truthful === true && item.audit_truncation_truthful === true)));
  const retentionCeiling = Number(fixture.maxRetainedRevisions ?? 8);

  const outcome = (id: string, requirement: string, satisfied: boolean | null, observed: any, safety = false) =>
    ({ id, requirement, safety, satisfied, observed: copy(observed) });

  const requiredOutcomes = () => {
    const writes = opFacts("remote.writeChunk").map((item: any) => item.digest);
    const localOnlyChunk = chunk("local");
    const firstMigration = ext("model-completed-migration")[0]?.checkpoint ?? null;
    const race = ext("same-revision-migration-competitor")[0]?.result ?? null;
    const corrupted = ext("external-storage-corruption")[0]?.evidence ?? null;
    const valid = validScopedRecords();
    const denialSources = ["wrong-tab", "foreign-backend", "wrong-conversation"];
    const denialProbes = externalEvents.filter(item => denialSources.includes(item.source));
    const lifecycleProbes = ["after-revocation", "after-expiry", "after-generation-change"].flatMap(ext);
    const grantedViews = ext("authorized-viewer");
    const lifecycle = generationLog.filter(item => ["pause", "resume", "cancel"].includes(item.action));
    const iPause = lifecycle.findIndex(item => item.action === "pause");
    const iResume = lifecycle.findIndex((item, index) => item.action === "resume" && index > iPause && iPause >= 0);
    const iCancel = lifecycle.findIndex((item, index) => item.action === "cancel" && index > iResume && iResume >= 0);

    switch (caseId) {
      case "test_001": return [
        outcome("OHR001", "The reconciler must return an explicit unresolved conflict for the path both sides changed.",
          workspaceObservations.some(item => item.action === "reconcile_production_workspace" && item.completed === false
            && (item.error_code === "conflict" || item.phase === "conflict") && Number(item.conflict_count) >= 1),
          { workspace_observations: workspaceObservations.map(({ projection_text, ...rest }) => rest) }),
        outcome("OHR002", "While the conflict stands the whole transaction is blocked: no chunk transfer, staging, remote commit, local apply, ledger effect or cursor write, and both replicas byte-identical to their inputs.",
          refusalHeld(),
          { judged_at: refusalSnapshot ? "the moment the conflict was resolved upstream" : "end of run",
            refusal_snapshot: copy(refusalSnapshot), upstream_conflict_resolved: conflictResolved,
            end_of_run_replicas_untouched: replicasUntouched(), write_chunk_calls: opCount("remote.writeChunk"),
            stage_chunk_calls: opCount("local.stageChunk"), remote_commits: committed.size,
            local_applies: opCount("local.apply"), side_effects: effectReceipts.size }, true),
        outcome("OHR003", "The refusal must be durable: after the browser product is restarted, the workspace record must still report the same unfinished conflict.",
          restartCount >= 1 && workspaceAfterRestart().some(item => Number(item.record_revision) >= 1
            && item.remote_committed === false && item.local_applied === false && item.cursor_committed === false),
          { restarts: restartCount, observations_after_restart: workspaceAfterRestart().map(({ projection_text, ...rest }) => rest) }),
        outcome("OHR004", "The unreturned callback from the previous browser generation must be suppressed and must leave the non-zero durable event cursor bit-identical.",
          staleCallbackSuppressedAboveZero(),
          { stale_probes: staleCallbackProbes().map(({ result, ...rest }) => rest),
            all_probes: ext("old-generation-callback").length,
            durable_cursor_trace: copy(durableCursorMarks) }, true),
        outcome("OHR005", "Runtime-event discipline: a fully authorized fresh event is accepted once and moves the cursor to exactly its own eventCursor; the identical event id re-delivered changes nothing; a forged fencing token and a wrong lease epoch are both suppressed with the cursor bit-identical.",
          eventDisciplineHeld(), { discipline: copy(eventDiscipline) }, true),
        outcome("OHR006", "After the overlapping edit is resolved upstream the reconciler must re-read the remote head and converge exactly once: one durable remote commit, one local apply, both replicas on the union of the three paths, and the completion cursor published last.",
          conflictResolved && committed.size === 1 && opCount("local.apply") === 1 && convergedOnExpectedPaths()
            && workspaceObservations.some(item => item.restart_count >= 1 && item.phase === "completed"
              && item.remote_committed === true && item.local_applied === true && item.cursor_committed === true),
          { upstream_conflict_resolved: conflictResolved, remote_commits: committed.size,
            local_applies: opCount("local.apply"), expected_paths: expectedUnionPaths(),
            observed_local_paths: pathsOf(localManifest), observed_remote_paths: pathsOf(remoteManifest),
            observations_after_restart: workspaceAfterRestart().map(({ projection_text, ...rest }) => rest) }, true),
        outcome("OHR007", "The durable outbox must hold: a well-formed effect enqueued under the live authority becomes a durable pending effect; its settlement completes it exactly once; the same deliveryId and idempotency key delivered again under a new event id changes nothing; and a settlement under a stale lease epoch changes nothing.",
          effectDeliveryHeld(), { effect_delivery: copy(effectDelivery) }, true),
      ];
      case "test_002": return [
        outcome("OHR101", "Exactly the genuinely missing content-addressed chunk is uploaded, once, even though its first write response was lost.",
          writes.length >= 1 && writes.length === new Set(writes).size && writes.every((digest: string) => digest === localOnlyChunk),
          { write_chunk_digests: writes, expected_single_digest: localOnlyChunk }),
        outcome("OHR102", "Every injected workspace crash boundary is observed durably and then recovered by a fresh reconciler.",
          crashBoundaries.length > 0 && crashIndex === crashBoundaries.length,
          { required: copy(crashBoundaries), reached: crashIndex, attempts: copy(crashObservations) }, true),
        outcome("OHR103", "The uncertain remote commit is reconciled rather than re-issued, and the workspace converges exactly once.",
          committed.size === 1 && opCount("remote.reconcileCommit") >= 1 && opCount("local.apply") === 1 && convergedOnExpectedPaths(),
          { remote_commits: committed.size, reconcile_commit_calls: opCount("remote.reconcileCommit"),
            local_applies: opCount("local.apply"), expected_paths: expectedUnionPaths(),
            observed_local_paths: pathsOf(localManifest), observed_remote_paths: pathsOf(remoteManifest) }, true),
        outcome("OHR104", "The completion must survive the browser: after a product restart against the same durable storage the completion cursor is still published and every stage reads committed.",
          restartCount >= 1 && workspaceAfterRestart().some(item => item.phase === "completed"
            && item.remote_committed === true && item.local_applied === true && item.cursor_committed === true),
          { restarts: restartCount, observations_after_restart: workspaceAfterRestart().map(({ projection_text, ...rest }) => rest) }, true),
        outcome("OHR105", "Runtime-event discipline: a fully authorized fresh event is accepted once and moves the cursor to exactly its own eventCursor; the identical event id re-delivered changes nothing; a forged fencing token and a wrong lease epoch are both suppressed with the cursor bit-identical.",
          eventDisciplineHeld(), { discipline: copy(eventDiscipline), durable_cursor_trace: copy(durableCursorMarks) }, true),
      ];
      case "test_003": return [
        outcome("OHR201", "The user's cancellation is the final durable lifecycle state with a recorded cancellation revision.",
          valid[0]?.terminalState === "cancelled" && Number.isSafeInteger(Number(valid[0]?.cancellationRevision)),
          { terminal_state: valid[0]?.terminalState ?? null, cancellation_revision: valid[0]?.cancellationRevision ?? null }, true),
        outcome("OHR202", "The burst of late terminal callbacks after cancellation changes nothing and leaves audit and retention bounded.",
          Boolean(lateTerminalBurst?.after) && lateTerminalBurst.after.terminal_state === "cancelled"
            && lateTerminalBurst.after.completed_effects === lateTerminalBurst.before.completed_effects
            && lateTerminalBurst.after.audit_length <= 64
            && lateTerminalBurst.after.retained_valid_revisions <= retentionCeiling,
          { burst: copy(lateTerminalBurst), retention_ceiling: retentionCeiling }, true),
        outcome("OHR203", "The delayed callback from the previous generation is suppressed and never advances the durable event cursor.",
          staleCallbackSuppressed(),
          { stale_probes: staleCallbackProbes().map(({ result, ...rest }) => rest),
            all_probes: ext("old-generation-callback").length }, true),
        outcome("OHR204", "Pause, then resume, then cancel are actually exercised and each one strictly increases the run generation.",
          iPause >= 0 && iResume > iPause && iCancel > iResume
            && Number(lifecycle[iResume]?.run_generation) > Number(lifecycle[iPause]?.run_generation)
            && Number(lifecycle[iCancel]?.run_generation) > Number(lifecycle[iResume]?.run_generation),
          { lifecycle_transitions: copy(lifecycle) }),
      ];
      case "test_004": return [
        outcome("OHR301", "A grant token presented by the wrong tab, a foreign backend or a neighbouring conversation is denied with no checkpoint or summary.",
          denialSources.every(source => ext(source).length >= 1) && denialProbes.length >= 1 && denialProbes.every(deniedProbe),
          { probes: denialProbes.map(({ projection, ...rest }) => ({ ...rest, access: projection?.access ?? null,
            checkpoint_present: (projection?.checkpoint ?? null) !== null, summary_present: (projection?.summary ?? null) !== null })) }, true),
        outcome("OHR302", "Both levels are really implemented: a status grant and a support grant are issued under the current lease, both are used by the audience tab, and the support inspection returns an actual bounded checkpoint projection. Denying everything is not least authority.",
          issuedGrants.some(item => item.grant?.level === "status") && issuedGrants.some(item => item.grant?.level === "support")
            && grantedViews.some(item => item.projection?.access === "status")
            && grantedViews.some(item => item.projection?.access === "support"
                && (item.projection?.checkpoint ?? null) !== null),
          { issued_levels: issuedGrants.map(item => item.grant?.level ?? null),
            granted_views: grantedViews.map(item => ({ grant_level: item.grant_level, access: item.projection?.access ?? null,
              checkpoint_present: (item.projection?.checkpoint ?? null) !== null,
              summary_present: (item.projection?.summary ?? null) !== null })) }, true),
        outcome("OHR303", "After revocation, expiry or a generation change the same token is denied.",
          lifecycleProbes.length >= 1 && lifecycleProbes.every(deniedProbe),
          { probes: lifecycleProbes.map(({ projection, ...rest }) => ({ ...rest, access: projection?.access ?? null,
            checkpoint_present: (projection?.checkpoint ?? null) !== null })) }, true),
        outcome("OHR304", "A granted inspection carries no fencing or claim token, a status-level grant returns no checkpoint projection, and a status-level grant still returns a usable bounded status summary.",
          grantedViews.length >= 1
            && [...grantedViews, ...denialProbes, ...lifecycleProbes].every(item => leakText(item.projection).length === 0)
            && !grantedViews.some(item => item.projection?.access === "status" && (item.projection?.checkpoint ?? null) !== null)
            && grantedViews.filter(item => item.projection?.access === "status").every(item => (item.projection?.summary ?? null) !== null)
            && projectionsLeakFree(),
          { granted_views: grantedViews.map(item => ({ grant_level: item.grant_level, access: item.projection?.access ?? null,
              checkpoint_present: (item.projection?.checkpoint ?? null) !== null,
              summary_present: (item.projection?.summary ?? null) !== null,
              leaked_value_count: leakText(item.projection).length })),
            harvested_value_count: secretStrings.size }, true),
        outcome("OHR305", "Every grant carries bounded maxima, and when the caller asks for far more than the grant allows the grant's own maxima win and the truncation flags tell the truth.",
          grantLimitsHonoured(), { probes: copy(grantLimitProbes) }, true),
      ];
      case "test_005": return [
        outcome("OHR401", "The legacy unscoped v2 lineage migrates once into default-local and keeps its cursor, completed effect, uncertain effect and event ids.",
          Boolean(firstMigration) && firstMigration.backendId === "default-local" && Number(firstMigration.schemaVersion) === 3
            && Number(firstMigration.eventCursor) === Number(legacyExpected?.eventCursor)
            && (firstMigration.completedEffects ?? []).some((item: any) => item.idempotencyKey === legacyExpected?.completedEffects?.[0]?.idempotencyKey)
            && (firstMigration.pendingEffects ?? []).some((item: any) => item.idempotencyKey === legacyExpected?.pendingEffects?.[0]?.idempotencyKey)
            && (firstMigration.recentEventIds ?? []).includes(legacyExpected?.recentEventIds?.[0]),
          { migrated_backend: firstMigration?.backendId ?? null, migrated_schema: firstMigration?.schemaVersion ?? null,
            migrated_event_cursor: firstMigration?.eventCursor ?? null, expected_event_cursor: legacyExpected?.eventCursor ?? null }, true),
        outcome("OHR402", "Two competitors replaying the same legacy compare-and-set base cannot both win: at most one of them may take the lineage forward.",
          Array.isArray(race?.outcomes) && race.outcomes.length === 2
            && race.outcomes.filter((item: any) => item.rejected !== true).length <= 1,
          { competitor_outcomes: race?.outcomes?.map((item: any) => ({ rejected: item.rejected === true })) ?? null }, true),
        outcome("OHR403", "The foreign backend record and the sync and workspace namespaces are byte-identical after migration and compaction.",
          namespaceSentinels.size >= 1 && [...namespaceSentinels].every(([key, expected]) => storage.getItem(key) === expected),
          { sentinels: [...namespaceSentinels].map(([key, expected]) => ({ namespace: key.split(":")[1], unchanged: storage.getItem(key) === expected })) }, true),
        outcome("OHR404", "Compaction stays scope-exact and bounded, and a valid predecessor still restores the conversation after the newest record is tampered with and a future-version record is injected.",
          corrupted?.injected === true && valid.length >= 1 && valid.length <= retentionCeiling
            && actionLog.some(item => item.restart_count >= 1 && item.revision !== null
                && ["inspect_recovery", "recover_conversation"].includes(item.action)),
          { corruption: copy(corrupted), valid_retained_revisions: valid.length, retention_ceiling: retentionCeiling,
            restarts: restartCount }),
      ];
      case "test_006": return [
        outcome("OHR501", "The lost transport response is retried from the same durable cursor rather than skipped.",
          syncPulls.length >= 2 && Number(syncPulls[0].request_cursor ?? -1) === Number(syncPulls[1].request_cursor ?? -2),
          { pulls: copy(syncPulls) }, true),
        outcome("OHR502", "Every transport pull asks from the verified durable ledger cursor, never from a channel notice or an advisory remote cursor -- and the durable cursor the pulls are taken from is the real, already-advanced one, not a constant zero.",
          pullsUseDurableCursor() && someDurableCursorAboveZero(),
          { pulls: copy(syncPulls), durable_cursor_trace: copy(durableCursorMarks),
            some_pull_above_zero: someDurableCursorAboveZero() }, true),
        outcome("OHR503", "The uncertain production remote commit is reconciled once, applied locally once, and the replicas converge.",
          committed.size === 1 && opCount("remote.reconcileCommit") >= 1 && opCount("local.apply") === 1 && convergedOnExpectedPaths(),
          { remote_commits: committed.size, reconcile_commit_calls: opCount("remote.reconcileCommit"),
            local_applies: opCount("local.apply"), expected_paths: expectedUnionPaths(),
            observed_local_paths: pathsOf(localManifest), observed_remote_paths: pathsOf(remoteManifest) }, true),
        outcome("OHR504", "The production sync coordinator and the production workspace reconciler are actually driven, and the replayed history is applied at most once: the durable event cursor is a high-water mark over the delivered events, never an accumulation of them, and no event id is retained twice.",
          usedAction("start_production_sync") && usedAction("sync_production") && usedAction("reconcile_production_workspace")
            && deliveredLegitimateCursors.length >= 1 && cursorIsHighWaterMark() && recentEventIdsDistinct(),
          { actions: actionLog.map(item => item.action), delivered_event_cursors: copy(deliveredLegitimateCursors),
            max_delivered_event_cursor: maxDeliveredCursor(), final_durable_event_cursor: finalDurableCursor(),
            recent_event_ids: Array.isArray(lastLiveCheckpoint?.recentEventIds) ? lastLiveCheckpoint.recentEventIds.length : null,
            distinct_recent_event_ids: Array.isArray(lastLiveCheckpoint?.recentEventIds) ? new Set(lastLiveCheckpoint.recentEventIds).size : null,
            durable_cursor_trace: copy(durableCursorMarks) }, true),
        outcome("OHR505", "No side effect is executed twice, and the public workspace projection is both complete and bounded: it carries every published stage field and no path, digest, chunk identifier, tab or incarnation identity or token.",
          opCount("effect.duplicate_execution") === 0 && workspaceProjectionComplete()
            && boundedWorkspaceProjection() && projectionsLeakFree(),
          { duplicate_executions: opCount("effect.duplicate_execution"),
            required_projection_keys: copy(REQUIRED_WORKSPACE_PROJECTION_KEYS),
            projection_complete: workspaceProjectionComplete(),
            projection_keys: workspaceObservations.map(item => item.projection_keys) }, true),
        outcome("OHR506", "Runtime-event discipline: a fully authorized fresh event is accepted once and moves the cursor to exactly its own eventCursor; the identical event id re-delivered changes nothing; a forged fencing token and a wrong lease epoch are both suppressed with the cursor bit-identical.",
          eventDisciplineHeld(), { discipline: copy(eventDiscipline) }, true),
        outcome("OHR507", "The durable outbox must hold: a well-formed effect enqueued under the live authority becomes a durable pending effect; its settlement completes it exactly once; the same deliveryId and idempotency key delivered again under a new event id changes nothing; and a settlement under a stale lease epoch changes nothing. OHR505's \"no effect twice\" is only meaningful next to this.",
          effectDeliveryHeld(), { effect_delivery: copy(effectDelivery) }, true),
      ];
      case "dev_001": return [
        outcome("OHR901", "Only the genuinely missing chunk is uploaded, once, after its write response is lost.",
          writes.length >= 1 && writes.length === new Set(writes).size && writes.every((digest: string) => digest === localOnlyChunk),
          { write_chunk_digests: writes, expected_single_digest: localOnlyChunk }),
        outcome("OHR902", "The workspace converges exactly once on the union of the local-only and remote-only paths.",
          committed.size === 1 && opCount("local.apply") === 1 && convergedOnExpectedPaths(),
          { remote_commits: committed.size, local_applies: opCount("local.apply"),
            expected_paths: expectedUnionPaths(), observed_local_paths: pathsOf(localManifest) }, true),
        outcome("OHR903", "The lost transport response is retried from the same durable cursor.",
          syncPulls.length >= 2 && Number(syncPulls[0].request_cursor ?? -1) === Number(syncPulls[1].request_cursor ?? -2),
          { pulls: copy(syncPulls) }, true),
        outcome("OHR904", "No side effect runs twice and the public workspace projection is complete, bounded and token-free.",
          opCount("effect.duplicate_execution") === 0 && workspaceProjectionComplete()
            && boundedWorkspaceProjection() && projectionsLeakFree(),
          { duplicate_executions: opCount("effect.duplicate_execution"),
            projection_complete: workspaceProjectionComplete() }, true),
        outcome("OHR905", "Runtime-event discipline: a fully authorized fresh event is accepted once and moves the cursor to exactly its own eventCursor; the identical event id re-delivered changes nothing; a forged fencing token and a wrong lease epoch are both suppressed with the cursor bit-identical.",
          eventDisciplineHeld(), { discipline: copy(eventDiscipline), durable_cursor_trace: copy(durableCursorMarks) }, true),
      ];
      case "dev_002": return [
        outcome("OHR911", "A grant presented by the wrong tab, a foreign backend or a neighbouring conversation is denied with no private detail.",
          denialSources.every(source => ext(source).length >= 1) && denialProbes.length >= 1 && denialProbes.every(deniedProbe),
          { probes: denialProbes.map(({ projection, ...rest }) => ({ ...rest, access: projection?.access ?? null })) }, true),
        outcome("OHR912", "The delayed callback from the replaced browser owner is suppressed and never advances the durable cursor.",
          staleCallbackSuppressed(),
          { stale_probes: staleCallbackProbes().map(({ result, ...rest }) => rest) }, true),
        outcome("OHR913", "The uncertain remote commit is reconciled once and applied locally once.",
          committed.size === 1 && opCount("remote.reconcileCommit") >= 1 && opCount("local.apply") === 1,
          { remote_commits: committed.size, reconcile_commit_calls: opCount("remote.reconcileCommit"),
            local_applies: opCount("local.apply") }, true),
        outcome("OHR914", "No granted inspection carries a fencing or claim token.",
          grantedViews.length >= 1 && grantedViews.every(item => leakText(item.projection).length === 0) && projectionsLeakFree(),
          { granted_views: grantedViews.length, harvested_value_count: secretStrings.size }, true),
      ];
      default: return [];
    }
  };
  const comparisons = () => ({
    case_id: caseId, fixture_digest: hash(fixture), scope,
    inputs: { initial_local_manifest: initialLocal, initial_remote_manifest: initialRemote, base_manifest: baseManifest,
      legacy_checkpoint: legacyExpected },
    observed: { local_manifest: copy(localManifest), remote_manifest: copy(remoteManifest), operations: copy(operations), external_callbacks: copy(externalEvents), durable_remote_commits: [...committed.values()], remote_effect_receipts: [...effectReceipts.values()], sync_responses: copy(syncResponses) },
    workspace_crash_coverage: { required: copy(crashBoundaries), reached: crashIndex,
      all_required_boundaries_observed: crashIndex === crashBoundaries.length, attempts: copy(crashObservations) },
    comparisons: {
      conflict_present_in_input: conflict,
      conflicting_replicas_preserved: conflict ? (refusalSnapshot ? refusalSnapshot.replicas_untouched
        : (hash(localManifest) === hash(initialLocal) && hash(remoteManifest) === hash(initialRemote))) : null,
      upstream_conflict_resolved: conflictResolved,
      refusal_snapshot: copy(refusalSnapshot),
      durable_cursor_trace: copy(durableCursorMarks),
      runtime_event_discipline: copy(eventDiscipline),
      effect_delivery_probe: copy(effectDelivery),
      replica_entries_converged: hash(localManifest.entries) === hash(remoteManifest.entries),
      expected_file_paths: [...new Set([...initialLocal.entries, ...initialRemote.entries].map(entry => entry.path))].sort(),
      observed_local_file_paths: localManifest.entries.map(entry => entry.path).sort(),
      observed_remote_file_paths: remoteManifest.entries.map(entry => entry.path).sort(),
      side_effect_execution_count: effectReceipts.size,
      migration_expected: legacyExpected ? { backendId: "default-local", schemaVersion: 3,
        preserved_event_cursor: legacyExpected.eventCursor, completed_effects: legacyExpected.completedEffects,
        uncertain_effects: legacyExpected.pendingEffects, recent_event_ids: legacyExpected.recentEventIds,
        max_retained_revisions: fixture.maxRetainedRevisions, compaction_must_have_verified_anchor: true } : null,
      migration_observed: fixture.schemas ? { first_checkpoint: externalEvents.find(item => item.source === "model-completed-migration")?.checkpoint ?? null,
        current_valid_checkpoint: validScopedRecords()[0] ?? null, valid_retained_revision_count: validScopedRecords().length,
        competing_old_revision_outcomes: externalEvents.find(item => item.source === "same-revision-migration-competitor")?.result ?? null } : null,
      sync_lost_response_requested_cursor: syncResponses[0]?.request.sinceEventCursor ?? null,
      sync_retry_requested_same_cursor: syncResponses.length > 1 ? syncResponses[0].request.sinceEventCursor === syncResponses[1].request.sinceEventCursor : null,
      sync_duplicate_recovery_events_have_distinct_ui_ids: syncResponses.length ? syncResponses.every(item => item.response.events[0].recoveryEvent.eventId === item.response.events[1].recoveryEvent.eventId && item.response.events[0].ordinaryEvent.id !== item.response.events[1].ordinaryEvent.id) : null,
      namespace_preservation: [...namespaceSentinels].map(([key, expected]) => ({ namespace: key.split(":")[1], scope_key: key, expected_sha256: hash(expected), observed_sha256: hash(storage.getItem(key)), unchanged: storage.getItem(key) === expected })),
      expected_convergence: !conflict || conflictResolved,
      required_outcomes: requiredOutcomes(),
      required_outcomes_total: requiredOutcomes().length,
      required_outcomes_satisfied: requiredOutcomes().filter(item => item.satisfied === true).length,
      required_safety_outcomes_failed: requiredOutcomes().filter(item => item.safety && item.satisfied !== true).map(item => item.id),
      product_actions_taken: actionLog.map(item => item.action),
    }, automatically_computed_score: null,
  });
  return { scope, fixture, now: () => clock, baseManifest, local, transport,
    workspaceFault: () => ({ crash_after: crashBoundaries[crashIndex] ?? null, restart_epoch: crashIndex }),
    authority: { current: () => ({ term, tabId: `tab-${nonce}`, incarnationId: incarnation, runGeneration: currentGeneration }) },
    visibleNotice, beforeAction, afterAction, comparisons, executeEffect, reconcileEffect };
}
