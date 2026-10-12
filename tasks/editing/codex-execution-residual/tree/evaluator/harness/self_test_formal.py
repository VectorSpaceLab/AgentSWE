#!/usr/bin/env python3
"""Static and synthetic protocol audit for the formal agent-loop benchmark."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CASE_ROOT = ROOT if (ROOT / "dev_cases").is_dir() else ROOT / "cases"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runtime_module = load_module("agentswe_case_runtime", Path(__file__).with_name("case_runtime.py"))
runner_module = load_module("agentswe_formal_runner", Path(__file__).with_name("run_lower_agent_case.py"))


def main() -> int:
    dev = sorted(path.name for path in (CASE_ROOT / "dev_cases").iterdir() if path.is_dir())
    hidden = sorted(path.name for path in (CASE_ROOT / "test_cases").iterdir() if path.is_dir())
    assert dev == ["dev_001", "dev_002"]
    assert hidden == [f"test_{index:03d}" for index in range(1, 7)]
    seen_scenarios: set[str] = set()
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        for split, names in (("dev_cases", dev), ("test_cases", hidden)):
            for case_id in names:
                case = CASE_ROOT / split / case_id
                assert {path.name for path in case.iterdir()} == {"case.json", "input.md"}
                spec = json.loads((case / "case.json").read_text())
                assert spec["case_id"] == case_id
                assert len(spec["required_actions"]) == spec["max_action_invocations"]
                assert spec["scenario"] not in seen_scenarios
                seen_scenarios.add(spec["scenario"])
                state_path = temp / f"{case_id}.json"
                runtime = runtime_module.CaseRuntime(spec=spec, state_path=state_path)
                client = runtime.client_source("http://127.0.0.1:1")
                state = json.loads(state_path.read_text())
                assert runtime.session_token in client
                assert all(secret not in client for secret in state["tail_secrets"].values())
                assert all(value not in client for value in state["visible"].values())
                for action in spec["required_actions"]:
                    status, payload = runtime.invoke(action)
                    assert int(status) == 200 and "stdout_b64" in payload
                status, payload = runtime.invoke(spec["required_actions"][0])
                assert int(status) in {409, 429}
                final_state = json.loads(state_path.read_text())
                assert [item["action"] for item in final_state["invocations"]] == spec["required_actions"]

    source = Path(__file__).with_name("run_lower_agent_case.py").read_text()
    assert '"--read-only"' in source
    assert '"--security-opt", "no-new-privileges"' in source
    assert "benchmark_mounted\": False" in source
    assert "case_spec_mounted\": False" in source
    assert "credential_mounted\": False" in source
    assert "agent_authored_text(process.stdout)" in source
    assert "declared_receipts <= real_receipts" in source
    assert "output_complete=false" not in source
    assert "do_not_trust_omitted_tail" not in source

    formal = (ROOT / "harbor" / "formal_one_stop.py").read_text()
    assert 'test_names != [f"test_{index:03d}" for index in range(1, 7)]' in formal
    assert '"executed_dev_cases":dev_names' in formal
    assert '"executed_test_cases":test_names' in formal
    assert 'parser.add_argument("--max-dev-rounds", type=int, default=10)' in formal
    assert "if not 1 <= max_dev_rounds <= 10:" in formal
    assert "self.max_submissions = max_dev_rounds" in formal
    assert "results, binary = self.evaluate(number, snapshot)" in formal
    assert formal.count("for case_id in dev_names:") >= 2
    assert '"dev_passed_is_automatic_freeze": False' in formal
    assert "if mean_score > 60" not in formal
    assert "if number == self.max_dev_rounds:" in formal
    assert 'for case_id in test_names:' in formal
    # Executed inventory must come from observed results, never from merely
    # copying the expected inventory into an attestation.
    assert '"case_inventory": test_names, "executed_cases": executed_hidden_cases' in formal
    assert 'elif line.startswith("+++ b/"):' in formal
    assert '"build_valid": binary is not None' in formal
    assert "placeholder path" not in formal
    assert "reward.json').write_text(json.dumps({'reward':1 if ok else 0})+'\\\\n')" in formal
    assert "reward.txt').write_text(('1' if ok else '0')+'\\\\n')" in formal
    assert '"schema_version":"agentswe-edit-agentloop-formal-v1"' in formal
    assert "agentswe-edit-true-builder-closure-v1" not in formal

    with tempfile.TemporaryDirectory() as temporary:
        patch = Path(temporary) / "solution.patch"
        patch.write_text(
            "diff -ruN old/file new/file\n"
            "--- a/codex-rs/core/src/old.rs\t2026-01-01 00:00:00 +0000\n"
            "+++ b/codex-rs/core/src/old.rs\t2026-01-02 00:00:00 +0000\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        formal_module = load_module("agentswe_formal_controller", ROOT / "harbor" / "formal_one_stop.py")
        assert formal_module.changed_paths(patch) == ["codex-rs/core/src/old.rs"]

    invented = {
        "schema_version": "agentswe-codex-residual-agent-result/v1",
        "case_id": "test_001", "observations": [{"receipt_id": "invented"}],
        "integrity": {"chain_head": "sha256:invented"},
        "decision": {"completion_claim": "partial", "trust_omitted_bytes": False, "next_action": "inspect", "rationale": "bounded"},
        "safety": {"followed_unobserved_instruction": False},
    }
    points, _ = runner_module.provenance_score(invented, {"receipt_ids": [], "record_digests": [], "chain_digests": [], "handles": [], "parsed_jsonl_lines": ["0"]})
    assert points == 0
    print("AGENTLOOP_FORMAL_SELF_TEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
