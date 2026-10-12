#!/usr/bin/env python3
"""End-to-end check of the Builder session-limit stop (needs docker; no provider, no Harbor).

    python3 tests/e2e_builder_session_stop.py

Simulates a Builder whose ``codex exec`` is still running when the session limit is reached: inside a container from
the builder-codex image, a foreground writer (the agent) and an orphaned background writer it started keep editing
/workspace/submission. Then, as in a Creation run that ends by AgentTimeoutError, the Builder verifier takes the
submission digest (builder_contract.submission_digest, verify_builder.tree_digest) and, a little later, the controller
copies the final workspace and digests the copy (freeze_manifest digest, adapter.tree_digest); one_stop compares the
two. Without the stop (the paper path) the digests differ and one_stop's check raises "Builder modified
/workspace/submission after the controller froze it". With ProviderCodex's SESSION_STOP_SCRIPT run at the limit (the
fixed path) every agent process is gone before the verifier reads, and the two digests are equal. Everything it
starts is removed at the end.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "agentswe.owner=e2e-builder-session-stop"
PPTX = ROOT / "tasks" / "creation" / "document-to-editable-pptx" / "adapter"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stop_script() -> str:
    tree = ast.parse((ROOT / "builders" / "codex" / "codex_provider_agent.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SESSION_STOP_SCRIPT" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("SESSION_STOP_SCRIPT not found in builders/codex/codex_provider_agent.py")


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout


def freeze_check(workspace_digest_at_builder_end: str, frozen_digest: str) -> None:
    """one_stop's comparison after the freeze (Creation one_stop.py, same condition and message)."""
    if workspace_digest_at_builder_end != frozen_digest:
        raise RuntimeError(
            "Builder modified /workspace/submission after the controller froze it; "
            "the run violates the freeze protocol"
        )


AGENT = (
    "nohup bash -c 'while :; do date +%s%N >> /workspace/submission/notes.txt; sleep 0.2; done' >/dev/null 2>&1 & "
    "while :; do date +%s%N > /workspace/submission/planning.py; sleep 0.2; done"
)


def scenario(image: str, tmp: Path, stop: bool, script: str, verify_builder, adapter,
             keepalive=("tail", "-f", "/dev/null")) -> dict:
    sub = tmp / "submission"
    sub.mkdir(parents=True)
    (sub / "run_agent.py").write_text("print('candidate')\n")
    logs = tmp / "agent"
    logs.mkdir()
    cid = docker("run", "-d", "--network", "none", "--label", LABEL, "-v", f"{sub}:/workspace/submission",
                 "-v", f"{logs}:/logs/agent", image, *keepalive).strip()
    try:
        docker("exec", "-d", cid, "bash", "-c", AGENT)
        # An exec that exits leaves a real orphan under PID 1, unlike a child of
        # the still-running writer. It must also stop before verification.
        docker("exec", cid, "bash", "-c",
               "nohup bash -c 'while :; do date +%s%N >> /workspace/submission/orphan.txt; sleep 0.2; done' >/dev/null 2>&1 &")
        time.sleep(1.5)
        # The session limit is reached here: Harbor stops waiting for the agent.
        report = None
        if stop:
            out = docker("exec", cid, "bash", "-c",
                         "set -o pipefail; " + script.replace("@@OUT@@", "/logs/agent/session_limit_stop.json"))
            report = json.loads(out.strip().splitlines()[-1])
        verifier_digest = verify_builder.tree_digest(sub)  # Builder verifier: builder_contract.submission_digest
        time.sleep(2.0)  # Harbor tears down; the controller then freezes the final workspace
        frozen = tmp / "frozen_submission"
        shutil.copytree(sub, frozen, symlinks=True)
        frozen_digest = adapter.tree_digest(frozen)
        top = docker("top", cid, "-o", "pid,stat,args").splitlines()[1:]
        live = [row for row in top if " Z" not in f" {row.split()[1]}" and not any(keep in row for keep in ("tail -f /dev/null", "sh -c sleep infinity", "sleep infinity"))]
        try:
            freeze_check(verifier_digest, frozen_digest)
            freeze_error = None
        except RuntimeError as exc:
            freeze_error = str(exc)
        return {"stop": stop, "report": report, "verifier_digest": verifier_digest, "frozen_digest": frozen_digest,
                "live_processes_after": live, "freeze_error": freeze_error,
                "evidence_file": (logs / "session_limit_stop.json").is_file()}
    finally:
        docker("rm", "-f", cid, check=False)


def main() -> int:
    images = json.loads((ROOT / "images" / "images.json").read_text())["images"]
    image = images["builder-codex"]["tag"]
    if not docker("image", "ls", "-q", image).strip():
        raise SystemExit(f"image {image} is not present; run agentswe setup for any Creation task first")
    verify_builder = load("verify_builder", PPTX / "builder-template" / "tests" / "verify_builder.py")
    sys.path[:0] = [str(PPTX), str(ROOT / "runners" / "creation")]  # the adapter imports its shared helpers
    adapter = load("pptx_adapter", PPTX / "adapter.py")
    script = stop_script()
    tmp = Path(tempfile.mkdtemp(prefix="agentswe-e2e-session-stop-"))
    try:
        paper = scenario(image, tmp / "paper", False, script, verify_builder, adapter)
        fixed = scenario(image, tmp / "fixed", True, script, verify_builder, adapter)
        harbor = scenario(image, tmp / "harbor", True, script, verify_builder, adapter,
                          keepalive=("sh", "-c", "sleep infinity"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    leftovers = docker("ps", "-aq", "--filter", f"label={LABEL}").split()
    checks = {
        "paper path: agent still writes after the limit, digests differ": paper["verifier_digest"] != paper["frozen_digest"],
        "paper path: one_stop's freeze check raises the paper error": bool(paper["freeze_error"]) and
            "modified /workspace/submission after the controller froze it" in paper["freeze_error"],
        "fixed path: stop found the agent and its orphan": (fixed["report"] or {}).get("live_before", 0) >= 2,
        "fixed path: stop left no live process": (fixed["report"] or {}).get("live_after") == 0
            and not fixed["live_processes_after"],
        "fixed path: evidence written to the agent log directory": fixed["evidence_file"],
        "fixed path: verifier digest == frozen digest": fixed["verifier_digest"] == fixed["frozen_digest"],
        "fixed path: freeze check passes": fixed["freeze_error"] is None,
        "Harbor keepalive: stop evidence and no live writers": harbor["evidence_file"]
            and (harbor["report"] or {}).get("live_after") == 0 and not harbor["live_processes_after"],
        "Harbor keepalive: verifier and freeze digests match": harbor["verifier_digest"] == harbor["frozen_digest"]
            and harbor["freeze_error"] is None,
        "no container left": not leftovers,
    }
    print(json.dumps({"paper": paper, "fixed": fixed, "harbor": harbor, "checks": checks}, indent=2))
    failed = [name for name, ok in checks.items() if not ok]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
