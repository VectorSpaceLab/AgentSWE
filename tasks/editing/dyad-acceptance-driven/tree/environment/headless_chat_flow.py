#!/usr/bin/env python3
"""Run a real Dyad chat flow without launching Electron.

This driver creates a temporary Vitest test outside the product tree. The test
imports the Candidate's own hybrid chat harness, which exercises real Dyad
SQLite, Git, chat:stream, response processing and full production IPC
registration. The repository Electron module and auxiliary fake endpoints are
test doubles and are reported as such; they do not count as native Electron
evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path


# Seconds held back from the case deadline so the timeout branch in _main can
# still copy the sandbox artifact and native evidence out and write its launch
# record.  Measured tail on run 0919-fw-001: evidence copy + record write
# <= 0.06 s, process-tree teardown <= 0.40 s.  The controller above kills this
# process LAUNCHER_TAIL_RESERVE_SECONDS before the deadline, so this reserve
# must stay strictly larger than that one.
EVIDENCE_PERSIST_RESERVE_SECONDS = 15.0


# Historical source retained only as migration context. Runtime selection below
# has no reference to it and has no fallback from the model-selected driver.
UNUSED_LEGACY_SINGLE_FLOW_SOURCE = r'''// @vitest-environment happy-dom
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const shared = vi.hoisted(() => {
  process.env.NODE_ENV = "development";
  process.env.E2E_TEST_BUILD = "true";
  const value = { ipcHandlers: new Map(), ipcListeners: new Map() };
  (globalThis as typeof globalThis & { __DYAD_ELECTRON_SHARED__?: unknown })
    .__DYAD_ELECTRON_SHARED__ = value;
  return value;
});

import { setupHybridChatHarness } from "@/testing/hybrid_chat_harness";
import { createFakeIpcEvent } from "@/testing/electron_mock";

const taskFile = process.env.DYAD_TASK_FILE!;
const artifactFile = process.env.DYAD_ARTIFACT_FILE!;
const caseId = process.env.DYAD_CASE_ID!;
const requestedChatMode = process.env.DYAD_CHAT_MODE || "acceptance";
const brokerEndpoint = process.env.DYAD_BROKER_ENDPOINT || "";
const brokerBaseUrl = brokerEndpoint.replace(/\/responses\/?$/, "");
const streamTimeoutMs = Math.max(
  1_000,
  Number(process.env.DYAD_STREAM_TIMEOUT_MS || "120000"),
);
const acceptanceSupported = fs
  .readFileSync(path.join(process.cwd(), "src/lib/schemas.ts"), "utf8")
  .includes('"acceptance"');
if (requestedChatMode === "acceptance" && !acceptanceSupported) {
  throw new Error(
    "Acceptance product surface is unavailable; refusing to masquerade as local-agent chat",
  );
}
const configuredChatMode = requestedChatMode;
const task = fs.readFileSync(taskFile, "utf8");
const sha256 = (value: string) =>
  crypto.createHash("sha256").update(value).digest("hex");
const jsonSafe = (value: unknown): unknown =>
  value === undefined || value === null
    ? value
    : JSON.parse(
    JSON.stringify(value, (_key, item) =>
      typeof item === "bigint" ? Number(item) : item,
    ),
      );

function writeArtifact(value: unknown): void {
  fs.mkdirSync(path.dirname(artifactFile), { recursive: true });
  fs.writeFileSync(artifactFile, JSON.stringify(value, null, 2) + "\n");
}

describe("Dyad lower-agent headless execution", () => {
  let harness: Awaited<ReturnType<typeof setupHybridChatHarness>> | undefined;
  let setupError: unknown;

  beforeAll(async () => {
    try {
      if (brokerEndpoint) {
        process.env.DYAD_ENGINE_URL = brokerBaseUrl;
        process.env.DYAD_PRO_API_KEY = "broker-only-placeholder";
        process.env.OPENAI_API_KEY = "broker-only-placeholder";
      }
      harness = await setupHybridChatHarness({
        electronMock: shared,
        // Acceptance is a distinct product surface. Never downgrade it to
        // ordinary local-agent chat, because ordinary chat is not acceptance evidence.
        chatMode: configuredChatMode as never,
        autoApprove: true,
        selectedModel: { provider: "openai", name: "gpt-5.6-sol" },
        engine: !brokerEndpoint,
        testBuild: true,
        assertNoMissingChannels: false,
        settings: {
          enableDyadPro: Boolean(brokerEndpoint),
          providerSettings: brokerEndpoint
            ? { auto: { apiKey: { value: "broker-only-placeholder" } } }
            : undefined,
        },
      });
    } catch (error) {
      setupError = error;
    }
  }, 45_000);

  afterAll(async () => {
    await harness?.dispose();
  }, 45_000);

  it("records real chat:stream and durable workspace evidence", async () => {
    if (!harness) {
      writeArtifact({
        schema_version: "dyad-lower-agent-artifact-v2",
        case_id: caseId,
        classification: "mock_or_stub_only",
        success: false,
        real_product: false,
        execution: "headless",
        acceptance: { supported: false },
        blockers: [
          "real chat-flow harness setup failed: " +
            (setupError instanceof Error
              ? setupError.stack || setupError.message
              : String(setupError)),
        ],
      });
      expect(harness).toBeTruthy();
      return;
    }

    let streamTimer: ReturnType<typeof setTimeout> | undefined;
    let streamed: Awaited<ReturnType<typeof harness.streamChat>>;
    try {
      streamed = await Promise.race([
        harness.streamChat(task, {
          requestedChatMode: configuredChatMode as never,
        }),
        new Promise<never>((_, reject) => {
          streamTimer = setTimeout(
            () =>
              reject(
                new Error(
                  `candidate chat:stream exceeded ${streamTimeoutMs}ms`,
                ),
              ),
            streamTimeoutMs,
          );
        }),
      ]);
    } catch (error) {
      const requiredAcceptanceChannels = [
        "acceptance:get-latest-run",
        "acceptance:get-run",
        "acceptance:start-preview",
        "acceptance:get-preview",
        "acceptance:get-attestation",
      ];
      const acceptanceChannels = requiredAcceptanceChannels.filter((channel) =>
        shared.ipcHandlers.has(channel),
      );
      writeArtifact({
        schema_version: "dyad-lower-agent-artifact-v2",
        case_id: caseId,
        classification: "candidate_stream_failure",
        success: false,
        real_product: true,
        execution: "headless",
        acceptance: {
          supported: acceptanceChannels.length === requiredAcceptanceChannels.length,
          channels: acceptanceChannels,
          requested_chat_mode: requestedChatMode,
          configured_chat_mode: configuredChatMode,
          started: false,
          persisted: false,
          attested: false,
        },
        evidence: {
          real_chat_stream: true,
          model_response_succeeded: false,
          persistent_user_and_assistant_messages: false,
          git_checkout_observed: Boolean(harness.appDir),
          git_history_observed: harness.gitLog().length >= 1,
          git_mutation_observed: false,
          acceptance_surface_observed:
            acceptanceChannels.length === requiredAcceptanceChannels.length,
          acceptance_chain_complete: false,
          broker_configured: Boolean(brokerEndpoint),
        },
        blockers: [
          "candidate chat stream did not reach a terminal product result: " +
            (error instanceof Error ? error.message : String(error)),
        ],
      });
      // A bounded product failure is valid evaluator evidence. The outer
      // classifier combines this artifact with broker deltas and attributes it
      // to the Candidate instead of retrying it as infrastructure failure.
      expect(true).toBe(true);
      return;
    } finally {
      if (streamTimer) clearTimeout(streamTimer);
    }
    const events = jsonSafe(streamed.events) as Array<{
      channel: string;
      payload: unknown;
    }>;
    const messages = jsonSafe(streamed.messages) as Array<
      Record<string, unknown>
    >;
    const channels = events.map((event) => event.channel);
    const hasStreamLifecycle = ["chat:stream:start", "chat:stream:end"].every(
      (channel) => channels.includes(channel),
    );
    const modelResponseSucceeded =
      channels.includes("chat:response:end") &&
      !channels.includes("chat:response:error");
    const hasPersistentMessages =
      messages.some((message) => message.role === "user") &&
      messages.some((message) => message.role === "assistant");
    const gitLog = harness.gitLog();
    const requiredAcceptanceChannels = [
      "acceptance:get-latest-run",
      "acceptance:get-run",
      "acceptance:start-preview",
      "acceptance:get-preview",
      "acceptance:get-attestation",
    ];
    const acceptanceChannels = requiredAcceptanceChannels.filter((channel) =>
      shared.ipcHandlers.has(channel),
    );
    const acceptanceEvents: Array<{ channel: string; payload: unknown }> = [];
    const invoke = async (channel: string, input: unknown): Promise<unknown> => {
      const handler = shared.ipcHandlers.get(channel);
      if (!handler) return { missingHandler: channel };
      return handler(createFakeIpcEvent(acceptanceEvents), input);
    };
    const unwrap = (value: unknown): unknown => {
      const envelope = value as { ok?: boolean; value?: unknown };
      return typeof envelope?.ok === "boolean"
        ? envelope.ok
          ? envelope.value
          : null
        : value;
    };
    const acceptance: Record<string, unknown> = {
      supported: requiredAcceptanceChannels.every((channel) =>
        shared.ipcHandlers.has(channel),
      ),
      channels: acceptanceChannels,
      requested_chat_mode: requestedChatMode,
      configured_chat_mode: configuredChatMode,
      started: false,
      persisted: false,
      attested: false,
    };
    let boundRunId = "";
    let boundSessionId = "";
    let boundWorkspaceRevision = "";
    let boundTargetFingerprint = "";
    if (acceptance.supported === true) {
      const latestEnvelope = await invoke("acceptance:get-latest-run", {
        appId: harness.appId,
        chatId: harness.chatId,
      });
      const latest = unwrap(latestEnvelope) as Record<string, unknown> | null;
      acceptance.latest_run = jsonSafe(latestEnvelope);
      const testRuns = Array.isArray(latest?.testRuns) ? latest.testRuns : [];
      const lastRun = testRuns.at(-1) as Record<string, unknown> | undefined;
      const targetValue = lastRun?.target as Record<string, unknown> | undefined;
      const target = {
        testFile:
          process.env.DYAD_TEST_FILE ||
          (typeof targetValue?.testFile === "string" ? targetValue.testFile : ""),
        grep:
          typeof targetValue?.grep === "string" ? targetValue.grep : null,
      };
      boundRunId = typeof latest?.runId === "string" ? latest.runId : "";
      boundWorkspaceRevision =
        typeof latest?.workspaceRevision === "string"
          ? latest.workspaceRevision
          : typeof lastRun?.workspaceRevision === "string"
            ? lastRun.workspaceRevision
            : "";
      boundTargetFingerprint =
        typeof lastRun?.targetFingerprint === "string"
          ? lastRun.targetFingerprint
          : "";
      const command = {
        appId: harness.appId,
        chatId: harness.chatId,
        runId: typeof latest?.runId === "string" ? latest.runId : "",
        operationId: "headless-" + caseId + "-" + sha256(task).slice(0, 16),
        expectedGeneration:
          typeof latest?.generation === "number" ? latest.generation : 0,
        target,
        presentation: "preview",
      };
      try {
        if (!command.runId || !target.testFile) {
          throw new Error("product did not expose a runnable Acceptance run/target");
        }
        const startEnvelope = await invoke("acceptance:start-preview", command);
        const startResult = unwrap(startEnvelope) as Record<string, unknown> | null;
        acceptance.started = Boolean(startResult?.sessionId);
        acceptance.start_envelope = jsonSafe(startEnvelope);
        const sessionId = startResult?.sessionId;
        if (typeof sessionId === "string" && sessionId.length > 0) {
          boundSessionId = sessionId;
          if (
            !boundTargetFingerprint &&
            typeof startResult?.testFingerprint === "string"
          ) {
            boundTargetFingerprint = startResult.testFingerprint;
          }
          const readbackEnvelope = await invoke("acceptance:get-preview", {
            appId: harness.appId,
            chatId: harness.chatId,
            sessionId,
          });
          const readback = unwrap(readbackEnvelope) as Record<string, unknown> | null;
          acceptance.persisted = readback != null;
          acceptance.readback_envelope = jsonSafe(readbackEnvelope);
          if (
            !boundTargetFingerprint &&
            typeof readback?.testFingerprint === "string"
          ) {
            boundTargetFingerprint = readback.testFingerprint;
          }
          const attestationEvent = [...acceptanceEvents]
            .reverse()
            .find((event) => event.channel === "acceptance:attestation-updated");
          const attestation = attestationEvent?.payload as
            | Record<string, unknown>
            | undefined;
          if (typeof attestation?.attestationId === "string") {
            if (
              !boundTargetFingerprint &&
              typeof attestation.testFingerprint === "string"
            ) {
              boundTargetFingerprint = attestation.testFingerprint;
            }
            const attestationReadback = await invoke(
              "acceptance:get-attestation",
              {
                appId: harness.appId,
                chatId: harness.chatId,
                attestationId: attestation.attestationId,
              },
            );
            acceptance.attested = unwrap(attestationReadback) != null;
            acceptance.attestation = jsonSafe(attestation);
            acceptance.attestation_readback = jsonSafe(attestationReadback);
          }
        }
      } catch (error) {
        acceptance.start_error = String(error);
      }
    }
    acceptance.events = jsonSafe(acceptanceEvents);
    const realChatEvidence =
      hasStreamLifecycle &&
      hasPersistentMessages &&
      Boolean(harness.appDir) &&
      gitLog.length >= 1;
    const acceptanceComplete =
      acceptance.supported === true &&
      acceptance.started === true &&
      acceptance.persisted === true &&
      acceptance.attested === true;
    const validCaseId = (value: unknown): boolean =>
      (typeof value === "string" && value.trim().length > 0) ||
      (typeof value === "number" &&
        Number.isSafeInteger(value) &&
        value > 0);
    const caseBindingComplete = Boolean(
      validCaseId(harness.appId) &&
        validCaseId(harness.chatId) &&
        boundRunId &&
        boundSessionId &&
        boundWorkspaceRevision &&
        boundTargetFingerprint,
    );
    const blockers: string[] = [];
    if (!modelResponseSucceeded) blockers.push("provider/model response did not complete");
    if (acceptance.supported !== true) blockers.push("Acceptance IPC surface is absent or incomplete");
    else if (!acceptanceComplete) blockers.push("Acceptance preview/session/attestation chain was not fully observed");
    if (!caseBindingComplete) blockers.push("Acceptance evidence is not bound to app/chat/run/session/revision/target fingerprint");

    writeArtifact({
      schema_version: "dyad-lower-agent-artifact-v2",
      case_id: caseId,
      classification: acceptanceComplete
        ? "headless_real_acceptance"
        : "headless_real_chat_stream",
      success:
        realChatEvidence &&
        modelResponseSucceeded &&
        acceptanceComplete &&
        caseBindingComplete,
      real_product: true,
      execution: "headless",
      product_entry:
        "real chat:stream IPC handler -> real SQLite/messages -> real Git app checkout/response processor",
      test_doubles: [
        "Electron module mock required by the repository chat-flow harness",
        "in-process fake LLM server for catalog/user-info auxiliary endpoints; it is not the model transport",
      ],
      model_transport: brokerEndpoint
        ? {
            kind: "evaluator-owned-responses-broker",
            endpoint: brokerEndpoint,
            model: "gpt-5.6-sol",
            reasoning_effort: "high",
            candidate_auth: "broker-only-placeholder",
            fake_llm_model_requests_allowed: false,
          }
        : { kind: "repository-fake-llm", counted_as_behavior_success: false },
      task: {
        sha256: sha256(task),
        bytes: Buffer.byteLength(task),
        source: taskFile,
      },
      workspace: {
        app_id: harness.appId,
        chat_id: harness.chatId,
        run_id: boundRunId,
        session_id: boundSessionId,
        revision: boundWorkspaceRevision,
        target_fingerprint: boundTargetFingerprint,
        app_dir: harness.appDir,
        user_data_dir: harness.userDataDir,
      },
      stream: {
        result: jsonSafe(streamed.result),
        channels,
        events,
        message_count: messages.length,
      },
      persistence: { messages, git_log: gitLog },
      acceptance,
      evidence: {
        real_chat_stream: hasStreamLifecycle,
        model_response_succeeded: modelResponseSucceeded,
        persistent_user_and_assistant_messages: hasPersistentMessages,
        git_checkout_observed: Boolean(harness.appDir),
        git_history_observed: gitLog.length >= 1,
        git_mutation_observed: gitLog.length > 1,
        acceptance_surface_observed: acceptance.supported === true,
        acceptance_chain_complete: acceptanceComplete,
        case_binding_complete: caseBindingComplete,
        broker_configured: Boolean(brokerEndpoint),
      },
      blockers,
    });
    expect(realChatEvidence).toBe(true);
  }, 300_000);
});
'''


# Vitest can externalize the npm ``electron`` launcher before a ``vi.mock``
# factory is consulted.  In plain Node that package exports only the Electron
# executable path, so named product imports such as ``safeStorage`` become
# undefined.  Route every transitive Electron import through an explicit ESM
# shim instead.  The core behavior still comes from the Candidate repository's
# reusable Electron test helper; the extra exports are inert collection-time
# boundaries for modules that the hybrid harness imports but does not execute.
ELECTRON_SHIM_SOURCE = r'''import { vi } from "vitest";
import { createElectronMock } from "@/testing/electron_mock";

const shared = (globalThis as typeof globalThis & {
  __DYAD_ELECTRON_SHARED__?: {
    ipcHandlers: Map<string, (...args: unknown[]) => unknown>;
    ipcListeners: Map<string, Array<(...args: unknown[]) => void>>;
  };
}).__DYAD_ELECTRON_SHARED__;

if (!shared) throw new Error("Dyad Electron shim initialized before shared state");
const core = createElectronMock(shared as never) as Record<string, any>;

export const app = core.app;
export const ipcMain = core.ipcMain;
export const BrowserWindow = core.BrowserWindow;
export const safeStorage = core.safeStorage ?? {
  isEncryptionAvailable: () => false,
  encryptString: (value: string) => Buffer.from(value),
  decryptString: (value: Buffer) => value.toString(),
};
export const Notification = core.Notification;
export const shell = core.shell;
export const dialog = core.dialog;
export const net = core.net;
export const utilityProcess = core.utilityProcess;
export const contextBridge = { exposeInMainWorld: vi.fn() };
export const ipcRenderer = {
  invoke: vi.fn(), on: vi.fn(), once: vi.fn(), send: vi.fn(),
  removeListener: vi.fn(), removeAllListeners: vi.fn(),
};
export const webFrame = { setZoomFactor: vi.fn(), getZoomFactor: vi.fn(() => 1) };
export const autoUpdater = { on: vi.fn(), once: vi.fn(), checkForUpdates: vi.fn() };
export const Menu = { buildFromTemplate: vi.fn(() => ({})), setApplicationMenu: vi.fn() };
export const protocol = { registerSchemesAsPrivileged: vi.fn(), handle: vi.fn() };
export const nativeImage = { createFromPath: vi.fn(() => ({})), createEmpty: vi.fn(() => ({})) };
export const crashReporter = { start: vi.fn(), addExtraParameter: vi.fn() };
export const session = { defaultSession: { webRequest: { onBeforeSendHeaders: vi.fn() } } };
export const clipboard = { readText: vi.fn(() => ""), writeText: vi.fn() };

export default {
  ...core, app, ipcMain, BrowserWindow, safeStorage, Notification, shell,
  dialog, net, utilityProcess, contextBridge, ipcRenderer, webFrame,
  autoUpdater, Menu, protocol, nativeImage, crashReporter, session, clipboard,
};
'''


def mount_snapshot(blocker: str) -> dict:
    """Facts that decide why a path was refused, captured while they still exist.

    Every hidden case of ds-010 and ds-011 failed with EACCES on a path under
    the launch directory, and by the time anyone looked the directory was gone,
    the container had exited, and no mount table had been kept. Directory
    permissions and ENOSPC were both ruled out from what survived; whether the
    `app/` level is a read-only bind mount could not be, for want of exactly
    this. Best-effort throughout: instrumentation that can fail the run it is
    instrumenting is worse than none.
    """
    import os
    import re as _re
    snapshot: dict = {"schema_version": "dyad-mount-snapshot-v1", "pid": os.getpid()}
    try:
        snapshot["mountinfo"] = Path("/proc/self/mountinfo").read_text().splitlines()
    except Exception as exc:
        snapshot["mountinfo_error"] = f"{type(exc).__name__}: {exc}"

    def governing(path: str) -> str | None:
        best = None
        for line in snapshot.get("mountinfo", []):
            fields = line.split()
            if len(fields) > 5:
                point = fields[4]
                if (path == point or path.startswith(point.rstrip("/") + "/")) and (
                        best is None or len(point) > len(best.split()[4])):
                    best = line
        return best

    found = _re.search(r"(/[^\s'\")]+)", blocker or "")
    target = found.group(1) if found else None
    snapshot["path"] = target
    levels = []
    while target and target != "/":
        row: dict = {"path": target}
        try:
            info = os.lstat(target)
            row.update(exists=True, mode=oct(info.st_mode), uid=info.st_uid, gid=info.st_gid,
                       is_dir=os.path.isdir(target), is_symlink=os.path.islink(target),
                       writable=os.access(target, os.W_OK))
        except FileNotFoundError:
            row["exists"] = False
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["mount"] = governing(target)
        levels.append(row)
        target = os.path.dirname(target)
    snapshot["levels"] = levels
    return snapshot


def write_failure(evidence: Path, case_id: str, blocker: str) -> None:
    """Write evaluator/native failure evidence, never an agent artifact."""
    evidence.parent.mkdir(parents=True, exist_ok=True)
    # Additive: written beside the evidence, never into it, so the record this
    # is meant to explain stays byte-for-byte what it was.
    try:
        (evidence.parent / (evidence.stem + ".mount-snapshot.json")).write_text(
            json.dumps(mount_snapshot(blocker), indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    evidence.write_text(
        json.dumps(
            {
                "schema_version": "dyad-lower-native-evidence-v1",
                "case_id": case_id,
                "real_product": False,
                "setup_error": blocker,
                "action_protocol_complete": False,
                "generic_fallback_used": False,
                "private_oracle_visible": False,
                "agent_artifact_origin": None,
                "product_action_trajectory": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def prepare_git_runtime(run_root: Path) -> tuple[Path | None, str | None]:
    """Provide dugite's expected layout without changing the Candidate tree.

    The prepared npm dependency cache intentionally omits dugite's platform
    archive.  Dyad's real git adapter still requires ``bin/git`` and the
    matching exec path, so expose the host Git installation through a
    run-private, read-only symlink layout.  This is evaluator runtime setup,
    not a product or Candidate source change.
    """
    candidates = (Path("/usr/bin/git"), Path("/bin/git"))
    git_binary = next((path for path in candidates if path.is_file()), None)
    if git_binary is None:
        return None, "system git executable is unavailable"
    exec_candidates = (Path("/usr/lib/git-core"), Path("/usr/libexec/git-core"))
    git_exec = next((path for path in exec_candidates if path.is_dir()), None)
    template_dir = Path("/usr/share/git-core/templates")
    if git_exec is None or not template_dir.is_dir():
        return None, "system git exec/template directories are unavailable"
    root = run_root / "system-git"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "libexec").mkdir(parents=True, exist_ok=True)
    (root / "share/git-core").mkdir(parents=True, exist_ok=True)
    binary_link = root / "bin/git"
    exec_link = root / "libexec/git-core"
    templates_link = root / "share/git-core/templates"
    for link, target in (
        (binary_link, git_binary),
        (exec_link, git_exec),
        (templates_link, template_dir),
    ):
        if link.exists() or link.is_symlink():
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            return None, f"runtime git path is unexpectedly occupied: {link}"
        link.symlink_to(target, target.is_dir())
    return root, None


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--native-evidence", type=Path, required=True)
    parser.add_argument("--scenario-public", type=Path, required=True)
    parser.add_argument("--broker-endpoint", default="")
    parser.add_argument("--chat-mode", default="acceptance")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--case-deadline-monotonic", type=float)
    parser.add_argument("--provider-free-browser-probe", action="store_true")
    args = parser.parse_args()

    repo = args.repository.resolve()
    artifact = args.artifact.resolve()
    native_evidence = args.native_evidence.resolve()
    scenario_public = args.scenario_public.resolve()
    if not (repo / "vitest.config.ts").is_file():
        write_failure(native_evidence, args.case_id, "vitest.config.ts is missing")
        return 2
    vitest = repo / "node_modules/.bin/vitest"
    if not vitest.is_file():
        write_failure(native_evidence, args.case_id, "prepared vitest dependency is missing")
        return 2
    if not scenario_public.is_file():
        write_failure(native_evidence, args.case_id, "run-local public scenario fixture is missing")
        return 2

    run_root = args.output.resolve().parent / (args.output.stem + "-headless")
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "node_modules").symlink_to((repo / "node_modules").resolve(), target_is_directory=True)
    git_runtime, git_error = prepare_git_runtime(run_root)
    if git_runtime is None:
        write_failure(native_evidence, args.case_id, git_error or "system git runtime preparation failed")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "schema_version": "dyad-headless-launch-v2",
                    "case_id": args.case_id,
                    "command": [],
                    "exit_code": 125,
                    "timed_out": False,
                    "duration_seconds": 0,
                    "artifact": str(artifact),
                    "artifact_present": artifact.is_file(),
                    "native_evidence": str(native_evidence),
                    "artifact_classification": "infrastructure-invalid",
                    "artifact_success": False,
                    "failure_class": "missing_product_runtime_dependency",
                    "attribution": {"owner": "evaluator/runtime", "candidate_behavior_evaluable": False},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1
    test_file = run_root / "scenario_chat_flow.test.ts"
    scenario_driver = Path(__file__).resolve().with_name("scenario_chat_flow.test.ts")
    if not scenario_driver.is_file():
        write_failure(native_evidence, args.case_id, "model-selected scenario driver is missing")
        return 2
    scenario_source=scenario_driver.read_text(encoding="utf-8")
    if scenario_source.count('harness!.streamChat(')!=1 or scenario_source.count('harness.streamChat(')!=1:
        raise RuntimeError('Expected exact two trusted scenario streamChat call sites')
    scenario_source=scenario_source.replace('harness!.streamChat(', 'agentsweStreamChat(harness!, ').replace('harness.streamChat(', 'agentsweStreamChat(harness, ')
    test_file.write_text('// Evaluator transport metadata; Candidate product code is unchanged.\nimport { AsyncLocalStorage as AgentSWEAsyncLocalStorage } from "node:async_hooks";\nconst agentsweActionScope = new AgentSWEAsyncLocalStorage<string>();\nlet agentsweActionSequence = 0;\nconst agentsweOriginalFetch = globalThis.fetch;\nglobalThis.fetch = (input, init) => {\n  const action = agentsweActionScope.getStore();\n  if (!action) return agentsweOriginalFetch(input, init);\n  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;\n  const target = new URL(url);\n  if (!["/v1/responses", "/v1/chat/completions"].includes(target.pathname)) return agentsweOriginalFetch(input, init);\n  const headers = new Headers(input instanceof Request ? input.headers : undefined);\n  new Headers(init?.headers).forEach((value, key) => headers.set(key, value));\n  headers.set("X-AgentSWE-Action", action);\n  return agentsweOriginalFetch(input, {...init, headers});\n};\nconst agentsweStreamChat = (harness: any, ...args: any[]) => agentsweActionScope.run(\n  `${process.env.DYAD_CASE_ID}:${process.env.DYAD_RESTART_EPOCH || "0"}:${++agentsweActionSequence}`,\n  () => harness.streamChat(...args),\n);\n'+scenario_source,encoding="utf-8")
    electron_shim = run_root / "electron-shim.ts"
    electron_shim.write_text(ELECTRON_SHIM_SOURCE, encoding="utf-8")
    # The product config uses Vitest projects whose includes are restricted to
    # ``src/**/*``.  The generated test intentionally lives outside the product
    # tree, so invoking that config directly makes Vitest report "No test files
    # found" before any Dyad code is loaded.  Use a run-local config instead:
    # keep the product's source alias, but explicitly select this generated
    # integration test and remove the product-level projects filter.  This is
    # evaluator plumbing only; it does not alter the Candidate repository.
    repo_literal = json.dumps(str(repo))
    test_literal = json.dumps(str(test_file))
    electron_shim_literal = json.dumps(str(electron_shim))
    vitest_config_module = json.dumps(str(repo / "node_modules/vitest/dist/config.js"))
    react_plugin_module = json.dumps(
        str(repo / "node_modules/@vitejs/plugin-react/dist/index.js")
    )
    config_file = run_root / "vitest.headless.config.ts"
    config_file.write_text(
        f"import {{ defineConfig }} from {vitest_config_module};\n"
        f"import react from {react_plugin_module};\n"
        "import { resolve } from 'node:path';\n"
        "export default defineConfig({\n"
        f"  cacheDir: {json.dumps(str(run_root / 'vite-cache'))},\n"
        "  plugins: [react()],\n"
        "  test: {\n"
        "    globals: true,\n"
        "    environment: 'happy-dom',\n"
        f"    include: [{test_literal}],\n"
        "    exclude: ['**/node_modules/**'],\n"
        "    pool: 'forks',\n"
        "    maxWorkers: 1,\n"
        "    minWorkers: 1,\n"
        "  },\n"
        "  server: { deps: { inline: ['electron'] } },\n"
        "  resolve: {\n"
        "    alias: {\n"
        f"      'electron': {electron_shim_literal},\n"
        f"      '@': resolve({repo_literal}, 'src'),\n"
        f"      'pg-schema-classifier': resolve({repo_literal}, 'packages/pg-schema-classifier/src/index.ts'),\n"
        f"      'ts-pg-schema-diff': resolve({repo_literal}, 'packages/ts-pg-schema-diff/src/index.ts'),\n"
        "    },\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )
    case_path = args.case.resolve()
    task_file_path = case_path / "input.md" if case_path.is_dir() else case_path
    if not task_file_path.is_file():
        write_failure(native_evidence, args.case_id, f"case input is missing: {task_file_path}")
        return 2
    node = Path('@@AGENTSWE_ENVS@@/runtime-deps-0910/dyad/node24.6.0/bin/node')
    modules = Path('@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006/node_modules')
    browser = Path('@@AGENTSWE_ENVS@@/runtime-deps-0909/dyad/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/chrome-headless-shell')
    env = {'PATH': str(node.parent) + ':/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    isolated_home = run_root / "home"
    isolated_config = run_root / "config"
    isolated_cache = run_root / "cache"
    isolated_tmp = run_root / "tmp"
    for directory in (isolated_home, isolated_config, isolated_cache, isolated_tmp):
        directory.mkdir(parents=True, exist_ok=True)
    sandbox_task = run_root / 'public-task.md'
    sandbox_fixture = run_root / 'public-fixture.json'
    sandbox_artifact = run_root / 'agent-result.json'
    sandbox_native = run_root / 'native-evidence.json'
    shutil.copyfile(task_file_path, sandbox_task)
    shutil.copyfile(scenario_public, sandbox_fixture)
    browser_runner = run_root / 'behavior_runner.mjs'
    shutil.copyfile(Path(__file__).with_name('behavior_runner.mjs'), browser_runner)
    browser_runtime = run_root / 'browser-runtime'
    browser_runtime.mkdir()
    env.update(
        {
            "DYAD_TASK_FILE": str(sandbox_task),
            "DYAD_SCENARIO_FILE": str(sandbox_fixture),
            "DYAD_ARTIFACT_FILE": str(sandbox_artifact),
            "DYAD_NATIVE_EVIDENCE_FILE": str(sandbox_native),
            "DYAD_BEHAVIOR_RUNNER": str(browser_runner),
            "DYAD_BROWSER_RUNTIME": str(browser_runtime),
            "DYAD_BROWSER_NODE_MODULES": str(modules),
            "DYAD_BROWSER_EXECUTABLE": str(browser),
            "DYAD_PROVIDER_FREE_BROWSER_PROBE": '1' if args.provider_free_browser_probe else '',
            "DYAD_CASE_ID": args.case_id,
            "DYAD_RESTART_CHECKPOINT": str(run_root / "restart-checkpoint.json"),
            "DYAD_RESTART_EPOCH": "0",
            "DYAD_FINAL_APP_SNAPSHOT": str(run_root / "final-app"),
            "DYAD_CHAT_MODE": args.chat_mode,
            "DYAD_BROKER_ENDPOINT": args.broker_endpoint,
            "LOCAL_GIT_DIRECTORY": str(git_runtime),
            "DYAD_DEV_USER_DATA_DIR": str(run_root / "user-data"),
            "FAKE_LLM_QUIET": "1",
            "NODE_ENV": "development",
            "CI": "true",
            "HOME": str(isolated_home),
            "XDG_CONFIG_HOME": str(isolated_config),
            "XDG_CACHE_HOME": str(isolated_cache),
            "TMPDIR": str(isolated_tmp),
        }
    )
    command = [str(node), str(repo / 'node_modules/vitest/vitest.mjs'),
        "run",
        str(test_file),
        "--config",
        str(config_file),
        "--maxWorkers=1",
        "--minWorkers=1",
    ]
    from transport_sandbox import FixedLowerRelay, sandbox_command
    deadline=args.case_deadline_monotonic or time.monotonic()+min(args.timeout,600)
    # `deadline` is when this process is killed from OUTSIDE.  Stop the Candidate
    # a reserve earlier so the TimeoutExpired branch below still has time to copy
    # the native evidence out of the sandbox and write this launch record;
    # without it a timed-out case leaves no evidence at all and the formal
    # publisher cannot attribute the failure (0919-fw-001 test_004/test_006).
    work_deadline = deadline - EVIDENCE_PERSIST_RESERVE_SECONDS
    relay = FixedLowerRelay(args.broker_endpoint,context_id=str(run_root.resolve())+'|'+args.case_id,deadline=work_deadline,journal=run_root/'lower-request-identities.json').start() if args.broker_endpoint else None
    transport_preflight = run_root / 'transport-preflight.json'
    if relay:
        command = ['/usr/bin/python3', '/run/agentswe/transport.py', '--inside',
            '--uds', '/run/agentswe/lower.sock', '--preflight', str(transport_preflight), '--'] + command
    nested_modules = repo / 'testing/fake-llm-server/node_modules'
    extra_modules = [nested_modules.resolve()] if nested_modules.is_dir() else []
    command = sandbox_command(command, writable=[run_root], readonly=[repo, *extra_modules,
        (repo / 'node_modules').resolve(), modules, node.parent.parent, browser.parent,
        sandbox_task, sandbox_fixture, test_file, electron_shim, config_file, browser_runner], cwd=repo, relay=relay)
    started = time.monotonic()
    try:
        # Deadline already covers transport setup and both restart epochs.
        epochs = []
        for epoch in (0, 1):
            proc = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True,
                check=False, timeout=max(.001, work_deadline - time.monotonic()))
            epochs.append({"epoch": epoch, "exit_code": proc.returncode,
                "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]})
            checkpoint = run_root / "restart-checkpoint.json"
            if epoch == 0 and checkpoint.is_file():
                value = json.loads(checkpoint.read_text())
                if value.get("epoch") != 1 or value.get("case_id") != args.case_id:
                    raise RuntimeError("Invalid cold-restart checkpoint; refusing replay")
                env["DYAD_RESTART_EPOCH"] = "1"
                continue
            break
        (run_root / "process-epochs.json").write_text(json.dumps(epochs, indent=2) + "\n")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(
            command, 124, exc.stdout or "", exc.stderr or "timeout"
        )
        timed_out = True
    finally:
        if relay:
            relay.close()
    for source, destination in [(sandbox_artifact, artifact), (sandbox_native, native_evidence)]:
        if source.is_file() and not source.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    independent = None
    # A timed-out case has no budget left for the ~90 s independent browser
    # recheck, and its own timeout would raise TimeoutExpired out of _main and
    # lose the launch record the timeout branch above just earned.
    if not timed_out and (run_root / "final-app").is_dir():
        # The original lower PID namespace has exited. Private assertions are
        # now materialized for an independent browser; the app server receives
        # a separate mount/PID namespace that cannot read those assertions.
        trusted_root = str(Path(__file__).resolve().parents[1])
        if trusted_root not in sys.path: sys.path.insert(0, trusted_root)
        from evaluator.behavior_worlds import materialize
        observer = run_root / "independent-observer"
        world = materialize(args.case_id, observer / "baseline")
        check_app = observer / "actual-app"
        shutil.copytree(run_root / "final-app", check_app, symlinks=True)
        check_spec = "private-independent.spec.ts"
        # Private target lives in the observer tree, outside the app server's mount.
        source_spec = Path(world["private_independent_test"])
        shutil.copyfile(source_spec, check_app / check_spec)
        options = {"appId": 1, "appDir": str(check_app), "testFile": check_spec,
            "grep": "", "runtimeRoot": str(observer / "runtime"), "nodeModules": str(modules),
            "browserExecutable": str(browser), "timeoutMs": min(90000, max(1, int((work_deadline-time.monotonic())*1000))),
            "runOriginalTarget": False, "isolateAppServer": True}
        # Hide the private spec from the Candidate app server by using a
        # public source-only directory for that server (see behavior runner).
        options["serverAppDir"] = str(run_root / "final-app")
        config = observer / "options.json"; config.write_text(json.dumps(options))
        result_path = observer / "result.json"
        cmd = sandbox_command([str(node), str(browser_runner), str(config), str(result_path)],
            writable=[observer], readonly=[run_root / "final-app", modules, node.parent.parent, browser.parent, browser_runner], cwd=observer, trusted_observer_orchestrator=True)
        check = subprocess.run(cmd, env=env, cwd=observer, text=True, capture_output=True,
            timeout=max(.001, work_deadline - time.monotonic()))
        (observer / "stderr.log").write_text(check.stderr)
        independent = json.loads(result_path.read_text()) if result_path.is_file() else {"infrastructure_invalid": True, "exit_code": check.returncode}
        if native_evidence.is_file():
            value = json.loads(native_evidence.read_text())
            value["independent_behavior_recheck"] = independent
            native_evidence.write_text(json.dumps(value, indent=2) + "\n")
    result = {
        "schema_version": "dyad-headless-launch-v2",
        "case_id": args.case_id,
        "command": command,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": proc.stdout[-6000:] if isinstance(proc.stdout, str) else "",
        "stderr_tail": proc.stderr[-6000:] if isinstance(proc.stderr, str) else "",
        "artifact": str(artifact),
        "artifact_present": artifact.is_file(),
        "native_evidence": str(native_evidence),
        "native_evidence_present": native_evidence.is_file(),
        "network_namespace": "isolated",
        "host_tcp_access": False,
        "private_fixture_definitions_mounted": False,
        "provider_credential_mounted": False,
        "provider_free_browser_probe": args.provider_free_browser_probe,
    }
    if artifact.is_file():
        try:
            loaded = json.loads(artifact.read_text(encoding="utf-8"))
            result["artifact_classification"] = loaded.get("classification")
            result["artifact_success"] = loaded.get("success") is True
        except (OSError, json.JSONDecodeError):
            result["artifact_classification"] = "model_artifact_unreadable"
    if native_evidence.is_file():
        try:
            loaded_evidence = json.loads(native_evidence.read_text(encoding="utf-8"))
            result["product_entry_observed"] = loaded_evidence.get("real_product") is True
            result["action_protocol_complete"] = loaded_evidence.get("action_protocol_complete") is True
        except (OSError, json.JSONDecodeError):
            result["product_entry_observed"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("product_entry_observed") is True else 1


def main() -> int:
    # This controller imports only evaluator-owned Python. Every Candidate
    # import/Node process is inside bwrap and the independently bounded scope.
    trusted_directory = str(Path(__file__).resolve().parent)
    if trusted_directory not in sys.path:
        sys.path.insert(0, trusted_directory)
    if '--owned-case' in sys.argv:
        sys.argv.remove('--owned-case')
        return _main()
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--case-deadline-monotonic', type=float)
    args, _ = parser.parse_known_args()
    deadline = args.case_deadline_monotonic or time.monotonic() + min(args.timeout, 600)
    remaining = min(args.timeout, 600, deadline - time.monotonic())
    if remaining <= 0:
        raise RuntimeError('Case budget expired before product setup')
    from owned_resources import run_owned
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    owned_args=list(sys.argv[1:])
    if '--case-deadline-monotonic' not in owned_args:
        owned_args.extend(['--case-deadline-monotonic',str(deadline)])
    command = [sys.executable, '-I', str(Path(__file__).resolve()), '--owned-case', *owned_args]
    try:
        proc, attestation = run_owned(command, cwd=Path('/'),
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            output=output.parent / (output.stem + '-resources'), timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        # The owned case did not return inside the budget.  Report the timeout
        # through this controller's normal exit path so the launcher above still
        # writes its record and turns the persisted native evidence into a
        # trajectory; a bare traceback here loses all of it (0919-fw-001).
        proc = subprocess.CompletedProcess(command, 124, exc.stdout or '', exc.stderr or 'timeout')
        attestation = {'schema_version': 'agentswe-owned-case-resources/v1',
                       'valid': False, 'timed_out': True, 'timeout_seconds': remaining,
                       'reason': 'owned case exceeded the derived case budget'}
    (output.parent / (output.stem + '-controller.stdout.log')).write_text(proc.stdout or '')
    (output.parent / (output.stem + '-controller.stderr.log')).write_text(proc.stderr or '')
    # Preserve a completed lower launch record; the separate attestation owns
    # resource attribution and cannot be replaced by partial artifact success.
    print(json.dumps({'exit_code': proc.returncode, 'resource_attestation': attestation,
        'case_deadline_monotonic': deadline, 'lower_launch_record': str(output)}), flush=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
