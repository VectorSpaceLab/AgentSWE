#!/usr/bin/env python3
"""Manual budget-exhausted freeze -> hidden -> finalize for ONE openwiki formal run.

Why this exists
---------------
The protocol says: when the Builder exits OR its budget is exhausted, the latest
accepted Candidate is frozen and the six hidden cases run against it.  In
formal_one_stop.run_formal the freeze is gated on ``native["valid"]``, and a
Harbor AgentTimeoutError at the budget end leaves the native stream without a
successful terminal event, so the run stopped at ``builder_lifecycle_incomplete``
with ``freeze=None`` although every accepted round is intact.

This tool performs, after the fact, exactly what run_formal would have done had
that gate let the freeze through -- and nothing else:

  stage freeze    Controller.freeze() of the sibling tree, on the retained
                  controller_state.json (same code path as freeze_latest_accepted;
                  the manifest additionally carries freeze_reason / manual_freeze /
                  builder_exit_evidence naming the manual, budget-exhausted cause).
  stage hidden    Controller.hidden() of the sibling tree (fresh evaluator-owned
                  lower broker, deepseek-flash/high, hidden-once gate).
  stage finalize  a fresh evaluator-owned Result-judge broker (deepseek-flash /
                  max / 64000, same judge_broker_runtime) + the tree's own
                  evaluator/formal_finalize.py with run_formal's exact argv and the
                  formal unit's environment; then summary.json / one_stop_summary.json
                  are rewritten the way run_formal + the shared summary writer do.

It never restarts the Builder, never edits a Candidate byte, never writes under
@@AGENTSWE_LEGACY_HOME@@ (tree / control plane), never rebinds, and never prints a credential.
Every pre-existing file it rewrites is first copied to <name>.pre-manual-freeze
(refusing if such a backup already exists).  Every stage is --dry-run first.

usage (run as root, from anywhere):
  /usr/bin/python3 -E -s -B manual_freeze_budget_exhausted.py --stage freeze   --dry-run
  /usr/bin/python3 -E -s -B manual_freeze_budget_exhausted.py --stage freeze   --apply --operator "..."
  ... --stage hidden   --dry-run | --apply
  ... --stage finalize --dry-run | --apply
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

# ----------------------------------------------------------------- constants --
RUN_DIR = Path("@@AGENTSWE_EDITING_RUNS@@/formal/codex_xhigh/openwiki/"
               "0905-edit-codex-xhigh-0922-tc-v1-001-openwiki")
TREE = Path("@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree")
CONTROL = Path("@@AGENTSWE_EDITING_CONTROL@@")
UNIT = "agentswe-formal-openwiki-0922-tc-v1-001"
LAUNCH_RECORD = Path("@@AGENTSWE_EDITING_RUNS@@/formal/launch_control/"
                     "0905-edit-codex-xhigh-0922-tc-v1-001/openwiki/launch_record.json")
CREDENTIAL = Path("@@AGENTSWE_CREDENTIAL_FILE@@")  # path only; never read here
UPSTREAM = "https://api.deepseek.com"          # run_formal default; the unit sets no AGENTSWE_UPSTREAM_BASE_URL
MAX_DEV_ROUNDS = 5
N_CONCURRENT = 1
EXPECTED_BINDING = {  # launch-time binding (launch_record sibling_digest + readiness_current_binding 0922-tc-r-001)
    "task": "openwiki",
    "source_digest": "bbb211a2eff2c80a5773e96229a285cac6e8399ba6ac1f8f6942db8f281d8b12",
    "contract_digest": "b343f700fc02f298effc9776e930a6b3daf55e0de06d75328f94361c709fe390",
    "registry_digest": "bfb8e9ee9a5c6c82ea4b60fb0c1908a3d21737339a199e8d69d6f33be174b85b",
}
# Package 122 (2026-09-24, after hidden): lower_agent_launcher.py deadline-kill attribution; the
# only tree change since launch.  Accepted only together with the 122 launcher bytes.
BINDING_122 = {
    "task": "openwiki",
    "source_digest": "617e59844607cb5b58266c053b7a3634b818593e4e5706d5282d6b0f4aebcade",
    "contract_digest": "b343f700fc02f298effc9776e930a6b3daf55e0de06d75328f94361c709fe390",
    "registry_digest": "63d6e7146389d6f01d9711d648322875895a640053b9310f34bc0a9740b06ddf",
}
LAUNCHER_SHA_122 = "b9f9816eb6df6885857499fbf05af57d1051b6248696c4ea924535ced76fcc5c"
EXPECTED_RESULT_JUDGE_SHA = "d4157b41734fac87dc029f109d6c242d7aa3507e601d36e70266f646d3a084c6"
EXPECTED_SESSION = "01a0c992-ed25-7bc0-8733-d329644ee18e"
EXPECTED_DIGESTS = ["d401cf74d14d5d8cdd775a8724916ef9222866df4a8956ba02e7a1f136cffc96",
                    "d01628cda87ae9f9ce78efdaba5b2ce7719c5f67cf0fa9505ed40747294052c2"]
# Environment of the formal unit (systemctl cat agentswe-formal-openwiki-0922-tc-v1-001).
UNIT_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "AGENTSWE_RESULT_JUDGE": str(CONTROL / "result_judge.py"),
    "AGENTSWE_CODE_JUDGE": str(CONTROL / "code_judge_runner.py"),
    "AGENTSWE_CREATE_ALIGNMENT_SNAPSHOT": str(CONTROL / "create_alignment_snapshot.json"),
    "AGENTSWE_EDIT_PROFILE_SCOPE": "codex_xhigh_only",
    "AGENTSWE_EDIT_EARLY_STOP_RESAMPLE": "1",
}
FREEZE_REASON = "budget_exhausted_freeze_latest_accepted"
BACKUP_SUFFIX = ".pre-manual-freeze"
EVIDENCE = RUN_DIR / "manual_freeze"
TOOL = Path(__file__).resolve()

LIFECYCLE = RUN_DIR / "lifecycle"
FREEZE_PATH = LIFECYCLE / "freeze_manifest.json"
SEAL_PATH = LIFECYCLE / "freeze_manifest.sha256"
FROZEN_REPO = LIFECYCLE / "frozen_candidate" / "repository"
GATE_PATH = LIFECYCLE / "hidden-once-gate.json"
HIDDEN_RESULT = LIFECYCLE / "hidden-result.json"
HIDDEN_ATTEST = LIFECYCLE / "hidden-after-freeze-attestation.json"
AGGREGATION = RUN_DIR / "formal_aggregation.json"

if str(TREE) not in sys.path:
    sys.path.insert(0, str(TREE))


class Refusal(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-manual-freeze")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def backup(path: Path, apply: bool, log: list) -> None:
    target = path.with_name(path.name + BACKUP_SUFFIX)
    if not path.exists():
        log.append(f"backup: {path} does not exist (will be created new)")
        return
    if target.exists():
        raise Refusal(f"backup already exists, refusing to overwrite: {target}")
    log.append(f"backup: {path.name} -> {target.name}")
    if apply:
        shutil.copy2(path, target)


def say(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ gates ----
def active_units() -> list[str]:
    out = subprocess.run(["systemctl", "list-units", "agentswe-*", "--state=active",
                          "--no-pager", "--no-legend", "--plain"], capture_output=True, text=True)
    units = [line.split()[0] for line in out.stdout.splitlines() if line.strip()]
    own = os.environ.get("INVOCATION_ID") and os.environ.get("AGENTSWE_MANUAL_FREEZE_UNIT")
    return [u for u in units
            if u != "agentswe-loopback-proxy.service"  # host infrastructure, not a run
            and not (own and u == os.environ["AGENTSWE_MANUAL_FREEZE_UNIT"] + ".service")]


def docker_inventory() -> list[str]:
    ids = subprocess.run(["docker", "ps", "-aq", "--no-trunc"], capture_output=True, text=True,
                         check=True).stdout.split()
    rows = []
    for cid in ids:
        out = subprocess.run(["docker", "inspect", "--format",
                              "{{.Id}} {{.Name}} {{.State.Status}} {{range .Mounts}}{{.Source}},{{end}}", cid],
                             capture_output=True, text=True)
        if out.returncode == 0:  # Mounts order is not stable across inspects: sort it
            head, _, mounts = out.stdout.strip().partition(" ")
            name, _, rest = mounts.partition(" ")
            status, _, sources = rest.partition(" ")
            rows.append(" ".join([head, name, status, ",".join(sorted(filter(None, sources.split(","))))]))
    return sorted(rows)


def machine_quiet(settle: int, log: list) -> dict:
    units = active_units()
    if units:
        raise Refusal("active agentswe units (another run is live): " + ", ".join(units))
    first = docker_inventory()
    if settle:
        time.sleep(settle)
    second = docker_inventory()
    if first != second:
        raise Refusal("docker inventory changed during the quiet window: "
                      f"{len(first)} -> {len(second)} containers")
    ours = [row for row in second if str(RUN_DIR) in row]
    if ours:
        raise Refusal("containers still mount this run: " + "; ".join(r[:120] for r in ours))
    log.append(f"machine quiet: no active agentswe run units; {len(second)} container(s) total, "
               f"stable over {settle}s, none mounting this run")
    return {"active_agentswe_units": [], "containers": len(second), "stable_seconds": settle,
            "container_rows": second}


def machine_after() -> dict:
    try:
        return machine_quiet(0, [])
    except Refusal as exc:
        return {"not_quiet": str(exc)}


def binding_now() -> dict:
    if str(CONTROL) not in sys.path:
        sys.path.insert(0, str(CONTROL))
    from audit_readiness import tree_digest as audit_tree_digest
    from readiness_admission import registry_digest
    from readiness_binding import verify_binding
    binding = {"task": "openwiki", "source_digest": audit_tree_digest(TREE),
               "contract_digest": sha256_file(TREE / "meta/0905_case_contract.json"),
               "registry_digest": registry_digest(CONTROL / "configuration_delta_registry.json", "openwiki")}
    verify_binding(TREE, binding, control_root=CONTROL)
    return binding


def common_checks(log: list) -> dict:
    if os.geteuid() != 0:
        raise Refusal("run as root (run directory is root-owned)")
    if not RUN_DIR.is_dir():
        raise Refusal(f"run dir missing: {RUN_DIR}")
    state = subprocess.run(["systemctl", "is-active", UNIT], capture_output=True, text=True).stdout.strip()
    if state == "active":
        raise Refusal(f"{UNIT} is active")
    log.append(f"{UNIT}: {state}")
    binding = binding_now()
    if binding == EXPECTED_BINDING:
        log.append("tree binding == launch-time binding (source/contract/registry) and verify_binding passed")
    elif (binding == BINDING_122
          and sha256_file(TREE / "agentloop/evaluator/lower_agent_launcher.py") == LAUNCHER_SHA_122):
        log.append("tree binding == post-122 binding (launch binding + package 122 launcher only); "
                   "verify_binding passed")
    else:
        raise Refusal("tree binding differs from launch-time and post-122 bindings: " + json.dumps(binding))
    judge_sha = sha256_file(CONTROL / "result_judge.py")
    if judge_sha != EXPECTED_RESULT_JUDGE_SHA:
        raise Refusal("result_judge.py differs from the launch record")
    log.append("result_judge.py sha256 == launch_record.result_judge_sha256")
    launch = read_json(LAUNCH_RECORD)
    if launch.get("run_dir") != str(RUN_DIR) or launch.get("code_axis") != "skipped_by_policy":
        raise Refusal("launch record does not describe this run")
    return {"binding": binding, "result_judge_sha256": judge_sha, "unit_state": state}


def check_accepted_history(log: list) -> dict:
    from agentloop.protocol import canonical_json, tree_digest, validate_internal_symlinks
    att = read_json(RUN_DIR / "builder_session_attestation.json")
    state = read_json(LIFECYCLE / "controller_state.json")
    seg = read_json(RUN_DIR / "builder_segment_receipt.json")
    native_errors = att["native_evidence"].get("errors")
    checks = {
        "attestation.accepted_submission_count == 2": att.get("accepted_submission_count") == 2,
        "attestation.builder_session_id": att.get("builder_session_id") == EXPECTED_SESSION,
        "attestation.builder_exit_code == 0": att.get("builder_exit_code") == 0,
        "attestation native errors == [no successful terminal event] only":
            native_errors == ["native Builder has no successful terminal event"],
        "attestation.candidate_digests_distinct": att.get("candidate_digests_distinct") is True,
        "attestation.accepted_submissions_with_public_dev": att.get("accepted_submissions_with_public_dev") is True,
        "attestation.feedback_chain_consumed": att.get("feedback_chain_consumed") is True,
        "segment: one attempt, infrastructure_cut / AgentTimeoutError, no resume (budget)":
            len(seg.get("attempts", [])) == 1
            and seg["attempts"][0].get("exit_reason") == "infrastructure_cut"
            and (seg["attempts"][0].get("harbor_exception") or {}).get("exception_type") == "AgentTimeoutError"
            and seg["attempts"][0]["decision"].get("resume") is False,
        "controller_state.builder_session_id": state.get("builder_session_id") == EXPECTED_SESSION,
        "controller_state.product_execution_guard": state.get("product_execution_guard") == "openwiki-product-no-replay/v1",
        "controller_state.readiness_profile/current_binding None":
            state.get("readiness_profile") is None and state.get("current_binding") is None,
        "controller_state records == attestation candidate_records":
            state.get("records") == att.get("candidate_records"),
        "accepted digests": [r.get("candidate_digest") for r in state.get("records", [])] == EXPECTED_DIGESTS,
        "rounds 1..2": [r.get("round") for r in state.get("records", [])] == [1, 2],
    }
    records = state["records"]
    feedback_events = [e for e in att["events"] if e.get("event") == "feedback_delivered"]
    checks["feedback_delivered events carry candidate_number 1,2 in order"] = (
        [e.get("candidate_number") for e in feedback_events] == [1, 2])
    per_round = []
    for index, record in enumerate(records, start=1):
        cand = Path(record["candidate_path"])
        entry = Path(record["build"]["product_entry"])
        repo = entry.parent.parent
        fb = record["feedback"]
        payload = json.loads(Path(fb["json"]).read_bytes())
        recomputed = hashlib.sha256(canonical_json(
            {k: v for k, v in payload.items() if k != "feedback_digest"})).hexdigest()
        event = feedback_events[index - 1] if len(feedback_events) >= index else {}
        row = {
            "round": record["round"], "role": record.get("role"),
            "candidate_digest": record["candidate_digest"],
            "submission_tree_digest_matches": tree_digest(cand) == record["candidate_digest"],
            "submission_files": sorted(p.name for p in cand.iterdir()),
            "build_valid": record["build"].get("valid") is True,
            "product_entry_exists": entry.is_file(),
            "accepted_repository": str(repo),
            "feedback_available": fb.get("available") is True,
            "feedback_infrastructure_invalid": fb.get("infrastructure_invalid"),
            "feedback_digest": fb.get("feedback_digest"),
            "feedback_bytes_match_digest": recomputed == fb.get("feedback_digest") == payload.get("feedback_digest"),
            "feedback_delivered_event_digest_matches": event.get("feedback_digest") == fb.get("feedback_digest"),
            "dev_cases": list(record.get("dev_cases", {})),
            "same_builder_session": record.get("builder_session_id") == EXPECTED_SESSION,
            "builder_metadata_valid": record["builder_metadata"].get("valid") is True,
            "revision": record.get("revision"),
        }
        if index >= 2:
            prev = records[index - 2]
            row["bound_to_previous_feedback"] = (
                record["revision"].get("parent_candidate_digest") == prev["candidate_digest"]
                and record["revision"].get("feedback_digest") == prev["feedback"]["feedback_digest"]
                and record["revision"].get("feedback_bound_submission") is True
                and record["revision"].get("same_builder_session") is True)
        per_round.append(row)
    latest = records[-1]
    repo = Path(latest["build"]["product_entry"]).parent.parent
    validate_internal_symlinks(repo)
    accepted_repo_digest = tree_digest(repo)
    # Nothing in the accepted repository may postdate the round-2 submission_finished event.
    finished = [e for e in att["events"] if e.get("event") == "submission_finished"
                and e.get("candidate_digest") == latest["candidate_digest"]]
    finished_at = datetime.fromisoformat(finished[-1]["at"]).timestamp() if finished else None
    newest = 0.0
    for base, dirs, files in os.walk(repo):
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(base, name)).st_mtime)
            except OSError:
                pass
    checks["accepted repository untouched after round-2 submission_finished"] = (
        finished_at is not None and newest <= finished_at + 1)
    for row in per_round:
        for key in ("submission_tree_digest_matches", "build_valid", "product_entry_exists",
                    "feedback_available", "feedback_bytes_match_digest",
                    "feedback_delivered_event_digest_matches", "same_builder_session",
                    "builder_metadata_valid"):
            checks[f"round {row['round']}: {key}"] = row[key] is True
        checks[f"round {row['round']}: dev_cases == dev_001,dev_002"] = row["dev_cases"] == ["dev_001", "dev_002"]
        checks[f"round {row['round']}: feedback not infra-invalid"] = row["feedback_infrastructure_invalid"] is False
        if "bound_to_previous_feedback" in row:
            checks[f"round {row['round']}: bound_to_previous_feedback"] = row["bound_to_previous_feedback"]
    failed = [name for name, ok in checks.items() if not ok]
    for name, ok in checks.items():
        log.append(("  ok   " if ok else "  FAIL ") + name)
    if failed:
        raise Refusal("accepted-history checks failed: " + "; ".join(failed))
    return {"checks": checks, "rounds": per_round, "accepted_repository": str(repo),
            "accepted_repository_tree_digest": accepted_repo_digest,
            "accepted_repository_newest_mtime": datetime.fromtimestamp(newest, timezone.utc).isoformat(),
            "round2_submission_finished_at": finished[-1]["at"] if finished else None,
            "native_evidence_errors": native_errors,
            "builder_started_at": att.get("started_at"), "builder_finished_at": att.get("finished_at"),
            "segment_attempt": {k: seg["attempts"][0].get(k) for k in
                                ("exit_reason", "exit_code", "rollout_sha256", "started_at_epoch",
                                 "ended_at_epoch")},
            "harbor_exception": {k: seg["attempts"][0]["harbor_exception"].get(k)
                                 for k in ("exception_type", "exception_message", "occurred_at")},
            "resume_decision": {k: seg["attempts"][0]["decision"].get(k)
                                for k in ("resume", "refusals", "remaining_seconds")}}


def one_stop_constants() -> tuple[str, str, str]:
    """The run's judge image/model/effort, read from the tree's formal_one_stop.py source
    (not imported: importing it would pull in the Builder transport modules)."""
    import ast
    wanted = {"BUILDER_IMAGE": None, "RESULT_JUDGE_MODEL": None, "RESULT_JUDGE_EFFORT": None}
    for node in ast.parse((TREE / "harbor/formal_one_stop.py").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in wanted and isinstance(node.value, ast.Constant):
                wanted[name] = node.value.value
    if wanted != {"BUILDER_IMAGE": "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812",
                  "RESULT_JUDGE_MODEL": "deepseek-flash", "RESULT_JUDGE_EFFORT": "max"}:
        raise Refusal("formal_one_stop judge constants changed: " + json.dumps(wanted))
    return wanted["BUILDER_IMAGE"], wanted["RESULT_JUDGE_MODEL"], wanted["RESULT_JUDGE_EFFORT"]


def make_controller():
    from agentloop.evaluator.controller import Controller
    controller = Controller(
        TREE / "input/repository", TREE, LIFECYCLE, "http://127.0.0.1:9/unused-after-builder",
        builder_session_id=EXPECTED_SESSION,
        hidden_credential_file=CREDENTIAL,
        hidden_upstream=UPSTREAM,
        max_dev_rounds=MAX_DEV_ROUNDS,
    )
    if controller.legacy_execution_read_only:
        raise Refusal("controller loaded the run as legacy read-only")
    return controller


# ------------------------------------------------------------------ stages ---
def stage_freeze(args, log: list) -> int:
    gate = common_checks(log)
    for path in (FREEZE_PATH, SEAL_PATH, FROZEN_REPO.parent, GATE_PATH, HIDDEN_RESULT, AGGREGATION):
        if path.exists():
            raise Refusal(f"already exists (freeze/hidden already happened?): {path}")
    att = read_json(RUN_DIR / "builder_session_attestation.json")
    if att.get("freeze") is not None:
        raise Refusal("attestation already carries a freeze")
    history = check_accepted_history(log)
    quiet = machine_quiet(0, log)
    controller = make_controller()
    if controller.frozen is not None or len(controller.records) != 2:
        raise Refusal("retained controller state is not the expected unfrozen 2-round history")
    latest = controller.records[-1]
    free = shutil.disk_usage(RUN_DIR).free
    if free < 5 * 2**30:
        raise Refusal("less than 5 GiB free on the run filesystem")
    operated_at = now()
    manual = {
        "schema_version": "agentswe-edit-manual-freeze/v1",
        "reason": FREEZE_REASON,
        "manual": True,
        "operated_at": operated_at,
        "operator": args.operator,
        "tool": str(TOOL), "tool_sha256": sha256_file(TOOL),
        "protocol_basis": "Builder exit OR budget exhaustion freezes the latest accepted Candidate; "
                          "Harbor cut the session at the budget end (AgentTimeoutError) before "
                          "formal_one_stop.run_formal reached controller.freeze(), whose gate requires "
                          "native['valid'] (a successful native terminal event).",
        "builder_exit_evidence": {
            "builder_exit_code": att.get("builder_exit_code"),
            "native_valid": False,
            "native_errors": history["native_evidence_errors"],
            "harbor_exception": history["harbor_exception"],
            "segment_exit_reason": history["segment_attempt"]["exit_reason"],
            "resume_decision": history["resume_decision"],
            "builder_started_at": history["builder_started_at"],
            "builder_finished_at": history["builder_finished_at"],
            "builder_session_id": EXPECTED_SESSION,
        },
        "not_done": ["Builder not restarted", "Candidate bytes not modified",
                     "task tree / control plane not modified", "no rebind"],
        "freeze_code_path": "agentloop.evaluator.controller.Controller.freeze (sibling tree, unmodified), "
                            "called on the retained lifecycle/controller_state.json",
    }
    log.append(f"latest accepted: round {latest['round']} digest {latest['candidate_digest'][:12]} "
               f"repo {history['accepted_repository']} tree_digest {history['accepted_repository_tree_digest'][:12]}")
    log.append("would write: lifecycle/frozen_candidate/repository (copy, read-only), lifecycle/freeze_manifest.json "
               "(+ .sha256 seal, 0444), lifecycle/controller_state.json (frozen), builder_session_attestation.json "
               "(freeze + manual_freeze), manual_freeze/manual_freeze_record.json")
    for path in (LIFECYCLE / "controller_state.json", RUN_DIR / "builder_session_attestation.json"):
        backup(path, args.apply, log)
    if not args.apply:
        log.append("DRY-RUN: nothing written")
        return 0

    EVIDENCE.mkdir(exist_ok=True)
    import agentloop.evaluator.controller as controller_module
    original_write_json = controller_module.write_json
    extra = {"freeze_reason": FREEZE_REASON, "manual_freeze": True,
             "manual_freeze_operated_at": operated_at, "manual_freeze_operator": args.operator,
             "manual_freeze_record": str(EVIDENCE / "manual_freeze_record.json")}

    def write_json_with_reason(path, value):
        if Path(path).resolve() == FREEZE_PATH.resolve() and isinstance(value, dict):
            value.update(extra)  # same dict object becomes controller.frozen and controller_state.frozen
        return original_write_json(path, value)

    controller_module.write_json = write_json_with_reason
    try:
        frozen = controller.freeze(builder_exit_evidence=manual["builder_exit_evidence"])
    finally:
        controller_module.write_json = original_write_json
    # post-verification with the evaluator's own validators
    from agentloop.evaluator.hidden_controller import validate_freeze
    from agentloop.protocol import tree_digest
    repository = validate_freeze(read_json(FREEZE_PATH), LIFECYCLE, FREEZE_PATH)
    state = read_json(LIFECYCLE / "controller_state.json")
    post = {
        "validate_freeze": "passed", "frozen_repository": str(repository),
        "state_frozen_equals_manifest": state.get("frozen") == read_json(FREEZE_PATH),
        "frozen_digest_equals_accepted_repository_digest":
            frozen["repository_digest"] == history["accepted_repository_tree_digest"]
            == tree_digest(Path(history["accepted_repository"])),
        "freeze_manifest_sha256": sha256_file(FREEZE_PATH),
    }
    if not (post["state_frozen_equals_manifest"] and post["frozen_digest_equals_accepted_repository_digest"]):
        raise Refusal("post-freeze verification failed: " + json.dumps(post))
    manual.update(freeze_manifest=str(FREEZE_PATH), freeze=frozen, post_verification=post,
                  pre_checks=history["checks"], gate=gate, machine=quiet["active_agentswe_units"])
    write_json(EVIDENCE / "manual_freeze_record.json", manual)
    att["freeze"] = frozen
    att["manual_freeze"] = {k: manual[k] for k in ("reason", "manual", "operated_at", "operator",
                                                   "tool", "tool_sha256", "protocol_basis")}
    att["manual_freeze"]["record"] = str(EVIDENCE / "manual_freeze_record.json")
    att["manual_freeze"]["complete_field_note"] = (
        "complete stays false: it is run_formal's own conjunction and native['valid'] is false; "
        "the freeze was made manually under the budget-exhaustion rule")
    write_json(RUN_DIR / "builder_session_attestation.json", att)
    log.append(f"FROZEN at {frozen['frozen_at']}: candidate_digest {frozen['candidate_digest']} "
               f"source_submission {frozen['source_submission_id']}")
    return 0


def stage_hidden(args, log: list) -> int:
    common_checks(log)
    if not FREEZE_PATH.is_file():
        raise Refusal("no freeze manifest; run --stage freeze first")
    for path in (GATE_PATH, HIDDEN_RESULT, HIDDEN_ATTEST, AGGREGATION):
        if path.exists():
            raise Refusal(f"already exists: {path}")
    from agentloop.evaluator.hidden_controller import validate_freeze
    freeze = read_json(FREEZE_PATH)
    validate_freeze(freeze, LIFECYCLE, FREEZE_PATH)
    if freeze.get("freeze_reason") != FREEZE_REASON or freeze.get("manual_freeze") is not True:
        raise Refusal("freeze manifest is not this tool's manual freeze")
    log.append(f"freeze valid: {freeze['candidate_digest'][:12]} frozen_at {freeze['frozen_at']}")
    quiet = machine_quiet(args.settle, log)
    controller = make_controller()
    if controller.frozen != freeze:
        raise Refusal("controller state frozen != sealed manifest")
    log.append(f"would run Controller.hidden(): run_hidden_suite(lifecycle/freeze_manifest.json, "
               f"lifecycle/hidden-result.json, <credential>, {TREE}, upstream={UPSTREAM}); "
               "lower broker = EvaluatorBrokerLifecycle defaults (deepseek-flash / high)")
    if not args.apply:
        log.append("DRY-RUN: nothing started")
        return 0
    EVIDENCE.mkdir(exist_ok=True)
    started = now()
    result = controller.hidden()
    record = {"schema_version": "agentswe-edit-manual-freeze-hidden/v1", "started_at": started,
              "finished_at": now(), "tool_sha256": sha256_file(TOOL), "machine_before": quiet,
              "hidden_result": str(HIDDEN_RESULT),
              "complete_inventory": result.get("complete_inventory"),
              "formal_result_eligible": result.get("formal_result_eligible"),
              "suite_errors": result.get("suite_errors"),
              "cases": {cid: {k: rec.get(k) for k in ("classification", "candidate_classification",
                                                     "infrastructure_invalid", "error")}
                        for cid, rec in (result.get("cases") or {}).items()}}
    write_json(EVIDENCE / "hidden_stage_record.json", record)
    log.append(json.dumps({k: record[k] for k in ("complete_inventory", "formal_result_eligible",
                                                   "suite_errors", "cases")}, ensure_ascii=False))
    return 0 if result.get("formal_result_eligible") is True else 2


def stage_finalize(args, log: list) -> int:
    common_checks(log)
    for path in (FREEZE_PATH, HIDDEN_RESULT, HIDDEN_ATTEST, GATE_PATH):
        if not path.is_file():
            raise Refusal(f"missing: {path}")
    if AGGREGATION.exists():
        raise Refusal("formal_aggregation.json already exists; use refinalize_run.py / rejudge_case.py")
    hidden = read_json(HIDDEN_RESULT)
    log.append(f"hidden: complete_inventory={hidden.get('complete_inventory')} "
               f"eligible={hidden.get('formal_result_eligible')} errors={hidden.get('suite_errors')}")
    for key, value in UNIT_ENV.items():
        if os.environ.get(key) != value:
            raise Refusal(f"environment differs from the formal unit: {key}")
    log.append("environment matches the formal unit (incl. AGENTSWE_EDIT_EARLY_STOP_RESAMPLE=1)")
    quiet = machine_quiet(args.settle, log)
    judge_dir = EVIDENCE / "result_judge_broker"
    cidfile = judge_dir / "container.cid"
    if cidfile.exists():
        raise Refusal(f"judge broker CID file already exists: {cidfile}")
    finalize_cmd = [sys.executable, str(TREE / "evaluator/formal_finalize.py"),
                    "--run-dir", str(RUN_DIR), "--output", str(AGGREGATION),
                    "--credential-file", str(CREDENTIAL),
                    "--result-judge-broker-endpoint", "<fresh judge broker>"]
    image, model, effort = one_stop_constants()
    log.append(f"judge constants from formal_one_stop.py: image={image} model={model} effort={effort}")
    log.append("would start a fresh evaluator-owned Result judge broker (judge_broker_runtime.JudgeBroker, "
               f"image of the run, cid {cidfile}) and run: " + " ".join(finalize_cmd))
    for path in (RUN_DIR / "summary.json", RUN_DIR / "one_stop_summary.json"):
        backup(path, args.apply, log)
    if not args.apply:
        log.append("DRY-RUN: nothing started")
        return 0

    sys.path.insert(0, str(CONTROL))
    import judge_broker_runtime as jbr
    BUILDER_IMAGE, RESULT_JUDGE_MODEL, RESULT_JUDGE_EFFORT = one_stop_constants()
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    judge_dir.mkdir(parents=True, exist_ok=False)
    broker = jbr.JudgeBroker(
        name="openwiki-result-judge-mf-" + hashlib.sha256(f"{RUN_DIR}:manual-freeze".encode()).hexdigest()[:12],
        credential=CREDENTIAL, image=BUILDER_IMAGE, port=port, cidfile=cidfile, upstream=UPSTREAM)
    record = {"schema_version": "agentswe-edit-manual-freeze-finalize/v1", "started_at": now(),
              "tool_sha256": sha256_file(TOOL), "machine_before": quiet}
    finalizer = None
    try:
        broker.start()
        initial = jbr.stats(broker.endpoint)
        write_json(EVIDENCE / "result_judge_broker_initial.json", initial)
        if not jbr.fresh_judge_stats(initial, broker.instance_id):
            raise Refusal("Result judge broker failed the fresh deepseek-flash/max/64000 zero-call gate")
        record["result_judge_broker"] = {"endpoint": broker.endpoint, "container_id": broker.container_id,
                                         "container_name": broker.name, "instance_id": broker.instance_id,
                                         "stats": str(broker.stats_path), "lifecycle": str(broker.lifecycle_path),
                                         "protocol": initial.get("protocol")}
        finalize_cmd[-1] = broker.endpoint
        finalizer = subprocess.run(finalize_cmd, text=True, capture_output=True, check=False, cwd=str(TREE))
        (EVIDENCE / "finalizer.stdout.log").write_text(finalizer.stdout)
        (EVIDENCE / "finalizer.stderr.log").write_text(finalizer.stderr)
        record["result_judge_broker"]["final_stats"] = jbr.stats(broker.endpoint)
    finally:
        record["result_judge_broker_cleanup"] = broker.close()
    aggregation = read_json(AGGREGATION) if AGGREGATION.is_file() else {}
    att = read_json(RUN_DIR / "builder_session_attestation.json")
    summary = {
        "status": "completed" if finalizer and finalizer.returncode == 0 else "formal_finalization_refused",
        "formal_result_claimed": aggregation.get("formal_result_publishable") is True,
        "code_score_claimed": aggregation.get("code_score_publishable") is True,
        "builder_session_attestation": att,
        "freeze_manifest": str(FREEZE_PATH),
        "hidden": hidden, "formal_aggregation": aggregation,
        "finalizer_exit_code": finalizer.returncode if finalizer else None,
        "finalizer_stderr_tail": (finalizer.stderr[-1500:] if finalizer else ""),
        "result_judge_broker": {"model": RESULT_JUDGE_MODEL, "reasoning_effort": RESULT_JUDGE_EFFORT,
                                "endpoint": broker.endpoint, "stats": str(broker.stats_path),
                                "lifecycle": str(broker.lifecycle_path)},
        "combined_score": None, "max_dev_rounds": MAX_DEV_ROUNDS, "n_concurrent": N_CONCURRENT,
        "manual_freeze": {"reason": FREEZE_REASON, "manual": True,
                          "record": str(EVIDENCE / "manual_freeze_record.json"),
                          "finalize_record": str(EVIDENCE / "finalize_stage_record.json")},
    }
    write_json(RUN_DIR / "summary.json", summary)
    import runpy
    shared = runpy.run_path(str(CONTROL / "one_stop_contract_shared.py"), run_name="manual_freeze_summary")
    shared["write_summary"](RUN_DIR, max_dev_rounds=MAX_DEV_ROUNDS, n_concurrent=N_CONCURRENT, mode="formal")
    one_stop = read_json(RUN_DIR / "one_stop_summary.json")
    one_stop["manual_freeze"] = summary["manual_freeze"]
    write_json(RUN_DIR / "one_stop_summary.json", one_stop)
    record.update(finished_at=now(), finalizer_exit_code=summary["finalizer_exit_code"],
                  formal_result_publishable=aggregation.get("formal_result_publishable"),
                  result_axis=aggregation.get("result_axis"), reasons=aggregation.get("reasons"),
                  machine_after=machine_after())
    write_json(EVIDENCE / "finalize_stage_record.json", record)
    log.append(json.dumps({"finalizer_exit": summary["finalizer_exit_code"],
                           "publishable": aggregation.get("formal_result_publishable"),
                           "result_axis": aggregation.get("result_axis"),
                           "reasons": aggregation.get("reasons")}, ensure_ascii=False)[:3000])
    return 0 if summary["finalizer_exit_code"] == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=("freeze", "hidden", "finalize"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--operator", default="", help="who/why; required with --apply --stage freeze")
    parser.add_argument("--settle", type=int, default=45, help="docker quiet window, seconds")
    args = parser.parse_args()
    if args.apply and args.stage == "freeze" and not args.operator.strip():
        parser.error("--operator is required for --apply --stage freeze")
    if Path.cwd() != TREE:
        os.chdir(TREE)
    lock_path = Path("/tmp/agentswe-manual-freeze-openwiki-0922-tc-v1-001.lock")
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("REFUSED: another manual_freeze invocation holds the lock")
            return 1
        log: list[str] = []
        say(f"== manual freeze tool  stage={args.stage}  mode={'APPLY' if args.apply else 'DRY-RUN'}  {now()}")
        try:
            code = {"freeze": stage_freeze, "hidden": stage_hidden, "finalize": stage_finalize}[args.stage](args, log)
        except Refusal as exc:
            for line in log:
                say("   " + line)
            say(f"REFUSED: {exc}")
            return 1
        for line in log:
            say("   " + line)
        if args.apply:
            EVIDENCE.mkdir(exist_ok=True)
            with (EVIDENCE / "tool_invocations.log").open("a") as handle:
                handle.write(json.dumps({"at": now(), "stage": args.stage, "exit": code,
                                         "operator": args.operator, "tool_sha256": sha256_file(TOOL),
                                         "log": log}, ensure_ascii=False) + "\n")
        say(f"== exit {code}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
