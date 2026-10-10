import type {
  RecoverySyncChannel,
  RecoverySyncNotice,
} from "#/api/recovery/recovery-sync-coordinator";

export class ManualSyncChannel implements RecoverySyncChannel {
  private listeners = new Set<(notice: RecoverySyncNotice) => void>();
  readonly posted: RecoverySyncNotice[] = [];

  postMessage(notice: RecoverySyncNotice) {
    this.posted.push(notice);
  }

  subscribe(listener: (notice: RecoverySyncNotice) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  deliver(notice: RecoverySyncNotice) {
    for (const listener of [...this.listeners]) listener(notice);
  }
}
