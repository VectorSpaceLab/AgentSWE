#!/usr/bin/env node
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import process from "node:process";

const args = process.argv.slice(2);
const get = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const product = get("--product"), state = get("--state-dir"), broker = get("--broker-endpoint"), port = get("--port"), token = get("--gateway-token"), method = get("--method") ?? "agent", paramsFile = get("--params"), output = get("--output");
if (!product || !state || !broker || !port || !token || !paramsFile || !output) throw new Error("missing required lower launcher argument");
const command = ["node", `${product}/openclaw.mjs`, "gateway", "call", method, "--url", `ws://127.0.0.1:${port}`, "--token", token, "--params", JSON.stringify(JSON.parse(readFileSync(paramsFile, "utf8"))), "--json"];
const record = { schema_version: "openclaw-lower-launch-v1", entry: "node openclaw.mjs gateway call", command, model: "gpt-5.6-sol", reasoning_effort: "medium", broker_endpoint: broker, candidate_credential: "broker-only-placeholder", executed: args.includes("--execute") };
if (record.executed) { const r = spawnSync(command[0], command.slice(1), { cwd: product, env: { ...process.env, OPENCLAW_STATE_DIR: state, OPENAI_BASE_URL: broker.replace(/\/v1\/responses$/, ""), OPENAI_API_KEY: "broker-only-placeholder", AGENTSWE_RESPONSES_BASE_URL: broker, AGENTSWE_REQUIRED_MODEL: "gpt-5.6-sol", AGENTSWE_REQUIRED_REASONING_EFFORT: "medium" }, encoding: "utf8", timeout: 600000 }); Object.assign(record, { exit_code: r.status, stdout_tail: (r.stdout ?? "").slice(-4000), stderr_tail: (r.stderr ?? "").slice(-4000) }); }
mkdirSync(output.substring(0, output.lastIndexOf("/")), { recursive: true }); writeFileSync(output, `${JSON.stringify(record, null, 2)}\n`); console.log(JSON.stringify(record)); process.exit(record.executed && record.exit_code !== 0 ? 1 : 0);
