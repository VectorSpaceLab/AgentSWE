#!/usr/bin/env python3
"""Non-heavy static contract self-test; never contacts a provider or Docker."""
from __future__ import annotations
import hashlib,json,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def check(condition:bool,message:str)->None:
    if not condition: raise AssertionError(message)

def main()->int:
    lock=json.loads((ROOT/"protocol_lock.json").read_text()); check(lock["lower_agent"]["product"]=="Aider","lower product"); check(lock["lower_agent"]["model"]=="deepseek-flash","model lock"); check(lock["lower_agent"]["reasoning_effort"]=="high","effort lock")
    check(lock["inventory"]["public_dev"]==["dev_001","dev_002"],"dev inventory"); check(len(lock["inventory"]["hidden"])==6,"hidden inventory")
    broker=ROOT/"evaluator/broker/responses_broker.py"; case=ROOT/"evaluator/case_runtime.py"; launcher=ROOT/"evaluator/harness/run_lower_agent_case.py"; scorer=ROOT/"evaluator/harness/score_agent_case.py"; controller=ROOT/"harbor/agentloop_controller.py"
    launcher_text=launcher.read_text(encoding="utf-8"); check('"--edit-format","diff"' in launcher_text,"lower uses shell-capable diff format"); check('"--analytics-disable"' in launcher_text,"lower disables analytics"); check('"artifact_contract"' in launcher_text and '"lower_product_workspace"' in launcher_text,"artifact provenance is explicit"); check('failures > 0' in launcher_text and '"infrastructure-invalid"' in launcher_text,"broker failures are infrastructure")
    finalizer_text=(ROOT/"evaluator/formal_finalize.py").read_text(encoding="utf-8"); check("RESULT_JUDGE" in finalizer_text and "CODE_JUDGE" in finalizer_text,"Result and Code judges are independent"); check("score_agent_case.py" not in finalizer_text,"native scorer is not formal Result wiring")
    scorer_text=scorer.read_text(encoding="utf-8"); check('"native_diagnostic_only"' in scorer_text and '"result_publishable": False' in scorer_text,"native scorer is diagnostic-only")
    formal_text=(ROOT/"harbor/formal_one_stop.py").read_text(encoding="utf-8"); check("cleanup_owned_containers" in formal_text and '"absent_after_cleanup"' in formal_text,"cleanup attestation verifies owned containers")
    check(formal_text.index("hidden = controller.controller.run_hidden()") < formal_text.index("finalized = subprocess.run"), "Result judge invocation starts after hidden execution")
    subprocess.run([sys.executable,"-m","py_compile",str(broker),str(case),str(launcher),str(scorer),str(controller),str(ROOT/"harbor/formal_one_stop.py"),str(ROOT/"evaluator/materialize_candidate.py"),str(ROOT/"evaluator/run_code_rubric.py"),str(ROOT/"evaluator/formal_finalize.py")],check=True)
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); spec={"case_id":"dev_001","scenario":"smoke","allowed_actions":["inspect","create","status"]}; (root/"spec.json").write_text(json.dumps(spec)); sys.path.insert(0,str(ROOT)); from evaluator.case_runtime import CaseRuntime,RuntimeServer
        runtime=CaseRuntime(spec,root/"case",root/"state.json"); server=RuntimeServer(runtime); server.start(); source=runtime.client_source(f"http://127.0.0.1:{server.port}"); check("/action" in source and "worktree_plan_adapter" in source,"client contract"); server.stop()
        from evaluator.materialize_candidate import validate_delivery
        delivery=root/"delivery"; delivery.mkdir()
        (delivery/"solution.patch").write_text("placeholder\n")
        (delivery/"edit_report.json").write_text(json.dumps({"schema_version":1,"changed_paths":["aider/worktree_plan_adapter.py","tests/test_worktree_plan_adapter.py"],"summary":"smoke","tests":[]}))
        (delivery/"run_report.json").write_text(json.dumps({"schema_version":1,"status":"ok","commands":[],"duration_seconds":0,"errors":[],"deepseek":0,"gateway":0,"gateway_image":0,"serper":0,"web_retrieval":0}))
        paths=["aider/worktree_plan_adapter.py","tests/test_worktree_plan_adapter.py"]
        check(validate_delivery(delivery,paths)==[],"valid delivery contract")
        check("patch must include focused source-adjacent Python tests" in validate_delivery(delivery,["aider/worktree_plan_adapter.py"]),"delivery test requirement")
    cases=json.loads((ROOT/"evaluator/agentloop_cases.json").read_text()); check(list(cases["public_dev"]) == ["dev_001", "dev_002"], "public case inventory"); check(len(cases["hidden"]) == 6, "hidden case inventory")
    print("SELF_TEST=PASS"); print("REAL_BROKER_CALLS=0"); print("STATUS=PARTIAL"); return 0

if __name__=="__main__": raise SystemExit(main())
