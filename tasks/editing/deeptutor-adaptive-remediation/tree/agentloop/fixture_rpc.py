"""Trusted host client for a case-agnostic, networkless product worker."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
try:
    from .isolated_runtime import sandbox_command
except ImportError:
    from isolated_runtime import sandbox_command


def _worker_failure_text(result):
    """Name the product exception class the worker already reported.

    fixture_worker.serve() (fixture_worker.py:96-98) replies
    {"ok": false, "error_type": type(exc).__name__, "reason": str(exc)}.
    Dropping error_type turned a product-side KeyError into a bare
    "slice(None, 50, None)" in fixture-failure.json, fixture-preparation-trace.json,
    case_result.reason and result_score_contract.reason: every record then named
    RuntimeError -- this wrapper's own class -- and read like an evaluator fault.
    Purely diagnostic; the caller, the classification and the score are unchanged.
    """
    reason = str(result.get("reason", "invalid response binding"))
    kind = result.get("error_type")
    if isinstance(kind, str) and kind and not reason.startswith(kind + ":"):
        return kind + ": " + reason
    return reason


class ProductWorker:
    def __init__(self, repository: Path, output: Path, *, python=None):
        self.repository, self.output = repository.resolve(), output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.python = python or sys.executable
        runtime = Path(os.environ["AGENTSWE_FIXTURE_RUNTIME_ROOT"]).resolve()
        home = Path(os.environ["DEEPTUTOR_HOME"]).resolve()
        runtime.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        for name in ("home", "tmp", "cache", "config"):
            (runtime / name).mkdir(exist_ok=True)
        worker = Path(__file__).with_name("fixture_worker.py")
        extra_home = [] if home.is_relative_to(runtime) else [home]
        command, self.boundary = sandbox_command([self.python, "-I", str(worker), "--repository", str(self.repository)],
            self.repository, runtime, self.python, readonly=[worker], writable=extra_home, repository_readonly=True)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(runtime / "home"),
            "TMPDIR": str(runtime / "tmp"), "XDG_CACHE_HOME": str(runtime / "cache"),
            "XDG_CONFIG_HOME": str(runtime / "config"), "DEEPTUTOR_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        # The host opens this write-only channel. Its containing private fixture
        # directory is not mounted into the child, nor is the controller source.
        self.stderr = (self.output / "worker-stderr.log").open("ab")
        self.process = subprocess.Popen(command, cwd="/", env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0)
        self.sequence, self.buffer = 0, b""
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        (self.output / "worker-boundary.json").write_text(json.dumps(self.boundary, indent=2) + "\n")

    def call(self, method, **arguments):
        self.sequence += 1
        self.process.stdin.write((json.dumps({"id": self.sequence, "method": method, "arguments": arguments}) + "\n").encode())
        deadline = time.monotonic() + 60
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("isolated product fixture RPC timed out")
            piece = os.read(self.process.stdout.fileno(), 65536)
            if not piece:
                raise RuntimeError("isolated product fixture worker exited before reply")
            self.buffer += piece
            if len(self.buffer) > 16 * 1024 * 1024:
                raise RuntimeError("isolated product fixture RPC exceeded bounded response size")
        line, self.buffer = self.buffer.split(b"\n", 1)
        result = json.loads(line)
        if result.get("id") != self.sequence or result.get("ok") is not True:
            raise RuntimeError("isolated product operation failed: " + _worker_failure_text(result))
        return result["value"]

    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        self.stderr.close()
