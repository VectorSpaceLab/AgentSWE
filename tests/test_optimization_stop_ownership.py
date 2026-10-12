"""`agentswe stop` for an Optimization run removes the run's containers, including those that mount only its Harbor
job directories under <home>/jobs, and never another run's (stdlib unittest, docker faked).

A public tau3 run left an exited Harbor verifier container whose mounts were
<home>/jobs/optimization-eval-<benchmark>-<run_id>-dev-r001-a001/<trial>/verifier and <home>/envs/...; stop looked
for mounts under the run directory only and kept it. Here two runs share one home, the second one's id being the
first one's with a label in front, and both have containers of every kind."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import optimization_native_v1 as opt  # noqa: E402

BENCH = "tau3-tool-agent-optimization-v1"
RUN_A = "o-tau3-codex-deepseek-flash-s1-formal-20260101t000000z"
RUN_B = "rerun-" + RUN_A  # `agentswe run --label rerun` in the same second


class Cfg:
    def __init__(self, home: Path):
        self.home = home


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class FakeDocker:
    """`docker ps`, `inspect`, `rm` and `network` over a fixed set of containers."""

    def __init__(self, containers: dict[str, dict]):
        self.containers = containers
        self.removed: list[str] = []

    def info(self, cid: str) -> dict:
        c = self.containers[cid]
        labels = dict(c.get("labels", {}))
        return {"Id": cid, "Name": "/" + c["name"], "Mounts": [{"Source": s} for s in c.get("mounts", [])],
                "Config": {"Labels": labels}}

    def out(self, argv, **kw):
        live = [cid for cid in self.containers if cid not in self.removed]
        if argv[:3] == ["docker", "ps", "-aq"]:
            if "--filter" in argv:
                key, _, value = argv[argv.index("--filter") + 1].removeprefix("label=").partition("=")
                return "\n".join(cid for cid in live if self.info(cid)["Config"]["Labels"].get(key) == value)
            return "\n".join(live)
        if argv[:2] == ["docker", "inspect"] and argv[2] in live:
            info = self.info(argv[2])
            if "--format" in argv:  # the earlier, Creation-style lookups
                if "Mounts" in argv[-1]:
                    return " ".join(m["Source"] for m in info["Mounts"])
                return info["Config"]["Labels"].get("com.docker.compose.project", "")
            return json.dumps([info])
        if argv[:2] == ["docker", "inspect"]:
            return "[]"
        if argv[:3] == ["docker", "network", "ls"]:
            return ""
        raise AssertionError(argv)

    def run(self, argv, **kw):
        if argv[:3] == ["docker", "rm", "-f"]:
            self.removed.append(argv[3])
            return subprocess.CompletedProcess(argv, 0, "")
        if argv[:3] == ["docker", "network", "rm"]:
            return subprocess.CompletedProcess(argv, 1, "no such network")
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, "")
        raise AssertionError(argv)


class StopFindsTheRunsOwnContainers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = home = Path(tmp.name) / "home"
        jobs, runs = home / "jobs", home / "runs" / "optimization"
        self.launch = {}
        for run_id in (RUN_A, RUN_B):
            run_dir = runs / run_id
            phase = f"{run_id}-dev-r001-a001"
            write(run_dir / "builder_job_config.json",
                  {"job_name": f"formal-persistent-builder-codex-{run_id}", "jobs_dir": str(jobs)})
            write(run_dir / "evaluations" / phase / "eval_job_config.json",
                  {"job_name": f"optimization-eval-{BENCH}-{phase}", "jobs_dir": str(jobs)})
            for name in ("builder.key", "evaluator.env"):
                write(home / "secrets" / run_id / name, {})
            self.launch[run_id] = {"run_id": run_id, "pid": 0, "run_dir": str(run_dir),
                                   "benchmark": str(home / "benchmarks" / "optimization" / BENCH),
                                   "broker": {"container_id": f"broker-{run_id}", "containers": [f"broker-{run_id}"]}}
        # a TerminalBench-style nested job of run A: its name does not carry the run id, its config is in run A's dir
        write(runs / RUN_A / "evaluations" / f"{RUN_A}-initial-test" / "native_eval" / "test_001" / "harbor_job.json",
              {"job_name": "agentswe-tb-fix-git-0a1b2c3d4e5f", "jobs_dir": str(jobs)})
        eval_a = jobs / f"optimization-eval-{BENCH}-{RUN_A}-dev-r001-a001"
        eval_b = jobs / f"optimization-eval-{BENCH}-{RUN_B}-dev-r001-a001"
        run_a, run_b = runs / RUN_A, runs / RUN_B
        shared = [str(home / "envs" / "optimization-python311")]
        project = "dev_008__kp6h7hf__verifier__trial"  # compose project names come from trial names, not runs
        self.docker = FakeDocker({
            # run A: the exited verifier the earlier stop kept (mounts under <home>/jobs, compose labels in the run dir)
            "a-verifier": {"name": f"{project}-main-1", "mounts": [str(eval_a / "dev_008__KP6H7hF" / "verifier"), *shared],
                           "labels": {"com.docker.compose.project": project,
                                      "com.docker.compose.project.working_dir":
                                          str(run_a / "evaluations" / f"{RUN_A}-dev-r001-a001" / "eval_tasks" / "dev_008" / "tests"),
                                      "com.docker.compose.project.config_files":
                                          f"{home}/tmp/x/compose.json,{run_a}/evaluations/{RUN_A}-dev-r001-a001/eval_tasks/dev_008/tests/docker-compose.yaml"}},
            "a-job-mount": {"name": "dev_009__q__verifier__trial-main-1", "mounts": [str(eval_a / "dev_009__Q" / "verifier")]},
            "a-sidecar": {"name": "dev_008__kp6h7hf__env-harbor-docker-egress-control-sidecar-1",
                          "labels": {"com.docker.compose.project": "dev_008__kp6h7hf__env",
                                     "com.docker.compose.project.working_dir": str(run_a / "evaluations" / "e" / "environment")}},
            "a-builder": {"name": "builder_task__x__env-main-1",
                          "mounts": [str(jobs / f"formal-persistent-builder-codex-{RUN_A}" / "builder_task__x" / "agent")]},
            "a-nested-tb": {"name": "fix-git__y__env-main-1",
                            "mounts": [str(jobs / "agentswe-tb-fix-git-0a1b2c3d4e5f" / "fix-git__y" / "verifier")]},
            "a-run-mount": {"name": "dev_011__tjj6fds__env-main-1", "mounts": [str(run_a / "evaluations" / "x" / "out")]},
            # run B: same compose project name as run A's verifier, its own job and run directories
            "b-verifier": {"name": f"{project}-main-1", "mounts": [str(eval_b / "dev_008__kp6h7hf" / "verifier"), *shared],
                           "labels": {"com.docker.compose.project": project,
                                      "com.docker.compose.project.working_dir": str(run_b / "evaluations" / "t")}},
            "b-run-mount": {"name": "dev_001__z__env-main-1", "mounts": [str(run_b / "evaluations" / "y")]},
            "b-builder": {"name": "builder_task__w__env-main-1",
                          "mounts": [str(jobs / f"formal-persistent-builder-codex-{RUN_B}" / "builder_task__w" / "agent")]},
            # neither run: shared mounts only, a name-prefix sibling of run A's dir and of its job, another home
            "shared-only": {"name": "x-main-1", "mounts": shared},
            "prefix-sibling": {"name": "y-main-1", "mounts": [str(runs / (RUN_A + "-old") / "z"),
                                                              str(jobs / (eval_a.name + "-old") / "t" / "verifier")]},
            "other-home": {"name": "z-main-1", "mounts": [f"/srv/other-home/jobs/{eval_a.name}/dev_008__K/verifier"],
                           "labels": {"com.docker.compose.project.working_dir": f"/srv/other-home/runs/optimization/{RUN_A}/e"}},
        })

    def stop(self, run_id: str) -> list[str]:
        before = len(self.docker.removed)
        with mock.patch.object(opt.util, "out", side_effect=self.docker.out), \
                mock.patch.object(opt.util, "run", side_effect=self.docker.run), \
                mock.patch.object(opt.util, "pid_alive", return_value=False):
            opt.stop(Cfg(self.home), self.launch[run_id])
        return sorted(self.docker.removed[before:])

    def test_stop_removes_exactly_the_first_runs_containers(self):
        self.assertEqual(self.stop(RUN_A), sorted(["a-verifier", "a-job-mount", "a-sidecar", "a-builder", "a-nested-tb",
                                                   "a-run-mount", f"broker-{RUN_A}"]))
        self.assertEqual(sorted(p.name for p in (self.home / "secrets" / RUN_A).iterdir()), [])
        self.assertEqual(sorted(p.name for p in (self.home / "secrets" / RUN_B).iterdir()),
                         ["builder.key", "evaluator.env"])
        log = (self.home / "DELETIONS.log").read_text()
        self.assertIn(f"stop {RUN_A}: removed container a-verifier", log)
        self.assertNotIn(RUN_B, log)

    def test_stop_of_the_labelled_run_removes_only_its_containers(self):
        self.assertEqual(self.stop(RUN_B), sorted(["b-verifier", "b-run-mount", "b-builder", f"broker-{RUN_B}"]))

    def test_the_listing_is_read_only_and_says_why(self):
        with mock.patch.object(opt.util, "out", side_effect=self.docker.out), \
                mock.patch.object(opt.util, "run", side_effect=AssertionError("listing must not run docker rm")):
            owned = {c["id"]: c["reason"] for c in opt.run_owned_containers(Cfg(self.home), self.launch[RUN_A])}
        self.assertEqual(owned, {"a-verifier": "mount under a Harbor job of the run",
                                 "a-job-mount": "mount under a Harbor job of the run",
                                 "a-sidecar": "compose working directory under the run",
                                 "a-builder": "mount under a Harbor job of the run",
                                 "a-nested-tb": "mount under a Harbor job of the run",
                                 "a-run-mount": "mount under the run directory"})

    def test_a_launch_whose_run_dir_does_not_name_the_run_selects_nothing(self):
        launch = dict(self.launch[RUN_A], run_dir=str(self.home / "runs" / "optimization"))
        with mock.patch.object(opt.util, "out", side_effect=self.docker.out):
            self.assertEqual(opt.run_owned_containers(Cfg(self.home), launch), [])


if __name__ == "__main__":
    unittest.main()
