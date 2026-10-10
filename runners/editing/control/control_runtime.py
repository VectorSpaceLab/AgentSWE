"""Explicit host control interpreter; never chooses a Candidate runtime.

The Harbor CLI venv installs a regular `harbor` package, which can hide a
task's namespace package. Probe the actual one-stop CLI with --help before
launch; no Docker/Builder/provider calls are part of this preflight.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess


def control_environment(environ=None):
    env = dict(os.environ if environ is None else environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP'):
        env.pop(name, None)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    return env


def control_command(python: Path, script: Path, *arguments: str) -> list[str]:
    if not python.is_absolute():
        raise ValueError('Control Python must be an explicit absolute path')
    return [str(python), '-E', '-s', '-B', str(script), *arguments]


def probe_control_runtime(python: Path, tasks: dict[str, Path]) -> dict:
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Control Python is missing or not an absolute executable: ' + str(python))
    # No credentials or environment values are returned by this probe.
    code = '''import hashlib, importlib.util, json, pathlib, platform, sys
import requests
spec = importlib.util.find_spec('harbor')
print(json.dumps({'executable': sys.executable, 'version': list(sys.version_info[:3]),
                  'platform': sys.platform, 'requests_version': requests.__version__,
                  'harbor_origin': spec.origin if spec else None,
                  'harbor_locations': list(spec.submodule_search_locations or []) if spec else [],
                  'isolated_python_environment': bool(sys.flags.ignore_environment and sys.flags.no_user_site)}))
'''
    env = control_environment()
    result = subprocess.run([str(python), '-E', '-s', '-B', '-c', code], cwd=Path(__file__).parent,
                            env=env, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise ValueError('Control interpreter dependency probe failed: ' + result.stderr[-2000:])
    value = json.loads(result.stdout)
    if value['platform'] != 'linux' or value['version'][:2] < [3, 10]:
        raise ValueError('Formal host control requires Linux Python >= 3.10')
    if value['harbor_origin'] or value['harbor_locations']:
        raise ValueError('Global harbor package conflicts with task-local harbor namespace: ' +
                         str(value['harbor_origin'] or value['harbor_locations']))
    if not value['isolated_python_environment']:
        raise ValueError('Python path/user-site isolation is not active')
    executable = python.resolve(strict=True)
    value.update(configured_path=str(python), resolved_path=str(executable),
                 executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
    checks = []
    for task, sibling in tasks.items():
        path = sibling / 'harbor/formal_one_stop.py'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        command = control_command(python, path, '--help')
        check = subprocess.run(command, cwd=sibling, env=env, capture_output=True,
                               text=True, timeout=20, check=False)
        if check.returncode != 0 or 'usage:' not in check.stdout.lower():
            raise ValueError(task + ': actual formal --help import probe failed: ' + check.stderr[-2000:])
        if hashlib.sha256(path.read_bytes()).hexdigest() != before:
            raise ValueError(task + ': one-stop changed during control probe')
        checks.append({'task': task, 'command': command, 'one_stop_sha256': before,
                       'exit_code': check.returncode,
                       'help_sha256': hashlib.sha256(check.stdout.encode()).hexdigest()})
    return {'valid': True, 'interpreter': value, 'task_cli_checks': checks,
            'scope': 'host controller dependency/import/argument-parser preflight only',
            'candidate_environment_validated': False, 'provider_calls': 0}
