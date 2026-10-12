"""Generate an API-only Candidate runtime. It has no model or private oracle."""
from pathlib import Path
import json


def product_server_source(lower, case_id: str, task: str, nonce: str) -> str:
    # Reuse the reviewed public dispatch table, not the old in-process loop.
    source = lower.js_driver(case_id, task, nonce, Path("/bridge/unused.json"), {})
    source = source.split('describe("OpenHands Agent Canvas lower-agent recovery case"', 1)[0]
    source = source.replace('import { createCaseWorld } from "./.agentswe_case_world";\n', '')
    start, end = source.index("async function askModel("), source.index("function resolveReference(")
    source = source[:start] + source[end:]
    source = source.replace('return method.bind(ConversationService);', 'return traceProduct(ConversationService, "ConversationService")[name];')
    source = source.replace('(AgentServerRuntimeService as any).executeRecoverableEffect(', 'traceProduct(AgentServerRuntimeService, "AgentServerRuntimeService").executeRecoverableEffect(')
    return 'import { traceProduct, productCalls, beginAction, storageEvidence, createRecoveryUi } from "./.agentswe_product_observation";\n' + source + r''' 
import http from "node:http";
const wire = (value: any): any => ArrayBuffer.isView(value) ? { __bytes__: Array.from(value as any) }
  : Array.isArray(value) ? value.map(wire)
  : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, wire(item)])) : value;
const unwire = (value: any): any => value && Array.isArray(value.__bytes__) ? new Uint8Array(value.__bytes__)
  : Array.isArray(value) ? value.map(unwire)
  : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, unwire(item)])) : value;
function worldRequest(path: string, body: any): Promise<any> {
  return new Promise((resolve, reject) => {
    const request = http.request({ socketPath: "/bridge/world.sock", path, method: "POST", headers: { "content-type": "application/json" } }, response => {
      let text = ""; response.on("data", chunk => { text += chunk; }); response.on("end", () => {
        try { const value = unwire(JSON.parse(text)); value.error ? reject(new Error(value.error)) : resolve(value.result); } catch (error) { reject(error); }
      });
    }); request.on("error", reject); request.end(JSON.stringify(wire(body)));
  });
}
describe("isolated Candidate product API server", () => {
  it("serves real product actions without provider or evaluator filesystem access", async () => {
    const initial = await worldRequest("/init", {});
    let view = initial;
    const proxy = (prefix: string, names: string[]) => Object.fromEntries(names.map(name => [name, (input: any) => worldRequest("/io", { method: prefix + "." + name, input })]));
    for (const [key, value] of initial.initialStorage) window.localStorage.setItem(key, value);
    caseWorld = { scope: initial.scope, baseManifest: initial.baseManifest, now: () => view.clock,
      workspaceCrashAfter: () => view.workspace_fault?.crash_after ?? null,
      authority: { current: () => view.authority },
      transport: proxy("transport", ["pull", "readHead", "missingChunks", "readChunk", "writeChunk", "commit", "reconcileCommit"]),
      local: proxy("local", ["capture", "readChunk", "missingStagedChunks", "stageChunk", "apply"]),
      executeEffect: (input: any) => worldRequest("/io", { method: "executeEffect", input }),
      reconcileEffect: (input: any) => worldRequest("/io", { method: "reconcileEffect", input }),
    };
    vi.spyOn(Date, "now").mockImplementation(caseWorld.now);
    const newAdapter = () => createRecoveryEvaluatorAdapter({ storage: window.localStorage, now: caseWorld.now, instanceId: `lower-${nonce}`, maxRetainedRevisions: 8 });
    let adapter = traceProduct(newAdapter(), "RecoveryEvaluatorAdapter");
    const state: any = { inspection: null, grant: null };
    const production = makeProductionState();
    const recoveryUi = createRecoveryUi(() => ({ tabId: `tab-${nonce}`, incarnationId: production.incarnationId }));
    let currentEvidence: any = null;
    const backgroundEffects: any[] = [];
    let workspaceRestartEpoch = -1;
    let browserRestartCount = 0;
    let removalFailureArmed = false;
    const storageFaultObservations: any[] = [];
    const storageRemove = Storage.prototype.removeItem;
    const scopePrefix = `openhands:recovery:v3:${encodeURIComponent(initial.scope.backendId)}:${encodeURIComponent(initial.scope.conversationId)}:`;
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function(this: Storage, key: string) {
      if (removalFailureArmed && key.startsWith(scopePrefix)) {
        removalFailureArmed = false;
        storageFaultObservations.push({ source: "remove_failure_after_publication", key, old_record_present: this.getItem(key) !== null,
          actual_storage_snapshot: storageSnapshot().map(([name, raw]) => ({ key: name,
            sha256: createHash("sha256").update(raw ?? "").digest("hex"),
            revision: JSON.parse(raw ?? "{}").revision, compaction: JSON.parse(raw ?? "{}").compaction })) });
        throw new Error("synthetic localStorage removeItem failure after publication");
      }
      return storageRemove.call(this, key);
    });
    const storageSnapshot = () => Array.from({ length: window.localStorage.length }, (_, index) => {
      const key = window.localStorage.key(index)!; return [key, window.localStorage.getItem(key)];
    });
    await new Promise<void>((done, failed) => {
      const server = http.createServer((request, response) => {
        let text = ""; request.on("data", chunk => { text += chunk; if (text.length > 2_000_000) request.destroy(); });
        request.on("end", async () => {
          try {
            const input = unwire(JSON.parse(text || "{}"));
            let result;
            if (request.url === "/action") {
              view = input.world;
              if (workspaceRestartEpoch !== view.workspace_fault?.restart_epoch) {
                production.workspace = null;
                workspaceRestartEpoch = view.workspace_fault?.restart_epoch ?? 0;
              }
              const choice = input.choice;
              const callStart = beginAction();
              currentEvidence = { parsed: Boolean(choice && typeof choice.action === "string"), action: choice.action, calls_start: callStart, storage_before: storageEvidence() };
              if (!allowedActions.has(choice.action)) throw new Error("unsupported product action");
              if (["watch_recovery_status", "mount_recovery_status", "notify_recovery_storage", "inspect_recovery_ui", "disconnect_recovery_ui", "resume_recovery_ui"].includes(choice.action)) {
                const args = resolve(choice.arguments ?? {}, state.inspection, state.grant);
                result = await recoveryUi.perform(choice.action, args, caseWorld.scope);
              } else if (choice.action === "restart_product") {
                browserRestartCount += 1; production.incarnationId = `incarnation-${nonce}-restart-${browserRestartCount}`;
                adapter = traceProduct(newAdapter(), "RecoveryEvaluatorAdapter"); production.workspace = null; production.sync = null; production.dispatcher = null; production.session = null;
                result = adapter.inspectRecovery(caseWorld.scope);
              } else if (["execute_effect", "sync_production"].includes(choice.action)) {
                const operation = dispatch(adapter, choice.action, choice.arguments ?? {}, state.inspection, state.grant, production);
                const observation: any = { status: "pending" };
                operation.then(value => { observation.status = "settled"; observation.result = sanitized(value); }, error => { observation.status = "rejected"; observation.error = safeError(error); });
                backgroundEffects.push(observation);
                await new Promise(resolve => setTimeout(resolve, 10));
                result = { background_operation: choice.action, background_effect: observation, inspection: adapter.inspectRecovery(caseWorld.scope) };
              } else result = await dispatch(adapter, choice.action, choice.arguments ?? {}, state.inspection, state.grant, production);
              updateRuntimeState(result, state);
              currentEvidence = { ...currentEvidence, outcome: "returned", calls: productCalls.slice(callStart), storage_after: storageEvidence(), ui: recoveryUi.observe() };
              result = { result, observation: sanitized(result), inspection: state.inspection, storage: storageSnapshot(), dispatched: true, action_evidence: currentEvidence };
            } else if (request.url === "/callback") {
              // 0921: enqueueEffect is part of the product's published adapter surface
              // (input/02 line 91) and the durable-outbox probe drives it through this
              // callback exactly like ingestRuntimeEvent; without it the probe could
              // never run against any candidate.
              if (["ingestRuntimeEvent", "inspectRecoveryAuthorized", "inspectRecovery", "enqueueEffect"].includes(input.method)) result = await adapter[input.method](input.input);
              else if (input.method === "armRemovalFailure") { removalFailureArmed = true; result = { armed: true }; }
              else if (input.method === "staleMigrationAttempt") {
                const outcomes = [];
                for (let index = 0; index < 2; index++) {
                  const competitor = newAdapter();
                  try { outcomes.push({ rejected: false, result: competitor.recoverConversation(input.input) }); }
                  catch (error) { outcomes.push({ rejected: true, error: safeError(error) }); }
                }
                result = { outcomes, after: adapter.inspectRecovery(caseWorld.scope) };
              } else if (input.method === "corruptNewestRecords") {
                const rows = storageSnapshot().filter(([key]) => key.startsWith(scopePrefix)).map(([key, raw]) => ({ key, raw, record: JSON.parse(raw!) }));
                rows.sort((left, right) => right.record.revision - left.record.revision);
                const newest = rows[0];
                if (!newest || rows.length < 2) result = { injected: false, reason: "No valid retained predecessor available", actual_record_count: rows.length };
                else {
                  const corrupt = structuredClone(newest.record); corrupt.workspace.digest = "sha256:" + "f".repeat(64);
                  const future = structuredClone(newest.record); future.schemaVersion = 999; future.revision = newest.record.revision + 1000;
                  window.localStorage.setItem(newest.key, JSON.stringify(corrupt));
                  const futureKey = scopePrefix + future.revision; window.localStorage.setItem(futureKey, JSON.stringify(future));
                  result = { injected: true, newest_revision_before: newest.record.revision,
                    newest_integrity_unchanged: corrupt.integrity.digest === newest.record.integrity.digest,
                    future_schema: 999, retained_predecessor_revisions: rows.slice(1).map(row => row.record.revision),
                    compaction_before: newest.record.compaction, corruption_keys: [newest.key, futureKey] };
                }
              } else throw new Error("unsupported external callback");
            } else if (request.url === "/state") {
              state.inspection = adapter.inspectRecovery(caseWorld.scope);
              result = { inspection: state.inspection, storage: storageSnapshot(), background_effects: backgroundEffects,
              storage_fault_observations: storageFaultObservations, product_calls: productCalls, ui_observation: recoveryUi.observe(),
              production_observations: { ordinary_events: production.sinks.ordinary, terminal_chunks: production.sinks.terminal,
                sync_projection: production.sync?.inspect() ?? null, sync_notices: production.channel.notices } };
            }
            else if (request.url === "/shutdown") { result = { closed: true }; server.close(() => done()); }
            else throw new Error("unsupported product endpoint");
            response.writeHead(200, { "content-type": "application/json" }); response.end(JSON.stringify(wire({ result: result ?? null })));
          } catch (error: any) {
            const actionCalls = currentEvidence ? productCalls.slice(currentEvidence.calls_start) : [];
            let projection = null;
            try { projection = production.workspace?.inspect(production.scope) ?? null; } catch { /* actual product inspection unavailable */ }
            try { state.inspection = adapter.inspectRecovery(caseWorld.scope); } catch { /* preserve prior observation */ }
            response.writeHead(400, { "content-type": "application/json" }); response.end(JSON.stringify({ error: safeError(error), workspace_projection: projection, inspection: state.inspection, storage: storageSnapshot(), action_evidence: currentEvidence ? { ...currentEvidence, outcome: "threw", error: safeError(error), calls: actionCalls, storage_after: storageEvidence(), ui: recoveryUi.observe() } : null }));
          }
        });
      });
      server.on("error", failed); server.listen("/bridge/product.sock");
    });
  }, 600000);
});
'''
