// @vitest-environment happy-dom
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { eq } from "drizzle-orm";

const shared = vi.hoisted(() => {
  process.env.E2E_TEST_BUILD = "true";
  const value = {
    ipcHandlers: new Map<string, (...args: any[]) => any>(),
    ipcListeners: new Map<string, Array<(...args: any[]) => void>>(),
    stopRequested: false,
    runnerCalls: 0,
    previewRunnerCalls: 0,
    agentRunnerCalls: 0,
    runnerOutcomes: [] as string[],
    nativeBrowserExecutions: [] as any[],
    appDirectories: new Map<number, string>(),
    lateCallbackDelivered: false,
  };
  (globalThis as typeof globalThis & { __DYAD_ELECTRON_SHARED__?: unknown })
    .__DYAD_ELECTRON_SHARED__ = value;
  return value;
});

// Keep the product's Tests owner/lock/controller/teardown/report parser real.
// Only offline dependency bootstrap and the external browser process boundary
// are adapted. No predetermined result is returned at the product API layer.
vi.mock("@/ipc/utils/playwright_bootstrap", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/ipc/utils/playwright_bootstrap")>();
  return {...actual, ensurePlaywrightBootstrap: async ({appPath}: {appPath: string}) => {
    const modules = path.join(appPath, "node_modules");
    if (!fs.existsSync(modules)) fs.symlinkSync(process.env.DYAD_BROWSER_NODE_MODULES!, modules, "dir");
    if (!fs.existsSync(path.join(modules, "@playwright/test/package.json")))
      throw new Error("Prepared offline Playwright dependency unavailable");
    return {installed: false, offline_evaluator_dependency_adapter: true};
  }};
});

vi.mock("@/ipc/utils/spawn_streaming", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/ipc/utils/spawn_streaming")>();
  const fixture = JSON.parse(
    fs.readFileSync(process.env.DYAD_SCENARIO_FILE!, "utf8"),
  ) as {
    target: { testFile: string; grep: string };
    fault_injection: {
      delay_ms: number;
      late_callback_after_cancel: boolean;
    };
  };
  return {
    ...actual,
    spawnStreaming: vi.fn(async (options: any) => {
      if (options.command !== "npx" || options.args?.[0] !== "playwright" || options.args?.[1] !== "test")
        return actual.spawnStreaming(options);
      const owner = [...shared.appDirectories].find(([, value]) => path.resolve(value) === path.resolve(options.cwd));
      if (!owner) throw new Error("External browser process has no actual owned app");
      const [appId, appDir] = owner;
      shared.runnerCalls++;
      if (options.args.includes("--headed")) shared.previewRunnerCalls += 1;
      else shared.agentRunnerCalls += 1;
      const grepIndex = options.args.indexOf("-g");
      const requestedGrep = grepIndex >= 0 ? options.args[grepIndex + 1] : fixture.target.grep;
      const expectedSelector = fixture.target.testFile.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (!options.args.includes(expectedSelector) && !options.args.includes("e2e-tests/"))
        throw new Error("Unsupported or mismatching actual browser target selector");
      const { runBehavioralTests } = await import(/* @vite-ignore */ process.env.DYAD_BEHAVIOR_RUNNER!);
      const result = await runBehavioralTests({appId, appDir,
        testFile: fixture.target.testFile, grep: requestedGrep, runOriginalTarget: true,
        runtimeRoot: process.env.DYAD_BROWSER_RUNTIME!, nodeModules: process.env.DYAD_BROWSER_NODE_MODULES!,
        browserExecutable: process.env.DYAD_BROWSER_EXECUTABLE!, signal: options.signal,
        lateCallbackAfterCancel: fixture.fault_injection.late_callback_after_cancel,
        delayBeforeDeliveryMs: fixture.fault_injection.delay_ms,
        timeoutMs: Math.min(options.timeoutMs || 90000, 90000)});
      shared.nativeBrowserExecutions.push(result.native_behavior);
      shared.runnerOutcomes.push(result.results[0].status);
      shared.lateCallbackDelivered ||= result.native_behavior.late_callback_after_cancel === true;
      if (result.native_behavior.infrastructure_invalid)
        throw new Error("Actual Chromium runtime failed before behavior execution");
      // The product parses the untouched actual report using its own parser.
      const report = path.join(appDir, "test-results/results.json");
      fs.mkdirSync(path.dirname(report), {recursive: true});
      fs.copyFileSync(result.native_behavior.actual_report_path, report);
      return {code: result.native_behavior.actual_browser_exit_code, stdout: "Actual Chromium report captured",
        stderr: "", timedOut: result.native_behavior.browser_timeout,
        aborted: Boolean(result.native_behavior.actual_browser_signal) ||
          (!fixture.fault_injection.late_callback_after_cancel && result.native_behavior.signal_aborted_at_delivery)};
    }),
  };
});

import { closeDatabase, initializeDatabase } from "@/db";
import { apps, chats } from "@/db/schema";
import { execFileSync } from "node:child_process";
import { setupHybridChatHarness } from "@/testing/hybrid_chat_harness";
import { createFakeIpcEvent } from "@/testing/electron_mock";
import { runningApps, processCounter } from "@/ipc/utils/process_manager";

type JsonObject = Record<string, any>;
type ActionRecord = {
  sequence: number;
  action: string;
  request: JsonObject;
  request_sha256: string;
  source_message_id: string | number;
  source_message_sha256: string;
  result: unknown;
  dispatch_status: "completed" | "error";
  requested_by_model: true;
  executor: "generic_model_action_dispatch";
  evaluator_defaulted: false;
};

const taskFile = process.env.DYAD_TASK_FILE!;
const scenarioFile = process.env.DYAD_SCENARIO_FILE!;
const artifactFile = process.env.DYAD_ARTIFACT_FILE!;
const evidenceFile = process.env.DYAD_NATIVE_EVIDENCE_FILE!;
const caseId = process.env.DYAD_CASE_ID!;
const brokerEndpoint = process.env.DYAD_BROKER_ENDPOINT || "";
const brokerBaseUrl = brokerEndpoint.replace(/\/responses\/?$/, "");
const task = fs.readFileSync(taskFile, "utf8");
const scenario = JSON.parse(fs.readFileSync(scenarioFile, "utf8")) as JsonObject;
const sha256 = (value: string) =>
  crypto.createHash("sha256").update(value).digest("hex");
const jsonSafe = (value: unknown): unknown =>
  value === undefined ? null : JSON.parse(JSON.stringify(value, (_key, item) =>
    typeof item === "bigint" ? Number(item) : item,
  ));
const canonical = (value: unknown) => JSON.stringify(value, Object.keys(value as object).sort());

function writeEvidence(value: unknown): void {
  fs.mkdirSync(path.dirname(evidenceFile), { recursive: true });
  fs.writeFileSync(evidenceFile, JSON.stringify(value, null, 2) + "\n");
}

function captureModelArtifact(value: unknown): void {
  fs.mkdirSync(path.dirname(artifactFile), { recursive: true });
  fs.writeFileSync(artifactFile, JSON.stringify(value, null, 2) + "\n");
}

function unwrap(value: unknown): unknown {
  const envelope = value as { ok?: boolean; value?: unknown };
  return typeof envelope?.ok === "boolean"
    ? envelope.ok ? envelope.value : null
    : value;
}

function actionMarkers(content: string): RegExpMatchArray[] {
  return [...content.matchAll(
    /<dyad-acceptance-action>([\s\S]*?)<\/dyad-acceptance-action>/g,
  )];
}

// Dyad's local-agent loop keeps appending to the SAME assistant message across
// its internal tool calls, so a model that emits the marker and does not
// immediately receive an observation naturally re-emits it.  Treating that as
// unparsable ended the case at step 0 with no action dispatched and no artifact
// -- an evaluator-side loss of a product the model was driving correctly.  The
// last marker is the model's latest intent, and exactly one action per step is
// still dispatched, so the protocol is unchanged.
function parseAction(content: string): JsonObject | null {
  const matches = actionMarkers(content);
  if (matches.length === 0) return null;
  try {
    const value = JSON.parse(matches[matches.length - 1][1]);
    return value && typeof value === "object" && !Array.isArray(value)
      ? value as JsonObject
      : null;
  } catch {
    return null;
  }
}

function sameId(left: unknown, right: unknown): boolean {
  return (typeof left === "string" || typeof left === "number") &&
    (typeof right === "string" || typeof right === "number") &&
    String(left) === String(right);
}

describe("model-selected Dyad Acceptance scenario", () => {
  let harness: Awaited<ReturnType<typeof setupHybridChatHarness>> | undefined;
  let setupError: unknown;
  // Which half of beforeAll was running when setupError was caught. The
  // catch spans evaluator preparation and the agent loop alike, and only
  // the first half is the evaluator's own fault.
  let setupPhase = "evaluator-preparation";
  const actions: ActionRecord[] = [];
  const ipcEvents: Array<{ channel: string; payload: unknown }> = [];
  const streamEvents: unknown[] = [];
  const streamErrors: unknown[] = [];
  const binding: JsonObject = {
    app_id: null,
    chat_id: null,
    run_id: "",
    session_id: "",
    revision: "",
    target_fingerprint: "",
  };
  let finished = false;
  let malformedAction = false;
  // Bounded record of every re-prompt the driver made and why, so the judge and
  // the Builder see that the evaluator -- not the product -- lost that step.
  const modelReprompts: JsonObject[] = [];
  let lostStartObserved = false;
  let firstStartRequest: JsonObject | null = null;
  let firstStartValue: JsonObject | null = null;
  let originalTarget: string | null = null;
  let latestSnapshotBeforeInvalid: unknown = null;
  let latestSnapshotAfterInvalid: unknown = null;
  // Evaluator-owned truth about the target bytes the product admitted, so a
  // fabricated or constant testFingerprint cannot pass as a real one.
  let targetBytesSha256AtAdmission: string | null = null;
  let snapshotBeforeDenials: unknown = null;
  let deniedRequestPayloads: JsonObject[] = [];
  // Evaluator-owned repeat reads. They spend no model turn and no case budget
  // worth naming: a read that changes what the next identical read returns is a
  // product defect (input/02 "Content addressability of the workspace
  // revision": reading does not change state), and an immutable attestation
  // must read back byte for byte. Only the verdict is kept, never the payload.
  const readStabilityProbes: JsonObject[] = [];
  // input/01 and input/03 item 10: a non-passing attestation may not carry the
  // run to `passed`. Recorded next to each attestation read, again for free.
  const acceptanceGateProbes: JsonObject[] = [];
  let providerFreeProbe: JsonObject | null = null;
  const checkpointFile = process.env.DYAD_RESTART_CHECKPOINT!;
  const resumed = process.env.DYAD_RESTART_EPOCH === "1";
  const checkpoint = resumed ? JSON.parse(fs.readFileSync(checkpointFile, "utf8")) : null;
  let restartRequested = false;
  let coldRestartEvidence: JsonObject | null = null;
  function persistRestart(nextPrompt: string) {
    if (resumed) throw new Error("Only one model-selected cold restart is available per case");
    const value = {case_id: caseId, task_sha256: sha256(task), epoch: 1, previous_pid: process.pid, process_namespace: fs.readlinkSync("/proc/self/ns/pid"),
      nextPrompt, appDir: harness!.appDir, userDataDir: harness!.userDataDir,
      appId: harness!.appId, chatId: harness!.chatId, binding, actions,
      ipcEvents, streamEvents, streamErrors, firstStartRequest, firstStartValue,
      originalTarget, latestSnapshotBeforeInvalid, latestSnapshotAfterInvalid,
      lostStartObserved, providerFreeProbe,
      readStabilityProbes, acceptanceGateProbes,
      targetBytesSha256AtAdmission, snapshotBeforeDenials, deniedRequestPayloads,
      runner: {calls: shared.runnerCalls, preview: shared.previewRunnerCalls, agent: shared.agentRunnerCalls,
        outcomes: shared.runnerOutcomes, browser: shared.nativeBrowserExecutions}};
    fs.writeFileSync(checkpointFile + ".tmp", JSON.stringify(value));
    fs.renameSync(checkpointFile + ".tmp", checkpointFile);
    // Abrupt process exit deliberately skips product teardown. bwrap's PID
    // namespace exits and kills all product/browser descendants. The outside
    // controller starts a fresh namespace and process against these same files.
    process.kill(process.pid, "SIGKILL");
    throw new Error("Self SIGKILL unexpectedly returned");
  }
  async function resumeProduct() {
    if (checkpoint.case_id !== caseId || checkpoint.task_sha256 !== sha256(task))
      throw new Error("Cold restart checkpoint identity mismatch");
    if (checkpoint.process_namespace === fs.readlinkSync("/proc/self/ns/pid")) throw new Error("Cold restart reused old PID namespace");
    process.env.DYAD_DEV_USER_DATA_DIR = checkpoint.userDataDir;
    const { configureTrustedRenderer } = await import("@/ipc/utils/renderer_security");
    configureTrustedRenderer({devServerUrl: "http://localhost:5173", packagedRendererUrl: "file:///app/renderer/main_window/index.html"});
    const {startFakeLlmServer} = await import(/* @vite-ignore */ path.join(process.cwd(), "testing/fake-llm-server/index.ts"));
    process.env.FAKE_LLM_FIXTURES_DIR = path.join(process.cwd(), "e2e-tests/fixtures");
    const fake = await startFakeLlmServer();
    process.env.DYAD_LANGUAGE_MODEL_CATALOG_URL = fake.url + "/api/language-model-catalog";
    process.env.DYAD_USER_INFO_URL = fake.url + "/api/user/info";
    process.env.FAKE_LLM_PORT = String(fake.port);
    if (!brokerEndpoint) {process.env.DYAD_ENGINE_URL = fake.url + "/engine/v1"; process.env.DYAD_GATEWAY_URL = fake.url + "/gateway/v1";}
    initializeDatabase();
    const {db} = await import("@/db");
    const {registerIpcHandlers} = await import("@/ipc/ipc_host");
    registerIpcHandlers();
    const {registerLegacyChatStreamTestHandler} = await import("@/ipc/handlers/chat_stream_handlers");
    registerLegacyChatStreamTestHandler();
    const observedApp = await db.query.apps.findFirst({where: eq(apps.id, checkpoint.appId)});
    const observedChat = await db.query.chats.findFirst({where: eq(chats.id, checkpoint.chatId)});
    if (observedApp?.path !== checkpoint.appDir || observedChat?.appId !== checkpoint.appId)
      throw new Error("Actual product SQLite did not retain restart ownership");
    coldRestartEvidence = {actual_fresh_process: true, previous_process_pid: checkpoint.previous_pid,
      fresh_process_pid: process.pid, previous_namespace: checkpoint.process_namespace, fresh_namespace: fs.readlinkSync("/proc/self/ns/pid"), persisted_app_id: observedApp.id, persisted_chat_id: observedChat.id,
      same_disk_state: true, evaluator_rewrote_product_state: false, automatic_test_run_count_at_start: shared.runnerCalls,
      presentation: "headless main-process registration; not native Electron"};
    const streamChat = async (prompt: string, options: any = {}) => {
      const events: any[] = [];
      const handler = shared.ipcHandlers.get("chat:stream");
      if (!handler) throw new Error("Fresh process lacks actual chat:stream entry");
      const result = await handler(createFakeIpcEvent(events), {chatId: checkpoint.chatId, prompt, ...options});
      return {result, events, messages: await db.query.messages.findMany(),
        eventsFor: (channel: string) => events.filter(e => e.channel === channel)};
    };
    Object.assign(binding, checkpoint.binding); actions.push(...checkpoint.actions);
    ipcEvents.push(...checkpoint.ipcEvents); streamEvents.push(...checkpoint.streamEvents);
    streamErrors.push(...checkpoint.streamErrors); firstStartRequest=checkpoint.firstStartRequest;
    firstStartValue=checkpoint.firstStartValue; originalTarget=checkpoint.originalTarget;
    latestSnapshotBeforeInvalid=checkpoint.latestSnapshotBeforeInvalid;
    latestSnapshotAfterInvalid=checkpoint.latestSnapshotAfterInvalid; lostStartObserved=checkpoint.lostStartObserved;
    providerFreeProbe=checkpoint.providerFreeProbe;
    readStabilityProbes.push(...(checkpoint.readStabilityProbes ?? []));
    acceptanceGateProbes.push(...(checkpoint.acceptanceGateProbes ?? []));
    targetBytesSha256AtAdmission=checkpoint.targetBytesSha256AtAdmission ?? null;
    snapshotBeforeDenials=checkpoint.snapshotBeforeDenials ?? null;
    deniedRequestPayloads=checkpoint.deniedRequestPayloads ?? [];
    shared.runnerCalls=checkpoint.runner.calls; shared.previewRunnerCalls=checkpoint.runner.preview;
    shared.agentRunnerCalls=checkpoint.runner.agent; shared.runnerOutcomes.push(...checkpoint.runner.outcomes);
    shared.nativeBrowserExecutions.push(...checkpoint.runner.browser);
    shared.appDirectories.set(checkpoint.appId, checkpoint.appDir);
    return {db, appDir: checkpoint.appDir, userDataDir: checkpoint.userDataDir, appId: checkpoint.appId,
      chatId: checkpoint.chatId, streamChat,
      gitLog: () => execFileSync("git", ["log", "--oneline"], {cwd: checkpoint.appDir}).toString().trim().split("\n"),
      dispose: async () => {await fake.close(); closeDatabase();}} as any;
  }

  const updateBinding = (value: unknown) => {
    if (!value || typeof value !== "object") return;
    const item = value as JsonObject;
    if (item.appId !== undefined) binding.app_id = item.appId;
    if (item.chatId !== undefined) binding.chat_id = item.chatId;
    if (typeof item.runId === "string") binding.run_id = item.runId;
    if (typeof item.sessionId === "string") binding.session_id = item.sessionId;
    for (const key of ["startedRevision", "workspaceRevision", "currentRevision", "finishedRevision"]) {
      if (typeof item[key] === "string" && item[key]) binding.revision = item[key];
    }
    for (const key of ["testFingerprint", "targetFingerprint"]) {
      if (typeof item[key] === "string" && item[key]) binding.target_fingerprint = item[key];
    }
  };

  const invoke = async (channel: string, payload: unknown): Promise<unknown> => {
    const handler = shared.ipcHandlers.get(channel);
    if (!handler) return { missingHandler: channel };
    const sink: Array<{ channel: string; payload: unknown }> = [];
    const result = await handler(createFakeIpcEvent(sink), payload);
    ipcEvents.push(...sink.map((event) => ({
      channel: event.channel,
      payload: jsonSafe(event.payload),
    })));
    updateBinding(unwrap(result));
    for (const event of sink) updateBinding(event.payload);
    return jsonSafe(result);
  };

  // A repeat read taken by the evaluator. It is deliberately NOT `invoke`: it
  // must not move `binding`, must not enter the ipc event record and must not
  // reach the model, so that the only thing it can ever prove is whether the
  // product returned the same bytes twice.
  const probeRead = async (channel: string, payload: unknown): Promise<unknown> => {
    const handler = shared.ipcHandlers.get(channel);
    if (!handler) return { missingHandler: channel };
    const sink: Array<{ channel: string; payload: unknown }> = [];
    try {
      return jsonSafe(await handler(createFakeIpcEvent(sink), payload));
    } catch (error) {
      return { probe_error: error instanceof Error ? error.message : String(error) };
    }
  };

  const TERMINAL_PREVIEW_STATES = new Set([
    "passed", "failed", "infrastructure", "cancelled", "invalidated"]);

  // Does the run snapshot present `attestationId` as evidence that something
  // passed?  Layout-agnostic on purpose: the product owns its snapshot shape,
  // so this looks for any object that carries the identifier and, beside it, a
  // passing verdict.
  const citesAsPassing = (snapshot: unknown, attestationId: string): boolean => {
    if (!attestationId) return false;
    let found = false;
    const visit = (value: unknown): void => {
      if (found || !value || typeof value !== "object") return;
      if (Array.isArray(value)) { value.forEach(visit); return; }
      const item = value as JsonObject;
      const carries = Object.values(item).some((entry) => entry === attestationId);
      if (carries) {
        const passing = item.passed === true || item.passing === true ||
          ["status", "outcome", "result", "verdict"].some((key) =>
            typeof item[key] === "string" && String(item[key]).toLowerCase() === "passed");
        if (passing) { found = true; return; }
      }
      Object.values(item).forEach(visit);
    };
    visit(snapshot);
    return found;
  };

  const requireCurrentOwnership = (request: JsonObject) => {
    if (!sameId(request.appId, binding.app_id) || !sameId(request.chatId, binding.chat_id)) {
      throw new Error("model action did not use the current product app/chat IDs");
    }
  };

  // The authorized snapshot immediately before the first denied request, plus
  // the exact payloads the model chose, so the evaluator can check that the
  // product recorded the denial without echoing foreign identity back.
  const captureDenialBaseline = async (request: JsonObject): Promise<void> => {
    deniedRequestPayloads.push(JSON.parse(JSON.stringify(request)));
    if (snapshotBeforeDenials !== null) return;
    snapshotBeforeDenials = unwrap(await invoke("acceptance:get-latest-run", {
      appId: harness!.appId,
      chatId: harness!.chatId,
    }));
  };

  const executeModelAction = async (request: JsonObject): Promise<unknown> => {
    const action = request.action;
    if (typeof action !== "string") throw new Error("model action name is missing");
    switch (action) {
      case "inspect_latest": {
        const result = await invoke("acceptance:get-latest-run", {
          appId: harness!.appId,
          chatId: harness!.chatId,
        });
        binding.app_id = harness!.appId;
        binding.chat_id = harness!.chatId;
        latestSnapshotBeforeInvalid = unwrap(result);
        return result;
      }
      case "start_preview":
      case "retry_preview": {
        const command = request.command;
        if (!command || typeof command !== "object") throw new Error("preview command is missing");
        requireCurrentOwnership(command);
        if (command.runId !== binding.run_id) throw new Error("preview runId is not current");
        if (canonical(command.target) !== canonical(scenario.target)) throw new Error("preview target differs from the run-local target");
        if (targetBytesSha256AtAdmission === null) {
          // Read the exact bytes the product is about to fingerprint. This is an
          // observation only: nothing is written and the product is not told.
          try {
            // Raw bytes, no encoding round-trip: the published contract defines
            // testFingerprint as SHA-256 over the admitted target file bytes.
            targetBytesSha256AtAdmission = crypto.createHash("sha256")
              .update(fs.readFileSync(path.join(harness!.appDir, scenario.target.testFile)))
              .digest("hex");
          } catch {
            targetBytesSha256AtAdmission = null;
          }
        }
        if (action === "start_preview") {
          if (firstStartRequest !== null) throw new Error("start_preview may be selected only once; use retry_preview");
          firstStartRequest = JSON.parse(JSON.stringify(command));
        } else if (firstStartRequest === null || JSON.stringify(command) !== JSON.stringify(firstStartRequest)) {
          throw new Error("retry_preview must replay the byte-equivalent original command");
        }
        const result = await invoke("acceptance:start-preview", command);
        const value = unwrap(result) as JsonObject | null;
        if (value) {
          updateBinding(value);
          if (!firstStartValue) firstStartValue = value;
        }
        if (action === "start_preview" && scenario.fault_injection.lose_first_start_response === true) {
          lostStartObserved = true;
          return {
            delivery: "lost_after_product_acceptance",
            product_action_accepted: Boolean(value?.sessionId),
            operationId: command.operationId,
            sessionIdWithheld: true,
          };
        }
        return result;
      }
      case "conflict_start": {
        const command = request.command;
        if (!command || typeof command !== "object") throw new Error("conflict probe command is missing");
        requireCurrentOwnership(command);
        if (command.runId !== binding.run_id) throw new Error("conflict probe runId is not current");
        if (firstStartRequest === null) throw new Error("there is no original preview start to conflict with");
        if (command.operationId !== firstStartRequest.operationId) {
          throw new Error("conflict probe must reuse the original caller-stable operationId");
        }
        if (canonical(command.target) === canonical(scenario.target)) {
          throw new Error("conflict probe target must differ from the original command target");
        }
        const bindingBefore = JSON.parse(JSON.stringify(binding));
        const runnerBefore = shared.previewRunnerCalls;
        const envelope = await invoke("acceptance:start-preview", command);
        const bindingAfter = JSON.parse(JSON.stringify(binding));
        // The legitimate session stays authoritative for later reads; whether the
        // product tried to rebind is recorded as a fact instead.
        Object.assign(binding, bindingBefore);
        return {
          conflict_probe: true,
          envelope: jsonSafe(envelope),
          runner_calls_before: runnerBefore,
          runner_calls_after: shared.previewRunnerCalls,
          rebound: JSON.stringify(bindingAfter) !== JSON.stringify(bindingBefore),
        };
      }
      case "get_preview": {
        requireCurrentOwnership(request);
        if (request.sessionId !== binding.session_id) throw new Error("preview read did not use the observed sessionId");
        const payload = {
          appId: request.appId,
          chatId: request.chatId,
          sessionId: request.sessionId,
        };
        const result = await invoke("acceptance:get-preview", payload);
        const value = unwrap(result) as JsonObject | null;
        const status = String(value?.status ?? "");
        if (value && TERMINAL_PREVIEW_STATES.has(status)) {
          const again = await probeRead("acceptance:get-preview", payload);
          readStabilityProbes.push({
            kind: "terminal_preview",
            sequence: actions.length + 1,
            status,
            stable: JSON.stringify(again) === JSON.stringify(result),
          });
        }
        return result;
      }
      case "get_run": {
        if (!sameId(request.appId, binding.app_id) || request.runId !== binding.run_id) {
          throw new Error("run read did not use the observed owner/run IDs");
        }
        return invoke("acceptance:get-run", { appId: request.appId, runId: request.runId });
      }
      case "get_attestation": {
        requireCurrentOwnership(request);
        if (typeof request.attestationId !== "string" || !request.attestationId) {
          throw new Error("attestationId is missing");
        }
        // `request` is the model's RAW action object: the run-local marker
        // protocol requires it to carry `action`, and models routinely repeat
        // the owner IDs they already know.  The product's input schema is
        // strict, so forwarding it verbatim made every get_attestation read
        // fail with `Unrecognized key: "action"` -- for any product, in every
        // case, with no repair available to the model.  Build the same clean
        // payload `get_preview` (above) and `get_run` (below) already build.
        const payload = {
          appId: request.appId,
          chatId: request.chatId,
          attestationId: request.attestationId,
        };
        const result = await invoke("acceptance:get-attestation", payload);
        const value = unwrap(result) as JsonObject | null;
        if (value && typeof value === "object") {
          const again = await probeRead("acceptance:get-attestation", payload);
          readStabilityProbes.push({
            kind: "attestation",
            sequence: actions.length + 1,
            status: String(value.outcome ?? ""),
            stable: JSON.stringify(again) === JSON.stringify(result),
          });
          const gate = await probeRead("acceptance:get-run", {
            appId: request.appId, runId: binding.run_id,
          });
          // Only a successful typed envelope counts as a readable snapshot: a
          // missing handler or a throwing read is an unmet obligation, not a
          // free pass.
          const typedGate = gate as { ok?: boolean; value?: unknown } | null;
          const snapshot = typedGate && typedGate.ok === true ? typedGate.value : null;
          const readable = Boolean(snapshot && typeof snapshot === "object" && !Array.isArray(snapshot));
          acceptanceGateProbes.push({
            sequence: actions.length + 1,
            attestation_outcome: String(value.outcome ?? ""),
            attestation_identity_present: typeof value.attestationId === "string" && Boolean(value.attestationId),
            run_snapshot_readable: readable,
            run_status: readable ? String((snapshot as JsonObject).status ?? "") : null,
            passing_evidence_cites_attestation: readable
              ? citesAsPassing(snapshot, String(value.attestationId ?? ""))
              : null,
          });
        }
        return result;
      }
      case "stale_control":
      case "foreign_control": {
        if (!request.command || typeof request.command !== "object") throw new Error("control command is missing");
        await captureDenialBaseline(request);
        const result = await invoke("acceptance:control-run", request.command);
        latestSnapshotAfterInvalid = await invoke("acceptance:get-latest-run", {
          appId: harness!.appId,
          chatId: harness!.chatId,
        });
        return result;
      }
      case "foreign_read": {
        if (typeof request.channel !== "string" || !request.payload) throw new Error("foreign read channel/payload is missing");
        if (!new Set(["acceptance:get-run", "acceptance:get-preview", "acceptance:get-attestation"]).has(request.channel)) {
          throw new Error("foreign read channel is not permitted by the generic dispatcher");
        }
        await captureDenialBaseline(request);
        const result = await invoke(request.channel, request.payload);
        latestSnapshotAfterInvalid = await invoke("acceptance:get-latest-run", {
          appId: harness!.appId,
          chatId: harness!.chatId,
        });
        return result;
      }
      case "mutate_target": {
        if (request.testFile !== scenario.target.testFile) throw new Error("mutation target differs from the run-local target");
        const target = path.join(harness!.appDir, request.testFile);
        originalTarget = fs.readFileSync(target, "utf8");
        fs.appendFileSync(target, `\n// model-selected drift ${scenario.operation_id}\n`, "utf8");
        return { mutated: true, testFile: request.testFile };
      }
      case "restore_target": {
        if (request.testFile !== scenario.target.testFile || originalTarget === null) {
          throw new Error("no matching model-selected target mutation can be restored");
        }
        fs.writeFileSync(path.join(harness!.appDir, request.testFile), originalTarget, "utf8");
        return { restored: true, testFile: request.testFile };
      }
      case "stop_tests":
      case "duplicate_stop": {
        if (!sameId(request.appId, binding.app_id)) throw new Error("stop did not use the current appId");
        shared.stopRequested = true;
        return invoke("tests:stop", { appId: request.appId });
      }
      case "restart_state": {
        if (resumed) throw new Error("Cold restart already consumed");
        restartRequested = true;
        return { restart_requested: true, same_disk_state: true, automaticRerunRequested: false };
      }
      case "compatibility_chat": {
        const compatibility = await harness!.streamChat(
          "Check that ordinary Dyad chat remains readable. Do not alter prior Acceptance evidence.",
          { requestedChatMode: "build" as never },
        );
        streamEvents.push(...compatibility.events.map((event) => jsonSafe(event)));
        return {
          streamEnd: compatibility.eventsFor("chat:stream:end").length,
          responseErrors: compatibility.eventsFor("chat:response:error").length,
        };
      }
      case "finish": {
        if (!request.artifact || typeof request.artifact !== "object" || Array.isArray(request.artifact)) {
          throw new Error("finish requires a model-authored artifact object");
        }
        captureModelArtifact(request.artifact);
        finished = true;
        return { captured: true, artifactSha256: sha256(JSON.stringify(request.artifact)) };
      }
      default:
        throw new Error(`unsupported model-selected action: ${action}`);
    }
  };

  beforeAll(async () => {
    try {
      if (brokerEndpoint) {
        process.env.DYAD_ENGINE_URL = brokerBaseUrl;
        process.env.DYAD_PRO_API_KEY = "broker-only-placeholder";
        process.env.OPENAI_API_KEY = "broker-only-placeholder";
      }
      if (resumed) {
        harness = await resumeProduct();
      } else {
      harness = await setupHybridChatHarness({
        electronMock: shared,
        chatMode: (process.env.DYAD_PROVIDER_FREE_BROWSER_PROBE ? "build" : "acceptance") as never,
        autoApprove: true,
        selectedModel: { provider: "openai", name: "gpt-5.6-sol" },
        engine: !brokerEndpoint,
        testBuild: true,
        assertNoMissingChannels: false,
        settings: {
          isTestMode: true,
          enableDyadPro: Boolean(brokerEndpoint),
          enableCodeExplorer: false,
          selectedChatMode: (process.env.DYAD_PROVIDER_FREE_BROWSER_PROBE ? "build" : "acceptance") as never,
          defaultChatMode: (process.env.DYAD_PROVIDER_FREE_BROWSER_PROBE ? "build" : "acceptance") as never,
          providerSettings: brokerEndpoint
            ? { auto: { apiKey: { value: "broker-only-placeholder" } } }
            : {},
        },
      });
      await harness.db.update(apps).set({ testingEnabled: true }).where(eq(apps.id, harness.appId));
      await harness.db.update(chats).set({ chatMode: "acceptance" as never }).where(eq(chats.id, harness.chatId));
      shared.appDirectories.set(harness.appId, harness.appDir);
      // The accepted candidate is frozen to 0444 before the hidden phase, and the
      // product harness copies its fixture app with fs.cpSync, which preserves
      // mode. The case then runs under bwrap with --cap-drop ALL -- measured
      // uid=0 CapEff=0000000000000000 -- so there is no CAP_DAC_OVERRIDE and
      // 0444 is genuinely unwritable. Every write into the scratch app then
      // fails: this scenario's own initial_files first, and the agent's repair
      // edits after that. Four of four runs that reached hidden died here with
      // calls == 0, reported as agent_no_model_call.
      // Only the per-case scratch copy is touched, and only by adding the owner
      // write bit back, so the fixture keeps every other permission it carries.
      // The frozen artifact is untouched and bwrap still mounts the repository
      // read-only.
      const restoreScratchWrite = (directory: string): void => {
        for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
          if (entry.isSymbolicLink()) continue;
          const full = path.join(directory, entry.name);
          fs.chmodSync(full, fs.statSync(full).mode | (entry.isDirectory() ? 0o300 : 0o200));
          if (entry.isDirectory()) restoreScratchWrite(full);
        }
      };
      fs.chmodSync(harness.appDir, fs.statSync(harness.appDir).mode | 0o300);
      restoreScratchWrite(harness.appDir);
      for (const [name, contents] of Object.entries(scenario.public_app.initial_files)) {
        const file = path.resolve(harness.appDir, name);
        if (!file.startsWith(path.resolve(harness.appDir) + path.sep)) throw new Error("Initial app file leaves owned app");
        fs.mkdirSync(path.dirname(file), {recursive: true});
        fs.writeFileSync(file, String(contents), "utf8");
      }
      const targetPath = path.join(harness.appDir, scenario.target.testFile);
      fs.mkdirSync(path.dirname(targetPath), { recursive: true });
      fs.writeFileSync(targetPath, scenario.public_app.initial_test, "utf8");
      const {startAppServer} = await import(/* @vite-ignore */ process.env.DYAD_BEHAVIOR_RUNNER!);
      const initialAppServer = await startAppServer(harness.appDir, process.env.DYAD_BROWSER_RUNTIME!);
      runningApps.set(harness.appId, {process: initialAppServer.child, processId: processCounter.increment(),
        mode: "host", lastViewedAt: Date.now(), proxyUrl: initialAppServer.baseUrl, originalUrl: initialAppServer.baseUrl});

      if (process.env.DYAD_PROVIDER_FREE_BROWSER_PROBE) {
        // Diagnostic-only, explicitly not agent execution or acceptance. It
        // exercises the Candidate's actual Tests owner, lock and Stop handler.
        const {runAppTestsWithIsolation} = await import("@/ipc/handlers/tests_handlers");
        const sink: Array<{channel: string; payload: unknown}> = [];
        const runOptions = {appId: harness.appId,
          event: createFakeIpcEvent(sink), source: "panel", headed: true,
          testFile: scenario.target.testFile, grep: scenario.target.grep, timeoutMs: 90000};
        const run = runAppTestsWithIsolation(runOptions as never);
        let stopReceipt: unknown = null;
        let completedReportBeforeStop = false;
        let priorRunResult: unknown = null;
        let successorRun: Promise<unknown> | null = null;
        const awaitReportCount = async (count: number) => {
          const until = Date.now() + 25000;
          while (Date.now() < until) {
            const runs = fs.readdirSync(process.env.DYAD_BROWSER_RUNTIME!, {withFileTypes: true})
              .filter(entry => entry.isDirectory());
            const completed = runs.filter(entry => fs.existsSync(path.join(
              process.env.DYAD_BROWSER_RUNTIME!, entry.name, "playwright-report.json"))).length;
            if (completed >= count) return true;
            await new Promise(resolve => setTimeout(resolve, 50));
          }
          return false;
        };
        if (caseId === "dev_002") {
          if (!await awaitReportCount(1)) throw new Error("First actual browser report did not complete");
          successorRun = runAppTestsWithIsolation(runOptions as never);
          priorRunResult = await run;
          completedReportBeforeStop = await awaitReportCount(2);
          stopReceipt = await invoke("tests:stop", {appId: harness.appId});
        } else if (caseId === "test_005") {
          completedReportBeforeStop = await awaitReportCount(1);
          stopReceipt = await invoke("tests:stop", {appId: harness.appId});
        }
        const result = await (successorRun || run);
        providerFreeProbe = {provider_calls: 0, model_actions: 0, benchmark_acceptance_claimed: false,
          real_product_Tests_result: jsonSafe(result), actual_product_stop_receipt: stopReceipt,
          prior_run_result: jsonSafe(priorRunResult),
          actual_report_before_stop: completedReportBeforeStop,
          tests_owner_scope: "unmodified Candidate runAppTestsWithIsolation and registered tests:stop",
          browser_adapter_scope: "offline bootstrap and external Playwright process only",
          run_state_events: jsonSafe(sink),
          private_fixture_module_visible: [
            '@@AGENTSWE_EDITING_TASKS@@/dyad-acceptance-driven/tree',
            '@@AGENTSWE_LEGACY_DATA@@/0919-hardening/19-edit-dyad-acceptance-driven-agentloop-v1',
          ].some((root) => fs.existsSync(path.join(root, 'evaluator/behavior_worlds.py'))),
          credential_file_visible: fs.existsSync('@@AGENTSWE_CREDENTIAL_FILE@@')};
        if (caseId === "test_006") persistRestart("provider-free cold-restart diagnostic");
        return;
      }

      } // initial product setup; resumed process reuses persisted app and DB
      if (resumed && process.env.DYAD_PROVIDER_FREE_BROWSER_PROBE) {
        providerFreeProbe!.cold_restart = coldRestartEvidence;
        return;
      }
      setupPhase = "driving-candidate";
      // Two bounded recoveries, each used at most once per process epoch.  Both
      // spend one of the SAME 20 loop steps, so the per-case turn budget is
      // exactly what it was.
      //  - transport: a `chat:response:error` (the resumed epoch's blocked
      //    provider fetch) persists an EMPTY assistant message.  That is an
      //    evaluator/provider fault; re-send the identical prompt once.
      //  - format: no dispatchable marker.  Tell the model exactly what was
      //    wrong, in a bounded message that discloses nothing about the case,
      //    instead of ending the rollout silently.
      const MAX_TRANSPORT_RETRIES = 1;
      const MAX_FORMAT_REPROMPTS = 1;
      let transportRetries = 0;
      let formatReprompts = 0;
      let prompt = resumed ? checkpoint.nextPrompt : task;
      for (let step = actions.length; step < 20 && !finished; step += 1) {
        const beforeMessages = await harness.db.query.messages.findMany();
        const streamed = await harness.streamChat(prompt, {
          requestedChatMode: "acceptance" as never,
        });
        streamEvents.push(...streamed.events.map((event) => jsonSafe(event)));
        const stepStreamErrors = streamed.eventsFor("chat:response:error").map((event) => jsonSafe(event));
        streamErrors.push(...stepStreamErrors);
        const messages = await harness.db.query.messages.findMany();
        const newAssistant = messages
          .slice(beforeMessages.length)
          .filter((message) => message.role === "assistant")
          .at(-1);
        const assistantContent = String(newAssistant?.content ?? "");
        const action = parseAction(assistantContent);
        if (!action) {
          const markerCount = actionMarkers(assistantContent).length;
          const transportFailed = stepStreamErrors.length > 0 || assistantContent.trim() === "";
          if (transportFailed && transportRetries < MAX_TRANSPORT_RETRIES) {
            transportRetries += 1;
            modelReprompts.push({
              step, kind: "model_response_error", owner: "evaluator/provider",
              stream_errors: stepStreamErrors.length, marker_count: markerCount,
              assistant_content_length: assistantContent.length,
              recovery: "identical prompt re-sent once",
            });
            continue;
          }
          if (!transportFailed && formatReprompts < MAX_FORMAT_REPROMPTS) {
            formatReprompts += 1;
            modelReprompts.push({
              step, kind: "unparsable_action_marker", owner: "lower-agent-format",
              stream_errors: stepStreamErrors.length, marker_count: markerCount,
              assistant_content_length: assistantContent.length,
              recovery: "bounded protocol reminder sent once",
            });
            prompt =
              "Your last response did not carry a product action this evaluator could dispatch " +
              "(documented action markers found: " + markerCount + "). Nothing was sent to the " +
              "product and the case state is unchanged.\n\nReply once more with exactly one " +
              "<dyad-acceptance-action>{\"action\":\"...\"}</dyad-acceptance-action> marker whose " +
              "content is a single JSON object naming your next product action; if more than one " +
              "marker is present only the last is dispatched. This reminder is sent only once.";
            continue;
          }
          malformedAction = true;
          break;
        }
        let actionResult: unknown;
        try {
          actionResult = await executeModelAction(action);
        } catch (error) {
          actionResult = {
            action_error: error instanceof Error ? error.message : String(error),
          };
        }
        const record: ActionRecord = {
          sequence: actions.length + 1,
          action: String(action.action),
          request: jsonSafe(action) as JsonObject,
          request_sha256: sha256(JSON.stringify(action)),
          source_message_id: newAssistant!.id,
          source_message_sha256: sha256(assistantContent),
          result: jsonSafe(actionResult),
          dispatch_status:
            actionResult &&
            typeof actionResult === "object" &&
            "action_error" in actionResult
              ? "error"
              : "completed",
          requested_by_model: true,
          executor: "generic_model_action_dispatch",
          evaluator_defaulted: false,
        };
        actions.push(record);
        if (record.action === "finish") break;
        prompt =
          "The product executed exactly your selected action. Here is the bounded returned observation:\n\n" +
          JSON.stringify({
            case_id: caseId,
            scenario_id: scenario.scenario_id,
            executed_task_sha256: scenario.executed_task_sha256,
            action: record.action,
            result: record.result,
            current_binding: binding,
            emitted_events: ipcEvents.slice(-12),
            runner_calls: shared.runnerCalls,
            runner_outcomes: shared.runnerOutcomes,
          }, null, 2) +
          "\n\nContinue the original task. Use normal Dyad tools as needed, then select exactly one next product action with the documented marker.";
        if (restartRequested) persistRestart(prompt);
      }
    } catch (error) {
      setupError = error;
    }
  }, 600_000);

  afterAll(async () => {
    if (harness) {
      const finalApp = process.env.DYAD_FINAL_APP_SNAPSHOT!;
      fs.cpSync(harness.appDir, finalApp, {recursive: true, dereference: false,
        filter: (source) => !path.relative(harness!.appDir, source).split(path.sep).some(part => ["node_modules", ".git"].includes(part))});
    }
    const finalWorkspace: {target_unchanged: boolean | null; changed_initial_files: string[]} = {
      target_unchanged: null,
      changed_initial_files: [],
    };
    if (harness) {
      try {
        finalWorkspace.target_unchanged = fs.readFileSync(
          path.join(harness.appDir, scenario.target.testFile), "utf8",
        ) === String(scenario.public_app.initial_test);
        finalWorkspace.changed_initial_files = Object.entries(scenario.public_app.initial_files)
          .filter(([name, contents]) => {
            try {
              return fs.readFileSync(path.join(harness!.appDir, name), "utf8") !== String(contents);
            } catch {
              return true;
            }
          })
          .map(([name]) => name);
      } catch {
        finalWorkspace.target_unchanged = null;
      }
    }
    const messages = harness ? await harness.db.query.messages.findMany().catch(() => []) : [];
    const gitLog = harness?.gitLog() ?? [];
    const finalPreview = actions
      .filter((item) => item.action === "get_preview")
      .map((item) => unwrap(item.result))
      .filter((item): item is JsonObject => Boolean(item && typeof item === "object"))
      .at(-1);
    const invalidResults = actions.filter((item) =>
      ["stale_control", "foreign_control", "foreign_read"].includes(item.action),
    );
    const okValue = (record: ActionRecord | undefined): JsonObject | null => {
      const envelope = record?.result as JsonObject | undefined;
      if (!envelope || typeof envelope !== "object" || envelope.ok !== true) return null;
      const value = unwrap(envelope);
      return value && typeof value === "object" ? (value as JsonObject) : null;
    };
    const valuesFor = (name: string) =>
      actions.filter((item) => item.action === name).map(okValue).filter((item): item is JsonObject => item !== null);
    const hex64 = (value: unknown) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
    const previewValues = valuesFor("get_preview");
    const attestationValues = valuesFor("get_attestation");
    const attestationIds = [...new Set(attestationValues
      .map((item) => item.attestationId)
      .filter((item) => typeof item === "string" && item))];
    const finalAttestation = attestationValues.at(-1);
    const actionIndex = (name: string) => actions.findIndex((item) => item.action === name);
    const previewValuesAround = (name: string) => {
      const pivot = actionIndex(name);
      if (pivot < 0) return {before: [] as JsonObject[], after: [] as JsonObject[]};
      const before = actions.slice(0, pivot).filter((i) => i.action === "get_preview").map(okValue)
        .filter((i): i is JsonObject => i !== null);
      const after = actions.slice(pivot).filter((i) => i.action === "get_preview").map(okValue)
        .filter((i): i is JsonObject => i !== null);
      return {before, after};
    };
    const attestationsAround = (name: string) => {
      const pivot = actionIndex(name);
      if (pivot < 0) return {before: [] as JsonObject[], after: [] as JsonObject[]};
      const before = actions.slice(0, pivot).filter((i) => i.action === "get_attestation").map(okValue)
        .filter((i): i is JsonObject => i !== null);
      const after = actions.slice(pivot).filter((i) => i.action === "get_attestation").map(okValue)
        .filter((i): i is JsonObject => i !== null);
      return {before, after};
    };
    const boundedReason = (value: unknown) =>
      typeof value === "string" && value.trim().length > 0 && value.length <= 200 &&
      !value.includes("/home/") && !value.includes("/data/") &&
      !(harness ? value.includes(harness.appDir) : false);
    const scenarioChecks: JsonObject = {};
    if (caseId === "test_001") {
      const failed = shared.runnerOutcomes.indexOf("failed");
      const passed = shared.runnerOutcomes.indexOf("passed");
      const restart = attestationsAround("restart_state");
      scenarioChecks.failed_before_passed = failed >= 0 && passed > failed;
      scenarioChecks.repair_kept_target_and_scope =
        finalWorkspace.target_unchanged === true &&
        finalWorkspace.changed_initial_files.length >= 1 &&
        finalWorkspace.changed_initial_files.length <= 2;
      scenarioChecks.terminal_passed = finalPreview?.status === "passed";
      scenarioChecks.fingerprint_matches_target_bytes =
        hex64(targetBytesSha256AtAdmission) &&
        finalPreview?.testFingerprint === targetBytesSha256AtAdmission;
      scenarioChecks.clean_run_revisions_pinned =
        hex64(finalPreview?.startedRevision) &&
        finalPreview?.currentRevision === finalPreview?.startedRevision;
      scenarioChecks.attestation_binds_session = Boolean(finalAttestation) &&
        finalAttestation!.sessionId === finalPreview?.sessionId &&
        finalAttestation!.outcome === "passed" &&
        finalAttestation!.testFingerprint === finalPreview?.testFingerprint &&
        finalAttestation!.startedRevision === finalPreview?.startedRevision &&
        finalAttestation!.finishedRevision === finalPreview?.startedRevision;
      scenarioChecks.attestation_stable_after_restart =
        actionIndex("restart_state") >= 0 && restart.before.length > 0 && restart.after.length > 0 &&
        JSON.stringify(restart.before.at(-1)) === JSON.stringify(restart.after.at(-1)) &&
        shared.previewRunnerCalls === 1;
    } else if (caseId === "test_002") {
      const retry = actions.find((item) => item.action === "retry_preview");
      const conflict = actions.find((item) => item.action === "conflict_start");
      const conflictValue = (conflict?.result ?? null) as JsonObject | null;
      const conflictEnvelope = conflictValue?.envelope as JsonObject | undefined;
      const conflictInner = conflictEnvelope ? unwrap(conflictEnvelope) as JsonObject | null : null;
      const around = previewValuesAround("conflict_start");
      scenarioChecks.lost_response_observed = lostStartObserved;
      scenarioChecks.retry_replayed_original = Boolean(retry && firstStartRequest &&
        JSON.stringify(retry.request.command) === JSON.stringify(firstStartRequest));
      scenarioChecks.single_runner_invocation = shared.previewRunnerCalls === 1;
      scenarioChecks.same_session_recovered = Boolean(firstStartValue?.sessionId && binding.session_id === firstStartValue.sessionId);
      scenarioChecks.conflicting_operation_identity_rejected = Boolean(conflictEnvelope) && (
        conflictEnvelope!.ok === false || conflictInner === null ||
        conflictInner.applied === false || conflictInner.accepted === false ||
        ["rejected", "conflict", "stale", "unauthorized", "busy"].includes(String(conflictInner.status)));
      scenarioChecks.conflict_started_no_second_run = Boolean(conflictValue) &&
        conflictValue!.rebound === false &&
        conflictValue!.runner_calls_after === conflictValue!.runner_calls_before &&
        shared.previewRunnerCalls === 1 &&
        around.before.length > 0 && around.after.length > 0 &&
        around.after.at(-1)!.sessionId === around.before.at(-1)!.sessionId &&
        Number(around.after.at(-1)!.lastEventSequence ?? -1) >= Number(around.before.at(-1)!.lastEventSequence ?? 0);
      scenarioChecks.single_attestation_identity = attestationIds.length === 1;
    } else if (caseId === "test_003") {
      const denied = actions.filter((item) =>
        ["stale_control", "foreign_control", "foreign_read"].includes(item.action));
      const before = (snapshotBeforeDenials ?? null) as JsonObject | null;
      const after = (unwrap(latestSnapshotAfterInvalid) ?? null) as JsonObject | null;
      const beforeLedger = Array.isArray(before?.deniedRequests) ? before!.deniedRequests as JsonObject[] : [];
      const afterLedger = Array.isArray(after?.deniedRequests) ? after!.deniedRequests as JsonObject[] : [];
      const newLedger = afterLedger.slice(beforeLedger.length);
      const foreignStrings = new Set<string>();
      const collect = (value: unknown) => {
        if (typeof value === "string" && value.length >= 8) foreignStrings.add(value);
        else if (Array.isArray(value)) value.forEach(collect);
        else if (value && typeof value === "object") Object.values(value as JsonObject).forEach(collect);
      };
      deniedRequestPayloads.forEach(collect);
      for (const own of [binding.run_id, binding.session_id, scenario.operation_id,
        scenario.target.testFile, scenario.target.grep, "acceptance:get-run",
        "acceptance:get-preview", "acceptance:get-attestation"]) {
        if (typeof own === "string") foreignStrings.delete(own);
      }
      const ledgerText = JSON.stringify(newLedger);
      scenarioChecks.invalid_requests_rejected = denied.length === 3 && denied.every((item) => {
        if (item.action === "foreign_read") return unwrap(item.result) == null;
        const result = item.result as JsonObject | null;
        const value = unwrap(item.result) as JsonObject | null;
        return item.result === null || result?.ok === false || Boolean(result?.error) ||
          value?.applied === false || ["rejected", "stale", "conflict", "unauthorized"].includes(String(value?.status));
      });
      // Compare exactly the control-plane facts a denied request must never
      // move.  Whole-snapshot equality would also fail when the authorized
      // preview legitimately advances between the two reads, which is product
      // progress, not a denial side effect.
      scenarioChecks.authorized_state_preserved = before !== null && after !== null &&
        before.runId === after.runId &&
        Number(after.generation ?? -1) === Number(before.generation ?? -2) &&
        Number(after.controlSequence ?? -1) === Number(before.controlSequence ?? -2) &&
        ["requirements", "evidence", "testRuns"].every((key) =>
          !Array.isArray(before[key]) ||
          (Array.isArray(after[key]) && after[key].length >= before[key].length));
      scenarioChecks.denial_ledger_auditable = Array.isArray(after?.deniedRequests) &&
        newLedger.length === 3 && newLedger.every((entry) =>
          Boolean(entry) && typeof entry === "object" && !Array.isArray(entry) &&
          typeof entry.classification === "string" && entry.classification.trim().length > 0 &&
          boundedReason(entry.reason) && typeof entry.at === "number") &&
        ![...foreignStrings].some((value) => ledgerText.includes(value));
      scenarioChecks.denials_disclosed_nothing = denied.length === 3 && denied.every((item) => {
        const value = unwrap(item.result) as JsonObject | null;
        if (value === null) return true;
        if (typeof value !== "object") return false;
        return !["requirements", "evidence", "testRuns", "sessionId", "attestationId", "deniedRequests"]
          .some((key) => key in value);
      });
      scenarioChecks.authorized_session_unaffected = Boolean(firstStartValue?.sessionId) &&
        previewValues.length > 0 && previewValues.at(-1)!.sessionId === firstStartValue!.sessionId;
    } else if (caseId === "test_004") {
      const around = previewValuesAround("mutate_target");
      const postMutation = around.after.at(0) ?? null;
      scenarioChecks.terminal_invalidated = finalPreview?.status === "invalidated";
      scenarioChecks.invalidation_reason_bounded = boundedReason(finalPreview?.invalidationReason);
      scenarioChecks.drift_moved_finished_revision = Boolean(finalAttestation) &&
        hex64(finalAttestation!.startedRevision) && hex64(finalAttestation!.finishedRevision) &&
        finalAttestation!.finishedRevision !== finalAttestation!.startedRevision;
      scenarioChecks.green_runner_result_refused = !shared.runnerOutcomes.includes("passed") ||
        (finalPreview?.status === "invalidated" && finalAttestation?.outcome === "invalidated");
      scenarioChecks.attestation_invalidated = Boolean(finalAttestation) &&
        finalAttestation!.outcome === "invalidated" &&
        finalAttestation!.sessionId === finalPreview?.sessionId;
      scenarioChecks.restoration_did_not_resurrect = finalPreview?.status === "invalidated" &&
        shared.previewRunnerCalls === 1 && attestationIds.length === 1 &&
        Boolean(postMutation) && postMutation!.sessionId === finalPreview?.sessionId;
    } else if (caseId === "test_005") {
      const around = previewValuesAround("duplicate_stop");
      scenarioChecks.cancelled_is_terminal = finalPreview?.status === "cancelled";
      scenarioChecks.late_callback_observed = shared.lateCallbackDelivered;
      scenarioChecks.single_runner_invocation = shared.previewRunnerCalls === 1;
      scenarioChecks.terminal_not_overwritten = around.before.length > 0 && around.after.length > 0 &&
        around.after.at(-1)!.status === "cancelled" &&
        around.after.at(-1)!.sessionId === around.before.at(-1)!.sessionId &&
        Number(around.after.at(-1)!.lastEventSequence ?? -1) >= Number(around.before.at(-1)!.lastEventSequence ?? 0);
      scenarioChecks.single_attestation_identity = attestationIds.length === 1;
      scenarioChecks.attestation_cancelled = Boolean(finalAttestation) &&
        finalAttestation!.outcome === "cancelled" &&
        finalAttestation!.sessionId === finalPreview?.sessionId;
    } else if (caseId === "test_006") {
      const restartPivot = actionIndex("restart_state");
      const compatibilityPivot = actionIndex("compatibility_chat");
      const runReads = actions
        .map((item, index) => ({index, item}))
        .filter((entry) => ["get_run", "inspect_latest"].includes(entry.item.action) && okValue(entry.item) !== null);
      const preRestart = runReads.filter((entry) => restartPivot < 0 || entry.index < restartPivot).at(-1);
      const postRestart = runReads.find((entry) => restartPivot >= 0 && entry.index > restartPivot);
      const preCompatibility = runReads.filter((entry) => compatibilityPivot < 0 || entry.index < compatibilityPivot).at(-1);
      const postCompatibility = runReads.find((entry) => compatibilityPivot >= 0 && entry.index > compatibilityPivot);
      const compatibility = actions.find((item) => item.action === "compatibility_chat")?.result as JsonObject | undefined;
      scenarioChecks.restart_selected = restartPivot >= 0 && coldRestartEvidence?.actual_fresh_process === true;
      scenarioChecks.no_automatic_rerun = shared.previewRunnerCalls === 1;
      scenarioChecks.interrupted_terminal_is_honest = ["infrastructure", "failed", "cancelled", "invalidated"]
        .includes(String(finalPreview?.status));
      scenarioChecks.restart_preserved_identity = Boolean(preRestart && postRestart) &&
        okValue(postRestart!.item)!.runId === okValue(preRestart!.item)!.runId &&
        Number(okValue(postRestart!.item)!.generation ?? -1) >= Number(okValue(preRestart!.item)!.generation ?? 0) &&
        Number(okValue(postRestart!.item)!.controlSequence ?? -1) >= Number(okValue(preRestart!.item)!.controlSequence ?? 0);
      scenarioChecks.attestation_matches_recovered_status = Boolean(finalAttestation) &&
        finalAttestation!.sessionId === finalPreview?.sessionId &&
        finalAttestation!.outcome === finalPreview?.status;
      scenarioChecks.compatibility_isolated = Boolean(preCompatibility && postCompatibility) &&
        JSON.stringify(okValue(preCompatibility!.item)) === JSON.stringify(okValue(postCompatibility!.item)) &&
        Number(compatibility?.streamEnd ?? 0) > 0 && Number(compatibility?.responseErrors ?? -1) === 0;
    } else {
      scenarioChecks.public_model_selected_flow = actions.length > 0;
    }
    writeEvidence({
      schema_version: "dyad-lower-native-evidence-v1",
      case_id: caseId,
      scenario_id: scenario.scenario_id,
      executed_task_path: taskFile,
      executed_task_sha256: sha256(task),
      real_product: Boolean(harness),
      setup_error: setupError instanceof Error ? setupError.stack || setupError.message : setupError ? String(setupError) : null,
      setup_phase: setupPhase,
      model_response_succeeded: streamErrors.length === 0,
      // A step the driver retried is named here rather than being folded,
      // silently, into `malformed_or_missing_model_action`.
      model_response_transport_errors: streamErrors.length,
      model_driver_reprompts: modelReprompts,
      action_protocol_complete: finished,
      malformed_or_missing_model_action: malformedAction,
      generic_fallback_used: false,
      private_oracle_visible: false,
      agent_artifact_origin: finished ? "model_finish_action" : null,
      provider_free_probe: providerFreeProbe,
      cold_restart: coldRestartEvidence,
      final_app_snapshot: harness ? process.env.DYAD_FINAL_APP_SNAPSHOT : null,
      workspace: binding,
      final_workspace_observation: finalWorkspace,
      target_bytes_sha256_at_admission: targetBytesSha256AtAdmission,
      denial_baseline_snapshot: jsonSafe(snapshotBeforeDenials),
      read_stability_probes: readStabilityProbes,
      acceptance_gate_probes: acceptanceGateProbes,
      product_action_trajectory: actions,
      scenario_checks: scenarioChecks,
      runner: {
        calls: shared.runnerCalls,
        preview_calls: shared.previewRunnerCalls,
        agent_calls: shared.agentRunnerCalls,
        outcomes: shared.runnerOutcomes,
        native_browser_executions: shared.nativeBrowserExecutions,
        headed_presentation_verified: false,
        actual_headless_chromium: true,
        late_callback_delivered: shared.lateCallbackDelivered,
      },
      ipc_events: ipcEvents,
      stream_events: streamEvents,
      persistence: { messages: jsonSafe(messages), git_log: gitLog },
      acceptance_surface_observed: [
        "acceptance:get-latest-run",
        "acceptance:get-run",
        "acceptance:start-preview",
        "acceptance:get-preview",
      ].every((channel) => shared.ipcHandlers.has(channel)),
    });
    await harness?.dispose();
  }, 45_000);

  it("records evidence without evaluator-selected fallback actions", () => {
    if (process.env.DYAD_PROVIDER_FREE_BROWSER_PROBE) {
      expect(setupError).toBeFalsy();
      expect(providerFreeProbe).toBeTruthy();
      expect(providerFreeProbe?.private_fixture_module_visible).toBe(false);
      expect(providerFreeProbe?.credential_file_visible).toBe(false);
      expect(shared.runnerCalls).toBe(caseId === "dev_002" ? 2 : 1);
      expect(shared.nativeBrowserExecutions).toHaveLength(caseId === "dev_002" ? 2 : 1);
    }
    expect(true).toBe(true);
  });
});
