#!/usr/bin/env python3
"""Run the Candidate's actual OpenHands Agent Canvas product as lower agent.

The evaluator-owned driver imports the Candidate's TypeScript recovery adapter
inside the pinned ``@openhands/agent-canvas`` package.  A model-selected bounded
plan reaches the Candidate only through an evaluator-owned Agent Server shim
and Responses broker.  No external Codex/Aider agent substitutes for the
edited OpenHands product.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


MODEL = "deepseek-flash"
EFFORT = "high"
PRODUCT_NAME = "@openhands/agent-canvas"
PRODUCT_ENTRY = "src/api/recovery/recovery-evaluator-adapter.ts"
PRODUCTION_SERVICE_ENTRY = "src/api/conversation-service/conversation-service.api.ts"
FORBIDDEN_CREDENTIAL_ENV = {
    "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "SERPER_TOKEN", "CODEX_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
}


def sanitized_environment() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key not in FORBIDDEN_CREDENTIAL_ENV
        and not key.upper().endswith("_API_KEY")
        and not key.upper().endswith("_TOKEN")
        and not any(part in key.upper() for part in ("SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY", "JUDGE", "EVALUATOR", "UPSTREAM"))
    }
    env["OPENAI_API_KEY"] = "broker-only-placeholder"
    env["AGENTSWE_LOWER_BROKER_TOKEN"] = "broker-only-placeholder"
    return env


def redact_runtime_text(value: str, *private_paths: Path) -> str:
    result = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    result = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer <redacted>", result)
    result = re.sub(r"(?i)[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*\s*=\s*[^\s,;]+", "<redacted-credential>", result)
    for path in private_paths:
        result = result.replace(str(path), "<private-runtime>")
    for marker in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        result = result.replace(marker, "<redacted-env-name>")
    return result[-2000:]


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        name = relative.as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            digest.update(b"F"); digest.update(len(name).to_bytes(8, "big")); digest.update(name)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024): digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind, payload = b"O", b""
        digest.update(kind); digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def product_identity(candidate: Path) -> dict[str, Any]:
    package_path = candidate / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("name") != PRODUCT_NAME:
        raise RuntimeError(f"Candidate is not the pinned OpenHands product: {package.get('name')!r}")
    entry = candidate / PRODUCT_ENTRY
    if not entry.is_file():
        raise RuntimeError(f"Candidate OpenHands recovery entry missing: {PRODUCT_ENTRY}")
    production_entry = candidate / PRODUCTION_SERVICE_ENTRY
    if not production_entry.is_file():
        raise RuntimeError(f"Candidate OpenHands production service entry missing: {PRODUCTION_SERVICE_ENTRY}")
    return {
        "package_name": PRODUCT_NAME,
        "package_version": package.get("version"),
        "executed_entry": PRODUCT_ENTRY,
        "executed_production_entry": PRODUCTION_SERVICE_ENTRY,
        "execution_runtime": "Candidate TypeScript via Vitest/jsdom model-selected recovery action loop and ConversationService production factories",
        "external_coding_agent_substituted": False,
    }


def materialize_runtime_repository(candidate: Path, output: Path, node_modules: Path) -> Path:
    if not node_modules.is_dir():
        raise FileNotFoundError(f"prepared node_modules directory missing: {node_modules}")
    runtime = output / "runtime_repository"
    # A frozen Candidate is intentionally read-only.  If a prior materialized
    # runtime exists, make its directory entries traversable before cleanup;
    # otherwise shutil.rmtree(..., ignore_errors=True) can leave a read-only
    # node_modules directory behind and the next copy fails with EACCES.
    if runtime.exists():
        for path in (runtime, runtime / "node_modules"):
            if not path.exists():
                continue
            for child in path.rglob("*"):
                try:
                    child.chmod(child.stat().st_mode | (0o700 if child.is_dir() else 0o600))
                except OSError:
                    pass
            try:
                runtime_mode = path.stat().st_mode
                path.chmod(runtime_mode | 0o700)
            except OSError:
                pass
    shutil.rmtree(runtime, ignore_errors=True)
    shutil.copytree(
        candidate, runtime, symlinks=True,
        ignore=shutil.ignore_patterns("node_modules", ".git", ".agentswe_lower_case.test.ts"),
    )
    # ``candidate`` is the frozen, read-only source tree.  ``copytree``
    # copies its directory mode bits, so the newly-created runtime directory
    # can still be non-writable even though it is a fresh destination.  Make
    # the parent writable before creating the evaluator-owned dependency copy;
    # doing this after the second copy is too late because that copy needs to
    # create ``runtime/node_modules`` first.
    runtime.chmod(runtime.stat().st_mode | 0o700)
    shutil.copytree(node_modules, runtime / "node_modules", symlinks=True)
    for path in (runtime, runtime / "node_modules"):
        try:
            path.chmod(path.stat().st_mode | 0o700)
        except OSError:
            pass
        for child in path.rglob("*"):
            try:
                child.chmod(child.stat().st_mode | (0o600 if child.is_file() else 0o700))
            except OSError:
                pass
    return runtime


ACTION_NAMES = (
    "create_checkpoint", "enqueue_effect", "record_effect", "acquire_recovery_lease",
    "recover_conversation", "pause", "resume", "cancel", "issue_inspection_grant",
    "revoke_inspection_grant", "inspect_recovery", "inspect_recovery_authorized",
    "ingest_runtime_event", "execute_effect",
    "open_production_dispatch_session", "dispatch_production_event",
    "close_production_dispatch_session", "start_production_sync",
    "sync_production", "heartbeat_production_sync", "freeze_production_sync",
    "resume_production_sync", "stop_production_sync", "inspect_production_sync",
    "inspect_production_workspace", "reconcile_production_workspace",
    "restart_product", "watch_recovery_status", "mount_recovery_status",
    "notify_recovery_storage", "inspect_recovery_ui", "disconnect_recovery_ui", "resume_recovery_ui",
)


def model_prompt(case_id: str, task: str, nonce: str) -> str:
    actions = ", ".join(ACTION_NAMES)
    return f"""You are the lower recovery Agent operating the Candidate OpenHands Agent Canvas product for case {case_id}.

User task:
{task}

This is a live product interaction, not a request to describe a protocol or to run a
prewritten test. At each turn choose exactly one next product action after looking at
the sanitized observations from prior actions. You may also finish honestly when the
task is complete, blocked, or a conflict is confirmed. The only permitted action names
are: {actions}.

Return JSON only, with either:
{{"kind":"action","action":"<one permitted name>","arguments":{{...}},"rationale":"why this action is next"}}
or:
{{"kind":"finish","decision":"complete"|"partial"|"blocked"|"conflict","rationale":"evidence-grounded explanation"}}

A bare kind of complete, partial, blocked, or conflict is also a terminal decision, never a product action.
Arguments must match the public TypeScript API. Do not invent receipts, tokens, paths,
hidden expected values, or successful observations. The evaluator supplies a case-local
scope and nonce. Use these non-secret references when an API needs durable authority:
$ref.scope.backendId, $ref.scope.conversationId, $ref.state.revision,
$ref.state.eventCursor, $ref.state.runGeneration, $ref.state.lease.ownerId,
$ref.state.lease.epoch, $ref.state.lease.fencingToken, $ref.grant.token, and $ref.grant.id.
Public argument schemas (not an action sequence):
- RecoveryScope is flat: backendId and conversationId are fields directly inside arguments. For mutation and authorized-inspection inputs, never nest them in a scope object.
- Example argument shape (not an action sequence): {{"backendId":"$ref.scope.backendId","conversationId":"$ref.scope.conversationId", ...other API fields}}.
- create_checkpoint: scope + eventCursor:number + workspace:{{digest:string,version:string|null}}; optional expectedRevision, progress:{{phase:string,summary?:string}}, now.
- acquire_recovery_lease: scope + ownerId:string + ttlMs:number + expectedRevision:number; optional now.
- inspect_recovery: scope; optional limits:{{maxEffects:number,maxAuditEntries:number}}.
- owner transitions (pause/resume/cancel): scope + ownerId + leaseEpoch + fencingToken + runGeneration + expectedRevision; optional now.
- recover_conversation: scope + ownerId + expectedRevision + workspace:{{digest,version}} + ttlMs; optional now.
- enqueue_effect/execute_effect: owner-transition fields + eventCursor + effect:{{deliveryId,effectId,idempotencyKey,toolName,kind,retryClass}}; optional claimTtlMs.
- issue_inspection_grant: owner-transition fields + audienceTabId + level:status|support + ttlMs + maxEffects + maxAuditEntries.
- inspect_recovery_authorized: scope + tabId + grantToken + limits; optional now.
- watch_recovery_status / mount_recovery_status: published AuthorizedInspectionInput (scope + tabId + grantToken, optional now); request only, no injected inspection callback.
- notify_recovery_storage: optional scope or key; dispatches a native browser StorageEvent for the supplied scope.
- inspect_recovery_ui / disconnect_recovery_ui / resume_recovery_ui: no arguments. Resume reuses the prior request in the same tab incarnation and durable localStorage.
The mutation actions use the published Candidate recovery-adapter instance;
execute_effect uses AgentServerRuntimeService.executeRecoverableEffect; inspection
and production factories use the published ConversationService surfaces. Missing
product methods are real Candidate failures and will be reported by name.
The references are resolved inside the product boundary and their secret values are
never included in the model-visible observation or final artifact. For event-dispatch,
browser-sync, and workspace cases, use the production_* actions: they call the
Candidate ConversationService factory and production recovery modules rather than
evaluator-native Vitest tests. Those production surfaces still require the normal
product preconditions: establish a case-local checkpoint and acquire its current
recovery lease before dispatching, syncing, or reconciling; after an observation says
that no checkpoint or authority exists, choose the prerequisite product action instead
of repeating the same production action. Treat a suppressed result as evidence to
recover or inspect, not as a successful completion. Keep the interaction bounded and
make no more than twelve action selections. restart_product simulates reopening
the browser product against the same durable storage; it does not clear state."""


def js_driver(case_id: str, task: str, nonce: str, out: Path, fixture: dict[str, Any] | None = None) -> str:
    safe = lambda value: json.dumps(value, ensure_ascii=False)
    planned_task = model_prompt(case_id, task, nonce)
    action_names = safe(list(ACTION_NAMES))
    return f'''import {{ createHash }} from "node:crypto";
import {{ describe, expect, it, vi }} from "vitest";
import {{ createRecoveryEvaluatorAdapter, legacyRecoveryStorageKey }} from "./src/api/recovery/recovery-evaluator-adapter";
import {{ createCaseWorld }} from "./.agentswe_case_world";
import ConversationService from "./src/api/conversation-service/conversation-service.api";
import AgentServerRuntimeService from "./src/api/runtime-service/agent-server-runtime-service";
import * as fs from "node:fs";

const caseId = {safe(case_id)};
const modelTask = {safe(planned_task)};
const nonce = {safe(nonce)};
const out = {safe(str(out))};
const trajectoryOut = {safe(str(out.with_name("trajectory.json")))};
const phaseOut = {safe(str(out.with_name("execution_phase.json")))};
const worldOut = {safe(str(out.with_name("case_world.json")))};
const worldFixture = {safe(fixture or {})};
let caseWorld: any;
const allowedActions = new Set({action_names});
const MAX_ACTIONS = 12;
function phase(name: string, detail: any = {{}}) {{
  fs.writeFileSync(phaseOut, JSON.stringify({{phase: name, observed_at_ms: Date.now(), ...detail}}));
}}
function safeError(error: any): any {{
  let message = String(error?.message ?? error ?? "unknown error");
  message = message.replace(/\\x1b\\[[0-9;]*m/g, "");
  message = message.replace(/Bearer\\s+[^\\s,;]+/gi, "Bearer <redacted>")
    .replace(/(?:token|secret|password|credential|api_key)\\s*[:=]\\s*[^\\s,;]+/gi, "<redacted>");
  return {{error_code: String(error?.name || "ProductActionError"), message: message.slice(0, 400)}};
}}

function canonical(value: any): any {{
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {{
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }}
  return value;
}}
function canonicalText(value: any): string {{ return JSON.stringify(canonical(value)); }}
function digest(value: any): string {{ return createHash("sha256").update(canonicalText(value)).digest("hex"); }}

function sanitized(value: any, depth = 0): any {{
  if (depth > 8) return "<truncated>";
  if (value === null || value === undefined) return value ?? null;
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) return value.slice(0, 100).map(item => sanitized(item, depth + 1));
  const result: any = {{}};
  for (const key of Object.keys(value).sort()) {{
    if (/(token|credential|secret|authorization|password|prompt|body|bytes|path|raw.?error)/i.test(key)) {{
      result[key] = "<redacted>";
    }} else {{
      result[key] = sanitized(value[key], depth + 1);
    }}
  }}
  return result;
}}
function privateComparison(value: any): any {{
  if (Array.isArray(value)) return value.map(privateComparison);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
    /(token|credential|secret|authorization|password|prompt|bytes)/i.test(key) ? "<redacted>" : privateComparison(item)]));
}}

function closeUnclosedJson(text: string): string | null {{
  const stack: string[] = []; let inString = false; let escaped = false;
  for (const ch of text) {{
    if (escaped) {{ escaped = false; continue; }}
    if (inString) {{ if (ch === "\\\\") escaped = true; else if (ch === '"') inString = false; continue; }}
    if (ch === '"') inString = true;
    else if (ch === "{{" || ch === "[") stack.push(ch);
    else if (ch === "}}" || ch === "]") {{
      const open = stack.pop();
      if ((ch === "}}" && open !== "{{") || (ch === "]" && open !== "[")) return null;
    }}
  }}
  if (!stack.length && !inString) return null;
  let closed = text + (inString ? '"' : "");
  while (stack.length) closed += stack.pop() === "{{" ? "}}" : "]";
  return closed;
}}
function parseJson(text: string): any {{
  const trimmed = String(text || "").trim();
  const fenced = trimmed.match(/^```(?:json)?\\s*([\\s\\S]*?)\\s*```$/i);
  const body = fenced ? fenced[1] : trimmed;
  try {{ return JSON.parse(body); }} catch (error) {{
    // 2026-09-20 (readiness 0920-hd-005): an answer missing only its closing delimiters is
    // the model's format defect, not evaluator infrastructure. Close what was left open
    // (never insert content) and parse again; anything else still throws.
    const closed = closeUnclosedJson(body);
    if (closed === null) throw error;
    const value = JSON.parse(closed);
    console.error("json_envelope_closed: appended " + JSON.stringify(closed.slice(body.length)));
    return value;
  }}
}}
function responseText(body: any): string {{
  if (typeof body?.output_text === "string" && body.output_text.trim()) return body.output_text;
  const output = Array.isArray(body?.output) ? body.output : [];
  // The answer lives in message items' output_text parts. A reasoning item is
  // not the answer: concatenating it prepends the model's thinking to the JSON
  // and JSON.parse fails at character 0. Models that hide their reasoning never
  // exposed this, which is why taking every item's content used to work.
  return output.filter((item: any) => item?.type === "message")
    .flatMap((item: any) => Array.isArray(item?.content) ? item.content : [])
    .filter((item: any) => item?.type === "output_text")
    .map((item: any) => typeof item?.text === "string" ? item.text : "").join("");
}}

async function askModel(prompt: string, phase: string): Promise<any> {{
  let lastStatus = 0;
  for (let attempt = 0; attempt < 3; attempt++) {{
    const response = await fetch(process.env.AGENT_SERVER_ENDPOINT! + "/v1/plan", {{
      method: "POST",
      headers: {{ "content-type": "application/json", authorization: "Bearer broker-only-placeholder" }},
      body: JSON.stringify({{ task: prompt, metadata: {{ case_id: caseId, nonce, phase }} }}),
    }});
    if (response.ok) {{
      const text = responseText(await response.json());
      const value = parseJson(text);
      Object.defineProperty(value, "_model_response_sha256", {{value: createHash("sha256").update(text).digest("hex"), enumerable: false}});
      return value;
    }}
    lastStatus = response.status;
    if (![502, 503, 504].includes(response.status) || attempt === 2) break;
    await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)));
  }}
  throw new Error(`broker status ${{lastStatus}} after bounded retry`);
}}

function resolveReference(value: any, inspection: any, grant: any): any {{
  if (typeof value !== "string" || !value.startsWith("$ref.")) return value;
  const path = value.slice("$ref.".length).split(".");
  if (path[0] === "scope") {{
    if (path[1] === "backendId") return caseWorld.scope.backendId;
    if (path[1] === "conversationId") return caseWorld.scope.conversationId;
  }}
  if (path[0] === "grant" && path[1] === "token") return grant?.token;
  if (path[0] === "grant" && path[1] === "id") return grant?.grant?.grantId;
  if (path[0] === "state") {{
    let current: any = inspection?.checkpoint ?? inspection?.summary ? inspection : null;
    for (const part of path.slice(1)) current = current?.checkpoint?.[part] ?? current?.[part];
    return current;
  }}
  throw new Error("unresolvable runtime reference");
}}
function resolve(value: any, inspection: any, grant: any): any {{
  if (Array.isArray(value)) return value.map(item => resolve(item, inspection, grant));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, resolve(item, inspection, grant)]));
  return resolveReference(value, inspection, grant);
}}

function requireProductMethod(name: string): (...args: any[]) => any {{
  const method = (ConversationService as any)[name];
  if (typeof method !== "function") throw new Error(`Candidate ConversationService.${{name}} is missing`);
  return method.bind(ConversationService);
}}

function productCall(name: string, ...args: any[]): any {{
  return requireProductMethod(name)(...args);
}}

function productionScope() {{
  return caseWorld.scope;
}}

function productionEvent(scope: any, inspection: any, suffix: string) {{
  const checkpoint = inspection?.checkpoint;
  const lease = checkpoint?.lease;
  return {{
    ...scope,
    eventId: `event-${{nonce}}-${{suffix}}`,
    eventCursor: Number(checkpoint?.eventCursor ?? 0) + 1,
    ownerId: String(lease?.ownerId ?? `owner-${{nonce}}`),
    leaseEpoch: Number(lease?.epoch ?? 0),
    fencingToken: String(lease?.fencingToken ?? `missing-${{nonce}}`),
    runGeneration: Number(checkpoint?.runGeneration ?? 0),
    expectedRevision: Number(checkpoint?.revision ?? 0),
    type: "resumed",
    now: caseWorld.now(),
  }};
}}

function productionWorkspaceInput(scope: any, inspection: any) {{
  const checkpoint = inspection?.checkpoint;
  const lease = checkpoint?.lease;
  if (!checkpoint || !lease) throw new Error("workspace reconciliation requires a product checkpoint and lease");
  const baseManifest = caseWorld.baseManifest;
  return {{
    ...scope,
    transactionId: `workspace-${{nonce}}`,
    baseManifest,
    ledgerAuthority: {{
      ownerId: lease.ownerId, leaseEpoch: lease.epoch,
      fencingToken: lease.fencingToken, runGeneration: checkpoint.runGeneration,
      expectedRevision: checkpoint.revision, eventCursor: checkpoint.eventCursor,
    }},
    completionEventId: `workspace-complete-${{nonce}}`,
    completionEventCursor: Number(checkpoint.eventCursor) + 1,
    now: caseWorld.now(),
  }};
}}

function makeProductionState() {{
  const scope = productionScope();
  const state: any = {{
    scope, dispatcher: null, session: null, sync: null, workspace: null, incarnationId: `incarnation-${{nonce}}`,
    sinks: {{ ordinary: [], terminal: [], emitOrdinaryEvent: (event: any) => state.sinks.ordinary.push(sanitized(event)), appendTerminalOutput: (chunk: any) => state.sinks.terminal.push(sanitized(chunk)) }},
    channel: {{ listeners: new Set<Function>(), notices: [], postMessage: (notice: any) => state.channel.notices.push(sanitized(notice)), subscribe: (listener: Function) => {{ state.channel.listeners.add(listener); return () => state.channel.listeners.delete(listener); }} }},
  }};
  state.transport = caseWorld.transport;
  state.authority = caseWorld.authority;
  state.local = caseWorld.local;
  return state;
}}

async function dispatch(adapter: any, name: string, suppliedArguments: any, inspection: any, grant: any, production: any): Promise<any> {{
  const args = resolve(suppliedArguments ?? {{}}, inspection, grant);
  switch (name) {{
    case "create_checkpoint": return adapter.createCheckpoint(args);
    case "enqueue_effect": return adapter.enqueueEffect(args);
    case "record_effect": return adapter.recordEffect(args);
    case "acquire_recovery_lease": return adapter.acquireRecoveryLease(args);
    case "recover_conversation": return adapter.recoverConversation(args);
    case "pause": return adapter.pause(args);
    case "resume": return adapter.resume(args);
    case "cancel": return adapter.cancel(args);
    case "issue_inspection_grant": return adapter.issueInspectionGrant(args);
    case "revoke_inspection_grant": return adapter.revokeInspectionGrant(args);
    case "inspect_recovery": return productCall("inspectRecovery", args.scope ?? args, args.scope ? args.limits : undefined);
    case "inspect_recovery_authorized": return productCall("inspectRecoveryAuthorized", args);
    case "ingest_runtime_event": return adapter.ingestRuntimeEvent(args);
    case "execute_effect": {{
      const input = args.input ?? args;
      if (typeof (AgentServerRuntimeService as any).executeRecoverableEffect !== "function") throw new Error("Candidate AgentServerRuntimeService.executeRecoverableEffect is missing");
      return (AgentServerRuntimeService as any).executeRecoverableEffect(
        input,
        async () => caseWorld.executeEffect(input),
        async (key: string) => caseWorld.reconcileEffect(key),
      );
    }}
    case "open_production_dispatch_session": {{
      if (!production.dispatcher) production.dispatcher = productCall("createRecoveryEventDispatcher", production.sinks);
      production.session = production.dispatcher.openSession({{ ...production.scope, sessionId: `session-${{nonce}}` }});
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, session: production.session }};
    }}
    case "dispatch_production_event": {{
      if (!production.dispatcher || !production.session) throw new Error("production dispatch session is not open");
      const event = productionEvent(production.scope, inspection, String(args.suffix ?? "current"));
      const result = production.dispatcher.dispatch({{ session: production.session, recoveryEvent: event, ordinaryEvent: {{ recoveryEventId: event.eventId, caseId }}, terminalOutput: {{ streamId: `stream-${{nonce}}`, chunkId: `chunk-${{event.eventId}}`, text: "case-local terminal output" }} }});
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, result, sink_counts: {{ ordinary: production.sinks.ordinary.length, terminal: production.sinks.terminal.length }} }};
    }}
    case "close_production_dispatch_session": {{
      if (!production.dispatcher || !production.session) throw new Error("production dispatch session is not open");
      production.dispatcher.closeSession(production.session);
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, closed: true }};
    }}
    case "start_production_sync": {{
      if (!production.sync) production.sync = productCall("createRecoverySyncCoordinator", {{ now: caseWorld.now, tabId: `tab-${{nonce}}`, incarnationId: production.incarnationId, leaseTtlMs: 5, channel: production.channel, transport: production.transport, sinks: production.sinks }});
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, projection: production.sync.start(production.scope) }};
    }}
    case "sync_production": {{
      if (!production.sync) throw new Error("production sync is not started");
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, result: await production.sync.sync() }};
    }}
    case "heartbeat_production_sync": {{
      if (!production.sync) throw new Error("production sync is not started");
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, projection: production.sync.heartbeat() }};
    }}
    case "freeze_production_sync": {{
      if (!production.sync) throw new Error("production sync is not started");
      production.sync.freeze(); return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, projection: production.sync.inspect() }};
    }}
    case "resume_production_sync": {{
      if (!production.sync) throw new Error("production sync is not started");
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, projection: production.sync.resume() }};
    }}
    case "stop_production_sync": {{
      if (!production.sync) throw new Error("production sync is not started");
      production.sync.stop(); return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, stopped: true }};
    }}
    case "inspect_production_sync": {{
      if (!production.sync) throw new Error("production sync is not started");
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, projection: production.sync.inspect() }};
    }}
    case "inspect_production_workspace": {{
      if (!production.workspace) production.workspace = productCall("createWorkspaceRecoveryReconciler", {{ now: caseWorld.now, instanceId: `workspace-${{nonce}}`, authority: production.authority, local: production.local, transport: production.transport, crashAfter: caseWorld.workspaceCrashAfter?.() ?? null }});
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, projection: production.workspace.inspect(production.scope) }};
    }}
    case "reconcile_production_workspace": {{
      if (!production.workspace) production.workspace = productCall("createWorkspaceRecoveryReconciler", {{ now: caseWorld.now, instanceId: `workspace-${{nonce}}`, authority: production.authority, local: production.local, transport: production.transport, crashAfter: caseWorld.workspaceCrashAfter?.() ?? null }});
      const input = productionWorkspaceInput(production.scope, inspection);
      return {{ product_surface: {safe(PRODUCTION_SERVICE_ENTRY)}, result: await production.workspace.reconcile(input) }};
    }}
    default: throw new Error("unsupported product action");
  }}
}}

function updateRuntimeState(value: any, state: {{ inspection: any, grant: any }}) {{
  const inspection = value?.inspection ?? value;
  if (inspection && typeof inspection === "object" && ("checkpoint" in inspection || "summary" in inspection)) state.inspection = inspection;
  if (value && typeof value === "object" && value.token && value.grant) state.grant = value;
}}

function validateArtifact(artifact: any, steps: any[], trajectoryDigest: string): void {{
  if (!artifact || artifact.schema_version !== "agentswe-openhands-agent-result/v1") throw new Error("model artifact schema invalid");
  if (artifact.case_id !== caseId || artifact.model !== {safe(MODEL)} || artifact.reasoning_effort !== {safe(EFFORT)} || artifact.product !== {safe(PRODUCT_NAME)} || artifact.product_entry !== {safe(PRODUCT_ENTRY)}) throw new Error("model artifact identity invalid");
  if (!Array.isArray(artifact.actions) || artifact.actions.length !== steps.length || artifact.actions.some((action: any, index: number) => action !== steps[index].action)) throw new Error("model artifact action trajectory mismatch");
  const expectedObservations = steps.map(step => ({{ action: step.action, result: step.observation }}));
  if (!Array.isArray(artifact.observations) || canonicalText(artifact.observations) !== canonicalText(expectedObservations)) throw new Error("model artifact observations do not bind to product trajectory");
  if (artifact.trajectory_digest !== trajectoryDigest || !/^[0-9a-f]{{64}}$/i.test(String(artifact.nonce_digest || ""))) throw new Error("model artifact digest binding invalid");
  if (!["complete", "partial", "blocked", "conflict"].includes(artifact.decision) || typeof artifact.rationale !== "string" || !artifact.rationale.trim()) throw new Error("model artifact decision invalid");
}}

describe("OpenHands Agent Canvas lower-agent recovery case", () => {{
  it("lets the lower model select bounded actions through the Candidate product API", async () => {{
    caseWorld = createCaseWorld(caseId, nonce, worldFixture, window.localStorage, legacyRecoveryStorageKey);
    vi.spyOn(Date, "now").mockImplementation(caseWorld.now);
    const newAdapter = () => createRecoveryEvaluatorAdapter({{
      storage: window.localStorage,
      now: caseWorld.now,
      instanceId: `lower-${{nonce}}`,
      maxRetainedRevisions: 8,
    }});
    let adapter = newAdapter();
    const state: {{ inspection: any, grant: any }} = {{ inspection: null, grant: null }};
    const production = makeProductionState();
    const trajectory: any[] = [];
    let finalChoice: any = null;
    for (let step = 0; step < MAX_ACTIONS; step++) {{
      const context = `${{modelTask}}\\n\\nCurrent user-visible runtime notice:\\n${{JSON.stringify(caseWorld.visibleNotice())}}\\n` +
        `Previous sanitized product trajectory:\\n${{JSON.stringify(sanitized(trajectory))}}\\n` +
        `Choose one next action or finish. Do not repeat a successful read without a reason.`;
      phase("model_request", {{step: step + 1}});
      const choice = await askModel(context, "action");
      phase("model_response", {{step: step + 1, response_sha256: choice._model_response_sha256}});
      const kind = choice?.kind ?? (choice?.action === "finish" ? "finish" : "action");
      if (kind === "finish") {{ finalChoice = choice; break; }}
      const name = String(choice?.action ?? "");
      const suppliedArguments = choice?.arguments ?? choice?.args ?? {{}};
      let observation: any;
      let dispatched = false;
      let attempted = false;
      const actionStarted = Date.now();
      try {{
        if (!allowedActions.has(name)) throw new Error("unsupported product action");
        phase("product_action", {{step: step + 1, action: name}});
        attempted = true;
        caseWorld.beforeAction(name, state.inspection);
        let productResult: any;
        if (name === "restart_product") {{
          adapter = newAdapter();
          production.workspace = null; production.sync = null; production.dispatcher = null; production.session = null;
          productResult = adapter.inspectRecovery(caseWorld.scope);
        }} else productResult = await dispatch(adapter, name, suppliedArguments, state.inspection, state.grant, production);
        await caseWorld.afterAction(name, adapter, productResult);
        updateRuntimeState(productResult, state);
        observation = sanitized(productResult);
        dispatched = true;
      }} catch (error) {{
        observation = {{ status: "action_rejected", ...safeError(error) }};
      }}
      trajectory.push({{
        step: step + 1,
        action: name,
        arguments: sanitized(suppliedArguments),
        product_method: dispatched ? name : null,
        product_entry: {safe(PRODUCT_ENTRY)},
        product_surface: dispatched && name.startsWith("production_") ? {safe(PRODUCTION_SERVICE_ENTRY)} : {safe(PRODUCT_ENTRY)},
        dispatched,
        attempted,
        action_duration_ms: Date.now() - actionStarted,
        model_response_sha256: choice._model_response_sha256,
        observation,
      }});
      fs.writeFileSync(worldOut, JSON.stringify(privateComparison(caseWorld.comparisons()), null, 2));
      await import("node:fs/promises").then(fs => fs.writeFile(trajectoryOut, JSON.stringify({{
        schema_version: "agentswe-openhands-trajectory/v1", case_id: caseId,
        model: {safe(MODEL)}, reasoning_effort: {safe(EFFORT)}, product_entry: {safe(PRODUCT_ENTRY)},
        steps: trajectory, trajectory_digest: digest(trajectory),
      }}, null, 2) + "\\n"));
    }}
    if (!finalChoice) finalChoice = {{ decision: "blocked", rationale: "bounded action budget exhausted" }};
    const steps = trajectory;
    const trajectoryDigest = digest(steps);
    const artifactPrompt = `${{modelTask}}\\n\\nYou are now authoring the final case-bound artifact from the observed product trajectory.\\n` +
      `Trajectory digest (copy exactly): ${{trajectoryDigest}}\\n` +
      `Nonce digest (copy exactly): ${{digest(nonce)}}\\n` +
      `Observed trajectory (copy observations exactly; do not invent facts):\\n${{JSON.stringify(sanitized(steps))}}\\n` +
      `Return JSON only with schema_version, case_id, model, reasoning_effort, product, product_entry, ` +
      `actions (the exact action-name list), observations (objects with action and result), ` +
      `decision, rationale, nonce_digest, and trajectory_digest.`;
    phase("artifact_model_request", {{action_count: steps.length}});
    const authored = await askModel(artifactPrompt, "artifact");
    phase("artifact_validation", {{response_sha256: authored._model_response_sha256}});
    const artifact = authored?.artifact ?? authored;
    validateArtifact(artifact, steps, trajectoryDigest);
    await import("node:fs/promises").then(fs => fs.writeFile(out, JSON.stringify(artifact, null, 2) + "\\n"));
    phase("complete", {{response_sha256: authored._model_response_sha256}});
    expect(artifact.schema_version).toBe("agentswe-openhands-agent-result/v1");
  }}, 360000);
}});
'''


def write_launcher(output: Path, value: dict[str, Any]) -> None:
    (output / "launcher_result.json").write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def classify_process(returncode: int, evidence_text: str, execution_phase: dict[str, Any], artifact_value: dict[str, Any] | None, binding: dict[str, Any] | None) -> str:
    if any(marker in evidence_text.lower() for marker in ("broker status", "agent_server_provider_failure", "broker_upstream_failure")):
        return "provider_failure"
    if returncode == 125:
        return "lower_agent_infrastructure_failure"
    if returncode == 124:
        observed_phase = execution_phase.get("phase")
        if observed_phase in {"model_request", "artifact_model_request"}:
            return "provider_failure"
        if observed_phase in {"product_action", "artifact_validation", "model_response"}:
            return "candidate_behavior_failure"
        return "lower_agent_infrastructure_failure"
    if artifact_value is None:
        return "candidate_product_failure"
    if not binding or binding.get("bound") is not True:
        return "candidate_behavior_failure"
    return "valid_behavior" if returncode == 0 and artifact_value.get("decision") == "complete" else "candidate_behavior_failure"


def validate_trajectory_binding(output: Path, case_id: str, artifact: dict[str, Any], *, trusted_origin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the model-authored artifact against evaluator-captured steps.

    The TypeScript driver performs the same check before writing the artifact.
    Repeating it here makes the launcher fail closed if a runtime or later step
    mutates either file, and prevents an evaluator-created summary from being
    mistaken for the model's final delivery.
    """
    trajectory_path = output / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    steps = trajectory.get("steps") if isinstance(trajectory, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("model trajectory is empty or malformed")
    if trajectory.get("schema_version") != "agentswe-openhands-trajectory/v1":
        raise ValueError("model trajectory schema mismatch")
    if trajectory.get("case_id") != case_id:
        raise ValueError("model trajectory case mismatch")
    if trajectory.get("product_entry") != PRODUCT_ENTRY:
        raise ValueError("model trajectory product entry mismatch")
    if trajectory.get("trajectory_digest") != hashlib.sha256(canonical_json(steps).encode("utf-8")).hexdigest():
        raise ValueError("model trajectory digest mismatch")
    expected_actions: list[str] = []
    expected_observations: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("model trajectory step is not an object")
        action = step.get("action")
        # A rejected or unsupported model action is observable Candidate
        # behavior, not missing provenance. Preserve it for honest low/zero
        # scoring instead of invalidating an otherwise bound trajectory.
        if step.get("dispatched") is True:
            if action not in ACTION_NAMES or step.get("product_method") != action:
                raise ValueError("trajectory contains a forged product action")
        elif not isinstance(step.get("observation"), dict) or step["observation"].get("status") != "action_rejected":
            raise ValueError("trajectory rejection has no observed error")
        expected_actions.append(action)
        expected_observations.append({"action": action, "result": step.get("observation")})
    factual_mismatches = []
    if trusted_origin is not None:
        if trusted_origin.get("schema_version") != "agentswe-openhands-captured-origin/v1" or trusted_origin.get("bound") is not True:
            raise ValueError("trusted capture origin is missing")
        expected_claims = {"schema_version": "agentswe-openhands-agent-result/v1", "case_id": case_id,
            "model": MODEL, "reasoning_effort": EFFORT, "product": PRODUCT_NAME, "product_entry": PRODUCT_ENTRY,
            "actions": expected_actions, "observations": expected_observations,
            "trajectory_digest": trajectory["trajectory_digest"], "nonce_digest": trusted_origin["nonce_digest"]}
        for field, expected in expected_claims.items():
            if canonical_json(artifact.get(field)) != canonical_json(expected):
                factual_mismatches.append({"field": field, "claimed": artifact.get(field), "observed": expected,
                    "classification": "author_factual_claim_error", "origin_failure": False})
    else:
        if artifact.get("schema_version") != "agentswe-openhands-agent-result/v1":
            raise ValueError("model artifact schema mismatch")
        if artifact.get("case_id") != case_id or artifact.get("model") != MODEL or artifact.get("reasoning_effort") != EFFORT:
            raise ValueError("model artifact identity mismatch")
        if artifact.get("product") != PRODUCT_NAME or artifact.get("product_entry") != PRODUCT_ENTRY:
            raise ValueError("model artifact product mismatch")
        if not isinstance(artifact.get("actions"), list) or any(not isinstance(item, str) for item in artifact["actions"]):
            raise ValueError("model artifact actions schema is malformed")
        if not isinstance(artifact.get("observations"), list) or any(not isinstance(item, dict) for item in artifact["observations"]):
            raise ValueError("model artifact observations schema is malformed")
        factual_mismatches = []
        for field, expected in (("actions", expected_actions), ("observations", expected_observations)):
            if canonical_json(artifact[field]) != canonical_json(expected):
                factual_mismatches.append({"field": field, "claimed": artifact[field], "observed": expected,
                    "classification": "author_factual_claim_error", "origin_failure": False})
        if not isinstance(artifact.get("trajectory_digest"), str) or artifact.get("trajectory_digest") != trajectory["trajectory_digest"]:
            raise ValueError("model artifact trajectory digest mismatch")
        nonce_digest = artifact.get("nonce_digest")
        if not isinstance(nonce_digest, str) or len(nonce_digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in nonce_digest):
            raise ValueError("model artifact nonce digest missing")
        config = output / "private_world_config.json"
        if config.is_file():
            actual_nonce = json.loads(config.read_text())["nonce"]
            if nonce_digest != hashlib.sha256(canonical_json(actual_nonce).encode()).hexdigest():
                raise ValueError("model artifact belongs to a foreign case-world nonce")
        if artifact.get("decision") not in {"complete", "partial", "blocked", "conflict"}:
            raise ValueError("model artifact decision invalid")
        if not isinstance(artifact.get("rationale"), str) or not artifact["rationale"].strip():
            raise ValueError("model artifact rationale missing")
    return {
        "path": "agent_result.json",
        "trajectory_path": "trajectory.json",
        "trajectory_digest": trajectory["trajectory_digest"],
        "steps": len(steps),
        "bound": True,
        "author_factual_claim_mismatches": factual_mismatches,
        "captured_origin": trusted_origin,
    }


def _legacy_inprocess_main_unused() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository", type=Path, required=True)
    ap.add_argument("--case", type=Path, required=True)
    ap.add_argument("--broker-endpoint", required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--node-modules", type=Path)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()
    candidate, case, output = args.repository.resolve(), args.case.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_digest = tree_digest(candidate)
    try:
        identity = product_identity(candidate)
        node_modules_value = args.node_modules or (
            Path(os.environ["OPENHANDS_NODE_MODULES"]) if os.environ.get("OPENHANDS_NODE_MODULES") else None
        )
        if node_modules_value is None:
            raise FileNotFoundError("--node-modules or OPENHANDS_NODE_MODULES is required")
        node_modules = node_modules_value.resolve()
        repo = materialize_runtime_repository(candidate, output, node_modules)
    except Exception as exc:
        summary = {
            "schema_version": "agentswe-openhands-lower-launch/v3",
            "case_id": case.name,
            "candidate_product": PRODUCT_NAME,
            "candidate_source_digest": source_digest,
            "exit_code": 125,
            "stderr_tail": f"{type(exc).__name__}: {exc}",
            "broker_endpoint_is_evaluator_owned": True,
            "credential_seen_by_candidate": "broker-only-placeholder",
            "classification": "lower_agent_infrastructure_failure" if isinstance(exc, FileNotFoundError) else "candidate_product_failure",
            "model_protocol": {"model": MODEL, "reasoning_effort": EFFORT},
        }
        write_launcher(output, summary)
        print(json.dumps(summary, indent=2)); return 1

    case_id = case.name
    task_path = case / "natural_task.md" if (case / "natural_task.md").is_file() else case / "input.md"
    task = task_path.read_text(encoding="utf-8")
    fixture_path = case / "assets" / "fixtures.json"
    fixture = json.loads(fixture_path.read_text()) if fixture_path.is_file() else {}
    nonce = secrets.token_hex(16)
    driver = repo / ".agentswe_lower_case.test.ts"
    artifact = output / "agent_result.json"
    if artifact.exists() or (output / "trajectory.json").exists():
        raise RuntimeError("refusing to overwrite existing lower-agent case evidence; use a new output directory")
    driver.write_text(js_driver(case_id, task, nonce, artifact, fixture), encoding="utf-8")
    shutil.copy2(Path(__file__).with_name("case_world.ts"), repo / ".agentswe_case_world.ts")
    port_socket = socket.socket(); port_socket.bind(("127.0.0.1", 0)); port = port_socket.getsockname()[1]; port_socket.close()
    child_env = sanitized_environment()
    shim = subprocess.Popen(
        ["python3", str(Path(__file__).with_name("agent_server_shim.py")), "--port", str(port),
         "--broker", args.broker_endpoint, "--evaluation-scope", hashlib.sha256(
             ("agentswe-lower-evaluation/v1\x00" + str(args.case) + "\x00" +
              str(Path(args.output).resolve())).encode()).hexdigest()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env,
    )
    shim_ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                shim_ready = response.status == 200
            if shim_ready:
                break
        except Exception:
            time.sleep(0.1)
    if not shim_ready:
        shim.terminate(); shim.wait(timeout=5)
        summary = {
            "schema_version": "agentswe-openhands-lower-launch/v3", "case_id": case_id,
            "candidate_product": PRODUCT_NAME, "candidate_source_digest": source_digest,
            "exit_code": 125, "stderr_tail": "evaluator Agent Server shim did not become healthy",
            "broker_endpoint_is_evaluator_owned": True, "credential_seen_by_candidate": "broker-only-placeholder",
            "classification": "lower_agent_infrastructure_failure",
            "model_protocol": {"model": MODEL, "reasoning_effort": EFFORT}, "product_attestation": identity,
        }
        write_launcher(output, summary); print(json.dumps(summary, indent=2)); return 1

    runtime_home, runtime_cache, runtime_tmp = output / "home", output / "cache", output / "tmp"
    for path in (runtime_home, runtime_cache, runtime_tmp):
        path.mkdir(parents=True, exist_ok=True)
    env = child_env
    env.update({
        "AGENT_SERVER_ENDPOINT": f"http://127.0.0.1:{port}", "CI": "1", "VITEST_POOL_SIZE": "1",
        "HOME": str(runtime_home), "XDG_CACHE_HOME": str(runtime_cache), "TMPDIR": str(runtime_tmp),
        "npm_config_cache": str(runtime_cache / "npm"), "OPENHANDS_NODE_MODULES": str(node_modules),
    })
    command = [str(repo / "node_modules/.bin/vitest"), "run", str(driver), "--environment", "jsdom", "--reporter", "dot"]
    try:
        try:
            process = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, timeout=args.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            process = subprocess.CompletedProcess(command, 124, str(exc.stdout or ""), str(exc.stderr or "") + "\ntimeout")
        except OSError as exc:
            process = subprocess.CompletedProcess(command, 125, "", f"launcher OSError: {exc}")
    finally:
        shim.terminate()
        try:
            shim.wait(timeout=5)
        except subprocess.TimeoutExpired:
            shim.kill(); shim.wait(timeout=5)
    driver.unlink(missing_ok=True)

    evidence_text = f"{process.stdout} {process.stderr}".lower()
    try:
        execution_phase = json.loads((output / "execution_phase.json").read_text())
    except (OSError, json.JSONDecodeError):
        execution_phase = {}
    trajectory_binding: dict[str, Any] | None = None
    artifact_value: dict[str, Any] | None = None
    if artifact.exists():
        try:
            parsed = json.loads(artifact.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                artifact_value = parsed
                trajectory_binding = validate_trajectory_binding(output, case_id, parsed)
        except Exception as exc:
            trajectory_binding = {"bound": False, "error": type(exc).__name__}
    classification = classify_process(process.returncode, evidence_text, execution_phase, artifact_value, trajectory_binding)
    safe_command = [Path(item).name if item.startswith("/") else item for item in command]
    summary = {
        "schema_version": "agentswe-openhands-lower-launch/v3",
        "case_id": case_id,
        "candidate_product": PRODUCT_NAME,
        "candidate_source_digest": source_digest,
        "command": safe_command,
        "exit_code": process.returncode,
        "stdout_tail": redact_runtime_text(process.stdout, candidate, repo, node_modules, output),
        "stderr_tail": redact_runtime_text(process.stderr, candidate, repo, node_modules, output),
        "broker_endpoint_is_evaluator_owned": True,
        "credential_seen_by_candidate": "broker-only-placeholder",
        "classification": classification,
        "execution_phase": execution_phase,
        "model_protocol": {"model": MODEL, "reasoning_effort": EFFORT},
        "product_attestation": identity,
        "task_source": str(task_path),
        "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest() if fixture_path.is_file() else None,
        "case_world_sha256": hashlib.sha256((output / "case_world.json").read_bytes()).hexdigest() if (output / "case_world.json").is_file() else None,
        "agent_artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact.is_file() else None,
        "trajectory_sha256": hashlib.sha256((output / "trajectory.json").read_bytes()).hexdigest() if (output / "trajectory.json").is_file() else None,
    }
    if trajectory_binding is not None:
        summary["trajectory_binding"] = trajectory_binding
    if artifact_value is not None and trajectory_binding and trajectory_binding.get("bound") is True:
        summary["agent_result"] = "agent_result.json"
    write_launcher(output, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if process.returncode == 0 else 1


def main() -> int:
    import sys
    from isolated_runtime import main as isolated_main
    return isolated_main(sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
