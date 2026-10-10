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
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .. import util
from ..config import REPO_ROOT, Config
from ..doctor import docker_ip
from ..profiles import staged
from ..registry import Task
from .creation_harbor_v1 import (active_runs, ended_without_result, free_port, generate_codex_config,
                                 process_gone, start_role_broker)

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
            "events": len(events), "round": last.get("round"), "submission_id": last.get("submission_id"),
            "resume_attempt": last.get("resume_attempt", last.get("infrastructure_resume_attempts")),
            "state": last.get("state"), "last_error": _dev_event_error(last)}
    return value


def _dev_event_error(event: dict) -> dict:
    """A dev controller event's error as type, time and short message. A resume pause records error_type, at and
    error_message; the record of a submission that ended without a resume (state infrastructure_error) records
    infrastructure_error ("Type: message") and finished_at. When the message names a phase's stderr.log, `detail` is
    that log's last line (the adapter's own error)."""
    kind, message = event.get("error_type"), event.get("error_message")
    combined = event.get("infrastructure_error") or event.get("last_infrastructure_error")
    if not message and combined:
        message = str(combined)
        head, sep, _ = message.partition(": ")
        if not kind and sep and re.fullmatch(r"[A-Za-z_][\w.]*", head):
            kind = head
    error = {"type": kind, "at": event.get("at") or event.get("finished_at") or event.get("paused_at"),
             "message": _short(message)}
    match = re.search(r"see (/\S+/stderr\.log)", str(message or ""))
    if match and Path(match.group(1)).is_file():
        lines = [line for line in Path(match.group(1)).read_text(errors="replace").splitlines() if line.strip()]
        if lines:
            error["detail"] = _short(lines[-1])
    return error


def status(cfg: Config, launch: dict) -> dict:
    run_dir = Path(launch["run_dir"])
    summary = util.read_json(run_dir / "one_stop_summary.json")
    lifecycle = util.read_json(run_dir / "dev_lifecycle.json", []) or []
    alive = util.pid_alive(int(launch["pid"]))
    # one_stop evaluates the starter on the dev split (writing baseline.json) before the Builder session starts
    phase = ("finished" if summary else "held-out" if (run_dir / "freeze_manifest.json").exists()
             else "stopped" if not alive else "builder/dev" if (run_dir / "baseline.json").exists() else "baseline")
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
    phases = evaluation_phases(Path(launch["run_dir"]), launch["run_id"])
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
           "infrastructure_resumes": {p["phase"]: p["resume_count"] for p in phases},
           # how each evaluation phase ended: completed_without_resume / completed_after_resume, or
           # infrastructure_error (replayed by the dev controller as a new phase) / failed for one that raised
           "evaluation_phase_status": {p["phase"]: p["status"] for p in phases}}
    util.write_json(Path(launch["log"]).with_name(launch["run_id"] + ".result.json"), res)
    return res


# The run's Harbor jobs live under <home>/jobs, outside the run directory. one_stop names the builder job
# formal-persistent-builder-<agent>-<run_id> and the adapter names each Candidate and Eval job
# optimization-[eval-]<benchmark>-<run_id>-<phase>; every job also has its config (job_name, jobs_dir) under the run
# directory, as do the TerminalBench controller's nested jobs (harbor_job.json).
JOB_PHASE = r"(?:baseline|dev-r\d{3}-a\d{3}|initial-test|candidate-test)(?:-infra-a\d{3})?(?:-row-retry-\d{3})?"
JOB_CONFIGS = ("builder_job_config.json", "evaluations/*/*job_config.json",
               "evaluations/*/row_infrastructure_retries/*/*job_config.json",
               "evaluations/*/native_eval/*/harbor_job.json")
REMOVAL_WAIT_SECONDS = 30  # an --rm container the daemon is still removing (Docker 29)


def _roots(paths) -> set[PurePosixPath]:
    roots = set()
    for path in paths:
        roots.add(PurePosixPath(os.path.normpath(str(path))))
        roots.add(PurePosixPath(os.path.realpath(str(path))))
    return roots


def _under(path, roots: set[PurePosixPath]) -> bool:
    """`path` is one of `roots` or lies below one, compared by whole path components."""
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    candidate = PurePosixPath(os.path.normpath(path))
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def run_job_dirs(cfg: Config, launch: dict) -> set[Path]:
    """The Harbor job directories of this run: those its own job configs name, and those under <home>/jobs whose
    name is one of this run's job names (the run id as a whole name part, with the run's benchmark and a phase)."""
    run_dir, run_id = Path(launch["run_dir"]), str(launch["run_id"])
    found: set[Path] = set()
    for pattern in JOB_CONFIGS:
        for path in run_dir.glob(pattern):
            config = util.read_json(path, {}) or {}
            name, jobs = config.get("job_name"), config.get("jobs_dir")
            if (isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
                    and isinstance(jobs, str) and os.path.isabs(jobs)):
                found.add(Path(jobs) / name)
    names = [rf"formal-persistent-builder-[a-z0-9_]+-{re.escape(run_id)}"]
    benchmark = Path(str(launch.get("benchmark") or "")).name
    if benchmark:
        names.append(rf"optimization-(?:eval-)?{re.escape(benchmark)}-{re.escape(run_id)}-{JOB_PHASE}")
    own = re.compile("^(?:" + "|".join(names) + ")$")
    jobs_root = cfg.home / "jobs"
    for child in (jobs_root.iterdir() if jobs_root.is_dir() else ()):
        if own.match(child.name):
            found.add(child)
    return found


def container_owner(info: dict, run_roots: set[PurePosixPath], job_roots: set[PurePosixPath]) -> str | None:
    """Why a container belongs to the run, or None: a mount source under the run directory or under one of the
    run's Harbor job directories, or a compose working directory or compose file under them. Names are not used."""
    labels = (info.get("Config") or {}).get("Labels") or {}
    mounts = [m.get("Source") for m in info.get("Mounts") or [] if isinstance(m, dict)]
    if any(_under(source, run_roots) for source in mounts):
        return "mount under the run directory"
    if any(_under(source, job_roots) for source in mounts):
        return "mount under a Harbor job of the run"
    owned = run_roots | job_roots
    if _under(labels.get("com.docker.compose.project.working_dir"), owned):
        return "compose working directory under the run"
    if any(_under(path.strip(), owned)
           for path in str(labels.get("com.docker.compose.project.config_files") or "").split(",")):
        return "compose file under the run"
    return None


def run_owned_containers(cfg: Config, launch: dict) -> list[dict]:
    """The containers (running or exited) that belong to this run (container_owner), as id, name and reason.
    Read-only: lists and inspects containers, removes nothing."""
    run_dir, run_id = str(launch.get("run_dir") or ""), str(launch.get("run_id") or "")
    if not run_id or not os.path.isabs(run_dir) or Path(run_dir).name != run_id:
        return []
    run_roots, job_roots = _roots([run_dir]), _roots(run_job_dirs(cfg, launch))
    owned = []
    for cid in util.out(["docker", "ps", "-aq", "--no-trunc"]).split():
        try:
            info = json.loads(util.out(["docker", "inspect", cid]))[0]
        except (ValueError, IndexError, KeyError, TypeError):
            continue
        reason = container_owner(info, run_roots, job_roots) if isinstance(info, dict) else None
        if reason:
            owned.append({"id": cid, "name": str(info.get("Name") or "").lstrip("/"), "reason": reason,
                          "project": ((info.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project")})
    return owned


def remove_run_containers(cfg: Config, launch: dict) -> list[str]:
    """Remove the run's containers (run_owned_containers), then the now-empty networks of their compose projects.
    Each removal is logged in <home>/DELETIONS.log."""
    owned = run_owned_containers(cfg, launch)
    removed, removing = [], []
    for container in owned:
        r = util.run(["docker", "rm", "-f", container["id"]], check=False)
        label = f"container {container['id'][:12]} {container['name']} ({container['reason']})"
        if r.returncode == 0:
            removed.append(label)
        elif "already in progress" in (r.stdout or "").lower():
            removing.append((container["id"], label))
    pending, deadline = [cid for cid, _ in removing], time.monotonic() + REMOVAL_WAIT_SECONDS
    while pending and time.monotonic() < deadline:
        pending = [cid for cid in pending if util.run(["docker", "inspect", cid], check=False).returncode == 0]
        if pending:
            time.sleep(1)
    removed += [f"{label} (removed by the daemon)" for cid, label in removing if cid not in pending]
    for project in sorted({c["project"] for c in owned if c.get("project")}):
        for net in util.out(["docker", "network", "ls", "-q", "--filter",
                             f"label=com.docker.compose.project={project}"]).split():
            busy = util.out(["docker", "network", "inspect", net, "--format", "{{len .Containers}}"])
            if busy.strip() == "0" and util.run(["docker", "network", "rm", net], check=False).returncode == 0:
                removed.append(f"network {net[:12]} ({project})")
    if removed:
        with open(cfg.home / "DELETIONS.log", "a") as log:
            for item in removed:
                log.write(f"{util.now()} stop {launch['run_id']}: removed {item}\n")
    return removed


def stop(cfg: Config, launch: dict) -> None:
    """Stop the controller's process group, then remove the run's containers (remove_run_containers: found by
    mounts and compose labels under the run directory and the run's Harbor job directories, never by name), the
    builder broker and the run's key files."""
    pid = int(launch["pid"])
    if util.pid_alive(pid):
        os.killpg(pid, 15)
        deadline = time.time() + 60
        while util.pid_alive(pid) and time.time() < deadline:
            time.sleep(2)
    remove_run_containers(cfg, launch)
    for cid in launch["broker"].get("containers") or [launch["broker"]["container_id"]]:
        util.run(["docker", "rm", "-f", cid], check=False)
    for name in ("builder.key", "runtime.key", "judge.key", "evaluator.env"):
        (cfg.home / "secrets" / launch["run_id"] / name).unlink(missing_ok=True)
