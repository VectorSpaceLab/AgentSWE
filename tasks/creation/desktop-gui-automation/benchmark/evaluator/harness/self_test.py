#!/usr/bin/env python3
"""Syntax-independent fixture and active-case-staging smoke tests."""

from __future__ import annotations

import json
import secrets
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .run_case import AuditProxy, bridge_state, stage_input, terminate_group, wait_for_health
except ImportError:
    from run_case import AuditProxy, bridge_state, stage_input, terminate_group, wait_for_health


ROOT = Path(__file__).resolve().parents[2]


def assert_fixture(case_dir: Path) -> None:
    token = secrets.token_urlsafe(32)
    process = subprocess.Popen(
        [
            sys.executable,
            str(case_dir / "assets" / "serve.py"),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--evaluator-token",
            token,
        ],
        cwd=case_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    proxy = None
    try:
        assert process.stdout is not None
        info = json.loads(process.stdout.readline())
        assert info["case_id"] == case_dir.name
        wait_for_health(info["url"], case_dir.name)
        protected = bridge_state(info["url"], token)
        assert protected["case_id"] == case_dir.name
        with tempfile.TemporaryDirectory(prefix="gui-harness-self-test-") as temporary:
            root = Path(temporary)
            audit = root / "requests.jsonl"
            audit.write_text("", encoding="utf-8")
            proxy = AuditProxy(info["url"], audit)
            proxy.start()
            with urllib.request.urlopen(proxy.url + "/health", timeout=5) as response:
                assert json.load(response)["case_id"] == case_dir.name
            try:
                urllib.request.urlopen(proxy.url + "/__evaluator__/state", timeout=5)
                raise AssertionError("proxy exposed the evaluator bridge")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            staged = root / "active"
            manifest = stage_input(case_dir / "input.md", staged, proxy.url)
            staged_text = (staged / "input.md").read_text(encoding="utf-8")
            assert "assets/serve.py" not in staged_text
            assert manifest["application_links_rewritten"] == 1
            assert not (staged / "assets").exists()
    finally:
        if proxy is not None:
            proxy.close()
        terminate_group(process, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_group(process, signal.SIGKILL)
            process.communicate()


def main() -> int:
    cases = sorted((ROOT / "dev_cases").glob("dev_*")) + sorted((ROOT / "test_cases").glob("test_*"))
    assert len(cases) == 8, f"expected 2 dev and 6 hidden cases, found {len(cases)}"
    for case in cases:
        assert_fixture(case)
        print(f"PASS {case.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
