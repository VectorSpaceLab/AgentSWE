"""Optimization runner: the task's one_stop controller with a Codex builder.

Per run:
  * a builder provider broker (broker/agentswe_broker) and a Codex config from the generator
    (both multi-agent switches off); Codex sees only a placeholder token;
  * one 0600 evaluator credential file holding the RUNTIME key, mounted only into the
    evaluator-owned per-case brokers and graders (as in the Lite runs), together with the
    non-secret RUNTIME Responses URL, model and effort and the JUDGE effort, which those brokers
    lock on every call; tasks whose `services` include "search" also get the SEARCH key, base URL
    and wire there (the BrowseComp broker sidecar is the only holder);
  * Harbor `site` profile plus the Optimization builder-resume overlay (Lite defaults: on, 3 tries);
  * one_stop.py under the Python-only optimization env; when it exits, the wrapper removes the
    builder broker and deletes the key file.
--smoke runs against a truncated copy of the benchmark (task.json `smoke`) and is not comparable.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import util
from ..config import REPO_ROOT, Config
from ..doctor import docker_ip
from ..profiles import staged
from ..registry import Task
from .creation_harbor_v1 import (active_runs, ended_without_result, free_port, generate_codex_config,
                                 process_gone, run_containers, start_role_broker)

FAMILY_DIR = "optimization"
POOLS = {"AGENTSWE_OPTIMIZATION_POOL": "198.18.0.0/15", "AGENTSWE_TAU3_POOL": "198.18.0.0/15",
         "AGENTSWE_PINCHBENCH_POOL": "100.64.0.0/10"}


def _paths(cfg: Config, run_id: str) -> dict[str, Path]:
    root = cfg.home / "runs" / FAMILY_DIR
    return {"root": root, "run_dir": root / run_id, "launch": root / f"{run_id}.launch.json",
            "log": root / f"{run_id}.one_stop.log", "codex": root / f"{run_id}.codex",
            "secrets": cfg.home / "secrets" / run_id, "ledger": cfg.home / "broker_ledgers" / run_id}


def task_config_env(cfg: Config, rc: dict) -> dict[str, str]:
    """The task's declared configuration settings (runner_config.config_env) as resolved (defaults < profile <
    .env < process); unset ones are left out. Exported to the task's controller and recorded in the manifest."""
    return {key: cfg.get(key) for key in rc.get("config_env", []) if cfg.get(key) is not None}


def benchmark_root(cfg: Config, task: Task, smoke: dict | None) -> Path:
    """AGENTSWE_OPTIMIZATION_BENCHMARK_ROOT holding <benchmark name>/ for the adapter's lookup table."""
    name = task.data["runner_config"]["benchmark_name"]
    source = staged(cfg, task, "benchmark")
    if not smoke:
        root = cfg.home / "benchmarks" / "optimization"
        root.mkdir(parents=True, exist_ok=True)
        link = root / name
        if not link.is_symlink() or link.resolve() != source.resolve():
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(source)
        return root
    root = cfg.home / "benchmarks" / "optimization-smoke"
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    contract = json.loads((dest / "task_contract.json").read_text())
    keep = {}
    for role, n_key, case_dir in (("dev", "dev_cases_n", "dev_cases"), ("hidden", "heldout_cases_n", "test_cases")):
        rows = contract["splits"][role]["cases"]
        explicit = smoke.get(f"{case_dir}_ids")  # optional explicit smoke cases (task.json `smoke`)
        rows = [r for r in rows if r["case_id"] in explicit] if explicit else rows[: int(smoke[n_key])]
        contract["splits"][role]["cases"] = rows
        keep[case_dir] = {str(r["case_id"]) for r in rows}
    contract["smoke"] = {"note": "truncated copy for an AgentSWE smoke run; scores are not comparable"}
    (dest / "task_contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    for case_dir, ids in keep.items():
        d = dest / case_dir
        if d.is_dir():
            for child in d.iterdir():
                if child.name not in ids:
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
    return root


def run(cfg: Config, task: Task, *, builder: str, seed: int, smoke: bool, label: str | None) -> dict:
    state = util.read_json(cfg.home / "state" / "setup.json", {}) or {}
    if task.id not in state.get("tasks", {}):
        raise SystemExit(f"run `agentswe setup {task.id}` first")
    limit = int(cfg.get("AGENTSWE_MAX_CONCURRENT_RUNS"))
    if len(active_runs(cfg)) >= limit:
        raise SystemExit(f"runs already active on this host reach AGENTSWE_MAX_CONCURRENT_RUNS={limit}")
    profiles = json.loads((REPO_ROOT / "builders" / "codex" / "profiles.json").read_text())["profiles"]
    if builder not in profiles:
        raise SystemExit(f"unknown builder profile {builder!r}; known: {sorted(profiles)}")
    profile = profiles[builder]
    roles = {n: cfg.role(n, "OPTIMIZATION") for n in ("BUILDER", "RUNTIME")}
    for n, r in roles.items():
        if not (r.base_url and r.api_key and r.model):
            raise SystemExit(f"role {n} is not fully configured; run `agentswe doctor`")
    if roles["RUNTIME"].wire != "responses":
        raise SystemExit("Optimization evaluator brokers need a Responses-wire RUNTIME provider for now")
    b = roles["BUILDER"]
    rt, jd = roles["RUNTIME"], cfg.role("JUDGE", "OPTIMIZATION")
    search: dict[str, str] = {}
    if "search" in task.data.get("services", []):
        search_base, search_key = cfg.search
        if not search_base or not search_key:
            raise SystemExit("this task needs a search provider: set AGENTSWE_SEARCH_BASE_URL and AGENTSWE_SEARCH_API_KEY")
        search = {"AGENTSWE_SEARCH_API_KEY": search_key, "AGENTSWE_SEARCH_BASE_URL": search_base,
                  "AGENTSWE_SEARCH_WIRE": (cfg.get("AGENTSWE_SEARCH_WIRE") or "serper").lower()}
    provider = {"AGENTSWE_RUNTIME_RESPONSES_URL": rt.responses_url, "AGENTSWE_RUNTIME_MODEL": rt.model,
                "AGENTSWE_RUNTIME_EFFORT": rt.effort, "AGENTSWE_JUDGE_EFFORT": jd.effort}
    rc = task.data["runner_config"]
    protocol = dict(task.data["protocol"])
    smoke_cfg = task.data.get("smoke") if smoke else None
    if smoke:
        protocol.update(smoke_cfg)
    mode = "smoke" if smoke else "formal"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"o-{task.short}-{builder}-{b.model.split('/')[-1]}-s{seed}-{mode}-{stamp}".lower()
    if label:
        run_id = f"{label}-{run_id}"
    p = _paths(cfg, run_id)
    p["root"].mkdir(parents=True, exist_ok=True)
    broker_host = cfg.get("AGENTSWE_BROKER_HOST") or docker_ip()
    bench_root = benchmark_root(cfg, task, smoke_cfg)
    bench = bench_root / rc["benchmark_name"]

    secrets = p["secrets"]
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    util.write_secret(secrets / "builder.key", {"AGENTSWE_BUILDER_API_KEY": b.api_key})
    util.write_secret(secrets / "evaluator.env", {"AGENTSWE_RUNTIME_API_KEY": rt.api_key, **provider, **search})
    images = json.loads((REPO_ROOT / "images" / "images.json").read_text())["images"]
    containers: list[str] = []
    try:
        builder_port = free_port(broker_host)
        containers.append(start_role_broker(cfg, run_id, "builder", broker_host, builder_port, secrets / "builder.key",
                                            p["ledger"] / "builder", images["builder-codex"]["tag"]))
        generate_codex_config(profile, b, f"http://{broker_host}:{builder_port}/v1", p["codex"])
    except BaseException:
        for cid in containers:
            util.run(["docker", "rm", "-f", cid], check=False)
        for name in ("builder.key", "evaluator.env"):
            (secrets / name).unlink(missing_ok=True)
        raise

    harbor_root = cfg.home / "harbor" / "roots" / "site"
    coord = cfg.home / "coordination"
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(("_API_KEY", "_TOKEN")) and k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    env.update({
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "AGENTSWE_HOME": str(cfg.home),
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "runners" / "optimization"), str(REPO_ROOT / "builders" / "codex")]),
        "AGENTSWE_HARBOR_ROOT": str(harbor_root), "AGENTSWE_DOCKER_CONFIG": str(cfg.home / "harbor" / "docker-config"),
        "AGENTSWE_OPTIMIZATION_BENCHMARK_ROOT": str(bench_root),
        "AGENTSWE_SNAPSHOTS": str(cfg.home / "snapshots"),
        "AGENTSWE_STANDALONE_PYTHON312": str(cfg.home / "tools" / "cpython-3.12"),
        "OPTIMIZATION_INFRA_RESUME": "1",
        "OPTIMIZATION_HARBOR_OVERLAY": str(REPO_ROOT / "runners" / "optimization" / "harbor_overlay"),
        "AGENTSWE_DEV_MAX_ROUNDS": str(protocol["max_dev_rounds"]),
        "AGENTSWE_INFRA_RESUME_MAX_ATTEMPTS": "3", "AGENTSWE_INFRA_RESUME_MIN_WAIT_SEC": "30",
        "AGENTSWE_INFRA_RESUME_MAX_WAIT_SEC": "180", "AGENTSWE_EVAL_RESUME_MAX_ATTEMPTS": "3",
        "AGENTSWE_EVAL_RESUME_MIN_WAIT_SEC": "30", "AGENTSWE_EVAL_RESUME_MAX_WAIT_SEC": "180",
        "AGENTSWE_BUILDER_CONTINUATION_POLL_SEC": "60", "AGENTSWE_BUILDER_CONTINUATION_MAX_ATTEMPTS": "20",
        "AGENTSWE_COMPOSE_COORDINATION_ROOT": str(coord / "compose"),
        "AGENTSWE_TAU3_COORDINATION_ROOT": str(coord / "tau3"),
        "AGENTSWE_PINCHBENCH_COORDINATION_ROOT": str(coord / "pinchbench"),
        "CODEX_CONFIG_TOML_PATH": str(p["codex"] / "config.toml"),
        "CODEX_MODEL_CATALOG_PATH": str(p["codex"] / "model_catalog.json"),
        "CODEX_PROVIDER_API_KEY_ENV": "AGENTSWE_BUILDER_API_KEY",
        "AGENTSWE_BUILDER_API_KEY": "broker-only-placeholder",
        "AGENTSWE_PROTOCOL_MODE": mode,
        **provider,
        "TMPDIR": str(cfg.home / "tmp"),
    })
    for key, default in POOLS.items():
        env[key] = cfg.get(key) or default
    if cfg.get("AGENTSWE_DOCKER_REGISTRY"):
        env["AGENTSWE_DOCKER_REGISTRY"] = cfg.get("AGENTSWE_DOCKER_REGISTRY")  # run-local template base images
    # task-declared host locations for its native controller (for example the TerminalBench assets)
    for key, value in rc.get("env", {}).items():
        env[key] = str(value).format(home=cfg.home, repo=REPO_ROOT)
    # task-declared configuration settings (defaults < profile < .env < process), exported as resolved
    config_env = task_config_env(cfg, rc)
    env.update(config_env)
    if smoke:
        env["AGENTSWE_BUILDER_TIMEOUT_SEC"] = str(protocol["builder_session_sec"])
    for d in (coord / "compose", coord / "tau3", coord / "pinchbench", cfg.home / "jobs", cfg.home / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    py = cfg.home / "envs" / rc["python_runtime_env"] / "bin" / "python3.11"
    cmd = [str(py), str(REPO_ROOT / "runners" / "optimization" / "one_stop.py"),
           "--builder-agent", profile["harness"],
           "--builder-agent-import-path", rc.get("agent_import_path", "resumable_provider_codex_agent:ResumableDeepSeekCodex"),
           "--builder-model", b.model, "--builder-harness-version", profile["harness_version"],
           "--builder-reasoning-effort", b.effort, "--builder-package", str(bench), "--benchmark", str(bench),
           "--env-prefix", str(cfg.home / "envs" / rc["python_runtime_env"]),
           "--credential-file", str(secrets / "evaluator.env"),
           "--jobs-dir", str(cfg.home / "jobs"), "--runs-dir", str(p["root"]), "--run-id", run_id,
           "--max-dev-rounds", str(protocol["max_dev_rounds"]), "--n-concurrent", str(rc.get("n_concurrent", 4)),
           "--infrastructure-resume"]
    cleanup = (f"docker rm -f {' '.join(containers)} >/dev/null 2>&1; rm -f "
               + " ".join(shlex.quote(str(secrets / n)) for n in ("builder.key", "evaluator.env")))
    wrapped = ["bash", "-c", " ".join(shlex.quote(c) for c in cmd) +
               f"; rc=$?; {cleanup}; echo \"===== $(date -u +%FT%TZ) one_stop exit $rc; broker and key files removed\"; exit $rc"]
    with open(p["log"], "a") as log:
        log.write(f"===== {util.now()} {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(wrapped, cwd=str(REPO_ROOT / "runners" / "optimization"), env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    manifest = {
        "run_id": run_id, "task": task.id, "family": task.family, "mode": mode, "comparable": not smoke,
        "host": socket.gethostname(), "started_at": util.now(), "pid": proc.pid, "run_dir": str(p["run_dir"]),
        "log": str(p["log"]), "broker": {"container_id": containers[0], "containers": containers,
                                          "ledger": str(p["ledger"]), "host": broker_host, "builder_port": builder_port},
        "protocol": protocol, "benchmark": str(bench), "config_env": config_env,
        "builder": {"profile": builder, "model": b.model, "effort": b.effort,
                    "codex_config": util.read_json(p["codex"] / "codex_config_manifest.json")},
        "runtime": {"model": rt.model, "effort": rt.effort, "responses_url": rt.responses_url,
                    "judge_effort": jd.effort},
        "search": ({"base_url": search["AGENTSWE_SEARCH_BASE_URL"], "wire": search["AGENTSWE_SEARCH_WIRE"]}
                   if search else None),
        "repo_commit": util.out(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "repo_dirty": bool(util.out(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"])),
    }
    util.write_json(p["launch"], manifest)
    return manifest


def setup(cfg: Config, task: Task, s) -> None:
    """Task-declared evaluator assets, verifier wheels and digest-pinned images (runner_config)."""
    from .optimization_assets import setup as assets_setup
    record = assets_setup(cfg, task)
    if record:
        s.state.setdefault("optimization_assets", {})[task.id] = {**record, "at": util.now()}
        s.save()


# one_stop.run_adapter_phase keeps, per evaluation phase (<run_id>-baseline, -dev-rNNN-aNNN, -initial-test,
# -candidate-test), controller_logs/<phase>/infrastructure_resume_state.json (status, resume_count, events) and
# attempt_NNN/stderr.log; the dev controller's own evaluation resumes go to infrastructure_events.json. With
# --infrastructure-resume (always passed) a phase resumes infrastructure failures without limit, as in the paper.
RESUME_STATE = "infrastructure_resume_state.json"
SHORT_MESSAGE = 300


def _short(text: str | None) -> str | None:
    text = " ".join(str(text or "").split())
    return text if len(text) <= SHORT_MESSAGE else text[: SHORT_MESSAGE - 3] + "..."


def _attempt_error(log_dir: Path, event: dict) -> dict:
    """An infrastructure event as type and short message: the controller records only that the attempt failed and
    where its stderr is, so the message is the last line of that attempt's stderr.log when it exists."""
    error = {"type": event.get("error_type") or event.get("status"), "attempt": event.get("attempt"),
             "at": event.get("at"), "message": _short(event.get("error_message"))}
    if not event.get("error_type") and (event.get("infrastructure_cases") or event.get("invalid_cases")):
        cases = sorted(set(event.get("infrastructure_cases") or []) | set(event.get("invalid_cases") or []))
        error["message"] = "infrastructure cases: " + ", ".join(cases)
    stderr = log_dir / f"attempt_{int(event.get('attempt') or 0):03d}" / "stderr.log"
    if event.get("error_type") and stderr.is_file():
        lines = [line for line in stderr.read_text(errors="replace").splitlines() if line.strip()]
        if lines:
            error["message"] = _short(lines[-1])
            # Harbor case failures name each trial's exception: keep the distinct ones (e.g. a start timeout)
            exceptions = sorted(set(re.findall(r"'exception': '([^']*)'", lines[-1])))[:5]
            if exceptions:
                error["exceptions"] = exceptions
        error["stderr"] = str(stderr)
    return error


def evaluation_phases(run_dir: Path, run_id: str) -> list[dict]:
    """Every evaluation phase the controller started, oldest first, from its controller_logs."""
    phases = []
    root = run_dir / "controller_logs"
    for path in root.glob(f"*/{RESUME_STATE}") if root.is_dir() else []:
        state = util.read_json(path, {}) or {}
        if not isinstance(state, dict):
            continue
        phase_id = str(state.get("phase_id") or path.parent.name)
        events = [e for e in state.get("events") or [] if isinstance(e, dict)]
        phases.append({"phase": phase_id[len(run_id) + 1:] if phase_id.startswith(run_id + "-") else phase_id,
                       "status": state.get("status"), "resume_count": int(state.get("resume_count") or 0),
                       "last_wait_sec": state.get("last_wait_sec"),
                       "last_error": _attempt_error(path.parent, events[-1]) if events else None,
                       "controller_logs": str(path.parent), "_mtime": path.stat().st_mtime})
    phases.sort(key=lambda p: p["_mtime"])
    for p in phases:
        p.pop("_mtime")
    return phases


def infrastructure_status(run_dir: Path, run_id: str) -> dict | None:
    """The current evaluation phase's infrastructure resumes (count, status, last error) and, while the Builder is
    developing, the dev controller's evaluation resumes (infrastructure_events.json)."""
    phases = evaluation_phases(run_dir, run_id)
    events = util.read_json(run_dir / "infrastructure_events.json", []) or []
    events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    if not phases and not events:
        return None
    value: dict = {"resume_limit": "none (infrastructure failures are resumed until the run is stopped)",
                   "controller_logs": str(run_dir / "controller_logs")}
    if phases:
        # the phase the controller wrote last is the one in progress (phases run one after another)
        value["current_phase"] = phases[-1]
    if events:
        last = events[-1]
        value["dev_evaluation_resumes"] = {
            "events": len(events), "round": last.get("round"), "resume_attempt": last.get("resume_attempt"),
            "state": last.get("state"), "last_error": {"type": last.get("error_type"), "at": last.get("at"),
                                                       "message": _short(last.get("error_message"))}}
    return value


def status(cfg: Config, launch: dict) -> dict:
    run_dir = Path(launch["run_dir"])
    summary = util.read_json(run_dir / "one_stop_summary.json")
    lifecycle = util.read_json(run_dir / "dev_lifecycle.json", []) or []
    alive = util.pid_alive(int(launch["pid"]))
    phase = ("finished" if summary else "held-out" if (run_dir / "freeze_manifest.json").exists()
             else "builder/dev" if alive else "stopped")
    res = {"run_id": launch["run_id"], "alive": alive, "phase": phase,
           "dev_scores": [r.get("dev_mean", r.get("score")) for r in lifecycle if isinstance(r, dict)],
           "frozen": (run_dir / "freeze_manifest.json").exists(),
           "heldout_mean": (summary or {}).get("hidden_mean"), "initial_mean": (summary or {}).get("initial_test_mean"),
           "status": (summary or {}).get("status")}
    infrastructure = infrastructure_status(run_dir, launch["run_id"])
    if infrastructure:
        res["infrastructure"] = infrastructure
    return res


def last_infrastructure_error(run_dir: Path, run_id: str) -> dict | None:
    """The newest infrastructure error status shows: the last evaluation phase's, else the dev controller's."""
    infrastructure = infrastructure_status(run_dir, run_id) or {}
    phase = infrastructure.get("current_phase") or {}
    if phase.get("last_error"):
        return {"source": "controller_logs", "phase": phase.get("phase"), "status": phase.get("status"),
                **phase["last_error"]}
    dev = infrastructure.get("dev_evaluation_resumes") or {}
    if dev.get("last_error") and any(dev["last_error"].values()):
        return {"source": "infrastructure_events.json", "phase": "dev_evaluation", "state": dev.get("state"),
                **dev["last_error"]}
    return None


def result(cfg: Config, launch: dict) -> dict | None:
    summary = util.read_json(Path(launch["run_dir"]) / "one_stop_summary.json")
    if not summary:
        if not process_gone(launch):
            return None
        return ended_without_result(launch, last_infrastructure_error(Path(launch["run_dir"]), launch["run_id"]))
    j1, j0 = summary.get("hidden_mean"), summary.get("initial_test_mean")
    s = None
    if isinstance(j1, (int, float)) and isinstance(j0, (int, float)):
        s = 100.0 * min(1.0, (j1 - j0) / (100.0 - j0)) if j0 < 100 else 0.0
    res = {"run_id": launch["run_id"], "task": launch["task"], "family": launch["family"], "mode": launch["mode"],
           "comparable": launch["comparable"], "builder": launch["builder"],
           "score": round(s, 2) if s is not None else None, "score_kind": "S (relative improvement over a0)",
           "candidate_heldout_mean": j1, "initial_heldout_mean": j0,
           "valid": summary.get("status") == "completed",
           "invalid_reason": None if summary.get("status") == "completed" else summary.get("status"),
           "per_case": {"candidate": summary.get("hidden_scores"), "initial": summary.get("initial_test_scores")},
           "provenance": {"repo_commit": launch.get("repo_commit"), "repo_dirty": launch.get("repo_dirty"),
                          "benchmark_digest": summary.get("benchmark_digest")},
           # how many infrastructure resumes each evaluation phase took (0 when it passed first time)
           "infrastructure_resumes": {p["phase"]: p["resume_count"]
                                      for p in evaluation_phases(Path(launch["run_dir"]), launch["run_id"])}}
    util.write_json(Path(launch["log"]).with_name(launch["run_id"] + ".result.json"), res)
    return res


def stop(cfg: Config, launch: dict) -> None:
    from .creation_harbor_v1 import stop as creation_stop
    creation_stop(cfg, launch)
    (cfg.home / "secrets" / launch["run_id"] / "evaluator.env").unlink(missing_ok=True)
