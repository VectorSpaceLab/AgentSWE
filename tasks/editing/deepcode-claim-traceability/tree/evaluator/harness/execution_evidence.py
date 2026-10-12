"""DeepCode-specific artifact validation, separate from Result judging."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

def validate_artifact(artifact: Path, trajectory: Path | None, case_id: str, *, preexisting=False):
    errors=[]
    try: value=json.loads(artifact.read_text())
    except (OSError,ValueError): return ["missing or invalid product artifact"]
    if not isinstance(value,dict): return ["product artifact must be object"]
    # Structural routing only: ordinary quality/schema/claim errors belong to Result.
    meaningful = {k:v for k,v in value.items() if k not in {"schema_version","case_id","operation_id","tenant_id","project_id"}
                  and v not in (None,"",[],{})}
    if not meaningful: errors.append("product artifact contains no substantive task result")
    if preexisting: errors.append("artifact preexisted product launch")
    if trajectory is None or not trajectory.is_file(): errors.append("raw product trajectory missing")
    else:
        product_events=[]
        for line in trajectory.read_text().splitlines():
            try:
                event=json.loads(line); msg=event.get("msg",{})
                if msg.get("type") in ("agent_message","tool_completed"): product_events.append(msg)
            except (ValueError,AttributeError): continue
        if not product_events: errors.append("no actual product assistant/tool events")
    return errors

def attest(record, *, output, workspace, case_id, candidate_digest):
    result=dict(record); delta=result.get("broker_delta") or {}
    attempted=record.get("execution_attempted") is True
    artifact=workspace/"agent_result.json"; trajectory=output/"stdout.jsonl"
    errors=validate_artifact(artifact,trajectory,case_id,preexisting=record.get("artifact_preexisting_before_launch") is True)
    result.update({"case_id":case_id,"candidate_digest":candidate_digest,
        "real_execution":attempted and int(delta.get("successful_calls",0) or 0)>0,
        "artifact_validation":{"validated_by":"evaluator","valid":not errors,
            "sha256":hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact.is_file() else None,"errors":errors}})
    try: value=json.loads(artifact.read_text())
    except (OSError,ValueError): value={}
    format_errors=[]
    if isinstance(value,dict):
        if value.get("schema_version")!="deepcode-agentloop-result/v1" or value.get("case_id")!=case_id:
            format_errors.append("DeepCode artifact schema/case mismatch")
        for key in ("observations","tool_trajectory_summary","state_receipts","artifact_paths"):
            if not isinstance(value.get(key),list):format_errors.append("missing task-native list "+key)
        if not isinstance(value.get("decision"),dict) or not isinstance(value.get("safety"),dict):
            format_errors.append("missing task-native decision/safety object")
    result["artifact_validation"].update(quality_schema_findings=format_errors,
        validation_scope="parseable substantive product artifact and actual native trajectory provenance; Result grades completeness, claims, and requested schema")
    if not errors and result.get("infra_valid") is True and result["real_execution"]:
        result.update(classification="candidate_partial" if format_errors or record.get("classification")!="candidate_valid" else "candidate_valid",contract_valid=True)
        result.pop("failure_attribution",None)
    elif record.get("classification")=="candidate_behavior_failure" and attempted and record.get("environment_preflight",{}).get("valid") is True:
        fatal = any(e in errors for e in ("missing or invalid product artifact","product artifact must be object","product artifact contains no substantive task result"))
        if fatal:
            result["failure_attribution"]={"party":"candidate","observed_by":"evaluator","fatal":True,
                "reason":"DeepCode completed attempted execution after healthy preflight without a parseable substantive task artifact",
                "evidence_paths":[str(output/"execution_observation.json")]}
        else:
            result.update(classification="evaluator_infrastructure_error",infra_valid=False)
            result["failure_attribution"]={"party":"evaluator","observed_by":"evaluator","fatal":False,
                "reason":"Product artifact provenance could not be established: "+"; ".join(errors)}
    return result
