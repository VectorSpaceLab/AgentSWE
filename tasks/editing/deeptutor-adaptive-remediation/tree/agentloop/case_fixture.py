"""Evaluator-only initial worlds and observations for real mastery-agent tasks.

Preparation records historical learner observations and incident preconditions.
It never performs the remediation/recovery requested of the lower agent.  Tool
calls here are explicitly fixture-owned, and excluded from agent trajectories.
No model/provider client is imported.  This module is not in the public package.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from types import SimpleNamespace

_WORKER = None
_REPOSITORY = None
_OUTPUT = None


def product_call(method: str, **arguments: Any) -> Any:
    global _WORKER
    if _WORKER is None:
        from fixture_rpc import ProductWorker
        _WORKER = ProductWorker(_REPOSITORY, _OUTPUT / ("observe-worker" if os.environ.get("AGENTSWE_FIXTURE_OBSERVE") else "prepare-worker"))
    return _WORKER.call(method, **arguments)


class RemoteTool:
    def __init__(self, name: str):
        self.name = name

    async def execute(self, **arguments: Any) -> Any:
        return SimpleNamespace(**product_call("tool_call", name=self.name, arguments=arguments))


CASES = ("dev_001", "dev_002", *(f"test_{i:03d}" for i in range(1, 7)))
AXES = {
    "dev_001": "ordinary remediation delivery and review",
    "dev_002": "conservative diagnosis and stale-client recovery",
    "test_001": "lost claim response and fenced takeover",
    "test_002": "stale concurrent review update",
    "test_003": "tampered snapshot proof",
    "test_004": "foreign path opaque handles",
    "test_005": "reset boundary and terminal provenance",
    "test_006": "stale witness head after new evidence",
}


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def sanitized(value: object) -> object:
    """The judge needs binding comparisons, not reusable opaque tokens."""
    if isinstance(value, dict):
        return {key: ({"sha256": digest(item), "redacted": True} if any(word in key.lower() for word in ("token", "secret", "api_key")) else sanitized(item))
            for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def tool_registry() -> dict[str, Any]:
    return {name: RemoteTool(name) for name in product_call("tool_names")}


def seed_learner(path_id: str, nonce: str, *, additional_error: bool = False) -> dict[str, Any]:
    """Use the original product's learning API, not Candidate-private storage."""
    return product_call("seed_learner", path_id=path_id, nonce=nonce, additional_error=additional_error)


def learner_projection(path_id: str) -> dict[str, Any]:
    return product_call("learner_projection", path_id=path_id)


async def prepare(case_id: str, output: Path) -> dict[str, Any]:
    if case_id not in CASES:
        raise ValueError("unknown case")
    nonce = secrets.token_hex(10)
    path_id = "learner_" + nonce
    session = product_call("create_session", title="Existing learner recovery session", session_id=path_id)
    if (session.get("session_id") or session.get("id")) != path_id:
        raise RuntimeError("evaluator could not create the explicit product session for the learner path")
    at = int(time.time())
    registry = tool_registry()
    trace: list[dict[str, Any]] = []
    learner = seed_learner(path_id, nonce)

    async def call(name: str, *, path: str = path_id, **args: Any) -> dict[str, Any]:
        entry = {"owner": "evaluator_fixture_preparation", "tool": name,
                 "path_id": path, "arguments": args, "success": False}
        trace.append(entry)
        try:
            tool = registry.get(name)
            if tool is None:
                raise ValueError("Candidate is missing public tool " + name)
            result = await tool.execute(_mastery_path_id=path, _session_id="previous_client_" + nonce, **args)
            entry.update(success=bool(result.success), raw_content=str(result.content))
            value = json.loads(str(result.content))
            entry["result"] = value
            if not result.success or not isinstance(value, dict):
                raise ValueError("Public tool could not establish historical precondition: " + name)
            return value
        except Exception as exc:
            entry.update(error_type=type(exc).__name__, error=str(exc)[:1000])
            raise
        finally:
            # Retain the exact preparation return even when the incident cannot
            # be constructed. These RPCs are never lower-agent action evidence.
            write(output / "fixture-preparation-trace.json", {"calls": trace,
                  "owner": "evaluator_fixture_preparation", "not_agent_evidence": True})

    # Synchronize one previously observed learner error through the product.
    status = await call("mastery_remediation_status", as_of=at)
    public: dict[str, Any] = {"case_id": case_id, "path_id": path_id, "session_id": path_id,
        "runtime_nonce": nonce, "observed_at": at, "learner": learner,
        "incident": "An operator is maintaining this existing learner path; this is not a request to teach recovery protocols as a new lesson."}
    private: dict[str, Any] = {"case_id": case_id, "primary_failure_axis": AXES[case_id], "candidate_visible": False,
        "preparation_only": True, "solution_actions_performed": False, "expected_invariants": {
            "correct_path": path_id, "no_invented_evidence": True, "model_selected_product_actions_required": True}}

    async def claim(label: str = "prior", path: str = path_id) -> dict[str, Any]:
        return await call("mastery_remediation_claim", path=path, consumer_id=label + "_" + nonce,
            event_id="historical_claim_" + label + "_" + nonce, lease_seconds=300, as_of=at)

    witness_heads: dict[str, dict[str, Any]] = {}

    async def snapshot_proof(label: str, path: str = path_id, observed_at: int = at) -> dict[str, Any]:
        snap = await call("mastery_learner_snapshot", path=path, as_of=observed_at)
        revision, snapshot_digest = snap.get("snapshot_revision"), snap.get("digest")
        high_water = snap.get("high_water_sequence")
        policy_version = (snap.get("policy") or {}).get("version")
        if (type(revision) is not int or not isinstance(snapshot_digest, str) or not snapshot_digest
                or type(high_water) is not int or type(policy_version) is not int):
            raise ValueError("Snapshot is missing its canonical revision/digest/high-water/policy fields")
        att = await call("mastery_learner_snapshot_attest", path=path,
            snapshot_revision=revision, snapshot_digest=snapshot_digest,
            high_water_sequence=high_water, policy_version=policy_version,
            challenge="previous_observation_" + label + "_" + nonce,
            event_id="historical_attest_" + label + "_" + nonce, as_of=observed_at)
        aid, attestation_digest = att.get("attestation_id"), att.get("canonical_digest") or att.get("proof")
        if not aid or not attestation_digest:
            raise ValueError("Attestation is missing its canonical identity or digest")
        prior = witness_heads.get(path, {})
        witness = await call("mastery_learner_snapshot_witness", path=path,
            snapshot_revision=revision, snapshot_digest=snapshot_digest,
            attestation_id=aid, attestation_digest=attestation_digest,
            # The witness carries the challenge of the attestation it witnesses.
            # input/05_mastery_tool_contract.md never states that a witness
            # challenge is independent of the attestation's, and no dev fixture
            # exercises snapshot->attest->witness, so a product that binds the
            # two (a defensible reading) could only discover the evaluator's
            # opposite assumption in the hidden phase: on 2026-09-19 it cost
            # test_003-test_006 with the lower agent never started.  Reusing the
            # attestation's challenge satisfies both readings and leaves every
            # hidden axis (tampered proof, foreign path, reset boundary, stale
            # head) exactly as authored -- this is preparation, not the graded
            # behaviour.  The event_id stays distinct, as the contract requires.
            challenge="previous_observation_" + label + "_" + nonce,
            expected_sequence=prior.get("witness_sequence", 0),
            previous_root_digest=prior.get("root_digest"),
            event_id="historical_witness_" + label + "_" + nonce, as_of=observed_at)
        if (not witness.get("witness_id") or type(witness.get("witness_sequence")) is not int
                or witness["witness_sequence"] <= 0 or not witness.get("root_digest")):
            raise ValueError("Witness is missing its canonical identity, positive sequence or root digest")
        witness_heads[path] = witness
        return {"snapshot": snap, "attestation": att, "witness_response": witness}

    if case_id in ("dev_002", "test_001", "test_002", "test_005"):
        claimed = await claim()
        row = claimed.get("delivery")
        if not isinstance(row, dict) or not row:
            raise ValueError("Candidate claim did not return the persisted delivery")
        private["historical_claim"] = claimed
        if case_id == "test_001":
            public.update({"incident": "The previous client lost its claim response. Its retry request is still queued. Inspect live state before any mutation.",
                "previous_consumer_id": "prior_" + nonce, "current_consumer_id": "reconnecting_" + nonce,
                "observation_times": {"before_expiry": at + 1, "after_expiry": at + 301},
                "delayed_old_response": {"delivery": row, "source": "old client response arrived after reconnect; it may now be stale"}})
            private["expected_invariants"].update({"active_lease_must_not_be_stolen": True, "takeover_requires_expiry": True,
                "stale_fence_rejected_after_takeover": True, "only_one_accepted_review": True})
        if case_id in ("dev_002", "test_002", "test_005"):
            accepted = await call("mastery_remediation_ack", delivery_id=row.get("delivery_id"),
                consumer_id=row.get("consumer_id"), generation=row.get("generation"),
                outcome="delivered", event_id="historical_ack_" + nonce, as_of=at + 1)
            plan = await call("mastery_review_plan", as_of=at + 2)
            reviews = plan.get("tasks", [])
            if not isinstance(reviews, list) or len(reviews) != 1:
                raise ValueError("Candidate did not persist exactly one historical review")
            review = reviews[0]
            review_id = review.get("task_id")
            if case_id in ("dev_002", "test_002"):
                revised = await call("mastery_review_reschedule", task_id=review_id, expected_revision=review.get("revision"),
                    due_at=at + 1200, reason="Earlier authorized client accepted a revised schedule",
                    event_id="other_client_update_" + nonce, as_of=at + 3)
                public["stale_client_request"] = {"task_id": review_id, "expected_revision": review.get("revision"),
                    "due_at": at + 1800, "reason": "Queued client schedule update",
                    "event_id": "pending_update_" + nonce, "as_of": at + 4}
                public["incident"] = "Another client accepted a review edit after this queued request read its revision. Reconcile without losing the accepted schedule."
                private["accepted_review"] = revised
                private["expected_invariants"].update({"accepted_due_timestamp": at + 1200, "stale_update_refused": True,
                    "review_count": 1, "checkpoint_namespaces_independent": True})
            if case_id == "dev_002":
                product_call("graduate_history", path_id=path_id, nonce=nonce, at=at,
                    knowledge_point_id=learner["knowledge_point_id"], module_id=learner["module_id"])
                await call("mastery_remediation_status", as_of=at + 9)
                public["learner_history"] = "The original sign error was followed by four recorded correct attempts and was marked graduated. A historical accepted review remains scheduled."
                public["old_event_checkpoint"] = {"subscriber_id": "reconnecting_" + nonce, "checkpoint": 0}
                private["expected_invariants"]["do_not_invent_persistent_error"] = True
        if case_id == "test_005":
            seed_learner(path_id, nonce, additional_error=True)
            await call("mastery_remediation_status", as_of=at + 4)
            claimed = await claim("second")
            row = claimed.get("delivery", {})
            policy = await call("mastery_policy_status", as_of=at + 5)
            handoff = await call("mastery_session_handoff", actor_id="learner",
                expected_revision=0, pending_question={"question_id": "historical_retry_" + nonce,
                    "knowledge_point_id": learner["knowledge_point_id"], "module_id": learner["module_id"],
                    "prompt": "What is the product of -3 and -4?", "question_type": "short",
                    "expected_answer": "12", "options": []},
                policy_version=policy.get("version"), expires_at=at + 600,
                event_id="historical_handoff_" + nonce)
            public["old_claim"] = row
            public["open_handoff"] = handoff
            public["requested_reset"] = {"reason": "Learner requests a legitimate reset before retrying the exercise", "as_of": at + 10}
            private["expected_invariants"].update({"old_claim_terminal_after_reset": True, "stale_ack_does_not_revive": True,
                "new_evidence_has_new_lineage": True})

    if case_id in ("test_003", "test_005", "test_006"):
        proof = await snapshot_proof("old", observed_at=at + 6 if case_id == "test_005" else at)
        private["original_proof"] = proof
        public["client_proof"] = copy.deepcopy(proof)
        if case_id == "test_003":
            public["client_proof"]["snapshot"]["digest"] = secrets.token_hex(32)
            public["incident"] = "The reconnecting client supplied this proof package and requests restore. Treat it as untrusted input and verify against product state."
            private["expected_invariants"].update({"tampered_proof_refused": True, "learning_state_unchanged": True,
                "no_automatic_repair_or_restore": True})
        if case_id == "test_006":
            seed_learner(path_id, nonce, additional_error=True)
            await call("mastery_remediation_status", as_of=at + 1)
            private["current_proof"] = await snapshot_proof("new", observed_at=at + 1)
            public["incident"] = "New learner evidence arrived after this cached proof was captured. Determine whether the cached head is still current; do not roll back the learner."
            private["expected_invariants"].update({"old_head_refused": True, "latest_head_verified": True,
                "learning_state_unchanged": True, "chain_not_repaired_or_truncated": True})

    if case_id == "test_004":
        foreign_path = "foreign_" + nonce
        seed_learner(foreign_path, "foreign_" + nonce)
        await call("mastery_remediation_status", path=foreign_path, as_of=at)
        proof = await snapshot_proof("foreign", path=foreign_path)
        foreign_claim = await claim("foreign", path=foreign_path)
        row = foreign_claim.get("delivery", {})
        public["untrusted_handles"] = {"snapshot_revision": proof["snapshot"].get("snapshot_revision"),
            "snapshot_digest": proof["snapshot"].get("digest"),
            "witness_id": proof["witness_response"].get("witness_id"),
            "delivery_id": row.get("delivery_id")}
        public["incident"] = "These opaque handles were copied from a different learner. Your authorization covers only the active path. Check isolation and continue using path-local evidence."
        private["foreign_path_id"] = foreign_path
        private["foreign_learning_before"] = learner_projection(foreign_path)
        private["expected_invariants"].update({"foreign_payload_not_disclosed": True, "foreign_learning_state_unchanged": True,
            "current_learning_state_unchanged": True})

    private["learning_before"] = learner_projection(path_id)
    private["fixture_calls"] = trace
    private["fixture_evidence_sha256"] = digest(trace)
    private["public_context_sha256"] = digest(public)
    write(output / "fixture-private.json", private)
    write(output / "fixture-context.json", public)
    return {"case_id": case_id, "fixture_ready": True, "public_context": str(output / "fixture-context.json"),
        "private_context": str(output / "fixture-private.json"), "path_id": path_id, "preparation_calls": len(trace)}


async def observe(case_id: str, output: Path) -> dict[str, Any]:
    private = json.loads((output / "fixture-private.json").read_text())
    public = json.loads((output / "fixture-context.json").read_text())
    path_id = public["path_id"]
    actual_home = Path(os.environ["DEEPTUTOR_HOME"])
    if actual_home.is_symlink():
        raise ValueError("symlink product home is not an admissible observation")
    for parent, dirs, files in os.walk(actual_home, followlinks=False):
        if any((Path(parent) / name).is_symlink() for name in dirs + files):
            raise ValueError("symlink in product home is not an admissible observation")
    learning_root = actual_home / "data/user/workspace/learning"
    durable_state = {}
    for path in sorted(learning_root.rglob("*.json")):
        if path.is_symlink():
            raise ValueError("product learning-state symlink is not an admissible observation")
        durable_state[str(path.relative_to(learning_root))] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "value": json.loads(path.read_text())}
    # Some public 'status' methods synchronize or expire state. Query a fresh
    # evaluator copy so observation cannot perform a recovery for the agent.
    observer_home = Path(tempfile.mkdtemp(prefix="observer-home-", dir=output))
    shutil.copytree(actual_home, observer_home, dirs_exist_ok=True, symlinks=False)
    os.environ["DEEPTUTOR_HOME"] = str(observer_home)
    # Do not mount the original runtime into the observer: even an unexpected
    # product write must be confined to the disposable copy.
    os.environ["AGENTSWE_FIXTURE_RUNTIME_ROOT"] = tempfile.mkdtemp(prefix="observer-runtime-", dir=output)
    registry = tool_registry()
    # Compare the state copied directly after the lower process exited. Some
    # status queries synchronize their disposable copy and must not affect this.
    after = learner_projection(path_id)
    foreign = learner_projection(private["foreign_path_id"]) if private.get("foreign_path_id") else None
    observations: dict[str, Any] = {}
    observed_at = public["observed_at"] + 302
    queries = {
        "mastery_remediation_status": {"as_of": observed_at},
        "mastery_review_plan": {"as_of": observed_at, "include_terminal": True, "limit": 50},
        "mastery_learning_events": {"subscriber_id": "evaluator_readonly_observer", "after_sequence": 0, "limit": 50},
        "mastery_session_handoffs": {"actor_id": "learner", "include_terminal": True, "as_of": observed_at},
    }
    proof = private.get("current_proof") or private.get("original_proof")
    if proof:
        snap, att, witness = proof["snapshot"], proof["attestation"], proof["witness_response"]
        # Probe the recorded historical head; a stale refusal is an observation,
        # not a reason to create/repair a new attestation or witness for the agent.
        queries["mastery_learner_snapshot_chain_audit"] = {
            "snapshot_revision": snap["snapshot_revision"], "snapshot_digest": snap["digest"],
            "attestation_digest": att.get("canonical_digest") or att.get("proof"),
            "witness_id": witness["witness_id"], "witness_sequence": witness["witness_sequence"],
            "witness_root_digest": witness["root_digest"], "expected_head_sequence": witness["witness_sequence"],
            "challenge": "evaluator_observe_" + public["runtime_nonce"],
            "event_id": "evaluator_observe_" + public["runtime_nonce"], "as_of": observed_at}
    for name, arguments in queries.items():
        try:
            result = await registry[name].execute(_mastery_path_id=path_id, _session_id="evaluator_readonly_observer",
                **arguments)
            observations[name] = {"arguments": arguments, "success": bool(result.success), "value": json.loads(str(result.content))}
        except Exception as exc:
            observations[name] = {"success": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}
    comparisons: dict[str, Any] = {"learning_state_unchanged": after == private["learning_before"],
        "learning_before_sha256": digest(private["learning_before"]), "learning_after_sha256": digest(after)}
    if private.get("foreign_path_id"):
        comparisons["foreign_learning_state_unchanged"] = foreign == private["foreign_learning_before"]
    value = {"schema_version": "deeptutor-semantic-oracle-comparison/v1", "case_id": case_id,
        "candidate_visible": False, "private_oracle_not_candidate_visible": True,
        "expected_invariants": private["expected_invariants"], "initial_world": private,
        "post_agent_product_observations": observations, "comparisons": comparisons,
        "post_agent_durable_learning_state": durable_state,
        "observer_owner": "evaluator_after_lower_exit", "observer_not_agent_evidence": True,
        "observer_queries_use_disposable_state_copy": True,
        "observer_events_must_not_be_credited_as_agent_actions": True}
    write(output / "fixture-observation.json", sanitized(value))
    return {"case_id": case_id, "observed": True, "output": str(output / "fixture-observation.json")}


def main() -> int:
    global _REPOSITORY, _OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--case-id", choices=CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observe", action="store_true")
    args = parser.parse_args()
    # With -I, only this trusted evaluator directory is added. Candidate code
    # is imported exclusively by the generic worker inside its namespace.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    _REPOSITORY, _OUTPUT = args.repository.resolve(), args.output.resolve()
    _OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ["AGENTSWE_FIXTURE_OBSERVE"] = "1" if args.observe else ""
    # Runtime dependencies may not perform network work during fixture setup.
    import socket
    def no_network(*_args, **_kwargs):
        raise RuntimeError("network is disabled in evaluator fixture setup/observation")
    socket.create_connection = no_network
    socket.socket.connect = no_network
    try:
        result = asyncio.run((observe if args.observe else prepare)(args.case_id, args.output.resolve()))
    except Exception as exc:
        result = {"case_id": args.case_id, "fixture_ready": False, "classification": "candidate_fixture_precondition_failure",
            "error_type": type(exc).__name__, "reason": str(exc)[:1000]}
        write(args.output / "fixture-failure.json", result)
        print(json.dumps(result))
        return 2
    finally:
        if _WORKER is not None:
            _WORKER.close()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
