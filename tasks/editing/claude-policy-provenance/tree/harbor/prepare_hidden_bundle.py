#!/usr/bin/env python3
"""Issue a fresh evaluator-only six-world bundle from the audited draft input.

No Candidate, model, Docker, or historical run is executed or modified. The
two interrupted-worker worlds gain only a historical PreToolUse prefix; their
recovery actions and all expected outcomes remain for the lower Agent to solve.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.dynamic_case_service import issue
from agentloop.evaluator.case_contract import load_case_bundle, digest_object
from agentloop.evaluator.hidden_executor import CASE_IDS


def prepare(source: Path, output: Path, branches: dict | None = None) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("issued hidden worlds must remain outside the Candidate benchmark tree")
    if output.exists() and any(output.iterdir()):
        raise ValueError("new bundle output must be empty; historical bundles are immutable")
    source_hashes, templates = {}, {}
    for case_id in CASE_IDS:
        pair = [source / f"{case_id}.json", source / f"{case_id}.oracle.json"]
        for path in pair:
            source_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        visible, oracle = [json.loads(path.read_text()) for path in pair]
        visible = {key: value for key, value in visible.items() if key not in {
            "case_contract", "case_contract_digest", "schema_version", "runtime_nonce", "case_id"}}
        oracle = {key: value for key, value in oracle.items() if key not in {
            "oracle_digest", "schema_version", "runtime_nonce", "case_id", "case_contract_digest"}}
        source_action = {"test_002": "replay_recorded_pre", "test_005": "replay_crashed_pre"}.get(case_id)
        if source_action:
            match = [a for a in visible["allowed_actions"] if a["id"] == source_action]
            if len(match) != 1 or match[0].get("type") != "hook_event":
                raise ValueError("interrupted world lacks its disclosed historical event")
            visible["initial_events"] = [match[0]["event"]]
        templates[case_id] = {"candidate": visible, "private_oracle": oracle}
    output.mkdir(parents=True, exist_ok=True)
    fingerprints = {}
    for case_id, template in templates.items():
        issue(case_id, template, output, output,
              branch=(branches or {}).get(case_id))
        visible, oracle, _, _ = load_case_bundle(output, case_id)
        ids = {a["id"] for a in visible["allowed_actions"]}
        if not set(oracle.get("required_action_ids", [])) <= ids or not set(oracle.get("forbidden_action_ids", [])) <= ids:
            raise ValueError("oracle refers to an action absent from the actual product catalog")
        if set(oracle.get("required_action_ids", [])) & set(oracle.get("forbidden_action_ids", [])):
            raise ValueError("oracle requires and forbids the same action")
        fingerprints[case_id] = digest_object({key: visible.get(key) for key in ("policy", "allowed_actions", "initial_events")})
    if len(set(fingerprints.values())) != 6:
        raise ValueError("worlds differ only by superficial identity rather than executable fixtures")
    manifest = {"schema_version": "agentswe-claude-issued-world-audit/v1", "case_inventory": list(CASE_IDS),
        "source_hashes": source_hashes, "world_fingerprints": fingerprints,
        "changes": ["remove private case_contract from visible payload", "fresh paired nonce and oracle digest",
                    "test_002 and test_005 establish disclosed historical PreToolUse through the edited hook",
                    "0919 hardening: adversarial worlds with a documented receipt-contract oracle"],
        "provider_calls": 0, "formal_result_publishable": False,
        "authority_branches": {case_id: (branches or {}).get(case_id) for case_id in CASE_IDS},
        "fixture_authority": "only initial historical events; no evaluator-written recovery state or answer",
        "tests_executed": False, "oracle_mounted_to_candidate": False}
    (output / "issuance_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--branches", default=None,
                        help="Optional 'case_id=0,case_id=1' pin for the authority branch. "
                             "Omitted, every world draws its branch at random as before.")
    args = parser.parse_args()
    branches = None
    if args.branches:
        branches = {}
        for item in args.branches.split(","):
            case_id, _, value = item.partition("=")
            case_id, value = case_id.strip(), value.strip()
            if case_id not in CASE_IDS or value not in ("0", "1"):
                raise SystemExit(f"invalid authority branch pin: {item!r}")
            branches[case_id] = int(value)
    print(json.dumps(prepare(args.source_dir, args.output_dir, branches), indent=2))
