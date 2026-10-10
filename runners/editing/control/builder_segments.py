"""Run the native Builder as ONE codex session across more than one Harbor trial.

This is the harness half of package 97.  The evidence half (``native_streams``,
``segment_ledger``, ``readiness_segment_streams`` in each tree's
``harbor/native_builder_evidence.py``, plus ``v2_readiness.py``) already accepts
a resumed session; this module is the only thing that *produces* one, and it is
implemented once here so that all ten trees share the same behaviour.

What it does, in order:

  0. ``prepare_run`` makes ``<run>/codex_home/`` and bind-mounts it into the
     evaluator-owned Builder compose, and points the Harbor agent at the
     evaluator-owned adapter ``agentswe_codex_resume:CodexResume`` (see that
     file).  Without a host-side CODEX_HOME a container that dies mid-turn takes
     its rollout with it and nothing can be resumed.
  0b. (package 105) on the gateway Builder machines only, ``prepare_run`` also pins
     the raised native transport retry policy -- ``request_max_retries=10``,
     ``stream_max_retries=10``, ``stream_idle_timeout_ms=300000`` (package 100,
     ``readiness_binding.configure_native_transport``) -- into the provider TOML
     the job config names in ``agents[0].env["CODEX_CONFIG_TOML_PATH"]``, which
     the ``agentswe_codex_resume:CodexResume`` adapter copies into the
     bind-mounted ``<run>/codex_home/config.toml`` at every launch.  This is the
     ONE place every Builder launch on a gateway host passes through -- readiness and
     formal, all ten trees, and every resumed segment (``derive_config`` copies
     the same env, so a resumed segment reads the same, already pinned file) --
     so the policy no longer depends on whether a given tree's own
     ``write_provider`` remembered to call it.  Before this, only five trees'
     READINESS writers called it (aider, deeptutor, codex, ai-scientist,
     openwiki) and no formal path in any tree did; the other five ran on codex's
     default 5 stream retries and died at "Reconnecting... 5/5" on gateway 503s.
     It refuses to launch if the keys cannot be pinned, and it is never active
     on a non-gateway host (different provider, different policy): see
     ``pin_native_retry_policy``.
  1. runs segment 1 through the tree's own launcher, unchanged.
  2. if the segment ended abnormally, has a codex session id, completed no turn
     and did not fail its thread, and there is remaining Builder budget, and the
     controller has not frozen, it launches segment N+1 as a *new Harbor job*
     that resumes the same codex session, inside the same still-open relay /
     auth / controller-socket contexts, with the same mounts, and with
     ``deadline - now`` of the ORIGINAL budget -- never a fresh one.
  3. the new trial directory is promoted into ``<run>/jobs/<job>/`` only once
     its ``agent/codex.txt`` exists, so a failed resume attempt leaves the run
     byte-identical to what it would have been without this module.
  4. ``<run>/builder_segments.json`` is written only when more than one segment
     was promoted, in the exact shape ``segment_ledger()`` validates.

A run that never resumes never writes a ledger, never promotes a trial and
never derives a config: it calls the tree's launcher once and returns its exit
code.  That is the single-segment parity guarantee, by construction.

Nothing here talks to a provider, reconstructs a request, retries a request or
handles a credential.  The resume is a new container running the unmodified
codex CLI against its own recorded session.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = "agentswe-builder-segments/v1"
LEDGER_NAME = "builder_segments.json"
RESUME_CAP = 2

CONTROL_PLANE = str(Path(__file__).resolve().parent)
AGENT_IMPORT_PATH = "agentswe_codex_resume:CodexResume"
REMOTE_CODEX_HOME = "/tmp/agentswe-codex-home"
CODEX_HOME_DIRNAME = "codex_home"
RESUME_SESSION_ENV = "AGENTSWE_CODEX_RESUME_SESSION_ID"

# --- package 105: the gateway native transport retry policy, on every launch -----
# The provider TOML the Harbor codex agent installs as $CODEX_HOME/config.toml.
CONFIG_TOML_ENV = "CODEX_CONFIG_TOML_PATH"
# Written next to the run; deliberately NOT the readiness writers' own
# ``readiness_native_retry_policy.json``, which must never be clobbered.
NATIVE_RETRY_RECEIPT = "builder_native_retry_policy.json"
RETRY_SCHEMA_VERSION = "agentswe-builder-native-retry/v1"
# The gateway Builder endpoint.  167 runs the same provider *id* (``gateway_direct``)
# against ``https://api.deepseek.com/v1`` with a different model and a different
# retry policy, so the endpoint -- not the section name -- is the discriminator.
GATEWAY_ENDPOINT_MARKER = "gateway.example.com"
# 167 is the DeepSeek machine and this module is not deployed there; the guard
# mirrors ``apply_session_recovery.sh`` so a stray copy can still never act.
NON_GATEWAY_HOSTNAME_SUFFIXES = ("-167", "167")

# Refuse to start a segment that cannot plausibly reach a submission: the
# resumed container still has to build, install and replay before it can work.
RESUME_MARGIN_SECONDS = 600.0
SEGMENT_ROOT = "builder_segments"


class BuilderSegmentError(RuntimeError):
    pass


class NativeRetryPolicyError(BuilderSegmentError):
    """The gateway transport retry policy could not be pinned: refuse to launch.

    Unlike every other ``prepare_run`` failure this one is NOT swallowed by
    ``run_segments``: a Builder that runs on codex's default 5 stream retries is
    the failure this package exists to prevent, so it must never start.
    """


# --------------------------------------------------------------------------
# run preparation: the host-side CODEX_HOME and the evaluator adapter
# --------------------------------------------------------------------------

def codex_home(run_dir: Path) -> Path:
    return Path(run_dir) / CODEX_HOME_DIRNAME


def codex_home_mount(run_dir: Path) -> dict:
    return {
        "type": "bind",
        "source": str(codex_home(run_dir)),
        "target": REMOTE_CODEX_HOME,
    }


def compose_path(run_dir: Path) -> Path:
    return Path(run_dir) / "builder_task/environment/docker-compose.yaml"


def host_is_gateway(hostname: str | None = None) -> bool:
    """False on a non-gateway host (e.g. the DeepSeek API), true on gateway Builder hosts."""
    name = (socket.gethostname() if hostname is None else str(hostname)).strip()
    return not name.endswith(NON_GATEWAY_HOSTNAME_SUFFIXES)


def pin_native_retry_policy(run_dir: Path, config: Path, *, hostname=None) -> dict:
    """Pin request/stream retries + SSE idle timeout on this run's provider TOML.

    Called once per run from ``prepare_run``, before any segment launches, so
    readiness, formal and every resumed segment of all ten trees get the same
    policy from one place.  ``readiness_binding.configure_native_transport`` is
    idempotent -- it appends the three keys when absent, accepts a file that
    already carries exactly these values (the five trees whose readiness writer
    already calls it), and raises when a file carries different values -- and it
    validates the provider model/effort, so it can never edit a provider that is
    not this machine's Builder provider.

    Refusals are loud: anything that leaves the policy unpinned on an gateway
    machine raises :class:`NativeRetryPolicyError` and the run does not start.
    """
    run_dir = Path(run_dir)
    host = socket.gethostname() if hostname is None else str(hostname)
    value = json.loads(Path(config).read_text(encoding="utf-8"))
    provider = (value["agents"][0].get("env") or {}).get(CONFIG_TOML_ENV)
    receipt = {
        "schema_version": RETRY_SCHEMA_VERSION,
        "hostname": host,
        "provider_config_path": provider,
        "job_config": str(config),
        "applied": False,
    }

    if not host_is_gateway(host):
        # 167 keeps its own provider and its own policy; nothing is touched.
        receipt["skipped"] = "not an gateway Builder machine"
        _write_retry_receipt(run_dir, receipt)
        return receipt
    if not provider:
        raise NativeRetryPolicyError(
            "the Builder job config carries no " + CONFIG_TOML_ENV
            + "; the gateway transport retry policy cannot be pinned"
        )
    path = Path(provider)
    if path.is_symlink() or not path.is_file():
        raise NativeRetryPolicyError(
            "missing or symlinked Builder provider config: " + str(path)
        )
    if not any(m in path.read_text(encoding="utf-8") for m in (GATEWAY_ENDPOINT_MARKER, "toolcode.cc")):
        # Not this policy's provider; leave it exactly as its author wrote it.
        receipt["skipped"] = "provider config is not an gateway endpoint"
        _write_retry_receipt(run_dir, receipt)
        return receipt

    ensure_importable()
    try:
        import readiness_binding

        policy = readiness_binding.configure_native_transport(path)
    except Exception as exc:
        raise NativeRetryPolicyError(
            "cannot pin the gateway transport retry policy on " + str(path)
            + ": " + type(exc).__name__ + ": " + str(exc)
        ) from exc
    receipt.update(policy)
    receipt["applied"] = True
    _write_retry_receipt(run_dir, receipt)
    return receipt


def _write_retry_receipt(run_dir: Path, receipt: dict) -> None:
    try:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        (Path(run_dir) / NATIVE_RETRY_RECEIPT).write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


# --- Lite: one codex tool surface for every Builder model ------------------
# codex 0.144.1 has no bundled metadata for non-GPT slugs such as deepseek-flash
# and falls back to generic metadata: no apply_patch tool, no reasoning field,
# multi_agent_v1 exposed.  The Lite catalog gives the Builder model an explicit
# entry (apply_patch freeform, multi-agent off).  Same mechanism as package
# 117b: copied into <run>/codex_home and pinned via top-level model_catalog_json.
LITE_MODEL_CATALOG_ASSET = Path(CONTROL_PLANE) / "codex_model_catalog_lite.json"
LITE_MODEL_CATALOG_SHA256 = "17ec93cb142438979e1ea9e8a915151074335885a1f4d2e12cfbdf27e4acae29"
LITE_FEATURES_TABLE = "[features]\nmulti_agent = false\n"
LITE_PROVIDER_HEADER = "[model_providers.gateway_direct]"
LITE_MODEL_CATALOG_KEY = "model_catalog_json"


def _lite_catalog_slugs(data: bytes) -> set:
    value = json.loads(data)
    rows = value.get("models") if isinstance(value, dict) else value
    return {row.get("slug") for row in rows or [] if isinstance(row, dict)}


def pin_lite_model_catalog(run_dir: Path, config: Path) -> dict:
    """Pin the Lite model catalog into this run's provider TOML (idempotent)."""
    value = json.loads(Path(config).read_text(encoding="utf-8"))
    provider = (value["agents"][0].get("env") or {}).get(CONFIG_TOML_ENV)
    if not provider:
        raise NativeRetryPolicyError("Lite catalog: job config carries no " + CONFIG_TOML_ENV)
    path = Path(provider)
    if path.is_symlink() or not path.is_file():
        raise NativeRetryPolicyError("Lite catalog: missing or symlinked provider config: " + str(path))
    if not LITE_MODEL_CATALOG_ASSET.is_file():
        raise NativeRetryPolicyError("Lite catalog asset missing: " + str(LITE_MODEL_CATALOG_ASSET))
    data = LITE_MODEL_CATALOG_ASSET.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != LITE_MODEL_CATALOG_SHA256:
        raise NativeRetryPolicyError("Lite catalog asset sha256 mismatch: " + digest)
    text = path.read_text(encoding="utf-8")
    model = None
    for line in text.splitlines():
        if line.replace(" ", "").startswith('model="'):
            model = line.split('"')[1]
            break
    if model not in _lite_catalog_slugs(data):
        raise NativeRetryPolicyError("Lite catalog has no entry for Builder model " + repr(model))
    home = codex_home(Path(run_dir)); home.mkdir(parents=True, exist_ok=True)
    dst = home / "model_catalog.json"
    if not dst.is_file() or dst.read_bytes() != data:
        dst.write_bytes(data)
    container_path = REMOTE_CODEX_HOME + "/model_catalog.json"
    if "multi_agent = false" not in text:
        if text.count(LITE_PROVIDER_HEADER) != 1:
            raise NativeRetryPolicyError("Lite catalog: provider header not unique in " + str(path))
        head, tail = text.split(LITE_PROVIDER_HEADER)
        if not head.endswith("\n"):
            head += "\n"
        text = head + LITE_FEATURES_TABLE + LITE_PROVIDER_HEADER + tail
    if LITE_MODEL_CATALOG_KEY not in text:
        text = LITE_MODEL_CATALOG_KEY + '="' + container_path + '"\n' + text
    path.write_text(text, encoding="utf-8")
    return {"pinned": True, "provider_config_path": str(path), "builder_model": model,
            "model_catalog_json": container_path, "catalog_sha256": digest}


def prepare_run(run_dir: Path, config: Path) -> dict:
    """Make the session survivable.  Idempotent; safe to call every launch.

    The compose file and the job config are both evaluator-owned and generated
    per run by ``formal_one_stop``; the Builder never sees either.
    """
    run_dir = Path(run_dir)
    home = codex_home(run_dir)
    home.mkdir(parents=True, exist_ok=True)
    receipt = {"codex_home": str(home), "remote_codex_home": REMOTE_CODEX_HOME}

    path = compose_path(run_dir)
    compose = json.loads(path.read_text(encoding="utf-8"))
    service = compose["services"]["main"]
    volumes = service.setdefault("volumes", [])
    mount = codex_home_mount(run_dir)
    if not any(
        isinstance(v, dict) and v.get("target") == REMOTE_CODEX_HOME for v in volumes
    ):
        volumes.append(mount)
        path.write_text(json.dumps(compose, indent=2) + "\n", encoding="utf-8")
        receipt["compose_mount_added"] = True
    else:
        receipt["compose_mount_added"] = False

    value = json.loads(Path(config).read_text(encoding="utf-8"))
    agent = value["agents"][0]
    receipt["agent_import_path"] = agent.get("import_path")
    receipt["agent_name"] = agent.get("name")
    if agent.get("import_path") != AGENT_IMPORT_PATH:
        raise BuilderSegmentError(
            "the Builder job config must use the evaluator adapter "
            + AGENT_IMPORT_PATH
        )
    # package 105: the gateway transport retry policy, before any segment launches.
    receipt["native_retry_policy"] = pin_native_retry_policy(run_dir, config)
    # Lite: the Lite model catalog, on every endpoint.
    receipt["lite_model_catalog"] = pin_lite_model_catalog(run_dir, config)
    return receipt


def control_plane_env(env: dict | None = None) -> dict:
    """An environment in which ``harbor run`` can import the resume adapter."""
    value = dict(env if env is not None else os.environ)
    parts = [p for p in (value.get("PYTHONPATH") or "").split(os.pathsep) if p]
    if CONTROL_PLANE not in parts:
        parts.insert(0, CONTROL_PLANE)
    value["PYTHONPATH"] = os.pathsep.join(parts)
    return value


def ensure_importable() -> None:
    """Make the adapter importable from this process too (tests, replay)."""
    if CONTROL_PLANE not in sys.path:
        sys.path.insert(0, CONTROL_PLANE)


# --------------------------------------------------------------------------
# reading what a finished segment left behind
# --------------------------------------------------------------------------

def job_identity(config: Path) -> tuple[Path, str]:
    value = json.loads(Path(config).read_text(encoding="utf-8"))
    jobs_dir = Path(value["jobs_dir"])
    job = value["job_name"]
    if not isinstance(job, str) or Path(job).name != job or not job:
        raise BuilderSegmentError("invalid native job identity")
    return jobs_dir, job


def trial_names(jobs_dir: Path, job: str) -> set[str]:
    base = Path(jobs_dir) / job
    if not base.is_dir():
        return set()
    return {p.name for p in base.iterdir() if p.is_dir()}


def stream_events(path: Path, *, live: bool = True) -> list[dict]:
    """The typed JSONL frames of a codex.txt, tolerating a torn final line."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    lines = data.splitlines()
    if data and not data.endswith(b"\n"):
        lines = lines[:-1] if live else lines
    result = []
    for line in lines:
        if not line.startswith(b'{"type":'):
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            result.append(value)
    return result


def stream_shape(path: Path) -> dict:
    """What the evaluator's cut rule needs, computed without importing a tree."""
    events = stream_events(path)
    threads = [
        v.get("thread_id") for v in events if v.get("type") == "thread.started"
    ]
    return {
        "exists": path.is_file(),
        "thread_ids": [t for t in threads if isinstance(t, str)],
        "turn_started": sum(1 for v in events if v.get("type") == "turn.started"),
        "turn_completed": sum(1 for v in events if v.get("type") == "turn.completed"),
        "turn_failed": sum(1 for v in events if v.get("type") == "turn.failed"),
        "thread_failed": sum(1 for v in events if v.get("type") == "thread.failed"),
    }


def rollout_paths(run_dir: Path) -> list[Path]:
    home = codex_home(run_dir)
    if not home.is_dir():
        return []
    return sorted(p for p in home.glob("sessions/**/rollout-*.jsonl") if p.is_file())


def session_identity(run_dir: Path) -> tuple[str | None, Path | None]:
    """The codex session id from the host-side rollout, or (None, None).

    A resume is only ever attempted when there is exactly one rollout and its
    ``session_meta`` carries an id; anything else is ambiguous and is refused.
    """
    paths = rollout_paths(run_dir)
    if len(paths) != 1:
        return None, None
    try:
        with paths[0].open("rb") as handle:
            first = handle.readline()
        value = json.loads(first)
    except (OSError, ValueError):
        return None, None
    if value.get("type") != "session_meta":
        return None, None
    identity = (value.get("payload") or {}).get("id")
    if not isinstance(identity, str) or not identity:
        return None, None
    return identity, paths[0]


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def trial_exception(jobs_dir: Path, job: str, trial: str):
    path = Path(jobs_dir) / job / trial / "result.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("exception_info")
    except (OSError, ValueError):
        return {"exception_type": "MissingTrialResult"}


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def resume_decision(
    *,
    code: int,
    shape: dict,
    session_id: str | None,
    segments_done: int,
    resume_cap: int,
    now_epoch: float,
    deadline_epoch: float,
    frozen,
    margin: float = RESUME_MARGIN_SECONDS,
) -> dict:
    """Should the harness launch another segment?  Reasons are recorded, always.

    Every clause is a refusal; the resume happens only when none of them fires.
    """
    reasons = []
    abnormal = code != 0 or shape.get("turn_completed", 0) == 0
    if not abnormal:
        reasons.append("the Builder segment terminated normally")
    if frozen:
        reasons.append("the controller already froze the Candidate")
    if segments_done > resume_cap:
        reasons.append("the Builder resume cap is exhausted")
    if not session_id:
        reasons.append("no codex session id was recorded on the host")
    if len(set(shape.get("thread_ids") or [])) != 1:
        reasons.append("the segment did not announce exactly one thread")
    elif session_id and shape["thread_ids"][0] != session_id:
        reasons.append("the recorded rollout is not this segment's thread")
    if shape.get("turn_started", 0) < 1:
        reasons.append("the segment never started a turn")
    if shape.get("turn_completed", 0):
        reasons.append("the segment completed its turn")
    if shape.get("thread_failed", 0):
        reasons.append("the segment failed its thread")
    remaining = deadline_epoch - now_epoch
    if remaining <= margin:
        reasons.append("the Builder budget has no room for another segment")
    return {
        "resume": not reasons,
        "refusals": reasons,
        "remaining_seconds": remaining,
        "exit_code": code,
        "stream": shape,
    }


# --------------------------------------------------------------------------
# launching segment N+1
# --------------------------------------------------------------------------

def derive_config(
    run_dir: Path, config: Path, index: int, session_id: str
) -> tuple[Path, Path]:
    """The segment-N config: same task, same mounts, a resumed codex session.

    Harbor refuses to reuse a job directory with a changed config
    (``harbor/job.py``: "cannot be resumed with a different config") and deletes
    trial directories that have no result.json, so segment N gets its own
    ``jobs_dir`` and its trial is promoted into ``<run>/jobs/<job>/`` afterwards.
    """
    value = json.loads(Path(config).read_text(encoding="utf-8"))
    root = Path(run_dir) / SEGMENT_ROOT / f"seg{index:03d}"
    root.mkdir(parents=True, exist_ok=True)
    value["jobs_dir"] = str(root / "jobs")
    agent = value["agents"][0]
    agent.setdefault("env", {})[RESUME_SESSION_ENV] = session_id
    path = root / "builder_job_config.json"
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path, Path(value["jobs_dir"])


def promote_trial(
    segment_jobs_dir: Path, jobs_dir: Path, job: str, existing: set[str]
) -> str | None:
    """Move a finished segment's trial beside segment 1, once it has a stream.

    Only a trial that actually left ``agent/codex.txt`` is promoted; anything
    else stays under ``<run>/builder_segments/`` as an audit record and the run
    keeps exactly the evidence it would have had without this module.
    """
    source_root = Path(segment_jobs_dir) / job
    if not source_root.is_dir():
        return None
    candidates = [
        p
        for p in sorted(source_root.iterdir())
        if p.is_dir() and (p / "agent/codex.txt").is_file()
    ]
    if len(candidates) != 1:
        return None
    source = candidates[0]
    name = source.name
    if name in existing:
        name = f"{name}-seg"
        if name in existing:
            return None
    target = Path(jobs_dir) / job / name
    if target.exists():
        return None
    shutil.move(str(source), str(target))
    return name


def observe_segment_container(run_dir: Path, observer) -> dict | None:
    """Record the resumed segment's container on the tree's own observer.

    The tree's ``BuilderResourceObserver`` stops after its first observation, so
    without this the resumed segment's container and compose network would never
    be in ``cleanup_owned``'s ownership set and would leak.  The row has the
    same shape the observer writes itself, and ``valid`` stays ANDed.
    """
    if observer is None:
        return None
    marker = str(Path(run_dir) / "builder_workspace/submission")
    try:
        ids = subprocess.check_output(["docker", "ps", "-q"], timeout=10).decode().split()
    except (OSError, subprocess.SubprocessError):
        return None
    for cid in ids:
        probe = subprocess.run(
            ["docker", "inspect", cid], capture_output=True, text=True, timeout=10
        )
        if probe.returncode:
            continue
        value = json.loads(probe.stdout)[0]
        if not any(
            m.get("Source") == marker and m.get("Destination") == "/workspace/submission"
            for m in value["Mounts"]
        ):
            continue
        if any(
            row.get("container_id") == value["Id"]
            for row in observer.proof["observations"]
        ):
            return None
        row = {
            "container_id": value["Id"],
            "container_name": value["Name"],
            "compose_project": value["Config"].get("Labels", {}).get(
                "com.docker.compose.project"
            ),
            "image_id": value["Image"],
            "pid": value["State"]["Pid"],
            "docker_memory_bytes": value["HostConfig"]["Memory"],
            "docker_cpu_quota": value["HostConfig"]["CpuQuota"],
            "docker_cpu_period": value["HostConfig"]["CpuPeriod"],
            "observed_epoch": time.time(),
            "builder_segment": True,
        }
        observer.proof["observations"].append(row)
        return row
    return None


def wait_for_segment_container(run_dir: Path, observer, *, seconds: float = 900.0):
    """Poll until the resumed segment's container is observable, in background."""
    import threading

    if observer is None:
        return None

    def _poll():
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if observe_segment_container(run_dir, observer):
                return
            time.sleep(1.0)

    thread = threading.Thread(target=_poll, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------
# the trees' own Harbor launch, factored once
# --------------------------------------------------------------------------

def segment_logs(run_dir: Path, index: int, log_paths=None) -> tuple[Path, Path]:
    """Segment 1 keeps the run's own pair of Builder logs, exclusively."""
    run_dir = Path(run_dir)
    if index == 1:
        if log_paths:
            return Path(log_paths[0]), Path(log_paths[1])
        return run_dir / "builder.stdout.log", run_dir / "builder.stderr.log"
    root = run_dir / SEGMENT_ROOT / f"seg{index:03d}"
    root.mkdir(parents=True, exist_ok=True)
    return root / "builder.stdout.log", root / "builder.stderr.log"


def launch_harbor_job(
    *,
    run_dir,
    harbor,
    config,
    timeout,
    observer=None,
    index: int = 1,
    env=None,
    on_start=None,
    write_json=None,
    log_mode: str = "x",
    log_paths=None,
):
    """One ``harbor run -c <config>``, with the trees' own outer-deadline wait.

    This is the launch loop that was inlined in every tree, moved here verbatim
    so that every segment is started exactly the way segment 1 always was:
    same process group, same 1 s poll, same resource-observer abort, same
    SIGTERM-then-SIGKILL ladder, same 124 / 125 exit codes.  Returns the exit
    code; ``on_start`` receives the child so the tree's own ``finally`` can
    still kill it.
    """
    run_dir = Path(run_dir)
    timeout = float(timeout)
    out_path, err_path = segment_logs(run_dir, index, log_paths)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = log_mode if index == 1 else "a"
    env = control_plane_env(env)
    with out_path.open(mode) as stdout, err_path.open(mode) as stderr:
        child = subprocess.Popen(
            [str(harbor), "run", "-c", str(config)],
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            env=env,
        )
        if on_start is not None:
            on_start(child)
        if write_json is not None and index == 1:
            write_json(
                run_dir / "owned_builder_process.json",
                {
                    "pid": child.pid,
                    "process_start": Path(f"/proc/{child.pid}/stat")
                    .read_text()
                    .split()[21],
                    "command_config": str(config),
                },
            )
        deadline = time.monotonic() + timeout
        resource_failure = False
        try:
            while True:
                if observer is not None and observer.proof["errors"]:
                    resource_failure = True
                    raise subprocess.TimeoutExpired(child.args, timeout)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(child.args, timeout)
                try:
                    code = child.wait(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            import signal

            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            if isinstance(exc, KeyboardInterrupt):
                code = 130
            else:
                code = 125 if resource_failure else 124
    return code


class BuilderSession:
    """The Builder's launch loop for one run, bound to that run's launcher."""

    def __init__(self, run_dir, config, *, harbor, observer=None,
                 write_json=None, on_start=None, env=None, log_mode="x",
                 log_paths=None):
        self.run_dir = Path(run_dir)
        self.config = Path(config)
        self.harbor = harbor
        self.observer = observer
        self.write_json = write_json
        self.on_start = on_start
        self.env = env
        self.log_mode = log_mode
        self.log_paths = log_paths
        self.index = 0
        self.child = None
        self.results = []

    def launch(self, segment_config, segment_timeout):
        self.index += 1

        def _adopt(process):
            self.child = process
            if self.on_start is not None:
                self.on_start(process)

        code = launch_harbor_job(
            run_dir=self.run_dir,
            harbor=self.harbor,
            config=segment_config,
            timeout=segment_timeout,
            observer=self.observer,
            index=self.index,
            env=self.env,
            on_start=_adopt,
            write_json=self.write_json,
            log_mode=self.log_mode,
            log_paths=self.log_paths,
        )
        self.results.append(code)
        return code

    def run(self, *, budget_seconds, frozen_probe=None,
            resume_cap: int = RESUME_CAP, margin: float = RESUME_MARGIN_SECONDS):
        return run_segments(
            run_dir=self.run_dir,
            config=self.config,
            launch=self.launch,
            budget_seconds=budget_seconds,
            observer=self.observer,
            frozen_probe=frozen_probe,
            resume_cap=resume_cap,
            margin=margin,
        )


def builder_session(run_dir, config, **kwargs):
    """Context-manager form, so a tree's call site keeps its indentation."""
    import contextlib

    @contextlib.contextmanager
    def _session():
        session = BuilderSession(run_dir, config, **kwargs)
        try:
            yield session
        finally:
            pass

    return _session()


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------

def ledger_document(segments: list[dict], *, resume_cap: int, deadline_epoch: float) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "resume_cap": int(resume_cap),
        "builder_deadline_epoch": float(deadline_epoch),
        "segments": segments,
    }


def ledger_trials(run_dir) -> list:
    """The ordered segment trial names this run's evaluator ledger declares.

    Empty for every run that never resumed, which is what the formal gates that
    count Harbor trials fall back to.  Shape only -- the admissibility gate is
    ``native_builder_evidence.segment_ledger``.
    """
    path = Path(run_dir) / LEDGER_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return []
    rows = value.get("segments")
    if not isinstance(rows, list) or not rows:
        return []
    trials = [row.get("trial") for row in rows if isinstance(row, dict)]
    if len(trials) != len(rows) or any(not isinstance(t, str) or not t for t in trials):
        return []
    return trials


def default_frozen_probe(run_dir):
    """True once the controller has frozen this run's Candidate.

    Read from the run directory rather than from a controller handle so that
    every call site, whatever its shape, applies the same rule: a frozen
    Candidate ends the Builder session, and no segment follows it.
    """
    run_dir = Path(run_dir)
    return (run_dir / "freeze_manifest.json").exists()


def write_ledger(run_dir: Path, document: dict) -> Path:
    path = Path(run_dir) / LEDGER_NAME
    part = path.with_suffix(".json.part")
    part.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(str(part), str(path))
    return path


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def run_segments(
    *,
    run_dir,
    config,
    launch,
    budget_seconds: float,
    observer=None,
    frozen_probe=None,
    resume_cap: int = RESUME_CAP,
    margin: float = RESUME_MARGIN_SECONDS,
) -> int:
    """Run the Builder to a terminal codex session and return its exit code.

    ``launch(config_path, timeout_seconds) -> int`` is the tree's own one-shot
    Harbor launcher, called unchanged for every segment.  It must already be
    inside the run's relay / auth / controller-socket contexts, so a resumed
    segment reuses all three without reopening anything.
    """
    run_dir = Path(run_dir)
    config = Path(config)
    started_epoch = time.time()
    deadline_epoch = started_epoch + float(budget_seconds)
    jobs_dir, job = job_identity(config)

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "resume_cap": int(resume_cap),
        "builder_deadline_epoch": deadline_epoch,
        "budget_seconds": float(budget_seconds),
        "attempts": [],
    }
    try:
        receipt["prepare"] = prepare_run(run_dir, config)
    except NativeRetryPolicyError as exc:             # package 105: refuse to run
        # The one prepare failure that must stop the Builder: launching without
        # the pinned policy is the failure this guard exists to prevent.
        receipt["prepare_error"] = f"{type(exc).__name__}: {exc}"
        try:
            (run_dir / "builder_segment_receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        raise
    except Exception as exc:                          # never fail the run here
        receipt["prepare_error"] = f"{type(exc).__name__}: {exc}"

    before = trial_names(jobs_dir, job)
    segment_config = config
    segments: list[dict] = []
    code = 125
    index = 0

    while True:
        index += 1
        segment_started = time.time()
        remaining = deadline_epoch - segment_started
        if index > 1:
            wait_for_segment_container(run_dir, observer)
        code = launch(segment_config, max(1.0, remaining))
        segment_ended = time.time()

        if index == 1:
            new = sorted(trial_names(jobs_dir, job) - before)
            trial = new[0] if len(new) == 1 else None
        else:
            trial = promote_trial(
                Path(json.loads(segment_config.read_text())["jobs_dir"]),
                jobs_dir,
                job,
                trial_names(jobs_dir, job),
            )

        shape = {"exists": False, "thread_ids": [], "turn_started": 0,
                 "turn_completed": 0, "turn_failed": 0, "thread_failed": 0}
        if trial:
            shape = stream_shape(jobs_dir / job / trial / "agent/codex.txt")
        session_id, rollout = session_identity(run_dir)

        row = {
            "segment_index": index,
            "trial": trial,
            "session_id": session_id,
            "started_at_epoch": segment_started,
            "ended_at_epoch": segment_ended,
            "exit_reason": _exit_reason(code, shape),
            "exit_code": code,
            "rollout_sha256": sha256_file(rollout) if rollout else None,
            "harbor_exception": trial_exception(jobs_dir, job, trial) if trial else None,
        }
        if index > 1:
            row["resume_of_session_id"] = segments[0].get("session_id")
        if trial:
            segments.append(row)
        receipt["attempts"].append({**row, "promoted": bool(trial)})

        if not trial:
            # The attempt produced no stream: leave the run exactly as the last
            # promoted segment left it, including its exit code, and stop.
            # A failed resume is never a new way to fail.
            if segments:
                code = segments[-1]["exit_code"]
            break

        try:
            frozen = bool(frozen_probe()) if frozen_probe else default_frozen_probe(run_dir)
        except Exception:
            frozen = True
        decision = resume_decision(
            code=code,
            shape=shape,
            session_id=session_id,
            segments_done=len(segments),
            resume_cap=resume_cap,
            now_epoch=time.time(),
            deadline_epoch=deadline_epoch,
            frozen=frozen,
            margin=margin,
        )
        receipt["attempts"][-1]["decision"] = decision
        if not decision["resume"]:
            break
        try:
            segment_config, _ = derive_config(run_dir, config, index + 1, session_id)
        except Exception as exc:
            receipt["attempts"][-1]["derive_error"] = f"{type(exc).__name__}: {exc}"
            break

    receipt["segment_count"] = len(segments)
    try:
        if len(segments) > 1:
            write_ledger(
                run_dir,
                ledger_document(
                    [_ledger_row(row) for row in segments],
                    resume_cap=resume_cap,
                    deadline_epoch=deadline_epoch,
                ),
            )
            receipt["ledger"] = LEDGER_NAME
        (run_dir / "builder_segment_receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    return code


def _ledger_row(row: dict) -> dict:
    keys = (
        "segment_index",
        "trial",
        "session_id",
        "resume_of_session_id",
        "started_at_epoch",
        "ended_at_epoch",
        "exit_reason",
        "exit_code",
        "rollout_sha256",
    )
    return {key: row[key] for key in keys if key in row}


def _exit_reason(code: int, shape: dict) -> str:
    if code == 0 and shape.get("turn_completed"):
        return "completed"
    if shape.get("thread_failed"):
        return "thread_failed"
    if shape.get("turn_failed"):
        return "turn_failed"
    if shape.get("turn_started") and not shape.get("turn_completed"):
        return "infrastructure_cut"
    if code == 124:
        return "outer_timeout"
    if code == 125:
        return "resource_or_gate_failure"
    return f"exit_{code}"


def run_builder_segments(run_dir, config, *, harbor, budget_seconds,
                         observer=None, write_json=None, on_start=None, env=None,
                         log_mode="x", log_paths=None, frozen_probe=None,
                         resume_cap: int = RESUME_CAP,
                         margin: float = RESUME_MARGIN_SECONDS) -> int:
    """Function form of :class:`BuilderSession` for call sites without a block."""
    session = BuilderSession(
        run_dir, config, harbor=harbor, observer=observer, write_json=write_json,
        on_start=on_start, env=env, log_mode=log_mode, log_paths=log_paths,
    )
    return session.run(budget_seconds=budget_seconds, frozen_probe=frozen_probe,
                       resume_cap=resume_cap, margin=margin)
