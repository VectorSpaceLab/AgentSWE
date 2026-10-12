import { describe, expect, it, vi } from "vitest";
import {
  authority,
  createLeased,
  effectSpec,
  executeInput,
  scope,
} from "./helpers";
import fixture from "./fixture.json";

describe("test_003 pause/resume/cancel generation ABA fencing", () => {
  it("[OH201:35] pause/resume ABA fences a callback claimed in the old generation", async () => {
    expect(fixture.transitions).toEqual(
      expect.arrayContaining(["pause", "resume"]),
    );
    const created = createLeased(scope("hidden-pause-resume-aba"));
    expect(fixture.syncTermIsOrthogonal).toBe(true);
    expect(fixture.workspaceFenceIsOrthogonal).toBe(true);
    created.storage.setItem(
      "openhands:recovery-sync:v1:fixture-backend-a:hidden-pause-resume-aba:99",
      JSON.stringify({
        backendId: "fixture-backend-a",
        conversationId: "hidden-pause-resume-aba",
        revision: 99,
        term: 9000,
        leaderTabId: "orthogonal-tab",
      }),
    );
    created.storage.setItem(
      "openhands:workspace-recovery:v1:fixture-backend-a:hidden-pause-resume-aba:transaction:99",
      JSON.stringify({
        schemaVersion: 1,
        transactionId: "orthogonal-workspace-transaction",
        revision: 99,
        fence: {
          term: 12000,
          tabId: "orthogonal-workspace-tab",
          incarnationId: "orthogonal-workspace-incarnation",
          runGeneration: 1,
        },
      }),
    );
    let entered!: () => void;
    let release!: () => void;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const callback = vi.fn(async () => {
      entered();
      await gate;
      return { wrote: true };
    });
    const task = created.adapter.executeEffect(
      executeInput(created.inspection, {
        deliveryId: "pause-resume-old",
        idempotencyKey: "aba:pause-resume",
        kind: "local_write",
        retryClass: "reconcile",
      }),
      callback,
    );
    await started;
    const claimed = created.adapter.inspectRecovery(
      scope("hidden-pause-resume-aba"),
    );
    const paused = created.adapter.pause({
      ...scope("hidden-pause-resume-aba"),
      ...authority(claimed),
      now: 102,
    });
    const resumed = created.adapter.resume({
      ...scope("hidden-pause-resume-aba"),
      ...authority(paused),
      now: 103,
    });
    expect(paused.checkpoint!.runGeneration).toBe(
      claimed.checkpoint!.runGeneration + 1,
    );
    expect(resumed.checkpoint!.runGeneration).toBe(
      paused.checkpoint!.runGeneration + 1,
    );
    expect(resumed.checkpoint!.runGeneration).toBe(3);
    release();
    await task;
    const final = created.adapter.inspectRecovery(
      scope("hidden-pause-resume-aba"),
    );
    expect(final.checkpoint!.terminalState).toBe("active");
    expect(final.checkpoint!.runGeneration).toBe(
      resumed.checkpoint!.runGeneration,
    );
    expect(
      final.checkpoint!.completedEffects.some(
        (item) => item.deliveryId === "pause-resume-old",
      ),
    ).toBe(false);
    expect(
      final.checkpoint!.audit.some(
        (item) =>
          item.type === "stale_settlement" &&
          item.runGeneration === claimed.checkpoint!.runGeneration,
      ),
    ).toBe(true);
  });

  it("[OH202:30] cancellation wins a newer in-flight attempt and cannot be resumed", async () => {
    const created = createLeased(scope("hidden-cancel-generation"));
    let entered!: () => void;
    let release!: () => void;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const callback = vi.fn(async () => {
      entered();
      await gate;
      return "late-success";
    });
    const task = created.adapter.executeEffect(
      executeInput(created.inspection, {
        deliveryId: "cancelled-delivery",
        idempotencyKey: "aba:cancel",
        kind: "remote_create",
        retryClass: "reconcile",
      }),
      callback,
    );
    await started;
    const claimed = created.adapter.inspectRecovery(
      scope("hidden-cancel-generation"),
    );
    const cancelled = created.adapter.cancel({
      ...scope("hidden-cancel-generation"),
      ...authority(claimed),
      now: 102,
    });
    expect(cancelled.checkpoint!.runGeneration).toBe(
      claimed.checkpoint!.runGeneration + 1,
    );
    release();
    await task;
    const final = created.adapter.inspectRecovery(
      scope("hidden-cancel-generation"),
    );
    expect(final.checkpoint!.terminalState).toBe("cancelled");
    expect(final.checkpoint!.cancellationRevision).toBe(
      cancelled.checkpoint!.revision,
    );
    expect(
      final.checkpoint!.completedEffects.some(
        (item) => item.idempotencyKey === "aba:cancel",
      ),
    ).toBe(false);
    expect(() =>
      created.adapter.resume({
        ...scope("hidden-cancel-generation"),
        ...authority(final),
        now: 103,
      }),
    ).toThrow();
  });

  it("[OH203:20] rejects old-generation retry authority after resume", async () => {
    const created = createLeased(scope("hidden-old-generation-retry"));
    const oldAuthority = authority(created.inspection);
    const paused = created.adapter.pause({
      ...scope("hidden-old-generation-retry"),
      ...oldAuthority,
      now: 101,
    });
    const resumed = created.adapter.resume({
      ...scope("hidden-old-generation-retry"),
      ...authority(paused),
      now: 102,
    });
    const callback = vi.fn(async () => "must-not-run");
    await expect(
      created.adapter.executeEffect(
        {
          ...scope("hidden-old-generation-retry"),
          ...oldAuthority,
          eventCursor: 2,
          effect: effectSpec({
            deliveryId: "old-generation-delivery",
            idempotencyKey: "old:generation",
          }),
          now: 103,
        },
        callback,
      ),
    ).rejects.toThrow();
    expect(callback).not.toHaveBeenCalled();
    const current = created.adapter.inspectRecovery(
      scope("hidden-old-generation-retry"),
    );
    expect(current.checkpoint!.revision).toBe(resumed.checkpoint!.revision);
    expect(current.checkpoint!.runGeneration).toBe(
      resumed.checkpoint!.runGeneration,
    );
    expect(current.checkpoint!.pendingEffects).toHaveLength(0);
  });

  it("[OH204:15] keeps late terminal evidence bounded without resurrection", () => {
    const created = createLeased(
      scope("hidden-bounded-late-terminal"),
      undefined,
      100,
      { maxRetainedRevisions: 8 },
    );
    let current = created.adapter.cancel({
      ...scope("hidden-bounded-late-terminal"),
      ...authority(created.inspection),
      now: 101,
    });
    for (let index = 0; index < 80; index++) {
      current = created.adapter.ingestRuntimeEvent({
        ...scope("hidden-bounded-late-terminal"),
        ...authority(current),
        eventId: `late-terminal-${index}`,
        eventCursor: index + 2,
        type: index % 2 ? "effect_failed" : "effect_completed",
        effect: {
          ...effectSpec({
            deliveryId: `late-delivery-${index}`,
            effectId: `late-effect-${index}`,
            idempotencyKey: `late:key:${index}`,
          }),
          status: "completed",
          attempt: 1,
          runGeneration: current.checkpoint!.runGeneration,
          enqueueRevision: current.checkpoint!.revision,
        },
        now: 102 + index,
      });
    }
    expect(current.checkpoint!.terminalState).toBe("cancelled");
    expect(current.checkpoint!.completedEffects).toHaveLength(0);
    expect(current.checkpoint!.audit.length).toBeLessThanOrEqual(64);
    expect(
      current.checkpoint!.audit.some((item) => item.type === "late_terminal"),
    ).toBe(true);
  });
});
