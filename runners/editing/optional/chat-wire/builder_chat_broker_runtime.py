"""Per-run, evaluator-owned Builder broker: codex Responses in, DeepInfra Chat out.

Installed by package 115 (`switch_builder_deepinfra.py`) next to the rest of the
shared control plane.  It is the Builder-side counterpart of
``builder_broker_runtime.py`` (which starts the Responses-to-Responses broker for
the three trees that have a ``--builder-transport broker`` branch): this one is
started from ``direct_harbor_builder.direct_auth`` and therefore covers all ten
trees, including the seven that only have the ``direct`` path.

One broker per Edit run.  It is started before the Harbor Builder job, stopped
after it, and its ``/stats`` and request ledger are snapshotted into the run
directory as ``builder_broker_stats.json`` / ``builder_broker_ledger/`` so that
usage and ``estimated_cost_usd`` are attributed to that run and readable by the
readiness/formal evidence layer.

The provider credential is opened only inside the broker container.  The Builder
container receives the fixed ``broker-only-placeholder`` token.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# switch-owned parameters (rewritten by switch_builder_deepinfra.py)
# --------------------------------------------------------------------------
MODEL = 'deepseek-ai/DeepSeek-V4-Flash-0731'
EFFORT = 'max'                # None means: send no reasoning_effort upstream
EFFORT_LABEL = 'max'          # what the attestations call this condition
CREDENTIAL_ENV = 'DEEPINFRA_API_KEY'
UPSTREAM_BASE_URL = 'https://api.deepinfra.com/v1/openai'
UPSTREAM_WIRE = "chat"
PROVIDER_ID = 'deepinfra_chat_broker'
BROKER_BASE_PORT = 18200
BROKER_PORT_SPAN = 200
RATE_LIMIT_POLICY = 'as_503'
REPLAY_POLICY = 'resend'
TRANSPORT = 'native_codex_via_evaluator_chat_broker'
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
FILES = ("builder_broker_chat_upstream.py", "chat_translate.py",
         "builder_request_ledger.py", "responses_stream.py")
BROKER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
BROKER_HOST_ADDRESS = "172.17.0.1"     # docker0; what the Builder container dials
PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
HEALTH_TIMEOUT_SECONDS = 300.0  # 116b: 124 load >100 makes broker startup exceed 60 s
STOP_TIMEOUT_SECONDS = 30.0
LABEL_OWNER = "edit-builder-chat-broker-v1"


class BuilderBrokerError(RuntimeError):
    """The evaluator-owned Builder broker could not be established or attested."""


def upstream_chat_url() -> str:
    return UPSTREAM_BASE_URL.rstrip("/") + "/chat/completions"


def require_credential(credential_path) -> str:
    """Prove the credential file carries CREDENTIAL_ENV without reading its value.

    ``direct_auth`` used to read the provider key and hand it to Harbor.  It must
    not any more: only the broker container opens the file.  This returns the
    variable name, never the value.
    """
    path = Path(credential_path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == CREDENTIAL_ENV and value.strip().strip("'\""):
            return CREDENTIAL_ENV
    raise BuilderBrokerError(
        "%s is missing from %s; the evaluator Builder broker cannot start"
        % (CREDENTIAL_ENV, path))


def port_for(anchor) -> int:
    """A deterministic per-run port, so provider TOML and broker always agree.

    ``anchor`` is the run-owned file whose directory identifies the run (the
    provider TOML when writing it, the Harbor job config when starting the
    broker); both live directly in the run directory, so both derive the same
    port without having to pass one through ten trees' call chains.
    """
    run = str(Path(anchor).resolve().parent)
    digest = hashlib.sha256(run.encode("utf-8")).digest()
    return BROKER_BASE_PORT + int.from_bytes(digest[:4], "big") % BROKER_PORT_SPAN


def builder_base_url(anchor) -> str:
    """The base_url the Builder's provider TOML must carry for this run."""
    return "http://%s:%d/v1" % (BROKER_HOST_ADDRESS, port_for(anchor))


def local_endpoint(port: int) -> str:
    return "http://127.0.0.1:%d" % port


def provider_port(provider_toml) -> int:
    """Read back the port this run's provider TOML was written with."""
    text = Path(provider_toml).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("base_url"):
            value = line.split("=", 1)[1].strip().strip('"')
            return int(value.rsplit(":", 1)[1].split("/", 1)[0])
    raise BuilderBrokerError("no base_url in Builder provider config: %s" % provider_toml)


def provider_config_path(job_config) -> Path:
    value = json.loads(Path(job_config).read_text(encoding="utf-8"))
    for agent in value.get("agents", []):
        path = (agent.get("env") or {}).get("CODEX_CONFIG_TOML_PATH")
        if path:
            return Path(path)
    raise BuilderBrokerError("the Builder job config carries no CODEX_CONFIG_TOML_PATH")


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _health(port: int) -> dict:
    with urllib.request.urlopen(local_endpoint(port) + "/healthz", timeout=2) as response:
        return json.load(response)


def _stats(port: int) -> dict:
    request = urllib.request.Request(
        local_endpoint(port) + "/stats",
        headers={"Authorization": "Bearer %s" % STATS_TOKEN})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def start_broker(run_dir, *, credential, port, name=None) -> dict:
    """Start the run's broker container and return its lifecycle receipt."""
    run_dir = Path(run_dir).resolve()
    credential = Path(credential).resolve()
    evidence = run_dir / "builder_broker_transport"
    cidfile = run_dir / "builder_chat_broker.cid"
    if cidfile.exists() or cidfile.is_symlink():
        raise BuilderBrokerError("existing Builder broker CID evidence cannot be replaced")
    evidence.mkdir(exist_ok=False)
    name = name or ("edit-builder-chat-broker-"
                    + hashlib.sha256(str(run_dir).encode()).hexdigest()[:16])
    missing = [f for f in FILES if not (ROOT / f).is_file()]
    if missing:
        raise BuilderBrokerError("package 114 broker files are not installed: %s" % missing)
    if not _free(port):
        raise BuilderBrokerError(
            "Builder broker port %d is already in use; refusing to attach to a "
            "process this run does not own" % port)
    refs = {str(ROOT / f): hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FILES}
    (evidence / "source_bindings.json").write_text(json.dumps(refs, indent=2) + "\n")
    mounts = [item for f in FILES for item in ("-v", "%s:/opt/builder_broker/%s:ro" % (ROOT / f, f))]
    effort_args = ["--effort", EFFORT] if EFFORT else []
    command = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "--label", "agentswe.owner=" + LABEL_OWNER,
        "--label", "agentswe.run_path_sha256=" + hashlib.sha256(str(run_dir).encode()).hexdigest(),
        "--cidfile", str(cidfile),
        "-v", "%s:/run/secrets/agentswe.env:ro" % credential,
        "-v", "%s:/evidence" % evidence, *mounts,
        "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
        "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        BROKER_IMAGE,
        "python3", "/opt/builder_broker/builder_broker_chat_upstream.py",
        "--credential-file", "/run/secrets/agentswe.env",
        "--credential-var", CREDENTIAL_ENV,
        "--provider-url", upstream_chat_url(),
        "--model", MODEL, *effort_args,
        "--rate-limit-policy", RATE_LIMIT_POLICY,
        "--replay-policy", REPLAY_POLICY,
        "--stats-file", "/evidence/broker_stats.json",
        "--bind", "0.0.0.0", "--port", str(port),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    if not cidfile.is_file():
        value = result.stdout.strip()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise BuilderBrokerError("Builder broker startup did not return a valid owned CID")
        cidfile.write_text(value + "\n")
    cid = cidfile.read_text().strip()
    info = json.loads(subprocess.check_output(["docker", "inspect", cid]))[0]
    if not any(m.get("Source") == str(evidence) and m.get("Destination") == "/evidence"
               for m in info["Mounts"]):
        raise BuilderBrokerError("Builder broker evidence mount differs from requested run")
    (evidence / "container_identity.json").write_text(json.dumps(
        {"id": info["Id"], "image": info["Image"], "labels": info["Config"]["Labels"],
         "mounts": info["Mounts"], "command": info["Config"]["Cmd"]}, indent=2) + "\n")

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    health = None
    while time.monotonic() < deadline:
        try:
            health = _health(port)
        except (OSError, ValueError):
            health = None
        else:
            # EFFORT is None for a model that is run without reasoning_effort at
            # all (Qwen); the health gate must accept that, not only a string.
            if (health.get("model") == MODEL and health.get("reasoning_effort") == EFFORT
                    and health.get("upstream_wire") == UPSTREAM_WIRE
                    and health.get("protocol") == "agentswe-builder-single-upstream/v1"):
                break
            health = None
        time.sleep(0.25)
    if health is None:
        raise BuilderBrokerError(
            "Builder broker did not become healthy on port %d; preserve CID and evidence" % port)

    receipt = {
        "schema_version": "agentswe-edit-builder-chat-broker/v1",
        "owner": "evaluator", "role": "builder", "scope": "per_run",
        "container_name": name, "container_id": cid, "image": BROKER_IMAGE,
        "port": port, "endpoint_local": local_endpoint(port) + "/v1/responses",
        "builder_base_url": "http://%s:%d/v1" % (BROKER_HOST_ADDRESS, port),
        "broker_endpoint_for_builder": "http://%s:%d/v1/responses" % (BROKER_HOST_ADDRESS, port),
        "native_model": MODEL, "native_effort": EFFORT_LABEL,
        "reasoning_effort_sent_upstream": EFFORT,
        "upstream_base_url": UPSTREAM_BASE_URL, "upstream_wire": UPSTREAM_WIRE,
        "provider_id": PROVIDER_ID, "credential_env": CREDENTIAL_ENV,
        "credential_visible_to_builder": PLACEHOLDER,
        "rate_limit_policy": RATE_LIMIT_POLICY, "replay_policy": REPLAY_POLICY,
        "evidence_dir": str(evidence), "cidfile": str(cidfile),
        "source_bindings": refs, "health": health,
    }
    (run_dir / "builder_broker_lifecycle.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def stop_broker(receipt) -> dict:
    """Snapshot usage and the ledger into the run, then stop the container."""
    run_dir = Path(receipt["evidence_dir"]).parent
    evidence = Path(receipt["evidence_dir"])
    cleanup = {"schema_version": "agentswe-edit-builder-chat-broker-cleanup/v1",
               "container_name": receipt["container_name"],
               "container_id": receipt["container_id"], "port": receipt["port"]}
    try:
        stats = _stats(receipt["port"])
    except (OSError, ValueError) as exc:
        stats = None
        cleanup["stats_error"] = type(exc).__name__
    if stats is None and (evidence / "broker_stats.json").is_file():
        # The process may already be gone; its own persisted file is the receipt.
        stats = json.loads((evidence / "broker_stats.json").read_text(encoding="utf-8"))
        cleanup["stats_source"] = "persisted_file"
    if stats is not None:
        (run_dir / "builder_broker_stats.json").write_text(
            json.dumps(stats, indent=2, sort_keys=True) + "\n")
        runtime = stats.get("runtime") or {}
        cleanup["calls"] = runtime.get("calls")
        cleanup["successful_calls"] = runtime.get("successful_calls")
        cleanup["estimated_cost_usd"] = runtime.get("estimated_cost_usd")
        cleanup["usage_complete"] = runtime.get("usage_complete")
    ledger = evidence / "builder_requests"
    target = run_dir / "builder_broker_ledger"
    if ledger.is_dir() and not target.exists():
        shutil.copytree(ledger, target, ignore=shutil.ignore_patterns(".process.lock"))
        cleanup["ledger_entries"] = len([p for p in target.iterdir() if p.is_dir()])
    stopped = subprocess.run(["docker", "stop", "-t", "5", receipt["container_id"]],
                             capture_output=True, text=True, check=False)
    cleanup["stop_returncode"] = stopped.returncode
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    alive = "unknown"
    while time.monotonic() < deadline:
        probe = subprocess.run(["docker", "ps", "-q", "--no-trunc", "--filter",
                                "id=" + receipt["container_id"]],
                               capture_output=True, text=True, check=False)
        alive = probe.stdout.strip()
        if not alive:
            break
        time.sleep(0.5)
    cleanup["container_removed"] = alive == ""
    with contextlib.suppress(OSError):
        (run_dir / "builder_broker_cleanup.json").write_text(
            json.dumps(cleanup, indent=2, sort_keys=True) + "\n")
    return cleanup


@contextlib.contextmanager
def run_broker(job_config, *, credential, run_dir=None):
    """Start this run's broker for the lifetime of the Builder job.

    ``job_config`` is the Harbor job config the evaluator wrote into the run
    directory; its ``CODEX_CONFIG_TOML_PATH`` names the provider TOML whose
    ``base_url`` already fixes this run's port.
    """
    job_config = Path(job_config).resolve()
    run_dir = Path(run_dir).resolve() if run_dir else job_config.parent
    provider = provider_config_path(job_config)
    port = provider_port(provider)
    if port != port_for(provider):
        # The TOML was written by a caller that passed an explicit base_url; the
        # broker follows the TOML, never the other way round.
        pass
    receipt = start_broker(run_dir, credential=credential, port=port)
    try:
        yield {k: receipt[k] for k in (
            "broker_endpoint_for_builder", "builder_base_url", "native_model",
            "native_effort", "reasoning_effort_sent_upstream", "upstream_base_url",
            "upstream_wire", "provider_id", "credential_env",
            "credential_visible_to_builder", "rate_limit_policy", "replay_policy",
            "container_name", "container_id", "port")}
    finally:
        stop_broker(receipt)


def stats_path(run_dir) -> Path:
    return Path(run_dir) / "builder_broker_stats.json"
