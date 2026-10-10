/** Evaluator-only transparent observations; no product recovery implementation. */
import { createHash } from "node:crypto";
import React from "react";
import { act, render } from "@testing-library/react";
export const productCalls: any[] = [];
let actionId = 0;
const hash = (value: string) => createHash("sha256").update(value).digest("hex");
export function beginAction() { actionId += 1; return productCalls.length; }
export function traceProduct(target: any, surface: string): any {
  return new Proxy(target, { get(object, key, receiver) {
    const original = Reflect.get(object, key, receiver);
    if (typeof original !== "function") return original;
    return (...args: any[]) => {
      const call: any = { action_id: actionId, surface, method: String(key), entered: true, outcome: "pending" };
      productCalls.push(call);
      const settle = (result: any) => { call.outcome = "returned"; return /^createRecovery|createWorkspace/.test(String(key)) && result ? traceProduct(result, surface + "." + String(key)) : result; };
      try {
        const result = original.apply(object, args);
        if (result && typeof result.then === "function") return result.then(settle, (error: any) => { call.outcome = "rejected"; call.error_type = error?.name ?? "Error"; throw error; });
        return settle(result);
      } catch (error: any) { call.outcome = "threw"; call.error_type = error?.name ?? "Error"; throw error; }
    };
  }});
}
export function storageEvidence() {
  return Array.from({ length: localStorage.length }, (_, i) => {
    const key = localStorage.key(i)!;
    return { key_sha256: hash(key), value_sha256: hash(localStorage.getItem(key) ?? "") };
  }).sort((a, b) => a.key_sha256.localeCompare(b.key_sha256));
}
export function createRecoveryUi(identity: () => any) {
  let store: any, component: any, rendered: any, activeRequest: any;
  const events: any[] = [];
  async function load() {
    store ??= (await import("./src/stores/recovery-store")).useRecoveryStore;
    if (!component) { const loaded = await import("./src/components/features/conversation/recovery-status");
      component = traceProduct({ RecoveryStatus: loaded.RecoveryStatus }, "RecoveryStatus").RecoveryStatus; }
  }
  function observe() {
    const s = store?.getState();
    return { identity: identity(), mounted: Boolean(rendered), active_request: activeRequest ? { backendId: activeRequest.backendId, conversationId: activeRequest.conversationId, tabId: activeRequest.tabId } : null,
      rendered_status: rendered?.container.querySelector('[data-testid="recovery-status"]')?.textContent ?? null,
      store: s ? { scope: s.scope, tabId: s.tabId, access: s.access, boundary: s.boundary, summary: s.summary, visibleMessage: s.visibleMessage } : null,
      storage_events: [...events] };
  }
  async function perform(action: string, args: any, scope: any) {
    await load();
    if (action === "watch_recovery_status") {
      activeRequest = args.request ?? args;
      // The published request alone is passed. Missing Candidate inspection wiring stays missing.
      await act(async () => { await traceProduct(store.getState(), "useRecoveryStore").watch(activeRequest); });
    } else if (action === "mount_recovery_status") {
      activeRequest = args.request ?? args;
      await act(async () => { const element = React.createElement(component, { request: activeRequest });
        if (rendered) rendered.rerender(element); else rendered = render(element); });
    } else if (action === "notify_recovery_storage") {
      const eventScope = args.scope ?? scope;
      const prefix = `openhands:recovery:v3:${encodeURIComponent(eventScope.backendId)}:${encodeURIComponent(eventScope.conversationId)}:`;
      const key = args.key ?? Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)!).filter(k => k.startsWith(prefix)).sort().at(-1) ?? prefix + "0";
      const before = observe();
      await act(async () => { window.dispatchEvent(new StorageEvent("storage", { key, storageArea: localStorage, newValue: localStorage.getItem(key) })); });
      events.push({ key_sha256: hash(key), matching_active_scope: Boolean(activeRequest && key.startsWith(`openhands:recovery:v3:${encodeURIComponent(activeRequest.backendId)}:${encodeURIComponent(activeRequest.conversationId)}:`)), before: before.store, after: observe().store });
    } else if (action === "disconnect_recovery_ui") {
      await act(async () => { rendered?.unmount(); rendered = null; traceProduct(store.getState(), "useRecoveryStore").disconnect(); });
    } else if (action === "resume_recovery_ui") {
      if (!activeRequest) throw new Error("No prior recovery UI request in this tab incarnation");
      await act(async () => { await traceProduct(store.getState(), "useRecoveryStore").watch(activeRequest);
        const element = React.createElement(component, { request: activeRequest });
        if (rendered) rendered.rerender(element); else rendered = render(element); });
    } else if (action !== "inspect_recovery_ui") throw new Error("unsupported UI action");
    return observe();
  }
  return { perform, observe };
}
