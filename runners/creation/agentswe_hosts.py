"""Render the egress allowlists of generated Harbor task.toml files from AgentSWE configuration.

Task templates name placeholder hosts instead of real providers:
  judge.example.com   -> host of AGENTSWE_JUDGE_BASE_URL (Result-judge jobs)
  search.example.com  -> kept when the run has a search front (AGENTSWE_SEARCH_FRONT), else the host of
                         AGENTSWE_SEARCH_BASE_URL, or dropped when search is not configured
  gateway.example.com -> dropped (Candidate model traffic goes to the evaluator broker, which
                         candidate_broker_protocol.candidate_task_network adds as BROKER_HOST)
"""
from __future__ import annotations

import json
import os
import urllib.parse

JUDGE_PLACEHOLDER = "judge.example.com"
SEARCH_PLACEHOLDER = "search.example.com"
GATEWAY_PLACEHOLDER = "gateway.example.com"


def _host(url: str) -> str:
    if not url:
        return ""
    return urllib.parse.urlsplit(url).hostname or ""


def resolve_host(host: str) -> str:
    if host == JUDGE_PLACEHOLDER:
        judge = _host(os.environ.get("AGENTSWE_JUDGE_BASE_URL", ""))
        if not judge:
            raise RuntimeError("AGENTSWE_JUDGE_BASE_URL is not configured; the Result judge has no egress host")
        return judge
    if host == SEARCH_PLACEHOLDER:
        if os.environ.get("AGENTSWE_SEARCH_FRONT"):
            return SEARCH_PLACEHOLDER  # the run's TLS front answers to this name inside the Candidate project
        return _host(os.environ.get("AGENTSWE_SEARCH_BASE_URL", ""))
    if host == GATEWAY_PLACEHOLDER:
        return ""
    return host


def render_hosts(toml_text: str) -> str:
    out = []
    for line in toml_text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("allowed_hosts = "):
            hosts = json.loads(stripped.split("=", 1)[1])
            rendered: list[str] = []
            for host in hosts:
                value = resolve_host(host)
                if value and value not in rendered:
                    rendered.append(value)
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}allowed_hosts = {json.dumps(rendered)}" + ("\n" if line.endswith("\n") else "")
        out.append(line)
    return "".join(out)


def judge_environment() -> dict[str, str]:
    """Result-judge endpoint, model and effort for the eval container (the key stays in the credential file)."""
    return {k: os.environ[k] for k in ("AGENTSWE_JUDGE_RESPONSES_URL", "AGENTSWE_JUDGE_MODEL", "AGENTSWE_JUDGE_EFFORT")
            if os.environ.get(k)}
