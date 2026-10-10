import type {
  AuthorizedInspectionInput,
  ExecuteEffectInput,
  ExecuteEffectResult,
  RecoveryInspection,
  RecoveryInspectionLimits,
  RecoveryScope,
} from "#/api/recovery/recovery-evaluator-adapter";
import {
  createRecoveryEvaluatorAdapter,
  legacyRecoveryStorageKey,
  recoveryStorageKey,
} from "#/api/recovery/recovery-evaluator-adapter";
import ConversationService from "#/api/conversation-service/conversation-service.api";
import AgentServerRuntimeService from "#/api/runtime-service/agent-server-runtime-service";
import { useRecoveryStore } from "#/stores/recovery-store";
import { useEventStore } from "#/stores/use-event-store";
import { RecoveryStatus } from "#/components/features/conversation/recovery-status";
import {
  createConversationEventDispatcher,
  type ConversationEventDispatcher,
  type ConversationEventDispatchResult,
  type ConversationEventSession,
} from "#/api/recovery/conversation-event-dispatcher";

declare const storage: Storage;
const scope: RecoveryScope = {
  backendId: "type-backend",
  conversationId: "type-conversation",
};
const adapter = createRecoveryEvaluatorAdapter({
  storage,
  now: () => 100,
  instanceId: "type-instance",
  maxRetainedRevisions: 8,
  defaultClaimTtlMs: 25,
  crashAfter: null,
});

const input: ExecuteEffectInput = {
  ...scope,
  ownerId: "runtime-a",
  leaseEpoch: 2,
  fencingToken: "fencing-token-type-0001",
  runGeneration: 3,
  expectedRevision: 7,
  eventCursor: 9,
  effect: {
    deliveryId: "delivery-type-contract",
    effectId: "effect-type-contract",
    idempotencyKey: "type:contract:1",
    toolName: "type_contract",
    kind: "remote_create",
    retryClass: "reconcile",
  },
  claimTtlMs: 25,
  now: 100,
};
const execute = async (): Promise<{ id: number }> => ({ id: 1 });
const reconcile = async (
  idempotencyKey: string,
): Promise<{ id: number } | undefined> =>
  idempotencyKey === "type:contract:1" ? { id: 1 } : undefined;
const adapterResult: Promise<ExecuteEffectResult<{ id: number }>> =
  adapter.executeEffect(input, execute, reconcile);
const runtimeResult: typeof adapterResult =
  AgentServerRuntimeService.executeRecoverableEffect(input, execute, reconcile);

const limits: RecoveryInspectionLimits = { maxEffects: 3, maxAuditEntries: 2 };
const authorized: AuthorizedInspectionInput = {
  ...scope,
  tabId: "type-tab",
  grantToken: "type-grant-token",
  limits,
  now: 100,
};
const internal: RecoveryInspection = adapter.inspectRecovery(scope, limits);
const projected: RecoveryInspection =
  adapter.inspectRecoveryAuthorized(authorized);

void adapterResult;
void runtimeResult;
void internal;
void projected;
void adapter.enqueueEffect(input);
void adapter.issueInspectionGrant({
  ...scope,
  ownerId: input.ownerId,
  leaseEpoch: input.leaseEpoch,
  fencingToken: input.fencingToken,
  runGeneration: input.runGeneration,
  expectedRevision: input.expectedRevision,
  audienceTabId: authorized.tabId,
  level: "status",
  ttlMs: 50,
  maxEffects: 0,
  maxAuditEntries: 0,
});
void ConversationService.inspectRecovery(scope, limits);
void ConversationService.inspectRecoveryAuthorized(authorized);
void useRecoveryStore.getState().refresh(authorized);
void useRecoveryStore.getState().watch(authorized);
void useRecoveryStore.getState().disconnect();
void useEventStore.getState().ingestRecoveryEvent({
  ...scope,
  eventId: "type-runtime-event",
  eventCursor: input.eventCursor,
  ownerId: input.ownerId,
  leaseEpoch: input.leaseEpoch,
  fencingToken: input.fencingToken,
  runGeneration: input.runGeneration,
  expectedRevision: input.expectedRevision,
  type: "resumed",
  now: 100,
});
const eventSinks = {
  emitOrdinaryEvent(_event: unknown) {},
  appendTerminalOutput(_chunk: {
    backendId: string;
    conversationId: string;
    streamId: string;
    chunkId: string;
    text: string;
  }) {},
};
const dispatcher: ConversationEventDispatcher =
  createConversationEventDispatcher({
    inspectRecovery: (value) => adapter.inspectRecovery(value),
    ingestRuntimeEvent: (value) => adapter.ingestRuntimeEvent(value),
    ...eventSinks,
  });
const session: ConversationEventSession = dispatcher.openSession({
  ...scope,
  sessionId: "type-history-socket-session",
});
const dispatched: ConversationEventDispatchResult = dispatcher.dispatch({
  session,
  recoveryEvent: {
    ...scope,
    eventId: "type-dispatch-event",
    eventCursor: input.eventCursor,
    ownerId: input.ownerId,
    leaseEpoch: input.leaseEpoch,
    fencingToken: input.fencingToken,
    runGeneration: input.runGeneration,
    expectedRevision: input.expectedRevision,
    type: "resumed",
  },
  ordinaryEvent: { id: "type-wrapper" },
  terminalOutput: {
    streamId: "type-terminal",
    chunkId: "type-chunk",
    text: "type output",
  },
});
dispatcher.closeSession(session);
void ConversationService.createRecoveryEventDispatcher(eventSinks);
void session.sinceEventCursor;
void dispatched;
void RecoveryStatus;
void recoveryStorageKey(scope.backendId, scope.conversationId, 1);
void legacyRecoveryStorageKey(scope.conversationId, 1);
