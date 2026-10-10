"""agentswe command line: list | doctor | probe-roles | setup | run | status | result | stop | freeze."""
from __future__ import annotations

import argparse
import json
import sys

from . import config, doctor, registry, runners, util
from .setup import setup_task


def _launches(cfg, run_id: str | None):
    rows = []
    for mf in sorted((cfg.home / "runs").glob("*/*.launch.json")):
        m = util.read_json(mf, {}) or {}
        if run_id is None or m.get("run_id") == run_id:
            rows.append(m)
    if run_id and not rows:
        raise SystemExit(f"unknown run {run_id}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentswe")
    ap.add_argument("--env-file", default=None, help="dotenv file (default: <repo>/.env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show the task registry")
    d = sub.add_parser("doctor", help="check host, configuration and setup state")
    d.add_argument("tasks", nargs="*")
    pr = sub.add_parser("probe-roles", help="one minimal request per configured role (and one search query)")
    pr.add_argument("--roles", default="BUILDER,RUNTIME,JUDGE", help="comma-separated roles to probe")
    pr.add_argument("--no-search", action="store_true", help="skip the search provider")
    pr.add_argument("--family", choices=("creation", "editing", "optimization"), default=None,
                    help="resolve the roles as that family does (AGENTSWE_<FAMILY>_<ROLE>_* first)")
    s = sub.add_parser("setup", help="install Harbor, build images and environments for a task")
    s.add_argument("task")
    r = sub.add_parser("run", help="start a run in the background")
    r.add_argument("task")
    r.add_argument("--builder", default="codex")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--smoke", action="store_true", help="cheap connectivity run: task smoke budget, not comparable")
    r.add_argument("--label", default=None)
    st = sub.add_parser("status", help="progress of runs")
    st.add_argument("run_id", nargs="?")
    rs = sub.add_parser("result", help="write and print result.json of a finished run")
    rs.add_argument("run_id")
    sp = sub.add_parser("stop", help="stop a run (process group, broker, key files)")
    sp.add_argument("run_id")
    fz = sub.add_parser(
        "freeze", help="budget freeze of a formal Editing run whose Builder was cut at its time budget",
        description="Apply the Editing rule to a formal run whose Builder Harbor cut at the 5 h budget: freeze the "
                    "last accepted submission, run the six held-out cases against it and finalize, through the task "
                    "tree's own code. Stages: check (read-only) -> freeze -> hidden -> finalize, or all. Without "
                    "--apply every stage is a dry run. Run it as the owner of the run directory. Supported tasks: "
                    "aider, deeptutor, openwiki; a run with no accepted submission is refused (its Result is 0).")
    fz.add_argument("run_id")
    fz.add_argument("--stage", choices=("check", "freeze", "hidden", "finalize", "all"), default="check",
                    help="check (default, read-only), freeze, hidden, finalize, or all three in turn")
    fz.add_argument("--apply", action="store_true", help="execute the stage (default: a dry run)")
    fz.add_argument("--operator", default=None,
                    help="who decided the freeze, for the record (default: the invoking user)")
    sub.add_parser("config", help="show resolved configuration (no key values)")
    a = ap.parse_args(argv)
    cfg = config.load(a.env_file)

    if a.cmd == "list":
        for t in registry.load_all():
            print(f"{t.label:48} {t.data.get('short',''):10} {t.data.get('tier',''):3} "
                  f"{'lite' if t.data.get('lite') else '    '} {t.data.get('status')}")
        return 0
    if a.cmd == "config":
        print(json.dumps(cfg.describe(), indent=2))
        return 0
    if a.cmd == "doctor":
        tasks = [registry.find(n) for n in a.tasks]
        return doctor.run_doctor(cfg, tasks)
    if a.cmd == "probe-roles":
        from .probe_roles import probe_roles
        report = probe_roles(cfg, tuple(r.strip().upper() for r in a.roles.split(",") if r.strip()),
                             search=not a.no_search, family=a.family)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if a.cmd == "setup":
        cfg.require_home_outside_git()
        setup_task(cfg, registry.find(a.task))
        return 0
    if a.cmd == "run":
        cfg.require_home_outside_git()
        task = registry.find(a.task)
        m = runners.load(task.runner).run(cfg, task, builder=a.builder, seed=a.seed, smoke=a.smoke, label=a.label)
        print(json.dumps({k: m[k] for k in ("run_id", "pid", "log", "mode")}, indent=2))
        return 0
    if a.cmd == "status":
        for m in _launches(cfg, a.run_id):
            task = registry.find(m["task"])
            print(json.dumps(runners.load(task.runner).status(cfg, m)))
        return 0
    if a.cmd == "result":
        m = _launches(cfg, a.run_id)[0]
        res = runners.load(registry.find(m["task"]).runner).result(cfg, m)
        if res is None:
            print("run has not finished")
            return 1
        print(json.dumps(res, indent=2))
        return 1 if res.get("ended_without_result") else 0
    if a.cmd == "freeze":
        m = _launches(cfg, a.run_id)[0]
        runner = runners.load(registry.find(m["task"]).runner)
        if not hasattr(runner, "freeze"):
            raise SystemExit(f"agentswe freeze applies to formal Editing runs only; {a.run_id} is a "
                             f"{m.get('family')} run")
        return runner.freeze(cfg, m, stage=a.stage, apply=a.apply, operator=a.operator)
    if a.cmd == "stop":
        m = _launches(cfg, a.run_id)[0]
        runners.load(registry.find(m["task"]).runner).stop(cfg, m)
        print(f"stopped {a.run_id}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
