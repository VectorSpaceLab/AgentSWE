#!/usr/bin/env python3
"""Run-local Dyad hidden-scenario preparation and private oracle comparison.

This module creates fixtures and judges observations.  It never invokes Dyad
product actions.  Product actions must appear in the native evidence as
model-selected requests captured from persisted typed-chat responses.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
if __package__:
    from .behavior_worlds import materialize
else:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from behavior_worlds import materialize


# Cross-cutting product obligations published in input/02 and input/03.  They
# are the same for every case because they are properties of the Acceptance
# surface itself, not of one case's adversity, and the evaluator derives each
# one from bytes the product already returned in this rollout: no extra
# lower-agent turn is spent on any of them.
UNIVERSAL_CONTRACT_CHECKS: tuple[str, ...] = (
    # input/02 "Acceptance target identity": the caller must be able to take the
    # current Acceptance target from the product instead of guessing it.
    "current_target_binds_started_target",
    # input/02 same section: the session echoes the accepted command's target
    # field by field, with no defaulting and no grep-to-null normalisation.
    "session_target_preserved",
    # input/02 same section: testFingerprint is SHA-256 over the admitted target
    # file's raw bytes, not a path, a constant or a canonicalised digest.
    "fingerprint_is_admitted_target_bytes",
    # input/02 "Attestation uniqueness and terminal monotonicity".
    "event_sequence_never_decreases",
    # input/02 "Content addressability of the workspace revision": reading does
    # not change state, so an immutable attestation and a terminal session read
    # back byte for byte.
    "terminal_and_attestation_reads_byte_stable",
    # input/02 "Durable attestation surface": resultDigest is the published
    # canonical digest of the terminal facts, recomputable by any caller.
    "result_digest_matches_terminal_facts",
)

# input/01 and input/03 item 10: a runner result that is not a current matching
# `passed` attestation may not carry a run to `passed`.  Only cases that reach a
# non-passing terminal state can observe this, so it is not universal.
GATE_CONTRACT_CHECK = "acceptance_gate_fails_closed"


@dataclass(frozen=True)
class Scenario:
    axis: str
    required_actions: tuple[str, ...]
    required_checks: tuple[str, ...]
    runner_delay_ms: int = 0
    lose_first_start_response: bool = False
    late_callback_after_cancel: bool = False
    restart_during_nonterminal: bool = False
    extra_contract_checks: tuple[str, ...] = ()

    @property
    def contract_checks(self) -> tuple[str, ...]:
        return UNIVERSAL_CONTRACT_CHECKS + self.extra_contract_checks


SCENARIOS: dict[str, Scenario] = {
    # Each hidden case proves one distinct hard capability. The required action
    # list is the minimum trajectory that can produce the evidence; the checks
    # are product-behaviour facts the evaluator derives from real runtime bytes,
    # never from what the agent says it did.
    "test_001": Scenario(
        "verified terminal proof that survives a cold restart",
        ("inspect_latest", "start_preview", "get_preview", "get_attestation",
         "restart_state", "get_preview", "get_attestation", "finish"),
        ("failed_before_passed", "repair_kept_target_and_scope", "terminal_passed",
         "fingerprint_matches_target_bytes", "clean_run_revisions_pinned",
         "attestation_binds_session", "attestation_stable_after_restart"),
    ),
    "test_002": Scenario(
        "idempotent retry after a lost response and operation-identity conflict",
        ("inspect_latest", "start_preview", "retry_preview", "get_preview",
         "conflict_start", "get_preview", "get_attestation", "finish"),
        ("lost_response_observed", "retry_replayed_original", "single_runner_invocation",
         "same_session_recovered", "conflicting_operation_identity_rejected",
         "conflict_started_no_second_run", "single_attestation_identity"),
        lose_first_start_response=True,
    ),
    "test_003": Scenario(
        "denial without disclosure and an auditable denial ledger",
        ("inspect_latest", "start_preview", "stale_control", "foreign_control",
         "foreign_read", "get_preview", "finish"),
        ("invalid_requests_rejected", "authorized_state_preserved",
         "denial_ledger_auditable", "denials_disclosed_nothing",
         "authorized_session_unaffected"),
    ),
    "test_004": Scenario(
        "content-addressed revision drift permanently invalidates a green run",
        ("inspect_latest", "start_preview", "get_preview", "mutate_target",
         "get_preview", "restore_target", "get_preview", "get_attestation", "finish"),
        ("terminal_invalidated", "invalidation_reason_bounded",
         "drift_moved_finished_revision", "green_runner_result_refused",
         "attestation_invalidated", "restoration_did_not_resurrect"),
        runner_delay_ms=60000,
        extra_contract_checks=(GATE_CONTRACT_CHECK,),
    ),
    "test_005": Scenario(
        "cancellation wins over a late callback and a duplicate stop",
        ("inspect_latest", "start_preview", "stop_tests", "get_preview",
         "duplicate_stop", "get_preview", "get_attestation", "finish"),
        ("cancelled_is_terminal", "late_callback_observed", "single_runner_invocation",
         "terminal_not_overwritten", "single_attestation_identity",
         "attestation_cancelled"),
        runner_delay_ms=60000,
        late_callback_after_cancel=True,
        extra_contract_checks=(GATE_CONTRACT_CHECK,),
    ),
    "test_006": Scenario(
        "cold restart reconciles interrupted work honestly and stays compatible",
        ("inspect_latest", "start_preview", "restart_state", "get_run",
         "get_preview", "get_attestation", "compatibility_chat", "get_run", "finish"),
        ("restart_selected", "no_automatic_rerun", "interrupted_terminal_is_honest",
         "restart_preserved_identity", "attestation_matches_recovered_status",
         "compatibility_isolated"),
        runner_delay_ms=60000,
        restart_during_nonterminal=True,
        extra_contract_checks=(GATE_CONTRACT_CHECK,),
    ),
    # Public development cases exercise the same protocol and the same *kinds*
    # of mechanism (lost response, exact retry, drift, cancellation) so the
    # Builder can develop against them, but their oracle stays structural:
    # hidden expected states are asserted only for test_001..test_006.
    "dev_001": Scenario(
        "public lost-response retry and durable proof",
        ("inspect_latest", "start_preview", "retry_preview", "get_preview",
         "get_attestation", "finish"),
        ("public_model_selected_flow",),
        lose_first_start_response=True,
    ),
    "dev_002": Scenario(
        "public drift invalidation and cancellation",
        ("inspect_latest", "start_preview", "mutate_target", "get_preview",
         "restore_target", "stop_tests", "get_preview", "finish"),
        ("public_model_selected_flow",),
        runner_delay_ms=60000,
    ),
}


ACTION_PROTOCOL = """

## Run-local Dyad product-action interface

Work through Dyad's normal typed chat and local-agent tools first.  You may
edit the app and call `run_tests`; those tool calls are part of the evidence.
When you need one typed Acceptance/Preview/Tests product action, end the
assistant response with exactly one line in this form:

`<dyad-acceptance-action>{"action":"inspect_latest", ...}</dyad-acceptance-action>`

Available actions are `inspect_latest`, `start_preview`, `retry_preview`,
`conflict_start`, `get_preview`, `get_run`, `get_attestation`, `stale_control`,
`foreign_control`, `foreign_read`, `mutate_target`, `restore_target`,
`stop_tests`, `duplicate_stop`, `restart_state`, `compatibility_chat`, and
`finish`.  Supply the concrete IDs and target returned by prior product
observations.  The runner executes only the one action you selected; it does
not fill missing IDs, choose the next action, or complete the workflow for
you.  A malformed or absent action ends the case honestly.

Payload shapes are deliberately neutral and do not prescribe an order:

- `inspect_latest` needs only `{"action":"inspect_latest"}` and returns the
  current product-owned IDs and generation.
- `start_preview` and `retry_preview` require `command` with concrete
  `appId`, `chatId`, `runId`, caller-stable `operationId`,
  `expectedGeneration`, exact `target`, and `presentation:"preview"`. The exact
  target is a product fact: read it from the product, do not guess its fields.
- `conflict_start` requires a `command` that reuses the original
  caller-stable `operationId` on the current owner/run while carrying target
  content that differs from the original command. It is a probe: the product,
  not the dispatcher, decides what happens to it.
- `get_preview`, `get_run`, and `get_attestation` require the concrete owner
  IDs plus the returned session/run/attestation ID.
- `stale_control` and `foreign_control` require the exact typed control
  `command` you chose to test. `foreign_read` requires a permitted Acceptance
  read `channel` and its concrete `payload`.
- `mutate_target` and `restore_target` require the exact run-local
  `testFile`; `stop_tests` and `duplicate_stop` require the observed `appId`.
- `restart_state` and `compatibility_chat` have no hidden defaults; they run
  only when selected.

For `finish`, include an `artifact` object authored by you.  The evaluator
compares it key by key, so use exactly these names:
`schema_version` set to "dyad-lower-agent-artifact-v3"; `case_id` and
`scenario_id` for the current case; `task_sha256` set to the executed task
SHA-256 reported in observations (observations call that field
`executed_task_sha256`; in the artifact the key is `task_sha256`);
`workspace`, an object -- not flattened into the artifact -- carrying the actual
`app_id`, `chat_id`, `run_id`, `session_id`, `revision` and `target_fingerprint`
exactly as observations report them; `product_actions`, a factual list of the
product actions you selected; and `blockers`, an array.  Do not invent unavailable evidence.  The evaluator captures
that object verbatim and compares it with product/native evidence; it never
constructs or repairs the agent artifact.
""".strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def object_sha256(value: object) -> str:
    return sha256_bytes(json_bytes(value))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def original_task_path(case: Path) -> Path:
    path = case / "input.md" if case.is_dir() else case
    if not path.is_file():
        raise ValueError(f"case input is missing: {path}")
    return path.resolve()


PUBLIC_FIXTURE_KEYS = {
    "schema_version", "case_id", "scenario_id", "target", "operation_id",
    "fault_injection", "executed_task_sha256", "public_app",
}
PRIVATE_PUBLIC_KEYS = {
    "required_actions", "required_checks", "required_scenario_checks",
    "required_contract_checks",
    "expected_terminal", "expected_terminal_status", "expected_product_status",
    "expected_truth", "private_oracle", "oracle",
}


def validate_public_fixture(value: dict[str, Any]) -> None:
    if set(value) != PUBLIC_FIXTURE_KEYS:
        raise ValueError("public scenario fixture has an unexpected contract surface")
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in PRIVATE_PUBLIC_KEYS:
                    found.add(key)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    if found:
        raise ValueError("public scenario fixture leaks private contract keys: " + ", ".join(sorted(found)))
    fault = value.get("fault_injection")
    if not isinstance(fault, dict) or fault.get("fixture_only") is not True:
        raise ValueError("public scenario fault injection must be explicitly fixture-only")
    if "outcomes" in fault:
        raise ValueError("predetermined browser outcomes are forbidden")


def prepare_scenario(case_id: str, case: Path, destination: Path) -> dict[str, Any]:
    if case_id not in SCENARIOS:
        raise ValueError(f"no explicit Dyad scenario dispatch for {case_id}")
    scenario = SCENARIOS[case_id]
    source = original_task_path(case)
    original = source.read_text(encoding="utf-8").strip()
    token = secrets.token_hex(8)
    target = f"e2e-tests/agentswe-{case_id}-{token}.spec.ts"
    operation_id = f"{case_id}-{token}-preview"
    destination.mkdir(parents=True, exist_ok=True)
    app = materialize(case_id, destination / 'initial-app')
    initial_test_path = Path(app['private_independent_test'])
    initial_test = initial_test_path.read_text()
    # Keep the original assertions private in memory after publishing their
    # initial target. The Candidate may edit its own target, not this baseline.
    initial_test_path.unlink()
    public_app = {"family": app['family'], "goal": app['public_goal'],
        "initial_files": {name: (destination / 'initial-app' / name).read_text()
            for name in app['source_sha256']}, "initial_test": initial_test,
        "source_sha256": app['source_sha256']}
    context = (
        "## Current run-local app facts\n\n"
        f"The evaluator-created behavior fixture is `{target}`. "
        f"Use caller-stable operation identity `{operation_id}` for the first "
        "preview command and any exact retry. These are dynamic inputs, not "
        "expected outcomes. Read all app/chat/run/session/revision/fingerprint "
        "values from the product before using them.\n\n"
        f"App goal: {app['public_goal']}\n"
        "The app is served by its local server.mjs. Inspect its real source and "
        "the public target; browser outcomes are determined by actual Chromium "
        "execution, not an expected outcome sequence.\n"
    )
    executed = destination / "executed_task.md"
    executed.parent.mkdir(parents=True, exist_ok=True)
    executed.write_text(f"{original}\n\n{context}\n{ACTION_PROTOCOL}\n", encoding="utf-8")
    task_sha = file_sha256(executed)
    public = {
        "schema_version": "dyad-scenario-public-v1",
        "case_id": case_id,
        "scenario_id": case_id,
        "target": {"testFile": target, "grep": f"{case_id} actual {app['family']} behavior"},
        "public_app": public_app,
        "operation_id": operation_id,
        "fault_injection": {
            "fixture_only": True,
            "delay_ms": scenario.runner_delay_ms,
            "lose_first_start_response": scenario.lose_first_start_response,
            "late_callback_after_cancel": scenario.late_callback_after_cancel,
            "restart_during_nonterminal": scenario.restart_during_nonterminal,
        },
        "executed_task_sha256": task_sha,
    }
    private = {
        "schema_version": "dyad-scenario-private-oracle-v1",
        "case_id": case_id,
        "scenario_id": case_id,
        "axis": scenario.axis,
        "executed_task_sha256": task_sha,
        "original_task_sha256": file_sha256(source),
        "required_actions": list(scenario.required_actions),
        "required_scenario_checks": list(scenario.required_checks),
        "required_contract_checks": list(scenario.contract_checks),
        "target": public["target"],
        "operation_id": operation_id,
        "expected_faults": public["fault_injection"],
        "private_not_candidate_visible": True,
        "business_goal": app['public_goal'],
        "initial_app_sha256": app['source_sha256'],
        "independent_browser_test": initial_test,
        "independent_browser_test_sha256": app['private_test_sha256'],
        "business_behavior_requires_independent_post_lower_browser_execution": True,
    }
    public_path = destination / "public_fixture.json"
    private_path = destination / "private_oracle.json"
    validate_public_fixture(public)
    write_json(public_path, public)
    # A reused output path must not expose an oracle left by an earlier run.
    # The current oracle remains a parent-process object until lower execution
    # has ended and the caller explicitly persists it as final evidence.
    if private_path.exists() or private_path.is_symlink():
        private_path.unlink()
    return {
        "case_id": case_id,
        "original_task_path": str(source),
        "original_task_sha256": file_sha256(source),
        "executed_task_path": str(executed),
        "executed_task_sha256": task_sha,
        "public_fixture_path": str(public_path),
        "public_fixture_sha256": file_sha256(public_path),
        "private_oracle_path": str(private_path),
        "private_oracle": private,
        "private_oracle_sha256": object_sha256(private),
        "scenario_id": case_id,
        "axis": scenario.axis,
    }


def persist_private_oracle(prepared: dict[str, Any]) -> Path:
    value = prepared.get("private_oracle")
    raw_path = prepared.get("private_oracle_path")
    if not isinstance(value, dict) or not isinstance(raw_path, str):
        raise ValueError("prepared scenario has no in-memory private oracle")
    if object_sha256(value) != prepared.get("private_oracle_sha256"):
        raise ValueError("in-memory private oracle digest changed before persistence")
    path = Path(raw_path)
    write_json(path, value)
    if file_sha256(path) != prepared.get("private_oracle_sha256"):
        raise ValueError("persisted private oracle digest mismatch")
    return path


def ordered_subsequence(required: list[str], observed: list[str]) -> bool:
    cursor = 0
    for action in observed:
        if cursor < len(required) and action == required[cursor]:
            cursor += 1
    return cursor == len(required)


def required_action_records(required: list[str], actions: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    matched: list[dict[str, Any] | None] = []
    cursor = 0
    for name in required:
        found: dict[str, Any] | None = None
        while cursor < len(actions):
            candidate = actions[cursor]
            cursor += 1
            if candidate.get("action") == name:
                found = candidate
                break
        matched.append(found)
    return matched


def _contains_dispatch_failure(value: object) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("action_error"), str) and value["action_error"]:
            return True
        if "missingHandler" in value or "missing_handler" in value:
            return True
        return any(_contains_dispatch_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_dispatch_failure(item) for item in value)
    return False


def _typed_envelope(value: object) -> tuple[bool, object, object]:
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return False, None, None
    return True, value.get("value"), value.get("error")


def _published_bare_rejection(value: object) -> bool:
    """The rejection shapes input/02 publishes beside an `ok:false` envelope.

    "different command content (including a different `target`) must be rejected as a typed conflict (`ok:false` or
    `applied:false`/`status:\"conflict\"`）" -- input/02_interface_and_delivery.md:257-258,
    repeated verbatim at :337-338.  Those two forms are offered as top-level
    ALTERNATIVES to the envelope, and across the four published input files the token
    `ok` occurs exactly twice, both inside that one sentence, so the `ok` envelope is
    not an independently published obligation and a product that answers
    {"applied": false} or {"status": "conflict"} satisfies the published text while
    _typed_envelope() reports it untyped.

    Deliberately narrow: ONLY the two published spellings.  `accepted:false` and the
    statuses `rejected` / `stale` / `unauthorized` / `not_applied` are accepted by
    _typed_rejection INSIDE an `ok:true` envelope but are published nowhere in
    input/0*.md (0 occurrences each; the only published `stale` is the unrelated
    `classification: "stale_generation"`), so they are NOT admitted bare.

    Objects carrying an `ok` key never reach here, so every envelope-shaped response
    keeps exactly its current semantics, and this helper can only turn an unusable
    observation into a usable one -- it can never fail something that passes today.
    """
    if not isinstance(value, dict) or "ok" in value:
        return False
    status = str(value.get("status", "")).lower().replace("-", "_")
    return value.get("applied") is False or status == "conflict"


def _typed_rejection(value: object, *, allow_null_value: bool = False) -> bool:
    typed, observed, error = _typed_envelope(value)
    if not typed:
        return _published_bare_rejection(value)
    if isinstance(value, dict) and value.get("ok") is False:
        return error is not None
    if allow_null_value and observed is None:
        return True
    if not isinstance(observed, dict):
        return False
    status = str(observed.get("status", "")).lower().replace("-", "_")
    return (
        observed.get("applied") is False
        or observed.get("accepted") is False
        or status in {"rejected", "stale", "conflict", "unauthorized", "not_applied"}
    )


def required_action_observation(case_id: str, action: str,
                                record: dict[str, Any] | None,
                                evidence: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Is this required product action observed with a usable product return?

    ``evidence`` is the whole native-evidence document of the same rollout.  It
    is optional and defaults to None so every existing caller keeps working; it
    is needed only by actions whose obligation the dispatcher's synchronous
    return cannot carry on its own (``restart_state``, whose cold restart the
    evaluator performs itself and records out-of-band).
    """
    if record is None:
        return False, "required action was not observed"
    if record.get("dispatch_status") != "completed":
        return False, "dispatcher did not complete the selected action"
    result = record.get("result")
    if _contains_dispatch_failure(result):
        return False, "dispatcher validation error or missing product handler"

    if case_id == "test_003" and action in {"stale_control", "foreign_control"}:
        return (
            (True, "typed product rejection observed")
            if _typed_rejection(result)
            else (False, "intentional control rejection lacks a typed product envelope")
        )
    if case_id == "test_003" and action == "foreign_read":
        return (
            (True, "typed product nondisclosure observed")
            if _typed_rejection(result, allow_null_value=True)
            else (False, "intentional foreign read lacks typed rejection or typed null nondisclosure")
        )

    if action == "start_preview" and case_id == "test_002":
        usable = (
            isinstance(result, dict)
            and result.get("delivery") == "lost_after_product_acceptance"
            and result.get("product_action_accepted") is True
            and isinstance(result.get("operationId"), str)
            and bool(result["operationId"])
            and result.get("sessionIdWithheld") is True
        )
        return usable, "accepted product start followed by intentional response loss" if usable else "lost-response start lacks product-acceptance evidence"

    if action == "conflict_start":
        envelope = result.get("envelope") if isinstance(result, dict) else None
        return (
            (True, "typed product conflict rejection observed")
            if _typed_rejection(envelope, allow_null_value=True)
            else (False, "conflicting operation identity lacks a typed product rejection")
        )

    if action in {"start_preview", "retry_preview"}:
        typed, observed, _error = _typed_envelope(result)
        usable = typed and isinstance(result, dict) and result.get("ok") is True and isinstance(observed, dict) and isinstance(observed.get("sessionId"), str) and bool(observed["sessionId"])
        return usable, "preview session observed" if usable else "preview start/retry lacks a successful typed session envelope"

    if action in {"inspect_latest", "get_preview", "get_run", "get_attestation"}:
        typed, observed, _error = _typed_envelope(result)
        usable = typed and isinstance(result, dict) and result.get("ok") is True and observed is not None
        return usable, "non-null typed read observed" if usable else "required read lacks a successful non-null typed envelope"

    if action in {"stop_tests", "duplicate_stop"}:
        typed, observed, _error = _typed_envelope(result)
        usable = typed and isinstance(result, dict) and result.get("ok") is True and observed is not None
        return usable, "owning Tests action returned a successful typed envelope" if usable else "Tests stop lacks a successful non-null typed product envelope"
    if action == "mutate_target":
        usable = isinstance(result, dict) and result.get("mutated") is True
        return usable, "target mutation observed" if usable else "target mutation was not observed"
    if action == "restore_target":
        usable = isinstance(result, dict) and result.get("restored") is True
        return usable, "target restoration observed" if usable else "target restoration was not observed"
    if action == "restart_state":
        # The generic product-action dispatcher answers this action with
        #   {restart_requested: true, same_disk_state: true, automaticRerunRequested: false}
        # (environment/scenario_chat_flow.test.ts:603-607) and then performs the
        # cold restart itself, recording it in the native evidence's
        # `cold_restart` block (:275-278).  The key `restarted` this rule used to
        # require is emitted nowhere in this tree except the evaluator's own unit
        # stub, so the rule was unsatisfiable on every real rollout and every case
        # whose required_actions contain restart_state was pinned to the
        # acceptance_chain_incomplete ceiling regardless of the Candidate.
        #
        # The obligation is unchanged, only read from the bytes that carry it:
        # input/03_requirements_and_constraints.md:53-54 (item 21) and
        # input/02_interface_and_delivery.md:268-274 require the restart to happen
        # against the same disk state and to re-run nothing automatically.  The
        # fresh-process predicate below is character for character the one
        # evaluator/tests/verify_0909_product_tests.py:62-63 already uses to
        # certify a real cold restart, so no new standard is invented here.
        cold = evidence.get("cold_restart") if isinstance(evidence, dict) else None
        cold = cold if isinstance(cold, dict) else {}
        automatic_runs = cold.get("automatic_test_run_count_at_start")
        fresh = (
            cold.get("actual_fresh_process") is True
            and cold.get("evaluator_rewrote_product_state") is False
            and cold.get("previous_namespace") != cold.get("fresh_namespace")
            and isinstance(automatic_runs, int)
            and not isinstance(automatic_runs, bool)
            and automatic_runs == 0
        )
        requested = (
            isinstance(result, dict)
            and result.get("restart_requested") is True
            and result.get("same_disk_state") is True
            and result.get("automaticRerunRequested") is False
        )
        usable = requested and fresh
        return usable, ("state restart observed without requested rerun" if usable
                        else "restart observation is incomplete")
    if action == "compatibility_chat":
        usable = isinstance(result, dict) and int(result.get("streamEnd", 0) or 0) > 0 and int(result.get("responseErrors", -1) or 0) == 0
        return usable, "ordinary typed chat completed" if usable else "ordinary chat compatibility observation is incomplete"
    if action == "finish":
        usable = isinstance(result, dict) and result.get("captured") is True and isinstance(result.get("artifactSha256"), str)
        return usable, "model artifact captured" if usable else "finish did not capture a model artifact"
    return False, "required action has no fail-closed observation rule"


TERMINAL_PREVIEW_STATES = frozenset({
    "passed", "failed", "infrastructure", "cancelled", "invalidated"})
RUN_SNAPSHOT_ACTIONS = ("inspect_latest", "get_run")
SESSION_VALUE_ACTIONS = ("start_preview", "retry_preview", "get_preview")


def _ok_value(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The typed product value of a completed action, or None.

    Only a successful typed envelope counts, which is the same rule the
    required-action observations use, so one helper decides both.
    """
    if not isinstance(record, dict) or record.get("dispatch_status") != "completed":
        return None
    result = record.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    value = result.get("value")
    return value if isinstance(value, dict) else None


def _positive_sequence(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def canonical_result_digest(session: dict[str, Any]) -> str:
    """The published resultDigest of a terminal session.

    input/02 'Durable attestation surface' pins this exactly: SHA-256 over the
    UTF-8 bytes of the canonical JSON text of ``{"result", "status", "target"}``
    taken from the session's terminal facts, object keys sorted ascending and no
    insignificant whitespace.  ``json.dumps(..., sort_keys=True,
    separators=(",", ":"), ensure_ascii=False)`` is that text, and it equals
    JavaScript ``JSON.stringify`` over the same object with sorted keys.
    """
    document = {
        "result": session.get("result"),
        "status": session.get("status"),
        "target": session.get("target"),
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(text.encode("utf-8"))


def contract_check_results(oracle: dict[str, Any], evidence: dict[str, Any],
                           actions: list[dict[str, Any]]) -> dict[str, bool]:
    """Cross-cutting product obligations, decided only from product returns.

    Every value here is derived from bytes the product already produced in this
    rollout (the dispatch trajectory and the evaluator's own read probes).  The
    agent's prose is never consulted, and no check costs a lower-agent turn.
    A check the rollout could not observe is False, never absent: an
    unobservable obligation is an unmet one, and the lower acceptance-chain
    ceiling already governs rollouts that never reached the surface.
    """
    run_snapshots = [value for value in
                     (_ok_value(item) for item in actions
                      if isinstance(item, dict) and item.get("action") in RUN_SNAPSHOT_ACTIONS)
                     if value is not None]
    session_values: list[dict[str, Any]] = []
    start_pairs: list[tuple[object, object]] = []
    for item in actions:
        if not isinstance(item, dict) or item.get("action") not in SESSION_VALUE_ACTIONS:
            continue
        value = _ok_value(item)
        if value is None:
            continue
        session_values.append(value)
        if item.get("action") in ("start_preview", "retry_preview"):
            request = item.get("request")
            command = request.get("command") if isinstance(request, dict) else None
            if isinstance(command, dict) and "target" in command:
                start_pairs.append((command.get("target"), value.get("target")))
    attestation_values = [value for value in
                          (_ok_value(item) for item in actions
                           if isinstance(item, dict) and item.get("action") == "get_attestation")
                          if value is not None]

    target = oracle.get("target")
    results: dict[str, bool] = {}

    # The product publishes the current Acceptance target, so a caller never has
    # to guess a field of it (input/02 'Acceptance target identity').
    results["current_target_binds_started_target"] = isinstance(target, dict) and any(
        snapshot.get("currentTarget") == target for snapshot in run_snapshots)

    # The accepted command's target survives into the session unchanged.
    results["session_target_preserved"] = bool(start_pairs) and all(
        isinstance(requested, dict) and observed == requested
        for requested, observed in start_pairs)

    # testFingerprint is the SHA-256 of the admitted target file's raw bytes.
    admitted = evidence.get("target_bytes_sha256_at_admission")
    observed_fingerprints = {value.get("testFingerprint")
                             for value in session_values + attestation_values
                             if isinstance(value.get("testFingerprint"), str)}
    results["fingerprint_is_admitted_target_bytes"] = (
        isinstance(admitted, str) and len(admitted) == 64
        and bool(observed_fingerprints) and observed_fingerprints == {admitted})

    # lastEventSequence never decreases inside one session.
    highest: dict[str, int] = {}
    monotonic = bool(session_values)
    for value in session_values:
        session_id = value.get("sessionId")
        sequence = _positive_sequence(value.get("lastEventSequence"))
        if not isinstance(session_id, str) or not session_id or sequence is None:
            monotonic = False
            continue
        if sequence < highest.get(session_id, 0):
            monotonic = False
        highest[session_id] = max(sequence, highest.get(session_id, 0))
    results["event_sequence_never_decreases"] = monotonic

    # Reading does not change state: an immutable attestation and a terminal
    # session read back byte for byte.  The evaluator's own repeat reads are
    # recorded by the run-local harness.
    probes = evidence.get("read_stability_probes")
    results["terminal_and_attestation_reads_byte_stable"] = (
        isinstance(probes, list) and bool(probes)
        and all(isinstance(item, dict) and item.get("stable") is True for item in probes))

    # resultDigest is the published canonical digest of the terminal facts.
    terminal = next((value for value in reversed(session_values)
                     if str(value.get("status", "")) in TERMINAL_PREVIEW_STATES), None)
    attestation = attestation_values[-1] if attestation_values else None
    results["result_digest_matches_terminal_facts"] = bool(
        terminal is not None and attestation is not None
        and isinstance(attestation.get("resultDigest"), str)
        and attestation["resultDigest"] == canonical_result_digest(terminal))

    # A runner result that is not a current matching `passed` attestation may
    # not carry the run to `passed`.
    gates = evidence.get("acceptance_gate_probes")
    if isinstance(gates, list) and gates:
        unresolved = [item for item in gates if isinstance(item, dict)
                      and str(item.get("attestation_outcome", "")) != "passed"]
        results[GATE_CONTRACT_CHECK] = all(
            item.get("run_snapshot_readable") is True
            and str(item.get("run_status", "")).lower() != "passed"
            and item.get("passing_evidence_cites_attestation") is not True
            for item in unresolved)
    else:
        results[GATE_CONTRACT_CHECK] = False
    return results


def _binding(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in (
        "app_id", "chat_id", "run_id", "session_id", "revision", "target_fingerprint"
    )}


def _valid_binding(value: dict[str, Any]) -> bool:
    for key in ("app_id", "chat_id"):
        item = value.get(key)
        if isinstance(item, bool) or not (
            (isinstance(item, int) and item > 0)
            or (isinstance(item, str) and bool(item.strip()))
        ):
            return False
    return all(
        isinstance(value.get(key), str) and bool(value[key].strip())
        for key in ("run_id", "session_id", "revision", "target_fingerprint")
    )


def compare_scenario(private_oracle: Path, native_evidence: Path,
                     agent_artifact: Path | None, destination: Path) -> dict[str, Any]:
    oracle = read_json(private_oracle)
    try:
        evidence = read_json(native_evidence)
        evidence_error = None
    except Exception as exc:
        evidence = {}
        evidence_error = f"{type(exc).__name__}: {exc}"
    artifact: dict[str, Any] = {}
    artifact_error = None
    if agent_artifact and agent_artifact.is_file():
        try:
            artifact = read_json(agent_artifact)
        except Exception as exc:
            artifact_error = f"{type(exc).__name__}: {exc}"
    else:
        artifact_error = "model-authored agent artifact is absent"

    trajectory = evidence.get("product_action_trajectory")
    actions = [item for item in trajectory if isinstance(item, dict)] if isinstance(trajectory, list) else []
    names = [item.get("action") for item in actions if isinstance(item, dict) and isinstance(item.get("action"), str)]
    all_model_selected = bool(actions) and all(
        isinstance(item, dict)
        and item.get("requested_by_model") is True
        and item.get("executor") == "generic_model_action_dispatch"
        and item.get("evaluator_defaulted") is False
        and isinstance(item.get("source_message_id"), (str, int))
        and not isinstance(item.get("source_message_id"), bool)
        and isinstance(item.get("source_message_sha256"), str)
        and len(item["source_message_sha256"]) == 64
        for item in actions
    )
    evidence_binding = _binding(evidence.get("workspace"))
    artifact_binding = _binding(artifact.get("workspace"))
    artifact_actions = artifact.get("product_actions") if isinstance(artifact.get("product_actions"), list) else []
    required = list(oracle.get("required_actions", []))
    matched = required_action_records(required, actions)
    observation_details: list[dict[str, Any]] = []
    for name, record in zip(required, matched):
        usable, reason = required_action_observation(str(oracle.get("case_id", "")), name, record,
                                                     evidence)
        observation_details.append({
            "action": name,
            "observed": record is not None,
            "sequence": record.get("sequence") if record else None,
            "usable": usable,
            "reason": reason,
        })
    required_observations_usable = bool(observation_details) and all(item["usable"] for item in observation_details)
    no_dispatch_failures = all(
        item.get("dispatch_status") == "completed"
        and not _contains_dispatch_failure(item.get("result"))
        for item in actions
    )
    checks = {
        "case_id": evidence.get("case_id") == oracle.get("case_id") == artifact.get("case_id"),
        "scenario_id": evidence.get("scenario_id") == oracle.get("scenario_id") == artifact.get("scenario_id"),
        "executed_task": evidence.get("executed_task_sha256") == oracle.get("executed_task_sha256") == artifact.get("task_sha256"),
        "model_selected_actions_only": all_model_selected,
        "required_action_order": ordered_subsequence(required, names),
        "required_action_observations_usable": required_observations_usable,
        "no_dispatch_validation_or_handler_failures": no_dispatch_failures,
        "no_generic_single_flow_fallback": evidence.get("generic_fallback_used") is False,
        "model_authored_artifact": evidence.get("agent_artifact_origin") == "model_finish_action" and artifact_error is None,
        "binding_matches_native_evidence": _valid_binding(evidence_binding) and evidence_binding == artifact_binding,
        "artifact_binds_action_trajectory": artifact_actions == names,
        "private_oracle_not_candidate_visible": oracle.get("private_not_candidate_visible") is True and evidence.get("private_oracle_visible") is False,
    }
    scenario_checks = evidence.get("scenario_checks") if isinstance(evidence.get("scenario_checks"), dict) else {}
    for key in oracle.get("required_scenario_checks", []):
        checks[f"scenario:{key}"] = scenario_checks.get(key) is True
    # Cross-cutting product obligations are decided here rather than inside the
    # run-local harness, so they are recomputable from persisted evidence and
    # are covered by evaluator/tests/test_0920_product_contract.py.
    contract = contract_check_results(oracle, evidence, actions)
    required_contract = oracle.get("required_contract_checks")
    if not isinstance(required_contract, list) or not required_contract:
        required_contract = list(UNIVERSAL_CONTRACT_CHECKS)
    for key in required_contract:
        checks[f"contract:{key}"] = contract.get(key) is True
    result = {
        "schema_version": "dyad-run-local-private-oracle-comparison-v1",
        "case_id": oracle.get("case_id"),
        "scenario_id": oracle.get("scenario_id"),
        "axis": oracle.get("axis"),
        "executed_task_sha256": oracle.get("executed_task_sha256"),
        "required_actions": oracle.get("required_actions"),
        "required_contract_checks": required_contract,
        "observed_actions": names,
        "required_action_observations": observation_details,
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
        "native_evidence_error": evidence_error,
        "agent_artifact_error": artifact_error,
        "native_evidence_sha256": file_sha256(native_evidence) if native_evidence.is_file() else None,
        "agent_artifact_sha256": file_sha256(agent_artifact) if agent_artifact and agent_artifact.is_file() else None,
        "private_oracle_sha256": file_sha256(private_oracle),
        "private_oracle_path": str(private_oracle),
        "private_oracle_not_candidate_visible": True,
    }
    write_json(destination, result)
    return result
