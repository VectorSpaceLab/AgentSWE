#!/usr/bin/env python3
"""Candidate-only broker injection: the evaluator-owned runtime model broker."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONTAINER_CREDENTIAL_SUFFIX = "/opt/agentswe/benchmark/envs/.env"
BROKER_HOST = os.environ.get("AGENTSWE_BROKER_HOST", "172.17.0.1")


def broker_port() -> int:
    raw = os.environ.get("AGENTSWE_CANDIDATE_BROKER_PORT", "18080").strip()
    value = int(raw)
    if not 1024 <= value <= 65535:
        raise RuntimeError(f"invalid Candidate broker port: {value}")
    return value


def candidate_credential_file(default: Path) -> Path:
    override = os.environ.get("AGENTSWE_CANDIDATE_CREDENTIAL_FILE", "").strip()
    path = Path(override).resolve() if override else default.resolve()
    return path


def candidate_endpoint() -> str:
    return f"http://{BROKER_HOST}:{broker_port()}/v1/responses"


def configure_candidate_compose(config: dict[str, Any]) -> dict[str, Any]:
    """Inject placeholder credentials and the evaluator-owned model broker."""
    services = config.get("services")
    if not isinstance(services, dict) or not isinstance(services.get("main"), dict):
        raise ValueError("Candidate compose has no main service")
    main = services["main"]
    volumes = main.get("volumes")
    if not isinstance(volumes, list):
        raise ValueError("Candidate compose has no volume array")
    replaced = 0
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        target = str(volume.get("target", ""))
        if target.endswith(CONTAINER_CREDENTIAL_SUFFIX):
            current_source = Path(str(volume.get("source", "")))
            credential = candidate_credential_file(current_source)
            volume["source"] = str(credential)
            replaced += 1
    if replaced != 1:
        raise ValueError(
            f"expected one Candidate credential mount, replaced {replaced}"
        )
    environment = main.setdefault("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("Candidate main environment is not an object")
    endpoint = candidate_endpoint()
    # explicit-none (the smoke default) reaches the provider as effort "none"; the Candidate is shown that value.
    runtime_effort = os.environ.get("AGENTSWE_RUNTIME_EFFORT", "high")
    environment.update({
        "AGENTSWE_RESPONSES_BASE_URL": endpoint,
        "GATEWAY_RESPONSES_ENDPOINT": endpoint,
        "OPENAI_BASE_URL": endpoint.removesuffix("/responses"),
        "OPENAI_API_KEY": "broker-only-placeholder",
        "GATEWAY_API_KEY": "broker-only-placeholder",
        # Pre-release candidates read the legacy names; same placeholder values.
        "S" "U8_API_KEY": "broker-only-placeholder",
        "S" "U8_RESPONSES_ENDPOINT": endpoint,
        "AGENTSWE_REQUIRED_MODEL": os.environ.get("AGENTSWE_RUNTIME_MODEL", "deepseek-flash"),
        "AGENTSWE_REQUIRED_REASONING_EFFORT": "none" if runtime_effort == "explicit-none" else runtime_effort,
    })
    add_search_front(services, main)
    return config


SEARCH_HOST = "search.example.com"
SEARCH_TRUST_BUNDLE = "/opt/agentswe-search-trust/ca-bundle.pem"
# Default trust stores the per-run bundle (system roots + the run CA) is ALSO bind-mounted over, read-only. In the
# published runs the search endpoint carried a publicly trusted certificate, so every client trust policy verified it;
# the trust-store variables alone miss clients that ignore them (requests.Session(trust_env=False), certifi-pinned
# httpx/aiohttp, a context built without SSL_CERT_FILE), which then fail TLS before reaching the broker.
SYSTEM_TRUST_FILES = ("/etc/ssl/certs/ca-certificates.crt",)  # Ubuntu bundle; /usr/lib/ssl/cert.pem links to it
SYSTEM_TRUST_DIR = "/etc/ssl/certs"  # OpenSSL default capath (/usr/lib/ssl/certs links to it): <subject_hash>.0
ENV_TRUST_FILES = ("ssl/cert.pem", "ssl/cacert.pem")  # a conda prefix's OpenSSL default cafile
ENV_MOUNT = re.compile(r"/opt/agentswe/benchmark/(?:agent-create-0804/)?envs/[^/.]+|/opt/agentswe-trusted-runtime")


def system_trust_targets(ca_subject_hash: str | None) -> list[str]:
    """Image-level default trust stores overridden in every Candidate main service."""
    targets = list(SYSTEM_TRUST_FILES)
    if ca_subject_hash:
        targets.append(f"{SYSTEM_TRUST_DIR}/{ca_subject_hash}.0")
    return targets


def env_trust_files(source: Path) -> list[str]:
    """Default trust stores inside one mounted environment prefix, relative to it: every certifi cacert.pem (pip's
    vendored copy included) and the prefix OpenSSL cafile. Symlinks are resolved and kept only inside the prefix, so
    every mount target already exists in the read-only prefix and nothing is created or written there."""
    root = source.resolve()
    found: set[str] = set()

    def keep(path: Path) -> None:
        try:
            real = path.resolve(strict=True)
        except OSError:
            return
        if real.is_file() and real.is_relative_to(root):
            found.add(real.relative_to(root).as_posix())

    for rel in ENV_TRUST_FILES:
        keep(root / rel)
    for directory, _subdirs, files in os.walk(root):  # symlinked directories are not followed
        if os.path.basename(directory) == "certifi" and "cacert.pem" in files:
            keep(Path(directory) / "cacert.pem")
    return sorted(found)


def search_trust_mounts(main: dict[str, Any], spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict]]:
    """Read-only file mounts of the per-run bundle over the default trust stores the Candidate can use: the image's
    system bundle and capath entry, and the certifi/OpenSSL files of every mounted environment prefix under each of
    its aliases (runtime_contract adds the alias mounts of the prefix itself when the compose file is written)."""
    from runtime_contract import environment_aliases

    volumes = [v for v in main.get("volumes", []) if isinstance(v, dict)]
    taken = {str(v.get("target", "")) for v in volumes}
    wanted: dict[str, str] = {target: spec["ca_bundle"] for target in SYSTEM_TRUST_FILES}
    if spec.get("ca") and spec.get("ca_subject_hash"):
        wanted[system_trust_targets(spec["ca_subject_hash"])[-1]] = spec["ca"]
    for target in tuple(wanted):  # a system target inside another bind mount would be created on the host
        if any(t and t != "/" and (target == t or target.startswith(t.rstrip("/") + "/")) for t in taken):
            raise RuntimeError(f"search trust target {target} lies in an existing Candidate mount")
    prefixes = []
    for volume in volumes:
        target = str(volume.get("target", ""))
        if volume.get("type", "bind") != "bind" or not ENV_MOUNT.fullmatch(target):
            continue
        source = Path(str(volume["source"]))
        files = env_trust_files(source)
        aliases = sorted(environment_aliases(source, target))
        for alias in aliases:
            for rel in files:
                wanted[f"{alias}/{rel}"] = spec["ca_bundle"]
        prefixes.append({"source": str(source), "target": target, "aliases": aliases, "files": files})
    mounts = []
    for target, source in wanted.items():
        if target in taken:
            raise RuntimeError(f"search trust target {target} is already mounted")
        mounts.append({"type": "bind", "source": source, "target": target, "read_only": True})
    return mounts, prefixes


def record_search_trust(spec: dict[str, Any], main: dict[str, Any], mounts: list[dict], prefixes: list[dict]) -> None:
    """One JSON line per Candidate compose (the launch manifest's search record names the file)."""
    path = spec.get("trust_record")
    if not path:
        return
    case = next((str(v.get("target")).rsplit("/", 1)[-1] for v in main.get("volumes", [])
                 if isinstance(v, dict) and str(v.get("target", "")).startswith("/active-case/")), None)
    row = {"at": datetime.now(timezone.utc).isoformat(), "case": case,
           "bundle_sha256": hashlib.sha256(Path(spec["ca_bundle"]).read_bytes()).hexdigest(),
           "targets": [m["target"] for m in mounts], "env_prefixes": prefixes}
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")


def add_search_front(services: dict[str, Any], main: dict[str, Any]) -> None:
    """Search-enabled tasks: a per-run TLS front named search.example.com, trusted only by this Candidate.

    AGENTSWE_SEARCH_FRONT (set by the runner) describes the front image, the run's search broker and the per-run
    certificate. The Candidate main service gets the per-run CA (appended to the system bundle) through the usual
    trust-store variables and, read-only, over the default trust stores (search_trust_mounts); the front relays to
    the broker, which holds the real key.
    """
    raw = os.environ.get("AGENTSWE_SEARCH_FRONT")
    if not raw:
        return
    spec = json.loads(raw)
    mounts, prefixes = search_trust_mounts(main, spec)
    main.setdefault("volumes", []).append(
        {"type": "bind", "source": spec["ca_bundle"], "target": SEARCH_TRUST_BUNDLE, "read_only": True})
    main["volumes"].extend(mounts)
    record_search_trust(spec, main, mounts, prefixes)
    main.setdefault("environment", {}).update({name: SEARCH_TRUST_BUNDLE for name in (
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")})
    services["agentswe-search-front"] = {
        "image": spec["image"],
        "command": ["python3", "-I", "/opt/agentswe/search_front.py", "--cert", "/run/front/cert.pem",
                    "--key", "/run/front/key.pem", "--port", "443",
                    "--upstream-host", spec["broker_host"], "--upstream-port", str(spec["broker_port"])],
        "volumes": [
            {"type": "bind", "source": spec["script"], "target": "/opt/agentswe/search_front.py", "read_only": True},
            {"type": "bind", "source": spec["cert"], "target": "/run/front/cert.pem", "read_only": True},
            {"type": "bind", "source": spec["key"], "target": "/run/front/key.pem", "read_only": True},
        ],
        "networks": {"default": {"aliases": [SEARCH_HOST]}},
        "cap_drop": ["ALL"], "cap_add": ["NET_BIND_SERVICE"], "security_opt": ["no-new-privileges:true"],
        "labels": {"agentswe_os_role": "search-front"},
    }


def candidate_task_network(toml_text: str) -> str:
    """Route Candidate model traffic to the host broker, preserving other hosts."""
    lines = toml_text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    name = ""
    body: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            sections.append((name, body))
            name, body = stripped, [line]
        else:
            body.append(line)
    sections.append((name, body))

    output: list[str] = []
    for section, section_lines in sections:
        if section not in {"[agent]", "[environment]"}:
            output.extend(section_lines)
            continue
        mode_index = next(
            (
                index for index, line in enumerate(section_lines)
                if line.strip().startswith("network_mode = ")
            ),
            None,
        )
        if mode_index is None:
            output.extend(section_lines)
            continue
        mode_line = section_lines[mode_index]
        is_public = '"public"' in mode_line
        if not is_public:
            section_lines[mode_index] = 'network_mode = "allowlist"'
        hosts_index = next(
            (
                index for index, line in enumerate(section_lines)
                if line.strip().startswith("allowed_hosts = ")
            ),
            None,
        )
        if hosts_index is not None:
            line = section_lines[hosts_index].replace(
                '"gateway.example.com"', f'"{BROKER_HOST}"'
            )
            if f'"{BROKER_HOST}"' not in line:
                line = line.rstrip().removesuffix("]")
                separator = "" if line.rstrip().endswith("[") else ", "
                line = line + separator + f'"{BROKER_HOST}"]'
            section_lines[hosts_index] = line
        elif not is_public:
            section_lines.insert(
                mode_index + 1, f'allowed_hosts = ["{BROKER_HOST}"]'
            )
        output.extend(section_lines)
    return "\n".join(output) + ("\n" if toml_text.endswith("\n") else "")
