#!/usr/bin/env python3
"""Dependency-free lifecycle and isolation controls.

This test never starts Docker, Harbor, a provider, or the real OpenHands
runtime. It exercises ordering and evidence gates with a local fake lower
launcher only; it is not a benchmark Result.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluator.controller.two_round_controller import TwoRoundController  # noqa: E402
from harbor.formal_one_stop import BuilderLifecycle, builder_attestation  # noqa: E402
from harbor.stage_public_package import stage as stage_public_package  # noqa: E402
from evaluator.broker.candidate_broker import Stats  # noqa: E402


FAKE_RUNNER = r'''#!/usr/bin/env python3
import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument("--output", type=Path); p.add_argument("--case"); a,_=p.parse_known_args()
a.output.mkdir(parents=True, exist_ok=True)
stats=Path(os.environ["FAKE_STATS"])
value=json.loads(stats.read_text())
runtime=value["runtime"]
runtime["calls"] += 1; runtime["successful_calls"] += 1; runtime["total_tokens"] += 7
stats.write_text(json.dumps(value))
case=Path(a.case).name
(a.output/"agent_result.json").write_text(json.dumps({"schema_version":"agentswe-openhands-agent-result/v1","case_id":case,"model":"deepseek-flash","reasoning_effort":"high","actions":["inspect"],"observations":[],"decision":"complete","rationale":"synthetic self-test only"}))
(a.output/"launcher_result.json").write_text(json.dumps({"schema_version":"agentswe-openhands-lower-launch/v3","case_id":case,"classification":"valid_behavior","broker_endpoint_is_evaluator_owned":True,"credential_seen_by_candidate":"broker-only-placeholder","model_protocol":{"model":"deepseek-flash","reasoning_effort":"high"}}))
'''


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    formal_source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
    for marker in (
        "--run-formal", "--role", "--reasoning-effort", "max",
        "builder_public_package", "AGENTSWE_DEV_CONTROLLER_SOCKET",
        "feedback_digest", "single_continuous_session", "run_all_hidden",
        "hidden-after-freeze-attestation.json",
    ):
        assert marker in formal_source, f"formal lifecycle marker missing: {marker}"
    legacy_source = (ROOT / "evaluator/controller/minimal_smoke_controller.py").read_text(encoding="utf-8")
    assert "external/synthetic Candidate lifecycle is disabled" in legacy_source
    lower_source = (ROOT / "lower_agent/openhands_lower_agent.py").read_text(encoding="utf-8")
    for marker in (
        "@openhands/agent-canvas", "recovery-evaluator-adapter.ts",
        "broker-only-placeholder", "external_coding_agent_substituted",
    ):
        assert marker in lower_source, f"lower product marker missing: {marker}"
    with tempfile.TemporaryDirectory(prefix="openhands-public-package-self-test-") as package_raw:
        package = Path(package_raw) / "public"
        manifest = stage_public_package(ROOT, package)
        assert manifest["visible_roots"] == ["input", "dev_cases"]
        assert manifest["hidden_mounted"] is False
        assert manifest["evaluator_mounted"] is False
        assert not (package / "test_cases").exists()
        assert not (package / "evaluator").exists()
    with tempfile.TemporaryDirectory(prefix="openhands-broker-schema-self-test-") as broker_raw:
        lower_stats = Stats(Path(broker_raw) / "lower.json", effort="high", role="lower")
        builder_stats = Stats(Path(broker_raw) / "builder.json", effort="max", role="builder")
        assert lower_stats.value["protocol"]["reasoning_effort"] == "high"
        assert builder_stats.value["protocol"]["reasoning_effort"] == "max"
        assert lower_stats.value["role"] == "lower"
        assert builder_stats.value["role"] == "builder"
    fake_lifecycle = SimpleNamespace(
        session_id="builder-session-self-test",
        invocation_id="harbor-invocation-self-test",
        events=[
            {"event":"builder_invocation_started", "builder_session_id":"builder-session-self-test", "builder_invocation_id":"harbor-invocation-self-test", "epoch_ns":1},
            {"event":"candidate_accepted", "builder_session_id":"builder-session-self-test", "builder_invocation_id":"harbor-invocation-self-test", "epoch_ns":2},
            {"event":"feedback_delivered", "builder_session_id":"builder-session-self-test", "builder_invocation_id":"harbor-invocation-self-test", "epoch_ns":3},
            {"event":"candidate_accepted", "builder_session_id":"builder-session-self-test", "builder_invocation_id":"harbor-invocation-self-test", "epoch_ns":4, "feedback_digest_ack":"feedback"},
            {"event":"feedback_delivered", "builder_session_id":"builder-session-self-test", "builder_invocation_id":"harbor-invocation-self-test", "epoch_ns":5, "candidate":2, "feedback_digest":"feedback-2"},
            {"event":"builder_invocation_ended", "builder_session_id":"builder-session-self-test", "builder_invocation_id":"harbor-invocation-self-test", "epoch_ns":6},
        ],
        attempts=[],
        controller=SimpleNamespace(records=[
            {"candidate_digest":"a"*64, "feedback_digest":"feedback", "dev":{"dev_001":{},"dev_002":{}}},
            {"candidate_digest":"b"*64, "feedback_digest":"feedback-2", "dev":{"dev_001":{},"dev_002":{}}},
        ], frozen=True, freeze_manifest={"freeze_reason":"builder_exit"},
        max_dev_rounds=10, n_concurrent=1, infrastructure_attempts=[]),
    )
    attestation = builder_attestation(fake_lifecycle, builder_exit_code=0, started_ns=0, ended_ns=6)
    assert attestation["complete"] is True
    with tempfile.TemporaryDirectory(prefix="openhands-lifecycle-self-test-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "candidate.txt").write_text("candidate-1\n", encoding="utf-8")
        cases = root / "cases"
        for group, names in (("dev_cases", ("dev_001", "dev_002")), ("test_cases", tuple(f"test_{i:03d}" for i in range(1, 7)))):
            for name in names:
                (cases / group / name).mkdir(parents=True)
                (cases / group / name / "input.md").write_text(f"{name}\n", encoding="utf-8")
        node_modules = root / "node_modules"
        node_modules.mkdir()
        stats = root / "stats.json"
        write_json(stats, {"runtime":{"calls":0,"failures":0,"successful_calls":0,"input_tokens":0,"output_tokens":0,"total_tokens":0}})
        fake = root / "fake_runner.py"
        fake.write_text(FAKE_RUNNER, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        os.environ["FAKE_STATS"] = str(stats)
        controller = TwoRoundController(
            workspace, root / "run", [sys.executable, str(fake)], case_root=cases,
            broker_endpoint="http://fake.invalid/v1/responses", node_modules=node_modules,
            stats_file=stats, timeout=5,
        )
        first = controller.submit()
        assert set(first["dev"]) == {"dev_001", "dev_002"}
        try:
            controller.run_hidden("test_001")
        except RuntimeError as exc:
            assert "before accepted Candidate freeze" in str(exc)
        else:
            raise AssertionError("hidden ran before freeze")
        (workspace / "candidate.txt").write_text("candidate-2-feedback-revision\n", encoding="utf-8")
        second = controller.submit(feedback_digest_ack=first["feedback_digest"])
        assert set(second["dev"]) == {"dev_001", "dev_002"}
        assert first["candidate_digest"] != second["candidate_digest"]
        controller.freeze_latest("builder_exit")
        manifest = json.loads((root / "run" / "freeze_manifest.json").read_text())
        assert manifest["schema_version"] == "agentswe-freeze-manifest/v1"
        assert manifest["source_submission"] == controller.records[-1]["submission"]
        assert manifest["accepted_submission_count"] == len(controller.records)
        assert manifest["hidden_allowed"] is True
        frozen = root / "run" / "frozen_candidate"
        assert not (frozen / "candidate.txt").stat().st_mode & stat.S_IWUSR
        hidden = controller.run_all_hidden()
        assert [item["case_id"] for item in hidden] == [f"test_{i:03d}" for i in range(1, 7)]
        assert all(item["freeze_proof"]["started_strictly_after_freeze"] for item in hidden)
        assert all(item["freeze_proof"]["digest_stable"] for item in hidden)
        attestation = json.loads((root / "run" / "hidden-after-freeze-attestation.json").read_text())
        assert attestation["complete"] is True
        assert attestation["executed_cases"] == [f"test_{i:03d}" for i in range(1, 7)]

        # Exercise the Builder facade itself without materialization, Harbor,
        # Docker, a provider, or a real lower process. The patched method still
        # goes through the real controller's feedback/digest/freeze gates.
        builder_workspace = root / "builder-workspace"
        builder_workspace.mkdir()
        (builder_workspace / "candidate.txt").write_text("builder-candidate-1\n", encoding="utf-8")
        lifecycle = BuilderLifecycle(
            run_dir=root / "builder-run", workspace=builder_workspace,
            lower_endpoint="http://fake.invalid/v1/responses", node_modules=node_modules, timeout=5,
        )
        lifecycle.controller.runner = [sys.executable, str(fake)]
        lifecycle.controller.stats_file = stats
        product_counter = {"value": 0}

        def fake_materialize(number: int, attempt: int):
            product_counter["value"] += 1
            product = root / f"product-{product_counter['value']}"
            shutil.copytree(builder_workspace, product)
            return 0, product, {"attempt": attempt, "exit_code": 0}

        lifecycle._materialize = fake_materialize
        code, first_payload = lifecycle.submit(None)
        assert code == 200 and first_payload["candidate"] == 1
        feedback_digest = first_payload["feedback_digest"]
        wrong_code, _ = lifecycle.submit("0" * 64)
        assert wrong_code == 409
        (builder_workspace / "candidate.txt").write_text("builder-candidate-2-after-feedback\n", encoding="utf-8")
        code, second_payload = lifecycle.submit(feedback_digest)
        assert code == 200 and second_payload["candidate"] == 2
        lifecycle.controller.freeze_latest("builder_exit")
        assert lifecycle.controller.frozen is True
        accepted = [event for event in lifecycle.events if event["event"] == "candidate_accepted"]
        feedback_events = [
            event for event in lifecycle.events
            if event["event"] == "feedback_delivered" and event.get("candidate") == 1
        ]
        assert len(accepted) == 2 and len(feedback_events) == 1
        assert feedback_events[0]["epoch_ns"] < accepted[1]["epoch_ns"]
        assert accepted[1]["feedback_digest_ack"] == feedback_digest
    print(json.dumps({"status":"PASS","checks":["2 public per Candidate","pre-freeze hidden rejection","distinct digest","immutable freeze","6 hidden after freeze","hidden attestation","same-session feedback digest gate"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
