#!/usr/bin/env python3
"""Run a frozen prediction in an evaluator-owned TerminalBench Harbor task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


# Host locations come from the runner (AGENTSWE_*).
_AGENTSWE_HOME = Path(os.environ.get("AGENTSWE_HOME", ".agentswe"))
HARBOR_ROOT = Path(os.environ.get("AGENTSWE_HARBOR_ROOT", str(_AGENTSWE_HOME / "harbor")))
HARBOR = HARBOR_ROOT / "bin/harbor"
PYTHON_ROOT = Path(os.environ.get(
    "AGENTSWE_STANDALONE_PYTHON312", str(_AGENTSWE_HOME / "tools" / "cpython-3.12")))
# Evaluator-owned assets (locked image archives, task build assets, verifier wheelhouse); setup
# fetches them by sha256 into this directory.
ASSETS_ROOT = Path(os.environ.get(
    "AGENTSWE_TERMINALBENCH_ASSETS", str(_AGENTSWE_HOME / "assets" / "terminalbench")))
# Broker image, pinned by digest.
BROKER_IMAGE = os.environ.get(
    "AGENTSWE_TERMINALBENCH_BROKER_IMAGE",
    "docker.io/library/ubuntu@"
    "sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea",
)
BROKER_STATS_TOKEN = "stats-only-placeholder"
# Build transport inside task image builds: the upstream apt sources and pip index unless a mirror is
# configured.
APT_MIRROR = os.environ.get("AGENTSWE_TERMINALBENCH_APT_MIRROR", "").rstrip("/")
PIP_INDEX_URL = os.environ.get("AGENTSWE_TERMINALBENCH_PIP_INDEX", "https://pypi.org/simple")
# Tasks whose upstream base is an end-of-life Debian release (debian:bullseye-slim, Debian 11). Its security updates
# have moved to the Debian archive: deb.debian.org (and every regular mirror) still publishes the bullseye indexes,
# but the bullseye-security pool files they name return 404, so `apt-get install` fails. For these tasks the build
# fetches all bullseye suites from the archive, with or without AGENTSWE_TERMINALBENCH_APT_MIRROR. Only the
# transport changes: on 2026-10-09 the archive's Packages indexes for bullseye, bullseye-updates and
# bullseye-security were byte-identical to deb.debian.org's, so apt installs the same versions; none of the three
# InRelease files carries a Valid-Until field, so no Check-Valid-Until override is needed.
# AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE names the archive base (serving /debian and /debian-security), for example a
# public mirror of archive.debian.org (several carry it as <mirror>/debian-archive).
DEBIAN_ARCHIVE = (os.environ.get("AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE", "") or "http://archive.debian.org").rstrip("/")
DEBIAN_ARCHIVE_TASKS = {"qemu-startup", "qemu-alpine-ssh"}
STATIC_AGENT_IMPORT = "optimization_native.terminalbench_agent:FrozenCommandPlanAgent"
LIVE_AGENT_IMPORT = "optimization_native.terminalbench_agent:LiveTerminus2Agent"
WHEELHOUSE_ROOT = Path(os.environ.get("AGENTSWE_TERMINALBENCH_WHEELHOUSE", str(ASSETS_ROOT / "wheelhouse")))
PY313_PURE_PACKAGES = (
    "pytest==8.4.1",
    "iniconfig==2.3.0",
    "packaging==26.3",
    "pluggy==1.6.0",
    "pygments==2.20.0",
)
PY313_TASKS = {
    "cancel-async-tasks",
    "circuit-fibsqrt",
    "hello-world",
    "count-dataset-tokens",
    "reverse-engineering",
}
PY312_TASKS = {
    "jsonl-aggregator",
    "log-summary",
    "write-compressor",
    "csv-to-parquet",
    # The upstream adaptive-rejection-sampler verifier installs curl, uv,
    # pytest, NumPy, and SciPy at runtime.  Its locked official test payload
    # only imports NumPy, so use the evaluator-owned py312 wheelhouse for the
    # dependencies actually exercised by that payload and keep verification
    # offline and bounded.
    "adaptive-rejection-sampler",
    # sqlite-db-truncate's upstream verifier installs curl/uv/pytest over the
    # network.  The official task image already contains Python 3.12, so use
    # the locked py312 wheelhouse instead and keep verifier execution bounded.
    "sqlite-db-truncate",
}
TASK_PRE_TEST_COMMANDS = {
    "cancel-async-tasks": ("cp /tests/test.py /app/test.py",),
}
TASK_TEST_REQUIREMENTS = {
    "csv-to-parquet": ("pytest==8.4.1", "pandas==2.3.0", "pyarrow==20.0.0"),
    "adaptive-rejection-sampler": ("pytest==8.4.1", "numpy==2.2.6"),
}
PINNED_BASE_IMAGES = {
    "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624": "sha256:2591fd9a98fc54fc79cddd50700fa320a77880cfdee1e06169b22e7708b48ad1",
    "ghcr.io/laude-institute/t-bench/python-3-13:20250620": "sha256:75cb41dc2d4f832786b0d4be2c474eb2177ece9d173bdfdff74af6509484dcc9",
}
PINNED_DERIVED_IMAGES = {
    "agentswe/tbench-write-compressor-deps:20260812": "sha256:4adf79eda1aab271f8e98da9b35aebacab74903d94e95191f2b7b972b48fcd87",
    "agentswe/tbench-reverse-engineering-deps:20260812": "sha256:93794132e469cc041bf225ed5010c580aa5a33c5fae268a5e7fc3a0a4efc4a5b",
    "agentswe/tbench-accelerate-maximal-square-deps:20260813": "sha256:f4d219c57284d2984868db90ab575ca38150025f9cff29aca2b238cef16c4680",
    "agentswe/tbench-csv-to-parquet-deps:20260813": "sha256:465209f6d99f544ba843ea9b80e0a4e3445a5603a20f3b1410c06534774e9c44",
}
# Docker 29/containerd exposes the image manifest ID after loading the locked
# legacy archive.  The archive's config filenames are the protocol IDs above,
# but they are not the value returned by ``docker image inspect .Id`` on this
# daemon.  These runtime IDs were obtained from the immutable recovery archive
# and are kept separate so we do not weaken the pinned-image contract.
PINNED_RUNTIME_IMAGE_IDS = {
    "ghcr.io/laude-institute/t-bench/python-3-13:20250620": "sha256:4527ef6ac693aa3d70db1774d82100263b3027bd3411b646b6c5318a08bf8ad1",
    "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624": "sha256:8db77515d3fce3a720b91ae4679065ca22b154743d43a4017f457657fd1b8bcd",
    "agentswe/tbench-write-compressor-deps:20260812": "sha256:ba965bae788590a9eebf6ae21fd5fa7083205508c3afa4fab2f9b26e1dfd92a0",
    "agentswe/tbench-reverse-engineering-deps:20260812": "sha256:845f9b3a1acf7273d39edd6ff7addd172e7ec30a15f071cdcde46249d8142251",
    "agentswe/tbench-csv-to-parquet-deps:20260813": "sha256:deb09aabf4d0e7f69d4f75e073dab5fd320e2bb0b637d75cfbe4819e6107b59b",
}
# Release: content identity of every pinned image, as the ordered rootfs layer digests (diff_ids) of
# the image configs that the protocol IDs above name. `docker image inspect .Id` is that config digest
# on the classic image store but a manifest digest on the containerd store, so the IDs alone cannot
# identify the same content on every daemon; the layer list can, for an image pulled by digest and
# for one loaded from the locked archive. The protocol and runtime IDs remain accepted as before.
PINNED_IMAGE_LAYERS = {
    "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624": (
        "sha256:3abdd8a5e7a8909e1509f1d36dcc8b85a0f95c68a69e6d86c6e9e3c1059d44b3",
        "sha256:f5414c85c764fde3860542d0d9e0b83cde7820bda73d64f3c7347f3a014919b5",
        "sha256:e86593aa47c118e633e3ea92390960d315ba246eb222df97a7967f5bc3284fd5",
    ),
    "ghcr.io/laude-institute/t-bench/python-3-13:20250620": (
        "sha256:f5fe472da25334617e6e6467c7ebce41e0ae5580e5bd0ecbf0d573bacd560ecb",
        "sha256:52fc15be27b420bfb40eb5c05d987a48267ba43d5277e5505fc00a8eff167df3",
        "sha256:e47eada9597c0a59b765450294565f1eb4be08aec044b41197ecf5f90d37d977",
        "sha256:955362541777c2c7efb02f705fc9bbb2c7620a832096c6a56fea713a054d974b",
        "sha256:b9e273228ebffa28589b5a2cc51f48c74b1be505cba179f560b8ba2a13b8d173",
        "sha256:9204e406a648ec41d379a62f56b7f16386ea6c9b331cd8a1703add8554af3fa1",
    ),
    "agentswe/tbench-write-compressor-deps:20260812": (
        "sha256:3abdd8a5e7a8909e1509f1d36dcc8b85a0f95c68a69e6d86c6e9e3c1059d44b3",
        "sha256:f5414c85c764fde3860542d0d9e0b83cde7820bda73d64f3c7347f3a014919b5",
        "sha256:e86593aa47c118e633e3ea92390960d315ba246eb222df97a7967f5bc3284fd5",
        "sha256:c64a3b7bba0e2b26fe4725ca05ee36ca9ea362fb399e1e51f1dcfcd43bc57c4a",
    ),
    "agentswe/tbench-reverse-engineering-deps:20260812": (
        "sha256:f5fe472da25334617e6e6467c7ebce41e0ae5580e5bd0ecbf0d573bacd560ecb",
        "sha256:52fc15be27b420bfb40eb5c05d987a48267ba43d5277e5505fc00a8eff167df3",
        "sha256:e47eada9597c0a59b765450294565f1eb4be08aec044b41197ecf5f90d37d977",
        "sha256:955362541777c2c7efb02f705fc9bbb2c7620a832096c6a56fea713a054d974b",
        "sha256:b9e273228ebffa28589b5a2cc51f48c74b1be505cba179f560b8ba2a13b8d173",
        "sha256:9204e406a648ec41d379a62f56b7f16386ea6c9b331cd8a1703add8554af3fa1",
        "sha256:4ddd0d8bbe680d9e74468d49d8773232a568130dd6417625d978a0a34e1e9d9f",
    ),
    "agentswe/tbench-csv-to-parquet-deps:20260813": (
        "sha256:3abdd8a5e7a8909e1509f1d36dcc8b85a0f95c68a69e6d86c6e9e3c1059d44b3",
        "sha256:f5414c85c764fde3860542d0d9e0b83cde7820bda73d64f3c7347f3a014919b5",
        "sha256:e86593aa47c118e633e3ea92390960d315ba246eb222df97a7967f5bc3284fd5",
        "sha256:c1f611534b9024e35cdf9c39e57bdf863a43d096d3e0d49dcfe537f4cea54153",
    ),
}
DERIVED_IMAGE_ARCHIVES = {
    "agentswe/tbench-write-compressor-deps:20260812": {
        "path": str(ASSETS_ROOT / "images/write-compressor.tar.gz"),
        "size": 296789603,
        "sha256": "b98daf9f49a7931b1ee6c975bcfdb9d739a426f1d6e38ee49d3299c944de896c",
    },
    "agentswe/tbench-reverse-engineering-deps:20260812": {
        "path": str(ASSETS_ROOT / "images/reverse-engineering.tar.gz"),
        "size": 200980858,
        "sha256": "5796d83ff4a79b94c1f347bb9d90db3787ca53d2c2965d36c3a1c18e2c9a934a",
    },
    "agentswe/tbench-csv-to-parquet-deps:20260813": {
        "path": str(ASSETS_ROOT / "images/csv-to-parquet.tar.gz"),
        "size": 52410836,
        "sha256": "585d7b9544548a5edd72871ac38f29361fe2ca26205dd4d1b39ab7c35486873f",
    },
}
TASK_BUILD_ASSETS = {
    "train-fasttext": {
        "test-00000-of-00001.parquet": {
            "path": str(ASSETS_ROOT / "task-assets/train-fasttext/test-00000-of-00001.parquet"),
            "size": 23515519,
            "sha256": "bf06d5969bff93ecd4a6d4b330643761ce108b42e9b8a894cf82c9df02a08540",
        },
        "train-00000-of-00001.parquet": {
            "path": str(ASSETS_ROOT / "task-assets/train-fasttext/train-00000-of-00001.parquet"),
            "size": 299436850,
            "sha256": "da3ca3dcc52b1e2e44195b53668610d645a59010a985dd45aa12e1109cd44601",
        },
    },
    "qemu-startup": {
        "alpine-extended-3.19.0-x86_64.iso": {
            "path": str(ASSETS_ROOT / "task-assets/alpine-3.19/alpine-extended-3.19.0-x86_64.iso"),
            "size": 1003487232,
            "sha256": "798e4805c5a4908ca231eae751ba5b2bef6cdbd3931f09b4da47fc35c329b741",
        },
    },
    "qemu-alpine-ssh": {
        "alpine-extended-3.19.0-x86_64.iso": {
            "path": str(ASSETS_ROOT / "task-assets/alpine-3.19/alpine-extended-3.19.0-x86_64.iso"),
            "size": 1003487232,
            "sha256": "798e4805c5a4908ca231eae751ba5b2bef6cdbd3931f09b4da47fc35c329b741",
        },
    },
    "make-mips-interpreter": {
        "doomgeneric-b94eba35.tar.gz": {
            "path": str(ASSETS_ROOT / "task-assets/make-mips-interpreter/doomgeneric-b94eba35.tar.gz"),
            "size": 3120653,
            "sha256": "1a4493740b066efe86b0203ad9df96dd86253fc65d810ddd78f576af117284d5",
        },
    },
    "crack-7z-hash": {
        "john-8b5bfefb.tar.gz": {
            "path": str(ASSETS_ROOT / "task-assets/crack-7z-hash/john-8b5bfefb.tar.gz"),
            "size": 56264579,
            "sha256": "1da7c18532e7c5f8b6d0bc46d27203ec090f8d9b15a3411ee0b4af3b35f694c7",
        },
    },
    "fix-code-vulnerability": {
        "bottle-0207a34f.tar.gz": {
            "path": str(ASSETS_ROOT / "task-assets/fix-code-vulnerability/bottle-0207a34f.tar.gz"),
            "size": 829931,
            "sha256": "65f496d5b5093b33637540a88813ee7ad17a1edce85dd3d5972233284943ec0a",
        },
        "flit-wheels.tar.gz": {
            "path": str(ASSETS_ROOT / "task-assets/fix-code-vulnerability/flit-wheels.tar.gz"),
            "size": 3160427,
            "sha256": "dd3b3460c06a82d4f09ee02bf7ec6ab28fa966daa4fc13510eab9a8c9122794e",
        },
    },
    "install-windows-3.11": {
        "win311.img": {
            "path": str(ASSETS_ROOT / "task-assets/install-windows-3.11/win311.img"),
            "size": 268435456,
            "sha256": "58fb76014dccf13ec048e894ca1e44773eee940ccbc71c97a2ab3d07285da553",
        },
    },
    "path-tracing": {
        "uv-0.12.1": {
            "path": str(ASSETS_ROOT / "task-assets/common/uv-0.12.1"),
            "size": 56107008,
            "sha256": "92face6b1f0462ad911857957bd168cd4ae45515e2a2cb3fcc3ecbda3d4d82b1",
        },
    },
}
TASK_ENVIRONMENT_IMAGES = {
    "adaptive-rejection-sampler": "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624",
    "cancel-async-tasks": "ghcr.io/laude-institute/t-bench/python-3-13:20250620",
    "circuit-fibsqrt": "ghcr.io/laude-institute/t-bench/python-3-13:20250620",
    "hello-world": "ghcr.io/laude-institute/t-bench/python-3-13:20250620",
    "count-dataset-tokens": "ghcr.io/laude-institute/t-bench/python-3-13:20250620",
    "reverse-engineering": "agentswe/tbench-reverse-engineering-deps:20260812",
    "jsonl-aggregator": "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624",
    "log-summary": "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624",
    "write-compressor": "agentswe/tbench-write-compressor-deps:20260812",
    "csv-to-parquet": "agentswe/tbench-csv-to-parquet-deps:20260813",
    "accelerate-maximal-square": "agentswe/tbench-accelerate-maximal-square-deps:20260813",
    "sqlite-db-truncate": "ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624",
}
STABILIZED_TEST_TASKS = PY313_TASKS | PY312_TASKS | {"accelerate-maximal-square"}
QEMU_TASKS = {"qemu-startup", "qemu-alpine-ssh"}
TAICHI_TAG = "taichidev/taichi:v0.7.26"
TAICHI_DIGEST = "sha256:bec1d405e56468c6dd376c18657df699431b20d9064bb0976977faf1c66240b2"
TAICHI_IMAGE = f"{TAICHI_TAG}@{TAICHI_DIGEST}"
TAICHI_CONFIG_DIGEST = "sha256:968f512d75f7f2b891be93cbcce4a40ba846d644bd4c4877378ef25496441ca8"
COMMAND_PLAN_BUDGET_SECONDS = 900
HARBOR_AGENT_GRACE_SECONDS = 30
ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER = 3.0
NATIVE_JOB_TIMEOUT_SECONDS = 4200
CONTROLLER_MAX_ATTEMPTS = 3
CONTROLLER_CHILD_ENV = "AGENTSWE_TERMINALBENCH_CONTROLLER_CHILD"
# Release: 34 of the 40 held-out and 5 of the 10 dev cases keep their upstream test.sh, which installs its test
# tools when the verifier runs: curl from the apt archive, uv with astral's installer
# (`curl -LsSf https://astral.sh/uv/<version>/install.sh | sh`, which downloads the uv release archive from
# github.com), then pytest from PyPI. The installers these tests fetch (uv 0.7.13, 0.8.14 and the current release)
# read UV_INSTALLER_GITHUB_BASE_URL as the base of <base>/astral-sh/uv/releases/download/<version>/<archive>.
# AGENTSWE_TERMINALBENCH_GITHUB_DOWNLOAD_BASE names a mirror of those GitHub release downloads. The controller passes it
# to the verifier as that variable through the job's `verifier.env` (Harbor adds it to the environment of the test.sh
# process), so the migrated task, its test.sh and the official test payload are unchanged. The installer checks no
# checksum, so the mirror must serve the release's own files.
GITHUB_DOWNLOAD_BASE = os.environ.get("AGENTSWE_TERMINALBENCH_GITHUB_DOWNLOAD_BASE", "").rstrip("/")
# Release: a verifier whose bootstrap failed never ran the official tests, so its reward 0 says nothing about the
# candidate. That is an infrastructure failure of the evaluation, not an official zero. Both sides need positive
# evidence in the verifier's test-stdout.txt: no pytest output at all (no line starting with "===", such as the
# session header or the summary, no "collected N items", no quiet-mode "N passed/failed" summary), and at least one
# of the bootstrap errors below. Any other output, an unknown one included, keeps the official reward.
PYTEST_STARTED = re.compile(
    r"^={3,}|\bcollected \d+ items?\b|\btest session starts\b|^\d+ (?:passed|failed|errors?|skipped)\b")
VERIFIER_BOOTSTRAP_ERRORS = (
    # astral's installer ("failed to download <url>") and curl's own error line ("curl: (56) ...")
    ("download_failed", re.compile(r"^(?:failed to download https?://\S+|curl: \(\d+\) )")),
    # bash "<script>: line N: uv: command not found"; dash "sh: 1: uv: not found"
    ("command_not_found", re.compile(r"\b(?:uv|uvx|pytest): (?:command )?not found\s*$")),
    ("module_not_found", re.compile(r"\bNo module named pytest\s*$")),
    ("apt_failed", re.compile(
        r"^E: (?:Unable to locate package|Failed to fetch|Unable to fetch some archives|"
        r"Package '[^']+' has no installation candidate)")),
    ("pip_failed", re.compile(
        r"^ERROR: (?:Could not find a version that satisfies the requirement|No matching distribution found for|"
        r"Could not install packages due to an OSError)")),
    ("uv_pip_failed", re.compile(r"^error: (?:Failed to fetch|Failed to download|Request failed after \d+ retries)")),
)
VERIFIER_STDOUT_MAX_BYTES = 8 * 1024 * 1024


def deterministic_environment_failure(errors: object) -> bool:
    """Return true for image identity defects that replay cannot fix."""
    if not isinstance(errors, list):
        return False
    text = " ".join(str(value) for value in errors)
    return bool(
        re.search(
            r"(?:pinned TerminalBench base image missing or drifted|"
            r"locked TerminalBench image restore failed|"
            r"approved immutable image)",
            text,
            re.IGNORECASE,
        )
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest_tree(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_file() and not path.is_symlink():
            h.update(b"F" + path.relative_to(root).as_posix().encode() + path.read_bytes())
    return h.hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_image_id(image: str) -> str | None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if inspect.returncode != 0:
        return None
    value = inspect.stdout.strip()
    return value or None


def inspect_image_layers(image: str) -> tuple[str, ...] | None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .RootFS.Layers}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if inspect.returncode != 0:
        return None
    try:
        layers = json.loads(inspect.stdout.strip() or "null")
    except json.JSONDecodeError:
        return None
    return tuple(layers) if isinstance(layers, list) else None


def image_content_verified(image: str) -> bool:
    expected = PINNED_IMAGE_LAYERS.get(image)
    return expected is not None and inspect_image_layers(image) == expected


def restore_derived_image(image: str, expected_id: str) -> None:
    metadata = DERIVED_IMAGE_ARCHIVES.get(image)
    if not isinstance(metadata, dict):
        raise RuntimeError(f"no locked TerminalBench archive for missing image: {image}")
    archive = Path(str(metadata["path"]))
    if not archive.is_file() or archive.stat().st_size != int(metadata["size"]):
        raise RuntimeError(f"locked TerminalBench image archive missing or wrong size: {image}")
    if file_digest(archive) != metadata["sha256"]:
        raise RuntimeError(f"locked TerminalBench image archive digest mismatch: {image}")
    loaded = subprocess.run(
        ["docker", "load", "--input", str(archive)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        timeout=900,
    )
    actual = inspect_image_id(image)
    runtime_expected = PINNED_RUNTIME_IMAGE_IDS.get(image)
    if loaded.returncode != 0 or (
        actual != expected_id and actual != runtime_expected and not image_content_verified(image)
    ):
        raise RuntimeError(f"locked TerminalBench image restore failed: {image}")


def prepare_task_build_assets(task: Path) -> dict[str, str]:
    assets = TASK_BUILD_ASSETS.get(task.name, {})
    if not assets:
        return {}
    target = task / "environment/agentswe-assets"
    target.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, str] = {}
    for name, metadata in assets.items():
        source = Path(str(metadata["path"]))
        if not source.is_file() or source.stat().st_size != int(metadata["size"]):
            raise RuntimeError(f"locked TerminalBench task asset missing or wrong size: {task.name}/{name}")
        digest = file_digest(source)
        if digest != metadata["sha256"]:
            raise RuntimeError(f"locked TerminalBench task asset digest mismatch: {task.name}/{name}")
        shutil.copy2(source, target / name)
        evidence[name] = digest
    return evidence


def environment_image(dockerfile: Path) -> str:
    text = dockerfile.read_text(encoding="utf-8")
    match = re.search(r"^FROM(?:\s+--platform=\S+)?\s+(\S+)", text, re.MULTILINE)
    if match is None:
        raise RuntimeError("TerminalBench task Dockerfile has no active FROM")
    return match.group(1)


def assert_pinned_environment_image(task: Path) -> None:
    image = environment_image(task / "environment/Dockerfile")
    expected_image = TASK_ENVIRONMENT_IMAGES.get(task.name)
    if expected_image is None or image != expected_image:
        raise RuntimeError(
            f"unexpected TerminalBench environment image for {task.name}: {image}"
        )
    expected = {**PINNED_BASE_IMAGES, **PINNED_DERIVED_IMAGES}.get(image)
    if expected is None:
        raise RuntimeError(f"TerminalBench image is not an approved immutable image: {image}")
    actual = inspect_image_id(image)
    runtime_expected = PINNED_RUNTIME_IMAGE_IDS.get(image)
    if actual == expected or (runtime_expected is not None and actual == runtime_expected):
        return
    if image_content_verified(image):
        return
    if image in PINNED_DERIVED_IMAGES:
        restore_derived_image(image, expected)
        actual = inspect_image_id(image)
        if actual in {expected, runtime_expected} or image_content_verified(image):
            return
    raise RuntimeError(f"pinned TerminalBench base image missing or drifted: {image}")


def assert_pinned_taichi_base() -> None:
    inspect = subprocess.run(
        [
            "docker", "image", "inspect", TAICHI_TAG, "--format",
            "{{json .RepoDigests}} {{.Id}}",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    try:
        raw_digests, image_id = inspect.stdout.strip().rsplit(" ", 1)
        repo_digests = json.loads(raw_digests) if inspect.returncode == 0 else []
    except (ValueError, json.JSONDecodeError):
        repo_digests = []
        image_id = ""
    digest_verified = isinstance(repo_digests, list) and any(
        str(value).endswith("@" + TAICHI_DIGEST) for value in repo_digests
    )
    if not digest_verified or image_id != TAICHI_CONFIG_DIGEST:
        raise RuntimeError(
            f"pinned Taichi image missing or digest not verified: {TAICHI_TAG}"
        )


def use_evaluator_owned_system_dependencies(task: Path) -> None:
    """Replace only unstable apt layers with locked evaluator-owned images."""
    dockerfile = task / "environment/Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    if task.name == "write-compressor":
        text = text.replace(
            "FROM ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624",
            "FROM agentswe/tbench-write-compressor-deps:20260812",
            1,
        )
        text = re.sub(r"^RUN apt update -y\s*$\n?", "", text, flags=re.MULTILINE)
        text = re.sub(
            r"^RUN apt install -y gcc rustc bc\s*$\n?", "", text,
            flags=re.MULTILINE,
        )
    elif task.name == "reverse-engineering":
        text = text.replace(
            "FROM --platform=linux/amd64 ghcr.io/laude-institute/t-bench/python-3-13:20250620",
            "FROM --platform=linux/amd64 agentswe/tbench-reverse-engineering-deps:20260812",
            1,
        )
        text = re.sub(
            r"^RUN apt-get update && apt-get install -y gcc make build-essential\s*$\n?",
            "", text, flags=re.MULTILINE,
        )
    elif task.name == "csv-to-parquet":
        text = text.replace(
            "FROM ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624",
            "FROM agentswe/tbench-csv-to-parquet-deps:20260813",
            1,
        )
        text = re.sub(
            r"^RUN apt-get update && apt-get install -y curl\s*$\n?",
            "", text, flags=re.MULTILINE,
        )
    elif task.name == "log-summary":
        # grep and bash are already present in the pinned official base image.
        text = re.sub(
            r"^RUN apt-get update && \\\n"
            r"\s*apt-get install -y grep bash && \\\n"
            r"\s*rm -rf /var/lib/apt/lists/\*\s*$\n?",
            "", text, flags=re.MULTILINE,
        )
    elif task.name == "accelerate-maximal-square":
        # Prove official base provenance before switching to the locked
        # evaluator-owned dependency layer.
        assert_pinned_taichi_base()
        text = re.sub(
            rf"^FROM\s+{re.escape(TAICHI_TAG)}(?:@sha256:[0-9a-f]{{64}})?$",
            "FROM agentswe/tbench-accelerate-maximal-square-deps:20260813",
            text, count=1, flags=re.MULTILINE,
        )
        text = re.sub(
            r"^RUN rm -f /etc/apt/sources\.list\.d/cuda\*\.list "
            r"/etc/apt/sources\.list\.d/nvidia\*\.list\s*$\n?",
            "", text, flags=re.MULTILINE,
        )
        text = re.sub(
            r"^RUN apt-get update && apt-get install -y\s+"
            r"vim tmux curl wget git python3-pip asciinema\s*$\n?",
            "", text, flags=re.MULTILINE,
        )
        text = re.sub(
            r"^RUN pip3 install numpy==1\.19\.5\s*$\n?",
            "", text, flags=re.MULTILINE,
        )
    dockerfile.write_text(text, encoding="utf-8")


def stabilize_build_transport(task: Path) -> None:
    """Use bounded public mirrors without changing task packages or versions."""
    dockerfile = task / "environment/Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    if "AGENTSWE_STABLE_BUILD_TRANSPORT" in text:
        return
    apt_preamble = "" if not APT_MIRROR else (
        "sed -i '"
        f"s|http://archive.ubuntu.com/ubuntu|{APT_MIRROR}/ubuntu|g; "
        f"s|http://security.ubuntu.com/ubuntu|{APT_MIRROR}/ubuntu|g; "
        f"s|http://deb.debian.org/debian-security|{APT_MIRROR}/debian-security|g; "
        f"s|http://deb.debian.org/debian|{APT_MIRROR}/debian|g' "
        "/etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; "
    )
    if task.name in DEBIAN_ARCHIVE_TASKS:
        # Runs before the mirror rewrite, which then finds no deb.debian.org line left to change.
        apt_preamble = (
            "sed -i '"
            f"s|http://deb.debian.org/debian-security|{DEBIAN_ARCHIVE}/debian-security|g; "
            f"s|http://security.debian.org/debian-security|{DEBIAN_ARCHIVE}/debian-security|g; "
            f"s|http://deb.debian.org/debian|{DEBIAN_ARCHIVE}/debian|g' "
            "/etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true; "
        ) + apt_preamble
    if task.name == "train-fasttext":
        for name in TASK_BUILD_ASSETS[task.name]:
            text = re.sub(
                rf"^RUN wget\s+-P /app/data\s+\S+/{re.escape(name)}\s*$",
                f"COPY agentswe-assets/{name} /app/data/{name}",
                text, flags=re.MULTILINE,
            )
    if task.name in {"qemu-startup", "qemu-alpine-ssh"}:
        text = re.sub(
            r"^RUN wget\s+.*?alpine-extended-3\.19\.0-x86_64\.iso\s+"
            r"-O /app/alpine\.iso\s*$",
            "COPY agentswe-assets/alpine-extended-3.19.0-x86_64.iso /app/alpine.iso",
            text, flags=re.MULTILINE,
        )
    if task.name == "install-windows-3.11":
        text = re.sub(
            r"^RUN wget\s+.*?-O /app/isos/win311\.img\s+"
            r"https://archive\.org/download/win311_202508/win311\.img\s*$",
            "COPY agentswe-assets/win311.img /app/isos/win311.img",
            text, flags=re.MULTILINE,
        )
    if task.name == "path-tracing":
        text = text.replace(
            "COPY install.sh /app",
            "COPY agentswe-assets/uv-0.12.1 /usr/local/bin/uv\n"
            "RUN chmod 0755 /usr/local/bin/uv\n"
            "COPY install.sh /app",
            1,
        )
        install = task / "environment/install.sh"
        install_text = install.read_text(encoding="utf-8")
        install_text = install_text.replace(
            "curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh",
            "true # evaluator-owned uv binary is already installed",
        )
        install_text = install_text.replace(
            "source $HOME/.local/bin/env",
            f"export PATH=/usr/local/bin:$PATH\nexport UV_INDEX_URL={PIP_INDEX_URL}",
        )
        install.write_text(install_text, encoding="utf-8")
    if task.name == "fix-code-vulnerability":
        text = text.replace(
            "FROM python:3.11-slim\n",
            "FROM python:3.11-slim\n"
            "COPY agentswe-assets/flit-wheels.tar.gz /tmp/flit-wheels.tar.gz\n"
            "RUN mkdir -p /opt/flit-wheels && tar -xzf /tmp/flit-wheels.tar.gz "
            "-C /opt && rm /tmp/flit-wheels.tar.gz\n",
            1,
        )
        text = re.sub(
            r"^RUN pip install .*?--upgrade pip==24\.2 && pip install flit==3\.12\.0\s*$",
            "RUN pip install --no-index --find-links /opt/flit-wheels flit==3.12.0",
            text, flags=re.MULTILINE,
        )
        text = text.replace(
            "RUN FLIT_ROOT_INSTALL=1 flit install",
            "RUN FLIT_ROOT_INSTALL=1 PIP_NO_INDEX=1 "
            "PIP_FIND_LINKS=/opt/flit-wheels flit install --deps=none",
            1,
        )
        text = text.replace(
            f"RUN pip install --index-url {PIP_INDEX_URL} "
            "--retries 10 --timeout 180 .",
            "RUN PIP_NO_INDEX=1 PIP_FIND_LINKS=/opt/flit-wheels "
            "pip install --no-deps .",
            1,
        )
    locked_git_archives = {
        "make-mips-interpreter": (
            "doomgeneric-b94eba35.tar.gz", "doomgeneric",
            r"^RUN git clone -o origin --single-branch https://github\.com/ozkl/doomgeneric.*?"
            r"git gc --prune=now --aggressive\s*$",
        ),
        "crack-7z-hash": (
            "john-8b5bfefb.tar.gz", "/app/john",
            r"^RUN git clone -o origin --single-branch https://github\.com/openwall/john\.git /app/john.*?"
            r"git gc --prune=now --aggressive\s*$",
        ),
        "fix-code-vulnerability": (
            "bottle-0207a34f.tar.gz", "/app",
            r"^RUN git clone -o origin --single-branch https://github\.com/bottlepy/bottle\.git /app.*?"
            r"git gc --prune=now --aggressive\s*$",
        ),
    }
    archive_spec = locked_git_archives.get(task.name)
    if archive_spec is not None:
        archive_name, destination, clone_pattern = archive_spec
        text = re.sub(
            clone_pattern,
            f"COPY agentswe-assets/{archive_name} /tmp/{archive_name}\n"
            f"RUN mkdir -p {destination} && tar -xzf /tmp/{archive_name} "
            f"--strip-components=1 -C {destination} && rm /tmp/{archive_name}",
            text, count=1, flags=re.MULTILINE | re.DOTALL,
        )
    text = text.replace("RUN apt-get update", "RUN " + apt_preamble + "apt-get update")
    text = text.replace("RUN apt update", "RUN " + apt_preamble + "apt update")
    text = re.sub(
        r"^RUN pip3? install\s+",
        f"RUN pip install --index-url {PIP_INDEX_URL} "
        "--retries 10 --timeout 180 ",
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"^RUN curl\s+",
        "RUN curl --retry 5 --retry-all-errors --connect-timeout 30 ",
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"^RUN wget\s+",
        "RUN wget --tries=5 --timeout=60 ",
        text, flags=re.MULTILINE,
    )
    if task.name == "financial-document-processor":
        # The setup script uses only Python 3.12-compatible stdlib APIs. Avoid
        # uv's implicit mutable CPython download while preserving its output.
        text = text.replace(
            "RUN uv run /root/randomize_filenames.py",
            "RUN python3 /root/randomize_filenames.py",
            1,
        )
    dockerfile.write_text(
        text.rstrip() + "\n\n# AGENTSWE_STABLE_BUILD_TRANSPORT\n",
        encoding="utf-8",
    )


def trial_agent_metadata(jobs_dir: Path, job_name: str) -> dict[str, Any]:
    results = sorted((jobs_dir / job_name).glob("*/result.json"))
    if len(results) != 1:
        return {}
    try:
        value = json.loads(results[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    agent_result = value.get("agent_result") if isinstance(value, dict) else None
    metadata = agent_result.get("metadata") if isinstance(agent_result, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def verifier_environment() -> dict[str, str]:
    """Environment Harbor adds to the verifier's test.sh process; empty unless a download mirror is configured."""
    return {"UV_INSTALLER_GITHUB_BASE_URL": GITHUB_DOWNLOAD_BASE} if GITHUB_DOWNLOAD_BASE else {}


def verifier_bootstrap_failure(test_stdout: str) -> dict[str, Any] | None:
    """The bootstrap errors of a verifier that never started its tests, or None (tests started, or no known error)."""
    lines = [line.strip() for line in re.split(r"[\r\n]+", test_stdout) if line.strip()]
    if any(PYTEST_STARTED.search(line) for line in lines):
        return None
    signatures: list[str] = []
    evidence: list[str] = []
    for name, pattern in VERIFIER_BOOTSTRAP_ERRORS:
        matched = [line for line in lines if pattern.search(line)]
        if matched:
            signatures.append(name)
            evidence.extend(matched[:3])
    if not signatures:
        return None
    return {"signatures": signatures, "evidence": [line[:300] for line in evidence[:8]]}


def trial_verifier_bootstrap_failure(jobs_dir: Path, job_name: str) -> dict[str, Any] | None:
    """verifier_bootstrap_failure of the job's single trial; None when its test-stdout.txt is missing or too large."""
    paths = sorted((jobs_dir / job_name).glob("*/verifier/test-stdout.txt"))
    if len(paths) != 1:
        return None
    try:
        if paths[0].stat().st_size > VERIFIER_STDOUT_MAX_BYTES:
            return None
        text = paths[0].read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    failure = verifier_bootstrap_failure(text)
    if failure is not None:
        failure["test_stdout"] = str(paths[0])
    return failure


def only_agent_timeout_errors(rows: list[dict[str, Any]]) -> bool:
    error_types: set[str] = set()
    error_count = 0
    for row in rows:
        error_count += int(row.get("n_errors", 0) or 0)
        stats = row.get("exception_stats")
        if isinstance(stats, dict):
            error_types.update(str(key) for key, value in stats.items() if value)
    return error_count > 0 and error_types == {"AgentTimeoutError"}


def broker_outcome(stats: dict[str, Any] | None) -> str:
    """Classify evaluator-owned model evidence before Harbor exception labels."""
    if not isinstance(stats, dict):
        return "missing"
    runtime = stats.get("runtime")
    if not isinstance(runtime, dict):
        return "missing"
    if runtime.get("budget_exceeded") is True:
        return "budget_exceeded"
    if int(runtime.get("failures", 0) or 0) > 0:
        return "provider_failure"
    return "ok"


def read_broker_stats(port: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/stats",
        headers={"Authorization": f"Bearer {BROKER_STATS_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("terminalbench_broker_stats_invalid")
    return value


def prediction_row(path: Path, case_id: str) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") != case_id:
        raise ValueError("prediction row missing or id mismatch")
    agent = rows[0].get("agent")
    if isinstance(agent, dict) and agent.get("kind") == "terminus2_live":
        policy = agent.get("policy", "")
        if not isinstance(policy, str) or len(policy) > 60_000:
            raise ValueError("terminus2_live policy must be a bounded string")
        return rows[0]
    commands = rows[0].get("commands")
    if not isinstance(commands, list) or not commands or len(commands) > 40:
        raise ValueError("TerminalBench prediction requires 1..40 commands")
    if not all(isinstance(item, str) for item in commands):
        raise ValueError("TerminalBench commands must be strings")
    return rows[0]


def patch_test_dependencies(task: Path) -> None:
    """Move verifier dependency installation to the image build, keeping tests intact."""
    dockerfile = task / "environment/Dockerfile"
    test_sh = task / "tests/test.sh"
    content = test_sh.read_text(encoding="utf-8")
    pytest_line = next(
        (line for line in content.splitlines() if "pytest" in line and "test_outputs.py" in line),
        "python3 -m pytest /tests/test_outputs.py -rA",
    )
    pytest_line = re.sub(r"^.*?(?:uv run )?pytest\s+", "python3 -m pytest ", pytest_line.strip())
    if "python3 -m pytest" not in pytest_line:
        pytest_line = "python3 -m pytest /tests/test_outputs.py -rA"
    test_sh.write_text(
        "#!/usr/bin/env bash\nset +e\n"
        "export PYTHONPATH=/opt/agentswe-testsite${PYTHONPATH:+:$PYTHONPATH}\n"
        + "".join(command + "\n" for command in TASK_PRE_TEST_COMMANDS.get(task.name, ()))
        + pytest_line + "\ncode=$?\n"
        "if [ $code -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; "
        "else echo 0 > /logs/verifier/reward.txt; fi\nexit $code\n",
        encoding="utf-8",
    )
    test_sh.chmod(0o755)
    docker_text = dockerfile.read_text(encoding="utf-8")
    if "AGENTSWE_PINNED_TEST_DEPS" not in docker_text:
        task_id = task.name
        if task_id == "accelerate-maximal-square":
            # The immutable Taichi v0.7.26 image is Python 3.6 and already
            # contains pytest 6.2.4. Pytest 8.4.1 requires Python >= 3.9, so
            # installing it here can never succeed. The image digest pins the
            # bundled pytest version; the official test module is unchanged.
            dockerfile.write_text(
                docker_text.rstrip() + "\n\n# AGENTSWE_PINNED_TEST_DEPS\n"
                "HEALTHCHECK --interval=1s --timeout=3s --retries=5 CMD "
                "python3 -c \"import pytest,sys; "
                "sys.exit(0 if pytest.__version__ == '6.2.4' else 1)\"\n",
                encoding="utf-8",
            )
            return
        if task_id in PY313_TASKS:
            abi = "py313"
        elif task_id in PY312_TASKS:
            abi = "py312"
        else:
            raise RuntimeError(f"no locked verifier ABI for {task_id}")
        wheelhouse = WHEELHOUSE_ROOT / abi
        if not wheelhouse.is_dir() or not list(wheelhouse.glob("pytest-8.4.1-*.whl")):
            raise RuntimeError(f"pinned TerminalBench {abi} wheelhouse is missing")
        pip_wheels = list(wheelhouse.glob("pip-25.1.1-*.whl"))
        if len(pip_wheels) != 1:
            raise RuntimeError(f"pinned TerminalBench {abi} pip bootstrap is missing")
        requirements = TASK_TEST_REQUIREMENTS.get(task_id, ("pytest==8.4.1",))
        shutil.copytree(wheelhouse, task / "environment/wheels")
        dependency_layer = (
            "COPY wheels /opt/agentswe-wheels\n"
            "RUN PYTHONPATH=/opt/agentswe-wheels/pip-25.1.1-py3-none-any.whl "
            "python3 -m pip install --root-user-action=ignore --no-cache-dir --no-index "
            "--find-links /opt/agentswe-wheels --target /opt/agentswe-testsite "
            + " ".join(requirements) + "\n"
            "ENV PYTHONPATH=/opt/agentswe-testsite\n"
        )
        dockerfile.write_text(
            docker_text.rstrip() + "\n\n# AGENTSWE_PINNED_TEST_DEPS\n"
            + dependency_layer
            + "HEALTHCHECK --interval=1s --timeout=3s --retries=5 CMD test -x /bin/sh\n",
            encoding="utf-8",
        )


def stabilize_qemu_verifier(task: Path) -> None:
    """Keep QEMU verifiers offline while preserving their official test payload."""
    dockerfile = task / "environment/Dockerfile"
    docker_text = dockerfile.read_text(encoding="utf-8")
    if "AGENTSWE_QEMU_VERIFIER_DEPS" not in docker_text:
        marker = "RUN apt install -y telnet netcat expect tmux asciinema"
        replacement = (
            "RUN apt install -y python3 python3-pytest telnet netcat expect tmux asciinema\n\n"
            "# AGENTSWE_QEMU_VERIFIER_DEPS"
        )
        if marker not in docker_text:
            raise RuntimeError("qemu task Dockerfile package install anchor is missing")
        dockerfile.write_text(docker_text.replace(marker, replacement, 1), encoding="utf-8")

    test_sh = task / "tests/test.sh"
    test_sh.write_text(
        "#!/usr/bin/env bash\n"
        "set +e\n"
        "python3 -m pytest /tests/test_outputs.py -rA\n"
        "code=$?\n"
        "if [ $code -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; "
        "else echo 0 > /logs/verifier/reward.txt; fi\n"
        "exit $code\n",
        encoding="utf-8",
    )
    test_sh.chmod(0o755)


def official_test_payload_digest(task: Path) -> str:
    """Hash official verifier payloads while excluding the dependency wrapper."""
    tests = task / "tests"
    h = hashlib.sha256()
    for path in sorted(tests.rglob("*"), key=lambda item: item.relative_to(tests).as_posix()):
        if path.is_file() and not path.is_symlink() and path != tests / "test.sh":
            h.update(b"F" + path.relative_to(tests).as_posix().encode() + path.read_bytes())
    return h.hexdigest()


def stabilize_migrated_task(task: Path) -> dict[str, Any]:
    """Apply evaluator-owned, offline verifier dependencies where they are locked."""
    build_assets = prepare_task_build_assets(task)
    stabilize_build_transport(task)
    if task.name in QEMU_TASKS:
        payload_digest = official_test_payload_digest(task)
        stabilize_qemu_verifier(task)
        if official_test_payload_digest(task) != payload_digest:
            raise RuntimeError("TerminalBench official test payload changed during QEMU stabilization")
        return {
            "build_transport_stabilized": True,
            "build_asset_digests": build_assets,
            "dependency_stabilized": True,
            "pinned_image_verified": False,
            "official_test_payload_digest": payload_digest,
            "environment_image": environment_image(task / "environment/Dockerfile"),
        }
    if task.name not in STABILIZED_TEST_TASKS:
        return {
            "build_transport_stabilized": True,
            "build_asset_digests": build_assets,
            "dependency_stabilized": False,
            "pinned_image_verified": False,
            "official_test_payload_digest": official_test_payload_digest(task),
            "environment_image": environment_image(task / "environment/Dockerfile"),
        }

    payload_digest = official_test_payload_digest(task)
    use_evaluator_owned_system_dependencies(task)
    patch_test_dependencies(task)
    if official_test_payload_digest(task) != payload_digest:
        raise RuntimeError("TerminalBench official test payload changed during stabilization")
    assert_pinned_environment_image(task)
    return {
        "build_transport_stabilized": True,
        "build_asset_digests": build_assets,
        "dependency_stabilized": True,
        "pinned_image_verified": True,
        "official_test_payload_digest": payload_digest,
        "environment_image": environment_image(task / "environment/Dockerfile"),
    }


def run_once_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--native-task", type=Path, required=True)
    parser.add_argument("--expected-task-digest", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, default=HARBOR_ROOT / "jobs")
    parser.add_argument("--credential-file", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        prediction = prediction_row(args.predictions, args.case_id)
    except Exception as exc:
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": True, "infrastructure_failure": False,
            "ordinary_agent_failure": True, "validity_gate": True,
            "score": 0, "reward": 0, "errors": [str(exc)], "case_id": args.case_id})
        return 0
    source_digest = digest_tree(args.native_task)
    if source_digest != args.expected_task_digest:
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0,
            "errors": ["terminalbench_native_task_digest_mismatch"],
            "expected_native_task_digest": args.expected_task_digest,
            "native_task_digest": source_digest,
        })
        return 0
    frozen_prediction = args.output_dir / "frozen_prediction.json"
    write_json(frozen_prediction, prediction)
    migrated_root = args.output_dir / "migrated"
    migrate = subprocess.run(
        [str(HARBOR), "task", "migrate", "-i", str(args.native_task), "-o", str(migrated_root)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        env={**os.environ, "HARBOR_TELEMETRY": "off"},
    )
    (args.output_dir / "migration.log").write_text(migrate.stdout, encoding="utf-8")
    task = migrated_root / args.native_task.name
    if migrate.returncode != 0 or not task.is_dir():
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0, "errors": ["terminalbench_migration_failed"]})
        return 0
    try:
        required = (
            task / "task.toml",
            task / "instruction.md",
            task / "environment/Dockerfile",
            task / "tests/test.sh",
        )
        missing = [path.relative_to(task).as_posix() for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(f"migrated task missing required files: {missing}")
        stabilization = stabilize_migrated_task(task)
        migrated_image = stabilization["environment_image"]
    except Exception as exc:
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0,
            "errors": [
                "terminalbench_official_migration_preflight_failed:"
                f"{type(exc).__name__}:{str(exc)}"
            ],
            "deterministic_infrastructure_failure": deterministic_environment_failure([
                f"{type(exc).__name__}:{str(exc)}"
            ]),
            "native_task_digest": source_digest})
        return 0
    prediction_digest = hashlib.sha256(frozen_prediction.read_bytes()).hexdigest()
    token = hashlib.sha256(
        (
            args.case_id
            + source_digest
            + prediction_digest
            + str(args.output_dir.resolve())
        ).encode()
    ).hexdigest()[:12]
    job_name = f"agentswe-tb-{args.native_task.name}-{token}"
    live = isinstance(prediction.get("agent"), dict) and prediction["agent"].get("kind") == "terminus2_live"
    agent_import = LIVE_AGENT_IMPORT if live else STATIC_AGENT_IMPORT
    agent_env = {"AGENTSWE_TB_PREDICTION_PATH": str(frozen_prediction)}
    broker_name = ""
    broker_network = ""
    broker_port: int | None = None
    broker_stats: dict[str, Any] | None = None
    if live:
        if args.credential_file is None or not args.credential_file.is_file():
            write_json(args.output_dir / "native_result.json", {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["terminalbench_live_broker_credentials_missing"],
            })
            return 0
        broker_name = f"agentswe-tb-broker-{token}"
        broker_run = subprocess.run([
            "docker", "run", "-d", "--rm", "--name", broker_name,
            "-p", "0.0.0.0::8080",
            "-v", f"{PYTHON_ROOT}:/python:ro",
            "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
            "-v", f"{Path(__file__).resolve().parent / 'responses_broker.py'}:/broker.py:ro",
            "-v", f"{args.credential_file.resolve()}:/run/secrets/agentswe.env:ro",
            "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
            BROKER_IMAGE, "/python/bin/python3", "/broker.py",
            "--credential-file", "/run/secrets/agentswe.env",
            "--max-runtime-calls", "20", "--max-runtime-tokens", "200000",
            # Terminus-2 reaches the broker on /v1/chat/completions; that path is the agent runtime
            # here (the release broker otherwise treats chat requests as tau3's NL judge).
            "--chat-role", "runtime",
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if broker_run.returncode != 0:
            write_json(args.output_dir / "native_result.json", {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["terminalbench_live_broker_start_failed"],
            })
            return 0
        port = subprocess.run(
            ["docker", "port", broker_name, "8080/tcp"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        match = re.search(r":(\d+)\s*$", port.stdout)
        if port.returncode != 0 or match is None:
            subprocess.run(["docker", "rm", "-f", broker_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            write_json(args.output_dir / "native_result.json", {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["terminalbench_live_broker_port_failed"],
            })
            return 0
        broker_port = int(match.group(1))
        agent_env.update({
            "AGENTSWE_TB_POLICY": str(prediction["agent"].get("policy", "")),
            "AGENTSWE_TB_MAX_TURNS": str(prediction["agent"].get("max_turns", 20)),
            "AGENTSWE_TB_API_BASE": f"http://127.0.0.1:{match.group(1)}/v1",
            "OPENAI_API_KEY": "runtime-only-placeholder",
        })
    config = {
        "job_name": job_name, "jobs_dir": str(args.jobs_dir), "n_attempts": 1,
        "n_concurrent_trials": 1, "quiet": True, "retry": {"max_retries": 0},
        "environment_build_timeout_multiplier": ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER,
        "environment": {"type": "docker", "delete": True, "force_build": False},
        "agents": [{
            "import_path": agent_import,
            "override_timeout_sec": COMMAND_PLAN_BUDGET_SECONDS + HARBOR_AGENT_GRACE_SECONDS,
            "env": agent_env,
        }],
        "tasks": [{"path": str(task)}],
    }
    verifier_env = verifier_environment()
    if verifier_env:
        config["verifier"] = {"env": verifier_env}
    config_path = args.output_dir / "harbor_job.json"
    write_json(config_path, config)
    env = os.environ.copy()
    env.update({"HARBOR_ROOT": str(HARBOR_ROOT), "HARBOR_TELEMETRY": "off", "PYTHONPATH": str(HARBOR_ROOT) + os.pathsep + env.get("PYTHONPATH", "")})
    started = time.monotonic()
    try:
        run = subprocess.run(
            [str(HARBOR), "run", "--config", str(config_path)], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env,
            timeout=NATIVE_JOB_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        (args.output_dir / "harbor.stdout.log").write_text(stdout, encoding="utf-8")
        (args.output_dir / "harbor.stderr.log").write_text(stderr, encoding="utf-8")
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0,
            "errors": ["terminalbench_native_job_timeout"],
            "runtime_seconds": round(time.monotonic() - started, 3),
            "native_task_digest": source_digest,
        })
        return 0
    finally:
        if broker_name:
            if broker_port is not None:
                try:
                    broker_stats = read_broker_stats(broker_port)
                except Exception:
                    broker_stats = None
            subprocess.run(
                ["docker", "rm", "-f", broker_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
    (args.output_dir / "harbor.stdout.log").write_text(run.stdout, encoding="utf-8")
    (args.output_dir / "harbor.stderr.log").write_text(run.stderr, encoding="utf-8")
    result_path = args.jobs_dir / job_name / "result.json"
    if run.returncode != 0 or not result_path.is_file():
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0, "errors": ["terminalbench_harbor_job_failed"],
            "native_task_digest": source_digest})
        return 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    stats = result.get("stats", {})
    model_outcome = broker_outcome(broker_stats) if live else "not_applicable"
    if model_outcome == "budget_exceeded":
        write_json(args.output_dir / "native_result.json", {
            "schema_version": "1.0", "benchmark": "TerminalBench-2.0",
            "case_id": args.case_id, "native_task_id": args.native_task.name,
            "native_task_digest": source_digest, "official_evaluation": True,
            "infrastructure_failure": False, "ordinary_agent_failure": True,
            "validity_gate": True, "score": 0, "reward": 0,
            "termination_reason": "model_budget_exceeded",
            "broker_stats": broker_stats, "harbor_job": job_name,
            "harbor_stats": stats, "prediction_digest": prediction_digest,
            "official_environment_image": migrated_image,
            "verifier_dependency_stabilized": stabilization["dependency_stabilized"],
            "pinned_environment_image_verified": stabilization["pinned_image_verified"],
            "official_test_payload_digest": stabilization["official_test_payload_digest"],
            "official_task_preserved_after_harbor_migration": True,
        })
        return 0
    if live and model_outcome != "ok":
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0,
            "errors": [f"terminalbench_model_{model_outcome}"],
            "native_task_digest": source_digest, "broker_stats": broker_stats,
            "harbor_stats": stats,
        })
        return 0
    evals = stats.get("evals", {}) if isinstance(stats, dict) else {}
    rows = [value for value in evals.values() if isinstance(value, dict)] if isinstance(evals, dict) else []
    n_errors = sum(int(row.get("n_errors", 0) or 0) for row in rows)
    metrics = [m for row in rows for m in (row.get("metrics") or []) if isinstance(m, dict)]
    agent_timeout = only_agent_timeout_errors(rows)
    if not rows or (n_errors and not agent_timeout):
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0, "errors": ["terminalbench_runtime_error"],
            "native_task_digest": source_digest, "harbor_stats": stats})
        return 0
    reward = float(metrics[0].get("mean", 0) or 0) if metrics else 0.0
    agent_metadata = trial_agent_metadata(args.jobs_dir, job_name)
    candidate_timed_out = agent_timeout or agent_metadata.get("candidate_timed_out") is True
    if candidate_timed_out:
        reward = 0.0
    bootstrap = None if reward >= 1 or candidate_timed_out else trial_verifier_bootstrap_failure(args.jobs_dir, job_name)
    if bootstrap is not None:
        write_json(args.output_dir / "native_result.json", {
            "official_evaluation": False, "infrastructure_failure": True,
            "validity_gate": False, "score": 0,
            "errors": ["terminalbench_verifier_bootstrap_failed:" + ",".join(bootstrap["signatures"])],
            "verifier_bootstrap_failure": bootstrap,
            "case_id": args.case_id, "native_task_id": args.native_task.name,
            "native_task_digest": source_digest, "harbor_job": job_name, "harbor_stats": stats,
            "runtime_seconds": round(time.monotonic() - started, 3),
            "official_test_payload_digest": stabilization["official_test_payload_digest"],
            **({"verifier_env": verifier_env} if verifier_env else {}),
        })
        return 0
    write_json(args.output_dir / "native_result.json", {
        "schema_version": "1.0", "benchmark": "TerminalBench-2.0",
        "case_id": args.case_id, "native_task_id": args.native_task.name,
        "native_task_digest": source_digest, "official_evaluation": True,
        "infrastructure_failure": False, "validity_gate": True,
        "score": 100 if reward >= 1 else 0, "reward": reward,
        "candidate_timed_out": candidate_timed_out,
        "agent_execution": agent_metadata,
        "harbor_job": job_name, "harbor_stats": stats,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "prediction_digest": prediction_digest,
        "broker_stats": broker_stats,
        "official_environment_image": migrated_image,
        "verifier_dependency_stabilized": stabilization["dependency_stabilized"],
        "pinned_environment_image_verified": stabilization["pinned_image_verified"],
        "official_test_payload_digest": stabilization["official_test_payload_digest"],
        "official_task_preserved_after_harbor_migration": True,
        **({"verifier_env": verifier_env} if verifier_env else {}),
    })
    return 0


def cli_option(argv: list[str], name: str) -> str:
    try:
        index = argv.index(name)
    except ValueError as exc:
        raise ValueError(f"missing required controller option: {name}") from exc
    if index + 1 >= len(argv):
        raise ValueError(f"missing value for controller option: {name}")
    return argv[index + 1]


def replace_cli_option(argv: list[str], name: str, value: str) -> list[str]:
    updated = list(argv)
    try:
        index = updated.index(name)
    except ValueError as exc:
        raise ValueError(f"missing required controller option: {name}") from exc
    if index + 1 >= len(updated):
        raise ValueError(f"missing value for controller option: {name}")
    updated[index + 1] = value
    return updated


def controller_result_is_terminal(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("official_evaluation") is True
        and value.get("validity_gate") is True
        and value.get("infrastructure_failure") is not True
    )


def main() -> int:
    if os.environ.get(CONTROLLER_CHILD_ENV) == "1":
        return run_once_main()

    argv = sys.argv[1:]
    output_dir = Path(cli_option(argv, "--output-dir")).resolve()
    case_id = cli_option(argv, "--case-id")
    output_dir.mkdir(parents=True, exist_ok=True)
    controller_attempts: list[dict[str, Any]] = []
    last_result: dict[str, Any] | None = None

    for attempt in range(1, CONTROLLER_MAX_ATTEMPTS + 1):
        attempt_dir = output_dir / f"attempt_{attempt:03d}"
        child_argv = replace_cli_option(argv, "--output-dir", str(attempt_dir))
        env = os.environ.copy()
        env[CONTROLLER_CHILD_ENV] = "1"
        started = time.monotonic()
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *child_argv],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "controller.child.stdout.log").write_text(
            child.stdout[-20_000:], encoding="utf-8"
        )
        (attempt_dir / "controller.child.stderr.log").write_text(
            child.stderr[-20_000:], encoding="utf-8"
        )
        result_path = attempt_dir / "native_result.json"
        if result_path.is_file():
            try:
                value = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                value = {
                    "official_evaluation": False,
                    "validity_gate": False,
                    "infrastructure_failure": True,
                    "score": 0,
                    "errors": [f"terminalbench_controller_result_invalid:{type(exc).__name__}"],
                }
        else:
            value = {
                "official_evaluation": False,
                "validity_gate": False,
                "infrastructure_failure": True,
                "score": 0,
                "errors": ["terminalbench_controller_child_failed_without_result"],
            }
        if not isinstance(value, dict):
            value = {
                "official_evaluation": False,
                "validity_gate": False,
                "infrastructure_failure": True,
                "score": 0,
                "errors": ["terminalbench_controller_result_not_object"],
            }
        value.setdefault("case_id", case_id)
        controller_attempts.append({
            "attempt": attempt,
            "returncode": child.returncode,
            "runtime_seconds": round(time.monotonic() - started, 3),
            "official_evaluation": value.get("official_evaluation") is True,
            "validity_gate": value.get("validity_gate") is True,
            "infrastructure_failure": value.get("infrastructure_failure") is True,
            "deterministic_infrastructure_failure": value.get("deterministic_infrastructure_failure") is True,
            "errors": value.get("errors", []),
            "result": str(result_path),
        })
        last_result = value
        frozen = attempt_dir / "frozen_prediction.json"
        if frozen.is_file() and not (output_dir / "frozen_prediction.json").is_file():
            shutil.copy2(frozen, output_dir / "frozen_prediction.json")
        if controller_result_is_terminal(value):
            value["controller_attempts"] = controller_attempts
            write_json(output_dir / "native_result.json", value)
            write_json(output_dir / "selected_attempt.json", {
                "attempt": attempt,
                "result": str(result_path),
            })
            return 0
        if value.get("deterministic_infrastructure_failure") is True:
            value["controller_attempts"] = controller_attempts
            value["errors"] = [
                "terminalbench_controller_environment_repair_required",
                *(value.get("errors") if isinstance(value.get("errors"), list) else []),
            ]
            write_json(output_dir / "native_result.json", value)
            return 0
        if attempt < CONTROLLER_MAX_ATTEMPTS:
            time.sleep(min(10.0, float(2 ** attempt)))

    assert last_result is not None
    last_result["case_id"] = case_id
    last_result["official_evaluation"] = False
    last_result["validity_gate"] = False
    last_result["infrastructure_failure"] = True
    last_result["score"] = 0
    last_result["controller_attempts"] = controller_attempts
    errors = last_result.get("errors")
    if not isinstance(errors, list):
        errors = []
    last_result["errors"] = ["terminalbench_controller_infrastructure_retries_exhausted", *errors]
    write_json(output_dir / "native_result.json", last_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
