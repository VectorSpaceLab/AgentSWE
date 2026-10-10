#!/usr/bin/env python3
"""Non-heavy Stage-A protocol self-test; it performs no real provider call."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluator" / "harness"))
from broker_server import BrokerState, EFFORT, MODEL
from candidate_adapter import tree_digest
from controller import CandidateController, ProtocolError
from public_package import prepare
from case_specs import HIDDEN_CASES
from builder_lifecycle import BuilderSession


def main() -> int:
    lock = json.loads((ROOT / "protocol_lock.json").read_text(encoding="utf-8"))
    assert lock["lower_agent"]["model"] == MODEL and lock["lower_agent"]["reasoning_effort"] == EFFORT
    inventory = json.loads((ROOT / "evaluator" / "hidden_inventory.json").read_text(encoding="utf-8"))
    assert len(inventory["cases"]) == 6
    assert {x["case_id"] for x in inventory["cases"]} == {f"test_{i:03d}" for i in range(1, 7)}
    broker = BrokerState("https://invalid.example", None, dry_run=True)
    broker.record(ok=True, status=200, payload={"usage": {"input_tokens": 2, "output_tokens": 3}})
    stats = broker.stats()
    assert stats["runtime"]["successful_calls"] == 1 and stats["tokens"]["total"] == 5
    assert all(stats["runtime"][name] == 0 for name in (
        "provider_failures", "credential_failures", "protocol_failures", "broker_failures"
    ))
    builder_broker = BrokerState("https://invalid.example", None, dry_run=True, effort="max")
    assert builder_broker.stats()["protocol"]["reasoning_effort"] == "max"
    with tempfile.TemporaryDirectory(prefix="deepcode-agentloop-selftest-") as raw:
        tmp = Path(raw); base = tmp / "base"; base.mkdir()
        (base / "deepcode.py").write_text("# base\n", encoding="utf-8")
        (base / "cli").mkdir(); (base / "core").mkdir()
        c1, c2 = tmp / "c1", tmp / "c2"
        for candidate, marker in ((c1, "one"), (c2, "two")):
            candidate.mkdir(); (candidate / "deepcode.py").write_text(marker, encoding="utf-8")
            (candidate / "cli").mkdir(); (candidate / "core").mkdir()
        controller = CandidateController(base_repository=base, run_dir=tmp / "run", launcher=ROOT / "evaluator/harness/deepcode_lower_agent.py", broker_endpoint=None, dry_run=True)
        session = BuilderSession(controller, session_id="builder-session-selftest", require_feedback_ack=True)
        session.submit(c1)
        try:
            session.submit(c2)
        except ProtocolError:
            pass
        else:
            raise AssertionError("same-session lifecycle allowed Candidate 2 before feedback")
        feedback = session.consume_feedback()
        session.submit(c2, feedback_digest_ack=feedback["feedback_digest"])
        frozen = session.freeze()
        assert frozen["hidden_allowed"] and tree_digest(Path(frozen["candidate_path"])) == frozen["candidate_digest"]
        try:
            CandidateController(base_repository=base, run_dir=tmp / "run2", launcher=controller.launcher, broker_endpoint=None, dry_run=True).run_hidden("test_001")
        except ProtocolError:
            pass
        else:
            raise AssertionError("hidden execution was allowed before freeze")
        assert frozen["source_submission"] == 2 and isinstance(frozen["frozen_at"], str)
        assert frozen["frozen_tree_read_only"] and frozen["frozen_digest_stable"]

        # Exercise the complete local post-freeze executor without a provider.
        # The fake launcher accepts the real executor's arguments and writes a
        # contract-valid terminal artifact plus evaluator-shaped broker stats;
        # no network, Docker, or external model is contacted.
        fake_launcher = tmp / "fake_lower_launcher.py"
        fake_launcher.write_text(
            "import argparse, json\n"
            "from pathlib import Path\n"
            "p=argparse.ArgumentParser(); p.add_argument('--output'); p.add_argument('--workspace'); p.add_argument('--case-id'); a,_=p.parse_known_args()\n"
            "out=Path(a.output); ws=Path(a.workspace); out.mkdir(parents=True, exist_ok=True); ws.mkdir(parents=True, exist_ok=True)\n"
            "stats={'schema_version':'deepcode-agentloop-broker-stats-v1','protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high','credential_owner':'evaluator-broker'},'runtime':{'calls':0,'failures':0,'successful_calls':0,'started_at':0},'calls':[],'failures':[],'tokens':{'input':0,'output':0,'total':0}}\n"
            "json.dump(stats, open(out/'broker_before.json','w')); stats['runtime']={'calls':1,'failures':0,'successful_calls':1,'started_at':0}; json.dump(stats, open(out/'broker_after.json','w'))\n"
            "json.dump({'schema_version':'deepcode-agentloop-result/v1','case_id':a.case_id,'observations':[]}, open(ws/'agent_result.json','w'))\n"
            "json.dump({'schema_version':'deepcode-agentloop-case-result-v1','case_id':a.case_id,'contract_valid':True,'classification':'candidate_valid','broker_delta':{'calls':1,'failures':0,'successful_calls':1},'model_protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high'},'credential_isolation':{'candidate_credential':'placeholder-only','real_credential_exposed':False},'runtime':{'sandbox':{'mechanism':'self-test'}}}, open(out/'result.json','w'))\n",
            encoding="utf-8",
        )
        executor = CandidateController(
            base_repository=base,
            run_dir=tmp / "executor-run",
            launcher=fake_launcher,
            broker_endpoint="http://127.0.0.1:1/v1/responses",
            hidden_root=ROOT / "test_cases",
            dry_run=True,
        )
        executor.submit(c1); executor.submit(c2); executor.freeze()
        hidden = executor.run_all_hidden()
        assert [item["case_id"] for item in hidden] == HIDDEN_CASES
        assert all(item["classification"] == "candidate_valid" for item in hidden)
        assert all(item["hidden_started_after_freeze"] and item["frozen_digest_stable"] for item in hidden)
        assert all(all((tmp / "executor-run" / "hidden" / item["case_id"] / name).is_file() for name in (
            "launcher.json", "broker_evidence.json", "isolation_evidence.json",
            "terminal_artifact_evidence.json", "case-attestation.json", "result.json"
        )) for item in hidden)
        attestation = json.loads((tmp / "executor-run" / "hidden-after-freeze-attestation.json").read_text())
        assert attestation["complete"] and attestation["all_six_dispatched"]
        public = prepare(ROOT, tmp / "public")
        assert (public / "input/repository/deepcode.py").is_file()
        assert not (public / "test_cases").exists() and not (public / "evaluator").exists()
    print("self-test: PASS; protocol lock, 2+6 inventory, broker stats, distinct digests, freeze fence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
