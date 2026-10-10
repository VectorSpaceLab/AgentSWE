import { describe, expect, it, vi } from "vitest";
import fixture from "./fixture.json";
import {
  createWorkspaceRecoveryReconciler,
  type WorkspaceRecoveryCrashBoundary,
} from "#/api/recovery/workspace-recovery-reconciler";
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

function scenario(name: string, storage = new DurableStorage()) {
  const recoveryScope = scope(name);
  const created = createLeased(recoveryScope, storage, 100);
  const baseEntry = workspaceEntry(
    "base.txt",
    workspaceDigests.base,
    workspaceBytes.base,
  );
  const base = workspaceManifest(recoveryScope, `base-${name}`, null, [
    baseEntry,
  ]);
  const local = new PublicLocalReplica(
    workspaceManifest(recoveryScope, `local-${name}`, base.revision, [
      baseEntry,
      workspaceEntry("local.txt", workspaceDigests.local, workspaceBytes.local),
    ]),
  );
  local.chunks.set(workspaceDigests.base, workspaceBytes.base);
  local.chunks.set(workspaceDigests.local, workspaceBytes.local);
  const transport = new PublicWorkspaceTransport(
    workspaceManifest(recoveryScope, `remote-${name}`, base.revision, [
      baseEntry,
      workspaceEntry(
        "remote.txt",
        workspaceDigests.remote,
        workspaceBytes.remote,
      ),
    ]),
  );
  transport.chunks.set(workspaceDigests.base, workspaceBytes.base);
  transport.chunks.set(workspaceDigests.remote, workspaceBytes.remote);
  const fence = mutableWorkspaceFence({
    term: 11,
    tabId: `tab-${name}`,
    incarnationId: `incarnation-${name}`,
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
    transactionId: `transaction-${name}`,
    baseManifest: base,
    ledgerAuthority: { ...authority(created.inspection), eventCursor: 2 },
    completionEventId: `complete-${name}`,
    completionEventCursor: 3,
    now: 101,
  };
  const options = {
    storage: storage.restart(),
    now: () => 101,
    instanceId: `instance-${name}`,
    authority: fence,
    local,
    transport,
    ledger,
  };
  return {
    ...created,
    recoveryScope,
    base,
    local,
    transport,
    fence,
    ledger,
    input,
    options,
  };
}

describe("test_002 resumable workspace transfer and crash recovery", () => {
  it("[OH101:15] reprobes a chunk after write-response loss and never retransfers present content", async () => {
    expect(fixture.workspaceChunkResponseLoss).toBe(true);
    const made = scenario("hidden-chunk-response-loss");
    made.transport.loseNextWriteResponse = true;
    const first = await createWorkspaceRecoveryReconciler(
      made.options,
    ).reconcile(made.input);
    expect(first).toEqual(
      expect.objectContaining({ completed: false, responseLost: true }),
    );
    expect(made.transport.chunks.has(workspaceDigests.local)).toBe(true);
    expect(made.local.applyCalls).toHaveLength(0);
    expect(
      made.adapter.inspectRecovery(made.recoveryScope).checkpoint!.eventCursor,
    ).toBe(1);
    const resumed = await createWorkspaceRecoveryReconciler({
      ...made.options,
      storage: made.storage.restart(),
      instanceId: "chunk-response-restart",
    }).reconcile(made.input);
    expect(resumed.completed).toBe(true);
    expect(made.transport.writeCalls).toEqual([workspaceDigests.local]);
    expect(
      made.local.staged.has(
        `${made.input.transactionId}:${workspaceDigests.remote}`,
      ),
    ).toBe(true);
  });

  it("[OH102:45] resumes every workspace plan/chunk/remote/local/cursor crash boundary", async () => {
    expect(fixture.workspaceCrashBoundaries).toEqual([
      "after_plan",
      "after_remote_commit",
      "after_local_apply",
    ]);
    for (const crashAfter of fixture.workspaceCrashBoundaries as WorkspaceRecoveryCrashBoundary[]) {
      const made = scenario(`hidden-workspace-crash-${crashAfter}`);
      const crashing = createWorkspaceRecoveryReconciler({
        ...made.options,
        crashAfter,
      });
      await expect(crashing.reconcile(made.input)).rejects.toThrow();
      const durable = crashing.inspect(made.recoveryScope);
      expect(durable.recordRevision).toBeGreaterThan(0);
      if (crashAfter === "after_plan") {
        expect(durable.phase).toBe("planned");
        expect(made.transport.writeCalls).toHaveLength(0);
      }
      if (crashAfter === "after_remote_commit") {
        expect(made.transport.committed.has(made.input.transactionId)).toBe(
          true,
        );
        expect(made.local.applyCalls).toHaveLength(0);
      }
      if (crashAfter === "after_local_apply") {
        expect(made.local.applyCalls).toHaveLength(1);
        expect(
          made.adapter.inspectRecovery(made.recoveryScope).checkpoint!
            .eventCursor,
        ).toBeLessThan(made.input.completionEventCursor);
      }
      const restarted = await createWorkspaceRecoveryReconciler({
        ...made.options,
        storage: made.storage.restart(),
        instanceId: `restart-${crashAfter}`,
        crashAfter: null,
      }).reconcile(made.input);
      expect(restarted.completed || restarted.suppressed).toBe(true);
      expect(made.local.manifest.rootDigest).toBe(
        made.transport.head.rootDigest,
      );
      expect(
        made.adapter.inspectRecovery(made.recoveryScope).checkpoint!
          .eventCursor,
      ).toBe(made.input.completionEventCursor);
      expect(made.transport.commitCalls).toHaveLength(1);
      expect(made.local.applyCalls).toHaveLength(1);
    }
  });

  it("[OH103:30] reconciles an uncertain remote CAS exactly once while a rival suppresses", async () => {
    expect(fixture.workspaceCommitResponseLoss).toBe(true);
    const made = scenario("hidden-workspace-commit-response-loss");
    made.transport.loseNextCommitResponse = true;
    const lost = await createWorkspaceRecoveryReconciler(
      made.options,
    ).reconcile(made.input);
    expect(lost.responseLost).toBe(true);
    expect(made.transport.committed.has(made.input.transactionId)).toBe(true);
    expect(made.local.applyCalls).toHaveLength(0);
    const uncertain = made.adapter.inspectRecovery(made.recoveryScope);
    expect(uncertain.checkpoint!.pendingEffects).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          deliveryId: made.input.transactionId,
          status: "uncertain",
          claim: null,
        }),
      ]),
    );

    let release!: () => void;
    let entered!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const originalReconcile = made.transport.reconcileCommit.bind(
      made.transport,
    );
    made.transport.reconcileCommit = vi.fn(async (input) => {
      entered();
      await gate;
      return originalReconcile(input);
    });
    const winner = createWorkspaceRecoveryReconciler({
      ...made.options,
      storage: made.storage.restart(),
      instanceId: "workspace-commit-winner",
    });
    const rival = createWorkspaceRecoveryReconciler({
      ...made.options,
      storage: made.storage.restart(),
      instanceId: "workspace-commit-rival",
    });
    const pendingWinner = winner.reconcile(made.input);
    await started;
    const losing = await rival.reconcile(made.input);
    expect(losing.suppressed).toBe(true);
    release();
    expect((await pendingWinner).completed).toBe(true);
    expect(made.transport.commitCalls).toHaveLength(1);
    expect(made.transport.reconcileCommit).toHaveBeenCalledTimes(1);
    expect(made.local.applyCalls).toHaveLength(1);
  });

  it("[OH104:10] rejects invalid manifests and corrupt chunks before commit or apply", async () => {
    expect(fixture.workspaceManifestAndChunkValidation).toBe(true);
    const invalid = scenario("hidden-invalid-manifest");
    invalid.local.manifest = workspaceManifest(
      invalid.recoveryScope,
      "invalid-local",
      invalid.base.revision,
      [
        workspaceEntry(
          "../outside.txt",
          workspaceDigests.local,
          workspaceBytes.local,
        ),
      ],
    );
    const invalidResult = await createWorkspaceRecoveryReconciler(
      invalid.options,
    ).reconcile(invalid.input);
    expect(invalidResult.projection).toEqual(
      expect.objectContaining({
        phase: "corrupt",
        errorCode: "invalid_manifest",
      }),
    );
    expect(invalid.transport.commitCalls).toHaveLength(0);
    expect(invalid.local.applyCalls).toHaveLength(0);

    const corrupt = scenario("hidden-corrupt-chunk");
    corrupt.transport.chunks.set(
      workspaceDigests.remote,
      new TextEncoder().encode("tampered bytes"),
    );
    const corruptResult = await createWorkspaceRecoveryReconciler(
      corrupt.options,
    ).reconcile(corrupt.input);
    expect(corruptResult.projection).toEqual(
      expect.objectContaining({ phase: "corrupt", errorCode: "corrupt_chunk" }),
    );
    expect(corrupt.transport.commitCalls).toHaveLength(0);
    expect(corrupt.local.applyCalls).toHaveLength(0);
    expect(
      corrupt.adapter.inspectRecovery(corrupt.recoveryScope).checkpoint!
        .eventCursor,
    ).toBe(1);
  });
});
