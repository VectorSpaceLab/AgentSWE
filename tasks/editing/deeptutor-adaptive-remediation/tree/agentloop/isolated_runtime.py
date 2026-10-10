"""Narrow task-local namespaces; never import Candidate code on the host."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess


def sandbox_command(argv, repository, runtime_root, python, *, relay_socket=None,
                    readonly=(), writable=(), repository_readonly=False):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap is required for Candidate execution")
    clean = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    probe = subprocess.run([python, "-I", "-c", "import json,sys; print(json.dumps([sys.prefix,sys.base_prefix]))"],
        cwd="/", env=clean, text=True, capture_output=True, timeout=30, check=True)
    prefixes = [Path(raw).resolve() for raw in json.loads(probe.stdout)]
    command = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net", "--cap-drop", "ALL"]
    for raw in ("/usr", "/bin", "/lib", "/lib64", "/etc/localtime", "/etc/hosts", "/etc/nsswitch.conf"):
        if Path(raw).exists():
            command += ["--ro-bind", raw, raw]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    mounted = []
    for prefix in dict.fromkeys(prefixes):
        if str(prefix).startswith(("/usr/", "/bin/", "/lib/")) or prefix == Path("/usr"):
            continue
        if len(prefix.parts) < 4 or prefix in (Path("@@AGENTSWE_LEGACY_DATA@@"), Path("@@AGENTSWE_LEGACY_HOME@@")):
            raise RuntimeError("refusing broad Python runtime mount")
        command += ["--ro-bind", str(prefix), str(prefix)]
        mounted.append(str(prefix))
    for path, flag in [(repository, "--ro-bind" if repository_readonly else "--bind"), (runtime_root, "--bind"),
                       *((path, "--ro-bind") for path in readonly), *((path, "--bind") for path in writable)]:
        path = Path(path).resolve()
        if len(path.parts) < 4 or path in (Path("@@AGENTSWE_LEGACY_DATA@@"), Path("@@AGENTSWE_LEGACY_HOME@@")):
            raise RuntimeError("refusing broad task mount")
        command += [flag, str(path), str(path)]
    if relay_socket is not None:
        command += ["--ro-bind", str(relay_socket), "/run/agentswe/lower.sock"]
    command += ["--chdir", str(repository), "--", *argv]
    return command, {"mechanism": "bubblewrap", "candidate_mount": str(repository),
        "runtime_mount": str(runtime_root), "python_dependency_mounts": mounted,
        "evaluator_source_mounted": False, "private_fixture_mounted": False,
        "hidden_inventory_mounted": False, "credential_file_mounted": False,
        "network_namespace": "isolated", "host_tcp_network_access": False,
        "extra_readonly_mounts": [str(p) for p in readonly], "extra_writable_mounts": [str(p) for p in writable],
        "broker_relay_mounted": relay_socket is not None, "capabilities": "dropped_all"}
