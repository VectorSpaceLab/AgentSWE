import { describe, expect, it } from "vitest";
import {
  createRecoveryEvaluatorAdapter,
  recoveryStorageKey,
} from "#/api/recovery/recovery-evaluator-adapter";
import { createConversationEventDispatcher } from "#/api/recovery/conversation-event-dispatcher";
import {
  appendRuntimeEvent,
  authority,
  createLeased,
  DurableStorage,
  effectSpec,
  scope,
  workspace,
  writeLegacyV2,
} from "./helpers";
import fixture from "./fixture.json";

describe("test_005 schema migration, isolation, corruption, and compaction", () => {
  it("[OH401:35] migrates valid v2 exactly once and preserves semantic evidence", () => {
    expect(fixture.schemas).toEqual([2, 3]);
    expect(fixture.dispatcherMigratedCursorHandoff).toBe(true);
    const storage = new DurableStorage();
    writeLegacyV2(storage, "hidden-legacy-migration");
    const adapter = createRecoveryEvaluatorAdapter({
      storage,
      now: () => 100,
      instanceId: "migration-a",
    });
    const migrated = adapter.recoverConversation({
      ...scope("hidden-legacy-migration", "default-local"),
      ownerId: "migrated-owner",
      expectedRevision: 1,
      workspace: workspace("legacy"),
      ttlMs: 50,
      now: 100,
    });
    expect(migrated.checkpoint).toEqual(
      expect.objectContaining({
        schemaVersion: 3,
        backendId: "default-local",
        eventCursor: 7,
        terminalState: "active",
      }),
    );
    expect(
      migrated.checkpoint!.completedEffects.some(
        (item) => item.idempotencyKey === "legacy:complete",
      ),
    ).toBe(true);
    expect(
      migrated.checkpoint!.pendingEffects.some(
        (item) =>
          item.idempotencyKey === "legacy:pending" &&
          item.status === "uncertain",
      ),
    ).toBe(true);
    expect(migrated.checkpoint!.recentEventIds).toContain("legacy-event-7");
    expect(
      migrated.checkpoint!.audit.filter((item) => item.type === "migrated"),
    ).toHaveLength(1);
    expect(
      storage
        .entries()
        .filter(([key]) => key.startsWith("openhands:recovery:v3:")),
    ).toHaveLength(1);

    const ordinaryEvents: unknown[] = [];
    const dispatcher = createConversationEventDispatcher({
      inspectRecovery: (value) => adapter.inspectRecovery(value),
      ingestRuntimeEvent: (value) => adapter.ingestRuntimeEvent(value),
      emitOrdinaryEvent: (event) => ordinaryEvents.push(event),
    });
    const migratedHistory = dispatcher.openSession({
      ...scope("hidden-legacy-migration", "default-local"),
      sessionId: "migrated-history",
    });
    expect(migratedHistory.sinceEventCursor).toBe(7);
    const legacyEcho = dispatcher.dispatch({
      session: migratedHistory,
      recoveryEvent: {
        ...scope("hidden-legacy-migration", "default-local"),
        ...authority(migrated),
        eventId: "legacy-event-7",
        eventCursor: 700,
        type: "resumed",
        now: 100,
      },
      ordinaryEvent: { wrapperId: "legacy-history-echo" },
    });
    expect(legacyEcho).toEqual(
      expect.objectContaining({ accepted: false, suppressed: true }),
    );
    expect(ordinaryEvents).toHaveLength(0);

    const fresh = createRecoveryEvaluatorAdapter({
      storage: storage.restart(),
      now: () => 101,
      instanceId: "migration-b",
    });
    const paused = fresh.pause({
      ...scope("hidden-legacy-migration", "default-local"),
      ...authority(migrated),
      now: 101,
    });
    expect(
      paused.checkpoint!.audit.filter((item) => item.type === "migrated"),
    ).toHaveLength(1);
  });

  it("[OH402:20] resolves first-migration revision races without touching a foreign backend", () => {
    expect(fixture.sameRevisionMigrationRace).toBe(true);
    expect(fixture.staleRetryAfterRestart).toBe(true);
    const storage = new DurableStorage();
    writeLegacyV2(storage, "hidden-backend-isolation");
    const foreignAdapter = createRecoveryEvaluatorAdapter({
      storage,
      now: () => 100,
      instanceId: "foreign-lineage",
    });
    expect(
      foreignAdapter.inspectRecovery(
        scope("hidden-backend-isolation", "foreign-backend"),
      ),
    ).toEqual(expect.objectContaining({ boundary: "none", checkpoint: null }));
    const foreign = foreignAdapter.createCheckpoint({
      ...scope("hidden-backend-isolation", "foreign-backend"),
      eventCursor: 1,
      workspace: workspace("foreign"),
      now: 100,
    });

    const winner = createRecoveryEvaluatorAdapter({
      storage: storage.restart(),
      now: () => 101,
      instanceId: "migration-winner",
    });
    const stale = createRecoveryEvaluatorAdapter({
      storage: storage.restart(),
      now: () => 101,
      instanceId: "migration-stale",
    });
    const migrated = winner.recoverConversation({
      ...scope("hidden-backend-isolation", "default-local"),
      ownerId: "default-owner",
      expectedRevision: 1,
      workspace: workspace("legacy"),
      ttlMs: 50,
      now: 101,
    });
    expect(() =>
      stale.recoverConversation({
        ...scope("hidden-backend-isolation", "default-local"),
        ownerId: "stale-owner",
        expectedRevision: 1,
        workspace: workspace("legacy"),
        ttlMs: 50,
        now: 101,
      }),
    ).toThrow();

    const restartedStale = createRecoveryEvaluatorAdapter({
      storage: storage.restart(),
      now: () => 102,
      instanceId: "migration-stale-restart",
    });
    expect(() =>
      restartedStale.recoverConversation({
        ...scope("hidden-backend-isolation", "default-local"),
        ownerId: "stale-owner-after-restart",
        expectedRevision: 1,
        workspace: workspace("legacy"),
        ttlMs: 50,
        now: 102,
      }),
    ).toThrow();

    const defaultAfterConflict = winner.inspectRecovery(
      scope("hidden-backend-isolation", "default-local"),
    );
    expect(defaultAfterConflict.checkpoint!.revision).toBe(
      migrated.checkpoint!.revision,
    );
    expect(defaultAfterConflict.checkpoint!.lease!.ownerId).toBe(
      "default-owner",
    );
    expect(
      defaultAfterConflict.checkpoint!.audit.filter(
        (item) => item.type === "migrated",
      ),
    ).toHaveLength(1);
    expect(
      storage
        .entries()
        .map(([key]) => key)
        .filter(
          (key) =>
            key.startsWith("openhands:recovery:v3:") &&
            key.includes(encodeURIComponent("default-local")),
        ),
    ).toHaveLength(1);
    expect(foreign.checkpoint!.backendId).toBe("foreign-backend");
    expect(migrated.checkpoint!.backendId).toBe("default-local");
    expect(
      foreignAdapter.inspectRecovery(
        scope("hidden-backend-isolation", "foreign-backend"),
      ).checkpoint!.workspace,
    ).toEqual(workspace("foreign"));
  });

  it("[OH403:25] falls back from corrupt/future newest records after compaction", () => {
    const recoveryScope = scope("hidden-corrupt-compacted");
    const created = createLeased(
      recoveryScope,
      undefined,
      100,
      { maxRetainedRevisions: 8 },
      "corrupt-a",
    );
    let current = created.inspection;
    for (let index = 0; index < 14; index++) {
      current = appendRuntimeEvent(
        created.adapter,
        current,
        `compact-event-${index}`,
        index + 2,
        101 + index,
      );
    }
    const newestRevision = current.checkpoint!.revision;
    const newestKey = recoveryStorageKey(
      recoveryScope.backendId,
      recoveryScope.conversationId,
      newestRevision,
    );
    created.storage.setItem(
      newestKey,
      created.storage
        .getItem(newestKey)!
        .replace("fixture-tree-a", "tampered-tree"),
    );
    const priorRaw = created.storage
      .entries()
      .find(([key]) => key !== newestKey)![1];
    created.storage.setItem(
      recoveryStorageKey(
        recoveryScope.backendId,
        recoveryScope.conversationId,
        999,
      ),
      priorRaw
        .replace('"schemaVersion":3', '"schemaVersion":999')
        .replace(/"revision":\d+/, '"revision":999'),
    );
    const fallback = created.adapter.inspectRecovery(recoveryScope);
    expect(fallback.ignoredInvalidRecords).toBeGreaterThanOrEqual(2);
    expect(fallback.checkpoint!.revision).toBeLessThan(newestRevision);
    expect(fallback.checkpoint!.compaction.generation).toBeGreaterThan(0);
    expect(fallback.checkpoint!.compaction.anchorDigest).toMatch(
      /^sha256:[0-9a-f]{64}$/,
    );
  });

  it("[OH404:20] compacts a migrated lineage without crossing scope", () => {
    expect(fixture.migratedCompactionScopeIsolation).toBe(true);
    expect(fixture.legacyRecordsImmutable).toBe(true);
    expect(fixture.removeFailureAfterPublication).toBe(true);
    class OneRemovalFailureStorage extends DurableStorage {
      armed = false;
      failureCount = 0;
      failedKey: string | null = null;

      removeItem(key: string) {
        if (this.armed && this.failureCount === 0) {
          this.failureCount += 1;
          this.failedKey = key;
          throw new Error("synthetic localStorage removeItem failure");
        }
        super.removeItem(key);
      }
    }
    const run = (injectRemovalFailure = false) => {
      const conversationId = "hidden-migrated-compact/%2F";
      const recoveryScope = scope(conversationId, "default-local");
      const foreignScope = scope(conversationId, fixture.foreignBackend);
      const storage = injectRemovalFailure
        ? new OneRemovalFailureStorage()
        : new DurableStorage();
      expect(fixture.syncRecordsSurviveLedgerCompaction).toBe(true);
      expect(fixture.workspaceRecordsSurviveLedgerCompaction).toBe(true);
      const syncRecordKey =
        `openhands:recovery-sync:v1:${encodeURIComponent(recoveryScope.backendId)}:` +
        `${encodeURIComponent(recoveryScope.conversationId)}:1`;
      const syncRecordValue = JSON.stringify({
        backendId: recoveryScope.backendId,
        conversationId: recoveryScope.conversationId,
        revision: 1,
        term: 7,
        durableEventCursor: 7,
      });
      storage.setItem(syncRecordKey, syncRecordValue);
      const workspaceRecordKey =
        `openhands:workspace-recovery:v1:${encodeURIComponent(recoveryScope.backendId)}:` +
        `${encodeURIComponent(recoveryScope.conversationId)}:transaction-a:1`;
      const workspaceRecordValue = JSON.stringify({
        schemaVersion: 1,
        backendId: recoveryScope.backendId,
        conversationId: recoveryScope.conversationId,
        transactionId: "transaction-a",
        revision: 1,
        phase: "planned",
      });
      storage.setItem(workspaceRecordKey, workspaceRecordValue);
      writeLegacyV2(storage, conversationId);
      const legacyBefore = storage
        .entries()
        .filter(([key]) => key.startsWith("openhands:recovery:v2:"));
      const foreign = createLeased(
        foreignScope,
        storage,
        100,
        { maxRetainedRevisions: 8 },
        "deterministic-foreign",
      );
      const foreignBefore = storage
        .entries()
        .filter(([key]) =>
          key.startsWith(
            `openhands:recovery:v3:${encodeURIComponent(foreignScope.backendId)}:${encodeURIComponent(foreignScope.conversationId)}:`,
          ),
        );
      const adapter = createRecoveryEvaluatorAdapter({
        storage:
          storage instanceof OneRemovalFailureStorage
            ? storage
            : storage.restart(),
        now: () => 101,
        instanceId: "deterministic-migrated",
        maxRetainedRevisions: 8,
      });
      const migrated = adapter.recoverConversation({
        ...recoveryScope,
        ownerId: "migrated-compact-owner",
        expectedRevision: 1,
        workspace: workspace("legacy"),
        ttlMs: 100,
        now: 101,
      });
      let current = adapter.enqueueEffect({
        ...recoveryScope,
        ...authority(migrated),
        eventCursor: 8,
        effect: effectSpec({
          deliveryId: "preserved-outbox",
          idempotencyKey: "preserved:outbox",
          kind: "remote_create",
          retryClass: "reconcile",
        }),
        now: 102,
      });
      if (storage instanceof OneRemovalFailureStorage) {
        storage.armed = true;
      }
      let postFailure:
        | {
            failedKey: string;
            failedKeyStillPresent: boolean;
            compactedThroughRevision: number;
          }
        | undefined;
      for (let index = 0; index < 16; index++) {
        const failuresBefore =
          storage instanceof OneRemovalFailureStorage
            ? storage.failureCount
            : 0;
        current = appendRuntimeEvent(
          adapter,
          current,
          `deterministic-${index}`,
          index + 9,
          103 + index,
        );
        if (
          storage instanceof OneRemovalFailureStorage &&
          failuresBefore === 0 &&
          storage.failureCount === 1
        ) {
          postFailure = {
            failedKey: storage.failedKey!,
            failedKeyStillPresent: storage.getItem(storage.failedKey!) !== null,
            compactedThroughRevision:
              current.checkpoint!.compaction.compactedThroughRevision,
          };
        }
      }
      const scopePrefix =
        `openhands:recovery:v3:${encodeURIComponent(recoveryScope.backendId)}:` +
        `${encodeURIComponent(recoveryScope.conversationId)}:`;
      return {
        adapter,
        storage,
        current,
        migrated,
        recoveryScope,
        foreign,
        foreignBefore,
        legacyBefore,
        scopePrefix,
        removalFailureCount:
          storage instanceof OneRemovalFailureStorage
            ? storage.failureCount
            : 0,
        postFailure,
        syncRecordKey,
        syncRecordValue,
        workspaceRecordKey,
        workspaceRecordValue,
      };
    };
    const first = run();
    const second = run();
    const faulted = run(true);
    expect(first.current.checkpoint!.compaction).toEqual(
      expect.objectContaining({
        generation: second.current.checkpoint!.compaction.generation,
        compactedThroughRevision:
          second.current.checkpoint!.compaction.compactedThroughRevision,
        compactedThroughEventCursor:
          second.current.checkpoint!.compaction.compactedThroughEventCursor,
        anchorDigest: expect.stringMatching(/^sha256:[0-9a-f]{64}$/),
      }),
    );
    expect(
      first.storage
        .entries()
        .filter(([key]) => key.startsWith(first.scopePrefix)),
    ).toHaveLength(8);
    expect(first.current.checkpoint!.lease!.fencingToken).toBe(
      first.migrated.checkpoint!.lease!.fencingToken,
    );
    expect(first.current.checkpoint!.runGeneration).toBe(
      first.migrated.checkpoint!.runGeneration,
    );
    expect(
      first.current.checkpoint!.pendingEffects.some(
        (item) => item.deliveryId === "preserved-outbox",
      ),
    ).toBe(true);
    expect(
      first.storage
        .entries()
        .filter(([key]) => key.startsWith("openhands:recovery:v2:")),
    ).toEqual(first.legacyBefore);
    const foreignAfter = first.storage
      .entries()
      .filter(([key]) =>
        key.startsWith(
          `openhands:recovery:v3:${encodeURIComponent(first.foreign.inspection.checkpoint!.backendId)}:${encodeURIComponent(first.foreign.inspection.checkpoint!.conversationId)}:`,
        ),
      );
    expect(foreignAfter).toEqual(first.foreignBefore);
    expect(first.storage.getItem(first.syncRecordKey)).toBe(
      first.syncRecordValue,
    );
    expect(first.storage.getItem(first.workspaceRecordKey)).toBe(
      first.workspaceRecordValue,
    );
    expect(
      first.foreign.adapter.inspectRecovery(
        scope(
          first.foreign.inspection.checkpoint!.conversationId,
          first.foreign.inspection.checkpoint!.backendId,
        ),
      ).checkpoint,
    ).toEqual(first.foreign.inspection.checkpoint);
    expect(faulted.removalFailureCount).toBe(1);
    expect(faulted.postFailure).toEqual(
      expect.objectContaining({ failedKeyStillPresent: true }),
    );
    const failedRevision = Number(
      faulted.postFailure!.failedKey.split(":").at(-1),
    );
    expect(faulted.postFailure!.compactedThroughRevision).toBeLessThan(
      failedRevision,
    );
    expect(
      faulted.storage
        .entries()
        .filter(([key]) => key.startsWith(faulted.scopePrefix)),
    ).toHaveLength(8);
    expect(faulted.current.checkpoint!.revision).toBeGreaterThan(
      faulted.migrated.checkpoint!.revision,
    );

    const dispatcherSideEffects: unknown[] = [];
    const dispatcher = createConversationEventDispatcher({
      inspectRecovery: (value) => first.adapter.inspectRecovery(value),
      ingestRuntimeEvent: (value) => first.adapter.ingestRuntimeEvent(value),
      emitOrdinaryEvent: (event) => dispatcherSideEffects.push(event),
    });
    const migratedSession = dispatcher.openSession({
      ...first.recoveryScope,
      sessionId: "compacted-default-local",
    });
    expect(migratedSession.sinceEventCursor).toBe(
      first.current.checkpoint!.eventCursor,
    );
    const foreignSession = dispatcher.openSession({
      backendId: first.foreign.inspection.checkpoint!.backendId,
      conversationId: first.foreign.inspection.checkpoint!.conversationId,
      sessionId: "foreign-lineage",
    });
    expect(foreignSession.sinceEventCursor).toBe(
      first.foreign.inspection.checkpoint!.eventCursor,
    );
    const delayedDefault = dispatcher.dispatch({
      session: migratedSession,
      recoveryEvent: {
        ...first.recoveryScope,
        ...authority(first.current),
        eventId: "delayed-default-local-after-scope-switch",
        eventCursor: first.current.checkpoint!.eventCursor + 1,
        type: "resumed",
        now: 200,
      },
      ordinaryEvent: { wrapperId: "must-not-cross-scope" },
    });
    expect(delayedDefault.staleSession).toBe(true);
    expect(dispatcherSideEffects).toHaveLength(0);
  });
});
