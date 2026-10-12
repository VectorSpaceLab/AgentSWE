/** Trusted external world process. No Candidate import or source execution. */
import fs from "node:fs";
import http from "node:http";
import { createCaseWorld } from "./case_world.ts";

const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const values = new Map<string, string>();
const storage: any = {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => values.set(key, String(value)),
  removeItem: (key: string) => values.delete(key),
  key: (index: number) => [...values.keys()][index] ?? null,
  get length() { return values.size; },
};
const world = createCaseWorld(config.case_id, config.nonce, config.fixture, storage,
  (conversation, revision) => `openhands:recovery:v2:${encodeURIComponent(conversation)}:${revision}`);
const initialStorage = [...values];
const wire = (value: any): any => value instanceof Uint8Array ? { __bytes__: [...value] }
  : Array.isArray(value) ? value.map(wire)
  : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, wire(item)])) : value;
const unwire = (value: any): any => value && Array.isArray(value.__bytes__) ? new Uint8Array(value.__bytes__)
  : Array.isArray(value) ? value.map(unwire)
  : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, unwire(item)])) : value;
const publicMethods: Record<string, Function> = {};
for (const name of ["pull", "readHead", "missingChunks", "readChunk", "writeChunk", "commit", "reconcileCommit"]) publicMethods[`transport.${name}`] = (world.transport as any)[name];
for (const name of ["capture", "readChunk", "missingStagedChunks", "stageChunk", "apply"]) publicMethods[`local.${name}`] = (world.local as any)[name];
publicMethods.executeEffect = world.executeEffect;
publicMethods.reconcileEffect = world.reconcileEffect;
const productRequest = (method: string, input: any) => new Promise<any>((resolve, reject) => {
  const request = http.request({ socketPath: config.product_socket, path: "/callback", method: "POST", headers: { "content-type": "application/json" } }, response => {
    let body = ""; response.on("data", chunk => { body += chunk; });
    response.on("end", () => { try { const value = unwire(JSON.parse(body)); value.error ? reject(new Error(value.error)) : resolve(value.result); } catch (error) { reject(error); } });
  }); request.on("error", reject); request.end(JSON.stringify(wire({ method, input })));
});
// 0921: every method the case world calls on the "adapter" it is handed must be
// on this shim.  It used to be written out inline and `enqueueEffect` was missing,
// so runEffectDeliveryProbe hit its own `typeof adapter.enqueueEffect !== "function"`
// guard in EVERY case for EVERY candidate and booked the forfeit as the Candidate's
// ("the Candidate adapter exposes no enqueueEffect", fh-003 test_006, OHR507) even
// though the product implements and exports it.  The shim is now built from one list
// and asserted, at startup, against the method set case_world.ts actually calls.
const ADAPTER_METHODS = ["ingestRuntimeEvent", "inspectRecoveryAuthorized", "inspectRecovery",
  "staleMigrationAttempt", "armRemovalFailure", "corruptNewestRecords", "enqueueEffect"];
const productAdapter: Record<string, (input: any) => Promise<any>> = Object.fromEntries(
  ADAPTER_METHODS.map(name => [name, (input: any) => productRequest(name, input)]));
const caseWorldSource = fs.readFileSync(new URL("./case_world.ts", import.meta.url), "utf8");
const calledAdapterMethods = [...new Set([...caseWorldSource.matchAll(/\badapter\.([A-Za-z_$][A-Za-z0-9_$]*)/g)]
  .map(match => match[1]))].sort();
const missingAdapterMethods = calledAdapterMethods.filter(name => typeof productAdapter[name] !== "function");
if (missingAdapterMethods.length) {
  throw new Error("world adapter shim exposes no " + missingAdapterMethods.join(", ")
    + "; case_world.ts calls it, so an oracle probe would be skipped and charged to the Candidate");
}
const publicProjection = () => ({ notice: world.visibleNotice(), scope: world.scope,
  clock: world.now(), authority: world.authority.current(), baseManifest: world.baseManifest,
  workspace_fault: world.workspaceFault() });
function handler(isPrivate: boolean) {
  return async (request: any, response: any) => {
    let body = "";
    request.on("data", (chunk: any) => { body += chunk; if (body.length > 2_000_000) request.destroy(); });
    request.on("end", async () => {
      try {
        const args = unwire(JSON.parse(body || "{}"));
        let result;
        if (isPrivate && request.url === "/before") {
          world.beforeAction(args.action, args.inspection); result = publicProjection();
        } else if (isPrivate && request.url === "/after") {
          if (Array.isArray(args.storage)) { values.clear(); for (const [key, value] of args.storage) values.set(key, value); }
          await world.afterAction(args.action, productAdapter, args.result);
          result = publicProjection();
        } else if (isPrivate && request.url === "/observe-state") {
          if (Array.isArray(args.storage)) { values.clear(); for (const [key, value] of args.storage) values.set(key, value); }
          result = publicProjection();
        } else if (isPrivate && request.url === "/snapshot") result = world.comparisons();
        else if (request.url === "/init") result = { ...publicProjection(), initialStorage };
        else if (request.url === "/io" && typeof publicMethods[args.method] === "function") result = await publicMethods[args.method](args.input);
        else { response.writeHead(403); response.end(JSON.stringify({ error: "world method unavailable" })); return; }
        response.writeHead(200, { "content-type": "application/json" }); response.end(JSON.stringify(wire({ result: result ?? null })));
      } catch (error: any) { response.writeHead(400); response.end(JSON.stringify({ error: String(error?.message ?? error) })); }
    });
  };
}
http.createServer(handler(false)).listen(config.public_socket);
http.createServer(handler(true)).listen(config.private_socket);
