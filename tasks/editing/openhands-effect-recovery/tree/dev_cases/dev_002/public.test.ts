import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import fixture from "./assets/scenario.json";
import ConversationService from "#/api/conversation-service/conversation-service.api";
import AgentServerRuntimeService from "#/api/runtime-service/agent-server-runtime-service";
import { RecoveryStatus } from "#/components/features/conversation/recovery-status";
import { useRecoveryStore } from "#/stores/recovery-store";
import { useEventStore } from "#/stores/use-event-store";
import {
  createRecoveryEvaluatorAdapter,
  recoveryStorageKey,
} from "#/api/recovery/recovery-evaluator-adapter";
import type { ExecuteEffectInput } from "#/api/recovery/recovery-evaluator-adapter";
import {
  mutableWorkspaceFence,
  PublicLocalReplica,
  PublicWorkspaceTransport,
  workspaceBytes,
  workspaceDigests,
  workspaceEntry,
  workspaceManifest,
} from "../workspace-helpers";
import {
  adapter,
  authority,
  PublicStorage,
  PublicSyncChannel,
  scope,
} from "../helpers";

it("fences an expired-lease race and enforces tab/backend grant scope", () => {
  const storage = new PublicStorage();
  const primaryScope = scope(fixture.conversationId, fixture.backendId);
  const first = adapter(storage, 100, "public-server-a");
  let state = first.createCheckpoint({
    ...primaryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 100,
  });
  state = first.acquireRecoveryLease({
    ...primaryScope,
    ownerId: "old-runtime",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 5,
    now: 100,
  });
  const old = state.checkpoint!;
  const expectedRevision = old.revision;

  const contenderA = adapter(storage.restart(), 106, "public-server-b");
  const contenderB = adapter(storage.restart(), 106, "public-server-c");
  const winner = contenderA.recoverConversation({
    ...primaryScope,
    ownerId: "new-runtime",
    expectedRevision,
    workspace: fixture.workspace,
    ttlMs: 100,
    now: 106,
  });
  expect(winner.checkpoint!.lease!.epoch).toBe(old.lease!.epoch + 1);
  expect(winner.checkpoint!.lease!.fencingToken).not.toBe(
    old.lease!.fencingToken,
  );
  expect(winner.checkpoint!.runGeneration).toBe(old.runGeneration + 1);
  expect(() =>
    contenderB.recoverConversation({
      ...primaryScope,
      ownerId: "losing-runtime",
      expectedRevision,
      workspace: fixture.workspace,
      ttlMs: 100,
      now: 106,
    }),
  ).toThrow();

  const issued = contenderA.issueInspectionGrant({
    ...primaryScope,
    ...authority(winner),
    audienceTabId: fixture.viewerTabId,
    level: "status",
    ttlMs: 50,
    maxEffects: 1,
    maxAuditEntries: 0,
    now: 107,
  });
  const allowed = contenderB.inspectRecoveryAuthorized({
    ...primaryScope,
    tabId: fixture.viewerTabId,
    grantToken: issued.token,
    now: 108,
  });
  expect(allowed.access).toBe("status");
  expect(allowed.summary).not.toBeNull();
  expect(allowed.checkpoint).toBeNull();

  const wrongTab = contenderB.inspectRecoveryAuthorized({
    ...primaryScope,
    tabId: "public-tab-wrong",
    grantToken: issued.token,
    now: 108,
  });
  const wrongBackend = contenderB.inspectRecoveryAuthorized({
    ...scope(fixture.conversationId, fixture.foreignBackendId),
    tabId: fixture.viewerTabId,
    grantToken: issued.token,
    now: 108,
  });
  expect(wrongTab).toEqual(
    expect.objectContaining({
      access: "denied",
      boundary: "forbidden",
      summary: null,
      checkpoint: null,
    }),
  );
  expect(wrongBackend).toEqual(
    expect.objectContaining({
      access: "denied",
      boundary: "forbidden",
      summary: null,
      checkpoint: null,
    }),
  );
});

it("hands reconnect sessions off from the durable recovery cursor", () => {
  expect(fixture.dispatcherReconnectHandoff).toBe(true);
  localStorage.clear();
  const recoveryScope = scope("public-dispatcher-reconnect", fixture.backendId);
  let state = ConversationService.createCheckpoint({
    ...recoveryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 150,
  });
  state = ConversationService.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "public-dispatcher-owner",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 150,
  });

  const ordinaryEvents: Array<{ recoveryEventId: string; wrapperId: number }> =
    [];
  const terminalChunks: string[] = [];
  const dispatcher = ConversationService.createRecoveryEventDispatcher({
    emitOrdinaryEvent(event: unknown) {
      const ordinary = event as {
        recoveryEventId: string;
        wrapperId: number;
      };
      expect(
        ConversationService.inspectRecovery(recoveryScope).checkpoint!
          .recentEventIds,
      ).toContain(ordinary.recoveryEventId);
      ordinaryEvents.push(ordinary);
    },
    appendTerminalOutput(chunk) {
      terminalChunks.push(chunk.chunkId);
    },
  });
  const oldSession = dispatcher.openSession({
    ...recoveryScope,
    sessionId: "public-socket",
  });
  const replacement = dispatcher.openSession({
    ...recoveryScope,
    sessionId: "public-socket",
  });
  expect(oldSession.sinceEventCursor).toBe(1);
  expect(replacement.sinceEventCursor).toBe(1);
  expect(replacement.generation).toBeGreaterThan(oldSession.generation);

  const stale = dispatcher.dispatch({
    session: oldSession,
    recoveryEvent: {
      ...recoveryScope,
      ...authority(state),
      eventId: "public-stale-session-event",
      eventCursor: 9999,
      type: "resumed",
      now: 151,
    },
    ordinaryEvent: {
      recoveryEventId: "public-stale-session-event",
      wrapperId: 700,
    },
    terminalOutput: {
      streamId: "public-terminal",
      chunkId: "public-stale-chunk",
      text: "must not appear",
    },
  });
  expect(stale).toEqual(
    expect.objectContaining({
      accepted: false,
      suppressed: true,
      staleSession: true,
    }),
  );
  expect(
    ConversationService.inspectRecovery(recoveryScope).checkpoint!.eventCursor,
  ).toBe(1);

  const recoveryEvent = {
    ...recoveryScope,
    ...authority(state),
    eventId: "public-history-socket-event",
    eventCursor: 2,
    type: "resumed" as const,
    now: 152,
  };
  const accepted = dispatcher.dispatch({
    session: replacement,
    recoveryEvent,
    ordinaryEvent: {
      recoveryEventId: recoveryEvent.eventId,
      wrapperId: 701,
    },
    terminalOutput: {
      streamId: "public-terminal",
      chunkId: "public-terminal-chunk-1",
      text: "one durable terminal line",
    },
  });
  expect(accepted.accepted).toBe(true);
  const replayed = dispatcher.dispatch({
    session: replacement,
    recoveryEvent: { ...recoveryEvent, eventCursor: 9999 },
    ordinaryEvent: {
      recoveryEventId: recoveryEvent.eventId,
      wrapperId: 799,
    },
    terminalOutput: {
      streamId: "public-terminal",
      chunkId: "public-terminal-chunk-1-replay",
      text: "must not duplicate",
    },
  });
  expect(replayed).toEqual(
    expect.objectContaining({ accepted: false, suppressed: true }),
  );
  expect(ordinaryEvents.map((event) => event.wrapperId)).toEqual([701]);
  expect(terminalChunks).toEqual(["public-terminal-chunk-1"]);

  const resumed = dispatcher.openSession({
    ...recoveryScope,
    sessionId: "public-socket-next",
  });
  expect(resumed.sinceEventCursor).toBe(2);
  dispatcher.closeSession(replacement);
  const next = dispatcher.dispatch({
    session: resumed,
    recoveryEvent: {
      ...recoveryScope,
      ...authority(accepted.inspection),
      eventId: "public-next-live-event",
      eventCursor: 3,
      type: "resumed",
      now: 153,
    },
    ordinaryEvent: {
      recoveryEventId: "public-next-live-event",
      wrapperId: 702,
    },
  });
  expect(next.accepted).toBe(true);
  expect(ordinaryEvents.map((event) => event.wrapperId)).toEqual([701, 702]);
});

it("converges production store and status from a matching cross-tab notification", async () => {
  localStorage.clear();
  useRecoveryStore.getState().disconnect();
  const primaryScope = scope("public-production-tabs", fixture.backendId);
  const effect = fixture.effect as ExecuteEffectInput["effect"];
  let state = ConversationService.createCheckpoint({
    ...primaryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 200,
  });
  state = ConversationService.acquireRecoveryLease({
    ...primaryScope,
    ownerId: "runtime-production",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 200,
  });
  const issued = ConversationService.issueInspectionGrant({
    ...primaryScope,
    ...authority(state),
    audienceTabId: fixture.viewerTabId,
    level: "status",
    ttlMs: 100,
    maxEffects: 0,
    maxAuditEntries: 0,
    now: 201,
  });
  const request = {
    ...primaryScope,
    tabId: fixture.viewerTabId,
    grantToken: issued.token,
    now: 202,
  };
  await useRecoveryStore.getState().watch(request);
  const rendered = render(React.createElement(RecoveryStatus, { request }));
  expect(await screen.findByTestId("recovery-status")).toHaveTextContent(
    /ready|queued|recover/i,
  );
  const beforeRevision = useRecoveryStore.getState().summary!.revision;

  const current = ConversationService.inspectRecovery(primaryScope);
  const execute = vi.fn(async () => ({ ok: true }));
  const result = await AgentServerRuntimeService.executeRecoverableEffect(
    {
      ...primaryScope,
      ...authority(current),
      eventCursor: 2,
      effect,
      now: 203,
    },
    execute,
  );
  expect(execute).toHaveBeenCalledTimes(1);
  expect(result.executed).toBe(true);

  useEventStore
    .getState()
    .clearEventsForConversation(primaryScope.conversationId);
  const beforeRuntimeEvent = ConversationService.inspectRecovery(primaryScope);
  const runtimeInput = {
    ...primaryScope,
    ...authority(beforeRuntimeEvent),
    eventId: "public-runtime-event-1",
    eventCursor: 3,
    type: "resumed" as const,
    now: 204,
  };
  const uiEvent: any = {
    id: 701,
    timestamp: "2026-08-02T00:00:00Z",
    source: "agent",
    message: "public recovery event",
  };
  expect(fixture.replayUsesDistinctUiEventId).toBe(true);
  const acceptedEvent = useEventStore
    .getState()
    .ingestRecoveryEvent(runtimeInput, uiEvent);
  const replayedEvent = useEventStore
    .getState()
    .ingestRecoveryEvent(
      { ...runtimeInput, eventCursor: 999 },
      { ...uiEvent, id: 702, message: "must not replay downstream" },
    );
  expect(replayedEvent.checkpoint!.revision).toBe(
    acceptedEvent.checkpoint!.revision,
  );
  expect(
    useEventStore.getState().events.filter((item: any) => item.id === 701),
  ).toHaveLength(1);
  expect(
    useEventStore.getState().events.some((item: any) => item.id === 702),
  ).toBe(false);

  act(() =>
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: recoveryStorageKey(
          primaryScope.backendId,
          primaryScope.conversationId,
          acceptedEvent.checkpoint!.revision,
        ),
        storageArea: localStorage,
      }),
    ),
  );
  await waitFor(() => {
    expect(useRecoveryStore.getState().summary!.revision).toBeGreaterThan(
      beforeRevision,
    );
    expect(useRecoveryStore.getState().summary!.completedCount).toBe(1);
  });
  expect(screen.getByTestId("recovery-status").textContent).not.toContain(
    effect.idempotencyKey,
  );

  expect(fixture.replacementWatchABA).toBe(true);
  const latest = ConversationService.inspectRecovery(primaryScope);
  const revoked = ConversationService.revokeInspectionGrant({
    ...primaryScope,
    ...authority(latest),
    grantId: issued.grant.grantId,
    now: 205,
  });
  const replacement = ConversationService.issueInspectionGrant({
    ...primaryScope,
    ...authority(revoked),
    audienceTabId: fixture.viewerTabId,
    level: "status",
    ttlMs: 100,
    maxEffects: 0,
    maxAuditEntries: 0,
    now: 206,
  });
  const replacementRequest = {
    ...primaryScope,
    tabId: fixture.viewerTabId,
    grantToken: replacement.token,
    now: 207,
  };
  await useRecoveryStore.getState().watch(replacementRequest);
  rendered.rerender(
    React.createElement(RecoveryStatus, { request: replacementRequest }),
  );
  act(() =>
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: recoveryStorageKey(
          primaryScope.backendId,
          primaryScope.conversationId,
          revoked.checkpoint!.revision,
        ),
        storageArea: localStorage,
      }),
    ),
  );
  await waitFor(() => {
    expect(useRecoveryStore.getState().access).toBe("status");
    expect(useRecoveryStore.getState().grantToken).toBe(replacement.token);
    expect(useRecoveryStore.getState().summary!.revision).toBe(
      replacement.inspection.checkpoint!.revision,
    );
    expect(screen.getByTestId("recovery-status")).toBeInTheDocument();
  });

  const convergedRevision = useRecoveryStore.getState().summary!.revision;
  act(() =>
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: recoveryStorageKey(
          fixture.foreignBackendId,
          primaryScope.conversationId,
          999,
        ),
        storageArea: localStorage,
      }),
    ),
  );
  expect(useRecoveryStore.getState().summary!.revision).toBe(convergedRevision);
  useRecoveryStore.getState().disconnect();
});

it("lets a successor complete before suppressing a late production callback", async () => {
  expect(fixture.lateSettlementTakeover).toBe(true);
  localStorage.clear();
  const deliveryScope = scope(
    "public-production-late-settlement",
    fixture.backendId,
  );
  const unsafeEffect: ExecuteEffectInput["effect"] = {
    ...fixture.effect,
    deliveryId: "public-production-late-delivery",
    effectId: "public-production-late-effect",
    idempotencyKey: "public:production:late-write",
    kind: "local_write",
    retryClass: "reconcile",
  } as ExecuteEffectInput["effect"];
  let state = ConversationService.createCheckpoint({
    ...deliveryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 300,
  });
  state = ConversationService.acquireRecoveryLease({
    ...deliveryScope,
    ownerId: "public-runtime-old",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 5,
    now: 300,
  });

  let entered!: () => void;
  let release!: () => void;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const oldExecute = vi.fn(async () => {
    entered();
    await gate;
    return { path: "src/public-late.ts", writer: "old" };
  });
  const oldTask = AgentServerRuntimeService.executeRecoverableEffect(
    {
      ...deliveryScope,
      ...authority(state),
      eventCursor: 2,
      effect: unsafeEffect,
      claimTtlMs: 5,
      now: 301,
    },
    oldExecute,
    async () => undefined,
  );
  await started;

  const claimed = ConversationService.inspectRecovery(deliveryScope);
  const takeover = createRecoveryEvaluatorAdapter({
    storage: localStorage,
    now: () => 306,
    instanceId: "public-runtime-successor",
  }).recoverConversation({
    ...deliveryScope,
    ownerId: "public-runtime-new",
    expectedRevision: claimed.checkpoint!.revision,
    workspace: fixture.workspace,
    ttlMs: 50,
    now: 306,
  });
  const successorExecute = vi.fn(async () => {
    throw new Error("unsafe work must be reconciled, not replayed");
  });
  const successorReconcile = vi.fn(async () => ({
    path: "src/public-late.ts",
    writer: "successor",
  }));
  const successor = await AgentServerRuntimeService.executeRecoverableEffect(
    {
      ...deliveryScope,
      ...authority(takeover),
      eventCursor: 3,
      effect: unsafeEffect,
      claimTtlMs: 5,
      now: 307,
    },
    successorExecute,
    successorReconcile,
  );
  expect(successor.reconciled).toBe(true);
  expect(successorExecute).not.toHaveBeenCalled();
  expect(successorReconcile).toHaveBeenCalledTimes(1);

  release();
  const late = await oldTask;
  expect(late.suppressed).toBe(true);
  const final = ConversationService.inspectRecovery(deliveryScope);
  expect(final.checkpoint!.lease!.fencingToken).toBe(
    takeover.checkpoint!.lease!.fencingToken,
  );
  expect(
    final.checkpoint!.completedEffects.filter(
      (item) => item.deliveryId === unsafeEffect.deliveryId,
    ),
  ).toHaveLength(1);
  expect(
    final.checkpoint!.audit.some((item) => item.type === "stale_settlement"),
  ).toBe(true);
});

it("production sync takeover fences a frozen tab's delayed response", async () => {
  expect(fixture.syncFrozenTabTakeover).toBe(true);
  expect(fixture.syncSameTabReincarnation).toBe(true);
  localStorage.clear();
  const channel = new PublicSyncChannel();
  const recoveryScope = scope("public-production-sync", fixture.backendId);
  let state = ConversationService.createCheckpoint({
    ...recoveryScope,
    eventCursor: 10,
    workspace: fixture.workspace,
    now: 500,
  });
  state = ConversationService.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "public-production-sync-runtime",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 500,
  });
  const event = {
    ...recoveryScope,
    ...authority(state),
    eventId: "public-production-sync-event",
    eventCursor: 11,
    type: "resumed" as const,
    now: 507,
  };
  const response = {
    remoteEventCursor: 11,
    events: [
      {
        recoveryEvent: event,
        ordinaryEvent: { id: 950, recoveryEventId: event.eventId },
        terminalOutput: {
          streamId: "public-production-sync-terminal",
          chunkId: "public-production-sync-chunk",
          text: "one accepted response",
        },
      },
    ],
  };
  let releaseOld!: (value: typeof response) => void;
  const oldResponse = new Promise<typeof response>((resolve) => {
    releaseOld = resolve;
  });
  let now = 501;
  const oldOrdinary = vi.fn();
  const oldTerminal = vi.fn();
  const oldCoordinator = ConversationService.createRecoverySyncCoordinator({
    now: () => now,
    tabId: fixture.ownerTabId,
    incarnationId: "public-production-sync-incarnation-old",
    leaseTtlMs: 5,
    channel,
    transport: { pull: vi.fn(() => oldResponse) },
    sinks: {
      emitOrdinaryEvent: oldOrdinary,
      appendTerminalOutput: oldTerminal,
    },
  });
  expect(oldCoordinator.start(recoveryScope).role).toBe("leader");
  const pendingOld = oldCoordinator.sync();
  oldCoordinator.freeze();

  now = 507;
  const winnerOrdinary = vi.fn();
  const winnerTerminal = vi.fn();
  const winnerTransport = {
    pull: vi.fn(async (request: { sinceEventCursor: number | null }) => {
      expect(request.sinceEventCursor).toBe(10);
      return response;
    }),
  };
  const successor = ConversationService.createRecoverySyncCoordinator({
    now: () => now,
    tabId: fixture.ownerTabId,
    incarnationId: "public-production-sync-incarnation-new",
    leaseTtlMs: 5,
    channel,
    transport: winnerTransport,
    sinks: {
      emitOrdinaryEvent: winnerOrdinary,
      appendTerminalOutput: winnerTerminal,
    },
  });
  const takeover = successor.start(recoveryScope);
  expect(takeover.role).toBe("leader");
  expect(takeover.leaderIncarnationId).toBe(
    "public-production-sync-incarnation-new",
  );
  const won = await successor.sync();
  expect(won.acceptedCount).toBe(1);
  expect(winnerOrdinary).toHaveBeenCalledTimes(1);
  expect(winnerTerminal).toHaveBeenCalledTimes(1);

  releaseOld(response);
  const stale = await pendingOld;
  expect(stale.staleLeader).toBe(true);
  expect(oldOrdinary).not.toHaveBeenCalled();
  expect(oldTerminal).not.toHaveBeenCalled();
  expect(
    ConversationService.inspectRecovery(recoveryScope).checkpoint!.eventCursor,
  ).toBe(11);

  const beforeNotice = successor.inspect();
  channel.deliver({
    ...scope(recoveryScope.conversationId, fixture.foreignBackendId),
    kind: "leader",
    term: beforeNotice.term + 100,
    revision: beforeNotice.revision + 100,
    durableEventCursor: 9999,
  });
  expect(successor.inspect()).toEqual(beforeNotice);
  oldCoordinator.stop();
  successor.stop();
});

it("reconciles a lost production workspace commit response before cursor publish", async () => {
  expect(fixture.workspaceCommitReconciliation).toBe(true);
  localStorage.clear();
  const recoveryScope = scope("public-production-workspace", fixture.backendId);
  let state = ConversationService.createCheckpoint({
    ...recoveryScope,
    eventCursor: 30,
    workspace: fixture.workspace,
    now: 900,
  });
  state = ConversationService.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "public-production-workspace-owner",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 900,
  });
  const baseEntry = workspaceEntry(
    "README.md",
    workspaceDigests.base,
    workspaceBytes.base,
  );
  const base = workspaceManifest(recoveryScope, "base0003", null, [baseEntry]);
  const local = new PublicLocalReplica(
    workspaceManifest(recoveryScope, "local003", base.revision, [
      baseEntry,
      workspaceEntry(
        "src/recovered.ts",
        workspaceDigests.successor,
        workspaceBytes.successor,
      ),
    ]),
  );
  local.chunks.set(workspaceDigests.base, workspaceBytes.base);
  local.chunks.set(workspaceDigests.successor, workspaceBytes.successor);
  const transport = new PublicWorkspaceTransport(
    workspaceManifest(recoveryScope, "remote03", base.revision, [baseEntry]),
  );
  transport.chunks.set(workspaceDigests.base, workspaceBytes.base);
  transport.loseNextCommitResponse = true;
  const fence = mutableWorkspaceFence({
    term: 9,
    tabId: "public-production-workspace-tab",
    incarnationId: "public-production-workspace-incarnation",
    runGeneration: state.checkpoint!.runGeneration,
  });
  const input = {
    ...recoveryScope,
    transactionId: "public-production-workspace-transaction",
    baseManifest: base,
    ledgerAuthority: { ...authority(state), eventCursor: 31 },
    completionEventId: "public-production-workspace-complete",
    completionEventCursor: 32,
    now: 901,
  };
  const first = ConversationService.createWorkspaceRecoveryReconciler({
    now: () => 901,
    instanceId: "public-production-workspace-a",
    authority: fence,
    local,
    transport,
  });
  const lost = await first.reconcile(input);
  expect(lost.responseLost).toBe(true);
  expect(transport.committed.has(input.transactionId)).toBe(true);
  expect(local.applyCalls).toHaveLength(0);
  expect(
    ConversationService.inspectRecovery(recoveryScope).checkpoint!.eventCursor,
  ).toBe(30);

  const restarted = ConversationService.createWorkspaceRecoveryReconciler({
    now: () => 902,
    instanceId: "public-production-workspace-b",
    authority: fence,
    local,
    transport,
  });
  const completed = await restarted.reconcile({ ...input, now: 902 });
  expect(completed.completed).toBe(true);
  expect(transport.commitCalls).toHaveLength(1);
  expect(transport.reconcileCalls).toHaveLength(1);
  expect(local.applyCalls).toHaveLength(1);
  expect(local.manifest.rootDigest).toBe(transport.head.rootDigest);
  const durable = ConversationService.inspectRecovery(recoveryScope);
  expect(durable.checkpoint!.eventCursor).toBe(input.completionEventCursor);
  expect(durable.checkpoint!.recentEventIds).toContain(input.completionEventId);
  expect(durable.checkpoint!.completedEffects).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        deliveryId: input.transactionId,
        idempotencyKey: `workspace-recovery:${input.transactionId}`,
        status: "completed",
      }),
    ]),
  );
  const resumedDispatcher = ConversationService.createRecoveryEventDispatcher({
    emitOrdinaryEvent: vi.fn(),
  });
  expect(
    resumedDispatcher.openSession({
      ...recoveryScope,
      sessionId: "public-workspace-after-reconcile",
    }).sinceEventCursor,
  ).toBe(input.completionEventCursor);
});
