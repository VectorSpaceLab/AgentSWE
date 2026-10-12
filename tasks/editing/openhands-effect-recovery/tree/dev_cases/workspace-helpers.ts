import type { RecoveryScope } from "#/api/recovery/recovery-evaluator-adapter";
import type {
  WorkspaceLocalReplica,
  WorkspaceManifest,
  WorkspaceManifestEntry,
  WorkspaceRecoveryFence,
  WorkspaceRecoveryTransport,
  WorkspaceRemoteCommitReceipt,
  WorkspaceRemoteCommitRequest,
} from "#/api/recovery/workspace-recovery-reconciler";

const encoder = new TextEncoder();

export const workspaceBytes = {
  base: encoder.encode("base"),
  local: encoder.encode("local"),
  remote: encoder.encode("remote"),
  conflict: encoder.encode("conflict"),
  successor: encoder.encode("successor"),
};

export const workspaceDigests = {
  base: "sha256:cae662172fd450bb0cd710a769079c05bfc5d8e35efa6576edc7d0377afdd4a2",
  local:
    "sha256:25bf8e1a2393f1108d37029b3df5593236c755742ec93465bbafa9b290bddcf6",
  remote:
    "sha256:b71199ebd070b36beab7317920c2c2f1d777df8d05e5527d8458fda57cb17a7a",
  conflict:
    "sha256:fa9e1d22205ad852b0dc9509ec4e31644e88742c4dfce93c08f011fee1cd8a1a",
  successor:
    "sha256:548e56fceb6ba189d7cd154c1a0af13de44f8cbfdb454a98ee9ebf3c10d30479",
};

export function workspaceEntry(
  path: string,
  digest: string,
  bytes: Uint8Array,
): WorkspaceManifestEntry {
  return {
    path,
    kind: "file",
    mode: 0o644,
    size: bytes.byteLength,
    contentDigest: digest,
    chunkDigests: [digest],
  };
}

export function workspaceManifest(
  scope: RecoveryScope,
  revision: string,
  baseRevision: string | null,
  entries: readonly WorkspaceManifestEntry[],
): WorkspaceManifest {
  const marker = revision.replace(/[^a-f0-9]/gi, "a").toLowerCase();
  return {
    ...scope,
    schemaVersion: 1,
    revision,
    baseRevision,
    rootDigest: `sha256:${(marker + "0".repeat(64)).slice(0, 64)}`,
    entries: [...entries].sort((left, right) =>
      left.path.localeCompare(right.path),
    ),
  };
}

export class PublicLocalReplica implements WorkspaceLocalReplica {
  readonly chunks = new Map<string, Uint8Array>();
  readonly staged = new Map<string, Uint8Array>();
  readonly applyCalls: Array<{ expectedRevision: string; target: string }> = [];

  constructor(public manifest: WorkspaceManifest) {}

  async capture(): Promise<WorkspaceManifest> {
    return structuredClone(this.manifest);
  }

  async readChunk(input: RecoveryScope & { digest: string }) {
    const value = this.chunks.get(input.digest);
    if (!value) throw new Error("missing local fixture chunk");
    return value;
  }

  async missingStagedChunks(input: {
    transactionId: string;
    digests: readonly string[];
  }) {
    return input.digests.filter(
      (digest) => !this.staged.has(`${input.transactionId}:${digest}`),
    );
  }

  async stageChunk(
    input: RecoveryScope & {
      transactionId: string;
      digest: string;
      bytes: Uint8Array;
    },
  ) {
    this.staged.set(`${input.transactionId}:${input.digest}`, input.bytes);
  }

  async apply(
    input: RecoveryScope & {
      expectedRevision: string;
      targetManifest: WorkspaceManifest;
    },
  ) {
    if (this.manifest.revision !== input.expectedRevision) {
      throw new Error("local revision conflict");
    }
    this.applyCalls.push({
      expectedRevision: input.expectedRevision,
      target: input.targetManifest.revision,
    });
    this.manifest = structuredClone(input.targetManifest);
    return structuredClone(this.manifest);
  }
}

export class PublicWorkspaceTransport implements WorkspaceRecoveryTransport {
  readonly chunks = new Map<string, Uint8Array>();
  readonly writeCalls: string[] = [];
  readonly commitCalls: WorkspaceRemoteCommitRequest[] = [];
  readonly reconcileCalls: WorkspaceRemoteCommitRequest[] = [];
  readonly committed = new Map<string, WorkspaceRemoteCommitReceipt>();
  loseNextWriteResponse = false;
  loseNextCommitResponse = false;

  constructor(public head: WorkspaceManifest) {}

  async readHead(): Promise<WorkspaceManifest> {
    return structuredClone(this.head);
  }

  async missingChunks(input: { digests: readonly string[] }) {
    return input.digests.filter((digest) => !this.chunks.has(digest));
  }

  async readChunk(input: RecoveryScope & { digest: string }) {
    const value = this.chunks.get(input.digest);
    if (!value) throw new Error("missing remote fixture chunk");
    return value;
  }

  async writeChunk(
    input: RecoveryScope & {
      digest: string;
      bytes: Uint8Array;
      fence: WorkspaceRecoveryFence;
    },
  ) {
    this.writeCalls.push(input.digest);
    this.chunks.set(input.digest, input.bytes);
    if (this.loseNextWriteResponse) {
      this.loseNextWriteResponse = false;
      throw new Error("synthetic chunk response loss");
    }
  }

  async commit(input: WorkspaceRemoteCommitRequest) {
    this.commitCalls.push(structuredClone(input));
    const existing = this.committed.get(input.transactionId);
    if (existing) return existing;
    if (this.head.revision !== input.expectedRevision) {
      throw new Error("remote revision conflict");
    }
    this.head = structuredClone(input.targetManifest);
    const receipt = {
      backendId: input.backendId,
      conversationId: input.conversationId,
      transactionId: input.transactionId,
      revision: input.targetManifest.revision,
      rootDigest: input.targetManifest.rootDigest,
    };
    this.committed.set(input.transactionId, receipt);
    if (this.loseNextCommitResponse) {
      this.loseNextCommitResponse = false;
      throw new Error("synthetic commit response loss");
    }
    return receipt;
  }

  async reconcileCommit(input: WorkspaceRemoteCommitRequest) {
    this.reconcileCalls.push(structuredClone(input));
    return this.committed.get(input.transactionId);
  }
}

export function mutableWorkspaceFence(initial: WorkspaceRecoveryFence) {
  let current = initial;
  return {
    current: () => current,
    replace(next: WorkspaceRecoveryFence) {
      current = next;
    },
  };
}
