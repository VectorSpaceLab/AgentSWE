#!/usr/bin/env python3
"""OpenClaw one-session Builder lifecycle entry.

This is the only entry that can create a real Builder Candidate lifecycle.
It is provider-free by default.  Only explicit ``--run-formal`` authorization
may start Docker, Harbor, provider calls, or hidden execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

JUDGE_BROKER_SCRIPT = Path("@@AGENTSWE_EDITING_CONTROL@@/judge_broker_xhigh.py")


def _judge_runtime():
    """Evaluator-only runtime; never used for Builder or lower roles."""
    import sys
    if str(JUDGE_BROKER_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(JUDGE_BROKER_SCRIPT.parent))
    import judge_broker_runtime
    return judge_broker_runtime

from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# openclaw expresses this profile through --pilot; the names let the launcher
# and the admission check agree with the other nine tasks about what ran.
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"
READINESS_PUBLIC_CASES = ("dev_001",)
READINESS_HIDDEN_CASES = ("test_001",)
SHARED_RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller.builder_session_controller import BuilderSessionController
from harbor import direct_harbor_builder as direct
from harbor.native_builder_runner import run_native_builder
from harbor.builder_dependencies import prepare as prepare_builder_dependencies
from controller.two_round_controller import tree_digest
from evaluator.dev_result import score_public_case
from evaluator.candidate_runtime import prepare_candidate_runtime, write_runtime_manifest, validate_runtime_manifest
from evaluator.case_service import write_private_case
from evaluator.hidden_executor import launch_case, run_suite
from lower_agent.launcher import CASE_TIMEOUT_SECONDS, read_broker_stats, runtime_stats

MODEL = "deepseek-flash"
BUILDER_MODEL = "deepseek-flash"  # upper Builder (Codex harness) only
BUILDER_EFFORT = "max"
LOWER_EFFORT = "high"
BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
BUILDER_BROKER_SCRIPT = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)
# 5 h Builder cap (D2, 2026-09-19).  The shared driver
# @@AGENTSWE_EDITING_CONTROL@@/formal_commands.py passes
# --builder-timeout 18000, so the generated Harbor task.toml has to stop the
# native Builder BEFORE the outer wait SIGTERMs it; otherwise the rollout
# stream never records a terminal event and the whole run is unusable.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
# The pilot/readiness branch keeps its historical 8 h generated cap: readiness
# passes --builder-timeout 28800 explicitly and its two-round canary approaches
# neither bound, so this leaves readiness behaviour unchanged.
PILOT_BUILDER_TASK_TIMEOUT_SECONDS = 28_800
# native_builder_runner turns an outer-deadline SIGTERM into 124, and reports
# 125 for a resource-observer failure or an invalid observer result.  Only 124
# is a survivable interrupt.
BUILDER_INTERRUPT_EXIT_CODES = (124,)


def builder_timeout_contract(effective_outer_timeout: int) -> dict[str, Any]:
    """Prove the outer deadline covers the generated task and cleanup margin."""
    if isinstance(effective_outer_timeout, bool) or effective_outer_timeout <= 0:
        raise ValueError("effective outer Builder timeout must be a positive integer")
    required = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
    return {
        "generated_task_timeout_seconds": GENERATED_BUILDER_TASK_TIMEOUT_SECONDS,
        "cleanup_margin_seconds": BUILDER_CLEANUP_MARGIN_SECONDS,
        "effective_outer_timeout_seconds": int(effective_outer_timeout),
        "covers_task_plus_cleanup": int(effective_outer_timeout) >= required,
        "required_outer_timeout_seconds": required,
    }


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def allocate_runtime_attempt(root: Path, number: int) -> tuple[Path, Path]:
    """An infrastructure retry keeps its round number but never reuses files."""
    parent=root/f'candidate_{number:03d}'
    parent.mkdir(parents=True,exist_ok=True)
    attempt=1
    while True:
        directory=parent/f'attempt_{attempt:03d}'
        try:
            directory.mkdir()
            return directory/'product',directory/'runtime_manifest.json'
        except FileExistsError:
            attempt+=1


def unavailable_runtime_results(manifest: dict, path: Path, cases) -> dict:
    """Only an attributed native Candidate build failure consumes a dev round."""
    classification=('candidate_build_failure' if manifest.get('classification')=='candidate_build_failure'
                    else 'infrastructure-invalid')
    return {case_id:{'case_id':case_id,'candidate_runtime_ready':False,
        'candidate_runtime_manifest':str(path),'classification':classification,
        'build_classification':manifest.get('classification','unknown'),
        'classification_reason':manifest.get('classification_reason','runtime outcome unknown'),
        'broker_stats_delta':{'calls':0,'failures':0,'successful_calls':0,
            'input_tokens':0,'output_tokens':0,'total_tokens':0,'tokens':0}}
        for case_id in cases}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_container_id(cidfile: Path) -> str | None:
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


# Docker 29 removes an exited or stopped --rm container asynchronously: `docker rm -f` then answers
# "removal of container ... is already in progress" and `docker inspect` still shows it for a few seconds.
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5


def removal_in_progress(removed: subprocess.CompletedProcess) -> bool:
    return removed.returncode != 0 and "already in progress" in ((removed.stdout or "") + (removed.stderr or "")).lower()


def await_daemon_removal(container_id: str) -> None:
    """Wait (bounded) until the daemon has finished removing a container; absence is judged afterwards."""
    wait_until = time.monotonic() + REMOVAL_WAIT_SECONDS
    while time.monotonic() < wait_until:
        try:
            if subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True,
                              check=False).returncode:
                return
        except (OSError, subprocess.SubprocessError):
            return
        time.sleep(REMOVAL_POLL_SECONDS)


def cleanup_owned_container(role: str, cidfile: Path, *, attempted: bool) -> dict[str, Any]:
    """Clean only a container ID captured by this run; fail closed otherwise."""
    container_id = read_container_id(cidfile)
    result: dict[str, Any] = {
        "role": role,
        "cidfile": str(cidfile),
        "container_id": container_id,
        "startup_attempted": attempted,
        "ownership_proven": container_id is not None,
        "cleanup_attempted": container_id is not None,
        "absent_after_cleanup": False,
    }
    if not attempted:
        result.update({"status": "not_started", "absent_after_cleanup": True})
        return result
    if container_id is None:
        result.update({
            "status": "ownership_unproven",
            "cleanup_error": "Docker startup was attempted but no current-run container ID was captured",
        })
        return result
    try:
        removed = subprocess.run(["docker", "rm", "-f", container_id], text=True, capture_output=True, check=False)
    except OSError as exc:
        result.update({
            "status": "cleanup_error",
            "cleanup_error": f"docker rm failed: {type(exc).__name__}: {exc}",
        })
        return result
    if removed.returncode:
        result["remove_stderr"] = (removed.stderr or "")[-500:]
    if removal_in_progress(removed):
        result["removal_in_progress_at_rm"] = True
        await_daemon_removal(container_id)
    try:
        inspected = subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True, check=False)
    except OSError as exc:
        result.update({
            "remove_exit_code": removed.returncode,
            "status": "cleanup_error",
            "cleanup_error": f"docker inspect failed: {type(exc).__name__}: {exc}",
        })
        return result
    inspect_text = ((inspected.stdout or "") + (inspected.stderr or "")).strip()
    absent_text = inspect_text.lower()
    absent = inspected.returncode != 0 and (
        "no such object" in absent_text or "no such container" in absent_text
    )
    result.update({
        "remove_exit_code": removed.returncode,
        "inspect_exit_code": inspected.returncode,
        "absent_after_cleanup": absent,
        "status": "absent" if absent else "cleanup_unverified",
    })
    if not absent:
        result["cleanup_error"] = inspect_text[-1000:] or "docker inspect did not prove absence"
    return result


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_broker(*, name: str, script: Path, credential: Path, port: int, builder: bool, image: str, cidfile: Path, defer_removal: bool = False) -> Any:
    if script.resolve() == JUDGE_BROKER_SCRIPT:
        if not builder:
            raise ValueError("Result judge cannot serve the lower role")
        return _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=cidfile, defer_removal=defer_removal)
    cidfile.parent.mkdir(parents=True, exist_ok=True)
    evidence_dir = None
    if not builder:
        if cidfile.exists():
            raise RuntimeError('lower broker CID file already exists; preserve ownership history')
        evidence_dir = cidfile.with_suffix('.broker-evidence').resolve()
        evidence_dir.mkdir(exist_ok=False)
    else:
        cidfile.unlink(missing_ok=True)
    command = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{script.resolve()}:/broker.py:ro",
        "-v", "@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py:/responses_stream.py:ro",
        "-v", f"{credential.resolve()}:/run/secrets/agentswe.env:ro",
        "--cidfile", str(cidfile.resolve()),
    ]
    if evidence_dir is not None:
        command += ['-v', f'{evidence_dir}:/broker-evidence', '-e', 'AGENTSWE_EVALUATOR_PROXY_URL=']
    command += [image, 'python3', '/broker.py', '--credential-file', '/run/secrets/agentswe.env',
                '--bind', '0.0.0.0' if builder else '127.0.0.1', '--port', str(port)]
    if builder:
        command.append("--builder")
    else:
        command += ['--stats-output', '/broker-evidence/stats.json']
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                if response.status == 200:
                    if not builder:
                        protocol = json.loads(response.read()).get('protocol', {})
                        if (protocol.get('model') != MODEL or protocol.get('reasoning_effort') != LOWER_EFFORT
                                or protocol.get('inner_retries') != 0
                                or protocol.get('absolute_case_deadline_required') is not True):
                            raise ValueError('lower broker protocol preflight failed')
                        stats_request = urllib.request.Request(f'http://127.0.0.1:{port}/stats',
                            headers={'Authorization': 'Bearer stats-only-placeholder'})
                        with urllib.request.urlopen(stats_request, timeout=2) as stats_response:
                            stats = json.loads(stats_response.read())
                        if stats.get('runtime', {}).get('calls') != 0 or not stats.get('broker_instance_id'):
                            raise ValueError('lower broker initial statistics are not fresh')
                        persisted = json.loads((evidence_dir / 'stats.json').read_text())
                        if persisted.get('broker_instance_id') != stats['broker_instance_id']:
                            raise ValueError('lower broker HTTP instance does not match owned evidence')
                        cid = cidfile.read_text().strip()
                        inspected = json.loads(subprocess.check_output(['docker', 'inspect', cid], text=True))[0]
                        if inspected.get('Id') != cid or inspected.get('Name') != '/' + name or not inspected.get('State', {}).get('Running'):
                            raise ValueError('lower broker container identity is not live and owned')
                        mounts = {item['Destination']: item for item in inspected.get('Mounts', [])}
                        for target, source, writable in [('/broker.py', script.resolve(), False),
                            ('/run/secrets/agentswe.env', credential.resolve(), False),
                            ('/broker-evidence', evidence_dir, True)]:
                            mount = mounts.get(target, {})
                            if mount.get('Source') != str(source) or mount.get('RW') is not writable:
                                raise ValueError('lower broker container mount identity mismatch')
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"broker {name} did not become healthy")


def start_builder_broker(*, name: str, credential: Path, port: int, image: str, cidfile: Path) -> None:
    sys.path.insert(0, "@@AGENTSWE_EDITING_CONTROL@@")
    import builder_broker_runtime
    return builder_broker_runtime.start_builder_broker(name=name, credential=credential, image=image, port=port, cidfile=cidfile)


def builder_task(
    *,
    run_dir: Path,
    public: Path,
    workspace: Path,
    worktree: Path,
    controller: BuilderSessionController,
    provider_config: Path,
    pilot: bool = False,
    readiness_profile: str | None = None,
    task_timeout_seconds: float | None = None,
) -> Path:
    # The generated [agent] timeout has to fire inside the caller's outer wait.
    # The pilot/readiness branch keeps its historical 8 h cap unchanged.
    if task_timeout_seconds is None:
        task_timeout_seconds = (PILOT_BUILDER_TASK_TIMEOUT_SECONDS if pilot
                                else GENERATED_BUILDER_TASK_TIMEOUT_SECONDS)
    task = run_dir / "builder_task"
    if not (worktree / ".git").exists():
        for command in (["git", "init", "-q"], ["git", "config", "user.email", "builder@invalid"], ["git", "config", "user.name", "Builder"], ["git", "add", "-A"], ["git", "commit", "-qm", "pristine baseline"]):
            subprocess.run(command, cwd=worktree, check=True)
    for directory in (task / "environment", task / "tests", task / "solution"):
        directory.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(
        f'''schema_version = "1.4"
[task]
name = "local/openclaw-channel-handoff-agentloop-builder"
version = "1.0.0"
description = "OpenClaw Edit Builder {'one-case pilot' if pilot else 'two-public-dev formal'} feedback lifecycle"
artifacts = [{{ source = "/workspace/submission", destination = "builder_submission" }}]
[agent]
timeout_sec = {float(task_timeout_seconds)}
user = "root"
network_mode = "public"
[verifier]
timeout_sec = 300.0
user = "root"
environment_mode = "shared"
network_mode = "no-network"
[environment]
docker_image = "{BUILDER_IMAGE}"
network_mode = "public"
build_timeout_sec = 900.0
cpus = 8
memory_mb = 16384
storage_mb = 32768
workdir = "/workspace"
[environment.healthcheck]
command = "python3 /usr/local/lib/agentswe-builder-resource-check.py && python3 /opt/agentswe-openclaw/dependency_check.py"
retries = 1
timeout_sec = 20.0
''',
        encoding="utf-8",
    )
    public_description = "dev_001 only" if pilot else "both dev_001 and dev_002"
    # Under the readiness profile the delivery carries four further fields and
    # the round count is fixed at two. The default text above ends by permitting
    # a single accepted submission, which is true of the ten-round lifecycle and
    # wrong here, so this paragraph is appended rather than woven in.
    readiness_requirements = "" if not readiness_profile else """
Under this readiness profile, complete exactly two accepted rounds and stop.
run_report.json must carry four further fields in both rounds. Round 1:
builder_session_id (your session id), submission_number 1,
revision_of_candidate_digest null, feedback_digest null. Round 2: the same
builder_session_id, submission_number 2, revision_of_candidate_digest set to the
`delivery_digest` field (top level, also under `submission.delivery_digest`) of the
evaluator's accepted round-1 response -- NOT `candidate_digest`, which is the
materialized product digest and is rejected here -- and feedback_digest set
to the exact feedback digest you acknowledged. Round 2's edit_report.json must
also carry feedback_response: a string of prose that you write, explaining how
this revision answers the feedback. It is your explanation, not a copy of the
evaluator's feedback object."""
    hidden_description = "test_001 only as a non-formal pilot" if pilot else "the six hidden cases"
    (task / "instruction.md").write_text(
        f"""# OpenClaw Edit Builder — one continuous session

You are the single upper Builder. Use deepseek-flash with xhigh reasoning. Read
the four files under /builder-package/input, {public_description}, and the
repository under /builder-package/input/repository. Do not inspect hidden cases,
evaluator source, credentials, prior runs, or Candidate snapshots.

Work on the writable repository at /workspace/worktree. Keep exactly
solution.patch, edit_report.json, and run_report.json under
/workspace/submission. Generate solution.patch from the repository-relative
diff in /workspace/worktree; patch paths must be a/<repo-path> and b/<repo-path>
and must never include builder-package/input/repository. First create a complete
initial solution and run
submit_dev_candidate --submit --wait. The evaluator runs {public_description}
through the real OpenClaw Gateway and returns redacted authoritative feedback.
Then run submit_dev_candidate --status, read the feedback, revise when useful,
regenerate all three delivery files, and submit another distinct digest with
submit_dev_candidate --submit --feedback-digest <exact previous digest> --wait. Continue for up to
{controller.lifecycle.max_dev_rounds} accepted rounds or exit when satisfied.
Duplicate digests are idempotent and dev mean above 60 is record-only.
{hidden_description} runs only after the latest accepted snapshot is frozen.
This run is {'a pipeline pilot and never publishes formal Result or Code' if pilot else 'the formal lifecycle'}.

The pinned Codex image uses the task-local offline Node 24.15.0 and pnpm
11.15.1 tools at /opt/agentswe-openclaw/bin. Begin each shell command with
export PATH=/opt/agentswe-openclaw/bin:$PATH, or use the absolute tool paths.
Verified prewarmed node_modules are mounted read-only into the writable
worktree, including workspace packages. Use the existing pnpm and
node_modules/.bin tools; dependency installation and runtime downloads are
unnecessary. The evaluator does not use Builder-created dependency caches.
Submit receives full public feedback with an evaluator-owned native thread ID,
Candidate digest and feedback digest. On later submissions, pass the exact
preceding digest explicitly; do not substitute a status query for reading the
full feedback. Exit only after the latest submission has returned HTTP 200;
one accepted submission followed by Builder exit is permitted.
{readiness_requirements}
""",
        encoding="utf-8",
    )
    submit = task / "environment" / "submit_dev_candidate"
    submit.write_text("#!/usr/bin/env python3\nimport json,os,socket,sys\nsock_path=os.environ['AGENTSWE_DEV_CONTROLLER_SOCKET'];token=os.environ['AGENTSWE_DEV_CONTROLLER_TOKEN']\ndef call(payload):\n with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:\n  sock.settimeout(10000);sock.connect(sock_path);sock.sendall((json.dumps({'token':token,**payload})+'\\n').encode())\n  return json.loads(sock.makefile().readline())\nstatus=call({'action':'status'})\nif status.get('status')!=200:\n print(json.dumps(status,ensure_ascii=False));raise SystemExit(1)\nsession=status['payload']['builder_session_id']\nack=None\nif '--feedback-digest' in sys.argv:\n ack=sys.argv[sys.argv.index('--feedback-digest')+1]\nif '--submit' in sys.argv:result=call({'action':'submit','builder_session_id':session,'feedback_digest':ack})\nelif '--ack-feedback' in sys.argv:result=call({'action':'ack_feedback','builder_session_id':session,'feedback_digest':ack})\nelse:result=status\nprint(json.dumps(result,ensure_ascii=False))\nraise SystemExit(0 if result.get('status')==200 else 1)\n", encoding="utf-8")
    submit.chmod(0o755)
    test = task / "tests" / "test.sh"
    test.write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    test.chmod(0o755)
    resource_check = task / "environment/builder_resource_check.py"
    shutil.copyfile(ROOT / "harbor/builder_resource_check.py", resource_check)
    dependency_mounts, dependency_proof = prepare_builder_dependencies(run_dir, public, worktree)
    write_json(run_dir / "builder_dependency_preflight.json", dependency_proof)
    write_json(task / "environment/docker-compose.yaml", {"services": {"main": {"cpu_quota": 800000, "cpu_period": 100000, "volumes": dependency_mounts + [
        {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
        {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
        {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
        {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
        {"type": "bind", "source": str(controller.socket_path), "target": "/run/openclaw-builder.sock", "read_only": True},
        {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
        {"type": "bind", "source": str(provider_config), "target": str(provider_config), "read_only": True},
    ], "environment": {
        "AGENTSWE_DEV_CONTROLLER_SOCKET": "/run/openclaw-builder.sock",
        "AGENTSWE_DEV_CONTROLLER_TOKEN": controller.token,
    }}}})
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"openclaw-agentloop-builder-{run_dir.name}",
        "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1,
        "n_concurrent_trials": 1, "quiet": True, "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": True},
        "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL, "env": {
            "CODEX_HOME": "/tmp/agentswe-codex-home",
            "CODEX_CONFIG_TOML_PATH": str(provider_config),
            }, "kwargs": {"reasoning_effort": BUILDER_EFFORT, "web_search": "disabled"}}],
        "tasks": [{"path": str(task)}],
    })
    return config


def run_harbor(harbor, config, run_dir, timeout_seconds, *, controller, credential):
    done = run_native_builder(lifecycle=controller, config=config, credential=credential,
        harbor=harbor, timeout=timeout_seconds)
    return done.returncode, str(run_dir / 'builder.stdout.log'), str(run_dir / 'builder.stderr.log')


def verify_readiness_binding(args: argparse.Namespace) -> dict[str, Any] | None:
    """Verify the readiness binding through the coordinator's own verifier.

    The binding covers all ten tasks, so this refuses anything the shared
    ``verify_binding`` does not reproduce byte for byte rather than trusting the
    file it was handed.
    """
    profile = getattr(args, "readiness_profile", None)
    if not profile:
        return None
    if profile != READINESS_PROFILE:
        raise ValueError("unsupported readiness profile")
    if not args.readiness_binding_file or not args.readiness_binding_sha256:
        raise ValueError("readiness requires binding file and SHA256")
    path = Path(args.readiness_binding_file).resolve()
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != args.readiness_binding_sha256:
        raise ValueError("readiness binding bytes changed")
    value = read_json(path)
    control_root = Path(os.environ.get("READINESS_CONTROL_ROOT", "@@AGENTSWE_EDITING_CONTROL@@"))
    verifier = control_root / "readiness_binding.py"
    if not verifier.is_file():
        raise ValueError("readiness binding verifier missing")
    import importlib.util
    if str(control_root) not in sys.path:
        sys.path.insert(0, str(control_root))
    spec = importlib.util.spec_from_file_location("readiness_binding", verifier)
    if spec is None or spec.loader is None:
        raise ValueError("readiness binding verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = Path(os.environ.get("READINESS_SOURCE_OVERRIDE", str(ROOT))).resolve()
    measured = module.verify_binding(source, value, control_root=control_root)
    if measured != value:
        raise ValueError("readiness binding changed during verification")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--run-formal", action="store_true", help="explicitly start evaluator brokers, Harbor, provider calls, and optional hidden execution")
    parser.add_argument("--pilot", action="store_true", help="run the isolated dev_001 -> feedback -> dev_001 -> freeze -> test_001 pipeline pilot")
    parser.add_argument("--self-test", action="store_true", help="run provider-free orchestration checks and exit")
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--runtime", type=Path, default=Path("@@AGENTSWE_ENVS@@/openclaw-channel-handoff-ledger-edit-v1"))
    parser.add_argument("--broker-script", type=Path, default=ROOT / "broker/responses_broker.py")
    parser.add_argument("--broker-image", default=BUILDER_IMAGE)
    parser.add_argument("--run-hidden", action="store_true")
    parser.add_argument("--builder-timeout", type=int, default=DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS)
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--readiness-profile")
    parser.add_argument("--readiness-binding-file", type=Path)
    parser.add_argument("--readiness-binding-sha256")
    args = parser.parse_args(argv)
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent must equal 1")
    if args.run_formal and not args.pilot:
        args.run_hidden = True
    pilot_static = args.pilot
    if args.readiness_profile:
        # The profile is the reduced one-case lifecycle, which on openclaw is
        # exactly what --pilot selects. Refusing here keeps a readiness tag from
        # ever naming a run that used the formal case set.
        if not args.pilot:
            parser.error("--readiness-profile requires --pilot")
        if args.max_dev_rounds != 2:
            parser.error("readiness profile requires --max-dev-rounds 2")
    # Structural checks first, so a missing --pilot is reported as a missing
    # --pilot rather than as a missing binding file.
    readiness_binding = verify_readiness_binding(args)
    static = {
        "schema_version": "openclaw-agentloop-one-stop-static/v1",
        "status": "static_config_ready",
        "formal_execution_started": False,
        "network_calls": 0,
        "docker_started": False,
        "harbor_started": False,
        "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True},
        "lower_agent": {"product": "OpenClaw", "model": MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": "broker-only-placeholder", "public_broker_phase": "Builder/public after each accepted submission", "hidden_broker_phase": "fresh after latest accepted Candidate freeze", "hidden_broker_initial_calls_required": 0},
        "execution_mode": "pilot" if pilot_static else "static",
        "public_cases": ["dev_001"] if pilot_static else ["dev_001", "dev_002"],
        "hidden_cases": ["test_001"] if pilot_static else [f"test_{index:03d}" for index in range(1, 7)],
        "hidden_after_freeze": True,
        "formal_result_claimed": False,
        "code_score_claimed": False,
    }
    if args.self_test:
        if args.run_formal:
            parser.error("--self-test and --run-formal are mutually exclusive")
        print(json.dumps({**static, "self_test": "PASS", "pilot_execution_started": False}, indent=2, ensure_ascii=False))
        return 0
    if not args.run_formal and not args.pilot:
        if args.run_dir:
            args.run_dir.mkdir(parents=True, exist_ok=True)
            write_json(args.run_dir / "one_stop_summary.json", {**static, "combined_score": None, "result_judge_contracts": {}, "code_contract": None, "cleanup_attestation": {"completed": True, "nothing_started": True}})
        print(json.dumps(static, indent=2, ensure_ascii=False))
        return 0
    if args.run_formal and args.pilot:
        parser.error("--run-formal and --pilot are mutually exclusive")
    if args.run_hidden and not args.run_formal:
        parser.error("--run-hidden requires --run-formal")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    try:
        timeout_contract = builder_timeout_contract(args.builder_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not timeout_contract["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({timeout_contract['required_outer_timeout_seconds']} seconds required)")
    if args.run_dir is None:
        parser.error("--run-dir is required")
    run_dir = args.run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    if readiness_binding is not None:
        # native_builder_runner decides defer_removal by asking whether this
        # exact filename exists, and nothing else writes it. Without it the
        # Builder container is removed outright and retained_manifest refuses
        # with 'Builder terminal retention is unproven'.
        write_json(run_dir / "readiness_current_binding.json", readiness_binding)
        # Written after mkdir: creating the directory earlier would trip the
        # non-empty guard above.
        write_json(run_dir / "readiness_profile.json", {
            "schema_version": "openclaw-agentloop-readiness-marker-v1",
            "readiness_profile": args.readiness_profile,
            "public_cases": list(READINESS_PUBLIC_CASES),
            "hidden_cases": list(READINESS_HIDDEN_CASES),
            "max_dev_rounds": args.max_dev_rounds,
            "binding": readiness_binding,
            "recorded_at": now(),
        })
    if not args.run_formal and not args.pilot:
        write_json(run_dir / "protocol_lock.json", static)
        write_json(run_dir / "summary.json", static)
        print(json.dumps(static, indent=2, ensure_ascii=False))
        return 0
    for path in (args.harbor, args.credential_file, args.broker_script, ROOT / "input/repository"):
        if not path.exists():
            raise FileNotFoundError(path)
    public_case_ids = ("dev_001",) if args.pilot else ("dev_001", "dev_002")
    hidden_case_ids = ("test_001",) if args.pilot else tuple(f"test_{index:03d}" for index in range(1, 7))
    evidence_kind = "pilot" if args.pilot else "formal"
    public = run_dir / "builder_public_package"
    shutil.copytree(ROOT / "input", public / "input", symlinks=True)
    (public / "dev_cases").mkdir(parents=True)
    for entry in (ROOT / "dev_cases").iterdir():
        if entry.is_dir():
            if entry.name in public_case_ids:
                shutil.copytree(entry, public / "dev_cases" / entry.name, symlinks=True)
        else:
            shutil.copy2(entry, public / "dev_cases" / entry.name)
    workspace = run_dir / "builder_workspace" / "submission"
    workspace.mkdir(parents=True)
    worktree = run_dir / "builder_workspace" / "worktree"
    shutil.copytree(ROOT / "input/repository", worktree, symlinks=True)
    lower_port, builder_port, judge_port = free_port(), free_port(), free_port()
    while len({lower_port, builder_port, judge_port}) != 3:
        builder_port, judge_port = free_port(), free_port()
    public_lower_endpoint = f"http://127.0.0.1:{lower_port}/v1/responses"
    builder_endpoint = f"http://172.17.0.1:{builder_port}/v1/responses"
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:12]
    public_lower_name = f"openclaw-public-lower-{suffix}"
    hidden_lower_name = f"openclaw-hidden-lower-{suffix}"
    builder_name = f"openclaw-builder-{suffix}"
    judge_name = f"openclaw-result-judge-{suffix}"
    cidfiles = {
        "public_lower": run_dir / "brokers/public_lower.cid",
        "hidden_lower": run_dir / "brokers/hidden_lower.cid",
        "builder": run_dir / "brokers/builder.cid",
        "judge": run_dir / "brokers/judge.cid",
        "readiness_judge": run_dir / "brokers/readiness_judge.cid",
    }
    hidden_lower_endpoint: str | None = None
    started_containers: list[str] = []
    attempted = {key: False for key in cidfiles}

    def start_owned(name: str, starter: Any, **kwargs: Any) -> Any:
        """Start one container; outer finally owns all ID-based cleanup.

        Returns whatever the starter returned: the judge broker handle is the
        only way to close it with retention evidence instead of removing it.
        """
        try:
            handle = starter(name=name, **kwargs)
        except Exception:
            # These starts happen before the main try/finally block.  If a
            # later broker fails, clean only IDs captured by this run; never
            # fall back to the deterministic name.
            cleanup_results = [
                cleanup_owned_container(key, cidfile, attempted=attempted[key])
                for key, cidfile in cidfiles.items()
            ]
            write_json(run_dir / "summary.json", {
                "schema_version": "openclaw-agentloop-startup-failure-summary-v1",
                "evidence_kind": evidence_kind,
                "status": "startup_infrastructure_failure",
                "formal_result_claimed": False,
                "formal_code_score_claimed": False,
                "error": "broker startup failed before lifecycle entered its main try/finally",
            })
            write_json(run_dir / "cleanup_attestation.json", {
                "schema_version": "agentswe-cleanup-attestation-v3",
                "startup_failure": True,
                "owned_container_cleanup": cleanup_results,
                "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
                "controller_closed": True,
                "unrelated_containers_touched": False,
            })
            raise
        # The append has to follow a successful start.  While it sat after a
        # ``try`` that returned it was unreachable, started_containers stayed
        # empty, and judge_broker_stats.json was always {"status":
        # "not_started"} even though the judge broker had run.
        started_containers.append(name)
        return handle

    attempted["public_lower"] = True
    start_owned(public_lower_name, start_broker, script=args.broker_script, credential=args.credential_file,
                port=lower_port, builder=False, image=args.broker_image, cidfile=cidfiles["public_lower"])
    # Public dev requires the same independent semantic judge in a pilot too.
    attempted["judge"] = True
    judge_handle = start_owned(judge_name, start_broker, script=JUDGE_BROKER_SCRIPT,
                credential=args.credential_file,
                port=judge_port, builder=True, image=args.broker_image, cidfile=cidfiles["judge"],
                # Handed to the coordinator under readiness, so it has to
                # outlive its own stop; --rm would delete it there and then.
                defer_removal=readiness_binding is not None)
    controller: BuilderSessionController | None = None
    runtime_manifests: dict[int, Path] = {}
    try:
        provider_config = run_dir / "builder_provider.toml"
        direct.write_provider(provider_config)

        def evaluate(number: int, candidate: Path) -> dict[str, Any]:
            from evaluator.public_evaluation import evaluate_owned
            observed = evaluate_owned(run_dir=run_dir,candidate=candidate,number=number,
                runtime=args.runtime,public_case_ids=public_case_ids,
                public_lower_endpoint=public_lower_endpoint,judge_endpoint=judge_endpoint)
            runtime_manifests[number] = Path(observed["runtime_manifest"])
            return observed["results"]

        controller = BuilderSessionController(
            run_dir=run_dir,
            workspace=workspace,
            source=ROOT / "input/repository",
            evaluate=evaluate,
            public_case_ids=public_case_ids,
            hidden_case_ids=hidden_case_ids,
            evidence_kind=evidence_kind,
            max_dev_rounds=args.max_dev_rounds,
            n_concurrent=args.n_concurrent,
            readiness_profile=args.readiness_profile,
            current_binding=readiness_binding,
        )
        controller.start()
        config = builder_task(run_dir=run_dir, public=public, workspace=workspace,
                              worktree=worktree, controller=controller,
                              provider_config=provider_config, pilot=args.pilot,
                              readiness_profile=args.readiness_profile,
                              task_timeout_seconds=(
                                  PILOT_BUILDER_TASK_TIMEOUT_SECONDS if args.pilot
                                  else timeout_contract["generated_task_timeout_seconds"]))
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "openclaw-agentloop-pilot-protocol-v1" if args.pilot else "openclaw-agentloop-formal-protocol-v1",
            "evidence_kind": evidence_kind,
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True, "visibility": ["input", "dev_cases", "repository"]},
            "lower_agent": {"product": "OpenClaw", "model": MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": "broker-only-placeholder", "public_broker_endpoint": public_lower_endpoint, "hidden_broker_endpoint": None},
            "fresh_hidden_broker_after_freeze": True,
            "timeout_contract": timeout_contract,
            "max_dev_rounds": args.max_dev_rounds, "n_concurrent": args.n_concurrent,
            "dev_passed_is_automatic_freeze": False, "public_cases_per_candidate": list(public_case_ids),
            "hidden_cases": list(hidden_case_ids), "hidden_after_freeze": True,
            "formal_finalizer_allowed": not args.pilot,
            "formal_result_publishable": False,
            "code_score_publishable": False,
        })
        started_at = now()
        builder_code, builder_stdout, builder_stderr = run_harbor(args.harbor.resolve(), config, run_dir, args.builder_timeout, controller=controller, credential=args.credential_file)
        finished_at = now()
        controller.close()
        if controller.submissions and not controller.frozen:
            controller.freeze_on_builder_exit()
        attestation = controller.write_attestation(builder_exit_code=builder_code, builder_started_at=started_at,
                                                   builder_finished_at=finished_at,
                                                   interrupt_exit_codes=BUILDER_INTERRUPT_EXIT_CODES)
        write_json(run_dir / "builder_process.json", {"exit_code": builder_code, "stdout": builder_stdout, "stderr": builder_stderr})
        summary: dict[str, Any] = {
            "schema_version": "openclaw-agentloop-pilot-summary-v1" if args.pilot else "openclaw-agentloop-lifecycle-v1",
            "evidence_kind": evidence_kind,
            "pilot": args.pilot,
            "status": "builder_lifecycle_ready_for_hidden" if attestation.get("pilot_lifecycle_eligible" if args.pilot else "formal_lifecycle_eligible") else "builder_lifecycle_incomplete",
            "formal_result_claimed": False, "formal_code_score_claimed": False,
            "result_axis": "N/A",
            "code_axis": "N/A",
            "builder_session_attestation": str(run_dir / "builder_session_attestation.json"),
            "timeout_contract": timeout_contract,
            "candidate_runtime_manifests": {str(k): str(v) for k, v in runtime_manifests.items()},
        }
        latest_round = len(controller.submissions)
        if args.pilot and attestation.get("pilot_lifecycle_eligible") and latest_round in runtime_manifests:
            hidden_port = free_port()
            hidden_lower_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
            attempted["hidden_lower"] = True
            start_broker(name=hidden_lower_name, script=args.broker_script, credential=args.credential_file, port=hidden_port, builder=False, image=args.broker_image, cidfile=cidfiles["hidden_lower"])
            hidden_broker_before = read_broker_stats(hidden_lower_endpoint)
            if runtime_stats(hidden_broker_before)["calls"] != 0:
                raise RuntimeError("fresh pilot hidden broker did not start at zero calls")
            write_json(run_dir / "pilot_hidden_broker_before.json", hidden_broker_before)
            protocol = read_json(run_dir / "protocol_lock.json")
            protocol["lower_agent"]["hidden_broker_endpoint"] = hidden_lower_endpoint
            protocol["hidden_executor_endpoint_switched_after_freeze"] = True
            write_json(run_dir / "protocol_lock.json", protocol)
            hidden = run_suite(
                freeze_manifest_path=run_dir / "lifecycle/freeze_manifest.json",
                hidden_root=ROOT / "test_cases", output=run_dir / "pilot_hidden",
                broker_endpoint=hidden_lower_endpoint, runtime=args.runtime, timeout_seconds=CASE_TIMEOUT_SECONDS,
                pilot=True, case_ids=hidden_case_ids,
                builder_attestation_path=run_dir / "builder_session_attestation.json",
                runtime_product_manifest_path=runtime_manifests[latest_round],
            )
            summary["pilot_hidden"] = hidden
            summary["public_lower_broker"] = read_broker_stats(public_lower_endpoint)
            summary["hidden_lower_broker"] = read_broker_stats(hidden_lower_endpoint)
            summary["fresh_hidden_broker_initial_calls"] = 0
            summary["pilot_evidence_complete"] = hidden.get("pilot_evidence_complete") is True
            if args.readiness_profile and summary["pilot_evidence_complete"]:
                # Readiness replaces the pilot measurement with one independent
                # smoke per judge over the frozen candidate. The Result judge's
                # ledger enters the bundle whole and must hold exactly its own
                # single call, so it gets a broker started after the freeze
                # rather than the one public dev scoring has already used.
                readiness_judge_port = free_port()
                readiness_judge_endpoint = f"http://127.0.0.1:{readiness_judge_port}/v1/responses"
                attempted["readiness_judge"] = True
                start_owned(f"openclaw-readiness-judge-{suffix}", start_broker,
                            script=JUDGE_BROKER_SCRIPT, credential=args.credential_file,
                            port=readiness_judge_port, builder=True, image=args.broker_image,
                            cidfile=cidfiles["readiness_judge"])
                readiness_broker_before = read_broker_stats(readiness_judge_endpoint)
                if runtime_stats(readiness_broker_before)["calls"] != 0:
                    raise RuntimeError("fresh readiness Result-judge broker did not start at zero calls")
                write_json(run_dir / "readiness_judge_broker_before.json", readiness_broker_before)
                from evaluator.readiness_smoke import run as run_readiness_judges
                readiness = run_readiness_judges(
                    task_root=ROOT, run_dir=run_dir,
                    credential=args.credential_file, result_endpoint=readiness_judge_endpoint)
                # Recorded before cleanup: the bundle reads these bytes, never a
                # live endpoint that will not exist by then.
                write_json(run_dir / "readiness_judge_broker_stats.json",
                           read_broker_stats(readiness_judge_endpoint))
                complete = readiness["readiness_judges_complete"]
                summary.update({
                    "status": "readiness_evidence_complete" if complete else "readiness_evidence_incomplete",
                    "readiness_profile": args.readiness_profile,
                    "readiness_judge_smoke": "readiness_judge_smoke.json",
                    "formal_result_claimed": False, "code_score_claimed": False,
                    "score_threshold": None, "pipeline_ready": False,
                    "admission_required": True, "formal_finalizer_invoked": False,
                })
                write_json(run_dir / "summary.json", summary)
                return 0 if complete else 2
            pilot_measurement: dict[str, Any] | str = "N/A"
            pilot_record = (hidden.get("cases") or {}).get("test_001") if isinstance(hidden.get("cases"), dict) else None
            if summary["pilot_evidence_complete"] and isinstance(pilot_record, dict):
                from evaluator.formal_finalize import score_case

                pilot_measurement = score_case(pilot_record, run_dir / "pilot_hidden" / "test_001")
                pilot_measurement.update({
                    "schema_version": "openclaw-agentloop-pilot-measurement-v1",
                    "evidence_kind": "pilot",
                    "formal_result_publishable": False,
                    "code_score_publishable": False,
                })
                write_json(run_dir / "pilot_scoring" / "test_001.json", pilot_measurement)
            summary["pilot_measurement"] = pilot_measurement
            summary["status"] = "pilot_pipeline_complete" if summary["pilot_evidence_complete"] else "pilot_pipeline_incomplete"
            summary["formal_finalizer_invoked"] = False
        elif args.pilot:
            summary["pilot_hidden"] = {"status": "not_started", "reason": "Builder pilot lifecycle gate failed"}
            summary["pilot_evidence_complete"] = False
            summary["formal_finalizer_invoked"] = False
        elif args.run_hidden and attestation.get("formal_lifecycle_eligible") and latest_round in runtime_manifests:
            hidden_port = free_port()
            hidden_lower_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
            attempted["hidden_lower"] = True
            start_broker(name=hidden_lower_name, script=args.broker_script, credential=args.credential_file, port=hidden_port, builder=False, image=args.broker_image, cidfile=cidfiles["hidden_lower"])
            hidden_broker_before = read_broker_stats(hidden_lower_endpoint)
            if runtime_stats(hidden_broker_before)["calls"] != 0:
                raise RuntimeError("fresh formal hidden broker did not start at zero calls")
            write_json(run_dir / "hidden_broker_before.json", hidden_broker_before)
            protocol = read_json(run_dir / "protocol_lock.json")
            protocol["lower_agent"]["hidden_broker_endpoint"] = hidden_lower_endpoint
            protocol["hidden_executor_endpoint_switched_after_freeze"] = True
            write_json(run_dir / "protocol_lock.json", protocol)
            hidden = run_suite(
                freeze_manifest_path=run_dir / "lifecycle/freeze_manifest.json",
                hidden_root=ROOT / "test_cases", output=run_dir / "hidden",
                broker_endpoint=hidden_lower_endpoint, runtime=args.runtime, timeout_seconds=CASE_TIMEOUT_SECONDS,
                formal=True, builder_attestation_path=run_dir / "builder_session_attestation.json",
                runtime_product_manifest_path=runtime_manifests[latest_round],
            )
            summary["hidden"] = hidden
            summary["status"] = "formal_evidence_complete" if hidden.get("formal_evidence_valid") else "formal_evidence_invalid"
            if hidden.get("formal_evidence_valid"):
                finalizer_output = run_dir / "formal_aggregation.json"
                finalizer = subprocess.run([sys.executable, str(ROOT / "evaluator/formal_finalize.py"),
                                            "--run-dir", str(run_dir), "--output", str(finalizer_output),
                                            "--credential-file", str(args.credential_file),
                                            "--result-broker-endpoint", judge_endpoint,
                                            "--result-judge", str(SHARED_RESULT_JUDGE),
                                            "--code-judge", str(CREATE_CODE_JUDGE)],
                                           text=True, capture_output=True, check=False)
                aggregation = read_json(finalizer_output) if finalizer_output.is_file() else {}
                summary["formal_finalizer_exit"] = finalizer.returncode
                summary["formal_aggregation"] = aggregation
                summary["formal_result_claimed"] = aggregation.get("formal_result_publishable") is True
                summary["formal_code_score_claimed"] = aggregation.get("code_score_publishable") is True
                summary["status"] = "completed" if finalizer.returncode == 0 else "formal_finalization_refused"
        elif args.run_hidden:
            summary["hidden"] = {"status": "not_started", "reason": "Builder lifecycle gate failed"}
        write_json(run_dir / "summary.json", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["status"] in {"builder_lifecycle_ready_for_hidden", "completed", "pilot_pipeline_complete"} else 2
    finally:
        if controller is not None:
            controller.close()
        try:
            judge_stats = read_broker_stats(judge_endpoint) if judge_name in started_containers else {"status": "not_started"}
        except Exception as exc:
            judge_stats = {"error": f"{type(exc).__name__}: {exc}"}
        write_json(run_dir / "judge_broker_stats.json", judge_stats)
        retained_judge = None
        retention_error = None
        if readiness_binding is not None and judge_handle is not None:
            try:
                closed = judge_handle.close()
                if closed.get("absent_after_cleanup") is not True and judge_handle.container_id:
                    retained_judge = {"role": "judge", "container_id": judge_handle.container_id,
                                      "ownership_proven": True, "cleanup_attempted": False,
                                      "absent_after_cleanup": False, "removal_deferred": True,
                                      "retained_terminal": True, "close": closed}
            except Exception as exc:
                # Retention that cannot prove a stopped container must not become
                # a silently skipped cleanup; fall through and remove as usual.
                retention_error = f"{type(exc).__name__}: {exc}"
        cleanup_results = [
            cleanup_owned_container("hidden_lower", cidfiles["hidden_lower"], attempted=attempted["hidden_lower"]),
            cleanup_owned_container("public_lower", cidfiles["public_lower"], attempted=attempted["public_lower"]),
            cleanup_owned_container("builder", cidfiles["builder"], attempted=attempted["builder"]),
            retained_judge or cleanup_owned_container("judge", cidfiles["judge"], attempted=attempted["judge"]),
            cleanup_owned_container("readiness_judge", cidfiles["readiness_judge"],
                                    attempted=attempted["readiness_judge"]),
        ]
        cleanup = {"schema_version": "agentswe-cleanup-attestation-v3", "owned_container_cleanup": cleanup_results, "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results), "controller_closed": controller is None or not controller.socket_path.exists(), "unrelated_containers_touched": False}
        cleanup["completed"] = bool(cleanup["controller_closed"] and cleanup["all_started_containers_absent"])
        if retention_error is not None:
            cleanup.setdefault("cleanup_errors", []).append("judge retention: " + retention_error)
        if retained_judge is not None:
            # A retained container is deliberately not absent, so this run is
            # complete only once the coordinator has removed it.
            cleanup.update(removal_deferred=True, coordinator_cleanup_required=True, completed=False)
            try:
                import types as _types
                from harbor.readiness_resources import retained_manifest
                cleanup["retained_resources"] = retained_manifest(
                    run_dir, [_types.SimpleNamespace(container_id=retained_judge["container_id"])])
            except Exception as exc:
                cleanup.setdefault("cleanup_errors", []).append(
                    "retained_manifest: " + type(exc).__name__ + ": " + str(exc))
        write_json(run_dir / "cleanup_attestation.json", cleanup)
        summary = read_json(run_dir / "summary.json") if (run_dir / "summary.json").is_file() else {}
        aggregation = read_json(run_dir / "formal_aggregation.json") if (run_dir / "formal_aggregation.json").is_file() else {}
        freeze = read_json(run_dir / "lifecycle" / "freeze_manifest.json") if (run_dir / "lifecycle" / "freeze_manifest.json").is_file() else {}
        write_json(run_dir / "one_stop_summary.json", {"schema_version": "agentswe-edit-one-stop-summary-v1", "status": summary.get("status", "formal_execution_failed"), "dev_lifecycle": "lifecycle/dev_lifecycle.json", "freeze": {"path": "lifecycle/freeze_manifest.json", "digest": freeze.get("candidate_digest"), "reason": freeze.get("freeze_reason")}, "hidden_inventory": list(hidden_case_ids), "hidden_summary": summary.get("hidden", "N/A"), "result_judge_contracts": aggregation.get("result_judge_contracts", {}), "code_contract": aggregation.get("code_contract"), "result_axis": aggregation.get("result_axis", "N/A"), "code_axis": aggregation.get("code_axis", "N/A"), "combined_score": None, "cleanup_attestation": "cleanup_attestation.json", "judge_broker_stats": "judge_broker_stats.json"})


if __name__ == "__main__":
    raise SystemExit(main())
