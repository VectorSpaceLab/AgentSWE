"""Creation runner: one run = (task x builder x seed), driven by the task's Harbor one_stop controller.

Per run:
  * three provider brokers (broker/agentswe_broker, one per role: builder, runtime, judge); they are
    the only processes holding provider keys, each read from its own 0600 file,
  * the task's evaluator broker (runners/creation/broker_v2.py: budgets, /events, failure classes),
    whose upstream is the runtime provider broker,
  * Codex config + model catalog from builders/codex/gen_codex_config.py (both multi-agent
    switches off), pointing Codex at the builder broker with a placeholder token,
  * the Result judge pointed at the judge broker with a placeholder token,
  * one_stop.py in its own process group; when it exits, the wrapper removes every broker (by
    container id) and deletes the key files. No real key enters one_stop or any job container.
Formal runs use the paper protocol from task.json; --smoke uses the task's smoke budget and is
marked as not comparable.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import util
from ..config import REPO_ROOT, Config
from ..doctor import docker_env, docker_ip
from ..profiles import staged
from ..registry import Task

FAMILY_DIR = "creation"


def _paths(cfg: Config, run_id: str) -> dict[str, Path]:
    root = cfg.home / "runs" / FAMILY_DIR
    return {"root": root, "run_dir": root / run_id, "launch": root / f"{run_id}.launch.json",
            "log": root / f"{run_id}.one_stop.log", "codex": root / f"{run_id}.codex",
            "result": root / f"{run_id}.result.json", "secrets": cfg.home / "secrets" / run_id,
            "ledger": cfg.home / "broker_ledgers" / run_id}


def active_runs(cfg: Config) -> list[dict]:
    rows = []
    for mf in (cfg.home / "runs").glob("*/*.launch.json"):
        m = util.read_json(mf, {}) or {}
        if m.get("pid") and util.pid_alive(int(m["pid"])) and m.get("host") == socket.gethostname():
            rows.append(m)
    return rows


def free_port(host: str) -> int:
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def judge_format(cfg: Config) -> str:
    """release (default): Result-judge outputs are bound to the request where the paper protocol required an echo;
    exact: the paper's rules. Recorded per case in the eval manifest (archived manifests replay as exact)."""
    mode = cfg.get("AGENTSWE_JUDGE_FORMAT") or "release"
    if mode not in ("release", "exact"):
        raise SystemExit("AGENTSWE_JUDGE_FORMAT must be release or exact")
    return mode


def generate_codex_config(profile: dict, builder, base_url: str, out_dir: Path) -> None:
    """Codex config + catalog via the generator; refuses on any multi-agent or tool-surface gap."""
    gen = REPO_ROOT / profile["generator"]
    cmd = [util.python(), str(gen), "--model", builder.model, "--effort", builder.effort, "--provider-type", "broker",
           "--base-url", base_url, "--catalog-path", "/tmp/codex-home/models.json",
           "--env-key", "AGENTSWE_BUILDER_API_KEY", "--out-dir", str(out_dir)]
    r = util.run(cmd, check=False)
    if r.returncode != 0:
        raise SystemExit("Codex config generation failed:\n" + r.stdout[-1500:])
    r = util.run([util.python(), str(gen), "--verify", str(out_dir)], check=False)
    if r.returncode != 0:
        raise SystemExit("Codex config verification failed:\n" + r.stdout[-1500:])


def start_role_broker(cfg: Config, run_id: str, role: str, bind: str, port: int, key_file: Path, ledger: Path,
                      image: str, family: str | None = "CREATION") -> str:
    """One provider broker per role (the only holder of that role's key)."""
    r = cfg.role(role.upper(), family)
    name = f"agentswe-os-{role}-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    ledger.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "-d", "--name", name, "--network", "host",
           "--label", f"agentswe_os_run={run_id}", "--label", f"agentswe_os_home={cfg.home}",
           "-v", f"{REPO_ROOT / 'broker' / 'agentswe_broker'}:/opt/agentswe/broker/agentswe_broker:ro",
           "-w", "/opt/agentswe/broker", "-v", f"{key_file}:/run/secrets/provider.key:ro", "-v", f"{ledger}:/ledger",
           "-e", "PYTHONDONTWRITEBYTECODE=1",
           image, "python3", "-m", "agentswe_broker", "serve", "--role", role,
           "--credential-file", "/run/secrets/provider.key", "--credential-var", f"AGENTSWE_{role.upper()}_API_KEY",
           "--wire", r.wire, "--base-url", r.base_url, "--model", r.model, "--effort", r.effort or "none",
           "--stats-file", f"/ledger/{role}_stats.json", "--ledger-dir", "/ledger", "--bind", bind, "--port", str(port)]
    if role == "builder":
        cmd += ["--replay-policy", "resend", "--rate-limit-policy", "as_503"]
    else:
        cmd += ["--normalize-reported-model"]
    out = util.run(cmd, env=docker_env(cfg), check=False)
    if out.returncode != 0:
        raise SystemExit(f"{role} broker start failed: " + out.stdout[-800:])
    cid = out.stdout.strip().splitlines()[-1]
    wait_healthy(cid, f"http://{bind}:{port}/healthz")
    return cid


SEARCH_HOST = "search.example.com"
SEARCH_CLIENT_TOKEN = "search-placeholder"


def make_search_certificates(out: Path) -> dict[str, str]:
    """Per-run CA and a leaf for search.example.com. The CA key is deleted once the leaf is signed."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_key, ca_crt = out / "ca.key", out / "ca.pem"
    key, csr, crt = out / "key.pem", out / "leaf.csr", out / "cert.pem"
    ext = out / "leaf.ext"
    ext.write_text(f"subjectAltName=DNS:{SEARCH_HOST}\nbasicConstraints=critical,CA:FALSE\n"
                   "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    run = lambda *a: util.run(["openssl", *a])  # noqa: E731
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key), "-out", str(ca_crt), "-days", "3",
        "-subj", "/CN=AgentSWE per-run search CA", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={SEARCH_HOST}")
    run("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(crt), "-days", "3", "-extfile", str(ext))
    for leftover in (ca_key, csr, ext, out / "ca.srl"):
        leftover.unlink(missing_ok=True)
    system = Path("/etc/ssl/certs/ca-certificates.crt")
    bundle = out / "ca-bundle.pem"
    bundle.write_text((system.read_text() if system.is_file() else "") + "\n" + ca_crt.read_text())
    for f in out.iterdir():
        f.chmod(0o600 if f.name == "key.pem" else 0o644)
    subject_hash = util.run(["openssl", "x509", "-noout", "-subject_hash", "-in", str(ca_crt)]).stdout.strip()
    if len(subject_hash) != 8 or any(c not in "0123456789abcdef" for c in subject_hash):
        raise SystemExit(f"unexpected CA subject hash {subject_hash!r}")
    return {"ca": str(ca_crt), "cert": str(crt), "key": str(key), "ca_bundle": str(bundle),
            "ca_sha256": util.sha256_file(ca_crt), "cert_sha256": util.sha256_file(crt),
            "ca_bundle_sha256": util.sha256_file(bundle), "ca_subject_hash": subject_hash}


def candidate_protocol():
    """runners/creation/candidate_broker_protocol.py (the module the Creation adapters use), without bytecode."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("agentswe_candidate_broker_protocol",
                                                  REPO_ROOT / "runners" / "creation" / "candidate_broker_protocol.py")
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def start_search_broker(cfg: Config, run_id: str, bind: str, port: int, key_file: Path, ledger: Path,
                        image: str) -> str:
    """The run's search broker: the only holder of the search key; Candidates authenticate with the placeholder."""
    base, _ = cfg.search
    name = "agentswe-os-search-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    ledger.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "-d", "--name", name, "--network", "host",
           "--label", f"agentswe_os_run={run_id}", "--label", f"agentswe_os_home={cfg.home}",
           "-v", f"{REPO_ROOT / 'broker' / 'agentswe_broker'}:/opt/agentswe/broker/agentswe_broker:ro",
           "-w", "/opt/agentswe/broker", "-v", f"{key_file}:/run/secrets/provider.key:ro", "-v", f"{ledger}:/ledger",
           "-e", "PYTHONDONTWRITEBYTECODE=1",
           image, "python3", "-m", "agentswe_broker", "serve", "--role", "search",
           "--credential-file", "/run/secrets/provider.key", "--credential-var", "AGENTSWE_SEARCH_API_KEY",
           "--wire", cfg.get("AGENTSWE_SEARCH_WIRE") or "serper", "--base-url", base,
           "--client-token", SEARCH_CLIENT_TOKEN,
           "--stats-file", "/ledger/search_stats.json", "--ledger-dir", "/ledger", "--bind", bind, "--port", str(port)]
    out = util.run(cmd, env=docker_env(cfg), check=False)
    if out.returncode != 0:
        raise SystemExit("search broker start failed: " + out.stdout[-800:])
    cid = out.stdout.strip().splitlines()[-1]
    wait_healthy(cid, f"http://{bind}:{port}/healthz")
    return cid


def wait_healthy(cid: str, url: str, seconds: float = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(2)
    logs = util.out(["docker", "logs", "--tail", "40", cid])
    util.run(["docker", "rm", "-f", cid], check=False)
    raise SystemExit(f"broker {cid[:12]} not healthy at {url}:\n" + logs[-1500:])


def start_broker(cfg: Config, run_id: str, host: str, port: int, runtime_env: Path, ledger: Path, image: str,
                 upstream: str) -> tuple[str, str]:
    """The task's evaluator broker (budgets, per-case events); its upstream is the runtime provider broker."""
    name = "agentswe-os-broker-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    ledger.mkdir(parents=True, exist_ok=True)
    rt = cfg.role("RUNTIME", "CREATION")
    shared = REPO_ROOT / "runners" / "creation"
    cmd = ["docker", "run", "-d", "--name", name, "--network", "host",
           "--label", f"agentswe_os_run={run_id}", "--label", f"agentswe_os_home={cfg.home}",
           "-v", f"{shared / 'broker_v2.py'}:/broker.py:ro", "-v", f"{shared / 'responses_broker.py'}:/responses_broker.py:ro",
           "-v", f"{ledger}:/telemetry", "-v", f"{runtime_env}:/run/secrets/agentswe.env:ro",
           "-e", f"AGENTSWE_RUNTIME_RESPONSES_URL={upstream}", "-e", f"AGENTSWE_RUNTIME_MODEL={rt.model}",
           "-e", f"AGENTSWE_RUNTIME_EFFORT={rt.effort}",
           "-e", f"AGENTSWE_BROKER_JUDGE_EFFORT={cfg.role('JUDGE', 'CREATION').effort}",
           "-e", f"AGENTSWE_RUNTIME_MODEL_ALIASES={cfg.get('AGENTSWE_RUNTIME_MODEL_ALIASES') or rt.model.split('/')[-1]}",
           image, "python3", "/broker.py", "--credential-file", "/run/secrets/agentswe.env", "--bind", host,
           "--port", str(port), "--ledger-file", "/telemetry/calls.jsonl", "--max-runtime-calls", "0",
           "--max-runtime-tokens", "0", "--max-attempts", "4", "--request-timeout", "560", "--upstream-timeout", "300"]
    r = util.run(cmd, env=docker_env(cfg), check=False)
    if r.returncode != 0:
        raise SystemExit("broker start failed: " + r.stdout[-800:])
    cid = r.stdout.strip().splitlines()[-1]
    wait_healthy(cid, f"http://{host}:{port}/healthz")
    return name, cid


def run(cfg: Config, task: Task, *, builder: str, seed: int, smoke: bool, label: str | None) -> dict:
    state = util.read_json(cfg.home / "state" / "setup.json", {}) or {}
    if task.id not in state.get("tasks", {}):
        raise SystemExit(f"run `agentswe setup {task.id}` first")
    limit = int(cfg.get("AGENTSWE_MAX_CONCURRENT_RUNS"))
    active = active_runs(cfg)
    if len(active) >= limit:
        raise SystemExit(f"{len(active)} runs already active on this host (limit AGENTSWE_MAX_CONCURRENT_RUNS={limit})")
    profiles = json.loads((REPO_ROOT / "builders" / "codex" / "profiles.json").read_text())["profiles"]
    if builder not in profiles:
        raise SystemExit(f"unknown builder profile {builder!r}; known: {sorted(profiles)}")
    profile = profiles[builder]
    roles = {name: cfg.role(name, "CREATION") for name in ("BUILDER", "RUNTIME", "JUDGE")}
    for name, r in roles.items():
        if not (r.base_url and r.api_key and r.model):
            raise SystemExit(f"role {name} is not fully configured; run `agentswe doctor`")
    b = roles["BUILDER"]

    protocol = dict(task.data["protocol"])
    if smoke:
        protocol.update(task.data["smoke"])
        # Smoke only: a shorter Builder session (e.g. a smoke that must end at the session limit);
        # recorded in the launch manifest's protocol. Formal runs always use task.json.
        override = cfg.get("AGENTSWE_SMOKE_BUILDER_SESSION_SEC")
        if override:
            if not override.isdigit() or int(override) < 600:
                raise SystemExit("AGENTSWE_SMOKE_BUILDER_SESSION_SEC must be an integer number of seconds >= 600")
            protocol["builder_session_sec"] = int(override)
    mode = "smoke" if smoke else "formal"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"c-{task.short}-{builder}-{b.model.split('/')[-1]}-s{seed}-{mode}-{stamp}".lower()
    if label:
        run_id = f"{label}-{run_id}"
    p = _paths(cfg, run_id)
    p["root"].mkdir(parents=True, exist_ok=True)
    if p["run_dir"].exists() or p["launch"].exists():
        raise SystemExit(f"run id {run_id} already exists")
    for d in ("tmp", "network_allocations", "jobs"):
        (cfg.home / d).mkdir(parents=True, exist_ok=True)

    images = json.loads((REPO_ROOT / "images" / "images.json").read_text())["images"]
    builder_image = images["builder-codex"]["tag"]
    broker_host = cfg.get("AGENTSWE_BROKER_HOST") or docker_ip()
    if not broker_host:
        raise SystemExit("cannot determine the broker host address (set AGENTSWE_BROKER_HOST)")
    secrets = p["secrets"]
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Real keys: one 0600 file per provider broker. Everything else gets placeholders.
    for role in ("builder", "runtime", "judge"):
        util.write_secret(secrets / f"{role}.key", {f"AGENTSWE_{role.upper()}_API_KEY": roles[role.upper()].api_key})
    util.write_secret(secrets / "evaluator-broker.env", {"AGENTSWE_RUNTIME_API_KEY": "runtime-only-placeholder"})
    util.write_secret(secrets / "judge.env", {"AGENTSWE_JUDGE_API_KEY": "judge-only-placeholder"})
    util.write_secret(secrets / "candidate.env", {"GATEWAY_API_KEY": "broker-only-placeholder",
                                                  "SERPER_TOKEN": "search-placeholder"})
    containers: list[str] = []
    try:
        builder_port, judge_port = free_port(broker_host), free_port(broker_host)
        runtime_port = free_port("127.0.0.1")
        containers.append(start_role_broker(cfg, run_id, "builder", broker_host, builder_port, secrets / "builder.key",
                                            p["ledger"] / "builder", builder_image))
        containers.append(start_role_broker(cfg, run_id, "runtime", "127.0.0.1", runtime_port, secrets / "runtime.key",
                                            p["ledger"] / "runtime", builder_image))
        containers.append(start_role_broker(cfg, run_id, "judge", broker_host, judge_port, secrets / "judge.key",
                                            p["ledger"] / "judge", builder_image))
        generate_codex_config(profile, b, f"http://{broker_host}:{builder_port}/v1", p["codex"])
        port = free_port(broker_host)
        broker_name, broker_cid = start_broker(cfg, run_id, broker_host, port, secrets / "evaluator-broker.env",
                                               p["ledger"] / "evaluator", builder_image,
                                               upstream=f"http://127.0.0.1:{runtime_port}/v1/responses")
        containers.append(broker_cid)
        search_front = None
        if "search" in task.data.get("services", []):
            search_base, search_key = cfg.search
            if not search_base or not search_key:
                raise SystemExit("this task needs a search provider: set AGENTSWE_SEARCH_BASE_URL and AGENTSWE_SEARCH_API_KEY")
            util.write_secret(secrets / "search.key", {"AGENTSWE_SEARCH_API_KEY": search_key})
            search_port = free_port(broker_host)
            containers.append(start_search_broker(cfg, run_id, broker_host, search_port, secrets / "search.key",
                                                  p["ledger"] / "search", builder_image))
            certs = make_search_certificates(secrets / "search-front")
            trust_record = p["root"] / f"{run_id}.search_trust.jsonl"
            search_front = {"image": builder_image, "broker_host": broker_host, "broker_port": search_port,
                            "script": str(REPO_ROOT / "runners" / "creation" / "search_front.py"),
                            "cert": certs["cert"], "key": certs["key"], "ca_bundle": certs["ca_bundle"],
                            "ca": certs["ca"], "ca_subject_hash": certs["ca_subject_hash"],
                            "trust_record": str(trust_record)}
            search_record = {"host": SEARCH_HOST, "broker_port": search_port, "ca_sha256": certs["ca_sha256"],
                             "cert_sha256": certs["cert_sha256"], "ca_private_key": "deleted after signing",
                             "trust": "Candidate main service only: per-run CA appended to the system bundle, "
                                      "mounted read-only and named by SSL_CERT_FILE, REQUESTS_CA_BUNDLE, "
                                      "CURL_CA_BUNDLE and NODE_EXTRA_CA_CERTS, and bind-mounted read-only over "
                                      "the default trust stores (trust_overrides)",
                             "trust_overrides": {
                                 "bundle_sha256": certs["ca_bundle_sha256"],
                                 "system_targets": candidate_protocol().system_trust_targets(certs["ca_subject_hash"]),
                                 "system_targets_note": "the bundle over the system bundle; the run CA alone as "
                                                        "the OpenSSL capath entry <subject_hash>.0",
                                 "env_prefix_targets": "every existing certifi/cacert.pem and ssl/cert.pem / "
                                                       "ssl/cacert.pem (symlinks resolved inside the prefix) of each "
                                                       "mounted Candidate env prefix, under each of its aliases; "
                                                       "discovered when each Candidate compose is staged",
                                 "per_compose_record": str(trust_record),
                                 "mode": "read-only file bind mounts; nothing is written into any environment"},
                             "candidate_token": SEARCH_CLIENT_TOKEN}
    except BaseException:
        for cid in containers:
            util.run(["docker", "rm", "-f", cid], check=False)
        for role in ("builder", "runtime", "judge", "search"):
            (secrets / f"{role}.key").unlink(missing_ok=True)
        raise
    judge_base = f"http://{broker_host}:{judge_port}/v1"

    rc = task.data["runner_config"]
    env_prefix = cfg.home / "envs" / task.data["envs"][0]
    j = roles["JUDGE"]
    search_base, _ = cfg.search
    harbor_root = cfg.home / "harbor"
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(("_API_KEY", "_TOKEN")) and k.lower() not in
           {"http_proxy", "https_proxy", "all_proxy"}}
    env.update({
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "AGENTSWE_HOME": str(cfg.home), "AGENTSWE_HARBOR_ROOT": str(harbor_root), "HARBOR_ROOT": str(harbor_root),
        "HARBOR_TELEMETRY": "off", "DOCKER_CONFIG": str(harbor_root / "docker-config"),
        "PATH": str(harbor_root / "bin") + os.pathsep + env.get("PATH", ""),
        "AGENTSWE_CREATION_SHARED": str(REPO_ROOT / "runners" / "creation"),
        "PYTHONPATH": str(REPO_ROOT / "builders" / "codex"),
        "AGENTSWE_CANDIDATE_CREDENTIAL_FILE": str(secrets / "candidate.env"),
        "AGENTSWE_TRUSTED_BROWSER_BUNDLE": str(cfg.home / "deps" / "trusted-browser-v1"),
        "AGENTSWE_PPTX_RENDERER_LIBS": str(cfg.home / "deps" / "runtime-dependencies-v1" / "pptx" / "lib"),
        "AGENTSWE_CANDIDATE_BROKER_PORT": str(port), "AGENTSWE_BROKER_HOST": broker_host,
        "AGENTSWE_CREATE_NETWORK_POOL": cfg.get("AGENTSWE_NETWORK_POOL"),
        "AGENTSWE_CREATE_NETWORK_STATE": str(cfg.home / "network_allocations"),
        "TMPDIR": str(cfg.home / "tmp"), "TEMP": str(cfg.home / "tmp"), "TMP": str(cfg.home / "tmp"),
        "CODEX_CONFIG_TOML_PATH": str(p["codex"] / "config.toml"),
        "CODEX_MODEL_CATALOG_PATH": str(p["codex"] / "model_catalog.json"),
        "CODEX_PROVIDER_API_KEY_ENV": "AGENTSWE_BUILDER_API_KEY",
        "AGENTSWE_BUILDER_API_KEY": "broker-only-placeholder",
        "AGENTSWE_DEV_CASES": ",".join(protocol["dev_cases"]),
        "AGENTSWE_HIDDEN_CASES": ",".join(protocol["heldout_cases"]),
        "AGENTSWE_PROMPT_STRATEGY": "none",
        "AGENTSWE_PROTOCOL_MODE": mode,
        "AGENTSWE_RUNTIME_MODEL": roles["RUNTIME"].model, "AGENTSWE_RUNTIME_EFFORT": roles["RUNTIME"].effort,
        "AGENTSWE_JUDGE_BASE_URL": judge_base, "AGENTSWE_JUDGE_RESPONSES_URL": judge_base + "/responses",
        "AGENTSWE_JUDGE_MODEL": j.model, "AGENTSWE_JUDGE_EFFORT": j.effort,
        "AGENTSWE_SEARCH_BASE_URL": search_base,
        "AGENTSWE_JUDGE_FORMAT": judge_format(cfg),
    })
    if search_front:
        env["AGENTSWE_SEARCH_FRONT"] = json.dumps(search_front)
    # task-specific evaluator paths (runner_config.env; "$AGENTSWE_HOME" expands to this install's home)
    for key, value in (rc.get("env") or {}).items():
        env[key] = str(value).replace("$AGENTSWE_HOME", str(cfg.home))
    if smoke:
        env["AGENTSWE_BUILDER_TIMEOUT_SEC"] = str(protocol["builder_session_sec"])
    adapter_dir = task.dir / "adapter"
    venv_python = harbor_root / "venv-creation" / "bin" / "python"
    cmd = [str(venv_python), str(adapter_dir / "one_stop.py"),
           "--builder-agent", profile["harness"], "--builder-agent-import-path", profile["agent_import_path"],
           "--builder-model", b.model, "--builder-harness-version", profile["harness_version"],
           "--builder-reasoning-effort", b.effort,
           "--builder-package", str(staged(cfg, task, "builder_package")), "--benchmark", str(staged(cfg, task, "benchmark")),
           "--env-prefix", str(env_prefix), "--credential-file", str(secrets / "judge.env"),
           "--jobs-dir", str(cfg.home / "jobs"), "--runs-dir", str(p["root"]), "--run-id", run_id,
           "--max-dev-rounds", str(protocol["max_dev_rounds"]), "--n-concurrent", str(rc.get("n_concurrent", 2))]
    cleanup = (f"docker rm -f {' '.join(containers)} >/dev/null 2>&1; rm -f "
               + " ".join(shlex.quote(str(secrets / f"{r}.key")) for r in ("builder", "runtime", "judge", "search"))
               + "; rm -rf " + shlex.quote(str(secrets / "search-front")))
    wrapped = ["bash", "-c", " ".join(shlex.quote(c) for c in cmd) +
               f"; rc=$?; {cleanup}; echo \"===== $(date -u +%FT%TZ) one_stop exit $rc; broker and key files removed\"; exit $rc"]
    with open(p["log"], "a") as log:
        log.write(f"===== {util.now()} {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(wrapped, cwd=str(adapter_dir), env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    manifest = {
        "run_id": run_id, "task": task.id, "family": task.family, "mode": mode, "comparable": not smoke,
        "profile": {"name": cfg.profile, "explicit": bool(cfg.get("AGENTSWE_PROFILE"))},
        "host": socket.gethostname(), "started_at": util.now(), "pid": proc.pid, "run_dir": str(p["run_dir"]),
        "log": str(p["log"]), "broker": {"name": broker_name, "container_id": broker_cid, "host": broker_host, "port": port,
                                          "ledger": str(p["ledger"]), "containers": containers,
                                          "provider_brokers": {"builder": builder_port, "runtime": runtime_port,
                                                               "judge": judge_port}},
        "protocol": protocol, "builder": {"profile": builder, "model": b.model, "effort": b.effort,
                                          "codex_config": util.read_json(p["codex"] / "codex_config_manifest.json")},
        "runtime": {"model": roles["RUNTIME"].model, "effort": roles["RUNTIME"].effort},
        "judge": {"model": j.model, "effort": j.effort},
        "search": search_record if search_front else None,
        "repo_commit": util.out(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "repo_dirty": bool(util.out(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"])),
    }
    util.write_json(p["launch"], manifest)
    return manifest


def status(cfg: Config, launch: dict) -> dict:
    run_dir = Path(launch["run_dir"])
    summary = util.read_json(run_dir / "one_stop_summary.json")
    lifecycle = util.read_json(run_dir / "dev_lifecycle.json", []) or []
    alive = util.pid_alive(int(launch["pid"]))
    dev = [r.get("dev_mean") for r in lifecycle if isinstance(r, dict)]
    phase = ("finished" if summary else "held-out" if (run_dir / "freeze_manifest.json").exists()
             else "builder/dev" if alive else "stopped")
    return {"run_id": launch["run_id"], "alive": alive, "phase": phase, "dev_scores": dev,
            "frozen": (run_dir / "freeze_manifest.json").exists(),
            "heldout_mean": (summary or {}).get("hidden_mean"), "status": (summary or {}).get("status")}


MAX_ERROR_CHARS = 4000


def last_infrastructure_error(run_dir: Path) -> dict | None:
    """The newest infrastructure error the one_stop recorded: hidden-phase replay events
    (hidden_infrastructure_events.json, written after the freeze) win over dev-evaluation events
    (infrastructure_events.json); within a ledger the last event is the newest."""
    for name, key in (("hidden_infrastructure_events.json", "error"), ("infrastructure_events.json", "infrastructure_error")):
        events = util.read_json(run_dir / name, []) or []
        events = [e for e in events if isinstance(e, dict) and e.get(key)] if isinstance(events, list) else []
        if events:
            last = events[-1]
            error = str(last[key])
            return {"source": name, "phase": last.get("phase") or "dev_evaluation",
                    "phase_id": last.get("phase_id") or last.get("submission_id"), "state": last.get("state"),
                    "at": last.get("recorded_at") or last.get("finished_at") or last.get("submitted_at"),
                    "error": error if len(error) <= MAX_ERROR_CHARS else error[:MAX_ERROR_CHARS - 3] + "..."}
    return None


def ended_without_result(launch: dict, last_error: dict | None) -> dict:
    """`result` for a run whose one_stop is gone without its summary (the counterpart of Editing's
    orchestrator_log_tail): its exit status, its last recorded infrastructure error and its log tail."""
    ended = util.ended_one_stop(Path(launch["log"]))
    return {"run_id": launch["run_id"], "task": launch["task"], "family": launch["family"], "mode": launch["mode"],
            "comparable": launch["comparable"], "builder": launch["builder"],
            "ended_without_result": True, "score": None, "valid": False,
            "invalid_reason": "one_stop ended without writing one_stop_summary.json",
            "exit_status": ended["exit_status"], "exit_logged_at": ended["exit_logged_at"],
            "last_infrastructure_error": last_error,
            "one_stop_log": ended["one_stop_log"], "one_stop_log_tail": ended["one_stop_log_tail"]}


def process_gone(launch: dict) -> bool:
    """The run's one_stop has exited: its pid is gone on the host that launched it."""
    return launch.get("host") in (None, socket.gethostname()) and not util.pid_alive(int(launch["pid"]))


def result(cfg: Config, launch: dict) -> dict | None:
    run_dir = Path(launch["run_dir"])
    summary = util.read_json(run_dir / "one_stop_summary.json")
    if not summary:
        return ended_without_result(launch, last_infrastructure_error(run_dir)) if process_gone(launch) else None
    hidden = launch["protocol"]["heldout_cases"]
    scores = summary.get("hidden_scores") or []
    ledger_calls = 0
    ledger_tokens = 0
    for f in (Path(launch["broker"]["ledger"]) / "evaluator").glob("*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                ledger_calls += 1
                ledger_tokens += int(row.get("tokens") or 0)
    res = {
        "run_id": launch["run_id"], "task": launch["task"], "family": launch["family"], "mode": launch["mode"],
        "comparable": launch["comparable"], "builder": launch["builder"],
        "score": summary.get("hidden_mean"), "score_kind": "heldout_mean",
        "valid": summary.get("status") == "completed", "invalid_reason": None if summary.get("status") == "completed"
        else summary.get("status"),
        "per_case": dict(zip(hidden, scores)),
        "dev_scores": [r.get("dev_mean") for r in summary.get("dev_lifecycle", []) if isinstance(r, dict)],
        "usage": {"runtime": {"calls": ledger_calls, "tokens": ledger_tokens}},
        "provenance": {"repo_commit": launch.get("repo_commit"), "repo_dirty": launch.get("repo_dirty"),
                       "benchmark_digest": summary.get("benchmark_digest"),
                       "trusted_runtime_digest": summary.get("trusted_runtime_digest")},
    }
    util.write_json(Path(launch["log"]).with_name(launch["run_id"] + ".result.json"), res)
    return res


def run_containers(run_dir: str) -> list[str]:
    """Containers whose mounts point into the run directory (Harbor compose projects of this run)."""
    ids = []
    for cid in util.out(["docker", "ps", "-aq"]).split():
        mounts = util.out(["docker", "inspect", cid, "--format", "{{range .Mounts}}{{.Source}} {{end}}"])
        if run_dir in mounts:
            ids.append(cid)
    return ids


def stop(cfg: Config, launch: dict) -> None:
    """Stop the controller's process group, then remove what Harbor leaves behind on SIGTERM: the run's
    compose containers (found by mount, never by name), their egress sidecars and their networks."""
    pid = int(launch["pid"])
    if util.pid_alive(pid):
        os.killpg(pid, 15)
        deadline = time.time() + 60
        while util.pid_alive(pid) and time.time() < deadline:
            time.sleep(2)
    projects = set()
    for cid in run_containers(launch["run_dir"]):
        projects.add(util.out(["docker", "inspect", cid, "--format", '{{index .Config.Labels "com.docker.compose.project"}}']))
        util.run(["docker", "rm", "-f", cid], check=False)
    for project in filter(None, projects):
        for cid in util.out(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split():
            util.run(["docker", "rm", "-f", cid], check=False)
        util.run(["docker", "network", "rm", f"{project}_default"], check=False)
    for cid in launch["broker"].get("containers") or [launch["broker"]["container_id"]]:
        util.run(["docker", "rm", "-f", cid], check=False)
    for role in ("builder", "runtime", "judge"):
        (cfg.home / "secrets" / launch["run_id"] / f"{role}.key").unlink(missing_ok=True)
