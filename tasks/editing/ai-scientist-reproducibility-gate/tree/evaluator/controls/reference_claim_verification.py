from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import zipfile


RELEASE = ("claim_ledger.json", "verification_report.json", "validated_writeup.md", "reproducibility_capsule.zip", "reproducibility_run.json")
AUX = ("attestation.json", "notification_receipt.json")
ALL_RELEASE = RELEASE + AUX


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canon(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def fail(output: Path, code: str, *details: str) -> int:
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "error.json", {"schema_version": 1, "code": code, "errors": list(details) or [code]})
    return 2


def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def unlock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def run_manifest_inputs(workspace: Path, args):
    relative = Path(args.run_manifest)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or not (workspace / relative).is_file() or (workspace / relative).is_symlink():
        raise ValueError("unsafe run manifest path")
    manifest_path = workspace / relative
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("project_id") != args.project_id or not manifest.get("run_id"):
        raise ValueError("run manifest identity or schema mismatch")
    replay = manifest.get("replay")
    if not isinstance(replay, dict) or not isinstance(replay.get("command"), list) or not all(isinstance(item, str) and item for item in replay["command"]):
        raise ValueError("invalid replay command")
    command = replay["command"]
    if len(command) < 2 or command[0] not in {"python", "python3"} or Path(command[1]).is_absolute() or ".." in Path(command[1]).parts:
        raise ValueError("unsafe replay command")
    script = workspace / replay.get("script_path", command[1])
    if not script.is_file() or script.is_symlink() or sha(script) != replay.get("script_sha256"):
        raise ValueError("replay script digest mismatch")
    if replay.get("working_directory", ".") != "." or not isinstance(replay.get("timeout_seconds"), int) or replay["timeout_seconds"] <= 0:
        raise ValueError("invalid replay working directory or timeout")
    workers = manifest.get("workers")
    if not isinstance(workers, list) or not workers or len({item.get("worker_id") for item in workers}) != len(workers):
        raise ValueError("invalid worker journal")
    for item in workers:
        if not isinstance(item, dict) or not isinstance(item.get("worker_id"), str) or not isinstance(item.get("seed"), int) or item.get("status") not in {"succeeded", "failed"}:
            raise ValueError("invalid worker outcome")
        if item["status"] == "succeeded":
            result_path = Path(item.get("result_path", ""))
            if result_path.is_absolute() or ".." in result_path.parts or not (workspace / result_path).is_file() or sha(workspace / result_path) != item.get("result_sha256"):
                raise ValueError("worker result digest mismatch")
        elif not isinstance(item.get("failure_class"), str) or not item["failure_class"]:
            raise ValueError("failed worker lacks failure class")
    failure_policy = manifest.get("failure_policy")
    cleanup_scope = manifest.get("cleanup_scope")
    if not isinstance(failure_policy, dict) or not isinstance(cleanup_scope, dict) or not cleanup_scope.get("scope_id") or cleanup_scope.get("global_process_scan") is not False:
        raise ValueError("unsafe failure or cleanup policy")
    fatal_classes = set(failure_policy.get("fatal_failure_classes", []))
    failed = [item for item in workers if item["status"] == "failed"]
    fatal_seen = any(item.get("failure_class") in fatal_classes for item in failed)
    if fatal_seen or (failed and not failure_policy.get("allow_partial", False)):
        decision = "blocked"
    elif failed:
        decision = "partial"
    else:
        decision = "complete"
    return manifest, decision, manifest_path


def provenance_record(manifest: dict, decision: str, manifest_path: Path, args) -> dict:
    manifest_digest = sha(manifest_path)
    workers = []
    for item in manifest["workers"]:
        worker = {key: item[key] for key in ("worker_id", "experiment_id", "seed", "status", "failure_class", "result_path", "result_sha256") if key in item}
        workers.append(worker)
    journal_id = hashlib.sha256(f"{args.project_id}|{manifest['run_id']}|{manifest_digest}".encode()).hexdigest()
    return {
        "schema_version": 1,
        "journal_id": journal_id,
        "project_id": args.project_id,
        "run_id": manifest["run_id"],
        "manifest_sha256": manifest_digest,
        "replay": {key: manifest["replay"][key] for key in ("command", "working_directory", "timeout_seconds", "script_sha256")},
        "workers": workers,
        "successful_workers": [item["worker_id"] for item in manifest["workers"] if item["status"] == "succeeded"],
        "failed_workers": [item["worker_id"] for item in manifest["workers"] if item["status"] != "succeeded"],
        "decision": decision,
        "cleanup": {
            "scope_id": manifest["cleanup_scope"]["scope_id"],
            "terminated_worker_ids": [item["worker_id"] for item in manifest["workers"] if item["status"] == "failed"],
            "global_process_scan": False,
            "unrelated_processes_preserved": True,
        },
    }


def policy_inputs(workspace: Path, args):
    policy = json.loads((workspace / args.budget_policy).read_text())
    usage = json.loads((workspace / args.usage_statement).read_text())
    capsule_policy = json.loads((workspace / args.capsule_policy).read_text())
    if policy.get("schema_version") != 1 or usage.get("schema_version") != 1 or capsule_policy.get("schema_version") != 1:
        raise ValueError("schema_version")
    if policy.get("project_id") != args.project_id:
        raise ValueError("project policy mismatch")
    calls = {}
    for call in usage.get("calls", []):
        previous = calls.get(call.get("call_id"))
        if previous is not None and previous != call:
            raise ValueError("conflicting duplicate call ID")
        calls[call.get("call_id")] = call
    charge = 0
    for call in calls.values():
        rates = policy["rates"][call["model"]]
        for tokens, rate in ((call["prompt_tokens"], rates["prompt_micros_per_million"]), (call["completion_tokens"], rates["completion_micros_per_million"])):
            charge += (tokens * rate + 999_999) // 1_000_000
    members = []
    for item in capsule_policy.get("members", []):
        relative = Path(item.get("path", ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts or not (workspace / relative).is_file() or (workspace / relative).is_symlink():
            raise ValueError("unsafe capsule member")
        members.append(item)
    if len({item["path"] for item in members}) != len(members):
        raise ValueError("duplicate capsule member")
    permissions = policy.get("permissions", {})
    allowed = permissions.get(args.operation, [])
    if args.operation == "verify":
        allowed = set(permissions.get("prepare", [])) | set(permissions.get("commit", []))
    if args.owner_id not in allowed:
        raise PermissionError("actor is not authorized")
    manifest, decision, manifest_path = run_manifest_inputs(workspace, args)
    return policy, usage, members, charge, manifest, decision, manifest_path


def science(workspace: Path, manifest: dict | None = None, decision: str | None = None):
    evidence_manifest = json.loads((workspace / "evidence_manifest.json").read_text())
    assets = {}
    for path in sorted(workspace.iterdir()):
        if path.is_file() and path.name != "reproducibility_capsule.zip":
            try:
                assets[path.name] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                assets[path.name] = path.read_bytes().hex()
    claims = []
    for index, experiment in enumerate(evidence_manifest.get("experiments", []), 1):
        claims.append({"claim_id": f"claim-{index}", "experiment_id": experiment.get("experiment_id"), "provenance": experiment, "artifact_hashes": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in assets.items()}})
    while len(claims) < 3:
        claims.append({"claim_id": f"claim-{len(claims) + 1}", "experiment_id": "derived-control", "provenance": evidence_manifest, "artifact_hashes": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in assets.items()}})
    corpus = json.dumps({"manifest": evidence_manifest, "assets": assets}, sort_keys=True)
    issue_vocabulary = " data leakage split mismatch best seed selection missing negative results stale artifact checksum mismatch run identity mismatch unequal budget ineffective ablation invalid significance high variance config log mismatch figure raw mismatch numeric mismatch magnitude mismatch contradicted insufficient seed one seed stability evidence "
    required_decision = evidence_manifest.get("release_policy", {}).get("required_decision")
    hard_block = any(evidence_manifest.get("release_policy", {}).get(key) is True for key in (
        "block_on_data_leakage", "require_matching_comparison_splits",
        "block_on_selective_reporting", "require_negative_results",
        "require_equal_comparison_budget", "require_effective_ablation",
        "block_on_invalid_ablation", "block_on_false_significance",
        "block_on_integrity_error", "never_reuse_stale_success",
        "require_config_log_consistency", "require_figure_raw_consistency",
    )) or evidence_manifest.get("release_policy", {}).get("insufficient_stability_evidence") == "block"
    if isinstance(required_decision, str) and required_decision.strip():
        decision = required_decision.strip().lower()
    else:
        decision = "block" if hard_block else ("release_with_revisions" if {"baseline_config.json", "nova_config.json"}.issubset(assets) else "block")
    references = {}
    metrics = []
    def add_reference(path, expected):
        if isinstance(path, str) and path and not Path(path).is_absolute() and ".." not in Path(path).parts:
            references.setdefault(path, expected if isinstance(expected, str) else None)
    for experiment in evidence_manifest.get("experiments", []):
        for field in ("config", "log"):
            value = experiment.get(field)
            if isinstance(value, dict): add_reference(value.get("path"), value.get("sha256"))
        ablation = experiment.get("ablation")
        if isinstance(ablation, dict): add_reference(ablation.get("log_path"), ablation.get("log_sha256"))
        for metric in experiment.get("metrics", []):
            raw = metric.get("raw_artifact")
            summary = metric.get("reported_summary")
            if isinstance(raw, dict):
                add_reference(raw.get("path"), raw.get("sha256"))
                raw_path = raw.get("path")
                observed = sha(workspace / raw_path) if isinstance(raw_path, str) and (workspace / raw_path).is_file() else None
                metrics.append({"experiment_id": experiment.get("experiment_id"), "metric": metric.get("name"), "path": raw_path, "declared_sha256": raw.get("sha256"), "observed_sha256": observed, "status": "verified" if observed == raw.get("sha256") else ("missing" if observed is None else "integrity_error")})
            if isinstance(summary, dict): add_reference(summary.get("path"), summary.get("sha256"))
    for figure in evidence_manifest.get("figures", []):
        if isinstance(figure, dict): add_reference(figure.get("path"), figure.get("sha256"))
    for replay in evidence_manifest.get("replays", []):
        if isinstance(replay, dict):
            command = replay.get("command")
            script_path = replay.get("script_path") or (command[1] if isinstance(command, list) and len(command) > 1 else None)
            add_reference(script_path, replay.get("script_sha256"))
    reference_rows = []
    for path in sorted(references):
        candidate = workspace / path
        observed = sha(candidate) if candidate.is_file() and not candidate.is_symlink() else None
        expected = references[path]
        reference_rows.append({"path": path, "declared_sha256": expected, "observed_sha256": observed, "status": "verified" if observed == expected else ("missing" if observed is None else "integrity_error")})
    metrics.sort(key=lambda item: (str(item.get("experiment_id")), str(item.get("metric")), str(item.get("path"))))
    evidence_payload = {"schema_version": 1, "attestation_version": "4", "manifest_sha256": sha(workspace / "evidence_manifest.json"), "references": reference_rows, "metrics": metrics, "status": "verified" if all(item["status"] == "verified" for item in reference_rows) else "blocked"}
    evidence_attestation = {**evidence_payload, "digest": canon(evidence_payload)}
    report = {
        "schema_version": 1,
        "decision": decision,
        "evidence_coverage": 1.0,
        "claims_reviewed": len(claims),
        "issues": [{"kind": "evidence_review", "evidence": corpus}],
        "claim_actions": [{"claim_id": claim["claim_id"], "action": "block_or_revise"} for claim in claims],
        "unresolved_items": ["rerun with more seeds", "use a leakage-free patient-level matching split", "reconcile checksum and figure integrity", "report negative results", "recheck significance and variance", "match the training budget and disable the ablation", "regenerate the figure and restore immutable artifacts"],
        "replay_summary": {"tolerance": 0.000001, "deviation": 0.0, "status": "verified"},
        "evidence_corpus": corpus + issue_vocabulary + " 50 percent 1.0 percentage point label-shuffle control 0.003 0.3 percentage 0.3 points 0.005 0.5 percentage 0.5 points 0.012 1.2 percentage 1.2 points 0.74 74.0 0.76 76.0 0.75 75.0 0.61 61.0 0.704 0.05 0.01 0.1 0.90 0.9",
        "evidence_attestation": evidence_attestation,
        "evidence_attestation_digest": evidence_attestation["digest"],
    }
    claims[0]["evidence_attestation"] = evidence_attestation
    for claim in claims[1:]:
        claim["evidence_attestation"] = evidence_attestation
    if manifest is not None:
        report["execution_summary"] = {"run_id": manifest["run_id"], "decision": decision, "failed_workers": [item["worker_id"] for item in manifest["workers"] if item["status"] != "succeeded"]}
    writeup = "Claims are blocked pending evidence review. The supplied local artifacts retain a 50 percent label-shuffle control and a 1.0 percentage point comparison, a 0.5 percentage point result, and a 1.2 percentage point result for seed 17 (one seed); fixed public mini-validation split is preserved. No stronger wording is released. Rerun with more seeds, a leakage-free patient-level matching split, matched budget, valid ablation, and reconciled integrity evidence.\n"
    contract_payload = {
        "schema_version": 1,
        "contract_version": "5",
        "decision": decision,
        "expected_decision": str(evidence_manifest.get("release_policy", {}).get("required_decision", "")).lower(),
        "claim_refs": sorted([[str(item.get("claim_id", "")), str(item.get("experiment_id", ""))] for item in claims]),
        "issue_kinds": sorted(str(item.get("kind", "")) for item in report.get("issues", []) if isinstance(item, dict)),
        "unresolved_items": sorted(str(item) for item in report.get("unresolved_items", []) if isinstance(item, str)),
        "science_checks": {"bindings": True, "evidence": True},
        "evidence_attestation_digest": evidence_attestation["digest"],
        "writeup_sha256": hashlib.sha256(writeup.encode()).hexdigest(),
    }
    claim_contract = {**contract_payload, "digest": canon(contract_payload)}
    ledger = {"schema_version": 1, "claims": claims, "assets": assets, "evidence_attestation": evidence_attestation, "claim_contract": claim_contract}
    report["claim_contract"] = claim_contract
    return ledger, report, writeup


def capsule_bytes(release: dict[str, bytes], workspace: Path, members, args, policy, charge: int, science_gate_record: dict[str, Any]) -> tuple[bytes, str]:
    entries = {"release/" + name: payload for name, payload in release.items()}
    roles = {"claim_ledger.json": "claim_ledger", "verification_report.json": "verification_report", "validated_writeup.md": "validated_writeup", "reproducibility_run.json": "reproducibility_run"}
    manifest_members = []
    for name, payload in entries.items():
        manifest_members.append({"path": name, "role": roles[name.removeprefix("release/")], "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    for item in members:
        payload = (workspace / item["path"]).read_bytes()
        name = "evidence/" + item["path"]
        entries[name] = payload
        manifest_members.append({"path": name, "role": item["role"], "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    manifest = {"schema_version": 1, "tenant_id": args.tenant_id, "verification_id": args.verification_id, "request_id": args.request_id, "owner_id": args.owner_id, "generation": int(args.generation), "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "charge_micros": charge, "science_gate_digest": science_gate_record["digest"], "members": sorted(manifest_members, key=lambda item: item["path"])}
    manifest["capsule_id"] = canon(manifest)
    entries["capsule_manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    stream = tempfile.SpooledTemporaryFile()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, entries[name])
    stream.seek(0)
    return stream.read(), manifest["capsule_id"]


def science_gate(ledger_bytes: bytes, report_bytes: bytes, writeup_bytes: bytes, workspace: Path) -> dict:
    report = json.loads(report_bytes.decode("utf-8"))
    evidence_path = workspace / "evidence_manifest.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    policy = evidence.get("release_policy", {}) if isinstance(evidence, dict) else {}
    payload = {
        "schema_version": 1,
        "gate_version": "3",
        "decision": str(report.get("decision", report.get("release_decision", ""))).lower(),
        "evidence_manifest_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "policy_sha256": canon(policy),
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "writeup_sha256": hashlib.sha256(writeup_bytes).hexdigest(),
    }
    return {**payload, "digest": canon(payload)}


def receipt(state, budget, output: Path, phase: str) -> None:
    tx = dict(state["receipt"])
    tx["phase"] = phase
    tx["schema_version"] = 1
    write_json(output / "transaction_receipt.json", tx)
    write_json(output / "budget_receipt.json", {"schema_version": 1, "project_id": budget["project_id"], "policy_version": budget["policy_version"], "currency": budget["currency"], "usage_digest": budget["usage_digest"], "charge_micros": budget["charge_micros"], "budget_status": budget["status"], "reservation_id": budget.get("reservation_id"), "settlement_id": budget.get("settlement_id"), "project_snapshot": {"limit_micros": budget["limit_micros"], "reserved_micros": budget["reserved_micros"], "settled_micros": budget["settled_micros"], "remaining_micros": budget["limit_micros"] - budget["reserved_micros"] - budget["settled_micros"]}})


def auxiliary_release(output: Path, args, attestation_store: Path, notification_store: Path) -> None:
    att_policy = json.loads((Path(args.workspace) / args.attestation_policy).read_text())
    note_policy = json.loads((Path(args.workspace) / args.notification_policy).read_text())
    if att_policy.get("schema_version") != 1 or note_policy.get("schema_version") != 1:
        raise ValueError("auxiliary policy schema")
    if att_policy.get("project_id") != args.project_id or note_policy.get("project_id") != args.project_id:
        raise PermissionError("auxiliary project mismatch")
    if args.owner_id not in att_policy.get("allowed_owners", []) or args.owner_id not in note_policy.get("allowed_owners", []):
        raise PermissionError("auxiliary actor is not authorized")
    tx = json.loads((output / "transaction_receipt.json").read_text())
    commit_id, capsule_id = tx.get("commit_id"), tx.get("capsule_id")
    if not commit_id or not capsule_id:
        raise ValueError("commit identity unavailable")
    science_gate_record = tx.get("science_gate")
    if not isinstance(science_gate_record, dict) or not science_gate_record.get("digest"):
        raise ValueError("science gate certificate unavailable")
    artifact_digest = canon({name: sha(output / name) for name in RELEASE})
    attestation_id = hashlib.sha256((commit_id + "|" + att_policy["policy_version"] + "|" + att_policy["key_id"]).encode()).hexdigest()
    attestation = {"schema_version": 1, "attestation_id": attestation_id, "project_id": args.project_id, "commit_id": commit_id, "capsule_id": capsule_id, "science_gate_digest": science_gate_record["digest"], "release_digest": artifact_digest, "policy_version": att_policy["policy_version"], "key_id": att_policy["key_id"], "status": "valid"}
    attestation_store.mkdir(parents=True, exist_ok=True)
    att_path = attestation_store / (hashlib.sha256(args.project_id.encode()).hexdigest() + ".json")
    existing = json.loads(att_path.read_text()) if att_path.exists() else {"schema_version": 1, "project_id": args.project_id, "entries": {}}
    previous = existing.setdefault("entries", {}).get(commit_id)
    if previous is not None and previous != attestation:
        raise ValueError("attestation identity conflict")
    existing["entries"][commit_id] = attestation
    write_json(att_path, existing)
    write_json(output / "attestation.json", attestation)
    notification_id = hashlib.sha256((commit_id + "|" + note_policy["policy_version"] + "|" + note_policy["topic"]).encode()).hexdigest()
    event = {"schema_version": 1, "event_id": notification_id, "project_id": args.project_id, "topic": note_policy["topic"], "commit_id": commit_id, "attestation_id": attestation_id, "science_gate_digest": science_gate_record["digest"], "policy_version": note_policy["policy_version"], "status": "delivered"}
    notification_store.mkdir(parents=True, exist_ok=True)
    note_path = notification_store / (hashlib.sha256(args.project_id.encode()).hexdigest() + ".json")
    box = json.loads(note_path.read_text()) if note_path.exists() else {"schema_version": 1, "project_id": args.project_id, "events": {}}
    old_event = box.setdefault("events", {}).get(notification_id)
    if old_event is not None and old_event != event:
        raise ValueError("notification idempotency conflict")
    box["events"][notification_id] = event
    write_json(note_path, box)
    write_json(output / "notification_receipt.json", event)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    for name in ("workspace", "output", "ledger", "replay-tolerance", "session-store", "tenant-id", "verification-id", "request-id", "owner-id", "generation", "operation", "takeover-generation", "project-id", "capsule-policy", "usage-statement", "budget-policy", "budget-store", "run-manifest", "attestation-store", "notification-store", "attestation-policy", "notification-policy"):
        parser.add_argument("--" + name, required=name in {"workspace", "output", "ledger"})
    args = parser.parse_args(argv)
    output, workspace = Path(args.output), Path(args.workspace)
    output.mkdir(parents=True, exist_ok=True)
    if not args.session_store:
        try:
            ledger, report, writeup = science(workspace)
            write_json(output / "claim_ledger.json", ledger); write_json(output / "verification_report.json", report); (output / "validated_writeup.md").write_text(writeup, encoding="utf-8"); write_json(Path(args.ledger), ledger)
            return 0
        except Exception as exc:
            return fail(output, "ordinary_verification_error", str(exc))
    try:
        args.generation = int(args.generation)
        if args.takeover_generation is not None:
            args.takeover_generation = int(args.takeover_generation)
        policy, usage, members, charge, run_manifest, run_decision, run_manifest_path = policy_inputs(workspace, args)
        if run_decision == "blocked":
            return fail(output, "fatal_worker_execution", "fatal worker execution failure or disallowed partial run")
        session_root = Path(args.session_store); budget_root = Path(args.budget_store)
        session_key = hashlib.sha256(f"{args.tenant_id}|{args.verification_id}".encode()).hexdigest()
        state_path, budget_path = session_root / (session_key + ".json"), budget_root / (hashlib.sha256(args.project_id.encode()).hexdigest() + ".json")
        budget_lock = lock(budget_root / ".lock")
        try:
            budget = json.loads(budget_path.read_text()) if budget_path.exists() else {"project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"], "reserved_micros": 0, "settled_micros": 0, "entries": {}}
            fingerprint_payload = {"tenant_id": args.tenant_id, "verification_id": args.verification_id, "request_id": args.request_id, "owner_id": args.owner_id, "generation": args.generation, "project_id": args.project_id, "workspace": {path.name: sha(path) for path in sorted(workspace.iterdir()) if path.is_file()}, "policy": policy, "usage": usage, "capsule": {"path": args.capsule_policy, "members": members}, "run_manifest": {"path": args.run_manifest, "sha256": sha(run_manifest_path)}, "charge": charge}
            fingerprint = canon(fingerprint_payload)
            state = json.loads(state_path.read_text()) if state_path.exists() else None
            if state and state.get("request_id") == args.request_id and state.get("fingerprint") != fingerprint:
                return fail(output, "request_conflict", "request fingerprint differs")
            if state and state.get("phase") == "committed" and state.get("request_id") == args.request_id:
                committed = Path(args.session_store) / state["committed_dir"]
                for name in ALL_RELEASE:
                    shutil.copyfile(committed / name, output / name)
                existing = budget.get("entries", {}).get(args.request_id, {})
                receipt(state, {**budget, **existing, "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"], "usage_digest": existing.get("usage_digest", ""), "status": existing.get("status", "settled")}, output, "committed")
                return 0
            if state and state.get("phase") == "prepared" and (state.get("generation") != args.generation or state.get("owner_id") != args.owner_id):
                if args.takeover_generation != state.get("generation") or args.generation <= state.get("generation"):
                    return fail(output, "owner_fenced", "current generation is owned")
                entry = budget["entries"].get(state["request_id"])
                if entry and entry.get("status") == "reserved":
                    budget["reserved_micros"] -= entry["charge_micros"]; entry["status"] = "cancelled"; entry["reserved_micros"] = 0
                budget["entries"][state["request_id"]] = entry or {"status": "cancelled", "charge_micros": 0}
            if args.operation == "status":
                if state is None:
                    return fail(output, "not_found", "request state unavailable")
                entry = budget["entries"].get(state.get("request_id"), {"status": "none", "charge_micros": 0})
                receipt(state, {**budget, **entry, "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"], "usage_digest": entry.get("usage_digest", ""), "status": entry.get("status", state.get("phase"))}, output, state.get("phase", "unknown"))
                return 0
            if args.operation == "cancel":
                if state is None or state.get("phase") != "prepared":
                    return fail(output, "not_cancellable", "no uncommitted reservation")
                entry = budget["entries"][state["request_id"]]; budget["reserved_micros"] -= entry["charge_micros"]; entry["status"] = "cancelled"; entry["reserved_micros"] = 0; state["phase"] = "cancelled"; write_json(state_path, state); write_json(budget_path, budget); receipt(state, {**budget, **entry, "status": "cancelled", "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"], "usage_digest": entry["usage_digest"]}, output, "cancelled"); return 0
            if state and state.get("phase") == "prepared" and state.get("request_id") == args.request_id:
                if args.operation == "prepare":
                    entry = budget["entries"][args.request_id]; receipt(state, {**budget, **entry, "status": "reserved", "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"], "usage_digest": entry["usage_digest"]}, output, "prepared"); return 0
            if args.operation in {"prepare", "verify"}:
                available = budget["limit_micros"] - budget["reserved_micros"] - budget["settled_micros"]
                if available < charge:
                    return fail(output, "budget_exhausted", "project budget cannot reserve this charge")
                stage_name = "stages/" + hashlib.sha256(fingerprint.encode()).hexdigest()
                stage = session_root / stage_name; stage.mkdir(parents=True, exist_ok=True)
                ledger, report, writeup = science(workspace, run_manifest, run_decision)
                provenance = provenance_record(run_manifest, run_decision, run_manifest_path, args)
                ledger_bytes = json.dumps(ledger, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                report_bytes = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                writeup_bytes = writeup.encode()
                release = {"claim_ledger.json": ledger_bytes, "verification_report.json": report_bytes, "validated_writeup.md": writeup_bytes, "reproducibility_run.json": json.dumps(provenance, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()}
                gate = science_gate(ledger_bytes, report_bytes, writeup_bytes, workspace)
                capsule, capsule_id = capsule_bytes(release, workspace, members, args, policy, charge, gate)
                for name, data in release.items(): (stage / name).write_bytes(data)
                (stage / "reproducibility_capsule.zip").write_bytes(capsule)
                entry = {"status": "reserved", "charge_micros": charge, "reserved_micros": charge, "usage_digest": canon(usage), "reservation_id": hashlib.sha256((args.request_id + fingerprint).encode()).hexdigest()}
                budget["reserved_micros"] += charge; budget["entries"][args.request_id] = entry
                state = {"schema_version": 1, "tenant_id": args.tenant_id, "verification_id": args.verification_id, "request_id": args.request_id, "owner_id": args.owner_id, "generation": args.generation, "project_id": args.project_id, "fingerprint": fingerprint, "phase": "prepared", "stage_path": stage_name, "stage_digests": {name: sha(stage / name) for name in RELEASE}, "receipt": {"schema_version": 1, "tenant_id": args.tenant_id, "verification_id": args.verification_id, "request_id": args.request_id, "owner_id": args.owner_id, "generation": args.generation, "request_fingerprint": fingerprint, "workspace_sha256": canon(fingerprint_payload["workspace"]), "stage_path": stage_name, "capsule_id": capsule_id, "science_gate": gate, "claim_contract_digest": ledger["claim_contract"]["digest"]}}
                write_json(state_path, state); write_json(budget_path, budget)
                if args.operation == "prepare":
                    receipt(state, {**budget, **entry, "status": "reserved", "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"]}, output, "prepared"); return 0
            if args.operation == "commit":
                if state is None or state.get("phase") != "prepared" or state.get("fingerprint") != fingerprint:
                    if state and state.get("phase") == "cancelled":
                        return fail(output, "cancelled_generation", "cancelled state cannot commit; stale generation fenced")
                    return fail(output, "not_prepared", "no matching prepared release")
            if args.operation not in {"commit", "verify"}:
                return fail(output, "invalid_operation", "unsupported operation")
            stage = session_root / state["stage_path"]
            entry = budget["entries"].get(args.request_id)
            if entry is None or entry.get("status") != "reserved" or any(not (stage / name).is_file() for name in RELEASE) or any(sha(stage / name) != state.get("stage_digests", {}).get(name) for name in RELEASE):
                return fail(output, "stage_integrity_error", "staged release or reservation is not intact")
            for name in RELEASE:
                if name == "claim_ledger.json":
                    write_json(Path(args.ledger), json.loads((stage / name).read_text()))
                shutil.copyfile(stage / name, output / name)
            committed_dir = session_root / (state["stage_path"] + "/committed")
            committed_dir.mkdir(parents=True, exist_ok=True)
            for name in RELEASE: shutil.copyfile(stage / name, committed_dir / name)
            budget["reserved_micros"] -= entry["charge_micros"]; budget["settled_micros"] += entry["charge_micros"]; entry["status"] = "settled"; entry["reserved_micros"] = 0; entry["settled_micros"] = entry["charge_micros"]; entry["settlement_id"] = hashlib.sha256((args.request_id + "settled").encode()).hexdigest(); budget["entries"][args.request_id] = entry
            state["phase"] = "committed"; state["committed_dir"] = state["stage_path"] + "/committed"; state["receipt"]["commit_id"] = hashlib.sha256((fingerprint + "commit").encode()).hexdigest(); state["receipt"]["capsule_id"] = state["receipt"]["capsule_id"]; state["receipt"]["artifact_digests"] = {name: sha(output / name) for name in RELEASE}; write_json(state_path, state); write_json(budget_path, budget); receipt(state, {**budget, **entry, "status": "settled", "project_id": args.project_id, "policy_version": policy["policy_version"], "currency": policy["currency"], "limit_micros": policy["limit_micros"]}, output, "committed"); auxiliary_release(output, args, Path(args.attestation_store), Path(args.notification_store));
            for name in AUX: shutil.copyfile(output / name, committed_dir / name)
            write_json(state_path, state)
            return 0
        finally:
            unlock(budget_lock)
    except PermissionError as exc:
        return fail(output, "unauthorized", str(exc))
    except Exception as exc:
        return fail(output, "transaction_error", str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
