import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createCaseWorld } from '../../lower_agent/case_world.ts';

class Storage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, value); }
  removeItem(key) { this.values.delete(key); }
  key(index) { return [...this.values.keys()][index] ?? null; }
  get length() { return this.values.size; }
}
const fixture = async id => JSON.parse(await readFile(new URL(`../../test_cases/${id}/assets/fixtures.json`, import.meta.url)));
const make = async (id, nonce='a'.repeat(32)) => {
  const storage = new Storage();
  return [createCaseWorld(id, nonce, await fixture(id), storage, (conversation, revision) => `openhands:recovery:v2:${conversation}:${revision}`), storage];
};

const [conflict] = await make('test_001');
const [conflict2] = await make('test_001', 'b'.repeat(32));
assert.notDeepEqual(conflict.baseManifest, conflict2.baseManifest, 'nonce must alter real input world');
assert.equal(conflict.comparisons().comparisons.conflict_present_in_input, true);
assert.equal(conflict.comparisons().comparisons.conflicting_replicas_preserved, true);
assert.equal(conflict.comparisons().automatically_computed_score, null);

const [loss] = await make('test_002');
loss.beforeAction('reconcile_production_workspace', { checkpoint: { runGeneration: 2 } });
assert.equal(loss.authority.current().runGeneration, 2, 'browser authority follows actual acquired ledger generation');
assert.equal(loss.workspaceFault().crash_after, 'after_plan');
assert.ok(!JSON.stringify(loss.visibleNotice()).includes('after_plan'), 'internal fault schedule is not a model prompt');
const local = await loss.local.capture(loss.scope);
const remote = await loss.transport.readHead(loss.scope);
const localOnly = local.entries.find(entry => entry.path !== 'README.md');
const bytes = await loss.local.readChunk({ ...loss.scope, digest: localOnly.contentDigest });
await assert.rejects(loss.transport.writeChunk({ ...loss.scope, digest: localOnly.contentDigest, bytes }), /response lost/);
assert.deepEqual(await loss.transport.missingChunks({ ...loss.scope, digests: [localOnly.contentDigest] }), [], 'lost response must persist actual bytes');
await assert.rejects(loss.transport.writeChunk({ ...loss.scope, digest: localOnly.contentDigest, bytes: new Uint8Array([0]) }), /digest mismatch/);
const target = { ...remote, revision: 'merged', entries: [...remote.entries, localOnly] };
const commit = { ...loss.scope, transactionId: 'tx-loss', expectedRevision: remote.revision, targetManifest: target };
await assert.rejects(loss.transport.commit(commit), /response lost/);
assert.equal((await loss.transport.reconcileCommit(commit)).revision, 'merged');
assert.equal((await loss.transport.commit(commit)).revision, 'merged');
assert.equal(loss.comparisons().observed.durable_remote_commits.length, 1, 'retry cannot duplicate durable commit');
await loss.local.apply({ ...loss.scope, transactionId: 'tx-loss', expectedRevision: local.revision, targetManifest: target });
assert.equal(loss.comparisons().comparisons.replica_entries_converged, true);

const [aba] = await make('test_003');
const checkpoint = { revision: 7, runGeneration: 2, lease: { ownerId: 'old', epoch: 2, fencingToken: 'synthetic-private-fence' } };
aba.beforeAction('resume', { checkpoint });
const delivered = [];
await aba.afterAction('resume', { ingestRuntimeEvent(event) { delivered.push(event); return { accepted: false }; } }, { checkpoint: { ...checkpoint, runGeneration: 3 } });
assert.equal(delivered.length, 1);
assert.equal(delivered[0].runGeneration, 2, 'external callback must carry stale generation');
assert.equal(aba.comparisons().observed.operations[0].operation, 'fault.delayed_callback_delivered');

const [asyncAba] = await make('test_003');
let settled = false;
const held = asyncAba.executeEffect({ effect: { idempotencyKey: 'real-delayed-callback' } }).then(value => { settled = true; return value; });
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(settled, false, 'actual effect response must remain in flight across model decisions');
assert.equal(asyncAba.visibleNotice().pending_external_callbacks, 1);
await asyncAba.afterAction('resume', {}, {});
assert.equal((await held).idempotencyKey, 'real-delayed-callback');
assert.equal(asyncAba.visibleNotice().pending_external_callbacks, 0);

const [grants] = await make('test_004');
const denied = [];
await grants.afterAction('issue_inspection_grant', { inspectRecoveryAuthorized(input) { denied.push(input); return { access: 'denied', checkpoint: null }; } }, { token: 'synthetic-private-grant' });
assert.equal(denied.length, 4, 'wrong-tab, foreign-backend and wrong-conversation probes (0919) plus the inflated-caller-limit probe (0920)');
assert.notEqual(denied[0].tabId, grants.visibleNotice().observer_tab);
assert.notEqual(denied[1].backendId, grants.scope.backendId);
assert.ok(!JSON.stringify(grants.visibleNotice()).includes('synthetic-private-grant'));

const [expiry] = await make('test_004');
const expiresAt = expiry.now() + 5;
const attempts = [];
const grantAdapter = { inspectRecoveryAuthorized(input) { attempts.push(input); return { access: input.now > expiresAt ? 'denied' : 'support' }; } };
await expiry.afterAction('issue_inspection_grant', grantAdapter, { token: 'synthetic-expiring-grant', grant: { expiresAt } });
await expiry.afterAction('inspect_recovery_authorized', grantAdapter, { access: 'support' });
assert.equal(attempts.at(-1).now, expiresAt + 1, 'expiry incident must advance actual runtime clock');
assert.equal(expiry.comparisons().observed.external_callbacks.find(item => item.source === 'after-expiry').projection.access, 'denied');

const [migration, storage] = await make('test_005');
assert.equal(migration.scope.backendId, 'default-local');
assert.equal(storage.length, 4, 'legacy + separate foreign namespaces must be real storage records');
assert.ok(migration.comparisons().comparisons.namespace_preservation.every(item => item.unchanged));
storage.removeItem(storage.key(1));
assert.ok(migration.comparisons().comparisons.namespace_preservation.some(item => !item.unchanged), 'comparison must detect observed namespace damage');

const [production] = await make('test_006');
const syncCheckpoint = { revision: 2, eventCursor: 7, runGeneration: 2,
  lease: { ownerId: 'owner', epoch: 1, fencingToken: 'private-fixture-fence' } };
production.beforeAction('sync_production', { checkpoint: syncCheckpoint });
const syncRequest = { ...production.scope, term: 1, tabId: 'owner-tab', incarnationId: 'first', sinceEventCursor: 7 };
await assert.rejects(production.transport.pull(syncRequest), /response lost/);
let syncSettled = false;
const oldSync = production.transport.pull(syncRequest).then(value => { syncSettled = true; return value; });
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(syncSettled, false, 'second actual response stays in flight until model-selected freeze');
const oldTerm = production.authority.current().term;
const actualLeaseEnd = production.now() + 500;
await production.afterAction('freeze_production_sync', {}, { projection: { leaseExpiresAt: actualLeaseEnd } });
assert.equal(production.authority.current().term, oldTerm + 1);
assert.equal(production.now(), actualLeaseEnd + 1, 'freeze incident must cross actual product lease expiry, not an arbitrary delay');
const syncResponse = await oldSync;
assert.equal(syncResponse.events[0].recoveryEvent.eventId, syncResponse.events[1].recoveryEvent.eventId);
assert.notEqual(syncResponse.events[0].ordinaryEvent.id, syncResponse.events[1].ordinaryEvent.id);
assert.equal(production.comparisons().comparisons.sync_retry_requested_same_cursor, true);
const firstIncarnation = production.authority.current().incarnationId;
await production.afterAction('restart_product', {}, {});
assert.notEqual(production.authority.current().incarnationId, firstIncarnation);
// --- 0921: the world shim must expose every method the probes call -----------
// world_service.ts hands case_world.ts an "adapter".  A method the world calls
// but the shim does not proxy makes the probe record "the Candidate adapter
// exposes no <method>" and books the forfeit against the product.
const worldServiceSource = await readFile(new URL('../../lower_agent/world_service.ts', import.meta.url), 'utf8');
const caseWorldSource = await readFile(new URL('../../lower_agent/case_world.ts', import.meta.url), 'utf8');
const shimMethods = new Set([...worldServiceSource.matchAll(/"([A-Za-z_$][A-Za-z0-9_$]*)"/g)]
  .map(match => match[1]));
const calledMethods = [...new Set([...caseWorldSource.matchAll(/\badapter\.([A-Za-z_$][A-Za-z0-9_$]*)/g)]
  .map(match => match[1]))].sort();
assert.ok(calledMethods.includes('enqueueEffect'), 'the outbox probe must still call enqueueEffect');
const missingFromShim = calledMethods.filter(name => !worldServiceSource.includes(`"${name}"`));
assert.deepEqual(missingFromShim, [], 'world_service.ts must proxy every adapter method case_world.ts calls');
const productSource = await readFile(new URL('../../lower_agent/isolated_product.py', import.meta.url), 'utf8');
for (const name of ['ingestRuntimeEvent', 'inspectRecovery', 'inspectRecoveryAuthorized', 'enqueueEffect']) {
  assert.ok(productSource.includes(`"${name}"`), `the product callback must route ${name} to the Candidate adapter`);
}

// --- 0921: a probe that could not run must re-arm ----------------------------
const leaseCheckpoint = (lease) => ({ revision: 3, eventCursor: 1, runGeneration: 1, terminalState: 'active',
  pendingEffects: [], completedEffects: [], recentEventIds: [],
  lease: lease ? { ownerId: 'owner-1', epoch: 1, fencingToken: 'fence-1', expiresAt: 10000 } : undefined });
const probeAdapter = (lease) => ({
  inspectRecovery: () => ({ checkpoint: leaseCheckpoint(lease) }),
  inspectRecoveryAuthorized: () => ({ access: 'denied' }),
  ingestRuntimeEvent: () => ({ accepted: true }),
  enqueueEffect: () => ({ queued: true }),
});
const [rearm] = await make('test_001');
await rearm.afterAction('acquire_recovery_lease', probeAdapter(false), { checkpoint: leaseCheckpoint(true) });
assert.equal(rearm.comparisons().comparisons.runtime_event_discipline.attempted, false,
  'a probe with no live leased checkpoint still records that it could not run');
assert.equal(rearm.comparisons().comparisons.effect_delivery_probe.attempted, false);
await rearm.afterAction('recover_conversation', probeAdapter(true), { checkpoint: leaseCheckpoint(true) });
assert.equal(rearm.comparisons().comparisons.runtime_event_discipline.attempted, true,
  'the discipline probe must re-arm on the next lease-bearing action instead of forfeiting the outcome');
assert.equal(rearm.comparisons().comparisons.effect_delivery_probe.attempted, true,
  'the durable-outbox probe must run once a live leased checkpoint exists');

console.log(JSON.stringify({ provider_free: true, model_calls: 0, cases: 6, checks: 'dynamic content, lost responses, ABA callbacks, foreign grants, real legacy storage, browser takeover', score: null }));
