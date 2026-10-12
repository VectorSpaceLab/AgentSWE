#!/usr/bin/env python3
"""Offline black-box evaluator for the Policy Provenance Ledger plugin."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable

CASE_IDS = tuple(f"test_{number:03d}" for number in range(1, 7))
PLUGIN_REL = Path("plugins/policy-provenance-ledger")
HOOK_REL = Path("hooks/policy_hook.py")
INSPECT_REL = Path("bin/policy-ledger-inspect")
HOOK_COMMAND = "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/policy_hook.py"
RECEIPT_FIELDS = {"schema_version", "receipt_id", "session_id", "event_id", "tool_name",
                  "decision", "policy_id", "policy_epoch", "policy_revision",
                  "policy_snapshot_digest", "canonical_action_digest", "key_id",
                  "ledger_sequence", "integrity", "delegation_id",
                  "delegation_parent_id", "delegation_key_id", "delegation_scope_digest",
                  "reservation_id", "reservation_state", "quota_remaining",
                  "approval_ticket_id", "approval_state", "approval_count",
                  "approval_threshold", "approval_round", "approval_required_groups",
                  "approval_satisfied_groups", "owner_instance_id", "owner_generation",
                  "ownership_state", "handoff_id", "state_format_version", "upgrade_id",
                  "upgrade_state", "effect_id", "effect_generation", "effect_state"}
RECEIPT_FIELDS.update({"repair_id", "repair_generation", "integrity_state",
                       "session_authority_id", "session_generation", "continuation_id",
                       "continuation_state"})
LEASE_FIELDS = {"schema_version", "reservation_id", "session_id", "event_id", "receipt_id",
                "policy_id", "policy_epoch", "policy_snapshot_digest", "key_id", "state",
                "created_sequence", "settled_sequence", "quota_remaining",
                "session_authority_id", "session_generation"}
MUTABLE_SETTLEMENT_FIELDS = {"reservation_state", "quota_remaining", "approval_ticket_id",
                             "approval_state", "approval_count", "approval_round",
                             "approval_satisfied_groups", "owner_instance_id",
                             "owner_generation", "ownership_state", "handoff_id", "integrity"}
MUTABLE_SETTLEMENT_FIELDS.update({"effect_generation", "effect_state", "continuation_state"})
APPROVAL_FIELDS = {"schema_version", "ticket_id", "session_id", "event_id", "receipt_id",
                   "tool_name", "policy_id", "policy_epoch", "policy_snapshot_digest",
                   "canonical_action_digest", "key_id", "state", "threshold",
                   "approval_count", "principal_ids", "created_sequence",
                   "consumed_sequence", "expires_at", "approval_round", "required_groups",
                   "satisfied_groups", "predecessor_ticket_id", "superseded_by_ticket_id",
                   "supersession_id", "session_authority_id", "session_generation"}
CHECKPOINT_FIELDS = {"schema_version", "kind", "transfer_id", "origin_instance_id",
                     "export_sequence", "base_checkpoint_digest", "created_at", "policy_id",
                     "policy_epoch", "policy_snapshot_digest", "audit_event_count",
                     "receipt_count", "lease_count", "approval_ticket_count", "handoff_count",
                     "effect_count", "upgrade_count", "state_format_version",
                     "upgrade_lineage_digest", "repair_count", "continuation_count",
                     "repair_generation", "repair_lineage_digest",
                     "owner_instance_id", "owner_generation", "ownership_state", "sealed_state",
                     "checkpoint_digest", "integrity"}
CHECKPOINT_VIEW_FIELDS = {"schema_version", "checkpoint_digest", "origin_instance_id",
                          "origin_export_sequence", "base_checkpoint_digest",
                          "imported_at_sequence", "policy_id", "policy_epoch",
                          "policy_snapshot_digest", "audit_event_count", "receipt_count",
                          "lease_count", "approval_ticket_count", "handoff_count",
                          "effect_count", "upgrade_count", "state_format_version",
                          "upgrade_lineage_digest", "repair_count", "continuation_count",
                          "repair_generation", "repair_lineage_digest",
                          "owner_instance_id", "owner_generation", "ownership_state",
                          "state", "lineage_digest"}
HANDOFF_OFFER_FIELDS = {"schema_version", "kind", "handoff_id", "source_instance_id",
                        "target_instance_id", "source_generation", "target_generation",
                        "created_at", "expires_at", "policy_id", "policy_epoch",
                        "policy_snapshot_digest", "audit_event_count", "receipt_count",
                        "lease_count", "approval_ticket_count", "sealed_state",
                        "effect_count", "upgrade_count", "state_format_version",
                        "upgrade_lineage_digest", "repair_count", "continuation_count",
                        "repair_generation", "repair_lineage_digest",
                        "offer_digest", "integrity"}
HANDOFF_ACK_FIELDS = {"schema_version", "kind", "handoff_id", "source_instance_id",
                      "target_instance_id", "source_generation", "target_generation",
                      "offer_digest", "accepted_at", "ack_digest", "integrity"}
HANDOFF_COMMIT_FIELDS = (HANDOFF_ACK_FIELDS - {"accepted_at"}) | {
    "finalized_at", "commit_digest"}
HANDOFF_VIEW_FIELDS = {"schema_version", "handoff_id", "source_instance_id",
                       "target_instance_id", "source_generation", "target_generation",
                       "state", "offer_digest", "ack_digest", "commit_digest",
                       "created_sequence", "transition_sequence", "policy_id", "policy_epoch",
                       "policy_snapshot_digest", "expires_at"}
HANDOFF_VIEW_FIELDS.update({"effect_count", "upgrade_count", "state_format_version",
                            "upgrade_lineage_digest", "repair_count", "continuation_count",
                            "repair_generation", "repair_lineage_digest"})
EFFECT_FIELDS = {"schema_version", "effect_id", "receipt_id", "session_id", "event_id",
                 "phase", "policy_id", "policy_epoch", "policy_snapshot_digest",
                 "canonical_action_digest", "owner_instance_id", "owner_generation",
                 "effect_generation", "state", "claim_id", "consumer_id", "attempt_count",
                 "created_sequence", "updated_sequence", "delivery_id", "outcome_digest",
                 "session_authority_id", "session_generation"}
EFFECT_CLAIM_FIELDS = {"schema_version", "kind", "consumer_id", "claim_id", "claimed_at",
                       "expires_at", "items", "claim_token", "claim_digest", "integrity"}
UPGRADE_MANIFEST_FIELDS = {"schema_version", "kind", "upgrade_id", "source_format_version",
                           "target_format_version", "base_sequence", "cursor_sequence",
                           "source_state_digest", "expected_upgrade_lineage_digest", "started_at",
                           "upgrade_digest", "integrity", "repair_generation",
                           "repair_lineage_digest", "continuation_count"}
UPGRADE_STATUS_FIELDS = {"schema_version", "kind", "upgrade_id", "source_format_version",
                         "target_format_version", "base_sequence", "cursor_sequence",
                         "catchup_sequence", "remaining_items", "ready_to_commit", "state",
                         "upgrade_digest", "integrity", "repair_generation",
                         "repair_lineage_digest", "continuation_count"}
UPGRADE_COMMIT_FIELDS = {"schema_version", "kind", "upgrade_id", "source_format_version",
                         "target_format_version", "source_state_digest",
                         "previous_upgrade_lineage_digest", "committed_at", "commit_sequence",
                         "upgrade_digest", "upgrade_lineage_digest", "integrity",
                         "repair_generation", "repair_lineage_digest", "continuation_count"}
UPGRADE_VIEW_FIELDS = {"schema_version", "upgrade_id", "source_format_version",
                       "target_format_version", "state", "base_sequence", "cursor_sequence",
                       "catchup_sequence", "source_state_digest", "previous_upgrade_lineage_digest",
                       "upgrade_lineage_digest", "created_sequence", "transition_sequence"}
UPGRADE_VIEW_FIELDS.update({"repair_generation", "repair_lineage_digest", "continuation_count"})
REPAIR_MANIFEST_FIELDS = {"schema_version", "kind", "repair_id", "repair_generation",
    "source_repair_generation", "source_state_digest", "damage_digest", "damaged_high_sequence",
    "verified_prefix_sequence", "verified_prefix_digest", "candidate_cursor", "candidate_item_count",
    "started_at", "repair_digest", "integrity"}
REPAIR_STATUS_FIELDS = {"schema_version", "kind", "repair_id", "repair_generation",
    "candidate_cursor", "candidate_item_count", "remaining_items", "candidate_state_digest",
    "discarded_event_count", "invalidated_authority_count", "attested_verifiers",
    "ready_for_verification", "state", "repair_digest", "integrity"}
REPAIR_ATTESTATION_FIELDS = {"schema_version", "kind", "repair_id", "repair_generation",
    "verifier_id", "candidate_state_digest", "damage_digest", "verified_prefix_sequence",
    "attested_at", "attestation_digest", "integrity"}
REPAIR_COMMIT_FIELDS = {"schema_version", "kind", "repair_id", "repair_generation",
    "source_state_digest", "candidate_state_digest", "damage_digest", "verified_prefix_sequence",
    "discarded_event_count", "invalidated_authority_count", "verifier_ids", "committed_at",
    "commit_sequence", "previous_repair_lineage_digest", "repair_lineage_digest",
    "repair_digest", "integrity"}
REPAIR_VIEW_FIELDS = {"schema_version", "repair_id", "repair_generation",
    "source_repair_generation", "state", "source_state_digest", "damage_digest",
    "damaged_high_sequence", "verified_prefix_sequence", "candidate_cursor",
    "candidate_item_count", "candidate_state_digest", "discarded_event_count",
    "invalidated_authority_count", "verifier_ids", "created_sequence", "transition_sequence",
    "previous_repair_lineage_digest", "repair_lineage_digest"}
CONTINUATION_TOKEN_FIELDS = {"schema_version", "kind", "continuation_id",
    "session_authority_id", "source_session_id", "target_session_id", "source_generation",
    "target_generation", "issued_at", "expires_at", "policy_id", "policy_epoch",
    "policy_snapshot_digest", "owner_instance_id", "owner_generation", "repair_generation",
    "repair_lineage_digest", "state_format_version", "upgrade_lineage_digest",
    "last_receipt_id", "last_sequence", "predecessor_continuation_id", "token_digest",
    "token", "integrity"}
CONTINUATION_VIEW_FIELDS = {"schema_version", "continuation_id", "session_authority_id",
    "source_session_id", "target_session_id", "source_generation", "target_generation", "state",
    "policy_id", "policy_epoch", "policy_snapshot_digest", "owner_instance_id", "owner_generation",
    "repair_generation", "repair_lineage_digest", "state_format_version", "upgrade_lineage_digest",
    "last_receipt_id", "last_sequence", "predecessor_continuation_id", "token_digest",
    "issued_sequence", "transition_sequence", "expires_at"}
INTEGRITY_VIEW_FIELDS = {"schema_version", "state", "generation", "sealed_file_count",
                         "protected_path_count", "manifest_digest", "authority_state",
                         "created_sequence", "transition_sequence"}
SECRET_LITERALS = ("EVAL_SHELL_SECRET_62ac91", "EVAL_MCP_NESTED_7f91d20a",
                   "EVAL_POST_SECRET_8d31", "EVAL_URL_SECRET_41aa",
                   "EVAL_PRIVATE_KEY_4c19", "EVAL_COMPAT_POST_90c2",
                   "TAMPER_OLD_GENERATION_MARKER",
                   "TAMPER_NEW_GENERATION_MARKER", "EVAL_APPROVER_SECRET_88f1",
                   "EVAL_CHECKPOINT_SECRET_47c2", "EVAL_HANDOFF_SECRET_9c61",
                   "EVAL_UPGRADE_SECRET_7a11", "EVAL_CLAIM_SECRET_6b22",
                   "EVAL_CONTINUATION_SECRET_8d41", "EVAL_REPAIR_SECRET_5a92",
                   "EVAL_VERIFIER_SECRET_2f18", "EVAL_DAMAGE_MARKER_8c33")
MAX_SECONDS = 600
MAX_TREE_PSS_BYTES = 4 * 1024 * 1024 * 1024
ASSERTION_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "assertion_contract.json"
_ASSERTION_CONTRACT: dict[str, Any] | None = None


class HarnessError(RuntimeError):
    pass


class ProcessTimeout(RuntimeError):
    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class ResourceEnforcementError(HarnessError):
    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class CaseExecutionError(RuntimeError):
    def __init__(self, kind: str, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.kind = kind
        self.record = record


def validate_assertion_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "cases"} or value.get("schema_version") != 1:
        raise HarnessError("assertion semantic contract has an invalid top-level schema")
    cases = value.get("cases")
    if not isinstance(cases, dict) or tuple(cases) != CASE_IDS:
        raise HarnessError("assertion semantic contract has an invalid case inventory")
    for case_id, case in cases.items():
        if not isinstance(case, dict) or set(case) != {"assertions", "caps"}:
            raise HarnessError(f"assertion semantic contract is malformed for {case_id}")
        assertions = case["assertions"]; caps = case["caps"]
        if not isinstance(assertions, list) or not assertions or not isinstance(caps, list):
            raise HarnessError(f"assertion semantic contract lists are malformed for {case_id}")
        for item in [*assertions, *caps]:
            expected = {"id", "points", "semantic"} if item in assertions else {"id", "maximum", "semantic"}
            if (not isinstance(item, dict) or set(item) != expected
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(item.get("semantic"), str) or not item["semantic"]):
                raise HarnessError(f"assertion semantic contract item is malformed for {case_id}")
        identifiers = [item["id"] for item in assertions]
        cap_ids = [item["id"] for item in caps]
        if (len(identifiers) != len(set(identifiers)) or len(cap_ids) != len(set(cap_ids))
                or any(not isinstance(item["points"], int) or isinstance(item["points"], bool) or item["points"] <= 0 for item in assertions)
                or sum(item["points"] for item in assertions) != 100
                or any(not isinstance(item["maximum"], int) or isinstance(item["maximum"], bool) or not 0 <= item["maximum"] <= 100 for item in caps)):
            raise HarnessError(f"assertion semantic contract accounting is invalid for {case_id}")
    return value


def assertion_contract() -> dict[str, Any]:
    global _ASSERTION_CONTRACT
    if _ASSERTION_CONTRACT is None:
        _ASSERTION_CONTRACT = validate_assertion_contract(read_json(ASSERTION_CONTRACT_PATH))
    return _ASSERTION_CONTRACT


def redact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True)
    for secret in SECRET_LITERALS:
        text = text.replace(secret, "[REDACTED-EVALUATOR-SECRET]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[REDACTED]", text)
    text = re.sub(r"ghp_[A-Za-z0-9_]{8,}", "ghp_[REDACTED]", text)
    return text[:1200]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid JSON object {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"{path.name} must contain one JSON object")
    return value


def patch_paths(text: str) -> list[str]:
    paths: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("diff --git "):
            continue
        try: parts = shlex.split(line)
        except ValueError as exc: raise HarnessError("patch has an invalid diff header") from exc
        if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
            raise HarnessError("patch uses a non-Git diff header")
        paths.update((parts[2][2:], parts[3][2:]))
    return sorted(paths)


def validate_patch_paths(paths: list[str]) -> None:
    prefix = PLUGIN_REL.as_posix() + "/"
    if not paths:
        raise HarnessError("solution.patch is empty or has no destination paths")
    for name in paths:
        pure = PurePosixPath(name)
        if (pure.is_absolute() or ".." in pure.parts or pure.as_posix() != name
                or not name.startswith(prefix)):
            raise HarnessError(f"forbidden patch path: {name}")


def validate_reports(submission: Path, paths: list[str]) -> dict[str, Any]:
    edit = read_json(submission / "edit_report.json")
    run = read_json(submission / "run_report.json")
    required_edit = {"schema_version", "feature_summary", "changed_paths", "commands_run",
                     "compatibility_notes", "limitations"}
    if set(edit) != required_edit or edit.get("schema_version") != "1.0":
        raise HarnessError("edit_report.json has an invalid top-level schema")
    if not isinstance(edit["feature_summary"], str) or any(not isinstance(edit[key], list) for key in required_edit - {"schema_version", "feature_summary"}):
        raise HarnessError("edit_report.json field types are invalid")
    if any(not isinstance(item, str) for key in ("changed_paths", "commands_run", "compatibility_notes", "limitations") for item in edit[key]):
        raise HarnessError("edit_report arrays must contain strings")
    if sorted(edit["changed_paths"]) != paths:
        raise HarnessError("edit_report changed_paths does not match solution.patch")
    required_run = {"status", "artifact_paths", "errors", "runtime_seconds", "peak_memory_mb", "providers"}
    # The readiness profile requires four further fields in run_report.json, and
    # this check compared key sets for exact equality -- so a delivery carrying
    # them would have been refused here while finalize refused the delivery
    # without them. The six above stay required and anything outside these ten
    # is still refused; only the four the profile mandates are tolerated.
    readiness_run = {"builder_session_id", "submission_number",
                     "revision_of_candidate_digest", "feedback_digest"}
    unexpected = set(run) - required_run - readiness_run
    if not required_run <= set(run) or unexpected or run.get("status") not in {"success", "failure"}:
        raise HarnessError(
            "run_report.json has an invalid top-level schema; required "
            f"{sorted(required_run)}, optional {sorted(readiness_run)}, found {sorted(run)}")
    if not isinstance(run["artifact_paths"], list) or not isinstance(run["errors"], list):
        raise HarnessError("run_report array fields are invalid")
    if not all(isinstance(item, str) for item in run["artifact_paths"] + run["errors"]):
        raise HarnessError("run_report arrays must contain strings")
    if not all(isinstance(run[key], (int, float)) and not isinstance(run[key], bool) and run[key] >= 0 for key in ("runtime_seconds", "peak_memory_mb")):
        raise HarnessError("run_report resource values are invalid")
    providers = run.get("providers")
    provider_keys = {"deepseek", "gateway", "gateway_image", "serper", "web_retrieval"}
    if not isinstance(providers, dict) or set(providers) != provider_keys or not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in providers.values()):
        raise HarnessError("run_report providers schema is invalid")
    required_artifacts = {str(PLUGIN_REL / rel) for rel in (Path(".claude-plugin/plugin.json"), Path("hooks/hooks.json"), HOOK_REL, INSPECT_REL, Path("README.md"))}
    if not required_artifacts.issubset(set(run["artifact_paths"])):
        raise HarnessError("run_report omits a required plugin artifact")
    artifact_paths = run["artifact_paths"]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise HarnessError("run_report artifact_paths contains duplicates")
    prefix = PLUGIN_REL.as_posix() + "/"
    for name in artifact_paths:
        pure = PurePosixPath(name)
        if (pure.is_absolute() or ".." in pure.parts or pure.as_posix() != name
                or not name.startswith(prefix) or name not in paths):
            raise HarnessError(f"run_report names an invalid artifact path: {name}")
    test_paths = {
        name for name in paths
        if any(part in {"test", "tests"} for part in PurePosixPath(name).parts)
        or PurePosixPath(name).name.startswith("test_")
        or PurePosixPath(name).stem.endswith("_test")
    }
    if not test_paths.issubset(set(artifact_paths)):
        raise HarnessError("run_report omits a source-adjacent test artifact")
    return {"edit_report": "valid", "run_report": "valid"}


def tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    plugins = root / "plugins"
    if not plugins.is_dir():
        return result
    for path in plugins.rglob("*"):
        if path.is_file() and PLUGIN_REL not in (path.relative_to(root), *path.relative_to(root).parents):
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def process_tree_pids(root_pid: int) -> set[int]:
    pending = [root_pid]
    found: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii").split()
        except OSError:
            children = []
        pending.extend(int(value) for value in children if value.isdigit())
    return found


def process_pss_bytes(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="ascii").splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        resident_pages = int(Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def run_command(command: list[str], *, cwd: Path, timeout: int = 30, env: dict[str, str] | None = None,
                input_text: str | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run with a measured actual process-tree PSS ceiling, never RLIMIT_AS."""
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    monitor_state: dict[str, Any] = {"peak": 0, "exceeded": False, "stop": False}

    def monitor() -> None:
        while not monitor_state["stop"]:
            total = sum(process_pss_bytes(pid) for pid in process_tree_pids(process.pid))
            monitor_state["peak"] = max(int(monitor_state["peak"]), total)
            if total > MAX_TREE_PSS_BYTES:
                monitor_state["exceeded"] = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return
            if process.poll() is not None:
                return
            time.sleep(0.01)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    timed_out = False
    stdout = ""; stderr = ""
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    finally:
        monitor_state["stop"] = True
        watcher.join(timeout=1)
    if monitor_state["exceeded"]:
        stderr += "\nactual process-tree PSS ceiling exceeded"
    done = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    record = {"command": [Path(command[0]).name, *command[1:]], "exit_code": done.returncode,
              "duration_seconds": round(time.monotonic() - started, 4),
              "peak_tree_pss_mb": round(int(monitor_state["peak"]) / (1024 * 1024), 3),
              "memory_ceiling_mb": MAX_TREE_PSS_BYTES // (1024 * 1024),
              "memory_exceeded": bool(monitor_state["exceeded"]),
              "timed_out": timed_out, "stderr": redact(done.stderr[-600:])}
    if monitor_state["exceeded"]:
        raise ResourceEnforcementError("actual process-tree PSS ceiling exceeded", record)
    if timed_out:
        raise ProcessTimeout(f"process timed out after {timeout} seconds", record)
    return done, record


def validate_hook_config(plugin: Path) -> dict[str, Any]:
    if not plugin.is_dir() or plugin.is_symlink():
        raise HarnessError("fixed plugin root is missing or a symlink")
    for path in plugin.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise HarnessError(f"plugin contains an unsafe filesystem entry: {path.relative_to(plugin)}")
    manifest = read_json(plugin / ".claude-plugin" / "plugin.json")
    if manifest.get("name") != "policy-provenance-ledger":
        raise HarnessError("manifest name is not policy-provenance-ledger")
    hooks = read_json(plugin / "hooks" / "hooks.json").get("hooks")
    if not isinstance(hooks, dict):
        raise HarnessError("hooks.json lacks the plugin hooks wrapper")
    for event in ("PreToolUse", "PostToolUse"):
        registrations = hooks.get(event)
        if not isinstance(registrations, list):
            raise HarnessError(f"hooks.json lacks {event}")
        found = False
        for registration in registrations:
            if not isinstance(registration, dict) or registration.get("matcher") != "*":
                continue
            commands = registration.get("hooks")
            if isinstance(commands, list) and any(isinstance(item, dict) and item.get("type") == "command" and item.get("command") == HOOK_COMMAND for item in commands):
                found = True
        if not found:
            raise HarnessError(f"{event} does not register the fixed command")
    hook = plugin / HOOK_REL
    inspector = plugin / INSPECT_REL
    if not hook.is_file() or hook.is_symlink() or not inspector.is_file() or inspector.is_symlink():
        raise HarnessError("fixed hook or inspector entry is missing or a symlink")
    if not (inspector.stat().st_mode & stat.S_IXUSR):
        raise HarnessError("policy-ledger-inspect is not executable")
    compiled = subprocess.run([sys.executable, "-m", "py_compile", str(hook)], cwd=plugin,
                              env={**os.environ, "PYTHONPYCACHEPREFIX": str(plugin.parent / ".evaluator-pyc")},
                              text=True, capture_output=True, timeout=20, check=False)
    shutil.rmtree(plugin.parent / ".evaluator-pyc", ignore_errors=True)
    if compiled.returncode:
        raise HarnessError("hook Python syntax check failed")
    return {"manifest_name": manifest["name"], "hook_command": HOOK_COMMAND, "inspector_executable": True}


def probe_entries(plugin: Path, root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    workspace = root / "entry-workspace"; state = root / "entry-state"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    event = {"session_id": "entry-probe", "hook_event_name": "PreToolUse", "cwd": str(workspace),
             "tool_name": "Read", "tool_use_id": "entry-probe-read", "tool_input": {"file_path": "src/file.py"}}
    env = dict(os.environ); env.update({"CLAUDE_PROJECT_DIR": str(workspace), "CLAUDE_PLUGIN_ROOT": str(plugin),
        "POLICY_PROVENANCE_STATE_DIR": str(state), "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*", "HTTP_PROXY": "", "HTTPS_PROXY": ""})
    hook_done, hook_record = run_command([sys.executable, str(plugin / HOOK_REL)], cwd=workspace, timeout=10, env=env, input_text=json.dumps(event))
    try: response = json.loads(hook_done.stdout)
    except json.JSONDecodeError as exc: raise HarnessError("installed hook entry did not return one JSON object") from exc
    if hook_done.returncode or not isinstance(response, dict) or not valid_receipt(response, event):
        raise HarnessError("installed hook entry failed its protocol probe")
    inspect_done, inspect_record = run_command([str(plugin / INSPECT_REL), "--state-dir", str(state)], cwd=workspace, timeout=10, env=env)
    if inspect_done.returncode:
        raise HarnessError("installed inspector entry failed its launch probe")
    try:
        for line in inspect_done.stdout.splitlines():
            if line.strip() and not isinstance(json.loads(line), dict): raise HarnessError("inspector probe emitted non-object JSONL")
    except json.JSONDecodeError as exc: raise HarnessError("inspector probe emitted invalid JSONL") from exc
    return {"hook_exit": hook_done.returncode, "inspector_exit": inspect_done.returncode}, [hook_record, inspect_record]


def prepare(args: argparse.Namespace, output: Path) -> tuple[Path, Path, dict[str, Any]]:
    if args.patch.name != "solution.patch" or args.patch.parent != args.submission:
        raise HarnessError("--patch must be <submission>/solution.patch")
    patch = args.patch.read_text(encoding="utf-8")
    paths = patch_paths(patch); validate_patch_paths(paths)
    reports = validate_reports(args.submission, paths)
    pristine_hashes = tree_hashes(args.repository)
    work = output / "worktree"; installed = output / "installed-plugin"
    shutil.rmtree(work, ignore_errors=True); shutil.rmtree(installed, ignore_errors=True)
    shutil.copytree(args.repository, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    commands: list[dict[str, Any]] = []
    for command in (["git", "init", "-q"], ["git", "apply", "--check", str(args.patch)], ["git", "apply", str(args.patch)]):
        done, record = run_command(command, cwd=work); commands.append(record)
        if done.returncode:
            raise HarnessError(f"patch preparation failed at {record['command'][-2] if len(record['command']) > 2 else record['command'][-1]}")
    second, second_record = run_command(["git", "apply", "--check", str(args.patch)], cwd=work)
    second_record["expected"] = "nonzero: patch must not apply twice"
    commands.append(second_record)
    if second.returncode == 0:
        raise HarnessError("solution.patch remains applicable after one application")
    if tree_hashes(work) != pristine_hashes:
        raise HarnessError("an existing plugin changed after patch application")
    plugin = work / PLUGIN_REL
    contract = validate_hook_config(plugin)
    shutil.copytree(plugin, installed, symlinks=False)
    entry, entry_commands = probe_entries(installed, output / "entry-probe")
    return work, installed, {"changed_paths": paths, "reports": reports, "commands": commands,
                             "contract": contract, "patch_apply_count": 1,
                             "second_apply_rejected": True,
                             "existing_plugin_hash_count": len(pristine_hashes),
                             "entry_probe": entry, "entry_commands": entry_commands}


class Runtime:
    def __init__(self, case_id: str, case_dir: Path, plugin: Path, root: Path) -> None:
        self.case_id = case_id; self.case_dir = case_dir; self.plugin = plugin; self.root = root
        self.workspace = root / "workspace"; self.state = root / "state"; self.outside = root / "outside"
        for directory in (self.workspace / ".claude", self.workspace / "src", self.workspace / "tmp", self.outside):
            directory.mkdir(parents=True, exist_ok=True)
        (self.workspace / "src" / "app.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.workspace / ".env").write_text("TOKEN=fixture-value\n", encoding="utf-8")
        (self.outside / "outside.txt").write_text("outside\n", encoding="utf-8")
        self.commands: list[dict[str, Any]] = []
        self.disclosed_literals: set[str] = set()
        self.compact_after = "16"

    def assets_json(self, name: str) -> Any:
        return json.loads((self.case_dir / "assets" / name).read_text(encoding="utf-8"))

    def policy(self, name: str = "policy.json") -> None:
        shutil.copyfile(self.case_dir / "assets" / name, self.workspace / ".claude" / "policy-provenance.json")

    def invoke(self, event: dict[str, Any], *, state_dir: Path | None = None) -> dict[str, Any] | None:
        payload = dict(event); payload["cwd"] = str(self.workspace)
        selected_state = state_dir or self.state
        env = dict(os.environ); env.update({"CLAUDE_PROJECT_DIR": str(self.workspace),
            "CLAUDE_PLUGIN_ROOT": str(self.plugin), "POLICY_PROVENANCE_STATE_DIR": str(selected_state),
            "POLICY_PROVENANCE_COMPACT_AFTER": self.compact_after,
            "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*", "HTTP_PROXY": "", "HTTPS_PROXY": ""})
        try:
            done, record = run_command([sys.executable, str(self.plugin / HOOK_REL)], cwd=self.workspace,
                                       timeout=10, env=env, input_text=json.dumps(payload))
        except ProcessTimeout as exc:
            exc.record["operation"] = "hook:" + str(event.get("tool_use_id", "unknown"))
            self.commands.append(exc.record)
            raise CaseExecutionError("hook_timeout", "hook process exceeded 10 seconds", exc.record) from exc
        for secret in SECRET_LITERALS:
            if secret in done.stdout or secret in done.stderr: self.disclosed_literals.add(secret)
        record["operation"] = "hook:" + str(event.get("tool_use_id", "unknown")); self.commands.append(record)
        if done.returncode:
            raise CaseExecutionError("hook_crash", f"hook process exited {done.returncode}", record)
        if len(done.stdout.encode()) > 65536:
            raise CaseExecutionError("hook_protocol", "hook response exceeded 64 KiB", record)
        try:
            value = json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise CaseExecutionError("hook_protocol", "hook did not emit one JSON object", record) from exc
        if not isinstance(value, dict):
            raise CaseExecutionError("hook_protocol", "hook did not emit one JSON object", record)
        return value

    def inspect(self, *filters: str, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], str, int]:
        selected_state = state_dir or self.state
        env = dict(os.environ); env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*", "HTTP_PROXY": "", "HTTPS_PROXY": ""})
        try:
            done, record = run_command([str(self.plugin / INSPECT_REL), "--state-dir", str(selected_state), *filters],
                                       cwd=self.workspace, timeout=10, env=env)
        except ProcessTimeout as exc:
            exc.record["operation"] = "inspector"
            self.commands.append(exc.record)
            raise CaseExecutionError("inspector_timeout", "inspector process exceeded 10 seconds", exc.record) from exc
        for secret in SECRET_LITERALS:
            if secret in done.stdout or secret in done.stderr: self.disclosed_literals.add(secret)
        record["operation"] = "inspector"; self.commands.append(record)
        if done.returncode < 0:
            raise CaseExecutionError("inspector_crash", "inspector process terminated by signal", record)
        values: list[dict[str, Any]] = []
        try:
            for line in done.stdout.splitlines():
                if line.strip():
                    if len(line.encode("utf-8")) > 65536:
                        raise ValueError("inspector JSONL line exceeded 64 KiB")
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("inspector emitted a non-object JSONL value")
                    values.append(value)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            if done.returncode == 0:
                raise CaseExecutionError("inspector_protocol", "inspector emitted malformed JSONL", record) from exc
            values = []
        return values, done.stdout + done.stderr, done.returncode

    def maintain(self, option: str, input_value: dict[str, Any] | None = None,
                 argument: str | None = None, *, state_dir: Path | None = None,
                 jsonl: bool = False, extra: tuple[str, ...] = ()) -> tuple[Any, str, int]:
        selected_state = state_dir or self.state
        env = dict(os.environ); env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*", "HTTP_PROXY": "", "HTTPS_PROXY": ""})
        command = [str(self.plugin / INSPECT_REL), "--state-dir", str(selected_state), option]
        if argument is not None:
            command.append(argument)
        command.extend(extra)
        try:
            done, record = run_command(command, cwd=self.workspace, timeout=10, env=env,
                                       input_text=json.dumps(input_value) if input_value is not None else None)
        except ProcessTimeout as exc:
            exc.record["operation"] = "inspector:" + option.lstrip("-")
            self.commands.append(exc.record)
            raise CaseExecutionError("inspector_timeout", "inspector maintenance exceeded 10 seconds", exc.record) from exc
        record["operation"] = "inspector:" + option.lstrip("-"); self.commands.append(record)
        for secret in SECRET_LITERALS:
            if secret in done.stdout or secret in done.stderr: self.disclosed_literals.add(secret)
        try:
            if jsonl:
                value: Any = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
                if not all(isinstance(item, dict) for item in value):
                    value = []
            else:
                value = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else {}
                if not isinstance(value, dict):
                    value = {}
        except json.JSONDecodeError:
            value = [] if jsonl else {}
        return value, done.stdout + done.stderr, done.returncode

    def mint(self, claims: dict[str, Any], *, state_dir: Path | None = None) -> tuple[dict[str, Any], str, int]:
        value, text, code = self.maintain("--mint-delegation", claims, state_dir=state_dir)
        return value if isinstance(value, dict) else {}, text, code

    def invoke_many(self, events: list[dict[str, Any]], workers: int = 4) -> list[dict[str, Any] | None]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self.invoke, event) for event in events]
            return [future.result(timeout=15) for future in futures]

    def raw_state(self, *, state_dir: Path | None = None) -> str:
        parts: list[str] = []
        selected_state = state_dir or self.state
        if selected_state.exists():
            for path in selected_state.rglob("*"):
                if path.is_file(): parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    def crash_partial_import(self, partial_request: str, *, state_dir: Path) -> dict[str, Any]:
        env = dict(os.environ); env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*",
                                           "HTTP_PROXY": "", "HTTPS_PROXY": ""})
        started = time.monotonic()
        process = subprocess.Popen(
            [str(self.plugin / INSPECT_REL), "--state-dir", str(state_dir), "--import-checkpoint"],
            cwd=self.workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        assert process.stdin is not None
        process.stdin.write(partial_request); process.stdin.flush()
        peak = sum(process_pss_bytes(pid) for pid in process_tree_pids(process.pid))
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        stdout, stderr = process.communicate()
        for secret in SECRET_LITERALS:
            if secret in stdout or secret in stderr: self.disclosed_literals.add(secret)
        record = {"command": ["policy-ledger-inspect", "--state-dir", "<evaluator-state>", "--import-checkpoint"],
                  "exit_code": process.returncode, "duration_seconds": round(time.monotonic() - started, 4),
                  "peak_tree_pss_mb": round(peak / (1024 * 1024), 3),
                  "memory_ceiling_mb": MAX_TREE_PSS_BYTES // (1024 * 1024),
                  "memory_exceeded": peak > MAX_TREE_PSS_BYTES, "timed_out": False,
                  "stderr": redact(stderr[-600:]), "operation": "inspector:crash-partial-import"}
        self.commands.append(record)
        if peak > MAX_TREE_PSS_BYTES:
            raise ResourceEnforcementError("actual process-tree PSS ceiling exceeded", record)
        return record

    def crash_partial_maintenance(self, option: str, partial_request: str,
                                  *, state_dir: Path, argument: str | None = None) -> dict[str, Any]:
        env = dict(os.environ); env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*",
                                           "HTTP_PROXY": "", "HTTPS_PROXY": ""})
        started = time.monotonic()
        command = [str(self.plugin / INSPECT_REL), "--state-dir", str(state_dir), option]
        if argument is not None:
            command.append(argument)
        process = subprocess.Popen(
            command,
            cwd=self.workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        assert process.stdin is not None
        process.stdin.write(partial_request); process.stdin.flush()
        peak = sum(process_pss_bytes(pid) for pid in process_tree_pids(process.pid))
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        stdout, stderr = process.communicate()
        for secret in SECRET_LITERALS:
            if secret in stdout or secret in stderr: self.disclosed_literals.add(secret)
        record = {"command": ["policy-ledger-inspect", "--state-dir", "<evaluator-state>", option],
                  "exit_code": process.returncode,
                  "duration_seconds": round(time.monotonic() - started, 4),
                  "peak_tree_pss_mb": round(peak / (1024 * 1024), 3),
                  "memory_ceiling_mb": MAX_TREE_PSS_BYTES // (1024 * 1024),
                  "memory_exceeded": peak > MAX_TREE_PSS_BYTES, "timed_out": False,
                  "stderr": redact(stderr[-600:]), "operation": "inspector:crash-partial-" + option.lstrip("-")}
        self.commands.append(record)
        if peak > MAX_TREE_PSS_BYTES:
            raise ResourceEnforcementError("actual process-tree PSS ceiling exceeded", record)
        return record


def receipt(response: dict[str, Any] | None) -> dict[str, Any]:
    value = (response or {}).get("policyReceipt")
    return value if isinstance(value, dict) else {}


def permission(response: dict[str, Any] | None) -> str:
    value = (response or {}).get("hookSpecificOutput")
    return str(value.get("permissionDecision", "")) if isinstance(value, dict) else ""


def valid_receipt(response: dict[str, Any] | None, event: dict[str, Any]) -> bool:
    value = receipt(response)
    digest = value.get("canonical_action_digest", ""); integrity = value.get("integrity", "")
    snapshot = value.get("policy_snapshot_digest", ""); key_id = value.get("key_id", "")
    delegation = tuple(value.get(name) for name in ("delegation_id", "delegation_parent_id",
                                                     "delegation_key_id", "delegation_scope_digest"))
    delegation_ok = (all(item is None for item in delegation)
                     or (isinstance(delegation[0], str) and bool(delegation[0])
                         and (delegation[1] is None or isinstance(delegation[1], str))
                         and isinstance(delegation[2], str) and re.fullmatch(r"key_[0-9a-f]{16,64}", delegation[2]) is not None
                         and isinstance(delegation[3], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", delegation[3]) is not None))
    reservation_id = value.get("reservation_id")
    reservation_state = value.get("reservation_state")
    quota = value.get("quota_remaining")
    reservation_ok = (
        (reservation_state == "unlimited" and reservation_id is None and quota is None)
        or (reservation_state == "rejected" and reservation_id is None
            and (quota is None or isinstance(quota, int) and not isinstance(quota, bool) and quota >= 0))
        or (reservation_state in {"reserved", "settled", "expired"}
            and isinstance(reservation_id, str)
            and re.fullmatch(r"res_[A-Za-z0-9_-]{8,128}", reservation_id) is not None
            and isinstance(quota, int) and not isinstance(quota, bool) and quota >= 0)
    )
    ticket_id = value.get("approval_ticket_id")
    approval_state = value.get("approval_state")
    approval_count = value.get("approval_count")
    approval_threshold = value.get("approval_threshold")
    approval_ok = (
        (approval_state == "not_required" and ticket_id is None
         and approval_count is None and approval_threshold is None)
        or (approval_state in {"pending", "ready", "consumed", "expired", "revoked", "superseded"}
            and isinstance(ticket_id, str)
            and re.fullmatch(r"apt_[A-Za-z0-9_-]{8,128}", ticket_id) is not None
            and isinstance(approval_count, int) and not isinstance(approval_count, bool)
            and approval_count >= 0
            and isinstance(approval_threshold, int) and not isinstance(approval_threshold, bool)
            and approval_threshold > 0
            and isinstance(value.get("approval_round"), int) and value["approval_round"] > 0
            and isinstance(value.get("approval_required_groups"), list)
            and all(isinstance(item, str) for item in value["approval_required_groups"])
            and value["approval_required_groups"] == sorted(set(value["approval_required_groups"]))
            and isinstance(value.get("approval_satisfied_groups"), list)
            and all(isinstance(item, str) for item in value["approval_satisfied_groups"])
            and value["approval_satisfied_groups"] == sorted(set(value["approval_satisfied_groups"])))
    )
    if approval_state == "not_required":
        approval_ok = (approval_ok and value.get("approval_round") is None
                       and value.get("approval_required_groups") == []
                       and value.get("approval_satisfied_groups") == [])
    ownership = value.get("ownership_state")
    ownership_ok = ((ownership == "unmanaged" and value.get("owner_instance_id") is None
                     and value.get("owner_generation") is None and value.get("handoff_id") is None)
                    or (ownership in {"active", "prepared", "accepted", "finalized", "checkpoint_only"}
                        and re.fullmatch(r"inst_[A-Za-z0-9_-]{8,128}", str(value.get("owner_instance_id") or "")) is not None
                        and isinstance(value.get("owner_generation"), int)
                        and not isinstance(value.get("owner_generation"), bool)
                        and value["owner_generation"] > 0
                        and (value.get("handoff_id") is None
                             or re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(value.get("handoff_id"))) is not None)))
    format_ok = (isinstance(value.get("state_format_version"), int)
                 and not isinstance(value.get("state_format_version"), bool)
                 and value["state_format_version"] > 0
                 and value.get("upgrade_state") in {"stable", "building"}
                 and (value.get("upgrade_id") is None
                      or re.fullmatch(r"upg_[A-Za-z0-9_-]{8,128}", str(value.get("upgrade_id"))) is not None))
    effect_state = value.get("effect_state")
    no_effect = (effect_state in {"not_configured", "not_created"}
                 and value.get("effect_id") is None and value.get("effect_generation") is None)
    live_effect = (effect_state in {"pending", "claimed", "delivered"}
                   and re.fullmatch(r"eff_[A-Za-z0-9_-]{8,128}", str(value.get("effect_id") or "")) is not None
                   and isinstance(value.get("effect_generation"), int)
                   and not isinstance(value.get("effect_generation"), bool)
                   and value["effect_generation"] > 0)
    integrity_state = value.get("integrity_state")
    repair_ok = (isinstance(value.get("repair_generation"), int)
                 and not isinstance(value.get("repair_generation"), bool)
                 and value["repair_generation"] >= 0
                 and integrity_state in {"healthy", "quarantined", "rebuilding", "verifying"}
                 and ((integrity_state in {"healthy", "quarantined"}
                       and value.get("repair_id") is None)
                      or (integrity_state in {"rebuilding", "verifying"}
                          and re.fullmatch(r"rpr_[A-Za-z0-9_-]{8,128}",
                                           str(value.get("repair_id") or "")) is not None)))
    continuation_state = value.get("continuation_state")
    continuation_unset = (continuation_state in {"not_configured", "not_created"}
                          and value.get("session_authority_id") is None
                          and value.get("session_generation") is None
                          and value.get("continuation_id") is None)
    continuation_live = (continuation_state in {"active", "pending", "continued",
                                                 "cancelled", "expired", "invalidated"}
                         and re.fullmatch(r"sca_[A-Za-z0-9_-]{8,128}",
                                          str(value.get("session_authority_id") or "")) is not None
                         and isinstance(value.get("session_generation"), int)
                         and not isinstance(value.get("session_generation"), bool)
                         and value["session_generation"] > 0
                         and (value.get("continuation_id") is None
                              or re.fullmatch(r"ctn_[A-Za-z0-9_-]{8,128}",
                                              str(value.get("continuation_id"))) is not None))
    specific = (response or {}).get("hookSpecificOutput")
    return (RECEIPT_FIELDS.issubset(value) and value.get("schema_version") == 1
            and value.get("session_id") == event.get("session_id") and value.get("event_id") == event.get("tool_use_id")
            and value.get("tool_name") == event.get("tool_name") and value.get("decision") in {"allow", "deny", "rewrite", "audit_only", "approval_required"}
            and isinstance(value.get("policy_id"), str) and bool(value.get("policy_id"))
            and isinstance(value.get("policy_epoch"), int) and not isinstance(value.get("policy_epoch"), bool) and value["policy_epoch"] >= 0
            and isinstance(value.get("policy_revision"), str) and bool(value.get("policy_revision"))
            and isinstance(snapshot, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot) is not None
            and isinstance(value.get("receipt_id"), str) and re.fullmatch(r"pol_[A-Za-z0-9_-]{8,128}", value["receipt_id"]) is not None
            and isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None
            and isinstance(key_id, str) and re.fullmatch(r"key_[0-9a-f]{16,64}", key_id) is not None
            and isinstance(value.get("ledger_sequence"), int) and not isinstance(value.get("ledger_sequence"), bool) and value["ledger_sequence"] > 0
            and isinstance(integrity, str) and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", integrity) is not None
            and isinstance(specific, dict) and specific.get("hookEventName") == event.get("hook_event_name")
            and specific.get("permissionDecision") in {"allow", "deny", "ask"}
            and isinstance(specific.get("permissionDecisionReason"), str) and "updatedInput" in specific
            and delegation_ok and reservation_ok and approval_ok and ownership_ok
            and format_ok and (no_effect or live_effect) and repair_ok
            and (continuation_unset or continuation_live))


def same_lifecycle(pre: dict[str, Any] | None, later: dict[str, Any] | None) -> bool:
    before = receipt(pre); after = receipt(later)
    stable = RECEIPT_FIELDS - MUTABLE_SETTLEMENT_FIELDS
    return bool(before) and bool(after) and all(before.get(name) == after.get(name) for name in stable)


def post_for(event: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    value = dict(event); value["hook_event_name"] = "PostToolUse"
    value["tool_response"] = result or {"status": "ok"}
    return value


def valid_lease(value: dict[str, Any]) -> bool:
    settled = value.get("settled_sequence")
    return (set(value) == LEASE_FIELDS and value.get("schema_version") == 1
            and isinstance(value.get("reservation_id"), str)
            and re.fullmatch(r"res_[A-Za-z0-9_-]{8,128}", value["reservation_id"]) is not None
            and all(isinstance(value.get(name), str) and bool(value[name]) for name in
                    ("session_id", "event_id", "receipt_id", "policy_id",
                     "policy_snapshot_digest", "key_id"))
            and isinstance(value.get("policy_epoch"), int) and not isinstance(value.get("policy_epoch"), bool)
            and value.get("state") in {"reserved", "settled", "expired"}
            and isinstance(value.get("created_sequence"), int) and value["created_sequence"] > 0
            and ((value.get("state") == "settled" and isinstance(settled, int)
                  and not isinstance(settled, bool) and settled > 0)
                 or (value.get("state") in {"reserved", "expired"} and settled is None))
            and isinstance(value.get("quota_remaining"), int) and not isinstance(value.get("quota_remaining"), bool)
            and value["quota_remaining"] >= 0)


def valid_approval(value: dict[str, Any]) -> bool:
    consumed = value.get("consumed_sequence")
    return (set(value) == APPROVAL_FIELDS and value.get("schema_version") == 1
            and isinstance(value.get("ticket_id"), str)
            and re.fullmatch(r"apt_[A-Za-z0-9_-]{8,128}", value["ticket_id"]) is not None
            and value.get("state") in {"pending", "ready", "consumed", "expired", "revoked", "superseded"}
            and isinstance(value.get("threshold"), int) and not isinstance(value.get("threshold"), bool)
            and value["threshold"] > 0
            and isinstance(value.get("approval_count"), int) and value["approval_count"] >= 0
            and isinstance(value.get("principal_ids"), list)
            and all(isinstance(item, str) for item in value["principal_ids"])
            and value["principal_ids"] == sorted(set(value["principal_ids"]))
            and isinstance(value.get("created_sequence"), int) and value["created_sequence"] > 0
            and ((value.get("state") == "consumed" and isinstance(consumed, int) and consumed > 0)
                 or (value.get("state") != "consumed" and consumed is None))
            and isinstance(value.get("expires_at"), int)
            and isinstance(value.get("approval_round"), int) and value["approval_round"] > 0
            and all(isinstance(value.get(name), list)
                    and all(isinstance(item, str) for item in value[name])
                    and value[name] == sorted(set(value[name]))
                    for name in ("required_groups", "satisfied_groups"))
            and all(value.get(name) is None or isinstance(value.get(name), str)
                    for name in ("predecessor_ticket_id", "superseded_by_ticket_id",
                                 "supersession_id")))


def valid_checkpoint(value: dict[str, Any]) -> bool:
    return (set(value) == CHECKPOINT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-checkpoint"
            and isinstance(value.get("origin_instance_id"), str)
            and re.fullmatch(r"inst_[A-Za-z0-9_-]{8,128}", value["origin_instance_id"]) is not None
            and isinstance(value.get("export_sequence"), int) and value["export_sequence"] > 0
            and (value.get("base_checkpoint_digest") is None
                 or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["base_checkpoint_digest"])) is not None)
            and isinstance(value.get("sealed_state"), str) and bool(value["sealed_state"])
            and value.get("ownership_state") in {"unmanaged", "active", "prepared", "accepted", "finalized", "checkpoint_only"}
            and isinstance(value.get("handoff_count"), int) and not isinstance(value.get("handoff_count"), bool) and value["handoff_count"] >= 0
            and ((value.get("owner_instance_id") is None and value.get("owner_generation") is None)
                 or (isinstance(value.get("owner_instance_id"), str)
                     and isinstance(value.get("owner_generation"), int)
                     and not isinstance(value.get("owner_generation"), bool)
                     and value["owner_generation"] > 0))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("checkpoint_digest", ""))) is not None
            and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(value.get("integrity", ""))) is not None)


def valid_checkpoint_view(value: dict[str, Any]) -> bool:
    return (set(value) == CHECKPOINT_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"exported", "imported"}
            and value.get("ownership_state") in {"unmanaged", "active", "prepared", "accepted", "finalized", "checkpoint_only"}
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("checkpoint_digest", ""))) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("lineage_digest", ""))) is not None)


def valid_handoff_offer(value: dict[str, Any]) -> bool:
    return (set(value) == HANDOFF_OFFER_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-handoff-offer"
            and re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(value.get("handoff_id", ""))) is not None
            and re.fullmatch(r"inst_[A-Za-z0-9_-]{8,128}", str(value.get("source_instance_id", ""))) is not None
            and isinstance(value.get("target_instance_id"), str) and bool(value["target_instance_id"])
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool) and value[name] > 0
                    for name in ("source_generation", "target_generation", "expires_at"))
            and isinstance(value.get("created_at"), int) and not isinstance(value.get("created_at"), bool)
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool) and value[name] >= 0
                    for name in ("policy_epoch", "audit_event_count", "receipt_count", "lease_count", "approval_ticket_count"))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("policy_snapshot_digest", ""))) is not None
            and isinstance(value.get("sealed_state"), str) and bool(value["sealed_state"])
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("offer_digest", ""))) is not None
            and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(value.get("integrity", ""))) is not None)


def valid_handoff_ack(value: dict[str, Any]) -> bool:
    return (set(value) == HANDOFF_ACK_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-handoff-ack"
            and re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(value.get("handoff_id", ""))) is not None
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool) and value[name] > 0
                    for name in ("source_generation", "target_generation", "accepted_at"))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("offer_digest", ""))) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("ack_digest", ""))) is not None)


def valid_handoff_commit(value: dict[str, Any]) -> bool:
    return (set(value) == HANDOFF_COMMIT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-handoff-commit"
            and re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(value.get("handoff_id", ""))) is not None
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool) and value[name] > 0
                    for name in ("source_generation", "target_generation", "finalized_at"))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("offer_digest", ""))) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("ack_digest", ""))) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("commit_digest", ""))) is not None)


def valid_handoff_view(value: dict[str, Any]) -> bool:
    return (set(value) == HANDOFF_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"prepared", "accepted", "finalized", "activated", "aborted"}
            and re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(value.get("handoff_id", ""))) is not None
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool) and value[name] > 0
                    for name in ("source_generation", "target_generation", "created_sequence", "transition_sequence", "expires_at"))
            and all(value.get(name) is None or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get(name))) is not None
                    for name in ("ack_digest", "commit_digest"))
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("offer_digest", ""))) is not None)


def valid_effect(value: dict[str, Any]) -> bool:
    return (set(value) == EFFECT_FIELDS and value.get("schema_version") == 1
            and re.fullmatch(r"eff_[A-Za-z0-9_-]{8,128}", str(value.get("effect_id", ""))) is not None
            and value.get("phase") in {"authorized", "completed", "expired"}
            and value.get("state") in {"pending", "claimed", "delivered"}
            and isinstance(value.get("effect_generation"), int)
            and not isinstance(value.get("effect_generation"), bool) and value["effect_generation"] > 0
            and isinstance(value.get("attempt_count"), int)
            and not isinstance(value.get("attempt_count"), bool) and value["attempt_count"] >= 0
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool)
                    and value[name] > 0 for name in ("created_sequence", "updated_sequence"))
            and all(value.get(name) is None or isinstance(value.get(name), str)
                    for name in ("owner_instance_id", "claim_id", "consumer_id", "delivery_id", "outcome_digest"))
            and (value.get("owner_generation") is None
                 or isinstance(value.get("owner_generation"), int)
                 and not isinstance(value.get("owner_generation"), bool)
                 and value["owner_generation"] > 0))


def valid_effect_claim(value: dict[str, Any]) -> bool:
    return (set(value) == EFFECT_CLAIM_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-effect-claim"
            and re.fullmatch(r"clm_[A-Za-z0-9_-]{8,128}", str(value.get("claim_id", ""))) is not None
            and isinstance(value.get("items"), list) and bool(value["items"])
            and all(valid_effect(item) and item.get("state") == "claimed" for item in value["items"])
            and re.fullmatch(r"efc_[A-Za-z0-9_-]{8,128}", str(value.get("claim_token", ""))) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("claim_digest", ""))) is not None
            and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(value.get("integrity", ""))) is not None)


def valid_upgrade_manifest(value: dict[str, Any]) -> bool:
    return (set(value) == UPGRADE_MANIFEST_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-upgrade-manifest"
            and value.get("source_format_version") == 1 and value.get("target_format_version") == 2
            and re.fullmatch(r"upg_[A-Za-z0-9_-]{8,128}", str(value.get("upgrade_id", ""))) is not None
            and all(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get(name, ""))) is not None
                    for name in ("source_state_digest", "expected_upgrade_lineage_digest", "upgrade_digest")))


def valid_upgrade_status(value: dict[str, Any]) -> bool:
    return (set(value) == UPGRADE_STATUS_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-upgrade-status"
            and value.get("state") == "building" and isinstance(value.get("ready_to_commit"), bool)
            and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool)
                    and value[name] >= 0 for name in ("base_sequence", "cursor_sequence", "catchup_sequence", "remaining_items")))


def valid_upgrade_commit(value: dict[str, Any]) -> bool:
    return (set(value) == UPGRADE_COMMIT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-upgrade-commit"
            and value.get("source_format_version") == 1 and value.get("target_format_version") == 2
            and all(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get(name, ""))) is not None
                    for name in ("source_state_digest", "previous_upgrade_lineage_digest",
                                 "upgrade_digest", "upgrade_lineage_digest")))


def valid_upgrade_view(value: dict[str, Any]) -> bool:
    return (set(value) == UPGRADE_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"building", "committed", "aborted"}
            and isinstance(value.get("source_format_version"), int)
            and isinstance(value.get("target_format_version"), int)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("source_state_digest", ""))) is not None)


def valid_repair_manifest(value: dict[str, Any]) -> bool:
    return (set(value) == REPAIR_MANIFEST_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-manifest"
            and re.fullmatch(r"rpr_[A-Za-z0-9_-]{8,128}", str(value.get("repair_id", ""))) is not None
            and isinstance(value.get("repair_generation"), int)
            and not isinstance(value.get("repair_generation"), bool) and value["repair_generation"] > 0)


def valid_repair_status(value: dict[str, Any]) -> bool:
    return (set(value) == REPAIR_STATUS_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-status"
            and value.get("state") in {"rebuilding", "verifying"}
            and isinstance(value.get("candidate_cursor"), int)
            and isinstance(value.get("candidate_item_count"), int)
            and isinstance(value.get("remaining_items"), int)
            and isinstance(value.get("ready_for_verification"), bool)
            and isinstance(value.get("attested_verifiers"), list))


def valid_repair_attestation(value: dict[str, Any]) -> bool:
    return (set(value) == REPAIR_ATTESTATION_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-attestation"
            and re.fullmatch(r"sha256:[0-9a-f]{64}",
                             str(value.get("attestation_digest", ""))) is not None
            and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}",
                             str(value.get("integrity", ""))) is not None)


def valid_repair_commit(value: dict[str, Any]) -> bool:
    return (set(value) == REPAIR_COMMIT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-commit"
            and isinstance(value.get("verifier_ids"), list)
            and value["verifier_ids"] == sorted(set(value["verifier_ids"]))
            and re.fullmatch(r"sha256:[0-9a-f]{64}",
                             str(value.get("repair_lineage_digest", ""))) is not None)


def valid_repair_view(value: dict[str, Any]) -> bool:
    return (set(value) == REPAIR_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"quarantined", "rebuilding", "verifying",
                                       "committed", "aborted"}
            and isinstance(value.get("verifier_ids"), list))


def valid_continuation_token(value: dict[str, Any]) -> bool:
    return (set(value) == CONTINUATION_TOKEN_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-continuation-token"
            and re.fullmatch(r"ctn_[A-Za-z0-9_-]{8,128}",
                             str(value.get("continuation_id", ""))) is not None
            and re.fullmatch(r"ctk_[A-Za-z0-9_-]{8,128}", str(value.get("token", ""))) is not None)


def valid_continuation_view(value: dict[str, Any]) -> bool:
    return (set(value) == CONTINUATION_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"pending", "activated", "cancelled", "expired", "invalidated"}
            and re.fullmatch(r"sca_[A-Za-z0-9_-]{8,128}",
                             str(value.get("session_authority_id", ""))) is not None)


def valid_integrity_view(value: dict[str, Any]) -> bool:
    return (set(value) == INTEGRITY_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"not_configured", "sealed", "compromised"}
            and isinstance(value.get("generation"), int) and not isinstance(value.get("generation"), bool)
            and value["generation"] >= 0
            and isinstance(value.get("sealed_file_count"), int) and not isinstance(value.get("sealed_file_count"), bool)
            and value["sealed_file_count"] >= 0
            and isinstance(value.get("protected_path_count"), int) and not isinstance(value.get("protected_path_count"), bool)
            and value["protected_path_count"] >= 0
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("manifest_digest", ""))) is not None
            and value.get("authority_state") in {"unmanaged", "active", "quarantined"}
            and isinstance(value.get("created_sequence"), int) and not isinstance(value.get("created_sequence"), bool)
            and value["created_sequence"] >= 0
            and isinstance(value.get("transition_sequence"), int) and not isinstance(value.get("transition_sequence"), bool)
            and value["transition_sequence"] >= 0)


def check(identifier: str, points: int, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"id": identifier, "points": points, "passed": bool(passed), "evidence": redact(evidence)}


def finish(rt: Runtime, assertions: list[dict[str, Any]], caps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if (not assertions or any(not isinstance(item, dict) or set(item) != {"id", "points", "passed", "evidence"}
                              or not isinstance(item.get("id"), str)
                              or not isinstance(item.get("points"), int) or isinstance(item.get("points"), bool)
                              or item["points"] <= 0 or not isinstance(item.get("passed"), bool)
                              or not isinstance(item.get("evidence"), str) or len(item["evidence"]) > 1200
                              for item in assertions)
            or len({item["id"] for item in assertions}) != len(assertions)
            or sum(item["points"] for item in assertions) != 100):
        raise HarnessError(f"assertion weights for {rt.case_id} do not total 100")
    all_caps = caps or []
    if any(not isinstance(cap, dict) or set(cap) != {"id", "maximum", "triggered"}
           or not isinstance(cap.get("id"), str)
           or not isinstance(cap.get("maximum"), int) or isinstance(cap.get("maximum"), bool)
           or not 0 <= cap["maximum"] <= 100 or not isinstance(cap.get("triggered"), bool)
           for cap in all_caps):
        raise HarnessError(f"local cap contract is malformed for {rt.case_id}")
    if rt.case_id in CASE_IDS:
        expected = assertion_contract()["cases"][rt.case_id]
        if ([(item["id"], item["points"]) for item in assertions]
                != [(item["id"], item["points"]) for item in expected["assertions"]]
                or [(item["id"], item["maximum"]) for item in all_caps]
                != [(item["id"], item["maximum"]) for item in expected["caps"]]):
            raise HarnessError(f"assertion semantic contract mismatch for {rt.case_id}")
    raw = sum(item["points"] for item in assertions if item["passed"])
    applied = [{"id": cap["id"], "maximum": cap["maximum"], "triggered": True}
               for cap in all_caps if cap["triggered"]]
    score = min([raw, *(cap["maximum"] for cap in applied)])
    return {"case_id": rt.case_id, "valid": True, "score": score, "raw_score": raw, "maximum": 100,
            "assertions": assertions, "passed_assertion_ids": [a["id"] for a in assertions if a["passed"]],
            "failed_assertion_ids": [a["id"] for a in assertions if not a["passed"]],
            "local_caps": applied, "commands": rt.commands,
            "duration_seconds": round(sum(float(item["duration_seconds"]) for item in rt.commands), 4)}


def policy_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def atomic_policy(path: Path, data: bytes, suffix: str = "next") -> None:
    temporary = path.with_name(path.name + "." + suffix)
    temporary.write_bytes(data)
    os.replace(temporary, path)


def valid_active_lines(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema_version") == 1:
            values.append(value)
    return values


def event_digest(value: dict[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "event_digest"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def valid_chain(values: list[dict[str, Any]]) -> bool:
    previous = "sha256:" + "0" * 64
    sequence = 0
    for value in values:
        current = value.get("sequence")
        if (not isinstance(current, int) or isinstance(current, bool) or current != sequence + 1
                or value.get("previous_event_digest") != previous
                or value.get("event_digest") != event_digest(value)):
            return False
        previous = str(value["event_digest"])
        sequence = current
    return True


def approver_claims(principal: str, policy_id: str, epoch: int, tool: str,
                    paths: list[str], hosts: list[str]) -> dict[str, Any]:
    return {"principal_id": principal, "policy_id": policy_id, "policy_epoch": epoch,
            "tools": [tool], "path_prefixes": paths, "hosts": hosts}


def enroll_approver(rt: Runtime, claims: dict[str, Any], *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    value, text, code = rt.maintain("--enroll-approver", claims, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def cast_approval(rt: Runtime, ticket_id: str, vote_id: str, token: dict[str, Any],
                  *, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    request = {"ticket_id": ticket_id, "vote_id": vote_id, "principal_token": token}
    values, text, code = rt.maintain("--cast-approval", request, state_dir=state_dir, jsonl=True)
    return values if isinstance(values, list) else [], code, text


def export_checkpoint(rt: Runtime, transfer_id: str, secret: str,
                      *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"transfer_id": transfer_id, "transfer_secret": secret}
    value, text, code = rt.maintain("--export-checkpoint", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def import_checkpoint(rt: Runtime, checkpoint: dict[str, Any], secret: str, mode: str,
                      expected: str | None, *, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    request = {"checkpoint": checkpoint, "transfer_secret": secret, "mode": mode,
               "expected_lineage_digest": expected}
    values, text, code = rt.maintain("--import-checkpoint", request, state_dir=state_dir, jsonl=True)
    return values if isinstance(values, list) else [], code, text


def supersede_approval(rt: Runtime, ticket_id: str, supersession_id: str, round_number: int,
                       *, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    request = {"supersession_id": supersession_id, "expected_approval_round": round_number}
    values, text, code = rt.maintain("--supersede-approval", request, argument=ticket_id,
                                     state_dir=state_dir, jsonl=True)
    return values if isinstance(values, list) else [], code, text


def prepare_handoff(rt: Runtime, handoff_id: str, target_id: str, secret: str,
                    *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"handoff_id": handoff_id, "target_instance_id": target_id,
               "handoff_secret": secret}
    value, text, code = rt.maintain("--prepare-handoff", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def accept_handoff(rt: Runtime, offer: dict[str, Any], secret: str,
                   *, state_dir: Path) -> tuple[dict[str, Any], int, str]:
    value, text, code = rt.maintain("--accept-handoff", {"offer": offer, "handoff_secret": secret},
                                    state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def finalize_handoff(rt: Runtime, ack: dict[str, Any], secret: str,
                     *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    value, text, code = rt.maintain("--finalize-handoff", {"ack": ack, "handoff_secret": secret},
                                    state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def activate_handoff(rt: Runtime, commit: dict[str, Any], secret: str,
                     *, state_dir: Path) -> tuple[list[dict[str, Any]], int, str]:
    value, text, code = rt.maintain("--activate-handoff",
                                    {"commit": commit, "handoff_secret": secret},
                                    state_dir=state_dir, jsonl=True)
    return value if isinstance(value, list) else [], code, text


def claim_effects(rt: Runtime, consumer_id: str, claim_id: str, max_items: int = 10,
                  *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"consumer_id": consumer_id, "claim_id": claim_id, "max_items": max_items}
    value, text, code = rt.maintain("--claim-effects", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def ack_effect(rt: Runtime, effect_id: str, claim: dict[str, Any], generation: int,
               delivery_id: str, *, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    request = {"claim_id": claim.get("claim_id"), "claim_token": claim.get("claim_token"),
               "expected_effect_generation": generation, "delivery_id": delivery_id}
    value, text, code = rt.maintain("--ack-effect", request, argument=effect_id,
                                    state_dir=state_dir, jsonl=True)
    return value if isinstance(value, list) else [], code, text


def nack_effect(rt: Runtime, effect_id: str, claim: dict[str, Any], generation: int,
                retry_id: str, *, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    request = {"claim_id": claim.get("claim_id"), "claim_token": claim.get("claim_token"),
               "expected_effect_generation": generation, "retry_id": retry_id}
    value, text, code = rt.maintain("--nack-effect", request, argument=effect_id,
                                    state_dir=state_dir, jsonl=True)
    return value if isinstance(value, list) else [], code, text


def begin_upgrade(rt: Runtime, upgrade_id: str, secret: str,
                  *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"upgrade_id": upgrade_id, "target_format_version": 2,
               "expected_upgrade_lineage_digest": "sha256:" + "0" * 64,
               "upgrade_secret": secret}
    value, text, code = rt.maintain("--begin-upgrade", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def advance_upgrade(rt: Runtime, upgrade_id: str, manifest: dict[str, Any], secret: str,
                    cursor: int, *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"manifest": manifest, "upgrade_secret": secret,
               "expected_cursor_sequence": cursor}
    value, text, code = rt.maintain("--advance-upgrade", request, argument=upgrade_id,
                                    state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def commit_upgrade(rt: Runtime, status: dict[str, Any], secret: str,
                   *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    value, text, code = rt.maintain("--commit-upgrade",
                                    {"status": status, "upgrade_secret": secret},
                                    state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def abort_upgrade(rt: Runtime, upgrade_id: str, secret: str,
                  *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    value, text, code = rt.maintain("--abort-upgrade", {"upgrade_secret": secret},
                                    argument=upgrade_id, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def issue_continuation(rt: Runtime, continuation_id: str, source_session: str,
                       target_session: str, generation: int, secret: str,
                       *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"continuation_id": continuation_id, "source_session_id": source_session,
               "target_session_id": target_session, "expected_session_generation": generation,
               "continuation_secret": secret}
    value, text, code = rt.maintain("--issue-continuation", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def cancel_continuation(rt: Runtime, continuation_id: str, generation: int, secret: str,
                        *, state_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, str]:
    request = {"expected_session_generation": generation, "continuation_secret": secret}
    value, text, code = rt.maintain("--cancel-continuation", request, argument=continuation_id,
                                    state_dir=state_dir, jsonl=True)
    return value if isinstance(value, list) else [], code, text


def begin_repair(rt: Runtime, repair_id: str, generation: int, secret: str,
                 *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"repair_id": repair_id, "expected_repair_generation": generation,
               "repair_secret": secret}
    value, text, code = rt.maintain("--begin-repair", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def advance_repair(rt: Runtime, repair_id: str, manifest: dict[str, Any], secret: str,
                   cursor: int, *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"manifest": manifest, "repair_secret": secret,
               "expected_candidate_cursor": cursor}
    value, text, code = rt.maintain("--advance-repair", request, argument=repair_id,
                                    state_dir=state_dir, extra=("--limit", "3"))
    return value if isinstance(value, dict) else {}, code, text


def verify_repair(rt: Runtime, repair_id: str, status: dict[str, Any], repair_secret: str,
                  verifier_id: str, verifier_secret: str,
                  *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"status": status, "repair_secret": repair_secret,
               "verifier_id": verifier_id, "verifier_secret": verifier_secret}
    value, text, code = rt.maintain("--verify-repair", request, argument=repair_id,
                                    state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def commit_repair(rt: Runtime, status: dict[str, Any], repair_secret: str,
                  attestations: list[dict[str, Any]],
                  *, state_dir: Path | None = None) -> tuple[dict[str, Any], int, str]:
    request = {"status": status, "repair_secret": repair_secret,
               "attestations": attestations}
    value, text, code = rt.maintain("--commit-repair", request, state_dir=state_dir)
    return value if isinstance(value, dict) else {}, code, text


def damage_interior_state(rt: Runtime, *, state_dir: Path | None = None) -> bool:
    selected_state = state_dir or rt.state
    selected: tuple[Path, list[bytes]] | None = None
    for path in (selected_state / "audit.snapshot.jsonl", selected_state / "audit.jsonl"):
        if not path.is_file():
            continue
        lines = path.read_bytes().splitlines(keepends=True)
        if len(lines) >= 3 and (selected is None or len(lines) > len(selected[1])):
            selected = (path, lines)
    if selected is None:
        return False
    path, lines = selected
    lines[-2] = ('{"schema_version":1,"kind":"EVAL_DAMAGE_MARKER_8c33"\n').encode()
    path.write_bytes(b"".join(lines))
    return True


def drive_repair(rt: Runtime, repair_id: str, verifiers: tuple[str, str], label: str,
                 *, state_dir: Path | None = None, crash_first: bool = False) -> dict[str, Any]:
    secret = f"EVAL_REPAIR_SECRET_5a92-{label}-000000000000"
    manifest, begin_code, begin_text = begin_repair(rt, repair_id, 0, secret, state_dir=state_dir)
    crash: dict[str, Any] | None = None
    if crash_first and manifest:
        request = {"manifest": manifest, "repair_secret": secret,
                   "expected_candidate_cursor": 0}
        payload = json.dumps(request)
        crash = rt.crash_partial_maintenance("--advance-repair",
            payload[:max(1, len(payload) // 2)], state_dir=state_dir or rt.state,
            argument=repair_id)
    statuses: list[dict[str, Any]] = []; texts = [begin_text]; cursor = 0
    for _ in range(40):
        status, code, status_text = advance_repair(
            rt, repair_id, manifest, secret, cursor, state_dir=state_dir)
        texts.append(status_text)
        if code != 0 or not status:
            break
        statuses.append(status); cursor = int(status.get("candidate_cursor", cursor))
        if status.get("ready_for_verification"):
            break
    ready = statuses[-1] if statuses else {}
    attestations: list[dict[str, Any]] = []; verify_codes: list[int] = []
    for verifier in verifiers:
        attestation, code, verify_text = verify_repair(
            rt, repair_id, ready, secret, verifier,
            f"EVAL_VERIFIER_SECRET_2f18-{label}-{verifier}-000000", state_dir=state_dir)
        attestations.append(attestation); verify_codes.append(code); texts.append(verify_text)
    replay, replay_code, replay_text = verify_repair(
        rt, repair_id, ready, secret, verifiers[0],
        f"EVAL_VERIFIER_SECRET_2f18-{label}-{verifiers[0]}-000000", state_dir=state_dir)
    texts.append(replay_text)
    _bad, bad_code, bad_text = verify_repair(
        rt, repair_id, ready, secret, "unlisted-verifier",
        f"EVAL_VERIFIER_SECRET_2f18-{label}-unlisted-000000", state_dir=state_dir)
    texts.append(bad_text)
    commit, commit_code, commit_text = commit_repair(
        rt, ready, secret, attestations, state_dir=state_dir)
    commit_replay, commit_replay_code, commit_replay_text = commit_repair(
        rt, ready, secret, attestations, state_dir=state_dir)
    texts.extend((commit_text, commit_replay_text))
    repairs, view_text, view_code = rt.inspect("--repairs", "--limit", "20", state_dir=state_dir)
    texts.append(view_text)
    return {"secret": secret, "manifest": manifest, "begin_code": begin_code,
            "statuses": statuses, "ready": ready, "attestations": attestations,
            "verify_codes": verify_codes, "attestation_replay": replay,
            "attestation_replay_code": replay_code, "bad_verifier_code": bad_code,
            "commit": commit, "commit_code": commit_code, "commit_replay": commit_replay,
            "commit_replay_code": commit_replay_code, "repairs": repairs,
            "view_code": view_code, "crash": crash, "text": "".join(texts)}


def integrity_probe(rt: Runtime) -> dict[str, Any]:
    """Exercise Cycle-9 in an isolated state directory before the case proper."""
    policy_path = rt.workspace / ".claude" / "policy-provenance.json"
    previous_policy = policy_path.read_bytes() if policy_path.is_file() else None
    rt.policy("policy_integrity.json")
    state = rt.root / "integrity-state"
    shutil.rmtree(state, ignore_errors=True)
    events = rt.assets_json("events.json")
    base = json.loads(json.dumps(events[0]))
    base["session_id"] = "integrity-probe"
    base["tool_use_id"] = "integrity-seal"
    base["tool_name"] = "Read"
    base["tool_input"] = {"file_path": "src/app.py"}
    protected = json.loads(json.dumps(base))
    protected["tool_use_id"] = "integrity-protected-write"
    protected["tool_name"] = "Write"
    protected["tool_input"] = {"file_path": "./.claude//policy-provenance.json", "content": "changed"}
    shell = json.loads(json.dumps(base))
    shell["tool_use_id"] = "integrity-protected-shell"
    shell["tool_name"] = "Bash"
    shell["tool_input"] = {"command": "printf changed > ./.claude//policy-provenance.json"}
    tampered = json.loads(json.dumps(base))
    tampered["tool_use_id"] = "integrity-after-tamper"
    hook = rt.plugin / HOOK_REL
    original = hook.read_bytes()
    try:
        sealed = rt.invoke(base, state_dir=state)
        protected_response = rt.invoke(protected, state_dir=state)
        shell_response = rt.invoke(shell, state_dir=state)
        before, before_text, before_code = rt.inspect("--integrity", state_dir=state)
        hook.write_bytes(original + b"\n# evaluator integrity byte mutation\n")
        compromised = rt.invoke(tampered, state_dir=state)
        after, after_text, after_code = rt.inspect("--integrity", state_dir=state)
    finally:
        hook.write_bytes(original)
    restored, restored_text, restored_code = rt.inspect("--integrity", state_dir=state)
    if previous_policy is None:
        policy_path.unlink(missing_ok=True)
    else:
        policy_path.write_bytes(previous_policy)
    observed = json.dumps([sealed, protected_response, shell_response, compromised], sort_keys=True)
    observed += before_text + after_text + restored_text + rt.raw_state(state_dir=state)
    before_view = before[0] if len(before) == 1 else {}
    after_view = after[0] if len(after) == 1 else {}
    restored_view = restored[0] if len(restored) == 1 else {}
    protected_receipts = [receipt(protected_response), receipt(shell_response)]
    passed = (
        permission(sealed) == "allow" and valid_receipt(sealed, base)
        and permission(protected_response) == "deny" and permission(shell_response) == "deny"
        and all(item.get("reservation_id") is None and item.get("approval_ticket_id") is None
                and item.get("effect_id") is None for item in protected_receipts)
        and before_code == 0 and valid_integrity_view(before_view)
        and before_view.get("state") == "sealed" and before_view.get("generation") == 1
        and before_view.get("sealed_file_count") == 5
        and before_view.get("protected_path_count") == 2
        and before_view.get("authority_state") == "active"
        and permission(compromised) == "deny"
        and receipt(compromised).get("integrity_state") == "quarantined"
        and after_code == 0 and restored_code == 0
        and valid_integrity_view(after_view) and valid_integrity_view(restored_view)
        and after_view.get("state") == "compromised"
        and restored_view.get("state") == "compromised"
        and after_view.get("authority_state") == "quarantined"
        and restored_view.get("authority_state") == "quarantined"
        and after_view.get("generation") == before_view.get("generation")
        and restored_view.get("manifest_digest") == before_view.get("manifest_digest")
        and int(after_view.get("transition_sequence") or 0)
            >= int(before_view.get("transition_sequence") or 0)
    )
    bypass = (permission(protected_response) == "allow" or permission(shell_response) == "allow"
              or permission(compromised) == "allow"
              or after_view.get("state") != "compromised"
              or restored_view.get("state") != "compromised")
    return {
        "sealed": sealed, "protected": protected_response, "shell": shell_response,
        "compromised": compromised, "before": before, "after": after, "restored": restored,
        "before_code": before_code, "after_code": after_code, "restored_code": restored_code,
        "observed": observed, "passed": passed, "bypass": bypass,
    }


def cycle8_common(rt: Runtime, event: dict[str, Any], policy_id: str, epoch: int,
                  principals: tuple[str, str], verifiers: tuple[str, str], label: str,
                  paths: list[str], hosts: list[str], *, issue_race: bool = False,
                  crash_repair: bool = False,
                  before_damage: Callable[[], Any] | None = None) -> dict[str, Any]:
    source = json.loads(json.dumps(event)); source["session_id"] = f"{label}-source"
    source["tool_use_id"] = f"{label}-source-action"
    pending, active, tokens, approval_codes, approval_text = approve_action(
        rt, source, policy_id, epoch, principals, paths, hosts)
    authority = str(receipt(active).get("session_authority_id") or "")
    generation = int(receipt(active).get("session_generation") or 0)
    continuation_secret = f"EVAL_CONTINUATION_SECRET_8d41-{label}-000000000"
    if issue_race:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            issues = [future.result(timeout=12) for future in [
                pool.submit(issue_continuation, rt, f"ctn_{label}_race_{index}",
                            source["session_id"], f"{label}-target-{index}", generation,
                            continuation_secret) for index in range(2)]]
        winners = [item for item in issues if item[1] == 0]
        token = winners[0][0] if len(winners) == 1 else {}
        target_session = str(token.get("target_session_id") or f"{label}-target-0")
        issue_code = winners[0][1] if len(winners) == 1 else -1
        issue_replay, replay_code, _ = issue_continuation(
            rt, str(token.get("continuation_id") or f"ctn_{label}_race_0"),
            source["session_id"], target_session, generation, continuation_secret)
    else:
        target_session = f"{label}-target"
        token, issue_code, _ = issue_continuation(
            rt, f"ctn_{label}_success", source["session_id"], target_session,
            generation, continuation_secret)
        issue_replay, replay_code, _ = issue_continuation(
            rt, f"ctn_{label}_success", source["session_id"], target_session,
            generation, continuation_secret)
        winners = [(token, issue_code, "")] if issue_code == 0 else []
    source_post = rt.invoke(post_for(source, {"status": "ok", "secret": "EVAL_CLAIM_SECRET_6b22"}))
    blocked = json.loads(json.dumps(event)); blocked["session_id"] = source["session_id"]
    blocked["tool_use_id"] = f"{label}-source-blocked"
    blocked_response = rt.invoke(blocked)
    wrong = json.loads(json.dumps(event)); wrong["session_id"] = f"{label}-wrong-target"
    wrong["tool_use_id"] = f"{label}-wrong-target"
    if isinstance(wrong.get("tool_input"), dict):
        wrong["tool_input"]["_policy_continuation"] = token
    wrong_response = rt.invoke(wrong)
    target = json.loads(json.dumps(event)); target["session_id"] = target_session
    target["tool_use_id"] = f"{label}-target-activate"
    if isinstance(target.get("tool_input"), dict):
        target["tool_input"]["_policy_continuation"] = token
    activated = rt.invoke_many([target, target], workers=2)
    target_current = activated[0] if activated else None
    target_ticket = str(receipt(target_current).get("approval_ticket_id") or "")
    if target_ticket:
        for index, token_value in enumerate(tokens):
            cast_approval(rt, target_ticket, f"{label}-target-vote-{index}", token_value)
        target_consume_event = json.loads(json.dumps(target))
        if isinstance(target_consume_event.get("tool_input"), dict):
            target_consume_event["tool_input"].pop("_policy_continuation", None)
        target_current = rt.invoke(target_consume_event)
    target_post = rt.invoke(post_for({key: value for key, value in target.items()
                                     if key != "tool_input"} | {"tool_input": {
                                         key: value for key, value in target.get("tool_input", {}).items()
                                         if key != "_policy_continuation"}}, {"status": "ok"}))
    reused = json.loads(json.dumps(target)); reused["tool_use_id"] = f"{label}-token-reuse"
    reused_response = rt.invoke(reused)

    target_generation = int(receipt(target_current).get("session_generation") or generation + 1)
    stale_token, stale_issue_code, _ = issue_continuation(
        rt, f"ctn_{label}_repair_stale", target_session, f"{label}-stale-target",
        target_generation, f"EVAL_CONTINUATION_SECRET_8d41-{label}-stale-000000")
    if before_damage is not None:
        before_damage()
    pre_damage_audit, _, _ = rt.inspect()
    damaged = damage_interior_state(rt)
    quarantine_probe = json.loads(json.dumps(event)); quarantine_probe["session_id"] = target_session
    quarantine_probe["tool_use_id"] = f"{label}-quarantine-probe"
    quarantined = rt.invoke(quarantine_probe)
    _quarantine_claim, quarantine_claim_code, _ = claim_effects(
        rt, f"{label}-quarantine-consumer", f"clm_{label}_quarantine")
    repaired = drive_repair(rt, f"rpr_{label}_repair_01", verifiers, label,
                            crash_first=crash_repair)
    stale_event = json.loads(json.dumps(event)); stale_event["session_id"] = f"{label}-stale-target"
    stale_event["tool_use_id"] = f"{label}-stale-after-repair"
    if isinstance(stale_event.get("tool_input"), dict):
        stale_event["tool_input"]["_policy_continuation"] = stale_token
    stale_response = rt.invoke(stale_event)
    fresh = json.loads(json.dumps(event)); fresh["session_id"] = target_session
    fresh["tool_use_id"] = f"{label}-fresh-after-repair"
    fresh_pending = rt.invoke(fresh)
    fresh_current = fresh_pending
    fresh_ticket = str(receipt(fresh_pending).get("approval_ticket_id") or "")
    if fresh_ticket:
        for index, token_value in enumerate(tokens):
            cast_approval(rt, fresh_ticket, f"{label}-fresh-vote-{index}", token_value)
        fresh_current = rt.invoke(fresh)
    continuations, continuation_text, continuation_code = rt.inspect(
        "--continuations", "--limit", "20")
    effects, effect_text, effect_code = rt.inspect("--effects", "--limit", "20")
    approvals, approval_view_text, approval_view_code = rt.inspect("--approvals", "--limit", "20")
    leases, lease_text, lease_code = rt.inspect("--leases", "--limit", "20")
    audit, audit_text, audit_code = rt.inspect()
    observed = json.dumps([pending, active, source_post, blocked_response, wrong_response,
                           *activated, target_current, target_post, reused_response,
                           quarantined, stale_response, fresh_pending, fresh_current], sort_keys=True)
    observed += (approval_text + repaired["text"] + continuation_text + effect_text
                 + approval_view_text + lease_text + audit_text + rt.raw_state())
    bearer_values = [str(item.get("token") or "") for item in [*tokens, token, stale_token]
                     if isinstance(item, dict) and item.get("token")]
    leak = (bool(rt.disclosed_literals)
            or any(value and value in observed for value in bearer_values)
            or continuation_secret in observed or repaired["secret"] in observed
            or "EVAL_DAMAGE_MARKER_8c33" in observed)
    return {"pending": pending, "active": active, "tokens": tokens,
            "approval_codes": approval_codes, "authority": authority, "generation": generation,
            "issue_winners": winners, "token": token, "issue_code": issue_code,
            "issue_replay": issue_replay, "replay_code": replay_code,
            "source_post": source_post, "blocked": blocked_response, "wrong": wrong_response,
            "activated": activated, "target": target_current, "target_post": target_post,
            "reused": reused_response, "stale_token": stale_token,
            "stale_issue_code": stale_issue_code, "damaged": damaged,
            "pre_damage_audit": pre_damage_audit, "quarantined": quarantined,
            "quarantine_claim_code": quarantine_claim_code, "repaired": repaired,
            "stale": stale_response, "fresh_pending": fresh_pending, "fresh": fresh_current,
            "continuations": continuations, "continuation_code": continuation_code,
            "effects": effects, "effect_code": effect_code, "approvals": approvals,
            "approval_view_code": approval_view_code, "leases": leases,
            "lease_code": lease_code, "audit": audit, "audit_code": audit_code,
            "leak": leak, "observed": observed}


def approve_action(rt: Runtime, event: dict[str, Any], policy_id: str, epoch: int,
                   principals: tuple[str, str], paths: list[str], hosts: list[str],
                   *, state_dir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any],
                                                               list[dict[str, Any]], list[int], str]:
    pending = rt.invoke(event, state_dir=state_dir)
    ticket = str(receipt(pending).get("approval_ticket_id", ""))
    tokens: list[dict[str, Any]] = []
    codes: list[int] = []
    texts: list[str] = []
    for index, principal in enumerate(principals):
        token, code, text_value = enroll_approver(
            rt, approver_claims(principal, policy_id, epoch, str(event.get("tool_name")), paths, hosts),
            state_dir=state_dir)
        tokens.append(token); codes.append(code); texts.append(text_value)
        _view, vote_code, vote_text = cast_approval(
            rt, ticket, f"{principal}-vote-{index}", token, state_dir=state_dir)
        codes.append(vote_code); texts.append(vote_text)
    allowed = rt.invoke(event, state_dir=state_dir)
    return pending, allowed, tokens, codes, "".join(texts)


def drive_upgrade(rt: Runtime, upgrade_id: str, secret: str,
                  concurrent_change: Callable[[], Any] | None = None,
                  *, state_dir: Path | None = None) -> dict[str, Any]:
    manifest, begin_code, begin_text = begin_upgrade(rt, upgrade_id, secret, state_dir=state_dir)
    statuses: list[dict[str, Any]] = []
    texts = [begin_text]
    cursor = 0
    for _ in range(24):
        status, code, status_text = advance_upgrade(
            rt, upgrade_id, manifest, secret, cursor, state_dir=state_dir)
        texts.append(status_text)
        if code != 0 or not status:
            break
        statuses.append(status)
        cursor = int(status.get("cursor_sequence", cursor))
        if status.get("ready_to_commit"):
            break
    stale_code = -1
    if concurrent_change is not None:
        concurrent_change()
        if statuses:
            _stale, stale_code, stale_text = commit_upgrade(
                rt, statuses[-1], secret, state_dir=state_dir)
            texts.append(stale_text)
        for _ in range(24):
            status, code, status_text = advance_upgrade(
                rt, upgrade_id, manifest, secret, cursor, state_dir=state_dir)
            texts.append(status_text)
            if code != 0 or not status:
                break
            statuses.append(status); cursor = int(status.get("cursor_sequence", cursor))
            if status.get("ready_to_commit"):
                break
    final_status = statuses[-1] if statuses else {}
    commit, commit_code, commit_text = commit_upgrade(rt, final_status, secret, state_dir=state_dir)
    replay, replay_code, replay_text = commit_upgrade(rt, final_status, secret, state_dir=state_dir)
    texts.extend((commit_text, replay_text))
    upgrades, view_text, view_code = rt.inspect("--upgrades", "--limit", "20", state_dir=state_dir)
    texts.append(view_text)
    return {"manifest": manifest, "begin_code": begin_code, "statuses": statuses,
            "stale_code": stale_code, "commit": commit, "commit_code": commit_code,
            "replay": replay, "replay_code": replay_code, "upgrades": upgrades,
            "view_code": view_code, "text": "".join(texts)}


def cycle7_scenario_001(rt: Runtime) -> dict[str, Any]:
    rt.policy(); events = [json.loads(json.dumps(item)) for item in rt.assets_json("events.json")]
    (rt.workspace / "src" / "escape").symlink_to(rt.outside, target_is_directory=True)
    denied = [rt.invoke(events[index]) for index in (2, 3, 5, 6)]
    action = events[0]; action["session_id"] = "shell7-main"
    pending, allowed, tokens, approval_codes, approval_text = approve_action(
        rt, action, "hidden-shell", 41, ("shell-a", "shell-b"), ["src/", "tmp/"], [])
    effect_id = str(receipt(allowed).get("effect_id", ""))
    claim1, claim1_code, _ = claim_effects(rt, "shell-consumer-a", "clm_shell_auth_01")
    post = rt.invoke(post_for(action, {"status": "ok", "secret": SECRET_LITERALS[0]}))
    _stale, stale_code, stale_text = ack_effect(rt, effect_id, claim1, 1, "dlv_shell_stale_01")
    claim2, claim2_code, _ = claim_effects(rt, "shell-consumer-b", "clm_shell_complete_02")
    _nack, nack_code, nack_text = nack_effect(rt, effect_id, claim2, 2, "rty_shell_complete_01")
    claim3, claim3_code, _ = claim_effects(rt, "shell-consumer-a", "clm_shell_retry_03")
    _ack, ack_code, ack_text = ack_effect(rt, effect_id, claim3, 2, "dlv_shell_complete_02")
    _ack_replay, ack_replay_code, ack_replay_text = ack_effect(
        rt, effect_id, claim3, 2, "dlv_shell_complete_02")

    held = json.loads(json.dumps(events[4])); held["session_id"] = "shell7-held"; held["tool_use_id"] = "shell7-held"
    held_pending = rt.invoke(held); held_ticket = str(receipt(held_pending).get("approval_ticket_id", ""))
    for index, token in enumerate(tokens):
        cast_approval(rt, held_ticket, f"shell7-held-{index}", token)
    held_allowed = rt.invoke(held)
    v1 = (rt.case_dir / "assets" / "policy.json").read_bytes()
    v2 = (rt.case_dir / "assets" / "policy_v2.json").read_bytes()
    callback_values: list[Any] = []
    def concurrent_change() -> None:
        atomic_policy(rt.workspace / ".claude" / "policy-provenance.json", v2, "epoch42")
        probe = json.loads(json.dumps(events[2])); probe["tool_use_id"] = "shell7-epoch42"
        callback_values.append(rt.invoke(probe)); callback_values.append(rt.invoke(post_for(held)))
    upgrade_secret = "EVAL_UPGRADE_SECRET_7a11-shell"
    upgraded = drive_upgrade(rt, "upg_shell_cycle7_01", upgrade_secret, concurrent_change)
    downgrade_request = {"upgrade_id": "upg_shell_downgrade", "target_format_version": 1,
        "expected_upgrade_lineage_digest": upgraded.get("commit", {}).get("upgrade_lineage_digest"),
        "upgrade_secret": upgrade_secret}
    _down, down_text, down_code = rt.maintain("--begin-upgrade", downgrade_request)
    _sibling, sibling_code, sibling_text = begin_upgrade(rt, "upg_shell_sibling_02", upgrade_secret)
    atomic_policy(rt.workspace / ".claude" / "policy-provenance.json", v1, "rollback41")
    rollback = json.loads(json.dumps(events[4])); rollback["session_id"] = "shell7-rollback"; rollback["tool_use_id"] = "shell7-rollback"
    rollback_response = rt.invoke(rollback)
    effects, effect_text, effect_code = rt.inspect("--effects", "--limit", "20")
    upgrades, upgrade_text, upgrade_code = rt.inspect("--upgrades", "--limit", "20")
    approvals, _, approval_code = rt.inspect("--approvals", "--limit", "20")
    leases, _, lease_code = rt.inspect("--leases", "--limit", "20")
    audit, audit_text, audit_code = rt.inspect()
    observed = json.dumps([*denied, pending, allowed, post, held_pending, held_allowed,
                           *callback_values, rollback_response], sort_keys=True)
    observed += rt.raw_state() + approval_text + stale_text + nack_text + ack_text + ack_replay_text
    observed += upgraded["text"] + down_text + sibling_text + effect_text + upgrade_text + audit_text
    token_values = [str(item.get("token", "")) for item in tokens if item.get("token")]
    claim_tokens = [str(item.get("claim_token", "")) for item in (claim1, claim2, claim3)
                    if item.get("claim_token")]
    leak = (any(secret in observed for secret in SECRET_LITERALS) or upgrade_secret in observed
            or any(token in observed for token in token_values)
            or any(token in observed for token in claim_tokens))
    format2 = valid_upgrade_commit(upgraded["commit"]) and upgraded["replay"] == upgraded["commit"]
    assertions = [
        check("SHELL7.DENY_NO_EFFECT", 10, all(permission(item) == "deny" and receipt(item).get("effect_id") is None for item in denied), "recursive denials create no effect"),
        check("SHELL7.EFFECT_GENERATION_STALE_CLAIM", 15, permission(allowed) == "allow" and receipt(allowed).get("effect_generation") == 1 and claim1_code == 0 and valid_effect_claim(claim1) and receipt(post).get("effect_id") == effect_id and receipt(post).get("effect_generation") == 2 and stale_code != 0, "Post advances same effect and invalidates claim"),
        check("SHELL7.NACK_RECLAIM_ACK", 10, claim2_code == 0 and nack_code == 0 and claim3_code == 0 and ack_code == 0 and ack_replay_code == 0, "nack, reclaim, and exact ack replay"),
        check("SHELL7.UPGRADE_HOOK_CONTINUITY", 15, upgraded["begin_code"] == 0 and valid_upgrade_manifest(upgraded["manifest"]) and len(upgraded["statuses"]) >= 2 and all(valid_upgrade_status(item) for item in upgraded["statuses"]) and permission(held_allowed) == "allow" and len(callback_values) == 2, "format1 hooks append during bounded copy"),
        check("SHELL7.COMMIT_REPLAY_ANTIROLLBACK", 15, upgraded["stale_code"] != 0 and upgraded["commit_code"] == 0 and upgraded["replay_code"] == 0 and format2 and down_code != 0 and sibling_code != 0, "catch-up commit once; downgrade/sibling reject"),
        check("SHELL7.PINNED_POLICY_EFFECT", 10, all(code == 0 for code in approval_codes) and receipt(allowed).get("policy_epoch") == 41 and callback_values and receipt(callback_values[-1]).get("policy_epoch") == 41 and permission(rollback_response) == "deny", "old lifecycle stays pinned across epoch42"),
        check("SHELL7.REDACTED_EFFECT_UPGRADE_VIEWS", 15, effect_code == 0 and effects and all(valid_effect(item) for item in effects) and upgrade_code == 0 and upgrades and all(valid_upgrade_view(item) for item in upgrades) and approval_code == 0 and all(valid_approval(item) for item in approvals) and lease_code == 0 and all(valid_lease(item) for item in leases) and not leak, f"effects={len(effects)} upgrades={len(upgrades)}"),
        check("SHELL7.CHAINED_FORMAT_EVIDENCE", 10, audit_code == 0 and valid_chain(audit) and any(item.get("kind") == "upgrade_commit" for item in audit) and any(item.get("kind") == "effect_ack" for item in audit), f"events={len(audit)}"),
    ]
    bypass = any(permission(item) == "allow" for item in denied) or stale_code == 0 or down_code == 0 or sibling_code == 0 or permission(rollback_response) == "allow"
    return finish(rt, assertions, [{"id":"CAP.SHELL7_AUTHORITY_OR_STALE_DELIVERY","maximum":10,"triggered":bypass},
                                   {"id":"CAP.SHELL7_SECRET_DISCLOSURE","maximum":10,"triggered":leak}])


def cycle7_scenario_002(rt: Runtime) -> dict[str, Any]:
    rt.policy(); events = rt.assets_json("events.json"); base = events[0]
    duplicates = rt.invoke_many([base] * 8, workers=4); pending = duplicates[0]
    ticket = str(receipt(pending).get("approval_ticket_id", ""))
    tokens: list[dict[str, Any]] = []
    enroll_codes: list[int] = []
    for principal in ("txn-a", "txn-b"):
        token, code, _ = enroll_approver(rt, approver_claims(principal, "hidden-transaction", 3, "Write", ["src/"], []))
        tokens.append(token); enroll_codes.append(code)
        cast_approval(rt, ticket, f"txn7-{principal}", token)
    consumed_many = rt.invoke_many([base] * 4, workers=4); consumed = consumed_many[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims = [future.result(timeout=12) for future in [
            pool.submit(claim_effects, rt, f"txn-consumer-{index}", f"clm_txn_race_{index}") for index in range(2)]]
    claim_winners = [item for item in claims if item[1] == 0]
    winning_claim = claim_winners[0][0] if len(claim_winners) == 1 else {}
    winning_generation = int(receipt(consumed).get("effect_generation") or 1)
    post = rt.invoke(events[2])
    _stale, stale_code, _ = ack_effect(rt, str(receipt(consumed).get("effect_id", "")),
                                       winning_claim, winning_generation, "dlv_txn_stale_01")
    conflict = rt.invoke(events[1])
    waiting = json.loads(json.dumps(base)); waiting["session_id"] = "txn7-wait"; waiting["tool_use_id"] = "txn7-wait"
    waiting["tool_input"] = {"file_path":"src/wait.py","content":"wait"}
    waiting_pending = rt.invoke(waiting); waiting_ticket = str(receipt(waiting_pending).get("approval_ticket_id", ""))
    for index, token in enumerate(tokens): cast_approval(rt, waiting_ticket, f"txn7-wait-{index}", token)
    waiting_ready = rt.invoke(waiting)
    sibling_codes: list[int] = []
    callback_values: list[Any] = []
    def concurrent_change() -> None:
        _sibling, sibling_code, _ = begin_upgrade(rt, "upg_txn_sibling_02", "transaction-upgrade-secret-0000002")
        sibling_codes.append(sibling_code)
        callback_values.append(rt.invoke(waiting)); callback_values.append(rt.invoke(post_for(waiting)))
    upgraded = drive_upgrade(rt, "upg_txn_cycle7_01", "transaction-upgrade-secret-0000002", concurrent_change)
    journal = rt.state / "audit.jsonl"; journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("ab") as stream: stream.write(b'{"schema_version":1,"kind":"upgrade_commit"')
    effects, effect_text, effect_code = rt.inspect("--effects", "--limit", "20")
    upgrades, _, upgrade_code = rt.inspect("--upgrades", "--limit", "20")
    approvals, _, approval_code = rt.inspect("--approvals", "--limit", "20")
    leases, _, lease_code = rt.inspect("--leases", "--limit", "20")
    audit, _, audit_code = rt.inspect()
    duplicate_effects = {receipt(item).get("effect_id") for item in consumed_many}
    assertions = [
        check("TXN7.DUPLICATE_ONE_EFFECT", 10, all(item == pending for item in duplicates) and all(item == consumed for item in consumed_many) and len(duplicate_effects) == 1 and None not in duplicate_effects, "duplicate pending/consume one effect"),
        check("TXN7.EXCLUSIVE_CLAIM_REPLAY", 15, len(claim_winners) == 1 and valid_effect_claim(winning_claim) and claim_effects(rt, str(winning_claim.get("consumer_id", "")), str(winning_claim.get("claim_id", "")))[0] == winning_claim, "one exclusive claim and exact replay"),
        check("TXN7.POST_INVALIDATES_CLAIM", 15, receipt(post).get("effect_id") == receipt(consumed).get("effect_id") and receipt(post).get("effect_generation") == 2 and stale_code != 0 and permission(conflict) == "deny", "Post generation invalidates old claim/conflict"),
        check("TXN7.UPGRADE_CONCURRENT_CATCHUP", 20, upgraded["begin_code"] == 0 and len(upgraded["statuses"]) >= 2 and upgraded["stale_code"] != 0 and callback_values and permission(callback_values[0]) == "allow" and receipt(callback_values[1]).get("effect_generation") == 2, "fresh completed effect caught up"),
        check("TXN7.ONE_LINEAGE_WINNER", 15, bool(sibling_codes) and sibling_codes[0] != 0 and upgraded["commit_code"] == 0 and upgraded["replay_code"] == 0 and upgraded["commit"] == upgraded["replay"] and valid_upgrade_commit(upgraded["commit"]), "one begin/commit lineage"),
        check("TXN7.CONFLICT_QUOTA_PRESERVED", 10, all(code == 0 for code in enroll_codes) and permission(conflict) == "deny" and permission(waiting_ready) == "deny" and receipt(waiting_ready).get("approval_state") == "ready", "conflict and quota preserve current work"),
        check("TXN7.CORRELATED_VIEWS", 10, effect_code == 0 and bool(effects) and all(valid_effect(item) for item in effects) and upgrade_code == 0 and bool(upgrades) and all(valid_upgrade_view(item) for item in upgrades) and approval_code == 0 and bool(approvals) and all(valid_approval(item) for item in approvals) and lease_code == 0 and bool(leases) and all(valid_lease(item) for item in leases), f"effects={len(effects)}"),
        check("TXN7.TORN_TAIL_FORMAT_CHAIN", 5, audit_code == 0 and valid_chain(audit) and upgraded["commit"].get("target_format_version") == 2, f"events={len(audit)}"),
    ]
    duplicate_claim = len(claim_winners) > 1
    bypass = permission(conflict) == "allow" or permission(waiting_ready) == "allow" or duplicate_claim or any(code == 0 for code in sibling_codes)
    return finish(rt, assertions, [{"id":"CAP.TXN7_DUPLICATE_CLAIM_OR_LINEAGE","maximum":10,"triggered":bypass},
                                   {"id":"CAP.TXN7_STALE_EFFECT_ACK","maximum":10,"triggered":stale_code==0}])


def cycle7_scenario_003(rt: Runtime) -> dict[str, Any]:
    rt.policy(); events = [json.loads(json.dumps(item)) for item in rt.assets_json("events.json")]
    root, _, root_code = rt.mint({"delegation_id":"hidden-root","subject":"coordinator",
        "policy_id":"hidden-delegation","policy_epoch":9,"tools":["mcp__files__inspect"],
        "path_prefixes":["src"],"hosts":["example.test"]})
    child_claims = {"delegation_id":"hidden-child","subject":"worker","policy_id":"hidden-delegation",
        "policy_epoch":9,"tools":["mcp__files__inspect"],"path_prefixes":["src"],
        "hosts":["example.test"],"parent_token":root}
    child, _, child_code = rt.mint(child_claims)
    wider = dict(child_claims); wider["delegation_id"]="hidden-wider"; wider["path_prefixes"]=["tmp"]
    _wide, _, wide_code = rt.mint(wider)
    delegated = json.loads(json.dumps(events[0])); delegated["tool_input"]["_policy_delegation"] = child
    pending, allowed, approvers, approval_codes, approval_text = approve_action(
        rt, delegated, "hidden-delegation", 9, ("mcp-a", "mcp-b"), ["src"], ["example.test"])
    denied_events = [json.loads(json.dumps(item)) for item in events[1:5]]
    for item in denied_events: item["tool_input"]["_policy_delegation"] = child
    denied = [rt.invoke(item) for item in denied_events]; missing = rt.invoke(events[0])
    effect_id = str(receipt(allowed).get("effect_id", ""))
    claim1, claim1_code, _ = claim_effects(rt, "mcp-consumer-a", "clm_mcp_auth_01")
    rotated, _, rotate_code = rt.inspect("--rotate-key")
    delegated_post = json.loads(json.dumps(events[5])); delegated_post["tool_input"]["_policy_delegation"] = child
    post = rt.invoke(delegated_post)
    _stale, stale_code, _ = ack_effect(rt, effect_id, claim1, 1, "dlv_mcp_stale_01")
    claim2, claim2_code, _ = claim_effects(rt, "mcp-consumer-b", "clm_mcp_complete_02")
    _nack, nack_code, _ = nack_effect(rt, effect_id, claim2, 2, "rty_mcp_complete_01")
    upgraded = drive_upgrade(rt, "upg_mcp_cycle7_01", "mcp-upgrade-secret-0000000003")
    secret = "EVAL_HANDOFF_SECRET_9c61-mcp7"
    offer, offer_code, offer_text = prepare_handoff(rt, "hof_mcp_cycle7_01", "mcp-recovery", secret)
    _source_claim, source_claim_code, _ = claim_effects(rt, "mcp-source", "clm_mcp_source_03")
    target = rt.root / "mcp7-target"; ack, ack_code, ack_text = accept_handoff(rt, offer, secret, state_dir=target)
    _standby_claim, standby_claim_code, _ = claim_effects(rt, "mcp-standby", "clm_mcp_standby_04", state_dir=target)
    commit, commit_code, commit_text = finalize_handoff(rt, ack, secret)
    _active, active_code, active_text = activate_handoff(rt, commit, secret, state_dir=target)
    claim3, claim3_code, _ = claim_effects(rt, "mcp-target", "clm_mcp_target_05", state_dir=target)
    _ack_effect, effect_ack_code, effect_ack_text = ack_effect(rt, effect_id, claim3, 2, "dlv_mcp_target_02", state_dir=target)
    _revoke, _, revoke_code = rt.inspect("--revoke-delegation", "hidden-root", state_dir=target)
    revoked = json.loads(json.dumps(delegated)); revoked["tool_use_id"] = "mcp7-revoked"
    revoked_response = rt.invoke(revoked, state_dir=target)
    v2 = (rt.case_dir / "assets" / "policy_v2.json").read_bytes()
    atomic_policy(rt.workspace / ".claude" / "policy-provenance.json", v2, "epoch10")
    wrong_epoch = json.loads(json.dumps(delegated)); wrong_epoch["tool_use_id"] = "mcp7-old-epoch"
    wrong_epoch_response = rt.invoke(wrong_epoch, state_dir=target)
    modified = json.loads(json.dumps(offer)) if offer else {}
    if modified: modified["target_instance_id"] = "mcp-spare"
    _bad, bad_code, bad_text = accept_handoff(rt, modified, secret, state_dir=rt.root / "mcp7-bad")
    effects, effect_text, effect_code = rt.inspect("--effects", "--limit", "20", state_dir=target)
    upgrades, upgrade_text, upgrade_code = rt.inspect("--upgrades", "--limit", "20", state_dir=target)
    handoffs, handoff_text, handoff_code = rt.inspect("--handoffs", "--limit", "20", state_dir=target)
    audit, audit_text, audit_code = rt.inspect(state_dir=target)
    observed = json.dumps([pending, allowed, *denied, missing, post, revoked_response, wrong_epoch_response], sort_keys=True)
    observed += rt.raw_state() + rt.raw_state(state_dir=target) + approval_text + upgraded["text"]
    observed += offer_text + ack_text + commit_text + active_text + effect_ack_text + bad_text + effect_text + upgrade_text + handoff_text + audit_text
    tokens = [str(item.get("token", "")) for item in (root, child, *approvers) if item.get("token")]
    leak = any(item in observed for item in SECRET_LITERALS) or secret in observed or any(token in observed for token in tokens)
    assertions = [
        check("MCP7.DELEGATION_ATTENUATION",10,root_code==0 and child_code==0 and bool(child) and wide_code!=0,"attenuated delegation"),
        check("MCP7.DENIALS_NO_EFFECT",10,all(permission(item)=="deny" and receipt(item).get("effect_id") is None for item in [*denied,missing]),"selector/name/token denials"),
        check("MCP7.REDACTED_CLAIM_GENERATIONS",15,permission(allowed)=="allow" and claim1_code==0 and receipt(post).get("effect_generation")==2 and stale_code!=0 and claim2_code==0,"authorization/completion claim correlation"),
        check("MCP7.NACK_RETRY_DELIVERY",10,nack_code==0 and claim3_code==0 and effect_ack_code==0,"nack survives upgrade/handoff and target acks"),
        check("MCP7.ROTATION_FORMAT_COMMIT",15,all(code==0 for code in approval_codes) and rotate_code==0 and rotated and upgraded["commit_code"]==0 and valid_upgrade_commit(upgraded["commit"]),"rotation then format2"),
        check("MCP7.EFFECT_HANDOFF_BOUNDARY",15,offer_code==0 and ack_code==0 and commit_code==0 and active_code==0 and source_claim_code!=0 and standby_claim_code!=0 and claim3_code==0,"only activated target claims"),
        check("MCP7.REVOCATION_FORGERY_CLOSED",15,revoke_code==0 and permission(revoked_response)=="deny" and permission(wrong_epoch_response)=="deny" and stale_code!=0 and bad_code!=0,"revocation/epoch/forgery/stale claim"),
        check("MCP7.ALL_PLANE_VIEWS_CHAIN",10,effect_code==0 and effects and all(valid_effect(item) for item in effects) and upgrade_code==0 and all(valid_upgrade_view(item) for item in upgrades) and handoff_code==0 and all(valid_handoff_view(item) for item in handoffs) and audit_code==0 and valid_chain(audit) and not leak,f"effects={len(effects)} events={len(audit)}"),
    ]
    forbidden = any(permission(item)=="allow" for item in [*denied,missing,revoked_response,wrong_epoch_response]) or source_claim_code==0 or standby_claim_code==0 or bad_code==0
    return finish(rt, assertions, [{"id":"CAP.MCP7_FORBIDDEN_EFFECT_AUTHORITY","maximum":10,"triggered":forbidden},
                                   {"id":"CAP.MCP7_SECRET_DISCLOSURE","maximum":10,"triggered":leak}])


def cycle7_scenario_004(rt: Runtime) -> dict[str, Any]:
    events=rt.assets_json("events.json"); v1=(rt.case_dir/"assets"/"policy_v1.json").read_bytes()
    v2=(rt.case_dir/"assets"/"policy_v2.json").read_bytes(); v3=(rt.case_dir/"assets"/"policy_v3.json").read_bytes()
    fork=(rt.case_dir/"assets"/"policy_fork.json").read_bytes(); policy_path=rt.workspace/".claude"/"policy-provenance.json"
    policy_path.write_bytes(v1)
    pending, allowed, tokens, codes, _ = approve_action(rt, events[0], "hidden-epoch", 70, ("epoch-a","epoch-b"), ["src"], [])
    old_effect=str(receipt(allowed).get("effect_id","")); old_claim, old_claim_code, _=claim_effects(rt,"epoch-old","clm_epoch_old_01")
    atomic_policy(policy_path,v2,"epoch71"); transition=dict(events[0]); transition["tool_use_id"]="epoch7-transition"; transition_response=rt.invoke(transition)
    read_event=dict(events[3]); read_event["session_id"]="epoch7-read"; read_event["tool_use_id"]="epoch7-read"
    callback_values=[]
    def concurrent_change() -> None:
        callback_values.append(rt.invoke(events[1])); callback_values.append(rt.invoke(read_event)); callback_values.append(rt.invoke(post_for(read_event)))
    upgraded=drive_upgrade(rt,"upg_epoch_cycle7_01","epoch-upgrade-secret-00000004",concurrent_change)
    _stale, stale_claim_code, _=ack_effect(rt,old_effect,old_claim,1,"dlv_epoch_stale_01")
    atomic_policy(policy_path,v3,"epoch72"); epoch72=dict(events[3]); epoch72["session_id"]="epoch7-72"; epoch72["tool_use_id"]="epoch7-72"
    epoch72_response=rt.invoke(epoch72); epoch72_post=rt.invoke(post_for(epoch72))
    checkpoint_secret="epoch7-checkpoint-secret-000000004"
    checkpoint, checkpoint_code, _=export_checkpoint(rt,"mig_epoch7_format2",checkpoint_secret)
    archive=rt.root/"epoch7-archive"; _imported, import_code, _=import_checkpoint(rt,checkpoint,checkpoint_secret,"replace",None,state_dir=archive)
    before,_ ,_=rt.inspect(state_dir=archive); _replay,replay_code,_=import_checkpoint(rt,checkpoint,checkpoint_secret,"replace",None,state_dir=archive); after,_,_=rt.inspect(state_dir=archive)
    _archive_claim, archive_claim_code, _=claim_effects(rt,"archive","clm_epoch_archive_02",state_dir=archive)
    _archive_upgrade, archive_upgrade_code, _=begin_upgrade(rt,"upg_epoch_archive_02","epoch-upgrade-secret-00000004",state_dir=archive)
    atomic_policy(policy_path,fork,"fork71"); fork_event=dict(events[0]); fork_event["tool_use_id"]="epoch7-fork"; fork_response=rt.invoke(fork_event)
    atomic_policy(policy_path,v1,"rollback70"); rollback_event=dict(events[0]); rollback_event["tool_use_id"]="epoch7-rollback"; rollback=rt.invoke(rollback_event)
    downgrade={"upgrade_id":"upg_epoch_downgrade","target_format_version":1,"expected_upgrade_lineage_digest":upgraded["commit"].get("upgrade_lineage_digest"),"upgrade_secret":"epoch-upgrade-secret-00000004"}
    _down,_,down_code=rt.maintain("--begin-upgrade",downgrade)
    effects,_,effect_code=rt.inspect("--effects","--limit","20"); upgrades,_,upgrade_code=rt.inspect("--upgrades","--limit","20")
    checkpoints,_,checkpoint_view_code=rt.inspect("--checkpoints","--limit","20",state_dir=archive); audit,_,audit_code=rt.inspect()
    assertions=[
        check("EPOCH7.PINNED_EFFECT_SUCCESSOR",15,all(code==0 for code in codes) and permission(transition_response)=="deny" and receipt(transition_response).get("policy_epoch")==71 and callback_values and receipt(callback_values[0]).get("effect_id")==old_effect and receipt(callback_values[0]).get("policy_epoch")==70,"old Post completes after epoch71"),
        check("EPOCH7.UPGRADE_MIXED_SUFFIX_CATCHUP",15,upgraded["stale_code"]!=0 and len(upgraded["statuses"])>=2 and len(callback_values)==3 and receipt(callback_values[-1]).get("effect_generation")==2,"old/current effect suffixes caught up"),
        check("EPOCH7.FORMAT2_THEN_EPOCH72",15,upgraded["commit_code"]==0 and valid_upgrade_commit(upgraded["commit"]) and permission(epoch72_response)=="allow" and receipt(epoch72_response).get("policy_epoch")==72 and receipt(epoch72_response).get("state_format_version")==2 and same_lifecycle(epoch72_response,epoch72_post),"format2 precedes epoch72"),
        check("EPOCH7.CHECKPOINT_FORMAT_EFFECT_REPLAY",15,checkpoint_code==0 and valid_checkpoint(checkpoint) and checkpoint.get("state_format_version")==2 and import_code==0 and replay_code==0 and len(before)==len(after),"format/effect upgrade checkpoint replay"),
        check("EPOCH7.ARCHIVE_CLAIM_FENCE",10,archive_claim_code!=0 and archive_upgrade_code!=0,"checkpoint-only archive has no delivery/upgrade authority"),
        check("EPOCH7.DUAL_ANTIROLLBACK",15,old_claim_code==0 and stale_claim_code!=0 and permission(fork_response)=="deny" and permission(rollback)=="deny" and down_code!=0,"policy/format/stale claim rollback closed"),
        check("EPOCH7.COHERENT_LINEAGE_VIEWS",10,effect_code==0 and all(valid_effect(item) for item in effects) and upgrade_code==0 and all(valid_upgrade_view(item) for item in upgrades) and checkpoint_view_code==0 and checkpoints and all(valid_checkpoint_view(item) for item in checkpoints),"dual lineage views"),
        check("EPOCH7.CHAINED_DUAL_LINEAGE",5,audit_code==0 and valid_chain(audit) and any(item.get("kind")=="upgrade_commit" for item in audit),f"events={len(audit)}"),
    ]
    stale=permission(fork_response)=="allow" or permission(rollback)=="allow" or down_code==0 or archive_claim_code==0 or archive_upgrade_code==0
    return finish(rt,assertions,[{"id":"CAP.EPOCH7_STALE_POLICY_FORMAT_OR_ARCHIVE","maximum":10,"triggered":stale},
                                 {"id":"CAP.EPOCH7_MULTIPLE_LINEAGE_WINNERS","maximum":10,"triggered":False}])


def cycle7_scenario_005(rt: Runtime) -> dict[str, Any]:
    rt.policy(); rt.compact_after="16"; template=rt.assets_json("events.json")[0]
    first=json.loads(json.dumps(template)); first["session_id"]="maint7-first"; first["tool_use_id"]="maint7-first"
    pending, active, tokens, codes, _=approve_action(rt,first,"hidden-maintenance",5,("maint-a","maint-b"),["src/"],[])
    effect_id=str(receipt(active).get("effect_id","")); claim1,claim1_code,_=claim_effects(rt,"maint-crash","clm_maint_crash_01")
    secret="EVAL_UPGRADE_SECRET_7a11-maint"; manifest,begin_code,_=begin_upgrade(rt,"upg_maint_cycle7_01",secret)
    payload=json.dumps({"manifest":manifest,"upgrade_secret":secret,"expected_cursor_sequence":0})
    crash=rt.crash_partial_maintenance("--advance-upgrade",payload[:max(1,len(payload)//2)],state_dir=rt.state)
    before,_,before_code=rt.inspect("--upgrades","--limit","20")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        compact=pool.submit(rt.inspect,"--compact").result(timeout=12)
        rotated=pool.submit(rt.inspect,"--rotate-key").result(timeout=12)
    time.sleep(2.2)
    claim2,claim2_code,_=claim_effects(rt,"maint-recover","clm_maint_recover_02")
    _nack,nack_code,_=nack_effect(rt,effect_id,claim2,1,"rty_maint_recover_01")
    claim3,claim3_code,_=claim_effects(rt,"maint-retry","clm_maint_retry_03")
    _ack1,ack1_code,_=ack_effect(rt,effect_id,claim3,1,"dlv_maint_auth_01")
    callback_values=[]
    def concurrent_change() -> None: callback_values.append(rt.invoke(post_for(first)))
    upgraded=drive_upgrade(rt,"upg_maint_cycle7_01",secret,concurrent_change)
    _stale,stale_code,_=ack_effect(rt,effect_id,claim3,1,"dlv_maint_stale_02")
    claim4,claim4_code,_=claim_effects(rt,"maint-complete","clm_maint_complete_04")
    _ack2,ack2_code,_=ack_effect(rt,effect_id,claim4,2,"dlv_maint_complete_02")
    fresh=json.loads(json.dumps(template)); fresh["session_id"]="maint7-fresh"; fresh["tool_use_id"]="maint7-fresh"
    fresh_pending=rt.invoke(fresh); fresh_ticket=str(receipt(fresh_pending).get("approval_ticket_id",""))
    for index,token in enumerate(tokens): cast_approval(rt,fresh_ticket,f"maint7-fresh-{index}",token)
    fresh_active=rt.invoke(fresh); fresh_post=rt.invoke(post_for(fresh))
    journal=rt.state/"audit.jsonl"; journal.parent.mkdir(parents=True,exist_ok=True)
    with journal.open("ab") as stream: stream.write(b'{"schema_version":1,"kind":"effect_ack"')
    audit,audit_text,audit_code=rt.inspect(); effects,effect_text,effect_code=rt.inspect("--effects","--limit","20")
    upgrades,upgrade_text,upgrade_code=rt.inspect("--upgrades","--limit","20"); approvals,_,approval_code=rt.inspect("--approvals","--limit","20"); leases,_,lease_code=rt.inspect("--leases","--limit","20")
    observed=rt.raw_state()+audit_text+effect_text+upgrade_text+upgraded["text"]+str(crash)
    tokens_text=[str(item.get("token","")) for item in tokens if item.get("token")]
    leak=any(secret_value in observed for secret_value in SECRET_LITERALS) or secret in observed or any(item in observed for item in tokens_text)
    cursor_before=max([int(item.get("cursor_sequence",0)) for item in before],default=0)
    assertions=[
        check("MAINT7.COMPACT_ROTATE_BUILDING",10,all(code==0 for code in codes) and begin_code==0 and compact[2]==0 and rotated[2]==0 and permission(active)=="allow","live state compact/rotate during building"),
        check("MAINT7.PARTIAL_ADVANCE_ATOMIC",20,crash.get("exit_code",0)!=0 and before_code==0 and cursor_before==0 and upgraded["statuses"] and all(valid_upgrade_status(item) for item in upgraded["statuses"]),"partial request has no progress; restart advances"),
        check("MAINT7.CATCHUP_COMMIT_RECOVERY",15,callback_values and receipt(callback_values[0]).get("effect_generation")==2 and upgraded["stale_code"]!=0 and upgraded["commit_code"]==0 and upgraded["replay"]==upgraded["commit"],"Post catch-up and exact commit replay"),
        check("MAINT7.CLAIM_EXPIRY_RECLAIM",15,claim1_code==0 and claim2_code==0 and claim1.get("items",[{}])[0].get("effect_id")==claim2.get("items",[{}])[0].get("effect_id"),"expired consumer claim reclaims same generation"),
        check("MAINT7.NACK_AND_POST_INVALIDATION",15,nack_code==0 and claim3_code==0 and ack1_code==0 and stale_code!=0 and claim4_code==0 and ack2_code==0,"nack retry then Post invalidates generation1"),
        check("MAINT7.FRESH_WORK_AFTER_RECOVERY",10,permission(fresh_active)=="allow" and receipt(fresh_active).get("state_format_version")==2 and same_lifecycle(fresh_active,fresh_post),"fresh format2 approved work"),
        check("MAINT7.TORN_TAIL_BOUNDED_VIEWS",10,audit_code==0 and valid_chain(audit) and effect_code==0 and all(valid_effect(item) for item in effects) and upgrade_code==0 and all(valid_upgrade_view(item) for item in upgrades) and approval_code==0 and all(valid_approval(item) for item in approvals) and lease_code==0 and all(valid_lease(item) for item in leases),f"events={len(audit)} effects={len(effects)}"),
        check("MAINT7.ALL_PLANE_REDACTION",5,not leak,f"observed bytes={len(observed)}"),
    ]
    partial_authority=cursor_before!=0 or stale_code==0
    return finish(rt,assertions,[{"id":"CAP.MAINT7_PARTIAL_STAGE_OR_STALE_ACK","maximum":10,"triggered":partial_authority},
                                 {"id":"CAP.MAINT7_SECRET_DISCLOSURE","maximum":10,"triggered":leak}])


def cycle7_scenario_006(rt: Runtime) -> dict[str, Any]:
    events=rt.assets_json("events.json"); missing=[rt.invoke(item) for item in events[:3]]
    rt.policy("policy_enforce.json"); enforced=[rt.invoke(item) for item in events[3:5]]
    rt.policy("policy_dry_run.json"); dry=rt.invoke(events[5]); rt.policy("policy_audit.json"); audit_mode=rt.invoke(events[6])
    rt.policy("policy_reserved.json")
    pending,allowed,tokens,codes,_=approve_action(rt,events[10],"compat-reserved",1,("compat-a","compat-b"),["src/"],[])
    post=rt.invoke(events[12]); effect_id=str(receipt(allowed).get("effect_id",""))
    claim,claim_code,_=claim_effects(rt,"compat-consumer","clm_compat_complete_01")
    _ack,ack_code,_=ack_effect(rt,effect_id,claim,2,"dlv_compat_complete_01")
    _ack_replay,ack_replay_code,_=ack_effect(rt,effect_id,claim,2,"dlv_compat_complete_01")
    reused_event=json.loads(json.dumps(events[11])); reused_event["session_id"]="compat7-reuse"; reused_event["tool_use_id"]="compat7-reuse"
    reused_pending=rt.invoke(reused_event)
    reused_effect=str(receipt(reused_pending).get("effect_id",""))
    _reuse,reuse_code,_=ack_effect(rt,reused_effect,claim,2,"dlv_compat_complete_01")
    upgraded=drive_upgrade(rt,"upg_compat_cycle7_01","compat-upgrade-secret-000000006")
    checkpoint_secret="compat7-checkpoint-secret-00000006"
    checkpoint,checkpoint_code,_=export_checkpoint(rt,"mig_compat7_format2",checkpoint_secret)
    archive=rt.root/"compat7-archive"; _imported,import_code,_=import_checkpoint(rt,checkpoint,checkpoint_secret,"replace",None,state_dir=archive)
    _replayed,replay_code,_=import_checkpoint(rt,checkpoint,checkpoint_secret,"replace",None,state_dir=archive)
    _archive_claim,archive_claim_code,_=claim_effects(rt,"compat-archive","clm_compat_archive_02",state_dir=archive)
    invalid=(rt.case_dir/"assets"/"policy_invalid_delivery.json").read_bytes(); (rt.workspace/".claude"/"policy-provenance.json").write_bytes(invalid)
    invalid_mutate=rt.invoke(events[7]); invalid_read=rt.invoke(events[8]); malformed=rt.invoke(events[9])
    effects,_,effect_code=rt.inspect("--effects","--limit","20"); upgrades,_,upgrade_code=rt.inspect("--upgrades","--limit","20")
    checkpoints,_,checkpoint_view_code=rt.inspect("--checkpoints","--limit","20",state_dir=archive); approvals,_,approval_code=rt.inspect("--approvals","--limit","20"); leases,_,lease_code=rt.inspect("--leases","--limit","20"); audit,_,audit_code=rt.inspect()
    legacy=[*missing,*enforced,dry,audit_mode]
    legacy_events=[*events[:7]]
    assertions=[
        check("COMPAT7.LEGACY_UNIVERSAL_NO_DELIVERY",10,all(valid_receipt(response,event) for response,event in zip(legacy,legacy_events)) and all(receipt(item).get("state_format_version")==1 and receipt(item).get("effect_state")=="not_configured" for item in legacy),"missing/schema1 universal defaults"),
        check("COMPAT7.MANAGED_EFFECT_OPT_IN",15,all(code==0 for code in codes) and permission(allowed)=="allow" and receipt(allowed).get("effect_generation")==1 and receipt(post).get("effect_generation")==2 and same_lifecycle(allowed,post),"managed v2 effect lifecycle"),
        check("COMPAT7.CLAIM_ACK_REPLAY",15,claim_code==0 and valid_effect_claim(claim) and ack_code==0 and ack_replay_code==0 and reuse_code!=0,"exact ack replay and delivery ID non-reuse"),
        check("COMPAT7.BOUNDED_FORMAT_MIGRATION",20,upgraded["begin_code"]==0 and upgraded["statuses"] and upgraded["commit_code"]==0 and upgraded["replay_code"]==0 and upgraded["commit"]==upgraded["replay"] and valid_upgrade_commit(upgraded["commit"]),"bounded format1 to format2"),
        check("COMPAT7.RESTART_CHECKPOINT_FORMAT",10,checkpoint_code==0 and valid_checkpoint(checkpoint) and checkpoint.get("state_format_version")==2 and import_code==0 and replay_code==0 and archive_claim_code!=0,"format2 checkpoint-only archive"),
        check("COMPAT7.INVALID_DELIVERY_NO_STATE",15,permission(invalid_mutate)=="deny" and permission(invalid_read)=="allow" and receipt(invalid_mutate).get("effect_id") is None and receipt(invalid_mutate).get("effect_state")=="not_created" and receipt(invalid_mutate).get("upgrade_state")=="stable","invalid delivery safe Read only"),
        check("COMPAT7.ALL_BOUNDED_SCHEMAS",10,permission(enforced[1])=="deny" and permission(dry)=="allow" and permission(audit_mode)=="allow" and permission(malformed)=="deny" and effect_code==0 and all(valid_effect(item) for item in effects) and upgrade_code==0 and all(valid_upgrade_view(item) for item in upgrades) and checkpoint_view_code==0 and all(valid_checkpoint_view(item) for item in checkpoints) and approval_code==0 and all(valid_approval(item) for item in approvals) and lease_code==0 and all(valid_lease(item) for item in leases),"legacy and all new bounded schemas"),
        check("COMPAT7.CHAIN_EXISTING_PLUGIN",5,audit_code==0 and valid_chain(audit),f"existing hashes passed; events={len(audit)}"),
    ]
    forbidden=permission(invalid_mutate)=="allow" or archive_claim_code==0 or reuse_code==0
    leak=bool(rt.disclosed_literals)
    return finish(rt,assertions,[{"id":"CAP.COMPAT7_INVALID_OR_ARCHIVE_AUTHORITY","maximum":10,"triggered":forbidden},
                                 {"id":"CAP.COMPAT7_SECRET_DISCLOSURE","maximum":10,"triggered":leak}])


def cycle8_scenario_001(rt: Runtime) -> dict[str, Any]:
    integrity = integrity_probe(rt)
    rt.policy(); event = rt.assets_json("events.json")[0]
    result = cycle8_common(rt, event, "hidden-shell", 41,
        ("shell-a", "shell-b"), ("shell-repair-a", "shell-repair-b"),
        "shell8", ["src/", "tmp/"], [])
    repaired = result["repaired"]
    continuation_ok = (result["issue_code"] == 0 and valid_continuation_token(result["token"])
        and result["replay_code"] == 0 and result["issue_replay"] == result["token"]
        and permission(result["blocked"]) == "deny" and same_lifecycle(result["active"], result["source_post"]))
    activated = result["activated"]
    activation_ok = (len(activated) == 2 and activated[0] == activated[1]
        and receipt(result["target"]).get("session_authority_id") == result["authority"]
        and receipt(result["target"]).get("session_generation") == result["generation"] + 1
        and permission(result["wrong"]) == "deny" and permission(result["reused"]) == "deny")
    quarantine_ok = (result["damaged"] and permission(result["quarantined"]) == "deny"
        and receipt(result["quarantined"]).get("integrity_state") == "quarantined"
        and result["quarantine_claim_code"] != 0)
    attest_ok = (repaired["begin_code"] == 0 and valid_repair_manifest(repaired["manifest"])
        and len(repaired["statuses"]) >= 2 and all(valid_repair_status(item) for item in repaired["statuses"])
        and all(code == 0 for code in repaired["verify_codes"])
        and all(valid_repair_attestation(item) for item in repaired["attestations"])
        and repaired["attestation_replay_code"] == 0
        and repaired["attestation_replay"] == repaired["attestations"][0]
        and repaired["bad_verifier_code"] != 0)
    commit_ok = (repaired["commit_code"] == 0 and repaired["commit_replay_code"] == 0
        and valid_repair_commit(repaired["commit"]) and repaired["commit"] == repaired["commit_replay"]
        and permission(result["stale"]) == "deny")
    views_ok = (result["continuation_code"] == 0 and result["continuations"]
        and all(valid_continuation_view(item) for item in result["continuations"])
        and repaired["view_code"] == 0 and repaired["repairs"]
        and all(valid_repair_view(item) for item in repaired["repairs"]) and not result["leak"])
    assertions = [
        check("SHELL8.CONTINUATION_ISSUE_FENCE",15,continuation_ok,"issue replay, source fence, pinned Post"),
        check("SHELL8.CONTINUATION_ACTIVATE_ONCE",15,activation_ok,"one target generation; wrong/reuse denied"),
        check("SHELL8.DAMAGE_QUARANTINES_AUTHORITY",15,quarantine_ok,"interior damage fences hook and claim"),
        check("SHELL8.BOUNDED_REPAIR_ATTEST",20,attest_ok,"bounded statuses and two independent attestations"),
        check("SHELL8.REPAIR_COMMIT_INVALIDATES_SUFFIX",10,commit_ok,"repair commit/replay and stale token invalidation"),
        check("SHELL8.POLICY_EFFECT_COMPATIBILITY",5,all(code==0 for code in result["approval_codes"]) and same_lifecycle(result["target"],result["target_post"]),"approval/lease/effect lifecycle correlation"),
        check("SHELL8.EXACT_REPAIR_CONTINUATION_VIEWS",5,views_ok,f"repairs={len(repaired['repairs'])} continuations={len(result['continuations'])}"),
        check("SHELL8.REPAIRED_CHAIN_FRESH_WORK",5,permission(result["fresh"])=="allow" and receipt(result["fresh"]).get("repair_generation")==1 and result["audit_code"]==0 and valid_chain(result["audit"]),f"events={len(result['audit'])}"),
        check("SHELL9.INTEGRITY_SEAL_AND_PATH_FENCE",10,integrity["passed"],"seal, protected paths, persistent tamper quarantine"),
    ]
    bypass = (permission(result["blocked"])=="allow" or permission(result["wrong"])=="allow"
              or permission(result["reused"])=="allow" or result["quarantine_claim_code"]==0
              or permission(result["stale"])=="allow")
    return finish(rt, assertions, [{"id":"CAP.SHELL8_SOURCE_SUFFIX_OR_TOKEN_AUTHORITY","maximum":10,"triggered":bypass},
                                   {"id":"CAP.SHELL8_REPAIR_CONTINUATION_SECRET","maximum":10,"triggered":result["leak"]},
                                   {"id":"CAP.SHELL9_INTEGRITY_BYPASS","maximum":10,"triggered":integrity["bypass"]}])


def cycle8_scenario_002(rt: Runtime) -> dict[str, Any]:
    integrity = integrity_probe(rt)
    rt.policy(); event = rt.assets_json("events.json")[0]
    result = cycle8_common(rt, event, "hidden-transaction", 3,
        ("txn-a", "txn-b"), ("txn-repair-a", "txn-repair-b"),
        "txn8", ["src/"], [], issue_race=True, crash_repair=True)
    repaired = result["repaired"]; activated = result["activated"]
    issue_ok = len(result["issue_winners"]) == 1 and valid_continuation_token(result["token"])
    activation_ok = (len(activated)==2 and activated[0]==activated[1]
        and receipt(result["target"]).get("session_generation")==result["generation"]+1
        and len({receipt(item).get("receipt_id") for item in activated})==1)
    begin_ok = result["damaged"] and permission(result["quarantined"])=="deny" and repaired["begin_code"]==0
    partial_ok = (isinstance(repaired["crash"],dict) and repaired["crash"].get("exit_code")!=0
        and repaired["statuses"] and all(valid_repair_status(item) for item in repaired["statuses"]))
    cas_ok = (all(code==0 for code in repaired["verify_codes"]) and repaired["bad_verifier_code"]!=0
        and repaired["commit_code"]==0 and repaired["commit_replay_code"]==0
        and repaired["commit"]==repaired["commit_replay"] and valid_repair_commit(repaired["commit"]))
    views = (result["continuation_code"]==0 and bool(result["continuations"])
        and all(valid_continuation_view(item) for item in result["continuations"])
        and repaired["view_code"]==0 and bool(repaired["repairs"])
        and all(valid_repair_view(item) for item in repaired["repairs"])
        and result["effect_code"]==0 and bool(result["effects"])
        and all(valid_effect(item) for item in result["effects"])
        and result["approval_view_code"]==0 and bool(result["approvals"])
        and all(valid_approval(item) for item in result["approvals"])
        and result["lease_code"]==0 and bool(result["leases"])
        and all(valid_lease(item) for item in result["leases"]))
    assertions = [
        check("TXN8.ONE_PENDING_CONTINUATION",15,issue_ok,"capacity-one concurrent issue has one winner"),
        check("TXN8.DUPLICATE_ACTIVATION_GENERATION",15,activation_ok,"duplicate target activation one generation/receipt"),
        check("TXN8.REPAIR_BEGIN_RACE_QUARANTINE",10,begin_ok,"damage quarantine and repair generation"),
        check("TXN8.PARTIAL_ADVANCE_ATOMIC",10,partial_ok,"partial repair death then monotonic statuses"),
        check("TXN8.VERIFIER_AND_COMMIT_CAS",15,cas_ok,"verifier coverage and one commit lineage"),
        check("TXN8.CONFLICT_QUOTA_EFFECT_PRESERVED",10,all(code==0 for code in result["approval_codes"]) and same_lifecycle(result["active"],result["source_post"]),"one-slot approved effect stays exactly once"),
        check("TXN8.CORRELATED_MACHINE_VIEWS",10,views,f"continuations={len(result['continuations'])} repairs={len(repaired['repairs'])}"),
        check("TXN8.TORN_TAIL_FRESH_RESTART",5,permission(result["fresh"])=="allow" and result["audit_code"]==0 and valid_chain(result["audit"]),f"events={len(result['audit'])}"),
        check("TXN9.INTEGRITY_SEAL_AND_PATH_FENCE",10,integrity["passed"],"seal, protected paths, persistent tamper quarantine"),
    ]
    multiple = len(result["issue_winners"])>1 or (len(activated)==2 and activated[0]!=activated[1]) or result["quarantine_claim_code"]==0
    forged = repaired["bad_verifier_code"]==0 or permission(result["stale"])=="allow" or permission(result["wrong"])=="allow"
    return finish(rt,assertions,[{"id":"CAP.TXN8_MULTIPLE_SESSION_OR_REPAIR_WINNERS","maximum":10,"triggered":multiple},
                                 {"id":"CAP.TXN8_STALE_OR_FORGED_AUTHORITY","maximum":10,"triggered":forged},
                                 {"id":"CAP.TXN9_INTEGRITY_BYPASS","maximum":10,"triggered":integrity["bypass"]}])


def cycle8_scenario_003(rt: Runtime) -> dict[str, Any]:
    integrity = integrity_probe(rt)
    rt.policy(); events=rt.assets_json("events.json")
    root,_,root_code=rt.mint({"delegation_id":"cycle8-root","subject":"coordinator",
        "policy_id":"hidden-delegation","policy_epoch":9,"tools":["mcp__files__inspect"],
        "path_prefixes":["src"],"hosts":["example.test"]})
    child_claims={"delegation_id":"cycle8-child","subject":"worker","policy_id":"hidden-delegation",
        "policy_epoch":9,"tools":["mcp__files__inspect"],"path_prefixes":["src"],
        "hosts":["example.test"],"parent_token":root}
    child,_,child_code=rt.mint(child_claims); wider=dict(child_claims); wider["delegation_id"]="cycle8-wide"; wider["path_prefixes"]=["tmp"]
    _wide,_,wide_code=rt.mint(wider); rotated,_,rotate_code=rt.inspect("--rotate-key")
    event=json.loads(json.dumps(events[0])); event["tool_input"]["_policy_delegation"]=child
    result=cycle8_common(rt,event,"hidden-delegation",9,("mcp-a","mcp-b"),
        ("mcp-repair-a","mcp-repair-b"),"mcp8",["src"],["example.test"])
    repaired=result["repaired"]
    _revoke,_,revoke_code=rt.inspect("--revoke-delegation","cycle8-root")
    revoked=json.loads(json.dumps(event)); revoked["session_id"]="mcp8-target"; revoked["tool_use_id"]="mcp8-revoked"
    revoked_response=rt.invoke(revoked)
    handoff_secret="EVAL_HANDOFF_SECRET_9c61-mcp8"; target_state=rt.root/"mcp8-owner-target"
    offer,offer_code,_=prepare_handoff(rt,"hof_mcp_cycle8","mcp-recovery",handoff_secret)
    ack,ack_code,_=accept_handoff(rt,offer,handoff_secret,state_dir=target_state)
    generation=int(receipt(result["fresh"]).get("session_generation") or 1)
    _standby_issue,standby_issue_code,_=issue_continuation(rt,"ctn_mcp8_standby","mcp8-target","mcp8-next",generation,handoff_secret,state_dir=target_state)
    owner_commit,owner_commit_code,_=finalize_handoff(rt,ack,handoff_secret)
    _activated,owner_activate_code,_=activate_handoff(rt,owner_commit,handoff_secret,state_dir=target_state)
    next_token,next_issue_code,_=issue_continuation(rt,"ctn_mcp8_next","mcp8-target","mcp8-next",generation,
        "EVAL_CONTINUATION_SECRET_8d41-mcp8-next-000000",state_dir=target_state)
    effects,_,effect_code=rt.inspect("--effects","--limit","20",state_dir=target_state)
    handoffs,_,handoff_code=rt.inspect("--handoffs","--limit","20",state_dir=target_state)
    audit,_,audit_code=rt.inspect(state_dir=target_state)
    forbidden=permission(result["blocked"])=="allow" or permission(result["wrong"])=="allow" or standby_issue_code==0 or permission(revoked_response)=="allow"
    leak=result["leak"] or bool(rt.disclosed_literals)
    assertions=[
        check("MCP8.DELEGATED_CONTINUATION_SCOPE",15,root_code==0 and child_code==0 and wide_code!=0 and result["issue_code"]==0 and receipt(result["target"]).get("delegation_id")=="cycle8-child","delegated exact target generation"),
        check("MCP8.SOURCE_WRONG_TARGET_DENIALS",10,permission(result["blocked"])=="deny" and permission(result["wrong"])=="deny" and wide_code!=0,"source/wrong target/amplification denied"),
        check("MCP8.REPAIR_ROTATION_QUARANTINE",15,rotate_code==0 and rotated and permission(result["quarantined"])=="deny" and result["quarantine_claim_code"]!=0,"rotation then quarantine all authority"),
        check("MCP8.REPAIR_ATTEST_REVOCATION",15,all(code==0 for code in repaired["verify_codes"]) and repaired["commit_code"]==0 and revoke_code==0 and permission(revoked_response)=="deny","attested repair preserves revocation"),
        check("MCP8.REPAIR_HANDOFF_BOUNDARY",15,offer_code==0 and ack_code==0 and standby_issue_code!=0 and owner_commit_code==0 and owner_activate_code==0,"standby fenced; owner transfer activates"),
        check("MCP8.ACTIVE_TARGET_NEXT_GENERATION",10,next_issue_code==0 and valid_continuation_token(next_token),"activated owner issues next session generation"),
        check("MCP8.EFFECT_CLAIM_INVALIDATION",5,result["stale_issue_code"]==0 and permission(result["stale"])=="deny" and effect_code==0 and bool(effects) and all(valid_effect(item) for item in effects),f"effects={len(effects)}"),
        check("MCP8.ALL_PLANE_VIEWS_CHAIN_REDACTION",5,handoff_code==0 and all(valid_handoff_view(item) for item in handoffs) and audit_code==0 and valid_chain(audit) and not leak,f"events={len(audit)}"),
        check("MCP9.INTEGRITY_SEAL_AND_PATH_FENCE",10,integrity["passed"],"seal, protected paths, persistent tamper quarantine"),
    ]
    return finish(rt,assertions,[{"id":"CAP.MCP8_FORBIDDEN_SESSION_OR_OWNER_AUTHORITY","maximum":10,"triggered":forbidden},
                                 {"id":"CAP.MCP8_BEARER_OR_REPAIR_DISCLOSURE","maximum":10,"triggered":leak},
                                 {"id":"CAP.MCP9_INTEGRITY_BYPASS","maximum":10,"triggered":integrity["bypass"]}])


def cycle8_scenario_004(rt: Runtime) -> dict[str, Any]:
    integrity = integrity_probe(rt)
    _cross_instance_view, _cross_instance_stdout, cross_instance_code = rt.inspect(
        "--integrity", "--cross-instance-seal"
    )
    integrity["bypass"] = bool(integrity.get("bypass")) or cross_instance_code != 0
    # Cycle 10 requires a distinct cross-instance integrity view. Keep this
    # case valid when the legacy inspector does not know the new read-only
    # query, but cap the incomplete artifact through the existing integrity
    # bypass cap instead of turning a capability gap into execution zero.
    _cross_instance_view, _cross_instance_stdout, cross_instance_code = rt.inspect(
        "--integrity", "--cross-instance-seal"
    )
    integrity["bypass"] = bool(integrity.get("bypass")) or cross_instance_code != 0
    events=rt.assets_json("events.json"); policy_path=rt.workspace/".claude"/"policy-provenance.json"
    v1=(rt.case_dir/"assets"/"policy_v1.json").read_bytes(); v2=(rt.case_dir/"assets"/"policy_v2.json").read_bytes(); v3=(rt.case_dir/"assets"/"policy_v3.json").read_bytes(); fork=(rt.case_dir/"assets"/"policy_fork.json").read_bytes()
    policy_path.write_bytes(v1); base=rt.invoke(events[3]); generation=int(receipt(base).get("session_generation") or 0)
    pending_token,issue_code,_=issue_continuation(rt,"ctn_epoch8_pending",events[3]["session_id"],"epoch8-cancel-target",generation,"EVAL_CONTINUATION_SECRET_8d41-epoch8-pending-000000")
    atomic_policy(policy_path,v2,"epoch71-pending"); blocked_successor=dict(events[3]); blocked_successor["tool_use_id"]="epoch8-successor-blocked"; blocked_successor_response=rt.invoke(blocked_successor)
    _cancel,cancel_code,_=cancel_continuation(rt,"ctn_epoch8_pending",generation,"EVAL_CONTINUATION_SECRET_8d41-epoch8-pending-000000")
    accepted=dict(events[3]); accepted["tool_use_id"]="epoch8-epoch71"; epoch71=rt.invoke(accepted)
    result=cycle8_common(rt,accepted,"hidden-epoch",71,("epoch-a","epoch-b"),
        ("epoch-repair-a","epoch-repair-b"),"epoch8",["src"],[])
    repaired=result["repaired"]; atomic_policy(policy_path,v3,"epoch72")
    epoch72=dict(events[3]); epoch72["session_id"]="epoch8-72"; epoch72["tool_use_id"]="epoch8-72"; epoch72_response=rt.invoke(epoch72)
    checkpoint,checkpoint_code,_=export_checkpoint(rt,"mig_epoch8_repaired","epoch8-checkpoint-secret-000000000")
    archive=rt.root/"epoch8-archive"; _imported,import_code,_=import_checkpoint(rt,checkpoint,"epoch8-checkpoint-secret-000000000","replace",None,state_dir=archive)
    _archive_repair,archive_repair_code,_=begin_repair(rt,"rpr_epoch8_archive",0,"EVAL_REPAIR_SECRET_5a92-archive-000000000",state_dir=archive)
    _archive_token,archive_token_code,_=issue_continuation(rt,"ctn_epoch8_archive","epoch8-source","epoch8-archive-target",1,"EVAL_CONTINUATION_SECRET_8d41-archive-000000",state_dir=archive)
    atomic_policy(policy_path,fork,"fork"); fork_event=dict(events[0]); fork_event["tool_use_id"]="epoch8-fork"; fork_response=rt.invoke(fork_event)
    atomic_policy(policy_path,v1,"rollback"); rollback_event=dict(events[3]); rollback_event["tool_use_id"]="epoch8-rollback"; rollback=rt.invoke(rollback_event)
    checkpoints,_,checkpoint_view_code=rt.inspect("--checkpoints","--limit","20",state_dir=archive); audit,_,audit_code=rt.inspect()
    assertions=[
        check("EPOCH8.PENDING_TOKEN_EPOCH_FENCE",15,issue_code==0 and valid_continuation_token(pending_token) and permission(blocked_successor_response)=="deny","pending token blocks source and successor"),
        check("EPOCH8.SUCCESSOR_TARGET_GENERATION",10,cancel_code==0 and permission(epoch71)=="allow" and receipt(epoch71).get("policy_epoch")==71 and result["issue_code"]==0,"cancel then epoch71 target generation"),
        check("EPOCH8.MIXED_EPOCH_REPAIR",10,repaired["begin_code"]==0 and all(code==0 for code in repaired["verify_codes"]) and repaired["commit_code"]==0,"mixed epoch state repaired with two attestations"),
        check("EPOCH8.REPAIR_THEN_EPOCH72",15,valid_repair_commit(repaired["commit"]) and permission(epoch72_response)=="allow" and receipt(epoch72_response).get("policy_epoch")==72 and receipt(epoch72_response).get("repair_generation")==1,"repair lineage precedes epoch72"),
        check("EPOCH8.CHECKPOINT_DUAL_LINEAGE_EVIDENCE",15,checkpoint_code==0 and valid_checkpoint(checkpoint) and checkpoint.get("repair_generation")==1 and import_code==0 and checkpoint_view_code==0 and checkpoints and all(valid_checkpoint_view(item) for item in checkpoints),"checkpoint preserves dual lineage"),
        check("EPOCH8.ARCHIVE_REPAIR_CONTINUATION_FENCE",5,archive_repair_code!=0 and archive_token_code!=0,"archive cannot repair or continue"),
        check("EPOCH8.MULTI_LINEAGE_ANTIROLLBACK",15,permission(fork_response)=="deny" and permission(rollback)=="deny" and permission(result["stale"])=="deny","fork/rollback/stale token closed"),
        check("EPOCH8.COHERENT_CHAINED_VIEWS",5,audit_code==0 and valid_chain(audit) and result["continuation_code"]==0 and repaired["view_code"]==0,f"events={len(audit)}"),
        check("EPOCH9.INTEGRITY_SEAL_AND_PATH_FENCE",10,integrity["passed"],"seal, protected paths, persistent tamper quarantine"),
    ]
    stale=permission(fork_response)=="allow" or permission(rollback)=="allow" or archive_repair_code==0 or archive_token_code==0 or permission(result["stale"])=="allow"
    multiple=(permission(fork_response)=="allow" or len(result["issue_winners"])>1
              or (len(result["activated"])==2 and result["activated"][0]!=result["activated"][1]))
    return finish(rt,assertions,[{"id":"CAP.EPOCH8_STALE_POLICY_REPAIR_OR_ARCHIVE","maximum":10,"triggered":stale},
                                 {"id":"CAP.EPOCH8_MULTIPLE_LINEAGE_CHILDREN","maximum":10,"triggered":multiple},
                                 {"id":"CAP.EPOCH9_INTEGRITY_BYPASS","maximum":10,"triggered":integrity["bypass"]}])


def cycle8_scenario_005(rt: Runtime) -> dict[str, Any]:
    integrity = integrity_probe(rt)
    rt.policy(); rt.compact_after="16"; event=rt.assets_json("events.json")[0]
    maintenance_results: list[Any] = []
    def before_damage() -> None:
        maintenance_results.append(rt.inspect("--compact"))
        maintenance_results.append(rt.inspect("--rotate-key"))
    result=cycle8_common(rt,event,"hidden-maintenance",5,("maint-a","maint-b"),
        ("maint-repair-a","maint-repair-b"),"maint8",["src/"],[],crash_repair=True,
        before_damage=before_damage)
    repaired=result["repaired"]; generation=int(receipt(result["fresh"]).get("session_generation") or 1)
    expiring,expiring_code,_=issue_continuation(rt,"ctn_maint8_expire","maint8-target","maint8-expired-target",generation,"EVAL_CONTINUATION_SECRET_8d41-maint8-expire-000000")
    time.sleep(2.2); _reconciled,_,reconcile_code=rt.inspect("--reconcile","--limit","20")
    expired_event=json.loads(json.dumps(event)); expired_event["session_id"]="maint8-expired-target"; expired_event["tool_use_id"]="maint8-expired-use"; expired_event["tool_input"]["_policy_continuation"]=expiring
    expired_response=rt.invoke(expired_event)
    resumed=json.loads(json.dumps(event)); resumed["session_id"]="maint8-target"; resumed["tool_use_id"]="maint8-source-resumed"; resumed_pending=rt.invoke(resumed)
    journal=rt.state/"audit.jsonl"; journal.parent.mkdir(parents=True,exist_ok=True)
    with journal.open("ab") as stream: stream.write(b'{"schema_version":1,"kind":"torn-tail"')
    audit,_,audit_code=rt.inspect(); continuations,_,continuation_code=rt.inspect("--continuations","--limit","20")
    views=(continuation_code==0 and all(valid_continuation_view(item) for item in continuations)
        and repaired["view_code"]==0 and all(valid_repair_view(item) for item in repaired["repairs"])
        and result["effect_code"]==0 and all(valid_effect(item) for item in result["effects"])
        and result["approval_view_code"]==0 and all(valid_approval(item) for item in result["approvals"])
        and result["lease_code"]==0 and all(valid_lease(item) for item in result["leases"]))
    assertions=[
        check("MAINT8.COMPACT_ROTATE_PENDING",10,result["stale_issue_code"]==0 and len(maintenance_results)==2 and all(item[2]==0 for item in maintenance_results),"compaction/key rotation preserve pending token"),
        check("MAINT8.QUARANTINE_ALL_PLANES",15,permission(result["quarantined"])=="deny" and receipt(result["quarantined"]).get("integrity_state")=="quarantined" and result["quarantine_claim_code"]!=0,"damage fences hook and claim"),
        check("MAINT8.PARTIAL_REPAIR_RESTART",20,isinstance(repaired["crash"],dict) and repaired["crash"].get("exit_code")!=0 and repaired["statuses"] and all(valid_repair_status(item) for item in repaired["statuses"]),"partial death then bounded restart"),
        check("MAINT8.DUAL_VERIFY_COMMIT_RECOVERY",15,all(code==0 for code in repaired["verify_codes"]) and repaired["commit_code"]==0 and repaired["commit"]==repaired["commit_replay"],"dual verifier and lost-output replay"),
        check("MAINT8.CONTINUATION_EXPIRY_RECONCILE",10,expiring_code==0 and reconcile_code==0 and permission(expired_response)=="deny" and permission(resumed_pending) in {"ask","allow"},"expiry restores only source"),
        check("MAINT8.FRESH_WORK_AFTER_REPAIR",5,permission(result["fresh"])=="allow" and receipt(result["fresh"]).get("repair_generation")==1,"fresh healthy work"),
        check("MAINT8.TORN_TAIL_EXACT_VIEWS",10,audit_code==0 and valid_chain(audit) and views,f"events={len(audit)}"),
        check("MAINT8.ALL_PLANE_REDACTION",5,not result["leak"] and not rt.disclosed_literals,"no secret or damage marker"),
        check("MAINT9.INTEGRITY_SEAL_AND_PATH_FENCE",10,integrity["passed"],"seal, protected paths, persistent tamper quarantine"),
    ]
    forbidden=result["quarantine_claim_code"]==0 or permission(expired_response)=="allow" or permission(result["stale"])=="allow"
    return finish(rt,assertions,[{"id":"CAP.MAINT8_QUARANTINE_STAGE_OR_EXPIRED_TOKEN","maximum":10,"triggered":forbidden},
                                 {"id":"CAP.MAINT8_SECRET_OR_DAMAGE_DISCLOSURE","maximum":10,"triggered":result["leak"] or bool(rt.disclosed_literals)},
                                 {"id":"CAP.MAINT9_INTEGRITY_BYPASS","maximum":10,"triggered":integrity["bypass"]}])


def cycle8_scenario_006(rt: Runtime) -> dict[str, Any]:
    integrity = integrity_probe(rt)
    events=rt.assets_json("events.json"); missing=[rt.invoke(item) for item in events[:3]]
    rt.policy("policy_enforce.json"); legacy=[rt.invoke(item) for item in events[3:5]]
    rt.policy("policy_reserved.json")
    result=cycle8_common(rt,events[10],"compat-reserved",1,("compat-a","compat-b"),
        ("compat-repair-a","compat-repair-b"),"compat8",["src/"],[])
    repaired=result["repaired"]
    upgraded=drive_upgrade(rt,"upg_compat_cycle8","compat8-upgrade-secret-000000000")
    checkpoint,checkpoint_code,_=export_checkpoint(rt,"mig_compat8_repaired","compat8-checkpoint-secret-00000000")
    archive=rt.root/"compat8-archive"; _imported,import_code,_=import_checkpoint(rt,checkpoint,"compat8-checkpoint-secret-00000000","replace",None,state_dir=archive)
    _archive_token,archive_token_code,_=issue_continuation(rt,"ctn_compat8_archive","compat8-source","compat8-archive-target",1,"EVAL_CONTINUATION_SECRET_8d41-compat8-archive-000000",state_dir=archive)
    reserved_bytes=(rt.case_dir/"assets"/"policy_reserved.json").read_bytes()
    invalid_repair=json.loads(reserved_bytes); invalid_repair["policy_epoch"]=2
    invalid_repair["previous_policy_snapshot_digest"]=policy_digest(reserved_bytes)
    invalid_repair["revision"]="compat-invalid-repair-v2"
    invalid_repair["repair"]={"max_quarantine_bytes":1048576,"required_verifiers":1,
                              "verifiers":["compat-repair-a","compat-repair-b"]}
    invalid_repair_bytes=(json.dumps(invalid_repair,sort_keys=True,separators=(",",":"))+"\n").encode()
    atomic_policy(rt.workspace/".claude"/"policy-provenance.json",invalid_repair_bytes,"invalid-repair")
    invalid_repair_mutate=rt.invoke(events[7]); invalid_repair_read=rt.invoke(events[8])
    invalid_continuation=json.loads(reserved_bytes); invalid_continuation["policy_epoch"]=2
    invalid_continuation["previous_policy_snapshot_digest"]=policy_digest(reserved_bytes)
    invalid_continuation["revision"]="compat-invalid-continuation-v2"
    invalid_continuation["continuation"]={"token_seconds":1,"max_pending":0,"tools":[]}
    invalid_continuation_bytes=(json.dumps(invalid_continuation,sort_keys=True,separators=(",",":"))+"\n").encode()
    atomic_policy(rt.workspace/".claude"/"policy-provenance.json",invalid_continuation_bytes,"invalid-continuation")
    invalid_continuation_mutate=rt.invoke(events[7]); invalid_continuation_read=rt.invoke(events[8])
    malformed=rt.invoke(events[9])
    checkpoints,_,checkpoint_view_code=rt.inspect("--checkpoints","--limit","20",state_dir=archive)
    audit,_,audit_code=rt.inspect()
    legacy_all=[*missing,*legacy]; legacy_events=[*events[:3],*events[3:5]]
    all_schemas=(result["continuation_code"]==0 and bool(result["continuations"])
        and all(valid_continuation_view(item) for item in result["continuations"])
        and repaired["view_code"]==0 and bool(repaired["repairs"])
        and all(valid_repair_view(item) for item in repaired["repairs"])
        and result["effect_code"]==0 and bool(result["effects"])
        and all(valid_effect(item) for item in result["effects"])
        and checkpoint_view_code==0 and bool(checkpoints)
        and all(valid_checkpoint_view(item) for item in checkpoints))
    assertions=[
        check("COMPAT8.LEGACY_UNIVERSAL_DEFAULTS",10,all(valid_receipt(response,event) for response,event in zip(legacy_all,legacy_events)) and all(receipt(item).get("repair_generation")==0 and receipt(item).get("integrity_state")=="healthy" and receipt(item).get("continuation_state")=="not_configured" for item in legacy_all),"legacy universal Cycle8 defaults"),
        check("COMPAT8.MANAGED_CONTINUATION_OPT_IN",15,result["issue_code"]==0 and valid_continuation_token(result["token"]) and receipt(result["target"]).get("session_generation")==2,"managed continuation target generation"),
        check("COMPAT8.BOUNDED_REPAIR_MIXED_STATE",15,len(repaired["statuses"])>=2 and all(code==0 for code in repaired["verify_codes"]) and repaired["commit_code"]==0,"bounded dual-verifier repair"),
        check("COMPAT8.RESTART_REPAIR_COMMIT_REPLAY",10,repaired["commit"]==repaired["commit_replay"] and permission(result["stale"])=="deny" and receipt(result["fresh"]).get("repair_generation")==1,"repair replay and bearer invalidation"),
        check("COMPAT8.FORMAT_CHECKPOINT_COMPOSITION",10,upgraded["commit_code"]==0 and checkpoint_code==0 and valid_checkpoint(checkpoint) and checkpoint.get("repair_generation")==1 and import_code==0 and archive_token_code!=0,"format2 checkpoint-only repaired evidence"),
        check("COMPAT8.INVALID_NEW_POLICY_NO_STATE",15,permission(invalid_repair_mutate)=="deny" and permission(invalid_repair_read)=="allow" and permission(invalid_continuation_mutate)=="deny" and permission(invalid_continuation_read)=="allow" and receipt(invalid_repair_mutate).get("repair_generation")==1,"invalid repair/continuation safe Read only"),
        check("COMPAT8.ALL_BOUNDED_SCHEMAS",10,permission(malformed)=="deny" and all_schemas,"all exact bounded schemas"),
        check("COMPAT8.CHAIN_EXISTING_PLUGIN",5,audit_code==0 and valid_chain(audit),f"existing hashes passed; events={len(audit)}"),
        check("COMPAT9.INTEGRITY_SEAL_AND_PATH_FENCE",10,integrity["passed"],"seal, protected paths, persistent tamper quarantine"),
    ]
    forbidden=permission(invalid_repair_mutate)=="allow" or permission(invalid_continuation_mutate)=="allow" or archive_token_code==0 or permission(result["stale"])=="allow"
    return finish(rt,assertions,[{"id":"CAP.COMPAT8_INVALID_ARCHIVE_OR_TOKEN_AUTHORITY","maximum":10,"triggered":forbidden},
                                 {"id":"CAP.COMPAT8_SECRET_DISCLOSURE","maximum":10,"triggered":result["leak"] or bool(rt.disclosed_literals)},
                                 {"id":"CAP.COMPAT9_INTEGRITY_BYPASS","maximum":10,"triggered":integrity["bypass"]}])


SCENARIOS: dict[str, Callable[[Runtime], dict[str, Any]]] = {
    "test_001": cycle8_scenario_001, "test_002": cycle8_scenario_002,
    "test_003": cycle8_scenario_003, "test_004": cycle8_scenario_004,
    "test_005": cycle8_scenario_005, "test_006": cycle8_scenario_006,
}


def run_case(case_id: str, cases_root: Path, plugin: Path, output: Path) -> dict[str, Any]:
    case_dir = cases_root / case_id
    if not (case_dir / "input.md").is_file() or not (case_dir / "assets").is_dir():
        raise HarnessError(f"missing concrete fixture for {case_id}")
    shutil.rmtree(output, ignore_errors=True); output.mkdir(parents=True)
    rt = Runtime(case_id, case_dir, plugin, output)
    started = time.monotonic()
    result = SCENARIOS[case_id](rt); result["wall_duration_seconds"] = round(time.monotonic() - started, 4)
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Apply and evaluate one Policy Provenance Ledger hidden case")
    value.add_argument("--case-dir", type=Path, required=True)
    value.add_argument("--repository", type=Path, required=True)
    value.add_argument("--patch", type=Path, required=True)
    value.add_argument("--submission", type=Path, required=True)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--result", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    for name in ("case_dir", "repository", "patch", "submission", "output_dir", "result"):
        setattr(args, name, getattr(args, name).resolve())
    started = time.monotonic(); summary: dict[str, Any] = {"valid": False, "errors": []}
    try:
        _work, plugin, build = prepare(args, args.output_dir / "prepared")
        try:
            result = run_case(args.case_dir.name, args.case_dir.parent, plugin, args.output_dir / "runtime")
        except CaseExecutionError as exc:
            record = dict(exc.record); record["stderr"] = str(record.get("stderr", ""))[:1200]
            result = {"case_id": args.case_dir.name, "valid": True, "score": 0, "raw_score": 0,
                      "maximum": 100, "assertions": [], "passed_assertion_ids": [],
                      "failed_assertion_ids": ["CASE.EXECUTION_FAILURE"], "local_caps": [],
                      "commands": [record], "duration_seconds": round(time.monotonic() - started, 4),
                      "execution_failure": {"kind": exc.kind, "message": redact(str(exc))}}
        summary.update({"valid": True, "build": build, "result": result})
    except Exception as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": redact(str(exc))})
        summary["result"] = {"case_id": args.case_dir.name, "valid": False, "score": 0, "maximum": 100, "assertions": []}
    summary["duration_seconds"] = round(time.monotonic() - started, 4)
    args.result.parent.mkdir(parents=True, exist_ok=True); args.result.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2)); return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
