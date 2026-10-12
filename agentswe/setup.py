"""`agentswe setup <task>`: Harbor runtime, Docker CLI plugins, images and trusted environments.

Everything lands under AGENTSWE_HOME; nothing is installed system-wide and no existing Docker
image tag outside the agentswe-os/ namespace is touched. Every step is idempotent and recorded
in AGENTSWE_HOME/state/setup.json.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from . import util
from .config import REPO_ROOT, Config
from .doctor import docker_env, version_tuple
from .registry import Task

MICROMAMBA_VERSION = "2.3.2"
HARBOR_VERSION = "0.20.0"
HARBOR_PYTHON = "3.12"
# Docker CLI plugins, used only when the host's own are missing or too old. Official static
# binaries first; Ubuntu packages (extracted with dpkg-deb, never installed) as the fallback.
PLUGINS = {
    "docker-compose": {
        "github": "https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64",
        "deb": "pool/universe/d/docker-compose-v2/docker-compose-v2_2.40.3%2Bds1-0ubuntu1~22.04.1_amd64.deb",
        "deb_member": "usr/libexec/docker/cli-plugins/docker-compose"},
    "docker-buildx": {
        "github": "https://github.com/docker/buildx/releases/download/v0.30.1/buildx-v0.30.1.linux-amd64",
        "deb": "pool/universe/d/docker-buildx/docker-buildx_0.30.1-0ubuntu1~22.04.1_amd64.deb",
        "deb_member": "usr/libexec/docker/cli-plugins/docker-buildx"},
}


# setup.json sections whose entries hold further keyed records, merged one level deeper (harbor: profiles/<profile>)
NESTED_STATE_SECTIONS = {"harbor": 1}


def _merge_changes(current, base, ours, depth: int):
    """`current` with this process's changes (`base` -> `ours`) applied, per key down to `depth` levels of dicts:
    keys another process added or changed since `base` survive; below `depth` a changed value replaces the whole."""
    if depth <= 0 or not all(isinstance(v, dict) for v in (current, base, ours)):
        return copy.deepcopy(ours)
    merged = dict(current)
    for key in [*base, *(k for k in ours if k not in base)]:
        if key not in ours:
            merged.pop(key, None)  # removed here (for example a task whose tree was re-rendered)
        elif key not in base:
            # new here: a new record replaces, a new section is merged with the one another process may have added
            merged[key] = _merge_changes(current[key], {}, ours[key], depth - 1) if key in current else \
                copy.deepcopy(ours[key])
        elif ours[key] != base[key]:
            merged[key] = _merge_changes(current.get(key, base[key]), base[key], ours[key], depth - 1)
    return merged


class Setup:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.home = cfg.home
        self.state_path = self.home / "state" / "setup.json"
        self.state = util.read_json(self.state_path, {}) or {}
        self._base = copy.deepcopy(self.state)  # what this process's changes are relative to
        for d in ("tools", "harbor", "envs", "runs", "jobs", "tmp", "network_allocations", "broker_ledgers", "cache"):
            (self.home / d).mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        """Write setup.json without losing another `setup` in the same home: under an exclusive lock next to it, re-read
        the file and apply only this process's changes since it last read or saved, per top-level section and per
        entry (harbor per profile). An unreadable file is replaced by this process's view, as before."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_path.with_name(self.state_path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = util.read_json(self.state_path, None) if self.state_path.exists() else {}
            if not isinstance(current, dict):
                current = copy.deepcopy(self._base)
            merged = dict(current)
            for key in [*self._base, *(k for k in self.state if k not in self._base)]:
                if key not in self.state:
                    merged.pop(key, None)
                elif self.state[key] != self._base.get(key, {}) or key not in self._base:
                    # a section: merged per entry (harbor per profile), never replaced whole
                    merged[key] = _merge_changes(current.get(key, self._base.get(key, {})), self._base.get(key, {}),
                                                 self.state[key], 1 + NESTED_STATE_SECTIONS.get(key, 0))
            util.write_json(self.state_path, merged)
        finally:
            os.close(fd)  # releases the lock
        # this process now sees the others' entries too; the top-level object stays the same for callers holding it
        self.state.clear()
        self.state.update(copy.deepcopy(merged))
        self._base = copy.deepcopy(merged)

    # ---------------------------------------------------------------- micromamba
    @property
    def micromamba(self) -> Path:
        return self.home / "tools" / "micromamba"

    def ensure_micromamba(self) -> None:
        if self.micromamba.exists():
            return
        channel = self.cfg.get("AGENTSWE_CONDA_CHANNEL").rstrip("/")
        url = f"{channel}/linux-64/micromamba-{MICROMAMBA_VERSION}-0.tar.bz2"
        util.log(f"downloading micromamba {MICROMAMBA_VERSION}")
        archive = util.download(url, self.home / "cache" / f"micromamba-{MICROMAMBA_VERSION}.tar.bz2")
        util.extract_member(archive, "bin/micromamba", self.micromamba)

    # ---------------------------------------------------------------- Harbor
    def ensure_harbor_python(self) -> Path:
        py = self.home / "harbor" / "python"
        if not (py / "bin" / "python3").exists():
            self.ensure_micromamba()
            util.log(f"creating Python {HARBOR_PYTHON} for Harbor")
            channel = self.cfg.get("AGENTSWE_CONDA_CHANNEL")
            util.run([str(self.micromamba), "create", "-y", "-q", "-p", str(py), "-r", str(self.home / "tools" / "mamba-root"),
                      "--override-channels", "-c", channel, f"python={HARBOR_PYTHON}", "pip"], capture=False)
        return py

    def ensure_harbor(self, profile: str = "creation") -> None:
        """harbor==0.20.0 with the paper's dependency set and one patch profile (third_party/harbor).

        Two environments reproduce the paper exactly: `creation` (Creation tasks) and `site` (Editing,
        Optimization); their codex.py differs. Each venv gets its own `bin/harbor[-<profile>]` wrapper.
        """
        h = self.home / "harbor"
        venv = h / f"venv-{profile}"
        tp = REPO_ROOT / "third_party" / "harbor"
        py = self.ensure_harbor_python()
        if not (venv / "bin" / "harbor").exists():
            util.log(f"installing harbor=={HARBOR_VERSION} ({profile}) with the pinned dependency set")
            util.run([str(py / "bin" / "python3"), "-m", "venv", str(venv)], capture=False)
            util.run([str(venv / "bin" / "pip"), "install", "-q", "--index-url", self.cfg.get("AGENTSWE_PIP_INDEX_URL"),
                      "-c", str(tp / "constraints-0.20.0.txt"), f"harbor=={HARBOR_VERSION}"], capture=False)
        receipt = h / f"receipt-{profile}.json"
        util.run([str(venv / "bin" / "python"), "-B", str(tp / "harbor_patch.py"), "apply", "--venv", str(venv),
                  "--profile", profile, "--receipt", str(receipt)], capture=False)
        (h / "bin").mkdir(parents=True, exist_ok=True)
        wrapper = h / "bin" / ("harbor" if profile == "creation" else f"harbor-{profile}")
        wrapper.write_text(f'#!/usr/bin/env bash\nexec "{venv}/bin/harbor" "$@"\n')
        wrapper.chmod(0o755)
        if profile != "creation":
            root = h / "roots" / profile
            (root / "bin").mkdir(parents=True, exist_ok=True)
            (root / "bin" / "harbor").write_text(f'#!/usr/bin/env bash\nexec "{venv}/bin/harbor" "$@"\n')
            (root / "bin" / "harbor").chmod(0o755)
            for shared in ("docker-config", "builder-base-image"):
                link = root / shared
                if not link.is_symlink():
                    link.symlink_to(h / shared)
        info = util.read_json(receipt, {}) or {}
        self.state.setdefault("harbor", {})
        if not isinstance(self.state["harbor"].get("profiles"), dict):
            self.state["harbor"] = {"profiles": {}}
        self.state["harbor"]["profiles"][profile] = {
            "version": HARBOR_VERSION, "venv": str(venv), "wrapper": str(wrapper),
            "tree_sha256": info.get("tree_sha256"), "receipt": str(receipt), "at": util.now()}
        self.save()

    # ---------------------------------------------------------------- Docker CLI plugins
    def ensure_docker_plugins(self) -> None:
        cfgdir = self.home / "harbor" / "docker-config"
        plugdir = cfgdir / "cli-plugins"
        plugdir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = str(cfgdir)
        need = []
        if version_tuple(util.out(["docker", "compose", "version", "--short"], env=env)) < (2, 20, 0):
            need.append("docker-compose")
        if "github.com/docker/buildx" not in util.out(["docker", "buildx", "version"], env=env):
            need.append("docker-buildx")
        source = (self.cfg.get("AGENTSWE_DOCKER_PLUGIN_SOURCE") or "auto").lower()  # auto | github | ubuntu
        for name in need:
            spec, dest = PLUGINS[name], plugdir / name
            try:
                if source == "ubuntu":
                    raise RuntimeError("ubuntu package source selected")
                util.log(f"downloading {name} (official release)")
                util.download(spec["github"], dest, timeout=30, attempts=1,
                              deadline=None if source == "github" else 120)
            except RuntimeError:
                if not shutil.which("dpkg-deb"):
                    raise SystemExit(f"cannot fetch {name}: GitHub unreachable and dpkg-deb unavailable")
                mirror = self.cfg.get("AGENTSWE_UBUNTU_MIRROR").rstrip("/")
                util.log(f"downloading {name} (Ubuntu package from {urllib.parse.urlsplit(mirror).hostname})")
                deb = util.download(f"{mirror}/{spec['deb']}", self.home / "cache" / Path(spec["deb"]).name)
                tmp = self.home / "cache" / f"{name}-deb"
                shutil.rmtree(tmp, ignore_errors=True)
                util.run(["dpkg-deb", "-x", str(deb), str(tmp)])
                shutil.copyfile(tmp / spec["deb_member"], dest)
                shutil.rmtree(tmp)
            dest.chmod(0o755)
        self.state["docker_plugins"] = {
            "compose": util.out(["docker", "compose", "version", "--short"], env=env),
            "buildx": util.out(["docker", "buildx", "version"], env=env).split("\n")[0], "at": util.now()}
        self.save()

    # ---------------------------------------------------------------- images
    def images(self) -> dict:
        return json.loads((REPO_ROOT / "images" / "images.json").read_text())

    def build_args(self, spec: dict, use_apt_mirror: bool) -> list[str]:
        args = []
        for key, value in spec.get("build_args", {}).items():  # fixed, task-specific variants of a shared context
            args += ["--build-arg", f"{key}={value}"]
        for key, cfg_key in spec.get("build_args_from_config", {}).items():
            if key == "APT_MIRROR" and not use_apt_mirror:
                continue  # first attempt: the Dockerfile's pinned apt snapshot
            value = self.cfg.get(cfg_key)
            if key == "CONDA_FORGE_URL" and not value:
                value = self.cfg.get("AGENTSWE_CONDA_CHANNEL")
            if value:
                args += ["--build-arg", f"{key}={value}"]
        return args

    def build_with_apt_fallback(self, name: str, spec: dict, cmd_for) -> str:
        """Build with the pinned apt snapshot; if that fails and AGENTSWE_APT_MIRROR is set, retry on the
        mirror (package versions may then differ from the paper images; recorded in setup state)."""
        env = docker_env(self.cfg)
        r = util.run(cmd_for(self.build_args(spec, use_apt_mirror=False)), env=env, check=False, capture=False)
        if r.returncode == 0:
            return "snapshot" if "APT_MIRROR" in spec.get("build_args_from_config", {}) else "n/a"
        if "APT_MIRROR" in spec.get("build_args_from_config", {}) and self.cfg.get("AGENTSWE_APT_MIRROR"):
            util.log(f"{name}: build with the apt snapshot failed; retrying on AGENTSWE_APT_MIRROR (versions may drift)")
            util.run(cmd_for(self.build_args(spec, use_apt_mirror=True)), env=env, capture=False)
            return "mirror"
        raise SystemExit(f"image build failed: {name}")

    def ensure_image(self, name: str, _seen: set | None = None) -> None:
        seen = _seen if _seen is not None else set()
        if name in seen:
            return
        seen.add(name)
        spec = self.images()["images"][name]
        for dep in spec.get("depends_on", []):
            self.ensure_image(dep, seen)
        env = docker_env(self.cfg)
        kind = spec.get("kind")
        if kind == "pull":
            ref = spec["ref"]
            if not util.out(["docker", "image", "inspect", ref, "--format", "{{.Id}}"], env=env).startswith("sha256:"):
                util.log(f"pulling {ref}")
                util.run(["docker", "pull", "-q", ref], env=env, capture=False)
            return
        context = REPO_ROOT / spec["context"]
        digest = tree_digest(context)
        if spec.get("downloads"):  # pinned inputs are part of what the export is built from
            digest = hashlib.sha256((digest + json.dumps(spec["downloads"], sort_keys=True)).encode()).hexdigest()
        record = self.state.get("images", {}).get(name, {})
        if kind == "export":
            exports = {k: Path(v.replace("$AGENTSWE_HOME", str(self.home))) for k, v in spec["exports"].items()}
            if record.get("context_sha256") == digest and all(p.is_dir() for p in exports.values()):
                return
            out = self.home / "cache" / f"export-{name}"
            shutil.rmtree(out, ignore_errors=True)
            util.log(f"building {name} (exported to {', '.join(str(p) for p in exports.values())})")
            # images.json "downloads": fetched on the host like env.json downloads, visible to the build as downloads/
            build_context, downloads = self.spec_context_with_downloads(context, spec)
            try:
                apt = self.build_with_apt_fallback(name, spec, lambda args: [
                    "docker", "buildx", "build", "--progress", "plain", *args,
                    "--output", f"type=local,dest={out}", str(build_context)])
            finally:
                if build_context != context:
                    shutil.rmtree(build_context, ignore_errors=True)
            for key, dest in exports.items():
                shutil.rmtree(dest, ignore_errors=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(out / key), dest)
            shutil.rmtree(out, ignore_errors=True)
            self.state.setdefault("images", {})[name] = {"kind": "export", "context_sha256": digest, "apt": apt,
                                                         "exports": {k: str(v) for k, v in exports.items()},
                                                         "downloads": downloads, "at": util.now()}
            self.save()
            return
        tag = spec["tag"]
        label = util.out(["docker", "image", "inspect", tag, "--format",
                          '{{index .Config.Labels "io.agentswe.context-sha256"}}'], env=env)
        apt = record.get("apt")
        if label != digest:
            util.log(f"building {tag}")
            apt = self.build_with_apt_fallback(name, spec, lambda args: [
                "docker", "buildx", "build", "--progress", "plain", "--load", "-t", tag,
                "--label", f"io.agentswe.context-sha256={digest}", *args, str(context)])
        image_id = util.out(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], env=env)
        if name == "builder-codex":
            self.write_builder_manifest(tag, image_id, spec)
        self.state.setdefault("images", {})[name] = {"tag": tag, "id": image_id, "context_sha256": digest, "apt": apt,
                                                     "at": util.now()}
        self.save()

    def write_builder_manifest(self, tag: str, image_id: str, spec: dict) -> None:
        """The Creation controller refuses a Builder image without this verified lock."""
        env = docker_env(self.cfg)
        codex = util.out(["docker", "run", "--rm", "--network", "none", tag, "codex", "--version"], env=env)
        node = util.out(["docker", "run", "--rm", "--network", "none", tag, "node", "--version"], env=env)
        history = util.out(["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", tag], env=env)
        codex_version = spec.get("codex_version", "0.144.1")
        node_version = spec.get("node_version", "24.6.0")
        if codex != f"codex-cli {codex_version}" or node != f"v{node_version}":
            raise SystemExit(f"builder image verification failed: {codex!r}, {node!r}")
        if any(k in history for k in ("API_KEY=", "auth.json", "Bearer ")):
            raise SystemExit("builder image history mentions credential material")
        util.write_json(self.home / "harbor" / "builder-base-image" / "image_manifest.json", {
            "schema_version": "1.0", "status": "verified", "verified_at": util.now(), "image": tag, "image_id": image_id,
            "codex_version": codex_version, "node_version": node_version,
            "networkless_runtime_verify": True, "credential_material_in_image_history": False,
            "auth_values_recorded": False})

    # ---------------------------------------------------------------- trusted environments
    @staticmethod
    def find_env_spec(name: str) -> tuple[Path, dict]:
        for path in sorted(REPO_ROOT.glob("tasks/*/*/env/env.json")):
            meta = json.loads(path.read_text())
            if name == meta.get("name") or name in meta.get("aliases", []):
                return path.parent, meta
        raise SystemExit(f"no environment spec named {name!r} under tasks/*/*/env")

    def ensure_env(self, env_name: str) -> None:
        spec_dir, meta = self.find_env_spec(env_name)
        name = meta["name"]
        if meta.get("complete") is False:
            raise SystemExit(f"{name}: environment spec is incomplete ({meta.get('notes', '')})")
        if meta.get("build_on") == "host":
            return  # built by the task runner's setup hook (e.g. bubblewrap venvs used from host paths)
        if meta.get("kind") not in ("conda", "venv", "image"):
            raise SystemExit(f"{name}: environment kind {meta.get('kind')!r} is built by its task runner, not by setup")
        digest = tree_digest(spec_dir)
        target = self.home / "envs" / name
        record = self.state.get("envs", {}).get(name)
        if record and record.get("spec_digest") == digest and target.is_dir():
            open_root(target)
            self.link_aliases(name, meta)
            return
        if meta["kind"] == "image":
            self.ensure_image_env(spec_dir, meta, target, digest)
            return
        builder = meta.get("builder_image", "env-builder")
        self.ensure_image(builder)
        builder_tag = self.images()["images"][builder]["tag"]
        out = self.home / "cache" / f"envbuild-{name}"
        shutil.rmtree(out, ignore_errors=True)
        args = ["--build-arg", f"ENV_BUILDER={builder_tag}", "--build-arg", f"ENV_NAME={name}"]
        for arg, key in (("CONDA_FORGE_URL", "AGENTSWE_CONDA_CHANNEL"), ("PIP_INDEX_URL", "AGENTSWE_PIP_INDEX_URL"),
                         ("NPM_REGISTRY", "AGENTSWE_NPM_REGISTRY"), ("PLAYWRIGHT_DOWNLOAD_HOST", "AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST"),
                         ("UBUNTU_ARCHIVE_URL", "AGENTSWE_UBUNTU_MIRROR")):
            if self.cfg.get(key):
                args += ["--build-arg", f"{arg}={self.cfg.get(key)}"]
        args += self.build_proxy_args()
        context, downloads = self.spec_context_with_downloads(spec_dir, meta)
        for item in downloads:
            if item.get("build_arg"):
                args += ["--build-arg", f"{item['build_arg']}=file:///spec/downloads/{item['name']}"]
        util.log(f"building environment {name} at {meta.get('mount')} (exported to {target})")
        try:
            util.run(["docker", "buildx", "build", "--progress", "plain", "-f", str(REPO_ROOT / "images" / "env-builder" / "env.Dockerfile"),
                      *args, "--build-context", f"spec={context}", "--output", f"type=local,dest={out}",
                      str(REPO_ROOT / "images" / "env-builder")], env=docker_env(self.cfg), capture=False)
        finally:
            if context != spec_dir:
                shutil.rmtree(context, ignore_errors=True)
        if target.exists() or target.is_symlink():
            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
        shutil.move(str(out), target)
        open_root(target)
        self.link_aliases(name, meta)
        self.state.setdefault("envs", {})[name] = {"mount": meta.get("mount"), "spec_digest": digest, "at": util.now(),
                                                    "downloads": downloads}
        self.save()

    def spec_context_with_downloads(self, spec_dir: Path, meta: dict) -> tuple[Path, list[dict]]:
        """Large release assets named in env.json "downloads" are fetched on the host (resumable, through
        AGENTSWE_BUILD_PROXY when set) and handed to the build as files under /spec/downloads; each one's build arg
        (for example LEAN_RELEASE_URL) then points at that file. A listed sha256 is enforced; the sha of every
        fetched file is recorded in the setup state either way."""
        items = meta.get("downloads") or []
        if not items:
            return spec_dir, []
        cache = self.home / "cache" / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        proxy = self.cfg.get("AGENTSWE_BUILD_PROXY")
        if proxy:
            env.update({"https_proxy": proxy, "http_proxy": proxy, "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy})
        records = []
        for item in items:
            path = cache / item["name"]
            want = item.get("sha256")
            if not (path.is_file() and (not want or util.sha256_file(path) == want)):
                util.log(f"downloading {item['url']} (resumable)")
                partial = path.with_suffix(path.suffix + ".part")
                for attempt in range(1, 31):
                    done = subprocess.run(["curl", "-fL", "-C", "-", "--retry", "5", "--retry-all-errors",
                                           "--connect-timeout", "30", "-sS", "-o", str(partial), item["url"]],
                                          env=env, check=False)
                    if done.returncode == 0:
                        break
                    if done.returncode not in (18, 28, 56, 92):  # partial file / timeout / recv failure / HTTP2 reset
                        raise SystemExit(f"download failed ({done.returncode}): {item['url']}")
                else:
                    raise SystemExit(f"download did not complete after 30 resumptions: {item['url']}")
                partial.rename(path)
            sha = util.sha256_file(path)
            if want and sha != want:
                raise SystemExit(f"{item['name']}: sha256 {sha} != pinned {want}")
            util.log(f"{item['name']}: sha256 {sha}" + ("" if want else " (not pinned in env.json yet)"))
            records.append({"name": item["name"], "url": item["url"], "build_arg": item.get("build_arg"), "sha256": sha,
                            "pinned": bool(want)})
        context = Path(tempfile.mkdtemp(prefix="envspec-", dir=self.home / "cache"))
        shutil.copytree(spec_dir, context, dirs_exist_ok=True)
        (context / "downloads").mkdir()
        for record in records:
            os.link(cache / record["name"], context / "downloads" / record["name"])
        return context, records

    def build_proxy_args(self) -> list[str]:
        """AGENTSWE_BUILD_PROXY: an HTTP(S) proxy for downloads made inside environment builds (for example the
        Lean toolchain from GitHub on hosts where direct access is slow). Every download there is checksum-verified,
        so the proxy cannot change content. A proxy on the host's loopback needs the build on the host network."""
        proxy = self.cfg.get("AGENTSWE_BUILD_PROXY")
        if not proxy:
            return []
        no_proxy = self.cfg.get("AGENTSWE_BUILD_NO_PROXY") or "localhost,127.0.0.1"
        args = ["--network", "host"]
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            args += ["--build-arg", f"{name}={proxy}"]
        for name in ("no_proxy", "NO_PROXY"):
            args += ["--build-arg", f"{name}={no_proxy}"]
        return args

    def ensure_image_env(self, spec_dir: Path, meta: dict, target: Path, digest: str) -> None:
        """A file subset of a built image, exported without a container and checked against its manifest."""
        name = meta["name"]
        self.ensure_image(meta["image"])
        source_tag = self.images()["images"][meta["image"]]["tag"]
        out = self.home / "cache" / f"envbuild-{name}"
        shutil.rmtree(out, ignore_errors=True)
        util.log(f"exporting environment {name} from {source_tag} (to {target})")
        util.run(["docker", "buildx", "build", "--progress", "plain", "-f", str(spec_dir / meta["dockerfile"]),
                  "--build-arg", f"{meta['image_build_arg']}={source_tag}", "--output", f"type=local,dest={out}",
                  str(spec_dir)], env=docker_env(self.cfg), capture=False)
        if meta.get("manifest_sha256") and util.sha256_file(out / "manifest.json") != meta["manifest_sha256"]:
            raise SystemExit(f"{name}: exported manifest does not match manifest_sha256")
        if target.exists():
            for p in target.rglob("*"):  # exported files are read-only; make the old copy removable
                if p.is_dir():
                    p.chmod(0o755)
            shutil.rmtree(target)
        shutil.move(str(out), target)
        open_root(target)
        self.state.setdefault("envs", {})[name] = {"kind": "image", "source": source_tag, "spec_digest": digest, "at": util.now()}
        self.save()

    def link_aliases(self, name: str, meta: dict) -> None:
        for alias in meta.get("aliases", []):
            link = self.home / "envs" / alias
            if link.is_symlink() and link.readlink() == Path(name):
                continue
            if link.exists() or link.is_symlink():
                shutil.rmtree(link) if link.is_dir() and not link.is_symlink() else link.unlink()
            link.symlink_to(name)


def open_root(path: Path) -> None:
    """Release fix: a BuildKit local export lands with a 0700 root, so an environment moved into
    place was unreadable to the non-root Candidate (uid 65534); the paper environments' roots are 0755. Only the root
    directory changes; everything inside keeps the exported modes. Idempotent, and applied to installs set up before
    this fix on their next setup."""
    if path.is_dir() and not path.is_symlink() and path.stat().st_mode & 0o7777 != 0o755:
        path.chmod(0o755)


def export_from_image(s: "Setup", image_name: str, path: str, dest: Path) -> None:
    spec = s.images()["images"][image_name]
    tag = spec["tag"]
    image_id = util.out(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], env=docker_env(s.cfg))
    key = f"{image_name}:{path}"
    record = s.state.get("host_exports", {}).get(key)
    if record and record.get("image_id") == image_id and dest.is_dir():
        open_root(dest)
        return
    ctx = s.home / "cache" / "export-ctx"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "Dockerfile").write_text(f"FROM {tag} AS src\nFROM scratch\nCOPY --from=src {path} /\n")
    out = s.home / "cache" / ("hostexport-" + dest.name)
    shutil.rmtree(out, ignore_errors=True)
    util.log(f"exporting {path} from {tag} to {dest}")
    util.run(["docker", "buildx", "build", "--progress", "plain", "--output", f"type=local,dest={out}", str(ctx)],
             env=docker_env(s.cfg), capture=False)
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(out), dest)
    open_root(dest)
    s.state.setdefault("host_exports", {})[key] = {"image_id": image_id, "dest": str(dest), "at": util.now()}
    s.save()


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + util.sha256_file(p).encode() + b"\n")
    return h.hexdigest()


SETUP_RUN_LOCK = "setup.run.lock"


@contextlib.contextmanager
def setup_run_lock(home: Path):
    """One `agentswe setup` at a time per AGENTSWE_HOME: setups share export staging (cache/export-*), environment and
    deps directories and the rendered Editing control plane, so a second one waits for the first."""
    path = home / "state" / SETUP_RUN_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            util.log(f"another setup is running in {home}; waiting")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


def setup_task(cfg: Config, task: Task) -> None:
    if task.data.get("status") == "planned":
        raise SystemExit(f"{task.label} is planned but not extracted yet")
    with setup_run_lock(cfg.home):
        _setup_task(cfg, task)


def _setup_task(cfg: Config, task: Task) -> None:
    s = Setup(cfg)  # reads setup.json under the lock, after any setup that ran before
    s.ensure_docker_plugins()
    s.ensure_harbor(task.data.get("runner_config", {}).get("harbor_profile", "creation"))
    for image in task.data.get("images", []):
        s.ensure_image(image)
    for env_name in task.data.get("envs", []):
        s.ensure_env(env_name)
    for item in task.data.get("runner_config", {}).get("host_exports", []):
        export_from_image(s, item["image"], item["path"], cfg.home / item["dest"])
    from . import runners
    module = runners.load(task.runner)
    if hasattr(module, "setup"):
        module.setup(cfg, task, s)
    s.state.setdefault("tasks", {})[task.id] = {"at": util.now()}
    s.save()
    util.log(f"setup complete for {task.label}")
