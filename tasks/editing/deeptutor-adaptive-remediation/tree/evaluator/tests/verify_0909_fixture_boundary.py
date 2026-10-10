"""Provider-free actual RPC boundary and isolated product import checks."""
import argparse
import ast
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.fixture_rpc import ProductWorker
from agentloop.runtime_probe import probe_runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    private = args.output / "private-canary.txt"
    private.write_text("not exposed to Candidate execution")
    runtime = args.output / "runtime"
    runtime.mkdir()
    os.environ.update(AGENTSWE_FIXTURE_RUNTIME_ROOT=str(runtime), DEEPTUTOR_HOME=str(runtime / "deeptutor-home"),
        AGENTSWE_TEST_PRIVATE_CANARY="synthetic-secret-not-credential")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    worker = ProductWorker(args.repository, args.output / "worker", python=args.python)
    try:
        paths = [str(private), str(ROOT / "agentloop/case_fixture.py"), str(ROOT / "evaluator/cases/test_002.json"),
                 "@@AGENTSWE_CREDENTIAL_FILE@@"]
        observed = worker.call("boundary_probe", paths=paths, host_port=sock.getsockname()[1])
        session = worker.call("create_session", title="Historical fixture boundary probe", session_id="boundary_probe_0909")
        registry = worker.call("tool_names")
    finally:
        worker.close()
        sock.close()
    probe = probe_runtime(args.python, args.repository, output=args.output / "runtime-probe.json")
    imports = [node.module for node in ast.walk(ast.parse((ROOT / "agentloop/case_fixture.py").read_text())) if isinstance(node, ast.ImportFrom)]
    checks = {"actual_product_session_created_in_worker": session.get("session_id", session.get("id")) == "boundary_probe_0909",
        "actual_product_tool_registry_loaded_in_worker": "mastery_remediation_claim" in registry,
        "private_canary_case_inventory_and_credentials_not_visible": not any(observed["visible"].values()),
        "host_tcp_inaccessible": observed["host_tcp_reachable"] is False,
        "host_environment_secret_not_inherited": observed["inherited_secret"] is False,
        "host_controller_has_no_candidate_import": not any(name and name.startswith("deeptutor") for name in imports),
        "runtime_probe_actual_product_imports_isolated": probe.get("valid") is True and probe["sandbox"]["network_namespace"] == "isolated",
        "worker_exited": worker.process.poll() is not None,
        "worker_has_no_cases_or_oracle": all(word not in (ROOT / "agentloop/fixture_worker.py").read_text() for word in ("test_001", "expected_invariants", "AXES ="))}
    result = {"all_passed": all(checks.values()), "checks": checks, "observed_boundary": observed, "provider_calls": 0}
    (args.output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0 if result["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
