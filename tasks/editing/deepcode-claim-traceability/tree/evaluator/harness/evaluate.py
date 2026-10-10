from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .case_specs import CASE_SPECS, case_directory, load_manifest
from .common import load_json, run_process


REQUIRED_CAPSULE_FILES = (
    "paper_spec.json", "traceability_graph.json", "reproduction_manifest.json",
    "assumptions.json", "deviations.json", "data_manifest.json", "checksums.json",
    "environment.lock", "replay.py", "payload",
)
PUBLIC_WEIGHTS = {
    "CAPSULE-COMPAT": 8, "ORDINARY-COMPAT": 4,
    "REV-REGISTER-IMMUTABLE": 2, "REV-SEMANTIC-DIFF": 2, "REVIEW-AUTH-POLICY": 2, "REVIEW-IDEMPOTENCY": 2,
    "EXEC-START-BINDING": 2, "EXEC-CHECKPOINTS": 3, "EXEC-RECOVERY-FENCE": 2, "EXEC-ATTESTATION": 1,
    "PROMOTION-GATE-CAS": 7, "CROSS-RECONCILE": 5,
    "SCOPE-NONDISCLOSURE": 2, "NO-CONTAMINATION": 1,
    "AUDIT-LEDGER": 15, "AUDIT-AUTH-REPLAY": 15, "QUARANTINE-RECOVERY": 15, "QUARANTINE-SAFETY": 12,
}
DIMENSION_IDS = {
    "preserved_behavior": {"CAPSULE-COMPAT", "ORDINARY-COMPAT"},
    "revision_review": {"REV-REGISTER-IMMUTABLE", "REV-SEMANTIC-DIFF", "REVIEW-AUTH-POLICY", "REVIEW-IDEMPOTENCY"},
    "resumable_execution": {"EXEC-START-BINDING", "EXEC-CHECKPOINTS", "EXEC-RECOVERY-FENCE", "EXEC-ATTESTATION"},
    "cross_surface": {"PROMOTION-GATE-CAS", "CROSS-RECONCILE"},
    "isolation_compatibility": {"SCOPE-NONDISCLOSURE", "NO-CONTAMINATION"},
    "operational_surfaces": {"AUDIT-LEDGER", "AUDIT-AUTH-REPLAY", "QUARANTINE-RECOVERY", "QUARANTINE-SAFETY"},
}


def _offline_env(repository: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(repository), "PYTHONDONTWRITEBYTECODE": "1",
        "DEEPCODE_OFFLINE": "1", "NO_PROXY": "*",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
    })
    for key in ("DEEPSEEK_API_KEY", "SERPER_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _contains_absolute(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute(item) for item in value)
    return isinstance(value, str) and (value.startswith("/") or (len(value) > 2 and value[1:3] in {":/", ":\\"}))


def _read_capsule(capsule: Path) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    errors: list[str] = []
    if not capsule.is_dir():
        return values, ["capsule directory missing"]
    for name in REQUIRED_CAPSULE_FILES:
        path = capsule / name
        if not path.exists():
            errors.append(f"missing {name}")
        elif name == "payload" and not path.is_dir():
            errors.append("payload is not a directory")
        elif name.endswith(".json"):
            try:
                value = load_json(path)
                if not isinstance(value, dict) or value.get("schema_version") != "1.0":
                    errors.append(f"{name} is not a schema 1.0 object")
                values[name] = value
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    return values, errors


def _checksum_ok(capsule: Path, checksums: Any) -> bool:
    if not isinstance(checksums, dict):
        return False
    mapping = checksums.get("files") if isinstance(checksums.get("files"), dict) else {
        key: value for key, value in checksums.items() if key != "schema_version"
    }
    if len(mapping) < 4:
        return False
    for raw, expected in mapping.items():
        relative = Path(str(raw))
        target = capsule / relative
        if relative.is_absolute() or ".." in relative.parts or not target.is_file() or target.is_symlink():
            return False
        if not isinstance(expected, str) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            return False
    return True


def _scientific_ok(case_id: str, capsule: Path, spec: dict[str, Any]) -> bool:
    if spec.get("blocked"):
        values, errors = _read_capsule(capsule)
        return not errors and values.get("reproduction_manifest.json", {}).get("status") == "blocked"
    artifact = next((path for path in capsule.rglob(spec["artifact"]) if path.is_file()), None)
    if artifact is None:
        return False
    try:
        value = load_json(artifact)
        if not isinstance(value, dict):
            return False
        if case_id == "dev_001":
            return value["output"] == [-3.449489742783178, -1.0, 1.449489742783178] and abs(value["variance"] - 4) < 1e-8
        if case_id == "dev_002":
            return value["selected"] == "alpha" and value["tie"] is True
        if case_id == "test_001":
            expected = [[1.0, math.exp(-.5), math.exp(-1)], [math.exp(-.5), math.exp(-1), math.exp(-2)]]
            return all(abs(value["weights"][r][c] - expected[r][c]) < 1e-12 for r in range(2) for c in range(3))
        if case_id == "test_002":
            return [run["seed"] for run in value["runs"]] == [7, 19, 31] and all(sum(run["counts"]) == 200 for run in value["runs"])
        if case_id == "test_003":
            return value["learning_rate"] == .01 and abs(value["final"] - .99 ** 10) < 1e-12
        if case_id == "test_004":
            return value["train_indices"] == list(range(6)) and value["test_indices"] == [6, 7]
        if case_id == "test_005":
            return value["full"]["outputs"] == [3, 5, 7] and value["improvement"] == 1
    except (KeyError, TypeError, ValueError, IndexError):
        return False
    return False


def _graph_ok(values: dict[str, Any], spec: dict[str, Any]) -> bool:
    graph = values.get("traceability_graph.json", {})
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    edges = graph.get("edges") if isinstance(graph, dict) else None
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return False
    ids = {node.get("id") for node in nodes if isinstance(node, dict) and isinstance(node.get("id"), str)}
    text = _json_text(graph).lower()
    # Some valid extractors represent a claim as the paper sentence rather than
    # repeating its caller-supplied identifier; require the semantic graph and
    # all stable code/provenance evidence while allowing one such alias gap.
    required_ids = spec["ids"] if spec["ids"] else []
    ids_ok = sum(item.lower() in text for item in required_ids) >= max(1, len(required_ids) - 1)
    return ids_ok and all(item.lower() in text for item in spec["mappings"] + spec["provenance"]) and all(
        isinstance(edge, dict) and edge.get("source") in ids and edge.get("target") in ids for edge in edges
    )


def _write_successor(base: Path, successor: Path, case_id: str) -> None:
    shutil.copytree(base, successor)
    graph_path = successor / "traceability_graph.json"
    graph = load_json(graph_path)
    nodes = graph.setdefault("nodes", [])
    existing = next((node for node in nodes if isinstance(node, dict) and isinstance(node.get("id"), str)), None)
    if existing is not None:
        existing["revision_marker"] = f"changed-{case_id}"
    marker = f"REVISION-EVIDENCE-{case_id}"
    nodes.append({"id": marker, "type": "test", "provenance": "test", "locator": "revision verification"})
    if existing is not None:
        graph.setdefault("edges", []).append({"source": existing["id"], "target": marker, "relation": "verified_by"})
    graph_path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksum_path = successor / "checksums.json"
    checksums = load_json(checksum_path)
    mapping = checksums.get("files") if isinstance(checksums.get("files"), dict) else checksums
    for raw in list(mapping):
        if Path(raw).as_posix() == "traceability_graph.json":
            mapping[raw] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    checksum_path.write_text(json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest_commands(capsule: Path) -> list[list[str]]:
    try:
        manifest = load_json(capsule / "reproduction_manifest.json")
    except Exception:
        return []
    raw = manifest.get("commands") if isinstance(manifest, dict) else None
    if not isinstance(raw, list):
        return []
    commands: list[list[str]] = []
    for item in raw:
        argv = item.get("argv") if isinstance(item, dict) else item
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
            return []
        commands.append(argv)
    return commands


def _checkpoint_command(checkpoint: dict[str, Any]) -> Any:
    for key in ("command", "argv", "command_argv"):
        if key in checkpoint:
            return checkpoint[key]
    return None


def _operation(case_id: str, sequence: str, action: str, spec: dict[str, Any], actor: str, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "operation_id": f"{case_id}-{sequence}", "action": action,
        "tenant_id": spec["tenant"], "project_id": spec["project"], "actor_id": actor, **fields,
    }


def _record(response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    return response.get("record") if isinstance(response.get("record"), dict) else response


def _accepted(call: tuple[ProcessResult, dict[str, Any] | None], action: str | None = None) -> bool:
    process, response = call
    return process.exit_code == 0 and bool(response) and response.get("accepted") is True and (action is None or response.get("action") == action)


def _rejected(call: tuple[ProcessResult, dict[str, Any] | None]) -> bool:
    process, response = call
    return process.exit_code == 2 and bool(response) and response.get("accepted") is False and isinstance(response.get("error"), dict) and isinstance(response["error"].get("code"), str)


def _field(response: dict[str, Any] | None, *names: str) -> Any:
    record = _record(response)
    for name in names:
        if name in record:
            return record[name]
    return None


def _weights(case_id: str) -> dict[str, int]:
    if case_id.startswith("test_"):
        return {key: int(value) for key, value in load_manifest(case_id)["assertions"].items()}
    return dict(PUBLIC_WEIGHTS)


def _case_safety_violation(scored_ids: set[str], *, scope_ok: bool, stale_fenced: bool) -> bool:
    """Apply the case-local safety ceiling only to safety assertions in scope."""
    return (
        ("SCOPE-NONDISCLOSURE" in scored_ids and not scope_ok)
        or ("EXEC-RECOVERY-FENCE" in scored_ids and not stale_fenced)
    )


def evaluate_case(repository: Path, case_id: str, *, build_gates_passed: bool = True) -> dict[str, Any]:
    spec = CASE_SPECS[case_id]
    weights = _weights(case_id)
    selected = set(weights)

    def selected_any(*assertion_ids: str) -> bool:
        return bool(selected.intersection(assertion_ids))

    # Keep the evaluator's observation state broad enough for the shared
    # lifecycle setup, but execute optional diagnostic families and score only
    # the assertion scope declared by this case's private manifest.  Genuine
    # prerequisites (for example, approval before an execution or promotion)
    # may still run, but an excluded audit, quarantine, reconciliation, diff,
    # ordinary-compatibility, scope, or recovery result cannot gate or cap the
    # selected score.
    state = {key: {"passed": False, "evidence": "not observed"} for key in PUBLIC_WEIGHTS}
    evidence: dict[str, Any] = {"case_id": case_id, "processes": [], "operations": []}
    workspace = Path(tempfile.mkdtemp(prefix=f"{case_id}-"))
    project = workspace / "project"
    shutil.copytree(case_directory(case_id) / "assets" / "project", project)
    canary = f"PRIVATE_{case_id}_CANARY"
    (workspace / "sibling-private.txt").write_text(canary, encoding="utf-8")
    env = _offline_env(repository)
    direct = project / ".deepcode" / "candidate_capsules" / "base"
    direct.parent.mkdir(parents=True, exist_ok=True)
    request = project / ".deepcode" / "traceability_request.json"
    direct_process = run_process([sys.executable, "-m", "workflows.traceability", "--request", str(request), "--output", str(direct)], cwd=repository, env=env)
    evidence["processes"].append(direct_process.to_json())
    values, capsule_errors = _read_capsule(direct)
    expected_exit = 2 if spec.get("blocked") or spec.get("statuses", ["complete"])[0] != "complete" else 0
    capsule_valid = direct_process.exit_code == expected_exit and not direct_process.timed_out and not capsule_errors
    replay = run_process([sys.executable, "replay.py"], cwd=direct, env=env) if capsule_valid else None
    if replay:
        evidence["processes"].append(replay.to_json())
    capsule_ok = (
        capsule_valid
        and _checksum_ok(direct, values.get("checksums.json"))
        and _graph_ok(values, spec)
        and _scientific_ok(case_id, direct, spec)
        and replay is not None
        and ((spec.get("blocked") and replay.exit_code != 0) or (not spec.get("blocked") and replay.exit_code == 0))
    )
    state["CAPSULE-COMPAT"] = {"passed": capsule_ok, "evidence": capsule_errors or f"direct_exit={direct_process.exit_code}; replay_exit={replay.exit_code if replay else None}"}

    policy = load_json(project / ".deepcode" / "traceability_policy.json")
    science_actor = policy["roles"]["scientist"][0]
    code_actor = policy["roles"]["maintainer"][0]
    operator_actor = policy["roles"]["operator"][0]
    auditor_actor = policy["roles"].get("auditor", ["auditor-unconfigured"])[0]
    security_actor = policy["roles"].get("security", ["security-unconfigured"])[0]
    revision_store = project / ".deepcode" / "traceability_revisions"
    execution_store = project / ".deepcode" / "traceability_execution"
    operation_dir = project / ".deepcode" / "operations"
    operation_dir.mkdir(parents=True, exist_ok=True)

    def invoke(surface: str, document: dict[str, Any]) -> tuple[ProcessResult, dict[str, Any] | None]:
        path = operation_dir / f"{document['operation_id']}.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        module = "workflows.traceability_revisions" if surface == "revision" else "workflows.traceability_execution"
        argv = [sys.executable, "-m", module, "--store", str(revision_store if surface == "revision" else execution_store)]
        if surface == "execution":
            argv.extend(["--revision-store", str(revision_store)])
        argv.extend(["--operation", str(path)])
        process = run_process(argv, cwd=repository, env=env)
        try:
            parsed = json.loads(process.stdout.strip())
            response = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, UnicodeError):
            response = None
        evidence["processes"].append(process.to_json())
        evidence["operations"].append({"surface": surface, "request": document, "response": response, "exit_code": process.exit_code})
        return process, response

    successor = direct.parent / "successor"
    if direct.is_dir():
        _write_successor(direct, successor, case_id)
    relative_base = "../candidate_capsules/base"
    relative_successor = "../candidate_capsules/successor"
    register_base_doc = _operation(case_id, "register-base", "register", spec, science_actor, revision_key="base", capsule_path=relative_base, parent_revision_id=None, expected_head_revision_id=None)
    concurrent_register_ok = True
    if spec["scenario"] == "concurrent_retry":
        with ThreadPoolExecutor(max_workers=4) as pool:
            base_calls = list(pool.map(lambda _: invoke("revision", register_base_doc), range(4)))
        base, base_retry = base_calls[0], base_calls[1]
        concurrent_register_ok = all(_accepted(call, "register") for call in base_calls) and all(
            call[1] == base[1] for call in base_calls
        )
    else:
        base = invoke("revision", register_base_doc)
        base_retry = invoke("revision", register_base_doc)
    base_id = _field(base[1], "revision_id")
    base_digest = _field(base[1], "revision_digest")
    register_next_doc = _operation(case_id, "register-next", "register", spec, science_actor, revision_key="successor", capsule_path=relative_successor, parent_revision_id=base_id, expected_head_revision_id=None)
    target = invoke("revision", register_next_doc)
    target_id = _field(target[1], "revision_id")
    target_digest = _field(target[1], "revision_digest")
    expected_commands = _manifest_commands(successor) if successor.is_dir() else []
    initial_generation = _field(target[1], "review_generation")
    if not isinstance(initial_generation, int):
        initial_generation = 0
    if direct.exists():
        shutil.rmtree(direct)
    inspect_after_source_loss = invoke("revision", _operation(case_id, "inspect-immutable", "inspect", spec, science_actor, revision_id=target_id))
    immutable_text = _json_text(inspect_after_source_loss[1]).lower()
    register_ok = all((_accepted(base, "register"), _accepted(base_retry, "register"), base[1] == base_retry[1], _accepted(target, "register"), concurrent_register_ok)) and isinstance(base_id, str) and isinstance(target_id, str) and base_id != target_id and isinstance(target_digest, str) and len(target_digest) == 64
    immutable_ok = _accepted(inspect_after_source_loss, "inspect") and not any(word in immutable_text for word in ("corrupt", "missing snapshot", "invalid integrity"))
    state["REV-REGISTER-IMMUTABLE"] = {"passed": register_ok and immutable_ok, "evidence": f"base={base_id}; target={target_id}; source_removed={not direct.exists()}; inspect={inspect_after_source_loss[1]}"}

    marker = f"REVISION-EVIDENCE-{case_id}"
    if selected_any("QUARANTINE-RECOVERY", "QUARANTINE-SAFETY"):
        # Security quarantine is an explicit operational boundary. It must
        # survive process restart, reject execution while active, and require
        # an authorized restore with the current digest/generation.
        quarantine = invoke("revision", _operation(case_id, "quarantine", "quarantine", spec, security_actor, revision_id=target_id, revision_digest=target_digest, reason="untrusted instruction or integrity incident"))
        quarantine_retry = invoke("revision", _operation(case_id, "quarantine", "quarantine", spec, security_actor, revision_id=target_id, revision_digest=target_digest, reason="untrusted instruction or integrity incident"))
        quarantine_record = _record(quarantine[1])
        quarantine_generation = quarantine_record.get("quarantine_generation")
        quarantine_start = invoke("execution", _operation(case_id, "start-quarantined", "start", spec, operator_actor, plan_key="quarantine-probe", revision_id=target_id, revision_digest=target_digest, expected_review_generation=initial_generation))
        restore = invoke("revision", _operation(case_id, "restore", "restore", spec, security_actor, revision_id=target_id, revision_digest=target_digest, expected_quarantine_generation=quarantine_generation))
        restore_retry = invoke("revision", _operation(case_id, "restore", "restore", spec, security_actor, revision_id=target_id, revision_digest=target_digest, expected_quarantine_generation=quarantine_generation))
        quarantine_ok = _accepted(quarantine, "quarantine") and quarantine[1] == quarantine_retry[1] and _rejected(quarantine_start) and _accepted(restore, "restore") and restore[1] == restore_retry[1] and _record(restore[1]).get("quarantine", {}).get("status") == "active"
        state["QUARANTINE-RECOVERY"] = {"passed": quarantine_ok, "evidence": {"quarantine": quarantine[1], "retry": quarantine_retry[1], "blocked_start": quarantine_start[1], "restore": restore[1]}}
        unauthorized_quarantine = invoke("revision", _operation(case_id, "quarantine-denied", "quarantine", spec, "outsider", revision_id=target_id, revision_digest=target_digest, reason="must deny"))
        state["QUARANTINE-SAFETY"] = {"passed": quarantine_ok and _rejected(unauthorized_quarantine), "evidence": unauthorized_quarantine[1]}

    if selected_any("AUDIT-LEDGER", "AUDIT-AUTH-REPLAY"):
        audit_denied = invoke("revision", _operation(case_id, "audit-denied", "audit", spec, "outsider"))
        audit_doc = _operation(case_id, "audit", "audit", spec, auditor_actor, since_sequence=0)
        audit = invoke("revision", audit_doc)
        audit_retry = invoke("revision", audit_doc)
        audit_record = _record(audit[1])
        events = audit_record.get("events") if isinstance(audit_record, dict) else None
        audit_ledger_ok = _accepted(audit, "audit") and audit[1] == audit_retry[1] and _rejected(audit_denied) and isinstance(events, list) and len(events) >= 3 and bool(audit_record.get("chain_valid")) and all(isinstance(item, dict) and isinstance(item.get("hash"), str) for item in events)
        state["AUDIT-LEDGER"] = {"passed": audit_ledger_ok, "evidence": audit[1]}
        # Changed operation bodies and policy drift must not permit an audit replay.
        audit_conflict_doc = dict(audit_doc); audit_conflict_doc["since_sequence"] = 1
        audit_conflict = invoke("revision", audit_conflict_doc)
        audit_replay_ok = audit_ledger_ok and _rejected(audit_conflict)
        state["AUDIT-AUTH-REPLAY"] = {"passed": audit_replay_ok, "evidence": {"denied": audit_denied[1], "retry_equal": audit[1] == audit_retry[1], "conflict": audit_conflict[1]}}

    if "REV-SEMANTIC-DIFF" in selected:
        compare = invoke("revision", _operation(case_id, "compare", "compare", spec, science_actor, base_revision_id=base_id, target_revision_id=target_id))
        compare_text = _json_text(compare[1])
        diff_ok = _accepted(compare, "compare") and marker in compare_text and "changed" in compare_text.lower() and base_id not in (None, target_id)
        state["REV-SEMANTIC-DIFF"] = {"passed": diff_ok, "evidence": compare[1]}

    unauthorized = invoke("revision", _operation(case_id, "review-denied", "review", spec, "outsider", revision_id=target_id, revision_digest=target_digest, role="scientist", decision="approve", expected_review_generation=initial_generation, note="must be denied"))
    before_execution = invoke("revision", _operation(case_id, "promote-too-early", "promote", spec, code_actor, revision_id=target_id, revision_digest=target_digest, expected_review_generation=initial_generation, expected_head_revision_id=None, execution_store="../traceability_execution", plan_id="not-complete", plan_generation=1))
    science_doc = _operation(case_id, "review-science", "review", spec, science_actor, revision_id=target_id, revision_digest=target_digest, role="scientist", decision="approve", expected_review_generation=initial_generation, note="scientific fidelity checked")
    concurrent_review_ok = True
    if spec["scenario"] == "concurrent_retry":
        with ThreadPoolExecutor(max_workers=4) as pool:
            science_calls = list(pool.map(lambda _: invoke("revision", science_doc), range(4)))
        science_review, science_retry = science_calls[0], science_calls[1]
        concurrent_review_ok = all(_accepted(call, "review") for call in science_calls) and all(
            call[1] == science_review[1] for call in science_calls
        )
    else:
        science_review = invoke("revision", science_doc)
        science_retry = invoke("revision", science_doc)
    generation_one = _field(science_review[1], "review_generation")
    code_doc = _operation(case_id, "review-code", "review", spec, code_actor, revision_id=target_id, revision_digest=target_digest, role="maintainer", decision="approve", expected_review_generation=generation_one, note="mapping checked")
    code_review = invoke("revision", code_doc)
    review_generation = _field(code_review[1], "review_generation")
    auth_ok = _rejected(unauthorized) and _rejected(before_execution) and _accepted(science_review, "review") and _accepted(code_review, "review") and isinstance(review_generation, int) and review_generation > initial_generation
    state["REVIEW-AUTH-POLICY"] = {"passed": auth_ok, "evidence": f"denied={unauthorized[1]}; generations={initial_generation},{generation_one},{review_generation}"}
    review_retry_ok = concurrent_review_ok and _accepted(science_retry, "review") and science_retry[1] == science_review[1] and _field(science_retry[1], "review_generation") == generation_one
    conflict_doc = dict(science_doc)
    conflict_doc["note"] = "changed operation body"
    operation_conflict = invoke("revision", conflict_doc)
    revision_conflicts_ok = True
    conflict_evidence: list[Any] = [operation_conflict[1]]
    if spec["scenario"] == "scope_conflict":
        conflicting_capsule = successor.parent / "conflicting"
        _write_successor(successor, conflicting_capsule, f"{case_id}-conflict")
        changed_key = invoke("revision", _operation(
            case_id, "changed-revision-key", "register", spec, science_actor,
            revision_key="successor", capsule_path="../candidate_capsules/conflicting",
            parent_revision_id=base_id, expected_head_revision_id=None,
        ))
        unsafe_path = invoke("revision", _operation(
            case_id, "unsafe-register", "register", spec, science_actor,
            revision_key="unsafe", capsule_path="../../../sibling-private.txt",
            parent_revision_id=None, expected_head_revision_id=None,
        ))
        escape_link = direct.parent / "escaping-capsule"
        try:
            escape_link.symlink_to(workspace, target_is_directory=True)
        except FileExistsError:
            pass
        symlink_path = invoke("revision", _operation(
            case_id, "symlink-register", "register", spec, science_actor,
            revision_key="symlink", capsule_path="../candidate_capsules/escaping-capsule",
            parent_revision_id=None, expected_head_revision_id=None,
        ))
        revision_conflicts_ok = all(_rejected(call) for call in (changed_key, unsafe_path, symlink_path))
        conflict_evidence.extend([changed_key[1], unsafe_path[1], symlink_path[1]])
    state["REVIEW-IDEMPOTENCY"] = {"passed": review_retry_ok and _rejected(operation_conflict) and revision_conflicts_ok, "evidence": f"exact_retry_equal={science_retry[1] == science_review[1]}; conflicts={conflict_evidence}"}

    wrong_scope_doc = _operation(case_id, "wrong-scope", "inspect", spec, science_actor, revision_id=target_id)
    wrong_scope_doc["tenant_id"] = spec["tenant"] + "-other"
    wrong_scope = invoke("revision", wrong_scope_doc)
    scope_text = _json_text(wrong_scope[1])
    scope_ok = _rejected(wrong_scope) and (not isinstance(target_id, str) or target_id not in scope_text) and (not isinstance(target_digest, str) or target_digest not in scope_text) and not _contains_absolute(wrong_scope[1])

    start_denied = invoke("execution", _operation(case_id, "start-denied", "start", spec, "outsider", plan_key="denied", revision_id=target_id, revision_digest=target_digest, expected_review_generation=review_generation))
    start_doc = _operation(case_id, "start", "start", spec, operator_actor, plan_key="main", revision_id=target_id, revision_digest=target_digest, expected_review_generation=review_generation)
    start = invoke("execution", start_doc)
    start_retry = invoke("execution", start_doc)
    plan_id = _field(start[1], "plan_id")
    generation = _field(start[1], "generation", "plan_generation")
    token = _field(start[1], "claim_token")
    start_ok = _rejected(start_denied) and _accepted(start, "start") and _accepted(start_retry, "start") and start[1] == start_retry[1] and isinstance(plan_id, str) and generation == 1 and isinstance(token, str) and len(token) >= 12 and target_digest in _json_text(start[1])
    state["EXEC-START-BINDING"] = {"passed": start_ok, "evidence": f"plan={plan_id}; generation={generation}; denied={start_denied[1]}"}
    wrong_plan_doc = _operation(case_id, "wrong-scope-plan", "status", spec, operator_actor, plan_id=plan_id)
    wrong_plan_doc["tenant_id"] = spec["tenant"] + "-other"
    wrong_plan = invoke("execution", wrong_plan_doc)
    wrong_plan_text = _json_text(wrong_plan[1])
    scope_ok = scope_ok and _rejected(wrong_plan) and (not isinstance(plan_id, str) or plan_id not in wrong_plan_text) and not _contains_absolute(wrong_plan[1])

    stale_fenced = True
    recovery_observed = False
    if spec["scenario"] in {"pause_resume", "response_loss"} and start_ok:
        pause_doc = _operation(case_id, "pause", "pause", spec, operator_actor, plan_id=plan_id, generation=generation, claim_token=token, worker_id="worker-old", reason={"code": "QUOTA_EXHAUSTED"})
        pause = invoke("execution", pause_doc)
        pause_retry = invoke("execution", pause_doc)
        resume_doc = _operation(case_id, "resume", "resume", spec, operator_actor, plan_id=plan_id, expected_generation=generation, reason="quota replenished")
        resume = invoke("execution", resume_doc)
        new_generation = _field(resume[1], "generation", "plan_generation")
        new_token = _field(resume[1], "claim_token")
        stale = invoke("execution", _operation(case_id, "stale-after-resume", "advance", spec, operator_actor, plan_id=plan_id, generation=generation, claim_token=token, worker_id="worker-old"))
        recovery_observed = _accepted(pause, "pause") and pause[1] == pause_retry[1] and _accepted(resume, "resume") and new_generation == generation + 1 and new_token != token
        stale_fenced = _rejected(stale)
        generation, token = new_generation, new_token

    if spec["scenario"] == "cancel_fence" and start_ok:
        cancel = invoke("execution", _operation(case_id, "cancel", "cancel", spec, operator_actor, plan_id=plan_id, expected_generation=generation, reason="superseded task lock"))
        stale = invoke("execution", _operation(case_id, "stale-after-cancel", "advance", spec, operator_actor, plan_id=plan_id, generation=generation, claim_token=token, worker_id="obsolete"))
        canceled_status = invoke("execution", _operation(case_id, "canceled-status", "status", spec, operator_actor, plan_id=plan_id))
        cancel_record = _record(cancel[1])
        canceled_record = _record(canceled_status[1])
        recovery_observed = _accepted(cancel, "cancel") and _accepted(canceled_status, "status")
        stale_fenced = _rejected(stale) and canceled_record.get("checkpoints", []) == cancel_record.get("checkpoints", []) and str(canceled_record.get("status", "")).lower() in {"canceled", "cancelled"}
        replacement = invoke("execution", _operation(case_id, "replacement", "start", spec, operator_actor, plan_key="replacement", revision_id=target_id, revision_digest=target_digest, expected_review_generation=review_generation))
        if _accepted(replacement, "start"):
            start = replacement
            plan_id = _field(start[1], "plan_id")
            generation = _field(start[1], "generation", "plan_generation")
            token = _field(start[1], "claim_token")

    checkpoints_before = 0
    exact_retry_ok = False
    concurrent_advance_ok = True
    advance_responses: list[dict[str, Any] | None] = []
    if isinstance(plan_id, str) and isinstance(generation, int) and isinstance(token, str):
        for index in range(12):
            advance_doc = _operation(case_id, f"advance-{index}", "advance", spec, operator_actor, plan_id=plan_id, generation=generation, claim_token=token, worker_id=f"worker-{index % 2}")
            if index == 0 and spec["scenario"] == "concurrent_retry":
                with ThreadPoolExecutor(max_workers=4) as pool:
                    advance_calls = list(pool.map(lambda _: invoke("execution", advance_doc), range(4)))
                first, retry = advance_calls[0], advance_calls[1]
                concurrent_advance_ok = all(_accepted(call, "advance") for call in advance_calls) and all(
                    call[1] == first[1] for call in advance_calls
                )
                exact_retry_ok = concurrent_advance_ok
            else:
                first = invoke("execution", advance_doc)
                if index == 0:
                    retry = invoke("execution", advance_doc)
                    exact_retry_ok = _accepted(first, "advance") and first[1] == retry[1]
            advance_responses.append(first[1])
            status = str(_field(first[1], "status") or "").lower()
            if not _accepted(first, "advance") or status in {"complete", "completed"}:
                break
    status_call = invoke("execution", _operation(case_id, "status", "status", spec, operator_actor, plan_id=plan_id))
    status_record = _record(status_call[1])
    checkpoints = status_record.get("checkpoints") if isinstance(status_record.get("checkpoints"), list) else []
    sequences = [item.get("sequence") for item in checkpoints if isinstance(item, dict)]
    expected_prefix = expected_commands[:len(checkpoints)]
    observed_commands = [_checkpoint_command(item) for item in checkpoints if isinstance(item, dict)]
    commands_ok = len(observed_commands) == len(checkpoints) and observed_commands == expected_prefix
    checkpoint_evidence_ok = all(
        isinstance(item, dict)
        and isinstance(item.get("exit_code"), int)
        and isinstance(item.get("output_digest"), str)
        and len(item["output_digest"]) == 64
        for item in checkpoints
    )
    prefix_ok = bool(checkpoints) or (spec.get("blocked") and not expected_commands)
    prefix_ok = prefix_ok and sequences == list(range(1, len(sequences) + 1)) and len(sequences) == len(set(sequences))
    blocked_ready = bool(spec.get("blocked")) and str(status_record.get("status", "")).lower() in {"complete", "completed"} and not checkpoints
    checkpoint_ok = _accepted(status_call, "status") and prefix_ok and commands_ok and (checkpoint_evidence_ok or blocked_ready) and concurrent_advance_ok and (exact_retry_ok or blocked_ready or str(status_record.get("status", "")).lower() in {"complete", "completed"})
    state["EXEC-CHECKPOINTS"] = {"passed": checkpoint_ok, "evidence": f"checkpoints={checkpoints}; expected_commands={expected_commands}; exact_retry={exact_retry_ok}; concurrent={concurrent_advance_ok}"}
    if spec["scenario"] not in {"pause_resume", "response_loss", "cancel_fence"}:
        wrong_token = invoke("execution", _operation(case_id, "wrong-token", "advance", spec, operator_actor, plan_id=plan_id, generation=generation, claim_token="invalid-token", worker_id="stale"))
        stale_fenced = _rejected(wrong_token)
        recovery_observed = stale_fenced
    state["EXEC-RECOVERY-FENCE"] = {"passed": recovery_observed and stale_fenced, "evidence": f"scenario={spec['scenario']}; recovery={recovery_observed}; stale_fenced={stale_fenced}"}
    status_value = str(status_record.get("status", "")).lower()
    attestation = status_record.get("attestation")
    attestation_ok = status_value in {"complete", "completed"} and isinstance(attestation, dict) and target_digest in _json_text(attestation) and _field(status_call[1], "generation", "plan_generation") == generation
    review_invalidation_ok = True
    review_invalidation_evidence: Any = None
    if spec["scenario"] == "response_loss" and attestation_ok:
        reject_after_run = invoke("revision", _operation(
            case_id, "review-after-run-reject", "review", spec, code_actor,
            revision_id=target_id, revision_digest=target_digest, role="maintainer",
            decision="reject", expected_review_generation=review_generation,
            note="execution evidence requires refresh",
        ))
        rejected_generation = _field(reject_after_run[1], "review_generation")
        stale_attestation_promotion = invoke("revision", _operation(
            case_id, "stale-attestation-promote", "promote", spec, code_actor,
            revision_id=target_id, revision_digest=target_digest,
            expected_review_generation=rejected_generation,
            expected_head_revision_id=None, execution_store="../traceability_execution",
            plan_id=plan_id, plan_generation=generation,
        ))
        reapprove_after_run = invoke("revision", _operation(
            case_id, "review-after-run-approve", "review", spec, code_actor,
            revision_id=target_id, revision_digest=target_digest, role="maintainer",
            decision="approve", expected_review_generation=rejected_generation,
            note="review refreshed after recovery",
        ))
        review_generation = _field(reapprove_after_run[1], "review_generation")
        refreshed = invoke("execution", _operation(
            case_id, "refreshed-plan", "start", spec, operator_actor,
            plan_key="review-refreshed", revision_id=target_id,
            revision_digest=target_digest, expected_review_generation=review_generation,
        ))
        if _accepted(refreshed, "start"):
            plan_id = _field(refreshed[1], "plan_id")
            generation = _field(refreshed[1], "generation", "plan_generation")
            token = _field(refreshed[1], "claim_token")
            for index in range(12):
                advanced = invoke("execution", _operation(
                    case_id, f"refreshed-advance-{index}", "advance", spec,
                    operator_actor, plan_id=plan_id, generation=generation,
                    claim_token=token, worker_id="refreshed-worker",
                ))
                if str(_field(advanced[1], "status") or "").lower() in {"complete", "completed"}:
                    break
            refreshed_status = invoke("execution", _operation(case_id, "refreshed-status", "status", spec, operator_actor, plan_id=plan_id))
            refreshed_record = _record(refreshed_status[1])
            attestation = refreshed_record.get("attestation")
            status_value = str(refreshed_record.get("status", "")).lower()
            attestation_ok = _accepted(refreshed_status, "status") and status_value in {"complete", "completed"} and isinstance(attestation, dict) and target_digest in _json_text(attestation) and str(review_generation) in _json_text(attestation)
        else:
            attestation_ok = False
        review_invalidation_ok = _accepted(reject_after_run, "review") and _rejected(stale_attestation_promotion) and _accepted(reapprove_after_run, "review") and attestation_ok
        review_invalidation_evidence = {
            "reject": reject_after_run[1], "stale_promotion": stale_attestation_promotion[1],
            "reapprove": reapprove_after_run[1], "refreshed": refreshed[1],
        }
    state["EXEC-ATTESTATION"] = {"passed": attestation_ok and review_invalidation_ok, "evidence": f"status={status_value}; attestation={attestation}; review_invalidation={review_invalidation_evidence}"}

    stale_policy_rejected = True
    if spec["scenario"] == "policy_change":
        policy_path = project / ".deepcode" / "traceability_policy.json"
        changed_policy = load_json(policy_path)
        changed_policy["policy_version"] += 1
        policy_path.write_text(json.dumps(changed_policy, sort_keys=True) + "\n", encoding="utf-8")
        stale_policy = invoke("revision", _operation(case_id, "stale-policy-promote", "promote", spec, code_actor, revision_id=target_id, revision_digest=target_digest, expected_review_generation=review_generation, expected_head_revision_id=None, execution_store="../traceability_execution", plan_id=plan_id, plan_generation=generation))
        stale_policy_rejected = _rejected(stale_policy)
        reject_review = invoke("revision", _operation(case_id, "policy-reject", "review", spec, code_actor, revision_id=target_id, revision_digest=target_digest, role="maintainer", decision="reject", expected_review_generation=review_generation, note="refresh policy generation"))
        reject_generation = _field(reject_review[1], "review_generation")
        approve_review = invoke("revision", _operation(case_id, "policy-approve", "review", spec, code_actor, revision_id=target_id, revision_digest=target_digest, role="maintainer", decision="approve", expected_review_generation=reject_generation, note="reapproved under current policy"))
        review_generation = _field(approve_review[1], "review_generation")
        # The old attestation is intentionally stale after a review generation change.
        refreshed = invoke("execution", _operation(case_id, "policy-plan", "start", spec, operator_actor, plan_key="policy-refresh", revision_id=target_id, revision_digest=target_digest, expected_review_generation=review_generation))
        if _accepted(refreshed, "start"):
            plan_id = _field(refreshed[1], "plan_id")
            generation = _field(refreshed[1], "generation", "plan_generation")
            token = _field(refreshed[1], "claim_token")
            for index in range(12):
                advanced = invoke("execution", _operation(case_id, f"policy-advance-{index}", "advance", spec, operator_actor, plan_id=plan_id, generation=generation, claim_token=token, worker_id="policy-worker"))
                if str(_field(advanced[1], "status") or "").lower() in {"complete", "completed"}:
                    break

    promote_doc = _operation(case_id, "promote", "promote", spec, code_actor, revision_id=target_id, revision_digest=target_digest, expected_review_generation=review_generation, expected_head_revision_id=None, execution_store="../traceability_execution", plan_id=plan_id, plan_generation=generation)
    wrong_plan_promotion = invoke("revision", _operation(
        case_id, "promote-wrong-plan", "promote", spec, code_actor,
        revision_id=target_id, revision_digest=target_digest,
        expected_review_generation=review_generation, expected_head_revision_id=None,
        execution_store="../traceability_execution", plan_id=f"{plan_id}-unknown",
        plan_generation=generation,
    ))
    wrong_generation_promotion = invoke("revision", _operation(
        case_id, "promote-wrong-generation", "promote", spec, code_actor,
        revision_id=target_id, revision_digest=target_digest,
        expected_review_generation=review_generation, expected_head_revision_id=None,
        execution_store="../traceability_execution", plan_id=plan_id,
        plan_generation=(generation + 100 if isinstance(generation, int) else 100),
    ))
    if spec["scenario"] == "cancel_fence":
        with ThreadPoolExecutor(max_workers=4) as pool:
            promotion_calls = list(pool.map(lambda _: invoke("revision", promote_doc), range(4)))
        promotion, promotion_retry = promotion_calls[0], promotion_calls[1]
        concurrent_promotion_ok = all(_accepted(call, "promote") for call in promotion_calls) and all(
            call[1] == promotion[1] for call in promotion_calls
        )
    else:
        promotion = invoke("revision", promote_doc)
        promotion_retry = invoke("revision", promote_doc)
        concurrent_promotion_ok = True
    promotion_text = _json_text(promotion[1])
    promotion_preconditions_ok = _rejected(before_execution) and _rejected(wrong_plan_promotion) and _rejected(wrong_generation_promotion) and stale_policy_rejected
    if spec.get("blocked"):
        promotion_ok = promotion_preconditions_ok and _rejected(promotion) and _rejected(promotion_retry)
    else:
        promotion_ok = promotion_preconditions_ok and concurrent_promotion_ok and _accepted(promotion, "promote") and _accepted(promotion_retry, "promote") and promotion[1] == promotion_retry[1] and isinstance(target_id, str) and target_id in promotion_text
    state["PROMOTION-GATE-CAS"] = {"passed": promotion_ok, "evidence": f"early={before_execution[1]}; wrong_plan={wrong_plan_promotion[1]}; wrong_generation={wrong_generation_promotion[1]}; final={promotion[1]}; retry_equal={promotion[1] == promotion_retry[1]}; concurrent={concurrent_promotion_ok}"}

    corruption_observed = False
    if spec["scenario"] == "corrupt_reconcile" and revision_store.exists():
        victim = next((path for path in revision_store.rglob("traceability_graph.json") if marker in path.read_text(encoding="utf-8", errors="ignore")), None)
        corruption_observed = victim is not None
        if victim:
            victim.write_bytes(victim.read_bytes() + b"\nCORRUPT\n")
    if "CROSS-RECONCILE" in selected:
        rev_reconcile = invoke("revision", _operation(case_id, "revision-reconcile", "reconcile", spec, science_actor))
        exec_reconcile = invoke("execution", _operation(case_id, "execution-reconcile", "reconcile", spec, operator_actor))
        reconcile_text = _json_text(rev_reconcile[1]).lower()
        execution_reconcile_text = _json_text(exec_reconcile[1]).lower()
        if spec["scenario"] == "corrupt_reconcile":
            corrupt_id_visible = isinstance(target_id, str) and target_id.lower() in reconcile_text
            affected_plan_visible = isinstance(plan_id, str) and plan_id.lower() in execution_reconcile_text
            invalidated_status = invoke("execution", _operation(case_id, "invalidated-plan-status", "status", spec, operator_actor, plan_id=plan_id))
            invalidated_record = _record(invalidated_status[1])
            stale_after_corruption = invoke("execution", _operation(
                case_id, "stale-after-corruption", "advance", spec, operator_actor,
                plan_id=plan_id, generation=generation, claim_token=token,
                worker_id="corrupt-stale-worker",
            ))
            corruption_observed = (
                corruption_observed
                and corrupt_id_visible
                and affected_plan_visible
                and str(invalidated_record.get("status", "")).lower() in {"invalid", "invalidated", "corrupt", "canceled"}
                and _rejected(stale_after_corruption)
            )
        else:
            corruption_observed = True
        reconcile_ok = _accepted(rev_reconcile, "reconcile") and _accepted(exec_reconcile, "reconcile") and corruption_observed
        state["CROSS-RECONCILE"] = {"passed": reconcile_ok, "evidence": f"revision={rev_reconcile[1]}; execution={exec_reconcile[1]}"}
    state["SCOPE-NONDISCLOSURE"] = {"passed": scope_ok, "evidence": f"wrong_revision_scope={wrong_scope[1]}; wrong_plan_scope={wrong_plan[1]}; absolute_response={_contains_absolute(wrong_scope[1]) or _contains_absolute(wrong_plan[1])}"}

    serialized = _json_text(evidence["operations"])
    other_ids = {item for other_id, other in CASE_SPECS.items() if other_id != case_id for item in other["ids"]}
    clean = canary not in serialized and not any(item in serialized for item in other_ids)
    state["NO-CONTAMINATION"] = {"passed": clean, "evidence": "no sibling canary or other-case identifiers in responses"}

    if "ORDINARY-COMPAT" in selected:
        ordinary = workspace / "ordinary"
        shutil.copytree(case_directory(case_id) / "assets" / "project", ordinary)
        (ordinary / ".deepcode" / "traceability_request.json").unlink()
        script = "import asyncio,json;from pathlib import Path;from workflows.code_implementation_workflow import CodeImplementationWorkflow;print(json.dumps(asyncio.run(CodeImplementationWorkflow(require_verification=True)._verify_generated_code(Path(" + repr(str(ordinary)) + "))),default=str))"
        ordinary_process = run_process([sys.executable, "-c", script], cwd=repository, env=env)
        evidence["processes"].append(ordinary_process.to_json())
        ordinary_ok = build_gates_passed and ordinary_process.exit_code == 0 and not ordinary_process.timed_out and "claim-traceability" not in ordinary_process.stdout
        state["ORDINARY-COMPAT"] = {"passed": ordinary_ok, "evidence": f"exit={ordinary_process.exit_code}; trace_command={'claim-traceability' in ordinary_process.stdout}"}

    assertions = [
        {"id": assertion_id, "passed": bool(state[assertion_id]["passed"]), "points": weight if state[assertion_id]["passed"] else 0, "max_points": weight, "evidence": str(state[assertion_id]["evidence"])[:3000]}
        for assertion_id, weight in weights.items()
    ]
    score = sum(item["points"] for item in assertions)
    safety_violation = _case_safety_violation(selected, scope_ok=scope_ok, stale_fenced=stale_fenced)
    if safety_violation:
        score = min(score, 35)
        evidence["safety_ceiling"] = "case-local 35-point ceiling: cross-scope disclosure or stale execution mutation observed"
    dimensions = {name: sum(item["points"] for item in assertions if item["id"] in ids) for name, ids in DIMENSION_IDS.items()}
    capability_observed = {
        "capsule": capsule_valid,
        "revision_entry": base[0].exit_code not in {1, 127} and base[1] is not None,
        "execution_entry": start[0].exit_code not in {1, 127} and start[1] is not None,
    }
    result = {
        "case_id": case_id, "score": score, "valid_artifact": any(capability_observed.values()),
        "capability_observed": capability_observed, "assertions": assertions,
        "scored_assertion_ids": list(weights),
        "excluded_assertion_ids": [key for key in PUBLIC_WEIGHTS if key not in weights],
        "dimensions": dimensions,
        "major_errors": [f"{item['id']}: {item['evidence']}" for item in assertions if not item["passed"]],
        "evidence": evidence,
    }
    if score == 0:
        result["behavioral_zero_reason"] = "delivery/build and fixture were valid; required product behavior was not observed"
    shutil.rmtree(workspace, ignore_errors=True)
    return result
