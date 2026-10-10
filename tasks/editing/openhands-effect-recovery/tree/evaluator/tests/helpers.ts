import { createHash } from "node:crypto";
import { expect } from "vitest";
import {
  createRecoveryEvaluatorAdapter,
  legacyRecoveryStorageKey,
  type RecoveryAdapterOptions,
  type RecoveryEffect,
  type RecoveryInspection,
  type RecoveryScope,
} from "#/api/recovery/recovery-evaluator-adapter";

export class DurableStorage implements Storage {
  constructor(private readonly data = new Map<string, string>()) {}
  get length() {
    return this.data.size;
  }
  clear() {
    this.data.clear();
  }
  getItem(key: string) {
    return this.data.get(key) ?? null;
  }
  key(index: number) {
    return [...this.data.keys()][index] ?? null;
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
  setItem(key: string, value: string) {
    this.data.set(key, String(value));
  }
  restart() {
    return new DurableStorage(this.data);
  }
  entries() {
    return [...this.data.entries()];
  }
}

export const workspace = (suffix = "a") => ({
  digest: `sha256:fixture-tree-${suffix}`,
  version: `git:fixture-${suffix}`,
});

export const scope = (
  conversationId: string,
  backendId = "fixture-backend-a",
): RecoveryScope => ({ backendId, conversationId });

export const createLeased = (
  recoveryScope: RecoveryScope,
  storage = new DurableStorage(),
  now = 100,
  options: Partial<
    Omit<RecoveryAdapterOptions, "storage" | "now" | "instanceId">
  > = {},
  instanceId = "instance-a",
) => {
  const adapter = createRecoveryEvaluatorAdapter({
    storage,
    now: () => now,
    instanceId,
    ...options,
  });
  let inspection = adapter.createCheckpoint({
    ...recoveryScope,
    eventCursor: 1,
    workspace: workspace(),
    now,
  });
  inspection = adapter.acquireRecoveryLease({
    ...recoveryScope,
    ownerId: "owner-a",
    expectedRevision: inspection.checkpoint!.revision,
    ttlMs: 50,
    now,
  });
  return { adapter, storage, inspection };
};

export const authority = (inspection: RecoveryInspection) => ({
  ownerId: inspection.checkpoint!.lease!.ownerId,
  leaseEpoch: inspection.checkpoint!.lease!.epoch,
  fencingToken: inspection.checkpoint!.lease!.fencingToken,
  runGeneration: inspection.checkpoint!.runGeneration,
  expectedRevision: inspection.checkpoint!.revision,
});

export const effectSpec = (
  overrides: Partial<RecoveryEffect> = {},
): Omit<
  RecoveryEffect,
  | "status"
  | "attempt"
  | "runGeneration"
  | "enqueueRevision"
  | "claim"
  | "appliedAt"
> => ({
  deliveryId: "delivery-1",
  effectId: "effect-1",
  idempotencyKey: "fixture:key:1",
  toolName: "fixture_tool",
  kind: "read_only",
  retryClass: "replay_safe",
  resultMetadata: { digestAlgorithm: "sha256" },
  ...overrides,
});

export const fullEffect = (
  inspection: RecoveryInspection,
  overrides: Partial<RecoveryEffect> = {},
): RecoveryEffect => ({
  ...effectSpec(overrides),
  status: "queued",
  attempt: 0,
  runGeneration: inspection.checkpoint!.runGeneration,
  enqueueRevision: inspection.checkpoint!.revision,
  claim: null,
  appliedAt: null,
  ...overrides,
});

export const executeInput = (
  inspection: RecoveryInspection,
  overrides: Partial<RecoveryEffect> = {},
) => ({
  backendId: inspection.checkpoint!.backendId,
  conversationId: inspection.checkpoint!.conversationId,
  ...authority(inspection),
  eventCursor: inspection.checkpoint!.eventCursor + 1,
  effect: effectSpec(overrides),
  now: 101,
});

export const appendRuntimeEvent = (
  adapter: ReturnType<typeof createRecoveryEvaluatorAdapter>,
  inspection: RecoveryInspection,
  eventId: string,
  eventCursor: number,
  now: number,
) =>
  adapter.ingestRuntimeEvent({
    backendId: inspection.checkpoint!.backendId,
    conversationId: inspection.checkpoint!.conversationId,
    ...authority(inspection),
    eventId,
    eventCursor,
    type: "resumed",
    now,
  });

export const checkpointJson = (storage: DurableStorage) =>
  storage
    .entries()
    .map(([, value]) => value)
    .join("\n");

const canonical = (value: unknown): string => {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`)
    .join(",")}}`;
};

export const writeLegacyV2 = (
  storage: Storage,
  conversationId: string,
  overrides: Record<string, unknown> = {},
) => {
  const unsigned = {
    schemaVersion: 2,
    conversationId,
    checkpointId: `legacy-${conversationId}-1`,
    revision: 1,
    eventCursor: 7,
    workspace: workspace("legacy"),
    lease: { ownerId: "legacy-owner", epoch: 4, expiresAt: 90 },
    terminalState: "active",
    cancellationRevision: null,
    completedEffects: [
      {
        effectId: "legacy-complete",
        idempotencyKey: "legacy:complete",
        toolName: "legacy_tool",
        kind: "local_write",
        status: "completed",
        retryClass: "already_completed",
        claim: null,
        resultMetadata: { digestAlgorithm: "sha256" },
      },
    ],
    pendingEffects: [
      {
        effectId: "legacy-pending",
        idempotencyKey: "legacy:pending",
        toolName: "legacy_remote",
        kind: "remote_create",
        status: "uncertain",
        retryClass: "reconcile",
        claim: null,
      },
    ],
    recentEventIds: ["legacy-event-7"],
    audit: [],
    compaction: {
      generation: 0,
      compactedThroughRevision: 0,
      compactedThroughEventCursor: 0,
      anchorDigest: null,
    },
    ...overrides,
  };
  const record = {
    ...unsigned,
    integrity: {
      algorithm: "sha256",
      digest: `sha256:${createHash("sha256").update(canonical(unsigned)).digest("hex")}`,
    },
  };
  storage.setItem(
    legacyRecoveryStorageKey(conversationId, Number(record.revision)),
    JSON.stringify(record),
  );
  return record;
};

export const expectDenied = (value: RecoveryInspection) => {
  expect(value).toEqual(
    expect.objectContaining({
      access: "denied",
      boundary: "forbidden",
      summary: null,
      checkpoint: null,
    }),
  );
};
