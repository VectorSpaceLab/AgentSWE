#!/usr/bin/env node
// Offline Chromium DevTools Protocol harness for the v4 review API.

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeSync } from "node:fs";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import path from "node:path";

const [reviewArg, manifestArg] = process.argv.slice(2);
if (!reviewArg || !manifestArg) {
  console.error("usage: node validate_viewer_interaction.mjs REVIEW_HTML REVIEW_MANIFEST_JSON");
  process.exit(1);
}

const candidates = [
  process.env.CHROMIUM,
  process.env.CHROME,
  "chromium",
  "chromium-browser",
  "google-chrome",
  "google-chrome-stable",
].filter(Boolean);
let executable = null;
for (const candidate of candidates) {
  const probe = spawnSync(candidate, ["--version"], { encoding: "utf8" });
  if (probe.status === 0) {
    executable = candidate;
    break;
  }
}
if (!executable) {
  console.log(JSON.stringify({ valid: false, skipped: true, reason: "no compatible Chromium-family executable found" }, null, 2));
  process.exit(2);
}

const manifest = JSON.parse(readFileSync(manifestArg, "utf8"));
const claimIds = Object.keys(manifest.claim_targets || {});
const evidenceIds = Object.keys(manifest.evidence_targets || {});
if (!claimIds.length || !evidenceIds.length) throw new Error("manifest has no claim/evidence targets");
const profile = mkdtempSync(path.join(tmpdir(), "docqa-v4-chromium-"));
const child = spawn(executable, [
  "--headless=new",
  "--disable-gpu",
  "--no-sandbox",
  "--disable-background-networking",
  "--disable-default-apps",
  "--disable-extensions",
  "--disable-sync",
  "--metrics-recording-only",
  "--no-first-run",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  "about:blank",
], { stdio: ["ignore", "pipe", "pipe"] });
let cleaned = false;
function cleanup() {
  if (cleaned) return;
  cleaned = true;
  try { child.kill("SIGTERM"); } catch {}
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
process.on("exit", cleanup);
process.on("SIGINT", () => process.exit(130));
process.on("SIGTERM", () => process.exit(143));

let stderr = "";
const endpoint = await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("Chromium did not expose DevTools endpoint")), 15000);
  child.stderr.on("data", chunk => {
    stderr += chunk.toString();
    const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
    if (match) {
      clearTimeout(timer);
      resolve(match[1]);
    }
  });
  child.on("exit", code => reject(new Error(`Chromium exited early with ${code}: ${stderr}`)));
});

const browserSocket = new WebSocket(endpoint);
await new Promise((resolve, reject) => {
  browserSocket.addEventListener("open", resolve, { once: true });
  browserSocket.addEventListener("error", reject, { once: true });
});
let sequence = 0;
const pending = new Map();
browserSocket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
  }
});
const browserCall = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  pending.set(id, { resolve, reject });
  browserSocket.send(JSON.stringify({ id, method, params }));
});
const target = await browserCall("Target.createTarget", { url: "about:blank" });
const targetInfo = await browserCall("Target.attachToTarget", { targetId: target.targetId, flatten: true });
const sessionId = targetInfo.sessionId;
const events = [];
const observedSessions = new Set([sessionId]);
const boundaryErrors = [];
browserSocket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (observedSessions.has(message.sessionId) && message.method) events.push(message);
});
const callInSession = (sid, method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  pending.set(id, { resolve, reject });
  browserSocket.send(JSON.stringify({ id, method, params, sessionId: sid }));
});
const sessionCall = (method, params = {}) => callInSession(sessionId, method, params);
await sessionCall("Page.enable");
await sessionCall("Runtime.enable");
await sessionCall("Network.enable");
await sessionCall('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
// Trusted health probe runs on about:blank before any Candidate JavaScript.
// A fontless browser may report zero-height text while still exposing a DOM.
const fontHealth = await evaluate(`(()=>{
  const el=document.createElement('span'); el.textContent='Visible ABC 123';
  el.style.cssText='font:16px sans-serif;display:inline-block;white-space:nowrap';
  document.body.appendChild(el); const rect=el.getBoundingClientRect();
  const result={width:rect.width,height:rect.height}; el.remove(); return result;
})()`);
if (!(fontHealth.width > 10 && fontHealth.height > 5)) {
  console.log(JSON.stringify({valid:false,skipped:true,reason:'trusted browser cannot render ordinary text',font_health:fontHealth},null,2));
  try { await browserCall('Browser.close'); } catch {}
  browserSocket.close(); cleanup(); process.exit(2);
}
const reviewUrl = pathToFileURL(path.resolve(reviewArg)).href;
// Block before navigation, not after recording an already completed request.
await sessionCall("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
browserSocket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (!observedSessions.has(message.sessionId) || message.method !== "Fetch.requestPaused") return;
  const { requestId, request } = message.params;
  const allowed = (message.sessionId === sessionId && request.url === reviewUrl) || /^(?:data:|blob:|about:)/.test(request.url);
  if (!allowed) boundaryErrors.push(request.url);
  callInSession(message.sessionId, allowed ? "Fetch.continueRequest" : "Fetch.failRequest",
    allowed ? { requestId } : { requestId, errorReason: "BlockedByClient" }).catch(() => {});
});
// Pause new pages/workers before their first script; apply the same boundary
// recursively so window.open cannot escape the main-page interception.
browserSocket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.method !== "Target.attachedToTarget") return;
  const sid = message.params.sessionId;
  if (sid === sessionId) return;
  observedSessions.add(sid);
  (async () => {
    await callInSession(sid, "Network.enable");
    await callInSession(sid, "Fetch.enable", { patterns: [{ urlPattern: "*" }] });
    await callInSession(sid, "Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
    await callInSession(sid, "Runtime.runIfWaitingForDebugger");
  })().catch(error => boundaryErrors.push("secondary target boundary unavailable: " + String(error)));
});
await browserCall("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
await sessionCall("Page.navigate", { url: reviewUrl });
await new Promise(resolve => setTimeout(resolve, 1000));

async function evaluate(expression) {
  const result = await sessionCall("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}

const errors = [];
const initialStyles = await evaluate(`(()=>Object.fromEntries(${JSON.stringify([...Object.values(manifest.claim_targets), ...Object.values(manifest.evidence_targets)].map(t=>t.dom_id))}.map(id=>{
  const el=document.getElementById(id);return [id,el?[el,...el.querySelectorAll('*')].map(node=>node.getAttribute('style')):[]];
})))()`);
let interaction;
try {
interaction = await evaluate(`(async()=>{
  const api=window.__reviewTestApi;
  if(!api||typeof api.selectClaim!=='function'||typeof api.selectEvidence!=='function'||typeof api.getState!=='function') throw new Error('missing or incomplete __reviewTestApi');
  const manifest=${JSON.stringify(manifest)};
  const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
  const visible=id=>{
    const el=document.getElementById(id); if(!el) return false;
    const style=getComputedStyle(el), rect=el.getBoundingClientRect();
    return style.display!=='none'&&style.visibility!=='hidden'&&style.opacity!=='0'&&rect.width>0&&rect.height>0;
  };
  const selected=id=>{const el=document.getElementById(id);return visible(id)&&(el.getAttribute('aria-selected')==='true'||el.getAttribute('data-selected')==='true'||el.classList.contains('is-selected'));};
  const meaningful=id=>{const el=document.getElementById(id);if(!visible(id))return false;return (el.innerText||el.textContent||'').trim().length>0||!!el.querySelector('img,object,iframe,canvas,svg,table,pre,a[download]');};
  const appearance=el=>[el,...el.querySelectorAll('*')].slice(0,200).map(node=>{
    const read=pseudo=>{const s=getComputedStyle(node,pseudo);return ['color','backgroundColor','borderColor','borderWidth','outlineColor','outlineWidth','boxShadow','fontWeight','textDecorationLine','opacity','visibility','display','content'].map(k=>s[k]);};
    return [read(null),read('::before'),read('::after')];
  });
  const initialAppearance=new Map();
  for(const t of [...Object.values(manifest.claim_targets),...Object.values(manifest.evidence_targets)]){
    const el=document.getElementById(t.dom_id);if(el)initialAppearance.set(t.dom_id,JSON.stringify(appearance(el)));
  }
  const visiblyMarked=id=>{
    const el=document.getElementById(id);if(!selected(id))return false;
    const before=JSON.stringify(appearance(el));
    const attrs=[...el.attributes].map(a=>[a.name,a.value]);
    el.setAttribute('aria-selected','false');el.setAttribute('data-selected','false');
    for(const name of [...el.classList])if(/select|active|highlight/i.test(name))el.classList.remove(name);
    const plain=JSON.stringify(appearance(el));
    for(const a of [...el.attributes])el.removeAttribute(a.name);
    for(const [k,v] of attrs)el.setAttribute(k,v);
    return before!==plain||before!==initialAppearance.get(id);
  };
  const domSet=targets=>Object.keys(targets).filter(id=>selected(targets[id].dom_id));
  const domObservation=()=>({claims:domSet(manifest.claim_targets),evidence:domSet(manifest.evidence_targets),
    visiblyMarkedClaims:domSet(manifest.claim_targets).filter(id=>visiblyMarked(manifest.claim_targets[id].dom_id)),
    visiblyMarkedEvidence:domSet(manifest.evidence_targets).filter(id=>visiblyMarked(manifest.evidence_targets[id].dom_id))});
  const sourceProof=async et=>{
    const target=document.getElementById(et.source_dom_id);
    if(!target) return {metadata:false,download:false,sha256:null};
    let locator=null; try{locator=JSON.parse(target.getAttribute('data-native-locator')||'null');}catch{}
    const metadata=target.getAttribute('data-source-id')===et.source_id&&target.getAttribute('data-source-sha256')===et.source_sha256&&JSON.stringify(canonical(locator))===JSON.stringify(canonical(et.native_locator));
    const control=target.querySelector('[data-source-download="true"]');
    const rawUrl=control&&(control.getAttribute('href')||control.getAttribute('src')||control.getAttribute('data'));
    if(!rawUrl) return {metadata,download:false,sha256:null};
    try{
      let bytes;
      if(rawUrl.startsWith('data:')){
        const comma=rawUrl.indexOf(','); if(comma<0) throw new Error('invalid data URL');
        const header=rawUrl.slice(0,comma), payload=rawUrl.slice(comma+1);
        if(/;base64(?:;|$)/i.test(header)){
          const binary=atob(payload); const array=new Uint8Array(binary.length);
          for(let index=0;index<binary.length;index++) array[index]=binary.charCodeAt(index);
          bytes=array.buffer;
        }else{
          bytes=new TextEncoder().encode(decodeURIComponent(payload)).buffer;
        }
      }else{
        const response=await fetch(rawUrl); bytes=await response.arrayBuffer();
      }
      const digest=await crypto.subtle.digest('SHA-256',bytes);
      const sha256=[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('');
      const style=getComputedStyle(target), rect=target.getBoundingClientRect();
      return {metadata,download:true,sha256, target_observation:{
        id:target.id, selected:target.getAttribute('aria-selected'), display:style.display,
        visibility:style.visibility, opacity:style.opacity, width:rect.width, height:rect.height,
        native_locator:locator}};
    }catch(error){return {metadata,download:false,sha256:null,error:String(error)};}
  };
  const claimChecks=[];
  for(const claimId of Object.keys(manifest.claim_targets)){
    await api.selectClaim(claimId);
    const state=api.getState(), ct=manifest.claim_targets[claimId], eid=state.selected_evidence_id, et=eid?manifest.evidence_targets[eid]:null;
    claimChecks.push({claimId,state,dom:domObservation(),hash:location.hash,claimVisible:selected(ct.dom_id),evidenceVisible:!!et&&selected(et.dom_id),sourceVisible:!!et&&selected(et.source_dom_id),sourceMeaningful:!!et&&meaningful(et.source_dom_id),sourceProof:et?await sourceProof(et):null});
  }
  const evidenceChecks=[];
  for(const evidenceId of Object.keys(manifest.evidence_targets)){
    await api.selectEvidence(evidenceId);
    const state=api.getState(), et=manifest.evidence_targets[evidenceId], claimId=state.selected_claim_id, ct=claimId?manifest.claim_targets[claimId]:null;
    evidenceChecks.push({evidenceId,state,dom:domObservation(),hash:location.hash,claimVisible:!!ct&&selected(ct.dom_id),evidenceVisible:selected(et.dom_id),sourceVisible:selected(et.source_dom_id),sourceMeaningful:meaningful(et.source_dom_id),sourceProof:await sourceProof(et)});
  }
  return {claimChecks,evidenceChecks};
})()`);
} catch (error) {
  console.log(JSON.stringify({ valid: false, skipped: false,
    errors: ["candidate review interaction failed: " + String(error)],
    checked_claims: 0, checked_evidence: 0 }, null, 2));
  try { await browserCall("Browser.close"); } catch {}
  browserSocket.close();
  cleanup();
  process.exit(1);
}

const sameIds = (actual, expected) => Array.isArray(actual) && actual.length === expected.length && [...actual].sort().join("\u0000") === [...expected].sort().join("\u0000");
const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
const sameLocator=(a,b)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
const fragmentHas = (fragment, claimId, evidenceId) => {
  try {
    const decoded = decodeURIComponent(fragment || "");
    return decoded.includes(claimId) && decoded.includes(evidenceId);
  } catch {
    return false;
  }
};
for (const check of interaction.claimChecks) {
  const ct = manifest.claim_targets[check.claimId];
  const eid = check.state?.selected_evidence_id;
  const et = eid ? manifest.evidence_targets[eid] : null;
  if (check.state?.selected_claim_id !== check.claimId) errors.push(`selectClaim(${check.claimId}) returned the wrong claim ID`);
  if (!et || !ct.evidence_ids.includes(eid)) errors.push(`selectClaim(${check.claimId}) did not select reciprocal evidence`);
  if (!sameIds(check.state?.highlighted_claim_ids, [check.claimId])) errors.push(`selectClaim(${check.claimId}) highlighted_claim_ids is incomplete or wrong`);
  if (!sameIds(check.state?.highlighted_evidence_ids, ct.evidence_ids)) errors.push(`selectClaim(${check.claimId}) highlighted_evidence_ids is incomplete or wrong`);
  if (!sameIds(check.dom.claims, [check.claimId]) || !sameIds(check.dom.evidence, ct.evidence_ids)) errors.push(`selectClaim(${check.claimId}) actual DOM reciprocal selection set is incomplete or wrong`);
  if (!sameIds(check.dom.visiblyMarkedClaims, [check.claimId]) || !sameIds(check.dom.visiblyMarkedEvidence, ct.evidence_ids)) errors.push(`selectClaim(${check.claimId}) lacks visible reciprocal highlighting`);
  if (check.state?.status !== ct.status || check.state?.confidence !== ct.confidence) errors.push(`selectClaim(${check.claimId}) status/confidence mismatch`);
  if (et && check.state?.relation !== et.relation) errors.push(`selectClaim(${check.claimId}) relation mismatch`);
  if (et && (check.state?.source_id !== et.source_id || check.state?.source_sha256 !== et.source_sha256)) errors.push(`selectClaim(${check.claimId}) source/hash mismatch`);
  if (et && !sameLocator(check.state?.native_locator, et.native_locator)) errors.push(`selectClaim(${check.claimId}) native locator mismatch`);
  if (!check.claimVisible || !check.evidenceVisible || !check.sourceVisible || !check.sourceMeaningful) errors.push(`selectClaim(${check.claimId}) lacks a visible meaningful reciprocal target`);
  if (et && (!check.sourceProof?.metadata || !check.sourceProof?.download || check.sourceProof?.sha256 !== et.source_sha256)) errors.push(`selectClaim(${check.claimId}) source target does not expose exact bundled bytes`);
  if (check.state?.url_fragment !== check.hash || !et || !fragmentHas(check.hash, check.claimId, eid)) errors.push(`selectClaim(${check.claimId}) URL fragment mismatch`);
}
for (const check of interaction.evidenceChecks) {
  const et = manifest.evidence_targets[check.evidenceId];
  const claimId = check.state?.selected_claim_id;
  const ct = claimId ? manifest.claim_targets[claimId] : null;
  if (check.state?.selected_evidence_id !== check.evidenceId) errors.push(`selectEvidence(${check.evidenceId}) returned the wrong evidence ID`);
  if (!ct || !et.claim_ids.includes(claimId)) errors.push(`selectEvidence(${check.evidenceId}) did not select reciprocal claim`);
  if (!sameIds(check.state?.highlighted_claim_ids, et.claim_ids)) errors.push(`selectEvidence(${check.evidenceId}) highlighted_claim_ids is incomplete or wrong`);
  if (!sameIds(check.state?.highlighted_evidence_ids, [check.evidenceId])) errors.push(`selectEvidence(${check.evidenceId}) highlighted_evidence_ids is incomplete or wrong`);
  if (!sameIds(check.dom.claims, et.claim_ids) || !sameIds(check.dom.evidence, [check.evidenceId])) errors.push(`selectEvidence(${check.evidenceId}) actual DOM reciprocal selection set is incomplete or wrong`);
  if (!sameIds(check.dom.visiblyMarkedClaims, et.claim_ids) || !sameIds(check.dom.visiblyMarkedEvidence, [check.evidenceId])) errors.push(`selectEvidence(${check.evidenceId}) lacks visible reciprocal highlighting`);
  if (ct && (check.state?.status !== ct.status || check.state?.confidence !== ct.confidence)) errors.push(`selectEvidence(${check.evidenceId}) status/confidence mismatch`);
  if (check.state?.relation !== et.relation) errors.push(`selectEvidence(${check.evidenceId}) relation mismatch`);
  if (check.state?.source_id !== et.source_id || check.state?.source_sha256 !== et.source_sha256) errors.push(`selectEvidence(${check.evidenceId}) source/hash mismatch`);
  if (!sameLocator(check.state?.native_locator, et.native_locator)) errors.push(`selectEvidence(${check.evidenceId}) native locator mismatch`);
  if (!check.claimVisible || !check.evidenceVisible || !check.sourceVisible || !check.sourceMeaningful) errors.push(`selectEvidence(${check.evidenceId}) lacks a visible meaningful reciprocal target`);
  if (!check.sourceProof?.metadata || !check.sourceProof?.download || check.sourceProof?.sha256 !== et.source_sha256) errors.push(`selectEvidence(${check.evidenceId}) source target does not expose exact bundled bytes`);
  if (!ct || check.state?.url_fragment !== check.hash || !fragmentHas(check.hash, claimId, check.evidenceId)) errors.push(`selectEvidence(${check.evidenceId}) URL fragment mismatch`);
}

await new Promise(resolve => setTimeout(resolve, 250));
const requestedUrls = events
  .filter(event => event.method === "Network.requestWillBeSent")
  .map(event => event.params.request.url);
const blocked = [...boundaryErrors, ...requestedUrls.filter(url => url !== reviewUrl && !/^(?:data:|blob:|about:)/.test(url))];
if (blocked.length) errors.push(`review attempted external or secondary file requests: ${[...new Set(blocked)].join(", ")}`);
const runtimeExceptions = events.filter(event => event.method === "Runtime.exceptionThrown");
if (runtimeExceptions.length) errors.push(`review raised ${runtimeExceptions.length} uncaught runtime exception(s)`);

const screenshots=[];
const pixelChecks=[];
// Decode only trusted CDP screenshots in an isolated JS world. Candidate
// overrides of canvas, Blob or ImageBitmap cannot supply the measured pixels.
const frameTree=await sessionCall('Page.getFrameTree');
const pixelWorld=await sessionCall('Page.createIsolatedWorld',{frameId:frameTree.frameTree.frame.id,worldName:'trusted-pixel-comparison'});
async function pixelDifference(first, second) {
  const result=await sessionCall('Runtime.evaluate',{contextId:pixelWorld.executionContextId,awaitPromise:true,returnByValue:true,
    expression:`(async()=>{
      const decode=async encoded=>{const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));const bitmap=await createImageBitmap(new Blob([bytes],{type:'image/png'}));const canvas=new OffscreenCanvas(bitmap.width,bitmap.height);const ctx=canvas.getContext('2d');ctx.drawImage(bitmap,0,0);return ctx.getImageData(0,0,bitmap.width,bitmap.height).data;};
      const a=await decode(${JSON.stringify(first)}),b=await decode(${JSON.stringify(second)});if(a.length!==b.length)throw new Error('pixel dimensions changed');
      let changed=0;for(let i=0;i<a.length;i+=4)if(Math.max(Math.abs(a[i]-b[i]),Math.abs(a[i+1]-b[i+1]),Math.abs(a[i+2]-b[i+2]))>8)changed++;
      return {changed_pixels:changed,total_pixels:a.length/4};
    })()`});
  if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
if (!errors.length) {
  for (const [kind, targets] of [['claim',manifest.claim_targets],['evidence',manifest.evidence_targets]]) {
    for (const [id,target] of Object.entries(targets)) {
      const setup=await evaluate(`(async()=>{
        await window.__reviewTestApi.${kind==='claim'?'selectClaim':'selectEvidence'}(${JSON.stringify(id)});
        const el=document.getElementById(${JSON.stringify(target.dom_id)});el.scrollIntoView({block:'center',inline:'nearest'});
        const r=el.getBoundingClientRect();return {attrs:[el,...el.querySelectorAll('*')].map(node=>[...node.attributes].map(a=>[a.name,a.value])),clip:{x:Math.max(0,r.x+scrollX-4),y:Math.max(0,r.y+scrollY-4),width:Math.min(1440,r.width+8),height:Math.min(1000,r.height+8),scale:1}};
      })()`);
      const capture=async()=>(await sessionCall('Page.captureScreenshot',{format:'png',clip:setup.clip,captureBeyondViewport:true})).data;
      const selected=await capture();
      await evaluate(`(()=>{const el=document.getElementById(${JSON.stringify(target.dom_id)});const styles=${JSON.stringify(initialStyles[target.dom_id] || [])};[el,...el.querySelectorAll('*')].forEach((node,index)=>{
        node.setAttribute('aria-selected','false');node.setAttribute('data-selected','false');for(const name of [...node.classList])if(/select|active|highlight/i.test(name))node.classList.remove(name);
        if(styles[index]===null||styles[index]===undefined)node.removeAttribute('style');else node.setAttribute('style',styles[index]);
      });})()`);
      const plain=await capture();
      await evaluate(`(()=>{const el=document.getElementById(${JSON.stringify(target.dom_id)});const attrs=${JSON.stringify(setup.attrs)};[el,...el.querySelectorAll('*')].forEach((node,index)=>{for(const a of [...node.attributes])node.removeAttribute(a.name);for(const [k,v] of attrs[index]||[])node.setAttribute(k,v);});})()`);
      const restored=await capture();
      const difference=await pixelDifference(selected,plain);
      const instability=await pixelDifference(selected,restored);
      const visible=difference.changed_pixels>=16&&difference.changed_pixels>instability.changed_pixels*2;
      pixelChecks.push({kind,id,...difference,unstable_pixels:instability.changed_pixels,visible});
      if(!visible)errors.push(`${kind}(${id}) selection has no stable visible pixel highlighting`);
    }
  }
}
const kinds=new Set();
if (!errors.length) {
  for (const [id, target] of Object.entries(manifest.evidence_targets)) {
    const kind=target.native_locator?.kind;
    if(kinds.has(kind))continue;
    kinds.add(kind);
    await evaluate(`(async()=>{await window.__reviewTestApi.selectEvidence(${JSON.stringify(id)});document.getElementById(${JSON.stringify(target.source_dom_id)}).scrollIntoView({block:'start'});})()`);
    await new Promise(resolve=>setTimeout(resolve,100));
    const result=await sessionCall('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    screenshots.push({evidence_id:id,source_id:target.source_id,native_locator:target.native_locator,mime_type:'image/png',bytes_base64:result.data});
  }
}
writeSync(1, JSON.stringify({ valid: errors.length === 0, skipped: false, executable, errors, checked_claims: claimIds.length, checked_evidence: evidenceIds.length, requested_urls: requestedUrls, interaction_checks: interaction, pixel_checks:pixelChecks, screenshots }, null, 2) + '\n');
try { await browserCall("Browser.close"); } catch {}
browserSocket.close();
cleanup();
process.exit(errors.length ? 1 : 0);
