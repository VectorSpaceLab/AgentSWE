#!/usr/bin/env python3
"""Secret-safe health probes for the two authorized runtime providers."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import PROTOCOL_VERSION, write_json


RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> tuple[int | None, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # The GATEWAY edge rejects urllib's default user agent even when the
            # same credential and payload succeed through requests.
            "User-Agent": "python-requests/2.32.0",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
            return response.status, "ok" if 200 <= response.status < 300 else "http_error"
    except urllib.error.HTTPError as exc:
        exc.read(4096)
        return exc.code, "http_error"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, type(exc).__name__


def probe(name: str, attempts: int, timeout: float) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    if name == "gateway":
        key = os.getenv("GATEWAY_API_KEY")
        if not key:
            return {"provider": name, "status": "not_configured", "attempts": observations}
        url = "https://gateway.example.com/v1/responses"
        payload = {"model": "gpt-5.6-sol", "reasoning": {"effort": "medium"}, "max_output_tokens": 32, "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Reply with OK."}]}]}
        headers = {"Authorization": f"Bearer {key}"}
    elif name == "serper":
        token = os.getenv("SERPER_TOKEN")
        if not token:
            return {"provider": name, "status": "not_configured", "attempts": observations}
        url = "https://search.example.com/serp_search_v1"
        payload = {"query": "site:example.com evaluator provider health", "page": 1, "search_type": "search", "token": token}
        headers = {}
    else:
        raise ValueError(name)
    for number in range(1, attempts + 1):
        started = time.monotonic()
        status_code, outcome = post_json(url, payload, headers, timeout)
        observations.append({"attempt": number, "status_code": status_code, "outcome": outcome, "elapsed_seconds": round(time.monotonic() - started, 3)})
        if status_code is not None and 200 <= status_code < 300:
            break
        if number < attempts:
            time.sleep(min(3.0, number * 1.5))
    if any(item["status_code"] is not None and 200 <= item["status_code"] < 300 for item in observations):
        status = "available"
    elif len(observations) >= 2 and all(item["status_code"] in RETRYABLE or item["status_code"] is None for item in observations):
        status = "unavailable_retryable"
    else:
        status = "unavailable_nonretryable"
    return {"provider": name, "status": status, "attempts": observations}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()
    result = {
        "protocol_version": PROTOCOL_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "providers": [probe("gateway", max(2, args.attempts), args.timeout), probe("serper", max(2, args.attempts), args.timeout)],
        "note": "No credential, response body, or provider-generated content is persisted.",
    }
    write_json(args.output.resolve(), result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
