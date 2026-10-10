import { expect, it, vi } from "vitest";
import fixture from "./assets/scenario.json";
import type { ExecuteEffectInput } from "#/api/recovery/recovery-evaluator-adapter";
import { createConversationEventDispatcher } from "#/api/recovery/conversation-event-dispatcher";
import { createRecoverySyncCoordinator } from "#/api/recovery/recovery-sync-coordinator";
import { createWorkspaceRecoveryReconciler } from "#/api/recovery/workspace-recovery-reconciler";
import {
  adapter,
  authority,
  PublicStorage,
  PublicSyncChannel,
  scope,
} from "../helpers";
import {
  mutableWorkspaceFence,
  PublicLocalReplica,
  PublicWorkspaceTransport,
  workspaceBytes,
  workspaceDigests,
  workspaceEntry,
  workspaceManifest,
} from "../workspace-helpers";

it("recovers a crash-claimed unsafe delivery across independent instances", async () => {
  const storage = new PublicStorage();
  const deliveryScope = scope(fixture.conversationId, fixture.backendId);
  const effect = fixture.effect as ExecuteEffectInput["effect"];
  const crashing = adapter(storage, 100, "public-instance-a", {
    crashAfter: "after_claim",
    defaultClaimTtlMs: 5,
    maxRetainedRevisions: 8,
  });
  let state = crashing.createCheckpoint({
    ...deliveryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 100,
  });
  state = crashing.acquireRecoveryLease({
    ...deliveryScope,
    ownerId: "runtime-a",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 100,
  });

  const execute = vi.fn(async () => ({ path: "src/public.ts" }));
  await expect(
    crashing.executeEffect(
      {
        ...deliveryScope,
        ...authority(state),
        eventCursor: 2,
        effect,
        claimTtlMs: 5,
        now: 101,
      },
      execute,
    ),
  ).rejects.toThrow(/crash|after_claim/i);
  expect(execute).not.toHaveBeenCalled();

  const contender = adapter(storage.restart(), 102, "public-instance-b", {
    maxRetainedRevisions: 8,
  });
  const claimed = contender.inspectRecovery(deliveryScope);
  expect(claimed.checkpoint!.pendingEffects).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        deliveryId: effect.deliveryId,
        status: "claimed",
        claim: expect.objectContaining({
          ownerId: "runtime-a",
          fencingToken: state.checkpoint!.lease!.fencingToken,
        }),
      }),
    ]),
  );
  const suppressed = await contender.executeEffect(
    {
      ...deliveryScope,
      ...authority(claimed),
      eventCursor: 3,
      effect,
      now: 102,
    },
    execute,
  );
  expect(suppressed.suppressed).toBe(true);
  expect(execute).not.toHaveBeenCalled();

  const restarted = adapter(storage.restart(), 107, "public-instance-c", {
    maxRetainedRevisions: 8,
  });
  const expired = restarted.inspectRecovery(deliveryScope);
  const reconcile = vi.fn(async (key: string) => {
    expect(key).toBe(effect.idempotencyKey);
    return { path: "src/public.ts" };
  });
  const recovered = await restarted.executeEffect(
    {
      ...deliveryScope,
      ...authority(expired),
      eventCursor: 4,
      effect,
      now: 107,
    },
    execute,
    reconcile,
  );
  expect(recovered.reconciled).toBe(true);
  expect(reconcile).toHaveBeenCalledTimes(1);
  expect(execute).not.toHaveBeenCalled();
  expect(recovered.inspection.checkpoint!.completedEffects).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        deliveryId: effect.deliveryId,
        status: "completed",
      }),
    ]),
  );

  const duplicate = await adapter(storage.restart(), 108, "public-instance-d", {
    maxRetainedRevisions: 8,
  }).executeEffect(
    {
      ...deliveryScope,
      ...authority(recovered.inspection),
      eventCursor: 5,
      effect,
      now: 108,
    },
    execute,
    reconcile,
  );
  expect(duplicate.suppressed).toBe(true);
  expect(reconcile).toHaveBeenCalledTimes(1);

  let rolling = duplicate.inspection;
  const current = adapter(storage.restart(), 109, "public-instance-d", {
    maxRetainedRevisions: 8,
  });
  for (let index = 0; index < 10; index++) {
    rolling = current.ingestRuntimeEvent({
      ...deliveryScope,
      ...authority(rolling),
      eventId: `public-event-${index}`,
      eventCursor: 6 + index,
      type: "resumed",
      now: 109 + index,
    });
  }
  expect(rolling.checkpoint!.compaction.generation).toBeGreaterThan(0);
  expect(
    storage.keys().filter((key) => key.includes("openhands:recovery:v3")),
  ).toHaveLength(8);
});

it("persists unsafe callback rejection before restart reconciliation", async () => {
  expect(fixture.callbackRejectionRecovery).toBe(true);
  const storage = new PublicStorage();
  const deliveryScope = scope("public-rejected-callback", fixture.backendId);
  const effect: ExecuteEffectInput["effect"] = {
    ...(fixture.effect as ExecuteEffectInput["effect"]),
    deliveryId: "public-rejected-delivery",
    effectId: "public-rejected-effect",
    idempotencyKey: "public:rejected:unsafe",
  };
  const first = adapter(storage, 300, "public-rejection-a", {
    defaultClaimTtlMs: 5,
  });
  let state = first.createCheckpoint({
    ...deliveryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 300,
  });
  state = first.acquireRecoveryLease({
    ...deliveryScope,
    ownerId: "public-rejection-owner",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 300,
  });
  const execute = vi.fn(async () => {
    throw new Error("synthetic public callback failure");
  });
  await expect(
    first.executeEffect(
      {
        ...deliveryScope,
        ...authority(state),
        eventCursor: 2,
        effect,
        claimTtlMs: 5,
        now: 301,
      },
      execute,
    ),
  ).rejects.toThrow(/callback failure/i);

  const durable = adapter(
    storage.restart(),
    302,
    "public-rejection-observer",
  ).inspectRecovery(deliveryScope);
  expect(durable.checkpoint!.pendingEffects).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        deliveryId: effect.deliveryId,
        status: "uncertain",
        claim: null,
      }),
    ]),
  );
  const reconcile = vi.fn(async () => ({ durable: true }));
  const recovered = await adapter(
    storage.restart(),
    303,
    "public-rejection-restart",
  ).executeEffect(
    {
      ...deliveryScope,
      ...authority(durable),
      eventCursor: 3,
      effect,
      now: 303,
    },
    execute,
    reconcile,
  );
  expect(recovered.reconciled).toBe(true);
  expect(execute).toHaveBeenCalledTimes(1);
  expect(reconcile).toHaveBeenCalledTimes(1);
  expect(recovered.inspection.checkpoint!.completedEffects).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ deliveryId: effect.deliveryId }),
    ]),
  );
});

it("retries a lost sync response from the durable cursor after takeover", async () => {
  expect(fixture.syncResponseLossRetry).toBe(true);
  expect(fixture.syncNoticeSelfEcho).toBe(true);
  const storage = new PublicStorage();
  const channel = new PublicSyncChannel();
  const recoveryScope = scope("public-sync-response-loss", fixture.backendId);
  const ledger = adapter(storage, 500, "public-sync-ledger");
  let state = ledger.createCheckpoint({
    ...recoveryScope,
    eventCursor: 1,
    workspace: fixture.workspace,
    now: 500,
  });
  state = ledger.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "public-sync-runtime",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 500,
  });

  const ordinary = vi.fn();
  const terminal = vi.fn();
  const makeDispatcher = () =>
    createConversationEventDispatcher({
      inspectRecovery: (value) => ledger.inspectRecovery(value),
      ingestRuntimeEvent: (value) => ledger.ingestRuntimeEvent(value),
      emitOrdinaryEvent: ordinary,
      appendTerminalOutput: terminal,
    });
  let now = 501;
  const lostTransport = {
    pull: vi.fn(async () => {
      throw new Error("synthetic response loss");
    }),
  };
  const first = createRecoverySyncCoordinator({
    storage: storage.restart(),
    now: () => now,
    tabId: "public-sync-tab-a",
    incarnationId: "public-sync-incarnation-a",
    leaseTtlMs: 5,
    channel,
    transport: lostTransport,
    dispatcher: makeDispatcher(),
  });
  expect(first.start(recoveryScope).role).toBe("leader");
  const lost = await first.sync();
  expect(lost.responseLost).toBe(true);
  expect(lost.acceptedCount).toBe(0);
  expect(ledger.inspectRecovery(recoveryScope).checkpoint!.eventCursor).toBe(1);
  expect(
    storage
      .keys()
      .filter((key) => key.startsWith("openhands:recovery-sync:v1:"))
      .map((key) => storage.getItem(key))
      .join("\n"),
  ).not.toContain("synthetic response loss");

  now = 507;
  const runtimeEvent = {
    ...recoveryScope,
    ...authority(state),
    eventId: "public-sync-event-2",
    eventCursor: 2,
    type: "resumed" as const,
    now,
  };
  const recoveredTransport = {
    pull: vi.fn(async (request: { sinceEventCursor: number | null }) => {
      expect(request.sinceEventCursor).toBe(1);
      return {
        remoteEventCursor: 999,
        events: [
          {
            recoveryEvent: runtimeEvent,
            ordinaryEvent: { id: "public-sync-wrapper" },
            terminalOutput: {
              streamId: "public-sync-terminal",
              chunkId: "public-sync-chunk",
              text: "accepted once",
            },
          },
        ],
      };
    }),
  };
  const successor = createRecoverySyncCoordinator({
    storage: storage.restart(),
    now: () => now,
    tabId: "public-sync-tab-b",
    incarnationId: "public-sync-incarnation-b",
    leaseTtlMs: 5,
    channel,
    transport: recoveredTransport,
    dispatcher: makeDispatcher(),
  });
  const takeover = successor.start(recoveryScope);
  expect(takeover.role).toBe("leader");
  expect(takeover.term).toBeGreaterThan(first.inspect().term);
  const synced = await successor.sync();
  expect(synced).toEqual(
    expect.objectContaining({
      acceptedCount: 1,
      suppressedCount: 0,
      staleLeader: false,
    }),
  );
  expect(ledger.inspectRecovery(recoveryScope).checkpoint!.eventCursor).toBe(2);
  expect(ordinary).toHaveBeenCalledTimes(1);
  expect(terminal).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(channel.notices)).not.toContain("accepted once");
  expect(JSON.stringify(channel.notices)).not.toContain(runtimeEvent.eventId);
});

it("resumes content-addressed workspace reconciliation without overwriting", async () => {
  expect(fixture.workspaceChunkResume).toBe(true);
  const storage = new PublicStorage();
  const recoveryScope = scope("public-workspace-resume", fixture.backendId);
  const ledger = adapter(storage, 700, "public-workspace-ledger");
  let state = ledger.createCheckpoint({
    ...recoveryScope,
    eventCursor: 10,
    workspace: fixture.workspace,
    now: 700,
  });
  state = ledger.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "public-workspace-owner",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 700,
  });

  const baseEntry = workspaceEntry(
    "shared.txt",
    workspaceDigests.base,
    workspaceBytes.base,
  );
  const base = workspaceManifest(recoveryScope, "base0001", null, [baseEntry]);
  const local = new PublicLocalReplica(
    workspaceManifest(recoveryScope, "local001", base.revision, [
      baseEntry,
      workspaceEntry(
        "src/local.ts",
        workspaceDigests.local,
        workspaceBytes.local,
      ),
    ]),
  );
  local.chunks.set(workspaceDigests.base, workspaceBytes.base);
  local.chunks.set(workspaceDigests.local, workspaceBytes.local);
  const transport = new PublicWorkspaceTransport(
    workspaceManifest(recoveryScope, "remote01", base.revision, [
      baseEntry,
      workspaceEntry(
        "docs/remote.md",
        workspaceDigests.remote,
        workspaceBytes.remote,
      ),
    ]),
  );
  transport.chunks.set(workspaceDigests.base, workspaceBytes.base);
  transport.chunks.set(workspaceDigests.remote, workspaceBytes.remote);
  transport.loseNextWriteResponse = true;
  const fence = mutableWorkspaceFence({
    term: 4,
    tabId: "public-workspace-tab",
    incarnationId: "public-workspace-incarnation",
    runGeneration: state.checkpoint!.runGeneration,
  });
  const options = {
    storage: storage.restart(),
    now: () => 701,
    instanceId: "public-workspace-reconciler-a",
    authority: fence,
    local,
    transport,
    ledger: {
      inspectRecovery: ledger.inspectRecovery.bind(ledger),
      executeEffect: ledger.executeEffect.bind(ledger),
      ingestRuntimeEvent: ledger.ingestRuntimeEvent.bind(ledger),
    },
  };
  const input = {
    ...recoveryScope,
    transactionId: "public-workspace-transaction",
    baseManifest: base,
    ledgerAuthority: { ...authority(state), eventCursor: 11 },
    completionEventId: "public-workspace-complete",
    completionEventCursor: 12,
    now: 701,
  };
  const lost =
    await createWorkspaceRecoveryReconciler(options).reconcile(input);
  expect(lost.responseLost).toBe(true);
  expect(local.applyCalls).toHaveLength(0);
  expect(ledger.inspectRecovery(recoveryScope).checkpoint!.eventCursor).toBe(
    10,
  );
  expect(transport.chunks.has(workspaceDigests.local)).toBe(true);

  const resumed = await createWorkspaceRecoveryReconciler({
    ...options,
    storage: storage.restart(),
    instanceId: "public-workspace-reconciler-b",
  }).reconcile(input);
  expect(resumed.completed).toBe(true);
  expect(transport.writeCalls).toEqual([workspaceDigests.local]);
  expect(
    local.staged.has(`${input.transactionId}:${workspaceDigests.remote}`),
  ).toBe(true);
  expect(local.manifest.rootDigest).toBe(transport.head.rootDigest);
  expect(local.applyCalls).toHaveLength(1);
  expect(ledger.inspectRecovery(recoveryScope).checkpoint!.eventCursor).toBe(
    input.completionEventCursor,
  );
  const projection = JSON.stringify(
    createWorkspaceRecoveryReconciler({
      ...options,
      storage: storage.restart(),
      instanceId: "public-workspace-inspector",
    }).inspect(recoveryScope),
  );
  for (const sensitive of [
    "src/local.ts",
    "docs/remote.md",
    workspaceDigests.local,
    workspaceDigests.remote,
    fence.current()!.incarnationId,
  ]) {
    expect(projection).not.toContain(sensitive);
  }
});

it("blocks a dual-sided workspace path conflict before any side effect", async () => {
  expect(fixture.workspaceConflictBlocksAll).toBe(true);
  const storage = new PublicStorage();
  const recoveryScope = scope("public-workspace-conflict", fixture.backendId);
  const ledger = adapter(storage, 800, "public-conflict-ledger");
  let state = ledger.createCheckpoint({
    ...recoveryScope,
    eventCursor: 20,
    workspace: fixture.workspace,
    now: 800,
  });
  state = ledger.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "public-conflict-owner",
    expectedRevision: state.checkpoint!.revision,
    ttlMs: 100,
    now: 800,
  });
  const base = workspaceManifest(recoveryScope, "base0002", null, [
    workspaceEntry("src/value.ts", workspaceDigests.base, workspaceBytes.base),
  ]);
  const local = new PublicLocalReplica(
    workspaceManifest(recoveryScope, "local002", base.revision, [
      workspaceEntry(
        "src/value.ts",
        workspaceDigests.local,
        workspaceBytes.local,
      ),
    ]),
  );
  const transport = new PublicWorkspaceTransport(
    workspaceManifest(recoveryScope, "remote02", base.revision, [
      workspaceEntry(
        "src/value.ts",
        workspaceDigests.conflict,
        workspaceBytes.conflict,
      ),
    ]),
  );
  const executeEffect = vi.fn(ledger.executeEffect.bind(ledger));
  const result = await createWorkspaceRecoveryReconciler({
    storage: storage.restart(),
    now: () => 801,
    instanceId: "public-conflict-reconciler",
    authority: mutableWorkspaceFence({
      term: 5,
      tabId: "public-conflict-tab",
      incarnationId: "public-conflict-incarnation",
      runGeneration: state.checkpoint!.runGeneration,
    }),
    local,
    transport,
    ledger: {
      inspectRecovery: ledger.inspectRecovery.bind(ledger),
      executeEffect,
      ingestRuntimeEvent: ledger.ingestRuntimeEvent.bind(ledger),
    },
  }).reconcile({
    ...recoveryScope,
    transactionId: "public-conflict-transaction",
    baseManifest: base,
    ledgerAuthority: { ...authority(state), eventCursor: 21 },
    completionEventId: "public-conflict-complete",
    completionEventCursor: 22,
    now: 801,
  });
  expect(result.completed).toBe(false);
  expect(result.projection).toEqual(
    expect.objectContaining({ phase: "conflict", conflictCount: 1 }),
  );
  expect(executeEffect).not.toHaveBeenCalled();
  expect(transport.writeCalls).toHaveLength(0);
  expect(transport.commitCalls).toHaveLength(0);
  expect(local.applyCalls).toHaveLength(0);
  expect(ledger.inspectRecovery(recoveryScope).checkpoint!.eventCursor).toBe(
    20,
  );
});
