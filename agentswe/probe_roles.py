"""`agentswe probe-roles`: one minimal request per configured role, through the broker code path.

Validates a profile's wiring cheaply (provider URL, key, wire, model name, effort) before any run: BUILDER, RUNTIME
and JUDGE each get one "Reply with the single word OK." request; SEARCH (when configured) gets one query. The broker
runs in-process on loopback with the key in memory; nothing but the verdicts is printed (no key, no completion text).
"""
from __future__ import annotations

import sys
from urllib.parse import urlparse

from .config import REPO_ROOT, Config

ROLES = ("BUILDER", "RUNTIME", "JUDGE")


def probe_roles(cfg: Config, roles: tuple[str, ...] = ROLES, search: bool = True, timeout: float = 300.0,
                family: str | None = None) -> dict:
    sys.path.insert(0, str(REPO_ROOT / "broker"))
    from agentswe_broker import config as broker_config
    from agentswe_broker.probe import run_probe, run_search_probe
    from agentswe_broker.server import Options

    report: dict = {"profile": cfg.profile, "profile_explicit": bool(cfg.get("AGENTSWE_PROFILE")),
                    "family": family, "roles": {}}
    for name in roles:
        r = cfg.role(name, family)
        row = {"model": r.model, "effort": r.effort, "wire": r.wire,
               "upstream_host": urlparse(r.base_url).hostname if r.base_url else None}
        if not (r.base_url and r.api_key and r.model):
            report["roles"][name] = {**row, "ok": False, "error": "not configured (base URL, key and model required)"}
            continue
        options = Options(role=name.lower(), upstream_wire=r.wire,
                          provider_url=broker_config.endpoint_for(r.base_url, r.wire), model=r.model,
                          effort=broker_config.normalize_effort(r.effort))
        result = run_probe(options, r.api_key, minimal=True, timeout=timeout)
        report["roles"][name] = {**row, **result["checks"]["basic"], "ok": result["ok"]}
    base, key = cfg.search
    if search and base:
        wire = (cfg.get("AGENTSWE_SEARCH_WIRE") or "serper").lower()
        row = {"wire": wire, "upstream_host": urlparse(base).hostname}
        if not key:
            report["roles"]["SEARCH"] = {**row, "ok": False, "error": "AGENTSWE_SEARCH_API_KEY is not set"}
        else:
            options = Options(role="search", upstream_wire=wire, provider_url=broker_config.endpoint_for(base, wire),
                              client_tokens=("search-placeholder",))
            result = run_search_probe(options, key)
            report["roles"]["SEARCH"] = {**row, **result["checks"]["search"], "ok": result["ok"]}
    report["ok"] = all(row.get("ok") for row in report["roles"].values())
    return report
