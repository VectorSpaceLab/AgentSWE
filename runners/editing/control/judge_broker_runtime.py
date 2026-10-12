"""Evaluator-only Docker lifecycle for the single-upstream Result judge.

No credential reading, no generation request at startup, no global cleanup.
All task callers preserve their Builder/lower implementations and invoke this
module only for explicitly identified Result roles.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

JUDGE_SCRIPT = Path(__file__).with_name('judge_broker_xhigh.py').resolve()
STREAM_MODULE = Path(__file__).with_name('responses_stream.py').resolve()
DEFAULT_UPSTREAM = 'https://api.deepseek.com/v1/responses'
REMOVAL_WAIT_SECONDS = 30  # an --rm container the daemon is still removing after `docker stop` (Docker 29)


def upstream_responses(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('invalid evaluator upstream URL')
    path = parts.path.rstrip('/')
    if not path.endswith('/responses'):
        path += '/responses' if path.endswith('/v1') else '/v1/responses'
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))


def fresh_judge_stats(stats, instance_id=None):
    if not isinstance(stats, dict):
        return False
    protocol, runtime = stats.get('protocol'), stats.get('runtime')
    if not isinstance(protocol, dict) or not isinstance(runtime, dict):
        return False
    return (stats.get('schema_version') == 'agentswe-judge-broker-stats/v1'
        and (instance_id is None or stats.get('broker_instance_id') == instance_id)
        and protocol.get('model') == 'deepseek-flash'
        and protocol.get('reasoning_effort') == 'max'
        and protocol.get('max_output_tokens') == 64000
        and protocol.get('inner_retries') == 0
        and protocol.get('max_upstream_attempts_per_transport') == 1
        and protocol.get('absolute_deadline_seconds_max') == 900
        and protocol.get('redirects_allowed') is False
        and protocol.get('response_transport_modes') == ['stream', 'nonstream']
        and protocol.get('downstream_response_format') == 'terminal_json'
        and protocol.get('connect_timeout_seconds_max') == 30
        and protocol.get('upstream_attempt_marker') == 'before_first_http_bytes_after_connection'
        and all(type(runtime.get(k)) is int and runtime[k] == 0
                for k in ('calls', 'completed_calls', 'successful_calls', 'failures', 'upstream_attempts',
                          'tokens', 'usage_unknown_calls', 'in_flight_calls')))


def write_new(path, value):
    with path.open('x') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats(endpoint, timeout=3):
    url = endpoint.split('/v1/', 1)[0] + '/stats'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer stats-only-placeholder'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class JudgeBroker:
    def __init__(self, *, name, credential, image, port, cidfile, upstream=DEFAULT_UPSTREAM,
                 defer_removal=False):
        if type(defer_removal) is not bool:
            raise ValueError('defer_removal must be a bool')
        self.defer_removal = defer_removal
        self.name, self.image, self.port = name, image, int(port)
        self.credential, self.cidfile = Path(credential).resolve(), Path(cidfile).absolute()
        self.upstream = upstream_responses(upstream)
        self.instance_id = uuid.uuid4().hex
        self.endpoint = f'http://127.0.0.1:{port}/v1/responses'
        self.directory = self.cidfile.parent / (self.cidfile.stem + '-judge-transport')
        self.stats_path = self.directory / 'broker_stats.json'
        self.lifecycle_path = self.directory / 'lifecycle.json'
        self.container_id = None
        self.cleanup = None

    def _inspect(self):
        if not self.container_id:
            return None
        result = subprocess.run(['docker', 'inspect', self.container_id], text=True,
                                capture_output=True, timeout=10)
        if result.returncode:
            if 'no such' in (result.stdout + result.stderr).lower():
                return None
            raise RuntimeError('Docker could not establish owned-container state')
        value = json.loads(result.stdout)[0]
        if (value.get('Id') != self.container_id or value.get('Config', {}).get('Labels', {}).get(
                'agentswe.judge.instance') != self.instance_id):
            raise RuntimeError('Judge broker ownership mismatch; refusing mutation')
        return value

    def start(self):
        if self.cidfile.exists() or self.cidfile.is_symlink():
            raise FileExistsError('existing broker CID file must not be overwritten')
        if (not JUDGE_SCRIPT.is_file() or JUDGE_SCRIPT.is_symlink() or not self.credential.is_file()
                or not STREAM_MODULE.is_file() or STREAM_MODULE.is_symlink()):
            raise ValueError('missing trusted broker or credential path')
        self.directory.mkdir(parents=True, exist_ok=False)
        bindings = {str(path): sha(path) for path in (
            JUDGE_SCRIPT, STREAM_MODULE, Path(__file__).resolve(), JUDGE_SCRIPT.with_name('result_judge.py'))}
        write_new(self.directory / 'source_bindings.json', bindings)
        command = ['docker', 'run', '-d', '--rm', '--pull', 'never', '--network', 'host',
            '--name', self.name, '--label', 'agentswe.owner=edit-result-judge',
            '--label', 'agentswe.judge.instance=' + self.instance_id,
            '--cidfile', str(self.cidfile),
            '-v', f'{JUDGE_SCRIPT}:/broker.py:ro',
            '-v', f'{STREAM_MODULE}:/responses_stream.py:ro',
            '-v', f'{self.credential}:/run/secrets/agentswe.env:ro',
            '-v', f'{self.directory}:/evidence',
            '-v', '/etc/ssl/certs:/etc/ssl/certs:ro',
            '-e', 'SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt',
            '-e', 'AGENTSWE_EVALUATOR_PROXY_URL=',  # direct egress; mihomo lost whole responses under load
            self.image, 'python3', '/broker.py',
            '--credential-file', '/run/secrets/agentswe.env', '--bind', '127.0.0.1', '--port', str(self.port),
            '--upstream', self.upstream, '--stats-path', '/evidence/broker_stats.json',
            '--timeout', '900', '--instance-id', self.instance_id]
        if self.defer_removal:
            command.remove('--rm')
        write_new(self.directory / 'launch.json', {'command': command, 'instance_id': self.instance_id,
            'credential_values_read_by_launcher': False, 'source_bindings': bindings})
        try:
            launched = subprocess.run(command, capture_output=True, text=True, timeout=40)
            cid = self.cidfile.read_text().strip() if self.cidfile.is_file() else launched.stdout.strip()
            if re.fullmatch('[0-9a-f]{64}', cid):
                self.container_id = cid
            if launched.returncode or not self.container_id:
                raise RuntimeError('judge Docker startup failed; inspect owned launch evidence')
            until = time.monotonic() + 30
            last_error = ''
            while time.monotonic() < until:
                observed = self._inspect()
                if observed is None or observed.get('State', {}).get('Running') is not True:
                    raise RuntimeError('owned judge container exited during startup')
                try:
                    value = stats(self.endpoint, min(2, max(.1, until-time.monotonic())))
                    if not fresh_judge_stats(value, self.instance_id):
                        raise RuntimeError('wrong/stale/nested-retry judge endpoint')
                    if bindings != {path: sha(Path(path)) for path in bindings}:
                        raise RuntimeError('judge source changed while starting')
                    write_new(self.lifecycle_path, {'started': True, 'container_id': self.container_id,
                        'container_name': self.name, 'broker_instance_id': self.instance_id,
                        'endpoint': self.endpoint, 'stats_path': str(self.stats_path),
                        'source_bindings': bindings, 'initial_stats': value,
                        'model_post_requests_during_startup': 0})
                    return self
                except Exception as exc:
                    last_error = type(exc).__name__ + ': ' + str(exc)
                    time.sleep(.1)
            raise RuntimeError('judge startup protocol check failed: ' + last_error)
        except BaseException:
            # Docker may have written this exact previously-absent CID path
            # before the client timed out. Never recover ownership by name.
            if self.container_id is None and self.cidfile.is_file():
                cid = self.cidfile.read_text().strip()
                if re.fullmatch('[0-9a-f]{64}', cid):
                    self.container_id = cid
            self.close()
            raise

    def close(self):
        if self.cleanup is not None:
            return self.cleanup
        result = {'container_id': self.container_id, 'container_name': self.name,
                  'instance_id': self.instance_id, 'ownership_proven': False, 'absent_after_cleanup': False}
        try:
            observed = self._inspect()
            if observed is None:
                result['absent_after_cleanup'] = self.container_id is not None
            else:
                result['ownership_proven'] = True
                try:
                    write_new(self.directory / 'pre_stop_stats.json', stats(self.endpoint))
                except Exception as exc:
                    result['stats_error_type'] = type(exc).__name__
                if self.defer_removal:
                    usage = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', self.container_id],
                        text=True, capture_output=True, timeout=15, check=True)
                    write_new(self.directory / 'container_pre_stop_stats.json', {
                        'container_id': self.container_id, 'stats_stdout': usage.stdout,
                        'state': observed.get('State'), 'observed_at': time.time()})
                stopped = subprocess.run(['docker', 'stop', '--time', '3', self.container_id],
                    text=True, capture_output=True, timeout=10)
                result['stop_exit_code'] = stopped.returncode
                if self.defer_removal:
                    terminal = self._inspect()
                    if (stopped.returncode or terminal is None or
                            terminal.get('State', {}).get('Running') is not False):
                        raise RuntimeError('retained judge broker has no stopped-container evidence')
                    write_new(self.directory / 'container_terminal.json', {
                        'container_id': self.container_id, 'state': terminal['State'],
                        'labels': terminal.get('Config', {}).get('Labels', {}), 'observed_at': time.time()})
                    result.update(removal_deferred=True, process_stopped=True,
                                  coordinator_cleanup_required=True)
                    self.cleanup = result
                    write_new(self.directory / 'cleanup.json', result)
                    return result
                if self._inspect() is not None:
                    removed = subprocess.run(['docker', 'rm', '-f', self.container_id],
                        text=True, capture_output=True, timeout=10)
                    if removed.returncode:
                        # The broker runs with --rm, so after `docker stop` the daemon may already be removing it:
                        # Docker 29 does that asynchronously and `docker rm -f` answers "removal of container ...
                        # is already in progress". Any other refusal is a cleanup failure, as before.
                        if 'already in progress' not in (removed.stdout + removed.stderr).lower():
                            raise subprocess.CalledProcessError(removed.returncode, removed.args,
                                                                removed.stdout, removed.stderr)
                        result['removal_in_progress_at_rm'] = True
                deadline = time.monotonic() + REMOVAL_WAIT_SECONDS
                while self._inspect() is not None and time.monotonic() < deadline:
                    time.sleep(0.5)
                result['absent_after_cleanup'] = self._inspect() is None
        except Exception as exc:
            result['cleanup_error_type'] = type(exc).__name__
        self.cleanup = result
        write_new(self.directory / 'cleanup.json', result)
        return result


def start_judge_broker(**kwargs):
    return JudgeBroker(**kwargs).start()
