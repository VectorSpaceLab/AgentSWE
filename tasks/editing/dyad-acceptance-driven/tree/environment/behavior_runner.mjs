/** Actual browser execution adapter. No case table or predetermined outcomes. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';

const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function stopOwnedChild(child) {
  // The process group can still contain browser descendants after its leader
  // exits. Each leader was created detached by this adapter; the enclosing
  // owned case cgroup is the final independent cleanup boundary.
  if (!child || !Number.isInteger(child.pid)) return;
  try { process.kill(-child.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  const end = Date.now() + 1500;
  while (child.exitCode === null && !child.signalCode && Date.now() < end) await sleep(20);
  try { process.kill(-child.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
}

function launch(command, args, options) {
  const child = spawn(command, args, { ...options, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', value => { stdout = (stdout + value.toString()).slice(-2_000_000); });
  child.stderr.on('data', value => { stderr = (stderr + value.toString()).slice(-2_000_000); });
  const done = new Promise(resolve => {
    child.once('error', error => resolve({ code: null, error: String(error), stdout, stderr }));
    child.once('close', (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
  return { child, done, output: () => ({ stdout, stderr }) };
}

function reportTests(report) {
  const found = [];
  const visit = suite => {
    for (const spec of suite.specs || []) for (const test of spec.tests || []) {
      const result = (test.results || []).at(-1) || {};
      found.push({ title: spec.title, status: result.status || test.status || 'missing',
        error: result.error?.message, duration_ms: result.duration });
    }
    for (const nested of suite.suites || []) visit(nested);
  };
  visit(report);
  return found;
}

export async function startAppServer(appDir, runtimeRoot) {
  fs.mkdirSync(runtimeRoot, {recursive: true});
  const server = launch(process.execPath, [path.join(appDir, 'server.mjs')], {
    cwd: appDir, env: {PATH: '/usr/bin:/bin', HOME: runtimeRoot, TMPDIR: runtimeRoot, LANG: 'C.UTF-8'}});
  const until = Date.now() + 8000;
  while (Date.now() < until) {
    for (const line of server.output().stdout.split('\n')) {
      let value; try { value = JSON.parse(line); } catch { continue; }
      if (value.listening === true && Number.isInteger(value.port)) return {
        child: server.child, baseUrl: 'http://127.0.0.1:' + value.port,
        close: () => stopOwnedChild(server.child)};
    }
    if (server.child.exitCode !== null || server.child.signalCode) break;
    await sleep(20);
  }
  await stopOwnedChild(server.child);
  throw new Error('Initial actual app server failed: ' + server.output().stderr.slice(-1000));
}

export async function runBehavioralTests({ appId, appDir, testFile, grep, runtimeRoot,
  nodeModules, browserExecutable, signal, lateCallbackAfterCancel = false,
  delayBeforeDeliveryMs = 0, timeoutMs = 90000, runOriginalTarget = false, isolateAppServer = false, serverAppDir = null }) {
  const started = Date.now();
  const source = path.resolve(appDir, testFile);
  if (!source.startsWith(path.resolve(appDir) + path.sep) || !fs.existsSync(source))
    throw new Error('actual browser target missing or outside the selected app');
  const target = fs.readFileSync(source);
  const run = path.join(runtimeRoot, crypto.randomUUID());
  fs.mkdirSync(run, { recursive: true });
  fs.symlinkSync(path.resolve(nodeModules), path.join(run, 'node_modules'), 'dir');
  const copiedTarget = path.join(run, 'actual-target.spec.ts');
  fs.writeFileSync(copiedTarget, target);
  const native = { engine: 'actual-playwright-chromium', app_id: appId, target_path: testFile,
    adapter_started_at_ms: started,
    target_sha256: digest(target), copied_target_sha256: digest(fs.readFileSync(copiedTarget)),
    browser_executable_sha256: digest(fs.readFileSync(browserExecutable)),
    app_source_sha256: Object.fromEntries(['index.html', 'server.mjs'].map(name =>
      [name, digest(fs.readFileSync(path.join(appDir, name)))])),
    predetermined_outcome: false, fixture_actions_credited_as_agent_actions: false };
  let server, runner;
  const env = { PATH: process.env.PATH || '/usr/bin:/bin', HOME: run, TMPDIR: run,
    LANG: 'C.UTF-8', CI: '1', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1' };
  try {
    const servedApp = serverAppDir || appDir;
    let serverCommand = process.execPath, serverArgs = [path.join(servedApp, 'server.mjs')];
    if (isolateAppServer) {
      serverCommand = '/usr/bin/bwrap';
      serverArgs = ['--die-with-parent', '--unshare-pid', '--unshare-ipc', '--cap-drop', 'ALL'];
      for (const directory of ['/usr', '/bin', '/lib', '/lib64'])
        if (fs.existsSync(directory)) serverArgs.push('--ro-bind', directory, directory);
      serverArgs.push('--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
        '--ro-bind', servedApp, servedApp, '--ro-bind', process.execPath, process.execPath,
        '--chdir', servedApp, process.execPath, path.join(servedApp, 'server.mjs'));
    }
    server = launch(serverCommand, serverArgs, { cwd: servedApp, env });
    native.app_server_private_spec_isolated = isolateAppServer;
    let address;
    const deadline = Date.now() + 8000;
    while (!address && Date.now() < deadline) {
      for (const line of server.output().stdout.split('\n')) {
        try { const value = JSON.parse(line); if (value.listening === true && Number.isInteger(value.port)) address = value; } catch {}
      }
      if (server.child.exitCode !== null || server.child.signalCode) break;
      if (!address) await sleep(20);
    }
    if (!address) throw new Error('actual app server did not become ready: ' + server.output().stderr.slice(-1000));
    native.app_base_url = 'http://127.0.0.1:' + address.port;
    const config = path.join(run, 'playwright.config.mjs');
    fs.writeFileSync(config, 'export default ' + JSON.stringify({
      testDir: runOriginalTarget ? appDir : run, testMatch: runOriginalTarget ? testFile : 'actual-target.spec.ts',
      timeout: Math.min(timeoutMs, 60000), expect: { timeout: 5000 }, fullyParallel: false, workers: 1, retries: 0,
      reporter: [['json', { outputFile: path.join(run, 'playwright-report.json') }]],
      use: { baseURL: native.app_base_url, headless: true, launchOptions: { executablePath: browserExecutable, args: ['--no-sandbox'] } } }) + ';\n');
    runner = launch(process.execPath, [path.join(nodeModules, 'playwright/cli.js'), 'test',
      '--config', config, '--grep', grep], { cwd: run, env });
    let timeout = false;
    const timer = setTimeout(() => { timeout = true; void stopOwnedChild(runner.child); }, timeoutMs);
    const abort = () => { if (!lateCallbackAfterCancel) void stopOwnedChild(runner.child); };
    signal?.addEventListener('abort', abort, { once: true });
    const actual = await runner.done;
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    fs.writeFileSync(path.join(run, 'playwright.stdout.log'), actual.stdout);
    fs.writeFileSync(path.join(run, 'playwright.stderr.log'), actual.stderr);
    const reportPath = path.join(run, 'playwright-report.json');
    const report = fs.existsSync(reportPath) ? JSON.parse(fs.readFileSync(reportPath, 'utf8')) : {};
    const tests = reportTests(report);
    const passed = actual.code === 0 && tests.length > 0 && tests.every(test => test.status === 'passed');
    native.actual_browser_completed_at = Date.now();
    native.actual_browser_exit_code = actual.code;
    native.actual_browser_signal = actual.signal;
    native.original_target_execution = runOriginalTarget;
    native.actual_test_count = tests.length;
    native.actual_test_results = tests;
    native.browser_launch_failed = tests.some(test => /browserType\.launch:/.test(test.error || ''));
    native.infrastructure_invalid = native.browser_launch_failed;
    native.actual_report_sha256 = fs.existsSync(reportPath) ? digest(fs.readFileSync(reportPath)) : null;
    native.actual_report_path = reportPath;
    native.browser_timeout = timeout;
    if (lateCallbackAfterCancel) {
      // Delay delivery of an actually computed result, never fabricate a pass.
      const until = Date.now() + Math.max(delayBeforeDeliveryMs, 30000);
      while (!signal?.aborted && Date.now() < until) await sleep(25);
      native.late_callback_after_cancel = signal?.aborted === true;
    } else if (delayBeforeDeliveryMs > 0) {
      const until = Date.now() + delayBeforeDeliveryMs;
      while (!signal?.aborted && Date.now() < until) await sleep(25);
    }
    native.signal_aborted_at_delivery = signal?.aborted === true;
    native.result_delivered_at_ms = Date.now();
    native.elapsed_ms = Date.now() - started;
    const cancelled = signal?.aborted && !lateCallbackAfterCancel;
    const status = cancelled ? 'cancelled' : passed ? 'passed' : 'failed';
    const value = { appId, results: [{ file: testFile, status, tests,
      error: passed && !cancelled ? undefined : cancelled ? 'Owned Tests cancellation' : 'Actual browser behavior assertions failed' }],
      native_behavior: native };
    fs.writeFileSync(path.join(run, 'native-browser-result.json'), JSON.stringify(value, null, 2) + '\n');
    return value;
  } finally {
    await stopOwnedChild(runner?.child);
    await stopOwnedChild(server?.child);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === new URL(import.meta.url).pathname) {
  const options = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  try {
    const result = await runBehavioralTests(options);
    fs.writeFileSync(process.argv[3], JSON.stringify(result, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ status: result.results[0].status,
      actual_test_count: result.native_behavior.actual_test_count }) + '\n');
  } catch (error) {
    process.stderr.write(String(error) + '\n');
    process.exitCode = 2;
  }
}
