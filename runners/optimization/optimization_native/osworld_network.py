#!/usr/bin/env python3
"""Evaluator-owned network policy and preflight for OSWorld live-web tasks."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def load_network_policy(path: Path, task_id: str) -> tuple[str, list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise RuntimeError("network_manifest_invalid")
    mode = value.get("mode")
    if mode != "controlled-direct":
        raise RuntimeError("network_mode_unsupported")
    tasks = value.get("tasks")
    if not isinstance(tasks, dict):
        raise RuntimeError("network_manifest_tasks_invalid")
    checks = tasks.get(task_id, [])
    if not isinstance(checks, list):
        raise RuntimeError("network_task_policy_invalid")
    return mode, checks


def preflight_network(path: Path, task_id: str, timeout: int = 45) -> dict[str, Any]:
    mode, checks = load_network_policy(path, task_id)
    evidence: list[dict[str, Any]] = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for row in checks:
        if not isinstance(row, dict) or set(row) != {"url", "allowed_hosts", "allowed_statuses"}:
            raise RuntimeError("network_check_invalid")
        url = row["url"]
        host = urllib.parse.urlparse(url).hostname
        if not isinstance(url, str) or not url.startswith("https://") or not host:
            raise RuntimeError("network_url_invalid")
        allowed_hosts = row["allowed_hosts"]
        allowed_statuses = row["allowed_statuses"]
        if not isinstance(allowed_hosts, list) or host not in allowed_hosts:
            raise RuntimeError("network_allowed_hosts_invalid")
        if not isinstance(allowed_statuses, list) or not allowed_statuses:
            raise RuntimeError("network_allowed_statuses_invalid")
        started = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AgentSWE-OSWorld-Preflight/1.0"})
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        final_host = urllib.parse.urlparse(response.geturl()).hostname
        status = int(response.status)
        response.close()
        if status not in allowed_statuses or final_host not in allowed_hosts:
            raise RuntimeError(f"network_preflight_rejected:{host}:{status}")
        evidence.append({
            "target_host": host, "final_host": final_host, "status": status,
            "seconds": round(time.monotonic() - started, 3),
        })
    return {"mode": mode, "checks": evidence}
