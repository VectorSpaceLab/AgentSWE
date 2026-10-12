#!/usr/bin/env python3
"""Pre-build Harbor's egress-control sidecar image through a registry mirror.

Run with the Python of the patched Harbor venv (it imports harbor):

    $AGENTSWE_HOME/harbor/venv-creation/bin/python seed_egress_sidecar.py --registry mirror.example.com
    ... --dry-run          # only print the image name Harbor will look for and whether it exists

Why: harbor 0.20.0 builds ``harbor-prebuilt:harbor-docker-egress-control-sidecar--<hash>``
on first use from a context whose Dockerfile pins ``FROM gogost/gost:<tag>@sha256:<digest>``
on Docker Hub.  On hosts without Docker Hub access that build fails (a daemon
``registry-mirrors`` entry is not always honoured by buildx, and some mirrors refuse
this repository).  The paper hosts were seeded by hand in the same way.

What: compute exactly the content-addressed name Harbor will look for (same hash
inputs: Dockerfile, context, build args, platform), build a temporary copy of the
context in which only the registry prefix of the FROM reference is changed (the
digest stays pinned, so the base layers are identical), and tag it with that name.
Harbor then finds the image and never builds.  Harbor itself is not modified.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def mirrored(reference: str, registry: str) -> str:
    registry = registry.strip().rstrip("/")
    if registry in ("", "docker.io", "registry-1.docker.io"):
        return reference
    first = reference.split("/", 1)[0]
    if "/" in reference and ("." in first or ":" in first or first == "localhost"):
        reference = reference.split("/", 1)[1]  # drop an explicit registry
    if "/" not in reference.split("@", 1)[0].split(":", 1)[0]:
        reference = "library/" + reference
    return f"{registry}/{reference}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--registry", default=os.environ.get("AGENTSWE_DOCKER_REGISTRY", "docker.io"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from harbor.environments.docker import utils
    from harbor.environments.docker.docker import DockerEnvironment

    context = Path(DockerEnvironment._EGRESS_CONTROL_SIDECAR_CONTEXT_PATH)
    dockerfile = context / "Dockerfile"
    platform = asyncio.run(utils.default_docker_platform())
    key = utils.docker_build_context_hash(
        context=context, dockerfile_path=dockerfile, build_args={}, platform=platform
    )
    image = utils._compute_image_name(DockerEnvironment._EGRESS_CONTROL_SIDECAR_DOCKER_NAME, key)
    exists = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    print(f"{image} ({platform}): {'present' if exists else 'missing'}")
    if exists or args.dry_run:
        return 0

    text = dockerfile.read_text(encoding="utf-8")
    match = re.search(r"^FROM\s+(\S+)", text, re.MULTILINE)
    if match is None or "@sha256:" not in match.group(1):
        print("refusing: the sidecar base image is not digest-pinned", file=sys.stderr)
        return 1
    base = match.group(1)
    source = mirrored(base, args.registry)
    with tempfile.TemporaryDirectory(prefix="agentswe-sidecar-") as tmp:
        build = Path(tmp) / "context"
        shutil.copytree(context, build)
        (build / "Dockerfile").write_text(text.replace(base, source, 1), encoding="utf-8")
        subprocess.run(["docker", "build", "--platform", platform, "-t", image, str(build)], check=True)
    print(f"seeded {image} from {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
