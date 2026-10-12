"""Evaluator-owned assets for native Optimization controllers (TerminalBench, OSWorld).

task.json `runner_config` may declare:

* `assets`: {"root": "<dir under AGENTSWE_HOME>", "files": [{"path", "sha256", "size", "urls": [...],
  "release_path", "extract"}]}. Each file is fetched once into AGENTSWE_HOME/cache/downloads (named by
  its sha256; resumable, through AGENTSWE_BUILD_PROXY when set; size and sha256 enforced) and
  hard-linked (or copied) to <root>/<path>. `urls` are upstream locations that serve the identical
  bytes; `release_path` names the file in the AgentSWE release-asset store, for files that exist only
  there: AGENTSWE_RELEASE_ASSETS_URL + "/" + release_path in a tree store, + "/" + release_asset_name()
  in a flat one such as a GitHub release (release_layout). A file already in the download cache with
  the pinned sha256 is used as is, so an operator can seed the cache by hand. Optional `extract`:
  {"kind": "zip", "member", "dest", "sha256", "size"} unpacks one member, or
  {"kind": "tar", "dest", "tree_sha256"} unpacks a tar into <root>/<dest>; both are verified.
* `wheels`: {"root": "<dir>", "sums": "<repo-relative SHA256SUMS>"}: each "<sha256>  <abi>/<wheel>" line
  is fetched from the configured Python package index (AGENTSWE_PIP_INDEX_URL, PEP 503 simple API) and
  placed at <root>/<abi>/<wheel> after the sha256 check.
* `pull_images`: [{"ref": "<repository>@sha256:<index digest>", "image": "<optional tag the controller uses>"}]:
  pulled by digest (and tagged when `image` is given), so the controller's checks see upstream bytes;
  with "layers" (the rootfs diff_ids) an image already present with that content is not pulled again,
  so a host without registry access can `docker load` a saved copy instead.
* `uv_venv`: {"dest", "project", "lock_sha256", "python", "uv", "editable": [...]}: a Python environment
  with exactly the locked distributions: the project's uv.lock (sha256 pinned) is exported with hashes
  and installed with --require-hashes from the configured index, then the listed local packages are
  installed editable without dependencies, as `uv sync --frozen --no-dev` lays them out.
"""
from __future__ import annotations

import hashlib
import html.parser
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.parse
import zipfile
from pathlib import Path

from .. import util
from ..config import REPO_ROOT, Config

RESUMABLE_CURL_CODES = (18, 28, 56, 92)  # partial file / timeout / recv failure / HTTP2 reset
UNREACHABLE_CURL_CODES = (28,)  # a timeout before any byte arrived means the host cannot reach the source


def _curl_env(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    proxy = cfg.get("AGENTSWE_BUILD_PROXY")
    if proxy:
        env.update({"https_proxy": proxy, "http_proxy": proxy, "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy})
    return env


def _fetch(cfg: Config, url: str, dest: Path) -> bool:
    partial = dest.with_name(dest.name + ".part")
    env = _curl_env(cfg)
    for _ in range(30):
        done = subprocess.run(["curl", "-fL", "-C", "-", "--retry", "5", "--retry-all-errors",
                               "--connect-timeout", "30", "-sS", "-o", str(partial), url], env=env, check=False)
        if done.returncode == 0:
            partial.rename(dest)
            return True
        if done.returncode not in RESUMABLE_CURL_CODES:
            util.log(f"download failed ({done.returncode}): {url}")
            partial.unlink(missing_ok=True)
            return False
        if done.returncode in UNREACHABLE_CURL_CODES and not (partial.is_file() and partial.stat().st_size):
            # nothing arrived: the source is unreachable from this host; try the next one instead of resuming
            util.log(f"no bytes from {url} (curl {done.returncode}); trying the next source")
            partial.unlink(missing_ok=True)
            return False
    util.log(f"download did not complete after 30 resumptions: {url}")
    return False


def cache_name(sha256: str, basename: str) -> str:
    return f"{sha256[:16]}__{basename}"


RELEASE_LAYOUTS = ("tree", "flat")
GITHUB_RELEASE_DOWNLOAD = re.compile(r"/releases/(?:latest/)?download(?:/|$)")
UNSAFE_ASSET_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def release_asset_name(sha256: str, basename: str) -> str:
    """A pinned file's name in a flat release-asset store: its download-cache name with every character outside
    [A-Za-z0-9._-] replaced by "_". A GitHub release has no directories and rewrites other characters in asset names
    (six OSWorld task files have spaces), so the store publishes each file under this name."""
    return cache_name(sha256, UNSAFE_ASSET_CHARS.sub("_", basename))


def release_layout(cfg: Config) -> str:
    """tree: AGENTSWE_RELEASE_ASSETS_URL/<release path> (a directory tree on a web server or bucket); flat: every file
    at AGENTSWE_RELEASE_ASSETS_URL/<release_asset_name> (a GitHub release). AGENTSWE_RELEASE_ASSETS_LAYOUT sets it;
    otherwise a GitHub release download URL (.../releases/download/<tag>) is flat and any other URL is tree."""
    explicit = (cfg.get("AGENTSWE_RELEASE_ASSETS_LAYOUT") or "").strip().lower()
    if explicit:
        if explicit not in RELEASE_LAYOUTS:
            raise SystemExit(f"AGENTSWE_RELEASE_ASSETS_LAYOUT={explicit!r}: expected one of {', '.join(RELEASE_LAYOUTS)}")
        return explicit
    return "flat" if GITHUB_RELEASE_DOWNLOAD.search(cfg.get("AGENTSWE_RELEASE_ASSETS_URL") or "") else "tree"


def release_asset_url(cfg: Config, release_path: str, sha256: str) -> str | None:
    """Where the release-asset store serves a pinned file, or None when AGENTSWE_RELEASE_ASSETS_URL is not set.
    Percent-encoded: some OSWorld release paths contain spaces."""
    base = (cfg.get("AGENTSWE_RELEASE_ASSETS_URL") or "").rstrip("/")
    if not base:
        return None
    if release_layout(cfg) == "flat":
        return f"{base}/{urllib.parse.quote(release_asset_name(sha256, release_path.rsplit('/', 1)[-1]))}"
    return f"{base}/{urllib.parse.quote(release_path)}"


def _cached(cfg: Config, basename: str, sha256: str, urls: list[str], size: int | None = None) -> Path:
    cache = cfg.home / "cache" / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / cache_name(sha256, basename)
    if path.is_file() and (size is None or path.stat().st_size == size) and util.sha256_file(path) == sha256:
        return path
    for url in urls:
        util.log(f"downloading {url} (resumable)")
        if _fetch(cfg, url, path):
            actual = util.sha256_file(path)
            if actual == sha256 and (size is None or path.stat().st_size == size):
                return path
            util.log(f"{basename}: sha256 {actual} != pinned {sha256}; trying the next source")
            path.unlink(missing_ok=True)
    raise SystemExit(
        f"{basename}: no source delivered the pinned sha256 {sha256}. Tried: {urls or ['(none)']}. "
        f"Set AGENTSWE_RELEASE_ASSETS_URL, or place the file at {path} by hand.")


def _place(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == source.stat().st_size and os.path.samefile(dest, source):
        return
    if dest.is_file() and dest.stat().st_size == source.stat().st_size and util.sha256_file(dest) == util.sha256_file(source):
        return
    dest.unlink(missing_ok=True)
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


# An editable install (uv_venv "editable") writes <name>.egg-info into the project tree it installs from; that is
# build metadata, not part of the pinned tree.
INSTALL_METADATA_SUFFIXES = (".egg-info",)


def tree_digest(root: Path, ignore_install_metadata: bool = False) -> str:
    """Files and symlinks under root (no __pycache__), by relative path and content; with ignore_install_metadata, also
    without editable-install metadata (INSTALL_METADATA_SUFFIXES)."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if "__pycache__" in path.parts:
            continue
        if ignore_install_metadata and any(part.endswith(INSTALL_METADATA_SUFFIXES)
                                           for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            h.update(b"L" + rel + b"\0" + os.readlink(path).encode() + b"\n")
        elif path.is_file():
            h.update(b"F" + rel + b"\0" + util.sha256_file(path).encode() + b"\n")
    return h.hexdigest()


def _marker_ok(marker: Path, target: Path, want: str) -> bool:
    try:
        rec = json.loads(marker.read_text())
        st = target.stat()
        return rec.get("digest") == want and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns
    except (OSError, ValueError):
        return False


def _write_marker(marker: Path, target: Path, digest: str) -> None:
    st = target.stat()
    marker.write_text(json.dumps({"digest": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns}) + "\n")


def _extract(archive: Path, root: Path, spec: dict) -> None:
    dest = root / spec["dest"]
    marker = root / f".{spec['dest'].replace('/', '__')}.verified.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if spec["kind"] == "zip":
        if dest.is_file() and _marker_ok(marker, dest, spec["sha256"]):
            return
        util.log(f"unpacking {spec['member']} from {archive.name}")
        part = dest.with_name(dest.name + ".part")
        with zipfile.ZipFile(archive) as zf, zf.open(spec["member"]) as src, open(part, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 24)
        if (spec.get("size") is not None and part.stat().st_size != spec["size"]) or util.sha256_file(part) != spec["sha256"]:
            part.unlink(missing_ok=True)
            raise SystemExit(f"{spec['member']} from {archive.name} does not match its pinned sha256")
        part.rename(dest)
        _write_marker(marker, dest, spec["sha256"])
    elif spec["kind"] == "tar":
        if dest.is_dir() and _marker_ok(marker, dest, spec["tree_sha256"]):
            return
        if dest.is_dir() and tree_digest(dest, ignore_install_metadata=True) == spec["tree_sha256"]:
            # Only editable-install metadata changed the tree (and its mtime) since it was verified: refresh the
            # marker instead of replacing a tree that a running controller may be using (OSWorld's osworld-source).
            util.log(f"{dest.name}: pinned tree unchanged apart from install metadata; marker refreshed")
            _write_marker(marker, dest, spec["tree_sha256"])
            return
        util.log(f"unpacking {archive.name} into {dest}")
        tmp = dest.with_name(dest.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        with tarfile.open(archive) as tf:
            tf.extractall(tmp, filter="tar") if hasattr(tarfile, "data_filter") else tf.extractall(tmp)
        actual = tree_digest(tmp)
        if actual != spec["tree_sha256"]:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit(f"{archive.name}: unpacked tree digest {actual} != pinned {spec['tree_sha256']}")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        _write_marker(marker, dest, spec["tree_sha256"])
    else:
        raise SystemExit(f"unknown extract kind {spec['kind']!r}")


def fetch_assets(cfg: Config, spec: dict) -> dict[str, str]:
    root = cfg.home / spec["root"]
    placed: dict[str, str] = {}
    for item in spec["files"]:
        # The release-asset store first when it is configured: every file is pinned by sha256, so the source does not
        # matter, and hosts that cannot reach an upstream (huggingface.co, for example) would otherwise spend a long
        # timeout per file before falling back to the store.
        store_url = release_asset_url(cfg, item["release_path"], item["sha256"]) if item.get("release_path") else None
        urls = ([store_url] if store_url else []) + list(item.get("urls") or [])
        cached = _cached(cfg, Path(item["path"]).name, item["sha256"], urls, item.get("size"))
        if item.get("extract"):
            _extract(cached, root, item["extract"])
        else:
            _place(cached, root / item["path"])
        placed[item["path"]] = item["sha256"]
    util.log(f"{len(placed)} evaluator assets ready under {root}")
    return placed


class _Links(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs += [v for k, v in attrs if k == "href" and v]


def _pip_index(cfg: Config) -> str:
    return (cfg.get("AGENTSWE_PIP_INDEX_URL") or "https://pypi.org/simple").rstrip("/")


def _wheel_url(cfg: Config, wheel: str) -> str:
    index = _pip_index(cfg)
    project = re.sub(r"[-_.]+", "-", wheel.split("-")[0]).lower()
    page = cfg.home / "cache" / "downloads" / f".simple-{project}.html"
    if not _fetch(cfg, f"{index}/{project}/", page):
        raise SystemExit(f"cannot read the package index page for {project} at {index}")
    links = _Links()
    links.feed(page.read_text(errors="replace"))
    page.unlink(missing_ok=True)
    for href in links.hrefs:
        if urllib.parse.unquote(href.split("#")[0].rsplit("/", 1)[-1]) == wheel:
            return urllib.parse.urljoin(f"{index}/{project}/", href.split("#")[0])
    raise SystemExit(f"{wheel} is not listed on {index}/{project}/")


def fetch_wheels(cfg: Config, spec: dict) -> int:
    root = cfg.home / spec["root"]
    count = 0
    for line in (REPO_ROOT / spec["sums"]).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        sha256, rel = line.split()
        wheel = rel.rsplit("/", 1)[-1]
        path = cfg.home / "cache" / "downloads" / cache_name(sha256, wheel)
        urls = [] if (path.is_file() and util.sha256_file(path) == sha256) else [_wheel_url(cfg, wheel)]
        _place(_cached(cfg, wheel, sha256, urls), root / rel)
        count += 1
    util.log(f"{count} pinned verifier wheels ready under {root}")
    return count


def _local_layers(image: str) -> list[str] | None:
    done = subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .RootFS.Layers}}"],
                          text=True, capture_output=True, check=False)
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout.strip() or "null")
    except ValueError:
        return None


def pull_images(items: list[dict]) -> list[str]:
    done = []
    for item in items:
        local = item.get("image") or item["ref"]
        if item.get("layers") and _local_layers(local) == item["layers"]:
            # already present with the pinned content (pulled earlier or `docker load`ed from an archive)
            util.log(f"{local}: present with the pinned rootfs layers")
            done.append(local)
            continue
        util.log(f"pulling {item['ref']}")
        util.run(["docker", "pull", "-q", item["ref"]], capture=True)
        if item.get("image"):  # the tag the controller names, when it does not use the digest itself
            util.run(["docker", "tag", item["ref"], item["image"]], capture=True)
        done.append(item.get("image") or item["ref"])
    return done


def uv_venv(cfg: Config, spec: dict) -> str:
    home = cfg.home
    dest, project = home / spec["dest"], home / spec["project"]
    python, uv = home / spec["python"], home / spec["uv"]
    lock = project / "uv.lock"
    if util.sha256_file(lock) != spec["lock_sha256"]:
        raise SystemExit(f"{lock}: sha256 differs from the pinned lock {spec['lock_sha256']}")
    marker = dest / ".agentswe-locked-env.json"
    want = {"lock_sha256": spec["lock_sha256"], "python": str(python), "editable": spec.get("editable", [])}
    if marker.is_file() and json.loads(marker.read_text()) == want:
        return str(dest)
    os.chmod(uv, 0o755)
    env = {**os.environ, "UV_NO_CONFIG": "1", "UV_PYTHON_DOWNLOADS": "never",
           "UV_CACHE_DIR": str(home / "cache" / "uv"), "UV_INDEX_URL": _pip_index(cfg),
           "UV_HTTP_TIMEOUT": "300"}
    env.update(_curl_env(cfg))
    reqs = home / "cache" / f"{dest.name}.requirements.txt"
    util.log(f"building {dest} from the pinned uv.lock")
    util.run([str(uv), "export", "--frozen", "--no-dev", "--no-emit-project", "--no-emit-local",
              "--format", "requirements-txt", "--project", str(project), "-o", str(reqs)], env=env, capture=True)
    shutil.rmtree(dest, ignore_errors=True)
    util.run([str(uv), "venv", "--python", str(python), str(dest)], env=env, capture=True)
    util.run([str(uv), "pip", "install", "--python", str(dest / "bin" / "python"), "--require-hashes",
              "--no-deps", "-r", str(reqs)], env=env, capture=False)
    for rel in spec.get("editable", []):
        util.run([str(uv), "pip", "install", "--python", str(dest / "bin" / "python"), "--no-deps",
                  "-e", str(project / rel)], env=env, capture=True)
    marker.write_text(json.dumps(want, indent=2) + "\n")
    return str(dest)


def setup(cfg: Config, task) -> dict:
    rc = task.data.get("runner_config", {})
    record: dict = {}
    if rc.get("pull_images"):
        record["images"] = pull_images(rc["pull_images"])
    if rc.get("assets"):
        record["assets"] = fetch_assets(cfg, rc["assets"])
    if rc.get("wheels"):
        record["wheels"] = fetch_wheels(cfg, rc["wheels"])
    if rc.get("uv_venv"):
        record["uv_venv"] = uv_venv(cfg, rc["uv_venv"])
    return record
