#!/usr/bin/env python3
"""Non-network, non-Docker protocol self-test."""

from __future__ import annotations

import json
import tempfile
import difflib
import subprocess
import sys
from pathlib import Path

from protocol import LOWER_EFFORT, LOWER_MODEL, tree_digest
from code_score_runner import score
from two_round_controller import Controller


def candidate(root: Path, marker: str) -> Path:
    path = root / marker; path.mkdir()
    source = Path(__file__).resolve().parents[1] / "input/repository/LICENSE"
    old = source.read_text(encoding="utf-8").splitlines(keepends=True)
    new = list(old); new[0] = new[0].rstrip("\n") + f" ({marker})\n"
    patch = ["diff --git a/LICENSE b/LICENSE\n", "--- a/LICENSE\n", "+++ b/LICENSE\n"]
    patch.extend(difflib.unified_diff(old, new, fromfile="a/LICENSE", tofile="b/LICENSE", n=len(old)))
    (path / "solution.patch").write_text("".join(patch), encoding="utf-8")
    (path / "edit_report.json").write_text("{}\n", encoding="utf-8")
    (path / "run_report.json").write_text('{"schema_version":"1.0","artifact_paths":["solution.patch","edit_report.json","run_report.json"],"errors":[],"runtime_seconds":0,"peak_memory_bytes":0,"api_calls":{"gateway":0,"serper":0,"web_retrieval":0}}\n', encoding="utf-8")
    return path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ai-scientist-agentloop-selftest-") as temp:
        root = Path(temp); source = Path(__file__).resolve().parents[1] / "input/repository"; run = root / "run"
        (root / "credential.env").write_text("OPENAI_API_KEY=broker-only-placeholder\n", encoding="utf-8")
        controller = Controller(Path(__file__).resolve().parents[1], run, "http://127.0.0.1:1/v1/responses", dry_run=True)
        session = "self-test-builder-session"
        first = controller.submit(candidate(root, "candidate-one"), builder_session_id=session)
        assert first["accepted"] and not first["frozen"]
        assert score(root / "candidate-one")["total"] <= 100 and score(root / "candidate-one")["result_score_read"] is False
        before = controller.run_hidden(); assert before["classification"] == "protocol_error"
        accepted = [first]
        for marker in ("candidate-two", "candidate-three"):
            previous_feedback = accepted[-1]["feedback_digest"]
            next_candidate = controller.submit(
                candidate(root, marker),
                builder_session_id=session,
                feedback_digest_ack=previous_feedback,
            )
            assert next_candidate["accepted"] and not next_candidate["frozen"]
            assert next_candidate["feedback_digest_ack"] == previous_feedback
            assert next_candidate["feedback_digest"] != previous_feedback
            accepted.append(next_candidate)

        freeze = controller.freeze_latest("builder_exit")
        assert freeze["source_submission"] == 3
        assert freeze["accepted_submission_count"] == 3
        assert freeze["accepted_candidate_digests"] == [item["candidate_digest"] for item in accepted]
        assert freeze["candidate_delivery_digest"] == accepted[-1]["candidate_digest"]
        assert freeze["dev_passed_is_automatic_freeze"] is False
        after = controller.run_hidden()
        assert after["valid"] and after["frozen_digest"] == freeze["candidate_digest"]

        wrapper_result = run / "wrapper_result.json"
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("run_hidden.py")), "--run-dir", str(run), "--result", str(wrapper_result)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        wrapped = json.loads(wrapper_result.read_text(encoding="utf-8"))
        assert wrapped["valid"] and wrapped["accepted_submission_count"] == 3
        assert wrapped["frozen_digest"] == freeze["candidate_digest"]
        assert LOWER_MODEL == "deepseek-flash" and LOWER_EFFORT == "high"
        assert len(json.loads((run / "dev_lifecycle.json").read_text())["records"]) == 3
        assert tree_digest(run / "candidates/candidate_001") != tree_digest(run / "candidates/candidate_002")
    print("self-test: PASS; 3 accepted candidates, fresh feedback chain, latest freeze, hidden-before-freeze rejection")
    return 0


if __name__ == "__main__": raise SystemExit(main())
