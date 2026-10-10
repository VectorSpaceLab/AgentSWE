"""Evaluator-owned, provider-free Codex build preflight and fault attribution."""
from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class BuildInfrastructureError(RuntimeError):
    """The Candidate could not be measured; no capability round is consumed."""


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def toolchain_identity(runtime: Path, target: Path) -> dict:
    """Bind actual compiler/Cargo and target libraries, not only rustup shims."""
    env = build_environment(runtime, target)
    def query(*args):
        result = subprocess.run([str(runtime / 'bin/rustc'), *args], env=env,
                                text=True, capture_output=True, timeout=30, check=True)
        return result.stdout.strip()
    try:
        sysroot = Path(query('--print', 'sysroot')).resolve()
        libdir = Path(query('--print', 'target-libdir')).resolve()
        files = [runtime / 'bin/cargo', runtime / 'bin/rustc', sysroot / 'bin/cargo', sysroot / 'bin/rustc']
        files += sorted(p for p in libdir.iterdir() if p.is_file())
        manifest = {str(path.resolve()): file_hash(path) for path in files}
        if not files or not manifest:
            raise OSError('empty compiler identity')
        content = {'runtime': str(runtime.resolve()), 'sysroot': str(sysroot),
                   'rustc_verbose': query('--version', '--verbose'), 'files': manifest}
        content['sha256'] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return content
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildInfrastructureError(f'toolchain identity unavailable: {exc}') from exc


def build_environment(runtime: Path, target: Path) -> dict[str, str]:
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
    env.update({
        "CARGO_HOME": str(runtime / "cargo-home"), "RUSTUP_HOME": str(runtime / "rustup-home"),
        "CARGO_TARGET_DIR": str(target), "CARGO_NET_OFFLINE": "true", "CARGO_BUILD_JOBS": "6",
        "OPENSSL_DIR": str(runtime), "PKG_CONFIG_PATH": str(runtime / "lib/pkgconfig"),
        "PATH": str(runtime / "bin") + os.pathsep + env.get("PATH", ""),
        "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
    })
    return env


def isolated_build_command(command: list[str], *, runtime: Path, target: Path, worktree: Path) -> list[str]:
    """Build scripts/proc macros see only the compiler, source and run cache."""
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise BuildInfrastructureError('bwrap is required before executing Candidate build scripts')
    runtime, target, worktree = runtime.resolve(), target.resolve(), worktree.resolve()
    for path in (runtime, target, worktree):
        if path == Path('/') or not path.is_dir():
            raise BuildInfrastructureError('invalid scoped build sandbox mount')
    cache = target.parent / 'isolated-cargo-home'
    if not cache.exists():
        subprocess.run(['cp', '-a', '--reflink=auto', str(runtime / 'cargo-home'), str(cache)], check=True)
    env = build_environment(runtime, target)
    env['CARGO_HOME'] = str(cache)
    env['HOME'] = '/tmp/build-home'
    wrapped = [bwrap, '--die-with-parent', '--new-session', '--unshare-all', '--clearenv']
    for path in ('/usr', '/bin', '/lib', '/lib64', '/etc/alternatives'):
        if Path(path).exists():
            wrapped += ['--ro-bind', path, path]
    wrapped += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/tmp/build-home',
                '--ro-bind', str(runtime), str(runtime), '--bind', str(cache), str(cache),
                '--bind', str(target), str(target), '--bind', str(worktree), str(worktree),
                '--chdir', str(worktree / 'codex-rs')]
    for key, value in env.items():
        wrapped += ['--setenv', key, value]
    return [*wrapped, '--', *command]


def private_candidate_target(baseline_target: Path, output: Path) -> Path:
    """Seed a disposable target from the unchanged baseline, never a Candidate.

    Cargo build scripts can modify writable dependency/build caches. Reusing one
    between deliveries lets a previous Candidate change the next measurement.
    Copy-on-write is permitted; hard links and shared writable mounts are not.
    """
    baseline_target = baseline_target.resolve()
    if baseline_target == Path('/') or not baseline_target.is_dir():
        raise BuildInfrastructureError('unchanged-baseline target cache is unavailable')
    target = output / 'build_cache' / 'target'
    if target.exists() or target.is_symlink():
        raise BuildInfrastructureError('Candidate target cache must be new')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(['cp', '-a', '--reflink=auto', '--', str(baseline_target), str(target)],
                       text=True, capture_output=True, timeout=180, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildInfrastructureError(f'private Candidate cache setup failed: {exc}') from exc
    return target


def check_build_environment(runtime: Path, target: Path) -> dict:
    """Probe permissions as the evaluator UID, never root-chmod a shared cache."""
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=target) as probe:
            probe.write(b"agentswe-build-write-probe")
            probe.flush()
            os.fsync(probe.fileno())
        cargo = runtime / "bin/cargo"
        if not cargo.is_file() or not os.access(cargo, os.X_OK):
            raise OSError("evaluator Cargo is unavailable or not executable")
        result = subprocess.run([str(cargo), "--version"], env=build_environment(runtime, target),
                                text=True, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise OSError("evaluator Cargo toolchain probe failed: " + result.stderr[-800:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildInfrastructureError(f"build environment preflight: {exc}") from exc
    return {"evaluator_uid": os.geteuid(), "target": str(target.resolve()),
            "writable": True, "cargo_version": result.stdout.strip(), "provider_calls": 0}


def infrastructure_build_error(label: str, stderr: str) -> bool:
    """Only source/patch diagnostics are Candidate failures, not host faults."""
    if label == "git_init":
        return True
    text = stderr.lower()
    markers = (
        "permission denied", "no space left on device", "read-only file system",
        "disk quota exceeded", "failed to download", "failed to get successful http",
        "failed to resolve", "could not resolve host", "no matching package named",
        "attempting to make an http request", "linker `cc` not found", "linker `clang` not found",
        "toolchain is not installed", "no default toolchain configured", "failed to run custom build command",
        "signal: 9", "sigkill", "out of memory", "cannot allocate memory",
    )
    if any(marker in text for marker in markers):
        return True
    if label in {"patch_check", "patch_apply"}:
        return False
    # Rust diagnostics identify source errors precisely. Unknown failures fail
    # closed as infrastructure, rather than silently assigning a capability 0.
    return not bool(re.search(r"error\[E\d+\]|error: (?:expected|unexpected|mismatched|cannot|unresolved)", stderr))


def freeze_binary(binary: Path, output: Path) -> Path:
    """Never let a later Candidate overwrite the executable of a frozen one."""
    destination = output / "binary" / "codex"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, destination)
    destination.chmod(0o555)
    return destination


def baseline_build(benchmark: Path, runtime: Path, target: Path, output: Path) -> dict:
    """Build unchanged public input once before paying for any Builder session."""
    evidence = check_build_environment(runtime, target)
    worktree = output / "baseline"
    if worktree.exists():
        raise BuildInfrastructureError("baseline preflight directory must be new")
    shutil.copytree(benchmark / "input/repository", worktree, symlinks=True)
    command = [str(runtime / "bin/cargo"), "build", "--offline", "-p", "codex-cli", "--bin", "codex"]
    try:
        completed = subprocess.run(isolated_build_command(command, runtime=runtime, target=target, worktree=worktree),
                                   cwd=worktree / "codex-rs", env=build_environment(runtime, target),
                                   text=True, capture_output=True, timeout=1800, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildInfrastructureError(f"baseline cannot compile: {exc}") from exc
    (output / "baseline.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output / "baseline.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode or not (target / "debug/codex").is_file():
        raise BuildInfrastructureError("unchanged baseline cannot compile: " + completed.stderr[-1200:])
    evidence.update({"baseline_compiled": True, "baseline_exit_code": completed.returncode,
                     'build_sandbox': 'bwrap --unshare-all; compiler read-only; source/run-cache only'})
    return evidence
