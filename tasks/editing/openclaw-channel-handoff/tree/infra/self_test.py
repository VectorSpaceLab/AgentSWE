#!/usr/bin/env python3
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from controller.builder_session_controller import PROBE_MARKER, validate_delivery
from controller.two_round_controller import DEV,HIDDEN,TwoRoundController,tree_digest,tree_is_read_only,validate_freeze_manifest
from evaluator.code_score_runner import WEIGHTS,score
from evaluator.formal_gates import validate_broker_protocol
def main()->int:
    lock=json.loads((ROOT/"schemas/protocol_lock.json").read_text()); assert lock["model"]=="deepseek-flash" and lock["reasoning_effort"]=="high" and lock["hidden_after_freeze"] is True; assert lock["builder_model"]=="deepseek-flash" and lock["builder_reasoning_effort"]=="max"
    inv=json.loads((ROOT/"dev_cases/agentloop_inventory.json").read_text()); assert [x["case_id"] for x in inv["cases"]]==list(DEV); assert len(HIDDEN)==6
    assert sum(WEIGHTS.values())==100 and score(WEIGHTS)["total"]==100
    assert (ROOT/"harbor/formal_one_stop.py").is_file(); assert validate_broker_protocol({"protocol":{"model":"deepseek-flash","reasoning_effort":"high","transport":"evaluator-owned-responses-broker"}})==[]
    for p in ROOT.joinpath("test_cases").glob("test_*/input.md"): assert p.is_file() and "oracle" not in p.read_text().lower()
    with tempfile.TemporaryDirectory() as td:
        base=Path(td); c1=base/"c1"; c2=base/"c2"; c1.mkdir(); c2.mkdir(); (c1/"x").write_text("one"); (c2/"x").write_text("two"); seen=[]
        def evaluate(n,path): seen.append(n); return {k:{"score":0,"broker":{"calls":0,"failures":0,"tokens":0},"classification":"smoke-not-run"} for k in DEV}
        ctl=TwoRoundController(base/"run",evaluate,max_dev_rounds=2); ctl.submit(c1); ctl.submit(c2); frozen=ctl.freeze_candidate_2(); assert seen==[1,2] and frozen["accepted_submission_count"]==2 and len(frozen["accepted_candidate_digests"])==2; assert tree_is_read_only(base/"run"/"frozen_candidate"); validate_freeze_manifest(frozen,base/"run"/"frozen_candidate")
        hidden = ctl.run_hidden(lambda _: {case:{"classification":"smoke-not-run"} for case in HIDDEN}); assert set(hidden)==set(HIDDEN); assert json.loads((base/"run"/"hidden-after-freeze-attestation.json").read_text())["candidate_digest_stable"] is True
        try:
            ctl.run_hidden(lambda _: {case:{} for case in HIDDEN})
        except RuntimeError as exc:
            assert "replay" in str(exc)
        else: raise AssertionError("hidden replay was accepted")
    with tempfile.TemporaryDirectory() as td:
        delivery=Path(td); changed="src/gateway/self-test.ts"; patch=(delivery/"solution.patch"); patch.write_text(f"diff --git a/{changed} b/{changed}\n--- /dev/null\n+++ b/{changed}\n@@ -0,0 +1 @@\n+ok\n", encoding="utf-8")
        (delivery/"edit_report.json").write_text(json.dumps({"summary":"ok","changed_paths":[changed],"production_seams":[changed],"known_limits":[]}), encoding="utf-8")
        (delivery/"run_report.json").write_text(json.dumps({"status":"ok","commands":[],"tests":[],"errors":[]}), encoding="utf-8")
        assert validate_delivery(delivery)==[]
        patch.write_text(patch.read_text()+PROBE_MARKER, encoding="utf-8"); assert validate_delivery(delivery)
    print("SELF_TEST=PASS; inventory=2+6; lifecycle=Candidate1->dev2->Candidate2->freeze->hidden; immutable_freeze=PASS; replay_gate=PASS; code_total=100; broker_success_calls=0 (not run)"); return 0
if __name__=="__main__": raise SystemExit(main())
