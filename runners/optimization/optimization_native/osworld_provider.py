#!/usr/bin/env python3
"""Digest-pinned, labelled fresh-container OSWorld Docker provider."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image


PROVIDER_REPOSITORY = "happysixd/osworld-docker"
PROVIDER_DIGEST = "sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9"
PROVIDER_IMAGE = f"{PROVIDER_REPOSITORY}@{PROVIDER_DIGEST}"
# Release: pulled from Docker Hub by digest (the paper pulled the same digest through a registry
# mirror); configure a daemon registry mirror where Docker Hub is unreachable.
PROVIDER_PULL_IMAGE = os.environ.get(
    "AGENTSWE_OSWORLD_PROVIDER_IMAGE", f"docker.io/{PROVIDER_REPOSITORY}@{PROVIDER_DIGEST}")
AUDIT_LABEL = "agentswe.osworld.attempt"
PROVIDER_OVERLAY_ROOT = Path(os.environ.get("AGENTSWE_OSWORLD_OVERLAYS", str(
    Path(os.environ.get("AGENTSWE_OSWORLD_ASSETS", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe"))
                                                       / "assets" / "osworld"))) / "provider-overlays")))
SCREEN_SIZE = (1920, 1080)
READY_CONSECUTIVE_FRAMES = 3
READY_NONBLACK_SAMPLES = 100
STABLE_DESKTOP_TIMEOUT_SECONDS = 1200
PROVIDER_CPUSET = os.environ.get("OSWORLD_PROVIDER_CPUSET", "0-15,128-143")
PROVIDER_CPUSET_MEMS = os.environ.get("OSWORLD_PROVIDER_CPUSET_MEMS", "0")
PROVIDER_CPU_SHARES = int(os.environ.get("OSWORLD_PROVIDER_CPU_SHARES", "8192"))


def visible_desktop(payload: bytes) -> bool:
    """Reject boot-time all-black frames even when the endpoint already returns 200."""
    try:
        image = Image.open(io.BytesIO(payload)).convert("RGB")
    except Exception:
        return False
    if image.size != SCREEN_SIZE:
        return False
    sample = image.resize((120, 68))
    return sum(1 for pixel in sample.getdata() if max(pixel) > 12) > READY_NONBLACK_SAMPLES


def install_provider_override(
    run_token: str,
    kernel_path: str,
    initrd_path: str,
    entrypoint_path: str,
) -> type[Any]:
    """Install the evaluator-owned KVM UEFI provider before DesktopEnv."""
    if not run_token or len(run_token) > 48 or not run_token.replace("-", "").isalnum():
        raise ValueError("invalid OSWorld run token")
    boot_assets = [Path(kernel_path), Path(initrd_path), Path(entrypoint_path)]
    if not all(path.is_file() for path in boot_assets):
        raise ValueError("uefi_boot_asset_missing")

    from desktop_env.providers.docker.manager import DockerVMManager
    from desktop_env.providers.docker.provider import DockerProvider
    from filelock import FileLock
    import desktop_env.desktop_env as desktop_env_module

    class AuditedDockerProvider(DockerProvider):
        overlay_path: Path | None = None

        def _remove_overlay(self) -> None:
            if self.overlay_path is not None:
                self.overlay_path.unlink(missing_ok=True)
                self.overlay_path = None

        def _allocate_ports(self) -> tuple[int, int, int, int]:
            used = self._get_used_ports()
            allocated: list[int] = []
            for start in (8006, 5000, 9222, 8080):
                port = start
                while port in used or port in allocated:
                    port += 1
                if port >= 65354:
                    raise RuntimeError("provider_port_allocation_failed")
                allocated.append(port)
            return tuple(allocated)  # type: ignore[return-value]

        def _wait_for_stable_desktop(self, timeout: int = STABLE_DESKTOP_TIMEOUT_SECONDS) -> None:
            deadline = time.monotonic() + timeout
            consecutive = 0
            last_error = "endpoint_not_ready"
            kernel_lockup_seen = False
            while time.monotonic() < deadline:
                if self.container is None:
                    raise RuntimeError("provider_container_missing")
                self.container.reload()
                if self.container.status != "running":
                    raise RuntimeError(f"provider_container_{self.container.status}")
                serial_tail = self.container.logs(tail=200)
                if b"watchdog: BUG: soft lockup" in serial_tail or b"rcu_preempt kthread starved" in serial_tail:
                    kernel_lockup_seen = True
                try:
                    response = requests.get(
                        f"http://127.0.0.1:{self.server_port}/screenshot",
                        timeout=(5, 30),
                    )
                    if response.status_code == 200 and visible_desktop(response.content):
                        consecutive += 1
                        if consecutive >= READY_CONSECUTIVE_FRAMES:
                            size = requests.post(
                                f"http://127.0.0.1:{self.server_port}/screen_size",
                                json={}, timeout=(5, 30),
                            )
                            if size.status_code != 200 or "1920" not in size.text or "1080" not in size.text:
                                raise RuntimeError("screen_size_contract_failed")
                            return
                    else:
                        consecutive = 0
                        last_error = f"screenshot_http_{response.status_code}_or_nonvisible"
                except (requests.RequestException, RuntimeError) as exc:
                    consecutive = 0
                    last_error = type(exc).__name__
                time.sleep(5)
            raise TimeoutError(
                f"stable_desktop_timeout:{last_error}:kernel_lockup_seen={str(kernel_lockup_seen).lower()}"
            )

        def start_emulator(self, path_to_vm: str, headless: bool, os_type: str):
            lock = FileLock(str(self.lock_file), timeout=30)
            try:
                with lock:
                    PROVIDER_OVERLAY_ROOT.mkdir(parents=True, exist_ok=True)
                    self.overlay_path = PROVIDER_OVERLAY_ROOT / f"{run_token}.qcow2"
                    self.overlay_path.unlink(missing_ok=True)
                    self.overlay_path.touch(mode=0o600)
                    self.vnc_port, self.server_port, self.chromium_port, self.vlc_port = self._allocate_ports()
                    image = self.client.images.get(PROVIDER_PULL_IMAGE)
                    repo_digests = image.attrs.get("RepoDigests") or []
                    if not any(ref.endswith(f"@{PROVIDER_DIGEST}") for ref in repo_digests):
                        raise RuntimeError("provider_image_digest_unverified")
                    if not os.path.exists("/dev/kvm"):
                        raise RuntimeError("kvm_device_missing")
                    volumes = {
                        os.path.abspath(path_to_vm): {"bind": "/System.qcow2", "mode": "ro"},
                        str(boot_assets[0].resolve()): {"bind": "/boot-assets/vmlinuz", "mode": "ro"},
                        str(boot_assets[1].resolve()): {"bind": "/boot-assets/initrd", "mode": "ro"},
                        str(boot_assets[2].resolve()): {"bind": "/provider_entry.sh", "mode": "ro"},
                        str(self.overlay_path.resolve()): {"bind": "/boot.qcow2", "mode": "rw"},
                    }
                    self.container = self.client.containers.run(
                        PROVIDER_PULL_IMAGE,
                        name=f"agentswe-osworld-{run_token}",
                        entrypoint="/usr/bin/tini",
                        command=["-s", "/provider_entry.sh"],
                        environment={
                            "KVM": "Y", "BOOT_MODE": "uefi", "DISK_SIZE": "32G",
                            "RAM_SIZE": "4G", "CPU_CORES": "2",
                            "DNSMASQ_OPTS": "--no-resolv --server=1.1.1.1",
                        },
                        cap_add=["NET_ADMIN", "SYS_NICE"],
                        cpuset_cpus=PROVIDER_CPUSET,
                        cpuset_mems=PROVIDER_CPUSET_MEMS,
                        cpu_shares=PROVIDER_CPU_SHARES,
                        devices=[
                            "/dev/kvm:/dev/kvm:rwm",
                            "/dev/net/tun:/dev/net/tun:rwm",
                            "/dev/vhost-net:/dev/vhost-net:rwm",
                        ],
                        sysctls={"net.ipv4.ip_forward": "1"},
                        volumes=volumes,
                        ports={
                            "8006/tcp": ("127.0.0.1", self.vnc_port),
                            "5000/tcp": ("127.0.0.1", self.server_port),
                            "9222/tcp": ("127.0.0.1", self.chromium_port),
                            "8080/tcp": ("127.0.0.1", self.vlc_port),
                        },
                        labels={
                            AUDIT_LABEL: run_token,
                            "agentswe.osworld.role": "provider",
                            "agentswe.osworld.acceleration": "kvm-uefi",
                        },
                        detach=True,
                    )
                self._wait_for_stable_desktop()
            except Exception:
                if self.container:
                    try:
                        self.container.remove(force=True, v=True)
                    except Exception:
                        pass
                    self.container = None
                self._remove_overlay()
                raise

        def stop_emulator(self, path_to_vm: str, region=None, *args, **kwargs):
            if self.container:
                try:
                    self.container.remove(force=True, v=True)
                    time.sleep(1)
                finally:
                    self.container = None
                    self.server_port = None
                    self.vnc_port = None
                    self.chromium_port = None
                    self.vlc_port = None
                    self._remove_overlay()

    def factory(provider_name: str, region: str, use_proxy: bool = False):
        if provider_name != "docker":
            raise RuntimeError("OSWorld runtime allows only docker provider")
        return DockerVMManager(), AuditedDockerProvider(region)

    desktop_env_module.create_vm_manager_and_provider = factory
    return AuditedDockerProvider
