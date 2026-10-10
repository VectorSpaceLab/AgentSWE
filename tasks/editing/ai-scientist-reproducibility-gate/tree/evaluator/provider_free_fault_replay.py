"""Real-Candidate, provider-free fault adapter diagnostic; never agent evidence."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentloop"))
import lower_agent_launcher as launcher
from case_world import CaseWorld
from protocol import tree_digest, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-candidate-digest", required=True)
    parser.add_argument("--case", choices=["test_001", "test_006"], required=True)
    parser.add_argument("--seed-preserved-releases", action="store_true", help="Create real prior/peer releases as diagnostic fixtures, not model evidence")
    args = parser.parse_args()
    output, candidate = args.output.resolve(), args.candidate.resolve()
    if tree_digest(candidate) != args.expected_candidate_digest: raise ValueError("Candidate digest changed")
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "fresh_workspace"; workspace.mkdir()
    case_file = ROOT / "agentloop/cases" / args.case / "case_input.json"
    launcher.copy_candidate_visible_assets(launcher.case_assets_root(case_file, case_file.parent), workspace)
    context = json.loads((workspace / "transaction_context.json").read_text())
    context["request_id"] += "-provider-free-adapter-check"
    context["verification_id"] += "-provider-free-adapter-check"
    world = CaseWorld(args.case, workspace, output, context)
    run_id = "fault-check-" + uuid.uuid4().hex[:16]
    os.environ["AGENTSWE_RUNTIME_OWNER_ID"] = run_id
    seed_events = []
    if args.seed_preserved_releases:
        world.initial_history = launcher.seed_existing_history(candidate_repo=candidate, case_file=case_file,
            output_dir=output, timeout=180, nonce="provider-free-history", image="sha256:10b0f65061629ca8edab1b33444f49661f1c47e98109fda74072839287630336", dependency_overlay=None)
        seed_events = json.loads((output / "initial_history.json").read_text())["product_events"]
        if not world.initial_history["valid"]:
            raise RuntimeError("real product diagnostic fixture seeding failed")
    operations = ["verify", "verify"] if args.case == "test_001" else ["prepare", "cancel", "commit", "verify", "verify"]
    events, comparisons, preservation_observations = [], [], []
    for sequence, operation in enumerate(operations, 1):
        protected_before = world._protected_state_snapshot()
        plan = world.prepare_action(operation)
        event = launcher._run_product_action(candidate_repo=candidate, workspace=workspace, output_dir=output,
                                            context=context, endpoint="http://unreachable.invalid/v1/responses",
                                            timeout=180, operation=operation, sequence=sequence,
                                            decision={"rationale": "provider-free harness adapter diagnostic; not a model decision", "response_text_sha256": "0" * 64},
                                            image="sha256:10b0f65061629ca8edab1b33444f49661f1c47e98109fda74072839287630336", dependency_overlay=None)
        evidence = world.observe_action(action_sequence=sequence, operation=operation, product_event=event, plan=plan)
        events.append(event); comparisons.append(evidence)
        protected_after = world._protected_state_snapshot()
        before_totals = protected_before["project_settled_micros"]
        after_totals = protected_after["project_settled_micros"]
        preservation_observations.append({"operation": operation, "settled_before": before_totals, "settled_after": after_totals,
            "settled_totals_never_decreased": all(isinstance(after_totals.get(project), int) and after_totals[project] >= value for project, value in before_totals.items() if isinstance(value, int)),
            "previous_published_bytes_preserved": all(protected_after["published_file_sha256"].get(path) == digest for path, digest in protected_before["published_file_sha256"].items() if path.startswith("sessions/committed/"))})
        print(json.dumps({"diagnostic_operation": operation, "exit_code": event["exit_code"], "transition": evidence.get("transition"), "advanced": evidence.get("advanced")}), flush=True)
    world.persist()
    # Negative control checks the same real receipt with a deliberately
    # mismatched request fingerprint. It must not borrow durable state.
    receipt_path = output / "transaction_receipt.json"
    negative = None
    if receipt_path.is_file():
        raw = receipt_path.read_bytes(); receipt = json.loads(raw)
        receipt["request_fingerprint"] = "deliberately-foreign-request"
        write_json(receipt_path, receipt)
        try: negative = not world._has_durable_phase("committed")
        finally: receipt_path.write_bytes(raw)
    report = {"schema_version": "agentswe-real-product-provider-free-fault-check/v1", "case_id": args.case,
              "real_candidate_product": True, "model_calls": 0, "model_decisions": False,
              "operations_selected_by": "evaluator diagnostic only", "score": None,
              "formal_result_publishable": False, "acceptance_result_publishable": False,
              "world_complete": world.complete, "foreign_fingerprint_rejected": negative,
              "candidate_digest": args.expected_candidate_digest,
              "candidate_unchanged": tree_digest(candidate) == args.expected_candidate_digest,
              "transitions": comparisons, "preservation_fixtures": {"requested": args.seed_preserved_releases, "actual_product_seed_operations": len(seed_events)},
              "actual_product_preservation_comparisons": preservation_observations,
              "candidate_preservation_passed": all(item["settled_totals_never_decreased"] and item["previous_published_bytes_preserved"] for item in preservation_observations),
              "adapter_fault_transitions_complete_is_not_candidate_correctness": True}
    names = []
    for event in seed_events + events:
        command = event.get("dispatched_product_command", [])
        if "--name" in command:
            names.append(command[command.index("--name") + 1])
    remaining = []
    for name in names:
        current = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^" + name + "$"], capture_output=True, text=True, check=True)
        if current.stdout.strip(): remaining.append(name)
    report["cleanup"] = {"container_names": names, "remaining_owned_containers": remaining, "all_absent": not remaining}
    write_json(output / "provider_free_fault_report.json", report)
    write_json(output / "diagnostic_product_events.json", {"provider_free": True, "not_lower_agent_trajectory": True, "fixture_seed_events": seed_events, "events": events})
    return 0 if report["world_complete"] and report["candidate_unchanged"] and negative and not remaining else 2


if __name__ == "__main__": raise SystemExit(main())
