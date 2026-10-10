"""Offline Dyad type compatibility in an attested, individually owned container."""
from __future__ import annotations
import argparse
import fcntl
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

IMAGE = '@@AGENTSWE_BUILDER_CODEX_IMAGE_ID@@'
MEMORY_BYTES = 4 * 1024 ** 3
TIMEOUT_SECONDS = 600
LABEL = 'agentswe.dyad.build-owner'
TYPE_COMMAND = ['npm', 'run', 'ts']
DOCKER_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
DIAGNOSTIC = re.compile(r'^(.+?)\((\d+),(\d+)\): error (TS\d+): (.*)$')
# These diagnostics cannot establish Candidate fault without dependency repair.
DEPENDENCY_CODES = {'TS2307', 'TS2688', 'TS2792', 'TS7016'}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def diagnostics(stdout):
    """Keep source/code/message and multiplicity; line shifts are not new errors."""
    result = []
    for line in stdout.splitlines():
        match = DIAGNOSTIC.match(line)
        if match:
            path, row, col, code, message = match.groups()
            path = path.removeprefix('/work/repository/').replace('\\', '/')
            result.append({'path': path, 'code': code, 'message': message,
                           'line': int(row), 'column': int(col)})
        elif line[:1].isspace() and result and line.strip():
            result[-1]['message'] += '\n' + line.rstrip()
    # A worker leaf can appear in npm run ts and again in its fallback.
    # Deduplicate only the exact location; separate failing locations still count.
    return list({json.dumps(item, sort_keys=True): item for item in result}.values())


def key(item):
    return json.dumps([item['path'], item['code'], item['message']], ensure_ascii=False)


def classify(result, baseline=None):
    value = dict(result)
    value.update(required_pass=True, ready_for_submission=False, passed=False)
    if result.get('classification') == 'infrastructure_invalid':
        return value
    resource = result.get('resource_attestation') or {}
    if not (resource.get('valid') is True and resource.get('cleanup', {}).get('complete') is True):
        value.update(classification='infrastructure_invalid', failure_class='build_resource_unverified')
        return value
    items = result.get('diagnostics') or []
    if result.get('exit_code') == 0:
        value.update(classification='baseline_typecheck_healthy' if baseline is None else 'ready_for_lower',
                     passed=True, ready_for_submission=True, environment_healthy=True)
        return value
    if result.get('timed_out') or resource.get('oom_killed') or result.get('exit_code') != 2 or not items:
        value.update(classification='infrastructure_invalid', failure_class='build_execution_unresolved')
        return value
    if any(item['code'] in DEPENDENCY_CODES or item['path'].startswith('node_modules/') for item in items):
        value.update(classification='infrastructure_invalid', failure_class='build_dependency_or_toolchain_failure')
        return value
    if baseline is None:
        value.update(classification='baseline_existing_type_errors', environment_healthy=True,
                     ready_for_submission=False, required_pass=False)
        return value
    if baseline.get('environment_healthy') is not True:
        value.update(classification='infrastructure_invalid', failure_class='baseline_environment_unhealthy')
        return value
    previous = Counter(key(item) for item in baseline.get('diagnostics', []))
    new = []
    for item in items:
        fingerprint = key(item)
        if previous[fingerprint]: previous[fingerprint] -= 1
        else: new.append(item)
    value['new_diagnostics'] = new
    if not new:
        value.update(classification='ready_for_lower', ready_for_submission=True,
                     compatibility='no new compiler diagnostics relative to authenticated baseline')
    else:
        value.update(classification='candidate_build_failure', failure_class='candidate_compile_diagnostics',
                     failure_attribution={'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                         'reason': 'New source compiler diagnostics under the same verified toolchain and dependencies.',
                         'evidence_paths': [result.get('result_path'), baseline.get('result_path')]})
    return value


def verify_container(record, cid, token):
    if record.get('Id') != cid or record.get('Image') != IMAGE or record.get('Config', {}).get('Labels', {}).get(LABEL) != token:
        raise RuntimeError('build container identity mismatch; refusing control')
    host = record['HostConfig']
    if (host.get('Memory') != MEMORY_BYTES or host.get('MemorySwap') != MEMORY_BYTES
            or host.get('NetworkMode') != 'none' or host.get('PidsLimit') != 512
            or host.get('ReadonlyRootfs') is not True or host.get('NanoCpus') != 8_000_000_000):
        raise RuntimeError('build container resource/isolation mismatch')


def run_typecheck(source, *, dependency_root, output, baseline=None, timeout=TIMEOUT_SECONDS):
    """600 seconds includes create, source/dependency preparation, compile and cleanup."""
    if not 20 < timeout <= TIMEOUT_SECONDS:
        raise ValueError('build timeout must be in (20, 600] seconds')
    started = time.monotonic()
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o755)
    evidence = output / 'evidence'; evidence.mkdir(mode=0o700)
    source = Path(source).resolve(); dependency_root = Path(dependency_root).resolve()
    token = uuid.uuid4().hex; cid = None; create_attempted = False
    container_name = 'agentswe-dyad-build-' + token
    record = {'schema_version': 'dyad-typecheck-v1', 'command': TYPE_COMMAND,
              'exit_code': None, 'timed_out': False, 'external_api_calls': 0,
              'result_path': str(output / 'typecheck.json')}
    attestation = {'valid': False, 'image': IMAGE, 'owner_token': token,
                   'timeout_seconds': timeout, 'memory_bytes': MEMORY_BYTES, 'swap_bytes': 0,
                   'budget_scope': 'entire container including source/dependency preparation, npm and descendants; cleanup included',
                   'network': 'none', 'cleanup': {'complete': False}, 'oom_killed': False,
                   'independent_deadline_monotonic': started + timeout - 12,
                   'independent_watchdog': 'kernel SIGALRM invokes trusted PID1 exit(124), terminating its subtree'}
    record['resource_attestation'] = attestation
    def docker(args, *, wait=None, check=True):
        remaining = timeout - 12 - (time.monotonic() - started)
        if remaining <= 0: raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.run(['docker', *args], capture_output=True, text=True,
                              timeout=min(wait or 20, remaining), check=check,
                              env=DOCKER_ENV)
    def inspect():
        return json.loads(docker(['inspect', cid]).stdout)[0]
    try:
        deps = dependency_root / 'node_modules'
        fake = dependency_root / 'testing/fake-llm-server/node_modules'
        if not deps.is_dir() or not fake.is_dir():
            raise RuntimeError('prepared root and fake-server dependencies must both exist')
        mounts = [('--mount', f'type=bind,src={source},dst=/source,readonly'),
                  ('--mount', f'type=bind,src={dependency_root},dst=/baseline,readonly'),
                  ('--mount', f'type=bind,src={deps.resolve()},dst=/prepared-root,readonly'),
                  ('--mount', f'type=bind,src={fake.resolve()},dst=/prepared-fake,readonly'),
                  ('--mount', f'type=bind,src={output},dst=/work'),
                  ('--mount', f'type=bind,src={Path(__file__).resolve()},dst=/control/build.py,readonly')]
        args = ['create', '--name', container_name, '--label', LABEL + '=' + token,
                '--network', 'none', '--memory', str(MEMORY_BYTES), '--memory-swap', str(MEMORY_BYTES),
                '--cpus', '8', '--pids-limit', '512', '--read-only', '--cap-drop', 'ALL',
                '--cap-add', 'CHOWN', '--cap-add', 'FOWNER', '--cap-add', 'DAC_OVERRIDE',
                '--cap-add', 'SETUID', '--cap-add', 'SETGID',
                '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,nodev,size=134217728',
                *[part for pair in mounts for part in pair], '--entrypoint', '/usr/bin/python3', IMAGE,
                '-E', '-s', '-B', '/control/build.py', '--worker',
                '--deadline-monotonic', str(started + timeout - 12)]
        create_attempted = True
        cid = docker(args).stdout.strip()
        observed = inspect(); verify_container(observed, cid, token)
        attestation.update(valid=True, container_id=cid, host_config={k: observed['HostConfig'][k] for k in
            ['Memory', 'MemorySwap', 'NanoCpus', 'NetworkMode', 'PidsLimit', 'ReadonlyRootfs']})
        write_json(output / 'ownership.json', attestation)
        result = docker(['start', '-a', cid], wait=timeout, check=False)
        (output / 'controller.stdout.log').write_text(result.stdout)
        (output / 'controller.stderr.log').write_text(result.stderr)
        state = inspect(); verify_container(state, cid, token)
        attestation['oom_killed'] = state['State'].get('OOMKilled') is True
        attestation['container_exit_code'] = state['State'].get('ExitCode')
        worker_path = evidence / 'worker.json'
        if worker_path.is_file(): record.update(json.loads(worker_path.read_text()))
        else: record.update(classification='infrastructure_invalid', failure_class='build_worker_incomplete')
        if state['State'].get('ExitCode') == 124:
            record.update(classification='infrastructure_invalid', failure_class='build_timeout', timed_out=True)
        if state['State'].get('Running') or attestation['oom_killed']:
            record.update(classification='infrastructure_invalid', failure_class='build_resource_failure')
    except subprocess.TimeoutExpired:
        record.update(classification='infrastructure_invalid', failure_class='build_timeout', timed_out=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        record.update(classification='infrastructure_invalid', failure_class='build_environment_failure',
                      error_type=type(exc).__name__, error_detail=str(exc)[-1500:])
    finally:
        if not cid and create_attempted:
            try:
                # An interrupted create can have committed. Recover only this
                # random exact name and verify its label/image before cleanup.
                found = subprocess.run(['docker', 'inspect', container_name], capture_output=True,
                                       text=True, timeout=4, env=DOCKER_ENV)
                if found.returncode == 0:
                    observed = json.loads(found.stdout)[0]
                    verify_container(observed, observed['Id'], token)
                    cid = observed['Id']
                    attestation['recovered_uncertain_create'] = True
                elif 'no such' not in found.stderr.lower():
                    raise RuntimeError('uncertain create cannot be proven absent')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                attestation['unresolved_create'] = str(exc)[-1000:]
        if cid:
            try:
                # Cleanup has its own reserved 12s; never act on a name or broad filter.
                observed = json.loads(subprocess.check_output(['docker', 'inspect', cid], text=True, timeout=4, env=DOCKER_ENV))[0]
                verify_container(observed, cid, token)
                subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, text=True, check=True, timeout=60, env=DOCKER_ENV)  # 116g: 5 s was hit on docker 29 + containerd
                absent = subprocess.run(['docker', 'inspect', cid], capture_output=True, text=True, timeout=3, env=DOCKER_ENV)
                attestation['cleanup'] = {'complete': absent.returncode != 0 and 'no such' in absent.stderr.lower(),
                                          'container_id': cid, 'identity_verified_before_remove': True}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                attestation['cleanup'] = {'complete': False, 'error': str(exc)[-1000:]}
        else:
            attestation['cleanup'] = {'complete': 'unresolved_create' not in attestation,
                                      'container_never_created_or_proven_absent': 'unresolved_create' not in attestation}
        attestation['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if attestation['elapsed_seconds'] > timeout:
            record.update(classification='infrastructure_invalid', failure_class='build_total_deadline_exceeded', timed_out=True)
        record = classify(record, baseline)
        write_json(output / 'typecheck.json', record)
    return record


def worker(deadline):
    """Trusted preparation; npm runs as an unprivileged user with no network."""
    if deadline is None:
        raise RuntimeError('missing evaluator-owned absolute deadline')
    # Namespace PID1 ignores signals with their default disposition. Install
    # a real handler so the timer exits the init and the runtime kills all children.
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.setitimer(signal.ITIMER_REAL, max(.001, deadline - time.monotonic()))
    evidence = Path('/work/evidence'); repository = Path('/work/repository')
    env = {'PATH': '/opt/node/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'CI': 'true',
           'HOME': '/work/repository/.build-home', 'npm_config_offline': 'true',
           'npm_config_audit': 'false', 'npm_config_fund': 'false', 'npm_config_update_notifier': 'false'}
    value = {'classification': 'typecheck_observed', 'diagnostics': []}
    try:
        cgroup = Path('/sys/fs/cgroup')
        actual = {name: (cgroup/name).read_text().strip() for name in
                  ('memory.max', 'memory.swap.max', 'cpu.max', 'pids.max')}
        quota, period = actual['cpu.max'].split()
        if (actual['memory.max'] != str(MEMORY_BYTES) or actual['memory.swap.max'] != '0'
                or actual['pids.max'] != '512' or quota == 'max' or int(quota) != 8 * int(period)):
            raise RuntimeError('actual container cgroup does not enforce build resources')
        actual['network_interfaces'] = sorted(line.split(':', 1)[0].strip()
                                               for line in Path('/proc/self/net/dev').read_text().splitlines() if ':' in line)
        # Linux may create a down, addressless tunl0 in every namespace.
        # Prove no non-loopback link is UP and no IPv4 route exists instead
        # of treating the presence of that kernel interface as connectivity.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            actual['network_interface_flags'] = {name: struct.unpack('H',
                fcntl.ioctl(probe.fileno(), 0x8913, struct.pack('256s', name.encode()))[16:18])[0]
                for name in actual['network_interfaces']}
        actual['ipv4_routes'] = Path('/proc/self/net/route').read_text().splitlines()[1:]
        if (any(flags & 1 and not flags & 8 for flags in actual['network_interface_flags'].values())
                or actual['ipv4_routes']):
            raise RuntimeError('build network namespace has an external link or route')
        actual['network_offline'] = True
        actual['cgroup'] = Path('/proc/self/cgroup').read_text().strip()
        value['runtime_resources'] = actual
        versions = {}
        for name, expected in [('node', 'v24.'), ('npm', '11.')]:
            observed = subprocess.check_output([name, '--version'], text=True, env=env, timeout=10).strip()
            versions[name] = observed
            if not observed.startswith(expected): raise RuntimeError('wrong ' + name + ' version')
        value['toolchain'] = versions
        for rel in ['package-lock.json', 'testing/fake-llm-server/package-lock.json']:
            # Reusing dependencies for a changed lock is unresolved preparation, never Candidate zero.
            if (Path('/source') / rel).read_bytes() != (Path('/baseline') / rel).read_bytes():
                raise RuntimeError('candidate lock differs from prepared dependency lock: ' + rel)
        baseline_package = json.loads(Path('/baseline/package.json').read_text())
        candidate_package = json.loads(Path('/source/package.json').read_text())
        shutil.copytree('/source', repository, symlinks=True,
                        ignore=shutil.ignore_patterns('.git', 'node_modules', 'out', '.build-home'))
        for src, rel in [('/prepared-root', 'node_modules'), ('/prepared-fake', 'testing/fake-llm-server/node_modules')]:
            target = repository / rel; target.mkdir(parents=True, exist_ok=True)
            subprocess.run(['cp', '-a', '--reflink=auto', src + '/.', str(target)], check=True)
        # The public typecheck entry remains npm run ts. Use the evaluator's
        # pinned command definitions in this disposable compile copy so an
        # allowed package.json edit cannot replace compilation with `true`.
        runtime_package = repository / 'package.json'
        scripts = candidate_package.setdefault('scripts', {})
        for name in list(scripts):
            if name in ('prets', 'postts') or name.startswith(('prets:', 'postts:')):
                del scripts[name]
        for name, command in baseline_package.get('scripts', {}).items():
            if name == 'ts' or name.startswith('ts:'): scripts[name] = command
        runtime_package.write_text(json.dumps(candidate_package, indent=2) + '\n')
        value['typecheck_command_owner'] = 'evaluator pinned baseline package scripts, disposable copy only'
        for cached in (repository / 'node_modules/.tmp').glob('*.tsbuildinfo'):
            cached.unlink()
        # npm's incremental output belongs exclusively to this disposable repository.
        for directory, dirs, files in os.walk(repository, followlinks=False):
            for path in [Path(directory), *[Path(directory)/name for name in dirs+files]]:
                if path.is_symlink(): continue
                path.chmod(path.stat().st_mode | 0o700)
                os.chown(path, 65534, 65534)
        Path(env['HOME']).mkdir(mode=0o700); os.chown(env['HOME'], 65534, 65534)
        def demote():
            os.setgroups([]); os.setgid(65534); os.setuid(65534)
        started = time.monotonic()
        commands = [TYPE_COMMAND]
        results = []
        with (evidence/'compiler.stdout.log').open('w') as stdout, (evidence/'compiler.stderr.log').open('w') as stderr:
            proc = subprocess.run(TYPE_COMMAND, cwd=repository, env=env, stdout=stdout, stderr=stderr,
                                  preexec_fn=demote, check=False)
            results.append({'command': TYPE_COMMAND, 'exit_code': proc.returncode})
            if proc.returncode:
                # npm run ts uses &&. Existing main errors must not hide a new
                # worker error, so evaluate each pinned worker leaf as well.
                for name in baseline_package.get('scripts', {}):
                    if name.startswith('ts:workers:'):
                        command = ['npm', 'run', name]
                        leaf = subprocess.run(command, cwd=repository, env=env, stdout=stdout, stderr=stderr,
                                              preexec_fn=demote, check=False)
                        results.append({'command': command, 'exit_code': leaf.returncode})
        value['compiler_commands'] = results
        value['runtime_resources']['memory.events'] = (cgroup/'memory.events').read_text().strip()
        if (cgroup/'memory.peak').is_file():
            value['runtime_resources']['memory.peak'] = int((cgroup/'memory.peak').read_text())
        events = dict(line.split() for line in value['runtime_resources']['memory.events'].splitlines())
        if int(events.get('oom', '0')) or int(events.get('oom_kill', '0')):
            raise RuntimeError('build cgroup observed an OOM event')
        unusual = next((item['exit_code'] for item in results if item['exit_code'] not in (0, 2)), None)
        exit_code = unusual if unusual is not None else (2 if any(item['exit_code'] for item in results) else 0)
        stdout = (evidence/'compiler.stdout.log').read_text(errors='replace')
        stderr = (evidence/'compiler.stderr.log').read_text(errors='replace')
        value.update(exit_code=exit_code, stdout_tail=stdout[-12000:], stderr_tail=stderr[-5000:],
                     diagnostics=diagnostics(stdout), compile_seconds=round(time.monotonic()-started, 3))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        value.update(classification='infrastructure_invalid', failure_class='build_dependency_or_toolchain_failure',
                     error_type=type(exc).__name__, error_detail=str(exc)[-1500:])
    write_json(evidence/'worker.json', value)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--worker', action='store_true')
    parser.add_argument('--deadline-monotonic', type=float)
    options = parser.parse_args()
    if options.worker: raise SystemExit(worker(options.deadline_monotonic))
