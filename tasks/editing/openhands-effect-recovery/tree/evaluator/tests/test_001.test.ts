import { describe, expect, it, vi } from "vitest";
import fixture from "./fixture.json";
import { createWorkspaceRecoveryReconciler } from "#/api/recovery/workspace-recovery-reconciler";
import type { WorkspaceManifest } from "#/api/recovery/workspace-recovery-reconciler";
import { authority, createLeased, DurableStorage, scope } from "./helpers";
import {
  mutableWorkspaceFence,
  PublicLocalReplica,
  PublicWorkspaceTransport,
  workspaceBytes,
  workspaceDigests,
  workspaceEntry,
  workspaceManifest,
} from "./workspace-helpers";

const makeWorkspace = (
  recoveryScope: ReturnType<typeof scope>,
  storage = new DurableStorage(),
) => {
  const created = createLeased(recoveryScope, storage, 100);
  const baseEntry = workspaceEntry(
    "shared.txt",
    workspaceDigests.base,
    workspaceBytes.base,
  );
  const base = workspaceManifest(recoveryScope, "base1001", null, [baseEntry]);
  const local = new PublicLocalReplica(
    workspaceManifest(recoveryScope, "local101", base.revision, [
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
    workspaceManifest(recoveryScope, "remote11", base.revision, [
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
  const fence = mutableWorkspaceFence({
    term: 3,
    tabId: "hidden-workspace-tab-a",
    incarnationId: "hidden-workspace-incarnation-a",
    runGeneration: created.inspection.checkpoint!.runGeneration,
  });
  const ledger = {
    inspectRecovery: created.adapter.inspectRecovery.bind(created.adapter),
    executeEffect: created.adapter.executeEffect.bind(created.adapter),
    ingestRuntimeEvent: created.adapter.ingestRuntimeEvent.bind(
      created.adapter,
    ),
  };
  const input = {
    ...recoveryScope,
    transactionId: `transaction-${recoveryScope.conversationId}`,
    baseManifest: base,
    ledgerAuthority: { ...authority(created.inspection), eventCursor: 2 },
    completionEventId: `workspace-complete-${recoveryScope.conversationId}`,
    completionEventCursor: 3,
    now: 101,
  };
  return { ...created, base, local, transport, fence, ledger, input };
};

describe("test_001 workspace reconciliation and cross-instance fencing", () => {
  it("[OH001:10] preserves backend scope and blocks a dual-sided manifest conflict", async () => {
    expect(fixture.workspaceConflictSafePlan).toBe(true);
    const recoveryScope = scope("hidden-workspace-conflict");
    const made = makeWorkspace(recoveryScope);
    made.local.manifest = workspaceManifest(
      recoveryScope,
      "local102",
      made.base.revision,
      [
        workspaceEntry(
          "shared.txt",
          workspaceDigests.local,
          workspaceBytes.local,
        ),
      ],
    );
    made.transport.head = workspaceManifest(
      recoveryScope,
      "remote12",
      made.base.revision,
      [
        workspaceEntry(
          "shared.txt",
          workspaceDigests.conflict,
          workspaceBytes.conflict,
        ),
      ],
    );
    const executeEffect = vi.fn(made.ledger.executeEffect);
    const result = await createWorkspaceRecoveryReconciler({
      storage: made.storage.restart(),
      now: () => 101,
      instanceId: "hidden-workspace-conflict-instance",
      authority: made.fence,
      local: made.local,
      transport: made.transport,
      ledger: { ...made.ledger, executeEffect },
    }).reconcile(made.input);
    expect(result.projection).toEqual(
      expect.objectContaining({ phase: "conflict", conflictCount: 1 }),
    );
    expect(result.plan!.operations).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ path: "shared.txt", action: "conflict" }),
      ]),
    );
    expect(executeEffect).not.toHaveBeenCalled();
    expect(made.transport.writeCalls).toHaveLength(0);
    expect(made.transport.commitCalls).toHaveLength(0);
    expect(made.local.applyCalls).toHaveLength(0);
    expect(
      made.adapter.inspectRecovery(recoveryScope).checkpoint!.eventCursor,
    ).toBe(1);
    expect(
      made.adapter.inspectRecovery(
        scope(recoveryScope.conversationId, "fixture-backend-b"),
      ).checkpoint,
    ).toBeNull();
  });

  it("[OH002:40] merges single-sided paths through chunks, ledger commit, local CAS, then cursor", async () => {
    expect(fixture.workspaceOrderedCommit).toBe(true);
    const recoveryScope = scope("hidden-workspace-ordered-commit");
    const made = makeWorkspace(recoveryScope);
    const order: string[] = [];
    const originalWrite = made.transport.writeChunk.bind(made.transport);
    made.transport.writeChunk = vi.fn(async (input) => {
      order.push(`upload:${input.digest}`);
      await originalWrite(input);
    });
    const originalStage = made.local.stageChunk.bind(made.local);
    made.local.stageChunk = vi.fn(async (input) => {
      order.push(`download:${input.digest}`);
      await originalStage(input);
    });
    const originalCommit = made.transport.commit.bind(made.transport);
    made.transport.commit = vi.fn(async (input) => {
      order.push("remote-commit");
      return originalCommit(input);
    });
    const originalApply = made.local.apply.bind(made.local);
    made.local.apply = vi.fn(async (input) => {
      order.push("local-apply");
      return originalApply(input);
    });
    const originalIngest = made.ledger.ingestRuntimeEvent;
    made.ledger.ingestRuntimeEvent = vi.fn((input) => {
      order.push("cursor-commit");
      expect(input.type).toBe("workspace_reconciled");
      return originalIngest(input);
    });
    const result = await createWorkspaceRecoveryReconciler({
      storage: made.storage.restart(),
      now: () => 101,
      instanceId: "hidden-workspace-ordered-instance",
      authority: made.fence,
      local: made.local,
      transport: made.transport,
      ledger: made.ledger,
    }).reconcile(made.input);
    expect(result.completed).toBe(true);
    expect(result.plan!.operations).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ path: "docs/remote.md", action: "download" }),
        expect.objectContaining({ path: "src/local.ts", action: "upload" }),
      ]),
    );
    expect(order.indexOf("remote-commit")).toBeGreaterThan(
      order.findIndex((item) => item.startsWith("upload:")),
    );
    expect(order.indexOf("local-apply")).toBeGreaterThan(
      order.indexOf("remote-commit"),
    );
    expect(order.at(-1)).toBe("cursor-commit");
    expect(made.local.manifest.rootDigest).toBe(made.transport.head.rootDigest);
    const checkpoint = made.adapter.inspectRecovery(recoveryScope).checkpoint!;
    expect(checkpoint.eventCursor).toBe(made.input.completionEventCursor);
    expect(checkpoint.completedEffects).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          deliveryId: made.input.transactionId,
          effectId: made.input.transactionId,
          idempotencyKey: `workspace-recovery:${made.input.transactionId}`,
          toolName: "workspace_recovery_commit",
          kind: "remote_create",
          retryClass: "reconcile",
        }),
      ]),
    );
  });

  it("[OH003:40] fences a delayed old transport response after term and generation takeover", async () => {
    expect(fixture.workspaceDelayedResponseFence).toBe(true);
    const recoveryScope = scope("hidden-workspace-delayed-takeover");
    const made = makeWorkspace(recoveryScope);
    let release!: (value: WorkspaceManifest) => void;
    let entered!: () => void;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const delayed = new Promise<WorkspaceManifest>((resolve) => {
      release = resolve;
    });
    const oldHead = structuredClone(made.transport.head);
    made.transport.readHead = vi.fn(async () => {
      entered();
      return delayed as Promise<typeof oldHead>;
    });
    const old = createWorkspaceRecoveryReconciler({
      storage: made.storage.restart(),
      now: () => 101,
      instanceId: "hidden-workspace-old-instance",
      authority: made.fence,
      local: made.local,
      transport: made.transport,
      ledger: made.ledger,
    });
    const pendingOld = old.reconcile(made.input);
    await started;

    const takeover = made.adapter.pause({
      ...recoveryScope,
      ...authority(made.adapter.inspectRecovery(recoveryScope)),
      now: 102,
    });
    const resumed = made.adapter.resume({
      ...recoveryScope,
      ...authority(takeover),
      now: 103,
    });
    made.fence.replace({
      term: 4,
      tabId: "hidden-workspace-tab-b",
      incarnationId: "hidden-workspace-incarnation-b",
      runGeneration: resumed.checkpoint!.runGeneration,
    });
    made.transport.readHead = vi.fn(async () => structuredClone(oldHead));
    const successor = createWorkspaceRecoveryReconciler({
      storage: made.storage.restart(),
      now: () => 104,
      instanceId: "hidden-workspace-successor-instance",
      authority: made.fence,
      local: made.local,
      transport: made.transport,
      ledger: made.ledger,
    });
    const won = await successor.reconcile({
      ...made.input,
      ledgerAuthority: {
        ...authority(resumed),
        eventCursor: resumed.checkpoint!.eventCursor + 1,
      },
      completionEventCursor: resumed.checkpoint!.eventCursor + 2,
      now: 104,
    });
    expect(won.completed).toBe(true);
    const commits = made.transport.commitCalls.length;
    const applies = made.local.applyCalls.length;
    const cursor =
      made.adapter.inspectRecovery(recoveryScope).checkpoint!.eventCursor;
    release(structuredClone(oldHead));
    const stale = await pendingOld;
    expect(stale).toEqual(
      expect.objectContaining({ staleAuthority: true, suppressed: true }),
    );
    expect(made.transport.commitCalls).toHaveLength(commits);
    expect(made.local.applyCalls).toHaveLength(applies);
    expect(
      made.adapter.inspectRecovery(recoveryScope).checkpoint!.eventCursor,
    ).toBe(cursor);
  });

  it("[OH004:10] keeps workspace inspection bounded and content-safe across exact scopes", async () => {
    expect(fixture.workspaceProjectionIsolation).toBe(true);
    const recoveryScope = scope("hidden-workspace-projection");
    const made = makeWorkspace(recoveryScope);
    const reconciler = createWorkspaceRecoveryReconciler({
      storage: made.storage.restart(),
      now: () => 101,
      instanceId: "hidden-workspace-projection-instance",
      authority: made.fence,
      local: made.local,
      transport: made.transport,
      ledger: made.ledger,
    });
    expect((await reconciler.reconcile(made.input)).completed).toBe(true);
    const projection = JSON.stringify(reconciler.inspect(recoveryScope));
    for (const secret of [
      "src/local.ts",
      "docs/remote.md",
      workspaceDigests.local,
      workspaceDigests.remote,
      made.fence.current()!.tabId,
      made.fence.current()!.incarnationId,
      made.input.ledgerAuthority.fencingToken,
    ]) {
      expect(projection).not.toContain(secret);
    }
    expect(
      reconciler.inspect(
        scope(recoveryScope.conversationId, "fixture-backend-b"),
      ),
    ).toEqual(
      expect.objectContaining({
        phase: "idle",
        transactionId: null,
        recordRevision: 0,
      }),
    );
  });
});
