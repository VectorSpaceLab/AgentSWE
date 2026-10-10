import { createRecoveryEvaluatorAdapter } from "#/api/recovery/recovery-evaluator-adapter";
import type {
  RecoveryAdapterOptions,
  RecoveryInspection,
  RecoveryScope,
} from "#/api/recovery/recovery-evaluator-adapter";
import type {
  RecoverySyncChannel,
  RecoverySyncNotice,
} from "#/api/recovery/recovery-sync-coordinator";

export class PublicStorage implements Storage {
  constructor(private data = new Map<string, string>()) {}
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
    return new PublicStorage(this.data);
  }
  keys() {
    return [...this.data.keys()];
  }
}

export class PublicSyncChannel implements RecoverySyncChannel {
  private listeners = new Set<(notice: RecoverySyncNotice) => void>();
  readonly notices: RecoverySyncNotice[] = [];

  postMessage(notice: RecoverySyncNotice) {
    this.notices.push(notice);
    for (const listener of [...this.listeners]) listener(notice);
  }

  subscribe(listener: (notice: RecoverySyncNotice) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  deliver(notice: RecoverySyncNotice) {
    for (const listener of [...this.listeners]) listener(notice);
  }
}

export const scope = (
  conversationId: string,
  backendId = "public-local",
): RecoveryScope => ({ backendId, conversationId });

export const adapter = (
  storage: Storage,
  now: number,
  instanceId: string,
  options: Partial<
    Omit<RecoveryAdapterOptions, "storage" | "now" | "instanceId">
  > = {},
) =>
  createRecoveryEvaluatorAdapter({
    storage,
    now: () => now,
    instanceId,
    ...options,
  });

export const authority = (inspection: RecoveryInspection) => ({
  ownerId: inspection.checkpoint!.lease!.ownerId,
  leaseEpoch: inspection.checkpoint!.lease!.epoch,
  fencingToken: inspection.checkpoint!.lease!.fencingToken,
  runGeneration: inspection.checkpoint!.runGeneration,
  expectedRevision: inspection.checkpoint!.revision,
});
