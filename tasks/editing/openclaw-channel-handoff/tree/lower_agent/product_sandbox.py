"""Filesystem/netns boundary for actual OpenClaw Gateway and CLI commands.

Trusted evaluator code remains outside. Gateway and CLI have separate mount
and PID namespaces, sharing only the pinned Gateway network namespace via an
already-open namespace fd. Resource enforcement belongs to owned_resources.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time
import threading

from lower_agent.sandbox_transport import FixedLowerRelay
from lower_agent.fixture_transport import NativeRelay, PREFIXES, placeholder


def process_identity(pid: int) -> dict:
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rindex(")") + 2:].split()
    return {"pid": pid, "ppid": int(fields[1]), "start_ticks": int(fields[19]),
            "net_inode": os.stat(f"/proc/{pid}/ns/net").st_ino,
            "cgroup": Path(f"/proc/{pid}/cgroup").read_text()}


def is_descendant(pid: int, parent: int) -> bool:
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == parent:
            return True
        seen.add(pid)
        pid = process_identity(pid)["ppid"]
    return False


class ProductSandbox:
    def __init__(self, *, product: Path, state: Path, workspace: Path,
                 runtime: dict, output: Path, endpoint: str, bridge_port: int,
                 env: dict[str, str], deadline: float, product_readonly=False,
                 fixture_services: dict | None = None, gateway_id: str | None = None,
                 case_socket: Path | None = None, native_origin_credential: str | None = None):
        self.product, self.state, self.workspace, self.output = (
            Path(item).resolve() for item in (product, state, workspace, output))
        # The evaluator output root includes oracle inputs/reports; it must
        # never be used as the default agent workspace or a mounted ancestor.
        self.mounts = (self.product, self.state, self.workspace)
        for mount in self.mounts:
            if len(mount.parts) < 4 or self.output == mount or self.output.is_relative_to(mount):
                raise ValueError("sandbox mount would expose evaluator output or a broad root")
        for index, mount in enumerate(self.mounts):
            if not mount.is_dir():
                raise ValueError("sandbox mounts must already exist")
            for other in self.mounts[index + 1:]:
                if mount == other or mount.is_relative_to(other) or other.is_relative_to(mount):
                    raise ValueError("sandbox writable roots must be disjoint")
        self.runtime = runtime
        runtime_root = Path(str(runtime["root"])).resolve()
        node = Path(str(runtime["node"])).resolve(strict=True)
        if not node.is_relative_to(runtime_root) or node.parent.name != "bin":
            raise ValueError("Node executable is outside the pinned task runtime")
        # Toolchain only: do NOT mount runtime/baseline, verification, caches,
        # the benchmark root, the original Candidate, or evaluator directories.
        self.toolchain_mounts = [runtime_root / "bin", node.parent.parent, runtime_root / "lib"]
        if any(not path.is_dir() or not path.resolve().is_relative_to(runtime_root)
               for path in self.toolchain_mounts):
            raise ValueError("pinned task toolchain mounts are missing or escape their prefix")
        self.deadline = deadline
        self.bridge_port = bridge_port
        self.endpoint = f"http://127.0.0.1:{bridge_port}/v1/responses"
        self.env = dict(env)
        self.env.update({"OPENAI_API_KEY": "broker-only-placeholder",
                         "AGENTSWE_REQUIRED_MODEL": "deepseek-flash",
                         "AGENTSWE_REQUIRED_REASONING_EFFORT": "high",
                         "AGENTSWE_RESPONSES_BASE_URL": self.endpoint,
                         "OPENAI_BASE_URL": self.endpoint.removesuffix("/v1/responses"),
                         "PATH": f"{runtime_root}/bin:/usr/bin:/bin"})
        self.product_readonly = product_readonly
        if gateway_id is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', gateway_id):
            raise ValueError('invalid evaluator-owned Gateway identity')
        self.gateway_id = gateway_id
        self.case_socket = Path(case_socket) if case_socket is not None else None
        if self.case_socket is not None and not self.case_socket.is_socket():
            raise ValueError('case client requires an existing owned Unix socket')
        services = fixture_services or {}
        if set(services) - set(PREFIXES):
            raise ValueError('unknown native fixture role')
        ports = [bridge_port]
        for role, spec in services.items():
            if set(spec) != {'socket', 'token', 'port'} or not Path(spec['socket']).is_socket():
                raise ValueError('invalid explicit native service mapping')
            if type(spec['port']) is not int or not 1024 <= spec['port'] <= 65535:
                raise ValueError('invalid native loopback port')
            if role != 'effect' and (not isinstance(spec['token'], str) or not spec['token']):
                raise ValueError('missing evaluator-owned native service token')
            ports.append(spec['port'])
        if len(set(ports)) != len(ports):
            raise ValueError('native and model bridge ports must be distinct')
        self.native_relays, self.native_env, self.native_ports = {}, {}, {}
        self.relay = FixedLowerRelay(endpoint, deadline)
        try:
            for role, spec in sorted(services.items()):
                self.native_relays[role] = NativeRelay(role=role, upstream=spec['socket'],
                    token=spec['token'], deadline=deadline, origin_credential=native_origin_credential)
                self.native_ports[role] = spec['port']
                prefix = 'OPENCLAW_HANDOFF_' + PREFIXES[role]
                suffix = '/effects' if role == 'effect' else ''
                self.native_env[prefix + '_URL'] = f"http://127.0.0.1:{spec['port']}{suffix}"
                if role != 'effect':
                    self.native_env[prefix + '_TOKEN'] = placeholder(role)
        except BaseException:
            for relay in self.native_relays.values():
                relay.close()
            self.relay.close()
            raise
        self.process = None
        self.process_owner_thread = None
        self.namespace = None
        self.net_fd = None
        self.peers = []
        self.attestation = {"schema_version": "openclaw-product-sandbox-v1", "valid": False,
                            "product_readonly": product_readonly,
                            "mounts": [str(path) for path in self.mounts],
                            "toolchain_mounts": [str(path) for path in self.toolchain_mounts],
                            "real_product_behavior_verified": False}

    def base_command(self, *, new_network: bool, gateway_id: str | None = None) -> list[str]:
        command = ["/usr/bin/bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                   "--unshare-cgroup", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                   "--clearenv", "--ro-bind", "/usr", "/usr"]
        if new_network:
            command.append("--unshare-net")
        for path in ("/bin", "/lib", "/lib64"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/etc"]
        for path in ("/etc/hosts", "/etc/nsswitch.conf", "/etc/localtime"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        for path in self.toolchain_mounts:
            command += ["--ro-bind", str(path), str(path)]
        for path in self.mounts:
            mode = "--ro-bind" if path == self.product and self.product_readonly else "--bind"
            command += [mode, str(path), str(path)]
        command += ["--dir", "/agentswe", "--ro-bind", str(self.relay.socket_path), "/agentswe/lower.sock",
                    "--ro-bind", str(Path(__file__).with_name("sandbox_transport.py")), "/agentswe/transport.py"]
        if self.native_relays:
            command += ['--ro-bind', str(Path(__file__).with_name('fixture_transport.py')),
                        '/agentswe/fixture_transport.py']
            for role, relay in sorted(self.native_relays.items()):
                command += ['--ro-bind', str(relay.socket_path), f'/agentswe/native-{role}.sock']
        if self.case_socket is not None:
            command += ['--ro-bind', str(self.case_socket), '/agentswe/case.sock',
                        '--ro-bind', str(Path(__file__).with_name('native_client.py')),
                        '/agentswe/native_client.py']
        # Explicit values only; no inherited evaluator credentials, proxy,
        # PYTHONPATH/NODE_OPTIONS preload, stats token, or task/oracle paths.
        allowed = {"PATH", "HOME", "XDG_CACHE_HOME", "TMPDIR", "LANG", "LC_ALL", "TZ",
                   "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_SKIP_CHANNELS",
                   "OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENTSWE_RESPONSES_BASE_URL",
                   "AGENTSWE_REQUIRED_MODEL", "AGENTSWE_REQUIRED_REASONING_EFFORT"}
        for key, value in sorted(self.env.items()):
            if key in allowed:
                command += ["--setenv", key, value]
        # These come only from constructor-owned mappings, not inherited env.
        for key, value in sorted(self.native_env.items()):
            command += ['--setenv', key, value]
        identity = gateway_id if gateway_id is not None else self.gateway_id
        if identity is not None:
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', identity):
                raise ValueError('invalid evaluator-owned Gateway identity')
            command += ['--setenv', 'OPENCLAW_HANDOFF_GATEWAY_ID', identity]
            # Native OpenClaw opt-in skips the shared-config singleton lock;
            # its per-port lock remains enabled. Required by two-Gateway cases.
            command += ['--setenv', 'OPENCLAW_ALLOW_MULTI_GATEWAY', '1']
            # Each Gateway is a separate PID/mount namespace. PID-based
            # process-local locks must not share a temp directory; persistent
            # handoff state remains at the same OPENCLAW_STATE_DIR/SQLite.
            command += ['--setenv', 'TMPDIR', '/tmp']
        command += ["--setenv", "NO_PROXY", "localhost,127.0.0.1,::1",
                    "--setenv", "RAYON_NUM_THREADS", "16", "--setenv", "MALLOC_ARENA_MAX", "1",
                    "--chdir", str(self.product)]
        return command

    def start(self, command: list[str], log, *, startup_deadline: float | None = None):
        if self.process is not None:
            raise RuntimeError("sandbox Gateway may only be started once")
        read_fd, write_fd = os.pipe()
        try:
            # The fixed native bridges wrap (not replace) the unchanged
            # lower-model transport and actual Gateway command.
            entry = []
            if self.native_relays:
                entry = ['/usr/bin/python3', '-I', '-B', '/agentswe/fixture_transport.py',
                         '--deadline', str(self.deadline)]
                for role, port in sorted(self.native_ports.items()):
                    entry += ['--service', role, f'/agentswe/native-{role}.sock', str(port)]
                entry += ['--']
            wrapped = self.base_command(new_network=True) + ["--info-fd", str(write_fd), "--",
                *entry,
                "/usr/bin/python3", "-I", "-B", "/agentswe/transport.py", "--inside",
                "--socket", "/agentswe/lower.sock", "--port", str(self.bridge_port),
                "--deadline", str(self.deadline), "--", *command]
            # bwrap --die-with-parent is tied to the Linux thread that forks.
            # A response-loss fault callback is short-lived; retain a dedicated
            # creator until the exact owned wrapper exits.
            launched=threading.Event()
            launch_errors=[]
            def own_process():
                try:
                    self.process = subprocess.Popen(wrapped, cwd=self.product, env={"PATH": "/usr/bin:/bin"},
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(write_fd,))
                    self.attestation['process_creator_native_thread_id']=threading.get_native_id()
                except BaseException as exc:launch_errors.append(exc)
                finally:launched.set()
                if self.process is not None:
                    while self.process.poll() is None:time.sleep(.02)
            self.process_owner_thread=threading.Thread(target=own_process,name='owned-gateway-creator',daemon=True)
            self.process_owner_thread.start()
            if not launched.wait(timeout=max(.001,min(10,self.deadline-time.monotonic()))):
                raise RuntimeError('owned Gateway creator did not report startup')
            if launch_errors:raise launch_errors[0]
            os.close(write_fd); write_fd = -1
            data = b""
            end = min(self.deadline, startup_deadline if startup_deadline is not None else time.monotonic() + 120)
            while time.monotonic() < end:
                if not select.select([read_fd], [], [], max(0, end - time.monotonic()))[0]:
                    break
                chunk = os.read(read_fd, 8192)
                if not chunk:
                    break
                data += chunk
                try:
                    info = json.loads(data)
                    break
                except json.JSONDecodeError:
                    pass
            else:
                raise RuntimeError("sandbox namespace handshake timed out")
            info = json.loads(data)
            identity = process_identity(int(info["child-pid"]))
            if (identity["net_inode"] != info["net-namespace"] or
                    identity["net_inode"] == os.stat("/proc/self/ns/net").st_ino or
                    identity["cgroup"] != Path("/proc/self/cgroup").read_text() or
                    not is_descendant(identity["pid"], self.process.pid)):
                raise RuntimeError("sandbox namespace ownership was not established")
            self.namespace = identity
            self.net_fd = os.open(f"/proc/{identity['pid']}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
            self.verify_namespace()
            self.attestation.update(valid=True, namespace=dict(identity), gateway_wrapper_pid=self.process.pid,
                                    network="fresh-loopback-plus-fixed-lower-UDS", cli_network="pinned-namespace-fd")
            self.attestation['native_fixture_roles'] = sorted(self.native_relays)
            self.attestation['native_fixture_credentials'] = 'role-placeholders-only'
            return self.process
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def verify_namespace(self):
        if self.process is None or self.process.poll() is not None or self.namespace is None or self.net_fd is None:
            raise RuntimeError("owned Gateway namespace is not live")
        actual = process_identity(self.namespace["pid"])
        if (actual != self.namespace or not is_descendant(actual["pid"], self.process.pid)
                or os.fstat(self.net_fd).st_ino != actual["net_inode"]):
            raise RuntimeError("owned Gateway namespace identity changed")

    def crash_gateway(self):
        """Crash this exact owned PID namespace, never the other Gateway.

        Pin and recheck the kernel identity before SIGKILL. Cleanup/relay
        shutdown is separate, so the acceptance observer can return promptly.
        """
        self.verify_namespace()
        identity = dict(self.namespace)
        descriptor = os.pidfd_open(identity['pid'])
        try:
            self.verify_namespace()
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            self.process.wait(timeout=max(.001,min(2,self.deadline-time.monotonic())))
        finally:
            os.close(descriptor)
        record={'event':'owned_gateway_crash','namespace':identity,
                'exit_code':self.process.returncode,'at_monotonic':time.monotonic()}
        self.attestation['deliberate_crash']=record
        return record

    def start_peer(self, command: list[str], log, *, gateway_id: str):
        """Second actual Gateway in the pinned netns, with the same state mount.

        No startup/seed/recovery operation is invented here. Each invocation
        has a fresh PID/mount namespace and an exact identity for fault control.
        """
        self.verify_namespace()
        if not gateway_id or gateway_id == self.gateway_id:
            raise ValueError('peer Gateway requires a distinct explicit identity')
        if any(p['gateway_id'] == gateway_id and p['process'].poll() is None for p in self.peers):
            raise ValueError('peer Gateway identity is already live')
        descriptor = os.dup(self.net_fd)
        read_fd, write_fd = os.pipe()
        process = None
        try:
            wrapped = ['/usr/bin/nsenter', f'--net=/proc/self/fd/{descriptor}', '--']
            wrapped += self.base_command(new_network=False, gateway_id=gateway_id)
            wrapped += ['--info-fd', str(write_fd), '--', *command]
            process = subprocess.Popen(wrapped, cwd=self.product, env={'PATH': '/usr/bin:/bin'},
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                pass_fds=(descriptor, write_fd))
            os.close(write_fd); write_fd = -1
            data = b''
            end = min(self.deadline, time.monotonic() + 10)
            while time.monotonic() < end:
                if not select.select([read_fd], [], [], max(0, end - time.monotonic()))[0]:
                    break
                chunk = os.read(read_fd, 8192)
                if not chunk:
                    break
                data += chunk
                try:
                    info = json.loads(data)
                    break
                except json.JSONDecodeError:
                    pass
            info = json.loads(data)
            identity = process_identity(int(info['child-pid']))
            if (identity['net_inode'] != self.namespace['net_inode'] or
                    identity['cgroup'] != self.namespace['cgroup'] or
                    not is_descendant(identity['pid'], process.pid)):
                raise RuntimeError('peer namespace ownership was not established')
            descriptor_pid = os.pidfd_open(identity['pid'])
            if process_identity(identity['pid']) != identity:
                os.close(descriptor_pid)
                raise RuntimeError('peer identity changed during pidfd pinning')
            peer = {'gateway_id': gateway_id, 'process': process, 'namespace': identity,
                    'pidfd': descriptor_pid, 'stopped': False}
            self.peers.append(peer)
            return peer
        except BaseException:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            raise
        finally:
            os.close(descriptor)
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def stop_peer(self, peer: dict, *, crash=False):
        if not any(item is peer for item in self.peers):
            raise ValueError('peer is not owned by this sandbox')
        if peer['stopped']:
            return
        # Signal a pinned PID namespace init, never a guessed/reused PID or
        # every process matching a name. Kernel tears down its descendants.
        try:
            signal.pidfd_send_signal(peer['pidfd'], signal.SIGKILL if crash else signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            peer['process'].wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                signal.pidfd_send_signal(peer['pidfd'], signal.SIGKILL)
            except ProcessLookupError:
                pass
            peer['process'].wait(timeout=1)
        os.close(peer['pidfd'])
        peer.update(stopped=True, pidfd=None, deliberate_crash=crash,
                    exit_code=peer['process'].returncode)

    @contextmanager
    def rpc_command(self, command: list[str]):
        self.verify_namespace()
        # Duplicate the verified FD, not a /proc/<pid> path susceptible to PID
        # reuse. nsenter only joins netns; CLI gets fresh PID/mount boundaries.
        descriptor = os.dup(self.net_fd)
        try:
            wrapped = ["/usr/bin/nsenter", f"--net=/proc/self/fd/{descriptor}", "--"]
            wrapped += self.base_command(new_network=False) + ["--", *command]
            yield wrapped, (descriptor,)
        finally:
            os.close(descriptor)

    def close(self, cleanup_seconds: float = 3):
        for peer in self.peers:
            self.stop_peer(peer)
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=max(0.001, cleanup_seconds))
            except subprocess.TimeoutExpired:
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=1)
        if self.process_owner_thread is not None:
            self.process_owner_thread.join(timeout=max(.001,min(1,cleanup_seconds)))
            self.attestation['process_creator_thread_reaped']=not self.process_owner_thread.is_alive()
            if self.process_owner_thread.is_alive():raise RuntimeError('owned Gateway creator still alive after process cleanup')
        if self.net_fd is not None:
            os.close(self.net_fd)
            self.net_fd = None
        self.relay.close()
        self.attestation['native_transports'] = {
            role: relay.close() for role, relay in self.native_relays.items()}
        self.attestation['peers'] = [{key: value for key, value in peer.items()
                                     if key not in {'process', 'pidfd'}} for peer in self.peers]
        self.attestation["transport"] = self.relay.snapshot()
        self.attestation["gateway_wrapper_reaped"] = self.process is None or self.process.poll() is not None
        # Descendant cleanup is independently guaranteed/checked by the
        # enclosing owned scope; a reaped wrapper alone is not that proof.
        self.attestation["scope_cleanup_required"] = True
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "sandbox-attestation.json").write_text(json.dumps(self.attestation, indent=2) + "\n")
        # LOWER_DISPATCH_RESERVE_SECONDS guard evidence, beside the trajectory: the
        # judge must be able to see that the evaluator's own budget guard stopped the
        # lower loop, rather than reading a bare missing artifact as Candidate silence.
        (self.output / "dispatch_guard.json").write_text(json.dumps(
            self.attestation["transport"].get("dispatch_guard", {}), indent=2) + "\n")
        return self.attestation
