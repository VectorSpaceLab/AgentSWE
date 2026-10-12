"""`agentswe doctor`: host, configuration and setup checks. Read-only; prints no key values."""
from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import util
from .config import REPO_ROOT, Config
from .registry import Task

OK, WARN, FAIL = "ok", "warn", "FAIL"
HOME_DISK_GIB = 30  # free space below this at AGENTSWE_HOME is a warning even without a task
CGROUP_ROOT = Path("/sys/fs/cgroup")  # cgroup v2 (unified hierarchy) when it holds cgroup.controllers
SYSTEMD_RUNTIME_DIR = Path("/run/systemd/system")  # present when systemd is the running init (sd_booted)


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""


def docker_env(cfg: Config) -> dict:
    env = os.environ.copy()
    plugins = cfg.home / "harbor" / "docker-config"
    if (plugins / "cli-plugins").is_dir():
        env["DOCKER_CONFIG"] = str(plugins)
    return env


def version_tuple(text: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def docker_ip(interface: str = "docker0") -> str:
    data = util.out(["ip", "-j", "-4", "addr", "show", interface])
    try:
        return json.loads(data)[0]["addr_info"][0]["local"]
    except (ValueError, IndexError, KeyError):
        return ""


def platform_check(system: str | None = None, machine: str | None = None) -> Check:
    """AgentSWE's images, environments and systemd units are built for Linux on x86-64."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    detail = f"{system} {machine}"
    if system != "Linux" or machine not in ("x86_64", "amd64"):
        return Check(WARN, "platform", f"{detail}: AgentSWE runs on Linux x86-64 hosts; images, environments and "
                                       "Editing's systemd units are not built for this platform")
    return Check(OK, "platform", detail)


def free_gib(path: Path) -> float:
    """Free space on the file system that holds `path` (its nearest existing parent while it does not exist)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 2**30


def host_checks(cfg: Config) -> list[Check]:
    checks: list[Check] = [platform_check()]
    if not shutil.which("docker"):
        return checks + [Check(FAIL, "docker", "docker CLI not found")]
    server = util.out(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20)
    checks.append(Check(OK if version_tuple(server) else FAIL, "docker daemon", server or "unreachable"))
    info = util.out(["docker", "info", "--format", "{{.CgroupVersion}} {{.CgroupDriver}} {{json .RegistryConfig.Mirrors}}"],
                    timeout=20)
    checks.append(Check(OK if info.startswith("2") else WARN, "cgroup", info))
    env = docker_env(cfg)
    compose = util.out(["docker", "compose", "version", "--short"], env=env, timeout=20)
    cv = version_tuple(compose)
    checks.append(Check(OK if cv >= (2, 20, 0) else WARN, "docker compose >= 2.20",
                        compose + ("" if cv >= (2, 20, 0) else "  (setup installs a private plugin)")))
    buildx = util.out(["docker", "buildx", "version"], env=env, timeout=20)
    has_buildx = "github.com/docker/buildx" in buildx
    checks.append(Check(OK if has_buildx else WARN, "docker buildx",
                        buildx.splitlines()[0] if has_buildx else "missing (setup installs a private plugin)"))
    home = cfg.home
    top = cfg.home_git_toplevel()
    checks.append(Check(FAIL if top else OK, "AGENTSWE_HOME outside git work trees",
                        f"{home} is inside {top}: evaluators' git apply would skip Candidate patches" if top else str(home)))
    home.mkdir(parents=True, exist_ok=True)
    free_gb = free_gib(home)
    checks.append(Check(OK if free_gb >= HOME_DISK_GIB else WARN, "disk at AGENTSWE_HOME",
                        f"{home}: {free_gb:.0f} GiB free" + ("" if free_gb >= HOME_DISK_GIB else
                                                            f" (less than {HOME_DISK_GIB} GiB)")))
    broker = cfg.get("AGENTSWE_BROKER_HOST") or docker_ip()
    checks.append(Check(OK if broker else FAIL, "broker host (docker0 address)", broker or "no docker0 IPv4 address"))
    routes, subnets = host_networks()
    checks.append(pool_check("AGENTSWE_NETWORK_POOL", cfg.get("AGENTSWE_NETWORK_POOL"), routes, subnets,
                             home_subnets(cfg.home)))
    return checks


# Address pools besides AGENTSWE_NETWORK_POOL. The Optimization runner exports all three to every run, with the
# defaults in runners/optimization_native_v1.py POOLS: the compose adapter allocates every Optimization task's /28
# networks from AGENTSWE_OPTIMIZATION_POOL, and the tau3 and PinchBench controllers allocate from their own pools.
OPTIMIZATION_TASK_POOLS = {"tau3-retail": ("AGENTSWE_TAU3_POOL",),
                           "pinchbench-openclaw": ("AGENTSWE_PINCHBENCH_POOL",)}


def task_pools(task: Task) -> tuple[str, ...]:
    """The pool variables a task's runs allocate Docker subnets from, besides AGENTSWE_NETWORK_POOL (a host check)."""
    if task.data.get("family") != "optimization":
        return ()
    return ("AGENTSWE_OPTIMIZATION_POOL", *OPTIMIZATION_TASK_POOLS.get(task.data.get("id"), ()))


def pool_value(cfg: Config, name: str) -> str | None:
    from .runners.optimization_native_v1 import POOLS
    return cfg.get(name) or POOLS.get(name)


def host_networks() -> tuple[list[str], list[str]]:
    """Destinations of the host's routes, and the subnets of every Docker network."""
    try:
        rows = json.loads(util.out(["ip", "-j", "route", "show"]) or "[]")
    except ValueError:
        rows = []
    routes = [r["dst"] for r in rows if isinstance(r, dict) and r.get("dst") not in (None, "default")]
    subnets: list[str] = []
    ids = util.out(["docker", "network", "ls", "-q"], timeout=20).split()
    if ids and not ids[0].startswith("<error"):
        listed = util.out(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids],
                          timeout=60)
        if not listed.startswith("<error"):
            subnets = listed.split()
    return routes, subnets


def home_subnets(home: Path) -> set[str]:
    """Subnets this AGENTSWE_HOME's own runs allocated: Creation's network_allocations/allocations.json and the
    Optimization IPAM registries under coordination/ (compose, tau3, PinchBench). A run's Docker network shows up
    both as a Docker network and as a host route on its bridge, so these are not overlaps with something else.
    A registry's base_cidr is the pool itself, not an allocation."""
    found: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key != "base_cidr":
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and "/" in value:
            try:
                found.add(str(ipaddress.ip_network(value, strict=False)))
            except ValueError:
                pass

    for path in [home / "network_allocations" / "allocations.json", *sorted((home / "coordination").glob("*/*.json"))]:
        try:
            walk(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return found


def pool_check(name: str, pool: str | None, routes: list[str], subnets: list[str],
               own: set[str] | frozenset[str] = frozenset()) -> Check:
    """WARN when the pool overlaps a host route or a Docker network's subnet that is not one of this home's own run
    networks (`own`, see home_subnets); FAIL when it is not a CIDR block."""
    label = "network pool" if name == "AGENTSWE_NETWORK_POOL" else f"network pool {name}"
    try:
        block = ipaddress.ip_network(pool)
    except (TypeError, ValueError) as exc:
        return Check(FAIL, label, f"{pool}: {exc}")

    mine: set[str] = set()

    def overlapping(cidrs: list[str]) -> list[str]:
        found = []
        for cidr in cidrs:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if net.version == block.version and net.overlaps(block) and cidr not in found:
                if str(net) in own:
                    mine.add(str(net))
                else:
                    found.append(cidr)
        return found

    def shown(cidrs: list[str]) -> str:
        return str(cidrs[:5]) + (f" (+{len(cidrs) - 5} more)" if len(cidrs) > 5 else "")

    on_routes = overlapping(routes)
    on_docker = [c for c in overlapping(subnets) if c not in on_routes]
    clash = ([f"overlaps routes {shown(on_routes)}"] if on_routes else []) + \
            ([f"overlaps Docker networks {shown(on_docker)}"] if on_docker else [])
    note = [f"{len(mine)} subnet(s) in use by this home's runs"] if mine else []
    return Check(WARN if clash else OK, label, f"{pool}" + (" " + "; ".join(clash + note) if clash + note else ""))


def config_checks(cfg: Config, roles: set[str], services: set[str]) -> list[Check]:
    checks = []
    for name in sorted(roles):
        r = cfg.role(name)
        problems = []
        if not r.base_url:
            problems.append("BASE_URL missing")
        if not r.api_key:
            problems.append("API_KEY missing")
        if not r.model:
            problems.append("MODEL missing")
        if r.wire not in ("responses", "chat"):
            problems.append(f"WIRE {r.wire!r} must be responses or chat")
        status = FAIL if problems else OK
        checks.append(Check(status, f"role {name}", "; ".join(problems) or
                            f"{r.model} / {r.effort or '-'} via {r.base_url} (key set)"))
        if r.base_url:
            # a request without the key: any HTTP status (401 included) means reachable; probe-roles tests the key
            status = util.reachable(r.base_url)
            checks.append(Check(OK, f"reach {name}", f"{r.base_url} -> HTTP {status} (reachable; `agentswe probe-roles` "
                                                    "sends a real request)") if status.isdigit() else
                          Check(WARN, f"reach {name}", f"{r.base_url} -> {status} (not reachable from this host)"))
    if "search" in services:
        base, key = cfg.search
        checks.append(Check(OK if base and key else FAIL, "role SEARCH", "configured" if base and key else
                            "AGENTSWE_SEARCH_BASE_URL / AGENTSWE_SEARCH_API_KEY required"))
    return checks


def builder_proxy_check() -> Check:
    """The Editing Builder relay's HTTP CONNECT proxy on 127.0.0.1:7890 (probed with a CONNECT to a throwaway local
    listener; nothing leaves the host)."""
    from . import loopback_proxy
    host, port = loopback_proxy.DEFAULT_LISTEN
    state, detail = loopback_proxy.probe(host, port)
    name = f"Builder CONNECT proxy {host}:{port}"
    if state == "proxy":
        return Check(OK, name, f"listener present ({detail}); runs use it")
    if state == "absent":
        return Check(OK, name, "none; `agentswe run` starts the bundled proxy (agentswe/loopback_proxy.py)")
    return Check(FAIL, name, f"{detail}: not an HTTP CONNECT proxy; free the port (an own proxy there works as is)")


def task_exports(cfg: Config, task: Task) -> list[Path]:
    """Host directories `setup` exports for this task: the `exports` of its kind=export images (images.json, with
    their depends_on) and its runner_config.host_exports destinations."""
    images = util.read_json(REPO_ROOT / "images" / "images.json", {}) or {}
    specs = images.get("images", {}) if isinstance(images, dict) else {}
    paths, seen, todo = [], set(), list(task.data.get("images", []))
    while todo:
        name = todo.pop(0)
        if name in seen or name not in specs:
            continue
        seen.add(name)
        spec = specs[name]
        todo += list(spec.get("depends_on", []))
        if spec.get("kind") == "export":
            paths += [Path(str(v).replace("$AGENTSWE_HOME", str(cfg.home))) for v in spec.get("exports", {}).values()]
    paths += [cfg.home / item["dest"] for item in task.data.get("runner_config", {}).get("host_exports", [])
              if item.get("dest")]
    return paths


def task_disk_check(cfg: Config, task: Task) -> Check | None:
    """WARN when AGENTSWE_HOME has less free space than the task declares (task.json host.disk_gb)."""
    need = task.data.get("host", {}).get("disk_gb")
    if not need:
        return None
    free = free_gib(cfg.home)
    detail = f"{free:.0f} GiB free at {cfg.home}; the task needs about {need} GiB (task.json host.disk_gb)"
    return Check(OK if free >= need else WARN, "disk for this task", detail)


def cgroup_v2_check() -> Check:
    """The host's cgroup hierarchy is v2 (unified), which the task declares it needs (task.json host.cgroup_v2)."""
    if (CGROUP_ROOT / "cgroup.controllers").is_file():
        return Check(OK, "cgroup v2", f"{CGROUP_ROOT} is the unified (v2) hierarchy")
    return Check(WARN, "cgroup v2", f"{CGROUP_ROOT} is not a cgroup v2 hierarchy; this task needs cgroup v2 "
                                    "(boot with the unified hierarchy, systemd.unified_cgroup_hierarchy=1)")


def systemd_delegation_checks() -> list[Check]:
    """What `systemd-run --unit=...` needs (task.json host.systemd_delegation): the tool, systemd as the running
    init, and permission to create system units (Editing runs start system transient units, not --user ones)."""
    tool = shutil.which("systemd-run")
    checks = [Check(OK if tool else FAIL, "systemd-run", tool or "not found; install systemd (the task starts its "
                                                                 "runs as systemd transient units)")]
    checks.append(Check(OK, "systemd init", f"{SYSTEMD_RUNTIME_DIR} present") if SYSTEMD_RUNTIME_DIR.is_dir() else
                  Check(WARN, "systemd init", f"{SYSTEMD_RUNTIME_DIR} is missing: systemd is not the running init "
                                              "(for example in a container), so systemd-run cannot start units"))
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        checks.append(Check(WARN, "systemd unit permission", "not running as root: the task's runs start system "
                                                             "transient units, which a non-root user may create only "
                                                             "if polkit allows it; run setup, run, result and stop as "
                                                             "the same user with the same AGENTSWE_HOME"))
    return checks


def editing_provider_check(cfg: Config) -> Check:
    """Editing task trees call the official DeepSeek API with pinned lower-agent and judge models."""
    from .runners.editing_agentloop_v1 import EDITING_PROVIDER_URL, editing_provider_problems
    problems = editing_provider_problems(cfg)
    if problems:
        return Check(FAIL, "Editing provider", "setup and run refuse this configuration: " + "; ".join(problems))
    return Check(OK, "Editing provider", f"every role on {EDITING_PROVIDER_URL}; lower agent and judge as the task "
                                         "trees pin them")


def task_checks(cfg: Config, task: Task) -> list[Check]:
    checks = []
    host = task.data.get("host", {})
    for tool in host.get("host_tools", []):
        checks.append(Check(OK if shutil.which(tool) else FAIL, f"host tool {tool}", shutil.which(tool) or "not found"))
    if host.get("kvm"):
        checks.append(Check(OK if os.path.exists("/dev/kvm") else FAIL, "KVM", "/dev/kvm"))
    if host.get("cgroup_v2"):
        checks.append(cgroup_v2_check())
    if host.get("systemd_delegation"):
        # the systemd-run line is left out when host_tools already lists it (checked above)
        checks += [c for c in systemd_delegation_checks()
                   if not (c.name == "systemd-run" and "systemd-run" in host.get("host_tools", []))]
    disk = task_disk_check(cfg, task)
    if disk:
        checks.append(disk)
    if task.data.get("family") == "editing":
        checks.append(editing_provider_check(cfg))
        checks.append(builder_proxy_check())
    pools = task_pools(task)
    if pools:
        routes, subnets = host_networks()
        own = home_subnets(cfg.home)
        checks += [pool_check(name, pool_value(cfg, name), routes, subnets, own) for name in pools]
    state = util.read_json(cfg.home / "state" / "setup.json", {}) or {}
    done = state.get("tasks", {}).get(task.id)
    missing = [str(p) for p in task_exports(cfg, task) if not p.is_dir()] if done else []
    if missing:
        checks.append(Check(WARN, f"setup {task.id}", f"done {done['at']}, but missing now: {', '.join(missing)}; "
                                                      f"set up again: agentswe setup {task.id}"))
    else:
        checks.append(Check(OK if done else WARN, f"setup {task.id}",
                            f"done {done['at']}" if done else "not set up yet: agentswe setup " + task.id))
    if task.data.get("status") not in ("runnable", "verified"):
        checks.append(Check(WARN, f"status {task.id}", f"task status is {task.data.get('status')!r}"))
    return checks


def run_doctor(cfg: Config, tasks: list[Task]) -> int:
    roles = {r.upper() for t in tasks for r in t.data.get("roles", [])} or {"BUILDER", "RUNTIME", "JUDGE"}
    services = {s for t in tasks for s in t.data.get("services", [])}
    sections = [("host", host_checks(cfg)), ("configuration", config_checks(cfg, roles, services))]
    for t in tasks:
        sections.append((t.label, task_checks(cfg, t)))
    worst = OK
    for title, checks in sections:
        print(f"== {title}")
        for c in checks:
            print(f"  [{c.status:4}] {c.name}: {c.detail}")
            if c.status == FAIL:
                worst = FAIL
            elif c.status == WARN and worst == OK:
                worst = WARN
    print(f"== result: {worst}")
    return 1 if worst == FAIL else 0
