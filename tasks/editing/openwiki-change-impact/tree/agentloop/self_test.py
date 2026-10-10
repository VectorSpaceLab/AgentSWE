#!/usr/bin/env python3
"""Provider-free Phase-A contract checks; no Docker, Node, network, or API calls."""
from __future__ import annotations
import json, re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from .protocol import DEV_CASES, HIDDEN_CASES, LOWER_EFFORT, LOWER_MODEL
    from .evaluator.code_rubric import WEIGHTS as CODE_WEIGHTS
    from .evaluator.result_rubric import WEIGHTS as RESULT_WEIGHTS
except ImportError:  # direct `python3 agentloop/self_test.py`
    from agentloop.protocol import DEV_CASES, HIDDEN_CASES, LOWER_EFFORT, LOWER_MODEL
    from agentloop.evaluator.code_rubric import WEIGHTS as CODE_WEIGHTS
    from agentloop.evaluator.result_rubric import WEIGHTS as RESULT_WEIGHTS

ROOT = Path(__file__).resolve().parents[1]
def main() -> int:
    errors: list[str] = []
    lock = json.loads((ROOT / "schemas/protocol_lock.json").read_text())
    if lock.get("lower_model") != LOWER_MODEL or lock.get("lower_reasoning_effort") != LOWER_EFFORT: errors.append("model lock drift")
    if tuple(lock.get("public_cases", [])) != DEV_CASES or tuple(lock.get("hidden_cases", [])) != HIDDEN_CASES: errors.append("inventory drift")
    inventory = json.loads((ROOT / "agentloop/cases/inventory.json").read_text())
    if len(inventory.get("public", [])) != 2 or len(inventory.get("hidden", [])) != 6: errors.append("machine-readable inventory is not 2+6")
    if sum(RESULT_WEIGHTS.values()) != 100 or sum(CODE_WEIGHTS.values()) != 100: errors.append("rubric weights do not sum to 100")
    public_dirs = {path.name for path in (ROOT / "dev_cases").iterdir() if path.is_dir() and re.fullmatch(r"dev_\d{3}", path.name)}
    if public_dirs != set(DEV_CASES): errors.append("public dev directory set is not exactly 2")
    hidden = sorted(path.stem for path in (ROOT / "evaluator/manifests").glob("test_*.json"))
    if hidden != list(HIDDEN_CASES): errors.append("native hidden inventory drift")
    launcher = (ROOT / "agentloop/evaluator/lower_agent_launcher.py").read_text()
    protocol = (ROOT / "agentloop/protocol.py").read_text()
    for required in ("dist/cli.js", "--update", "OPENAI_API_KEY"):
        if required not in launcher: errors.append(f"launcher missing {required}")
    for required in ("broker-only-placeholder", LOWER_MODEL, LOWER_EFFORT):
        if required not in launcher + protocol: errors.append(f"launcher missing {required}")
    for required in ("node-v22.12.0-linux-x64", "runtime_preflight", "native_binding_failure", "evaluator_runtime"):
        if required not in launcher: errors.append(f"runtime preflight missing {required}")
    runtime_setup = ROOT / "agentloop/evaluator/prepare_node22_runtime.sh"
    if not runtime_setup.is_file(): errors.append("Node 22 runtime setup script missing")
    broker = (ROOT / "agentloop/evaluator/broker.py").read_text()
    for required in ("/v1/responses", "successful_calls", "forced_overrides"):
        if required not in broker: errors.append(f"broker missing {required}")
    for required in (LOWER_MODEL, LOWER_EFFORT):
        if required not in broker + protocol: errors.append(f"broker missing {required}")
    hidden_controller = (ROOT / "agentloop/evaluator/hidden_controller.py").read_text()
    for required in (
        "repository_digest",
        "source_submission",
        "HIDDEN_CASES",
        "hidden-once-gate",
        "os.O_EXCL",
        "evidence-manifest",
        "EvaluatorBrokerLifecycle",
        "credential_mounted_to_candidate",
    ):
        if required not in hidden_controller:
            errors.append(f"hidden controller missing {required}")
    for required in ("DEEPSEEK_API_KEY", "credential_value_recorded", "broker_instance_id"):
        if required not in broker:
            errors.append(f"broker missing {required}")
    formal_one_stop = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
    if 'parser.add_argument("--result-judge-broker-endpoint"' in formal_one_stop:
        errors.append("formal entry still requires an external Result-judge endpoint")
    for required in (
        "RESULT_JUDGE_MODEL = \"deepseek-flash\"",
        "RESULT_JUDGE_EFFORT = \"xhigh\"",
        "result_judge_broker = EvaluatorBrokerLifecycle(",
        "container_prefix=\"openwiki-result-judge-broker\"",
        'brokers=(("public_lower", public_broker), ("result_judge", result_judge_broker))',
        '"final_stats": lifecycle_record.get("final_stats")',
    ):
        if required not in formal_one_stop:
            errors.append(f"formal Result-judge lifecycle missing {required}")
    if "pilot_not_formal=True" not in formal_one_stop or "pilot_hidden_lower" not in formal_one_stop:
        errors.append("pilot formal/non-formal separation drift")
    lower_launcher = (ROOT / "agentloop/evaluator/lower_agent_launcher.py").read_text(encoding="utf-8")
    for required in ("artifact_owner", "lower_agent_product", "trajectory_path"):
        if required not in lower_launcher:
            errors.append(f"lower artifact provenance missing {required}")
    for required in ("--model", "--reasoning-effort", "self.model", "self.reasoning_effort"):
        if required not in broker:
            errors.append(f"broker stage configuration missing {required}")
    freeze_schema = json.loads((ROOT / "schemas/freeze_manifest.schema.json").read_text())
    if freeze_schema.get("properties", {}).get("hidden_started_at", {}).get("const", "missing") is not None:
        errors.append("freeze schema permits post-freeze manifest mutation")
    if freeze_schema.get("properties", {}).get("repository_digest_algorithm", {}).get("const") != "sha256-tree-v1":
        errors.append("freeze schema does not lock repository digest algorithm")
    public_text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "dev_cases").glob("dev_*/input.md"))
    for secret_marker in ("expected_pages", "forbidden_text", "expected_examples", "oracle_digest"):
        if secret_marker in public_text: errors.append(f"public prompt leaks oracle field {secret_marker}")
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, indent=2)); return 1
    print(json.dumps({"ok": True, "phase": "A", "provider_calls": 0, "status": "PARTIAL", "source_digest_recorded": "9f89b2c56a072f2e5c63d3b60bd6e7c7e9f56861ee1500e0925603ab938e1160"}, indent=2)); return 0

if __name__ == "__main__": raise SystemExit(main())
