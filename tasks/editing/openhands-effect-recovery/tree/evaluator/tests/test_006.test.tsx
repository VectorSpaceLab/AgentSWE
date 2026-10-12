import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ConversationService from "#/api/conversation-service/conversation-service.api";
import AgentServerRuntimeService from "#/api/runtime-service/agent-server-runtime-service";
import {
  createRecoveryEvaluatorAdapter,
  recoveryStorageKey,
} from "#/api/recovery/recovery-evaluator-adapter";
import { RecoveryStatus } from "#/components/features/conversation/recovery-status";
import { useEventStore } from "#/stores/use-event-store";
import { useRecoveryStore } from "#/stores/recovery-store";
import {
  authority,
  effectSpec,
  expectDenied,
  scope,
  workspace,
} from "./helpers";
import { ManualSyncChannel } from "./sync-helpers";
import type { WorkspaceManifest } from "#/api/recovery/workspace-recovery-reconciler";
import {
  mutableWorkspaceFence,
  PublicLocalReplica,
  PublicWorkspaceTransport,
  workspaceBytes,
  workspaceDigests,
  workspaceEntry,
  workspaceManifest,
} from "./workspace-helpers";
import fixture from "./fixture.json";

describe("test_006 production services and cross-tab UI convergence", () => {
  it("[OH501:5] preserves neutral no-checkpoint and existing production APIs", () => {
    expect(fixture.productionSeams).toContain("event_store");
    localStorage.clear();
    const neutral = ConversationService.inspectRecovery(
      scope("normal-conversation", "production-a"),
    );
    expect(neutral).toEqual(
      expect.objectContaining({
        boundary: "none",
        checkpoint: null,
        summary: null,
      }),
    );
    expect(typeof ConversationService.getTrajectory).toBe("function");
    expect(typeof ConversationService.getVSCodeUrl).toBe("function");
    expect(typeof AgentServerRuntimeService.executeCommand).toBe("function");

    const store = useEventStore.getState();
    store.clearEventsForConversation("compat-events-v3");
    const later: any = {
      id: 2,
      timestamp: "2026-01-02T00:00:00Z",
      source: "agent",
      message: "later",
    };
    const earlier: any = {
      id: 1,
      timestamp: "2026-01-01T00:00:00Z",
      source: "agent",
      message: "earlier",
    };
    store.addEvent(later);
    store.addEvent(earlier);
    store.addEvent(earlier);
    expect(useEventStore.getState().events.map((item: any) => item.id)).toEqual(
      [1, 2],
    );
  });

  it("[OH502:5] conversation service shares scoped schema-v3 state with fresh instances", () => {
    localStorage.clear();
    const recoveryScope = scope("production-shared-v3", "production-a");
    const created = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 1,
      workspace: workspace(),
      now: 100,
    });
    const leased = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-owner",
      expectedRevision: created.checkpoint!.revision,
      ttlMs: 50,
      now: 100,
    });
    const fresh = createRecoveryEvaluatorAdapter({
      storage: localStorage,
      now: () => 100,
      instanceId: "production-fresh",
    });
    expect(fresh.inspectRecovery(recoveryScope).checkpoint).toEqual(
      leased.checkpoint,
    );
    expect(leased.checkpoint).toEqual(
      expect.objectContaining({
        schemaVersion: 3,
        backendId: "production-a",
        runGeneration: 1,
        lease: expect.objectContaining({ fencingToken: expect.any(String) }),
      }),
    );
    expect(
      fresh.inspectRecovery(scope("production-shared-v3", "production-b"))
        .checkpoint,
    ).toBeNull();
  });

  it("[OH503:10] runtime claim is visible but late production settlement is takeover-fenced", async () => {
    expect(fixture.successorCompletesBeforeLateSettlement).toBe(true);
    expect(fixture.lateRejectedCallbackAfterTakeover).toBe(true);
    localStorage.clear();
    const recoveryScope = scope("production-runtime-delivery", "production-a");
    let state = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 1,
      workspace: workspace(),
      now: 100,
    });
    state = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-runtime",
      expectedRevision: state.checkpoint!.revision,
      ttlMs: 5,
      now: 100,
    });
    let entered!: () => void;
    let release!: () => void;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const execute = vi.fn(async (): Promise<{ path: string }> => {
      const during = createRecoveryEvaluatorAdapter({
        storage: localStorage,
        now: () => 101,
        instanceId: "production-observer",
      }).inspectRecovery(recoveryScope);
      expect(during.checkpoint!.pendingEffects).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            deliveryId: "production-runtime-delivery-1",
            status: "claimed",
            claim: expect.objectContaining({
              fencingToken: state.checkpoint!.lease!.fencingToken,
              runGeneration: state.checkpoint!.runGeneration,
            }),
          }),
        ]),
      );
      entered();
      await gate;
      throw new Error("late production callback rejection");
    });
    const task = AgentServerRuntimeService.executeRecoverableEffect(
      {
        ...recoveryScope,
        ...authority(state),
        eventCursor: 2,
        effect: effectSpec({
          deliveryId: "production-runtime-delivery-1",
          idempotencyKey: "production:runtime:write",
          kind: "local_write",
          retryClass: "reconcile",
        }),
        claimTtlMs: 5,
        now: 101,
      },
      execute,
      async (): Promise<{ path: string } | undefined> => undefined,
    );
    await started;
    const claimed = ConversationService.inspectRecovery(recoveryScope);
    const takeover = createRecoveryEvaluatorAdapter({
      storage: localStorage,
      now: () => 106,
      instanceId: "production-takeover",
    }).recoverConversation({
      ...recoveryScope,
      ownerId: "production-runtime-new",
      expectedRevision: claimed.checkpoint!.revision,
      workspace: claimed.checkpoint!.workspace,
      ttlMs: 50,
      now: 106,
    });
    const successorExecute = vi.fn(async () => {
      throw new Error("successor must reconcile uncertain production work");
    });
    const successorReconcile = vi.fn(
      async (): Promise<{ path: string }> => ({
        path: "src/runtime.ts",
      }),
    );
    const successor = await AgentServerRuntimeService.executeRecoverableEffect(
      {
        ...recoveryScope,
        ...authority(takeover),
        eventCursor: 3,
        effect: effectSpec({
          deliveryId: "production-runtime-delivery-1",
          idempotencyKey: "production:runtime:write",
          kind: "local_write",
          retryClass: "reconcile",
        }),
        claimTtlMs: 5,
        now: 107,
      },
      successorExecute,
      successorReconcile,
    );
    expect(successor.reconciled).toBe(true);
    expect(successorExecute).not.toHaveBeenCalled();
    expect(successorReconcile).toHaveBeenCalledTimes(1);
    expect(
      successor.inspection.checkpoint!.completedEffects.filter(
        (item) => item.deliveryId === "production-runtime-delivery-1",
      ),
    ).toHaveLength(1);

    release();
    const late = await task;
    expect(late.suppressed).toBe(true);
    expect(execute).toHaveBeenCalledTimes(1);
    const final = ConversationService.inspectRecovery(recoveryScope);
    expect(final.checkpoint!.lease!.fencingToken).toBe(
      takeover.checkpoint!.lease!.fencingToken,
    );
    expect(
      final.checkpoint!.completedEffects.filter(
        (item) => item.deliveryId === "production-runtime-delivery-1",
      ),
    ).toHaveLength(1);
    expect(
      final.checkpoint!.audit.some((item) => item.type === "stale_settlement"),
    ).toBe(true);

    expect(fixture.productionSyncLateResponseFence).toBe(true);
    const syncChannel = new ManualSyncChannel();
    const syncOrdinary = vi.fn();
    const syncTerminal = vi.fn();
    const syncEvent = {
      ...recoveryScope,
      ...authority(final),
      eventId: "production-sync-after-runtime-takeover",
      eventCursor: final.checkpoint!.eventCursor + 1,
      type: "resumed" as const,
      now: 108,
    };
    const syncResponse = {
      remoteEventCursor: syncEvent.eventCursor,
      events: [
        {
          recoveryEvent: syncEvent,
          ordinaryEvent: { id: 750, recoveryEventId: syncEvent.eventId },
          terminalOutput: {
            streamId: "production-sync-runtime-terminal",
            chunkId: "production-sync-runtime-chunk",
            text: "successor transport response",
          },
        },
      ],
    };
    let releaseSync!: (value: typeof syncResponse) => void;
    const delayedSync = new Promise<typeof syncResponse>((resolve) => {
      releaseSync = resolve;
    });
    let syncNow = 108;
    const oldSync = ConversationService.createRecoverySyncCoordinator({
      now: () => syncNow,
      tabId: "production-runtime-sync-tab",
      incarnationId: "production-runtime-sync-old",
      leaseTtlMs: 5,
      channel: syncChannel,
      transport: { pull: vi.fn(() => delayedSync) },
      sinks: {
        emitOrdinaryEvent: syncOrdinary,
        appendTerminalOutput: syncTerminal,
      },
    });
    oldSync.start(recoveryScope);
    const oldSyncTask = oldSync.sync();
    oldSync.freeze();
    syncNow = 114;
    const newSync = ConversationService.createRecoverySyncCoordinator({
      now: () => syncNow,
      tabId: "production-runtime-sync-tab",
      incarnationId: "production-runtime-sync-new",
      leaseTtlMs: 5,
      channel: syncChannel,
      transport: { pull: vi.fn(async () => syncResponse) },
      sinks: {
        emitOrdinaryEvent: syncOrdinary,
        appendTerminalOutput: syncTerminal,
      },
    });
    expect(newSync.start(recoveryScope).role).toBe("leader");
    expect((await newSync.sync()).acceptedCount).toBe(1);
    releaseSync(syncResponse);
    expect((await oldSyncTask).staleLeader).toBe(true);
    expect(syncOrdinary).toHaveBeenCalledTimes(1);
    expect(syncTerminal).toHaveBeenCalledTimes(1);
  });

  it("[OH504:10] matching takeover notification invalidates watched grant and UI", async () => {
    expect(fixture.replacementWatchABA).toBe(true);
    localStorage.clear();
    useRecoveryStore.getState().disconnect();
    const recoveryScope = scope("production-tab-watch", "production-a");
    let state = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 1,
      workspace: workspace(),
      now: 200,
    });
    state = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-tab-owner",
      expectedRevision: state.checkpoint!.revision,
      ttlMs: 5,
      now: 200,
    });
    const issued = ConversationService.issueInspectionGrant({
      ...recoveryScope,
      ...authority(state),
      audienceTabId: "production-viewer-tab",
      level: "status",
      ttlMs: 100,
      maxEffects: 0,
      maxAuditEntries: 0,
      now: 201,
    });
    const request = {
      ...recoveryScope,
      tabId: "production-viewer-tab",
      grantToken: issued.token,
      now: 202,
    };
    await useRecoveryStore.getState().watch(request);
    const rendered = render(<RecoveryStatus request={request} />);
    expect(await screen.findByTestId("recovery-status")).toBeInTheDocument();
    const before = useRecoveryStore.getState().summary!.revision;

    const current = ConversationService.inspectRecovery(recoveryScope);
    const takeover = createRecoveryEvaluatorAdapter({
      storage: localStorage,
      now: () => 206,
      instanceId: "production-tab-takeover",
    }).recoverConversation({
      ...recoveryScope,
      ownerId: "production-tab-new-owner",
      expectedRevision: current.checkpoint!.revision,
      workspace: current.checkpoint!.workspace,
      ttlMs: 50,
      now: 206,
    });
    act(() =>
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: recoveryStorageKey(
            "production-b",
            recoveryScope.conversationId,
            999,
          ),
          storageArea: localStorage,
        }),
      ),
    );
    expect(useRecoveryStore.getState().summary!.revision).toBe(before);

    act(() =>
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: recoveryStorageKey(
            recoveryScope.backendId,
            recoveryScope.conversationId,
            takeover.checkpoint!.revision,
          ),
          storageArea: localStorage,
        }),
      ),
    );
    await waitFor(() => {
      expect(useRecoveryStore.getState().access).toBe("denied");
      expect(useRecoveryStore.getState().summary).toBeNull();
      expect(screen.queryByTestId("recovery-status")).toBeNull();
    });

    const replacementScope = scope(
      "production-tab-watch-replacement",
      "production-a",
    );
    let replacementState = ConversationService.createCheckpoint({
      ...replacementScope,
      eventCursor: 1,
      workspace: workspace("replacement"),
      now: 207,
    });
    replacementState = ConversationService.acquireRecoveryLease({
      ...replacementScope,
      ownerId: "production-tab-replacement-owner",
      expectedRevision: replacementState.checkpoint!.revision,
      ttlMs: 100,
      now: 207,
    });
    const replacement = ConversationService.issueInspectionGrant({
      ...replacementScope,
      ...authority(replacementState),
      audienceTabId: "production-viewer-tab",
      level: "status",
      ttlMs: 100,
      maxEffects: 0,
      maxAuditEntries: 0,
      now: 207,
    });
    const replacementRequest = {
      ...replacementScope,
      tabId: "production-viewer-tab",
      grantToken: replacement.token,
      now: 208,
    };
    await useRecoveryStore.getState().watch(replacementRequest);
    rendered.rerender(<RecoveryStatus request={replacementRequest} />);
    expect(useRecoveryStore.getState().access).toBe("status");
    expect(useRecoveryStore.getState().grantToken).toBe(replacement.token);
    expect(await screen.findByTestId("recovery-status")).toBeInTheDocument();

    act(() =>
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: recoveryStorageKey(
            recoveryScope.backendId,
            recoveryScope.conversationId,
            issued.inspection.checkpoint!.revision,
          ),
          storageArea: localStorage,
        }),
      ),
    );
    await waitFor(() => {
      expect(useRecoveryStore.getState().access).toBe("status");
      expect(useRecoveryStore.getState().grantToken).toBe(replacement.token);
      expect(useRecoveryStore.getState().scope).toEqual(replacementScope);
      expect(useRecoveryStore.getState().summary!.revision).toBe(
        replacement.inspection.checkpoint!.revision,
      );
      expect(screen.getByTestId("recovery-status")).toBeInTheDocument();
    });
    expect(fixture.productionSyncNoticeIsolation).toBe(true);
    const syncChannel = new ManualSyncChannel();
    const syncCoordinator = ConversationService.createRecoverySyncCoordinator({
      now: () => 208,
      tabId: "production-viewer-tab",
      incarnationId: "production-viewer-sync-incarnation",
      leaseTtlMs: 20,
      channel: syncChannel,
      transport: {
        pull: vi.fn(async () => ({ events: [], remoteEventCursor: null })),
      },
      sinks: { emitOrdinaryEvent: vi.fn() },
    });
    const syncProjection = syncCoordinator.start(replacementScope);
    syncChannel.deliver({
      ...scope(replacementScope.conversationId, "production-b"),
      kind: "progress",
      term: syncProjection.term + 50,
      revision: syncProjection.revision + 50,
      durableEventCursor: 9999,
    });
    expect(syncCoordinator.inspect()).toEqual(syncProjection);
    expect(useRecoveryStore.getState().scope).toEqual(replacementScope);
    expect(screen.getByTestId("recovery-status")).toBeInTheDocument();
    syncCoordinator.stop();
    useRecoveryStore.getState().disconnect();
  });

  it("[OH505:5] production grants deny wrong backend/tab without state disclosure", () => {
    localStorage.clear();
    const recoveryScope = scope("production-grant-isolation", "production-a");
    let state = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 1,
      workspace: workspace(),
      now: 300,
    });
    state = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-grant-owner",
      expectedRevision: state.checkpoint!.revision,
      ttlMs: 100,
      now: 300,
    });
    const issued = ConversationService.issueInspectionGrant({
      ...recoveryScope,
      ...authority(state),
      audienceTabId: "production-grant-tab",
      level: "support",
      ttlMs: 50,
      maxEffects: 1,
      maxAuditEntries: 1,
      now: 301,
    });
    expect(
      ConversationService.inspectRecoveryAuthorized({
        ...recoveryScope,
        tabId: "production-grant-tab",
        grantToken: issued.token,
        now: 302,
      }).access,
    ).toBe("support");
    expectDenied(
      ConversationService.inspectRecoveryAuthorized({
        ...scope("production-grant-isolation", "production-b"),
        tabId: "production-grant-tab",
        grantToken: issued.token,
        now: 302,
      }),
    );
    expectDenied(
      ConversationService.inspectRecoveryAuthorized({
        ...recoveryScope,
        tabId: "production-wrong-tab",
        grantToken: issued.token,
        now: 302,
      }),
    );
  });

  it("[OH506:5] production dispatcher hands history to socket side-effect-once", () => {
    expect(fixture.replayUsesDistinctUiEventId).toBe(true);
    expect(fixture.dispatcherProductionHandoff).toBe(true);
    expect(fixture.terminalEchoSuppression).toBe(true);
    localStorage.clear();
    const recoveryScope = scope("production-event-dispatch", "production-a");
    let state = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 1,
      workspace: workspace(),
      now: 400,
    });
    state = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-event-owner",
      expectedRevision: state.checkpoint!.revision,
      ttlMs: 100,
      now: 400,
    });
    useEventStore
      .getState()
      .clearEventsForConversation(recoveryScope.conversationId);
    const terminalChunks: string[] = [];
    const dispatcher = ConversationService.createRecoveryEventDispatcher({
      emitOrdinaryEvent(event: unknown) {
        const ordinary = event as any;
        expect(
          ConversationService.inspectRecovery(recoveryScope).checkpoint!
            .recentEventIds,
        ).toContain(ordinary.recoveryEventId);
        useEventStore.getState().addEvent(ordinary);
      },
      appendTerminalOutput(chunk) {
        terminalChunks.push(chunk.chunkId);
      },
    });
    const history = dispatcher.openSession({
      ...recoveryScope,
      sessionId: "production-history",
    });
    expect(history.sinceEventCursor).toBe(1);
    const runtimeInput = {
      ...recoveryScope,
      ...authority(state),
      eventId: "production-recovery-event-1",
      eventCursor: 2,
      type: "resumed" as const,
      now: 401,
    };
    const uiEvent: any = {
      id: 801,
      recoveryEventId: runtimeInput.eventId,
      timestamp: "2026-08-02T00:00:00Z",
      source: "agent",
      message: "accepted recovery event",
    };
    const accepted = dispatcher.dispatch({
      session: history,
      recoveryEvent: runtimeInput,
      ordinaryEvent: uiEvent,
      terminalOutput: {
        streamId: "production-terminal",
        chunkId: "production-terminal-chunk-1",
        text: "one command output",
      },
    });
    const duplicate = dispatcher.dispatch({
      session: history,
      recoveryEvent: { ...runtimeInput, eventCursor: 9999 },
      ordinaryEvent: {
        ...uiEvent,
        id: 899,
        message: "must not replay downstream",
      },
      terminalOutput: {
        streamId: "production-terminal",
        chunkId: "production-terminal-chunk-replay",
        text: "must not duplicate",
      },
    });
    expect(accepted.accepted).toBe(true);
    expect(duplicate).toEqual(
      expect.objectContaining({ accepted: false, suppressed: true }),
    );
    expect(terminalChunks).toEqual(["production-terminal-chunk-1"]);

    const socket = dispatcher.openSession({
      ...recoveryScope,
      sessionId: "production-socket",
    });
    expect(socket.sinceEventCursor).toBe(2);
    const delayedHistory = dispatcher.dispatch({
      session: history,
      recoveryEvent: {
        ...recoveryScope,
        ...authority(accepted.inspection),
        eventId: "production-delayed-history-event",
        eventCursor: 9998,
        type: "resumed",
        now: 402,
      },
      ordinaryEvent: {
        ...uiEvent,
        id: 898,
        recoveryEventId: "production-delayed-history-event",
      },
    });
    expect(delayedHistory.staleSession).toBe(true);
    dispatcher.closeSession(history);
    const next = dispatcher.dispatch({
      session: socket,
      recoveryEvent: {
        ...recoveryScope,
        ...authority(accepted.inspection),
        eventId: "production-recovery-event-next",
        eventCursor: 3,
        type: "resumed",
        now: 403,
      },
      ordinaryEvent: {
        ...uiEvent,
        id: 803,
        recoveryEventId: "production-recovery-event-next",
        message: "accepted live event",
      },
    });
    expect(next.accepted).toBe(true);
    const reordered = dispatcher.dispatch({
      session: socket,
      recoveryEvent: {
        ...recoveryScope,
        ...authority(next.inspection),
        eventId: "production-recovery-event-reordered",
        eventCursor: 1,
        type: "resumed",
        now: 404,
      },
      ordinaryEvent: {
        ...uiEvent,
        id: 802,
        recoveryEventId: "production-recovery-event-reordered",
        message: "must not be applied",
      },
    });
    expect(reordered.accepted).toBe(false);
    expect(reordered.inspection.checkpoint!.eventCursor).toBe(3);
    expect(useEventStore.getState().events.map((item: any) => item.id)).toEqual(
      [801, 803],
    );
    expect(
      ConversationService.inspectRecovery(recoveryScope).checkpoint!
        .recentEventIds,
    ).toContain("production-recovery-event-1");

    expect(fixture.productionSyncResponseLossRetry).toBe(true);
    const syncChannel = new ManualSyncChannel();
    let pullAttempt = 0;
    const syncCoordinator = ConversationService.createRecoverySyncCoordinator({
      now: () => 405,
      tabId: "production-event-sync-tab",
      incarnationId: "production-event-sync-incarnation",
      leaseTtlMs: 20,
      channel: syncChannel,
      transport: {
        pull: vi.fn(async (request) => {
          expect(request.sinceEventCursor).toBe(3);
          pullAttempt += 1;
          if (pullAttempt === 1) throw new Error("lost response fixture");
          const latest = ConversationService.inspectRecovery(recoveryScope);
          return {
            remoteEventCursor: 9999,
            events: [
              {
                recoveryEvent: {
                  ...recoveryScope,
                  ...authority(latest),
                  eventId: "production-sync-retry-event",
                  eventCursor: 4,
                  type: "resumed" as const,
                  now: 405,
                },
                ordinaryEvent: {
                  ...uiEvent,
                  id: 804,
                  recoveryEventId: "production-sync-retry-event",
                },
                terminalOutput: {
                  streamId: "production-terminal",
                  chunkId: "production-sync-retry-chunk",
                  text: "sync retry accepted",
                },
              },
            ],
          };
        }),
      },
      sinks: {
        emitOrdinaryEvent(event: unknown) {
          useEventStore.getState().addEvent(event as any);
        },
        appendTerminalOutput(chunk) {
          terminalChunks.push(chunk.chunkId);
        },
      },
    });
    syncCoordinator.start(recoveryScope);
    return syncCoordinator.sync().then(async (lost) => {
      expect(lost.responseLost).toBe(true);
      expect(
        ConversationService.inspectRecovery(recoveryScope).checkpoint!
          .eventCursor,
      ).toBe(3);
      const retried = await syncCoordinator.sync();
      expect(retried.acceptedCount).toBe(1);
      expect(
        ConversationService.inspectRecovery(recoveryScope).checkpoint!
          .eventCursor,
      ).toBe(4);
      expect(
        useEventStore.getState().events.map((item: any) => item.id),
      ).toEqual([801, 803, 804]);
      expect(terminalChunks).toContain("production-sync-retry-chunk");
      expect(JSON.stringify(syncChannel.posted)).not.toContain(
        "sync retry accepted",
      );
    });
  });
});

describe("test_006 production workspace recovery integration", () => {
  it("[OH507:40] reconciles a lost remote commit before local apply and sync cursor advance", async () => {
    expect(fixture.productionWorkspaceCommitRecovery).toBe(true);
    localStorage.clear();
    const recoveryScope = scope(
      "production-workspace-commit-loss",
      "production-a",
    );
    let state = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 40,
      workspace: workspace("production-workspace"),
      now: 1000,
    });
    state = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-workspace-owner",
      expectedRevision: state.checkpoint!.revision,
      ttlMs: 100,
      now: 1000,
    });
    const baseEntry = workspaceEntry(
      "base.txt",
      workspaceDigests.base,
      workspaceBytes.base,
    );
    const base = workspaceManifest(recoveryScope, "base6001", null, [
      baseEntry,
    ]);
    const local = new PublicLocalReplica(
      workspaceManifest(recoveryScope, "local601", base.revision, [
        baseEntry,
        workspaceEntry(
          "src/new.ts",
          workspaceDigests.local,
          workspaceBytes.local,
        ),
      ]),
    );
    local.chunks.set(workspaceDigests.base, workspaceBytes.base);
    local.chunks.set(workspaceDigests.local, workspaceBytes.local);
    const transport = new PublicWorkspaceTransport(
      workspaceManifest(recoveryScope, "remote61", base.revision, [baseEntry]),
    );
    transport.chunks.set(workspaceDigests.base, workspaceBytes.base);
    transport.loseNextCommitResponse = true;
    const fence = mutableWorkspaceFence({
      term: 21,
      tabId: "production-workspace-tab",
      incarnationId: "production-workspace-incarnation",
      runGeneration: state.checkpoint!.runGeneration,
    });
    const input = {
      ...recoveryScope,
      transactionId: "production-workspace-transaction",
      baseManifest: base,
      ledgerAuthority: { ...authority(state), eventCursor: 41 },
      completionEventId: "production-workspace-complete",
      completionEventCursor: 42,
      now: 1001,
    };
    const lost = await ConversationService.createWorkspaceRecoveryReconciler({
      now: () => 1001,
      instanceId: "production-workspace-reconciler-a",
      authority: fence,
      local,
      transport,
    }).reconcile(input);
    expect(lost.responseLost).toBe(true);
    expect(transport.committed.has(input.transactionId)).toBe(true);
    expect(local.applyCalls).toHaveLength(0);
    const before = ConversationService.createRecoveryEventDispatcher({
      emitOrdinaryEvent: vi.fn(),
    }).openSession({ ...recoveryScope, sessionId: "workspace-before-resume" });
    expect(before.sinceEventCursor).toBe(40);

    const completed =
      await ConversationService.createWorkspaceRecoveryReconciler({
        now: () => 1002,
        instanceId: "production-workspace-reconciler-b",
        authority: fence,
        local,
        transport,
      }).reconcile({ ...input, now: 1002 });
    expect(completed.completed).toBe(true);
    expect(transport.commitCalls).toHaveLength(1);
    expect(transport.reconcileCalls).toHaveLength(1);
    expect(local.applyCalls).toHaveLength(1);
    const durable = ConversationService.inspectRecovery(recoveryScope);
    expect(durable.checkpoint!.eventCursor).toBe(42);
    expect(durable.checkpoint!.recentEventIds).toContain(
      input.completionEventId,
    );
    const after = ConversationService.createRecoveryEventDispatcher({
      emitOrdinaryEvent: vi.fn(),
    }).openSession({ ...recoveryScope, sessionId: "workspace-after-resume" });
    expect(after.sinceEventCursor).toBe(42);

    const channel = new ManualSyncChannel();
    const pull = vi.fn(async (request) => {
      expect(request.sinceEventCursor).toBe(42);
      return { events: [], remoteEventCursor: 4200 };
    });
    const sync = ConversationService.createRecoverySyncCoordinator({
      now: () => 1003,
      tabId: "production-workspace-sync-tab",
      incarnationId: "production-workspace-sync-incarnation",
      leaseTtlMs: 20,
      channel,
      transport: { pull },
      sinks: { emitOrdinaryEvent: vi.fn() },
    });
    expect(sync.start(recoveryScope).role).toBe("leader");
    expect((await sync.sync()).acceptedCount).toBe(0);
    expect(pull).toHaveBeenCalledTimes(1);
    expect(
      ConversationService.inspectRecovery(recoveryScope).checkpoint!
        .eventCursor,
    ).toBe(42);
  });

  it("[OH508:20] fences a delayed production workspace response and redacts its projection", async () => {
    expect(fixture.productionWorkspaceTakeoverFence).toBe(true);
    localStorage.clear();
    const recoveryScope = scope(
      "production-workspace-takeover",
      "production-a",
    );
    let state = ConversationService.createCheckpoint({
      ...recoveryScope,
      eventCursor: 50,
      workspace: workspace("production-takeover"),
      now: 1100,
    });
    state = ConversationService.acquireRecoveryLease({
      ...recoveryScope,
      ownerId: "production-workspace-old-owner",
      expectedRevision: state.checkpoint!.revision,
      ttlMs: 100,
      now: 1100,
    });
    const baseEntry = workspaceEntry(
      "secret-name.txt",
      workspaceDigests.base,
      workspaceBytes.base,
    );
    const base = workspaceManifest(recoveryScope, "base6002", null, [
      baseEntry,
    ]);
    const local = new PublicLocalReplica(
      workspaceManifest(recoveryScope, "local602", base.revision, [
        baseEntry,
        workspaceEntry(
          "private/change.ts",
          workspaceDigests.successor,
          workspaceBytes.successor,
        ),
      ]),
    );
    local.chunks.set(workspaceDigests.base, workspaceBytes.base);
    local.chunks.set(workspaceDigests.successor, workspaceBytes.successor);
    const transport = new PublicWorkspaceTransport(
      workspaceManifest(recoveryScope, "remote62", base.revision, [baseEntry]),
    );
    transport.chunks.set(workspaceDigests.base, workspaceBytes.base);
    const oldHead = structuredClone(transport.head);
    let release!: (value: WorkspaceManifest) => void;
    let entered!: () => void;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const delayed = new Promise<WorkspaceManifest>((resolve) => {
      release = resolve;
    });
    transport.readHead = vi.fn(async () => {
      entered();
      return delayed;
    });
    const fence = mutableWorkspaceFence({
      term: 31,
      tabId: "production-old-tab",
      incarnationId: "production-old-incarnation",
      runGeneration: state.checkpoint!.runGeneration,
    });
    const input = {
      ...recoveryScope,
      transactionId: "production-workspace-takeover-transaction",
      baseManifest: base,
      ledgerAuthority: { ...authority(state), eventCursor: 51 },
      completionEventId: "production-workspace-takeover-complete",
      completionEventCursor: 52,
      now: 1101,
    };
    const old = ConversationService.createWorkspaceRecoveryReconciler({
      now: () => 1101,
      instanceId: "production-workspace-old-instance",
      authority: fence,
      local,
      transport,
    });
    const pendingOld = old.reconcile(input);
    await started;
    const paused = ConversationService.pause({
      ...recoveryScope,
      ...authority(ConversationService.inspectRecovery(recoveryScope)),
      now: 1102,
    });
    const resumed = ConversationService.resume({
      ...recoveryScope,
      ...authority(paused),
      now: 1103,
    });
    fence.replace({
      term: 32,
      tabId: "production-new-tab",
      incarnationId: "production-new-incarnation",
      runGeneration: resumed.checkpoint!.runGeneration,
    });
    transport.readHead = vi.fn(async () => structuredClone(oldHead));
    const successor = ConversationService.createWorkspaceRecoveryReconciler({
      now: () => 1104,
      instanceId: "production-workspace-new-instance",
      authority: fence,
      local,
      transport,
    });
    expect(
      (
        await successor.reconcile({
          ...input,
          ledgerAuthority: {
            ...authority(resumed),
            eventCursor: resumed.checkpoint!.eventCursor + 1,
          },
          completionEventCursor: resumed.checkpoint!.eventCursor + 2,
          now: 1104,
        })
      ).completed,
    ).toBe(true);
    release(oldHead);
    expect(await pendingOld).toEqual(
      expect.objectContaining({ staleAuthority: true, suppressed: true }),
    );
    expect(transport.commitCalls).toHaveLength(1);
    expect(local.applyCalls).toHaveLength(1);
    const projection = JSON.stringify(successor.inspect(recoveryScope));
    for (const secret of [
      "secret-name.txt",
      "private/change.ts",
      workspaceDigests.successor,
      "production-new-tab",
      "production-new-incarnation",
      input.ledgerAuthority.fencingToken,
    ]) {
      expect(projection).not.toContain(secret);
    }
  });
});
