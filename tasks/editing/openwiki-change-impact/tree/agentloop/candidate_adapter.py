#!/usr/bin/env python3
"""Materialize and build a patched OpenWiki Candidate.

Runtime binaries are evaluator-provided immutable inputs.  All package-manager
stores, npm caches, and other writable state belong to the individual build
directory; the sibling source tree is never used as a writable runtime.
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, time, sys, uuid
from pathlib import Path
try:
    from .protocol import apply_patch, tree_digest, write_json
    from .evaluator.transport_sandbox import sandbox_command
except ImportError:  # direct `python agentloop/candidate_adapter.py`
    from protocol import apply_patch, tree_digest, write_json
    from evaluator.transport_sandbox import sandbox_command

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (ROOT / '.runtime').resolve()
TASK_NODE = RUNTIME / 'node-v22.12.0-linux-x64/bin/node'
_BUILD_SCOPE = None
TASK_NPM_CLI = RUNTIME / 'node-v22.12.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js'
try:
    from .owned_resources import run_owned
except ImportError:
    from owned_resources import run_owned


def _pnpm_command() -> list[str]:
    configured = os.environ.get("OPENWIKI_PNPM_BIN")
    candidates = [
        Path(configured) if configured else None,
        RUNTIME / "node_modules" / "pnpm" / "bin" / "pnpm.cjs",
    ]
    # The prepared Node 22.12 runtime also contains lockfile-compatible pnpm
    # 10.33.2 in its offline store. Prefer it when the convenience link points
    # at pnpm 11, whose engine floor is Node 22.13.
    compatible_store = sorted(
        (RUNTIME / "pnpm-store" / "v11" / "links" / "@" / "pnpm" / "10.33.2").glob(
            "*/node_modules/pnpm/bin/pnpm.cjs"
        )
    )
    candidates[1:1] = compatible_store
    for path in candidates:
        if path and path.is_file():
            if not TASK_NODE.is_file():
                raise FileNotFoundError(f"task-local Node 22 runtime missing: {TASK_NODE}")
            return [str(TASK_NODE), str(path)]
    system = shutil.which("pnpm")
    if system:
        return [system]
    raise FileNotFoundError(
        "pnpm is unavailable; set OPENWIKI_PNPM_BIN or prepare the sibling .runtime"
    )


def _store_dir(build_dir: Path) -> Path | None:
    # The store is deliberately fresh per build.  If the evaluator supplies a
    # prepared offline store, copy it into that fresh directory first; the
    # shared source/runtime tree is never used as a writable owner.
    configured = os.environ.get("OPENWIKI_PNPM_STORE")
    local = build_dir / "runtime-state" / "pnpm-store"
    local.mkdir(parents=True, exist_ok=True)
    if configured:
        prepared = Path(configured).resolve()
        if not prepared.is_dir():
            raise FileNotFoundError(f"evaluator-provided pnpm store is missing: {prepared}")
        shutil.copytree(prepared, local, symlinks=True, dirs_exist_ok=True)
    return local


def _ensure_external_modules(repo: Path) -> str | None:
    if (repo / "node_modules").exists():
        return None
    configured = os.environ.get("OPENWIKI_NODE_MODULES")
    target = Path(configured).resolve() if configured else None
    if target and target.is_dir():
        # Materialize the approved dependency snapshot into the run-local
        # build tree; never leave a host-absolute link in the Candidate build.
        shutil.copytree(target, repo / "node_modules", symlinks=True)
        return str(target)
    return None


def _run_in_build_scope(command, *, cwd, env, output, timeout):
    """Keep build children in their already attested aggregate parent scope."""
    expected = _BUILD_SCOPE
    current = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    root = Path('/sys/fs/cgroup') / current.lstrip('/')
    if current != expected['cgroup'] or (root/'memory.max').read_text().strip() != str(expected['memory_bytes']) or (root/'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('build child is not inside the attested parent budget')
    output.mkdir(parents=True, exist_ok=False)
    started=time.monotonic()
    try:
        process=subprocess.run(command,cwd=cwd,env=env,timeout=timeout,capture_output=True,text=True)
        timed_out=False
    except subprocess.TimeoutExpired as exc:
        process=subprocess.CompletedProcess(command,124,exc.stdout or '',exc.stderr or '')
        process.stdout=process.stdout.decode(errors='replace') if isinstance(process.stdout,bytes) else process.stdout
        process.stderr=process.stderr.decode(errors='replace') if isinstance(process.stderr,bytes) else process.stderr
        timed_out=True
    resource={**expected,'schema_version':'agentswe-inherited-build-resources/v1','valid':True,
        'aggregate_parent_scope':True,'memory_max':(root/'memory.max').read_text().strip(),
        'timeout_seconds':timeout,'timed_out':timed_out,'elapsed_seconds':round(time.monotonic()-started,3),
        'cleanup_owner':'complete build entry owns and verifies parent subtree cleanup'}
    write_json(output/'resource-attestation.json',resource)
    return process,resource


def _run(argv: list[str], cwd: Path, timeout: int, *, state_dir: Path) -> dict[str, object]:
    started = time.monotonic()
    home, temp = state_dir / 'home', state_dir / 'tmp'
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    pnpm = _pnpm_command()
    dependencies = [TASK_NODE.parent.parent, RUNTIME / 'agentloop-bin']
    if len(pnpm) > 1:
        dependencies.append(Path(pnpm[1]).parent.parent)
    isolated = sandbox_command(argv, writable=(cwd, state_dir), readonly=dependencies, cwd=cwd)
    executor = _run_in_build_scope if _BUILD_SCOPE is not None else run_owned
    process, resources = executor(
        isolated,
        cwd=cwd,
        timeout=min(float(timeout),600),
        output=state_dir/("owned-command-"+uuid.uuid4().hex),
        env={
            'HOME': str(home), 'TMPDIR': str(temp), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            # Lifecycle scripts invoke `pnpm` by name even when the
            # evaluator starts it through an absolute Node + pnpm.cjs argv.
            # Keep the pinned Node runtime and sibling-local pnpm shim visible
            # to those child scripts.
            "PATH": os.pathsep.join((
                str(TASK_NODE.parent),
                str(RUNTIME / "agentloop-bin"),
                "/usr/bin", "/bin",
            )),
            "COREPACK_ENABLE_PROJECT_SPEC": "0",
            "CC": str(Path("/usr/bin/gcc").resolve()),
            "CXX": str(Path("/usr/bin/g++").resolve()),
            "npm_config_build_from_source": "true",
            "npm_config_nodedir": str(TASK_NODE.parent.parent),
            "npm_config_cache": str(state_dir / "npm-cache"),
        },
    )
    return {
        "argv": argv,
        "resource_contract": resources,
        "sandbox": {"filesystem_isolated": True, "network_namespace_isolated": True,
            "network_egress": "none", "inherited_environment": False},
        "exit_code": process.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": process.stdout[-4000:],
        "stderr_tail": process.stderr[-4000:],
    }


def _native_runtime(repo: Path, state_dir: Path) -> dict[str, object]:
    """Rebuild and exercise better-sqlite3 with the pinned Node 22 runtime."""
    if not TASK_NODE.is_file() or not TASK_NPM_CLI.is_file():
        return {
            "valid": False,
            "classification": "native_binding_failure",
            "failure": "task_local_node_or_npm_missing",
        }
    rebuild = _run(
        [
            str(TASK_NODE),
            str(TASK_NPM_CLI),
            "rebuild",
            "better-sqlite3",
            "--foreground-scripts",
        ],
        repo,
        1800,
        state_dir=state_dir,
    )
    matches = sorted(
        (repo / "node_modules" / ".pnpm").glob(
            "better-sqlite3@*/node_modules/better-sqlite3"
        )
    )
    package = matches[0].resolve() if matches else None
    if rebuild.get("exit_code") != 0 or package is None:
        return {
            "valid": False,
            "classification": "native_binding_failure",
            "failure": "better_sqlite3_rebuild",
            "rebuild": rebuild,
            "package": str(package) if package else None,
        }
    script = r"""
const path = require('node:path');
const Database = require(path.resolve(process.argv[1]));
const db = new Database(':memory:');
db.exec("CREATE TABLE runtime_probe (value TEXT NOT NULL); INSERT INTO runtime_probe VALUES ('node22-native-ok')");
const row = db.prepare('SELECT value FROM runtime_probe').get();
db.close();
process.stdout.write(JSON.stringify({node: process.version, modules: process.versions.modules, value: row.value, closed: !db.open}) + '\n');
"""
    probe = _run([str(TASK_NODE), "-e", script, str(package)], repo, 60, state_dir=state_dir)
    stdout = str(probe.get("stdout_tail", ""))
    valid = (
        probe.get("exit_code") == 0
        and '"node":"v22.12.0"' in stdout
        and '"value":"node22-native-ok"' in stdout
        and '"closed":true' in stdout
    )
    return {
        "valid": valid,
        "classification": "runtime_ready" if valid else "native_binding_failure",
        "failure": None if valid else "better_sqlite3_round_trip",
        "node": str(TASK_NODE),
        "package": str(package),
        "binding": str(package / "build" / "Release" / "better_sqlite3.node"),
        "rebuild": rebuild,
        "probe": probe,
    }


def build_candidate(source: Path, candidate: Path, build_dir: Path, run_build: bool = True,
                    readiness_profile: str | None = None) -> dict:
    """One owned build budget includes source/dependency copy, native rebuild and compilation."""
    build_dir=build_dir.resolve();build_dir.mkdir(parents=True,exist_ok=True)
    result_path=build_dir/'owned-build-result.json'
    if result_path.exists():raise RuntimeError('existing build evidence must not be overwritten')
    argv=[sys.executable,'-I',str(Path(__file__).with_name('build_resource_driver.py')),
        '--source',str(source.resolve()),'--candidate',str(candidate.resolve()),'--build-dir',str(build_dir)]
    if not run_build:argv.append('--no-build')
    if readiness_profile:
        if readiness_profile != 'single-dev-two-round-hidden-smoke-v1':
            raise ValueError('unknown build readiness profile')
        argv.append('--readiness-source-check')
    env={key:os.environ[key] for key in ('OPENWIKI_PNPM_BIN','OPENWIKI_PNPM_STORE','OPENWIKI_NODE_MODULES') if key in os.environ}
    env.update(PATH='/usr/bin:/bin',LANG='C.UTF-8',PYTHONDONTWRITEBYTECODE='1')
    try:
        completed,resource=run_owned(argv,cwd='/',env=env,output=build_dir/'build_resources',timeout=600)
        result=json.loads(result_path.read_text()) if result_path.is_file() else {'valid':False,'classification':'infrastructure_failure','failure':'owned build driver did not produce result','driver_stderr':completed.stderr[-2000:]}
        result['build_resource_contract']=resource
        if resource.get('timed_out'):result.update(valid=False,classification='candidate_build_failure',failure='aggregate build deadline exceeded')
    except Exception as exc:result={'valid':False,'classification':'infrastructure_failure','failure':'owned build failure: '+type(exc).__name__}
    write_json(build_dir/'build_manifest.json',result);return result

def build_source_identity(repository):
    """Bind product inputs; pnpm dependencies and compiler dist are runtime outputs."""
    import hashlib
    rows = []
    for path in sorted(repository.rglob('*')):
        relative = path.relative_to(repository)
        if relative.parts[0] in {'.git', 'node_modules', 'dist'}:
            continue
        if path.is_symlink():
            rows.append([relative.as_posix(), 'link', os.readlink(path)])
        elif path.is_file():
            rows.append([relative.as_posix(), 'file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o111])
        elif path.is_dir():
            rows.append([relative.as_posix(), 'directory'])
        else:
            raise ValueError('special build source entry')
    value = {'algorithm': 'openwiki-build-inputs-v1', 'excluded': ['.git', 'node_modules', 'dist'], 'entries': rows}
    value['digest'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value


def _build_candidate(source: Path, candidate: Path, build_dir: Path, run_build: bool = True,
                     readiness_source_check: bool = False) -> dict:
    result = apply_patch(source, candidate / "solution.patch", build_dir / "repository")
    if not result.get("valid"): return result
    repo = build_dir / "repository"
    source_before = build_source_identity(repo) if readiness_source_check else None
    try:
        try:from .product_identity import product_source_identity
        except ImportError:from product_identity import product_source_identity
        identity=product_source_identity(repo)
        identity_path=build_dir/'product-source-identity.json'
        write_json(identity_path,identity)
        result.update(product_source_digest=identity['product_source_digest'],
            product_source_identity_path=str(identity_path),
            product_source_identity_sha256=__import__('hashlib').sha256(identity_path.read_bytes()).hexdigest())
    except (ValueError,OSError) as exc:
        result.update(valid=False,classification='candidate_build_failure',failure='product_source_identity',errors=[str(exc)])
        return result
    state_dir = build_dir / "runtime-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    result["lower_entry"] = "dist/cli.js"
    result["lower_entry_exists_before_build"] = (repo / "dist/cli.js").is_file()
    if run_build:
        try:
            external_modules = _ensure_external_modules(repo)
            pnpm = _pnpm_command()
            store = _store_dir(build_dir)
            install_argv = [*pnpm, "install", "--frozen-lockfile", "--offline", "--ignore-scripts"]
            if store:
                install_argv += ["--store-dir", str(store)]
            result["pnpm"] = pnpm
            result["pnpm_store"] = str(store) if store else None
            result["external_node_modules"] = external_modules
            if (repo / "node_modules").exists():
                result["pnpm_install"] = {
                    "skipped": True,
                    "reason": "prepared node_modules already present",
                }
            else:
                result["pnpm_install"] = _run(install_argv, repo, 1800, state_dir=state_dir)
                if result["pnpm_install"].get("exit_code") != 0:
                    result.update(valid=False, classification="candidate_build_failure", failure="pnpm_install")
                    return result
            result["native_runtime"] = _native_runtime(repo, state_dir)
            if not result["native_runtime"].get("valid"):
                result.update(
                    valid=False,
                    classification="native_binding_failure",
                    infrastructure_invalid=True,
                    candidate_classification=None,
                    failure="evaluator_native_runtime",
                )
                write_json(build_dir / "build_manifest.json", result)
                return result
            build_argv = [*pnpm, "run", "build"]
            result["pnpm_build"] = _run(build_argv, repo, 1800, state_dir=state_dir)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            result.update(
                valid=False,
                classification="infrastructure_failure",
                failure=f"{type(exc).__name__}: {exc}",
            )
            return result
        if result["pnpm_build"].get("exit_code") != 0 or not (repo / "dist/cli.js").is_file():
            result.update(valid=False, classification="candidate_build_failure", failure="pnpm_build"); return result
    if readiness_source_check:
        source_after = build_source_identity(repo)
        result['readiness_source_immutability'] = {'before': source_before, 'after': source_after,
            'unchanged': source_before == source_after, 'materialized_repository_digest': tree_digest(repo)}
        if source_before != source_after:
            result.update(valid=False, classification='infrastructure_failure', failure='source changed during controlled build')
            write_json(build_dir / 'build_manifest.json', result)
            return result
    result.update(valid=True, classification="candidate_ready", candidate_digest=tree_digest(candidate), product_entry=str(repo / "dist/cli.js"))
    write_json(build_dir / "build_manifest.json", result)
    return result

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--source-repository", type=Path, required=True); parser.add_argument("--candidate", type=Path, required=True); parser.add_argument("--build-dir", type=Path, required=True); parser.add_argument("--result", type=Path, required=True); parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args(); value = build_candidate(args.source_repository.resolve(), args.candidate.resolve(), args.build_dir.resolve(), not args.no_build); write_json(args.result.resolve(), value); print(json.dumps(value, indent=2, ensure_ascii=False)); return 0 if value.get("valid") else 1

if __name__ == "__main__": raise SystemExit(main())
