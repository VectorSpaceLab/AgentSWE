"""Export one terminal Claude v2 readiness run without dispatching providers.

The exporter only copies existing run evidence and writes evaluator receipts.
Original broker, Builder, execution, judge, and cleanup bytes remain available
inside the bundle and are revalidated by the shared admission implementation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


PROFILE = "single-dev-two-round-hidden-smoke-v1"
FILES = ("solution.patch", "edit_report.json", "run_report.json")
ROLES = ("public_lower", "hidden_lower", "result_judge", "code_judge")


MAX_NATIVE_RECONNECTS = 40


def _reconnects_recovered(native):
    """Bounded, recorded reconnects are recovery, not a replay.

    A reconnect resumes a stream that disconnected before completion, so no
    Candidate, feedback, hidden result or usage is reused and every attempt stays
    announced in the rollout. The turn must still complete exactly once and the
    recovery must have happened inside that same turn.
    """
    termination = native.get('native_termination') or {}
    count = termination.get('native_retry_announcements', 0)
    if type(count) is not int or count < 0 or count > MAX_NATIVE_RECONNECTS:
        return False
    return not count or termination.get('recovered_in_same_turn') is True


def sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: " + str(path))
    return value


def _is_hash(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


class Writer:
    """Copy raw run files and create content-addressed evaluator receipts."""

    FORBIDDEN = {"builder_direct_provider.toml", "builder_broker_provider.toml",
                 ".env", "auth.json"}

    def __init__(self, run_dir: str | Path, destination: str | Path) -> None:
        self.run = Path(run_dir).resolve(strict=True)
        self.root = Path(destination).resolve()
        self.root.mkdir(parents=True, exist_ok=False)

    def _regular_run_file(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.run / candidate
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self.run)
        except (OSError, ValueError) as exc:
            raise ValueError("missing or foreign raw run evidence: " + str(path)) from exc
        current = self.run
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("raw run evidence contains a symlink: " + str(path))
        if not resolved.is_file() or resolved.name in self.FORBIDDEN:
            raise ValueError("raw run evidence is forbidden or not regular: " + str(path))
        return resolved

    def raw(self, path: str | Path) -> dict[str, str]:
        source = self._regular_run_file(path)
        relative = source.relative_to(self.run)
        destination = self.root / "raw" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        if destination.exists() and destination.read_bytes() != payload:
            raise ValueError("raw evidence changed during export: " + str(source))
        if not destination.exists():
            shutil.copyfile(source, destination)
        _require(source.read_bytes() == payload == destination.read_bytes(),
                 "raw evidence changed during export")
        return {"path": destination.relative_to(self.root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest()}

    def receipt(self, name: str, **fields: Any) -> dict[str, str]:
        if fields.get("run_id", self.run.name) != self.run.name:
            raise ValueError("receipt run identity mismatch")
        if fields.get("owner", "evaluator") != "evaluator":
            raise ValueError("receipt owner mismatch")
        value = {"run_id": self.run.name, "owner": "evaluator", **fields}
        path = self.root / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, indent=2,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        return {"path": path.relative_to(self.root).as_posix(), "sha256": sha(path)}

    def imported_ref(self, reference: dict[str, Any]) -> dict[str, str]:
        _require(isinstance(reference, dict) and _is_hash(reference.get("sha256"))
                 and isinstance(reference.get("path"), str),
                 "external artifact reference malformed")
        _require(sha(reference["path"]) == reference["sha256"],
                 "external artifact changed before export")
        return self.raw(reference["path"])

    def references(self, value: Any) -> Any:
        """Make absolute path/hash references bundle-local without rewriting data."""
        if isinstance(value, dict):
            if (isinstance(value.get("path"), str)
                    and Path(value["path"]).is_absolute()
                    and _is_hash(value.get("sha256"))):
                return {**value, **self.imported_ref(value)}
            return {key: self.references(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.references(child) for child in value]
        return value


def _ledger_ids(before: dict[str, Any], after: dict[str, Any], *, label: str) -> list[str]:
    _require(before.get("protocol") == after.get("protocol") == {
        "model": "deepseek-flash", "reasoning_effort": "high"},
        label + " broker protocol mismatch")
    old = before.get("request_ledger")
    new = after.get("request_ledger")
    _require(isinstance(old, list) and isinstance(new, list) and new[:len(old)] == old,
             label + " broker request ledger is not append-only")
    rows = new[len(old):]
    ids = [row.get("response_id") for row in rows if isinstance(row, dict)]
    _require(rows and len(ids) == len(rows)
             and all(isinstance(request_id, str) and request_id for request_id in ids)
             and len(ids) == len(set(ids)), label + " request identities missing or reused")
    return ids


def _chain_ledger(observed: list[dict[str, Any]], before: dict[str, Any],
                  after: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    """Append one public execution snapshot to the reconstructed broker ledger.

    Every public execution - an accepted round or an attempt that was discarded for an
    evaluator-side infrastructure fault - snapshots the shared public lower broker before
    and after it runs.  Chaining the snapshots in execution order rebuilds the terminal
    ledger row for row, which is what proves the broker served no untracked request.
    """
    rows = after.get("request_ledger")
    _require(isinstance(rows, list)
             and list(before.get("request_ledger") or []) == observed
             and rows[:len(observed)] == observed and len(rows) > len(observed),
             label + " broker snapshot does not continue the observed ledger")
    return list(rows)


def _discarded_public_attempts(lifecycle: Path) -> list[dict[str, Any]]:
    """Public round attempts that were discarded for an infrastructure fault and replayed.

    controller.py:448-456 keeps the whole record plus an archived copy of every public
    round attempt it threw away because the evaluator's own scoring path failed.  The
    round was not consumed and no Candidate, feedback or score survives it, but the
    provider requests it already made are real and stay in the terminal broker ledger,
    so the export has to attribute them rather than pretend the ledger starts at the
    accepted round.
    """
    attempts: list[dict[str, Any]] = []
    for path in sorted(lifecycle.glob("round_*_infrastructure_invalid_attempt_*.json")):
        parts = path.stem.split("_")
        record = read(path)
        dev = record.get("dev")
        _require(isinstance(dev, list) and len(dev) == 1 and isinstance(dev[0], dict)
                 and dev[0].get("case_id") == "dev_001"
                 and dev[0].get("infrastructure_invalid") is True
                 and record.get("accepted") is False
                 and record.get("round_consumed") is False
                 and record.get("classification") == "public_infrastructure_failure"
                 and record.get("ineligible_dev_cases") == ["dev_001"]
                 and record.get("public_inventory") == ["dev_001"],
                 "discarded public attempt did not consume its round")
        archive = lifecycle / "infrastructure_attempts" / (
            "round_" + parts[1] + "_attempt_" + parts[-1])
        _require(archive.is_dir() and not archive.is_symlink(),
                 "discarded public attempt is not archived")
        broker = dev[0].get("broker", {})
        label = "discarded public attempt " + path.name
        attempts.append({
            "round": int(parts[1]), "label": label, "record": path.name,
            "before": broker.get("before", {}), "after": broker.get("after", {}),
            "request_ids": _ledger_ids(broker.get("before", {}), broker.get("after", {}),
                                       label=label)})
    return attempts


def _access(writer: Writer, tree_digest: Any) -> dict[str, Any]:
    def read_json(reference: dict[str, Any]) -> dict[str, Any]:
        path = writer.root / reference["path"]
        _require(sha(path) == reference["sha256"], "bundled artifact changed")
        return read(path)

    def directory(reference: dict[str, Any]) -> Path:
        path = writer.root / reference["path"]
        _require(tree_digest(path) == reference["tree_digest"],
                 "bundled directory changed")
        return path

    return {"run_id": writer.run.name, "read_json": read_json,
            "directory": directory}


def _lower_usage(writer: Writer, evidence: dict[str, Any], role: str,
                 stats_path: Path, request_ids: list[str], normalizer: Any,
                 access: dict[str, Any], cleanup_ref: dict[str, str],
                 discarded_ids: list[str] = ()) -> None:
    stats_ref = writer.raw(stats_path)
    raw = read(stats_path)
    # The Claude lower broker records no separate lifecycle block; terminal state is
    # evidenced by its runtime counters (every call completed, nothing in flight)
    # plus the coordinator cleanup receipt proving the container is gone (cleanup_ref).
    runtime = raw.get("runtime", {})
    _require(isinstance(runtime, dict) and type(runtime.get("calls")) is int
             and runtime.get("calls") == runtime.get("completed_responses")
             and runtime.get("in_flight_calls", 0) == 0 and runtime.get("failures") == 0,
             role + " broker lacks terminal evidence (calls/completed/in-flight mismatch)")
    final_ids = [row.get("response_id") for row in raw.get("request_ledger", [])
                 if isinstance(row, dict)]
    # Requests spent by a discarded infrastructure-invalid attempt are attributed to that
    # archived attempt record, never to an accepted round, so they are accounted for here
    # but carry no usage row: shared admission requires the usage rows of this role to be
    # exactly the accepted rounds' request ids (v2_readiness.py:354).
    discarded = list(discarded_ids)
    _require(len(final_ids) == len(set(final_ids))
             and set(discarded) <= set(final_ids)
             and not set(discarded) & set(request_ids)
             and [rid for rid in final_ids if rid not in set(discarded)] == list(request_ids),
             role + " request attribution is incomplete")
    terminal = writer.receipt("usage/" + role + "_terminal", state="terminal",
        process_reaped=True, stats_sha256=stats_ref["sha256"], source_evidence=cleanup_ref)
    index_value = {
        "schema_version": "agentswe-evaluator-receipt-index/v1",
        "task": "claude", "owner": "evaluator", "run_id": writer.run.name,
        "broker_stats": stats_ref, "broker_terminal": terminal,
    }
    index_ref = writer.receipt("usage/" + role + "_index", **index_value)
    rows = []
    for request_id in request_ids:
        normalized = normalizer.normalize_with_artifacts(index_value, request_id, access)
        rows.append({**normalized, "provider_record": index_ref})
    evidence["usage"][role] = writer.receipt("usage/" + role, role=role,
        discarded_attempt_requests=discarded,
        calls=len(rows), actual_upstream_attempts=sum(row["upstream_attempts"] for row in rows),
        successes=sum(row["state"] == "success" for row in rows),
        failures=sum(row["state"] == "failure" for row in rows),
        known_tokens=sum(row["known_tokens"] for row in rows), unknown_usage=0,
        in_flight=0, requests=rows)


def _judge_ledger_projection(raw: dict[str, Any], request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project one Result-judge broker ledger onto the hidden smoke request.

    claude runs the public dev-round judging and the hidden smoke through the same
    evaluator-owned Result-judge broker, so the ledger legitimately carries one row
    per judged public round plus the smoke row. The shared normalizer proves "one
    logical request, one attempt" for the smoke, so it is handed only that row, with
    the runtime counters recomputed for it; the raw ledger stays attached unchanged.
    """
    rows = raw.get("attempts") if isinstance(raw.get("attempts"), list) else []
    kept = [row for row in rows if isinstance(row, dict) and row.get("request_id") == request_id]
    excluded = [row.get("request_id") for row in rows if isinstance(row, dict) and row.get("request_id") != request_id]
    _require(len(kept) == 1, "result judge smoke request is not exactly one ledger row")
    row = kept[0]
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    runtime = dict(raw.get("runtime") or {})
    ok = row.get("state") == "terminal" and row.get("completed_response") is True and row.get("usage_unknown") is False
    runtime.update({
        "calls": 1, "completed_calls": 1, "successful_calls": 1 if ok else 0, "failures": 0 if ok else 1,
        "in_flight_calls": 0, "upstream_attempts": int(row.get("upstream_attempts") or 0),
        "usage_unknown_calls": 0 if ok else 1,
        "input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
        "tokens": int(usage.get("total_tokens") or 0), "total_tokens": int(usage.get("total_tokens") or 0)})
    projected = {**raw, "attempts": [row], "runtime": runtime}
    note = {"projected_from_rows": len(rows), "kept_request_id": request_id,
            "excluded_request_ids": excluded, "reason": "public dev-round judge requests share this broker; smoke attempt isolated for the one-attempt contract"}
    return projected, note


def _judge_usage(writer: Writer, evidence: dict[str, Any], role: str,
                 stats_path: Path, request_id: str, normalizer: Any) -> None:
    original_ref = writer.raw(stats_path)
    raw = read(stats_path)
    projected, projection = _judge_ledger_projection(raw, request_id)
    # The shared validator re-normalizes request["provider_record"] itself, so the
    # provider_record must be the one-attempt projection. It is written as a derived,
    # run-local file next to the scoring observations; the full ledger stays attached.
    projection_path = stats_path.parent / "readiness_scoring" / (stats_path.stem + ".smoke_projection.json")
    projection_path.parent.mkdir(parents=True, exist_ok=True)
    projection_text = json.dumps(projected, sort_keys=True, indent=2) + "\n"
    if not projection_path.is_file() or projection_path.read_text() != projection_text:
        projection_path.write_text(projection_text)
    provider_ref = writer.raw(projection_path)
    normalized = normalizer(read(projection_path), request_id)
    evidence["usage"][role] = writer.receipt("usage/" + role, role=role,
        calls=1, actual_upstream_attempts=normalized["upstream_attempts"],
        successes=1, failures=0, known_tokens=normalized["known_tokens"],
        unknown_usage=0, in_flight=0,
        ledger_projection={**projection, "original": original_ref, "original_sha256": original_ref["sha256"],
                           "projection": provider_ref},
        requests=[{**normalized, "provider_record": provider_ref}])


def export(run_dir: str | Path, destination: str | Path, *, cleanup_receipt: str | Path,
           trusted_binding: dict[str, Any], broker_normalizers: dict[str, Any]) -> dict[str, Any]:
    """Export existing terminal evidence; this function performs no live work."""
    from agentloop.protocol import candidate_tree_digest, tree_digest
    from agentloop.evaluator.semantic_score import execution_verdict
    from harbor.native_builder_evidence import events, readiness_segment_streams, verify_native

    run = Path(run_dir).resolve(strict=True)
    _require(run.is_dir() and not run.is_symlink(), "run directory must be regular")
    _require(isinstance(trusted_binding, dict)
             and trusted_binding.get("task") == "claude"
             and all(_is_hash(trusted_binding.get(key)) for key in
                     ("source_digest", "contract_digest", "registry_digest")),
             "trusted Claude binding malformed")
    _require(isinstance(broker_normalizers, dict)
             and set(broker_normalizers) >= set(ROLES),
             "shared broker normalizers are required")

    cleanup_path = Path(cleanup_receipt).resolve(strict=True)
    cleanup = read(cleanup_path)
    _require(cleanup.get("run_id") == run.name and cleanup.get("owner") == "evaluator"
             and all(cleanup.get(key) == "terminal" for key in
                     ("unit_state", "harbor_state", "builder_state")),
             "terminal evaluator-owned coordinator cleanup required")

    attestation_path = run / "pilot_builder_session_attestation.json"
    if not attestation_path.is_file():
        attestation_path = run / "builder_session_attestation.json"
    attestation = read(attestation_path)
    session = attestation.get("builder_session_id")
    _require(attestation.get("complete") is True
             and attestation.get("readiness_profile") == PROFILE
             and attestation.get("current_binding") == trusted_binding
             and attestation.get("builder_model") == "deepseek-flash"
             and attestation.get("builder_reasoning_effort") == "max"
             and attestation.get("builder_exit_code") == 0
             and attestation.get("accepted_candidate_count") == 2
             and attestation.get("readiness_two_rounds") is True
             and attestation.get("public_case_inventory") == ["dev_001"]
             and attestation.get("hidden_case_inventory") == ["test_001"]
             and isinstance(session, str) and session,
             "complete bound Claude readiness Builder attestation missing")

    lifecycle = run / "lifecycle"
    round_paths = [lifecycle / f"round_{number:03d}.json" for number in (1, 2)]
    records = [read(path) for path in round_paths]
    _require(attestation.get("candidate_records") == records,
             "Builder attestation differs from terminal round records")
    freeze_path = lifecycle / "freeze_manifest.json"
    frozen = read(freeze_path)
    frozen_root = Path(str(frozen.get("candidate_root", frozen.get("candidate_path", ""))))
    _require(frozen.get("source_submission") == 2
             and frozen.get("accepted_submission_count") == 2
             and frozen.get("builder_session_id") == session
             and frozen.get("hidden_case_inventory") == ["test_001"]
             and frozen.get("feedback_chain_complete") is True
             and frozen.get("hidden_allowed") is True
             and frozen.get("immutable_candidate") is True
             and frozen_root.is_dir()
             and candidate_tree_digest(frozen_root) == frozen.get("candidate_digest"),
             "terminal Candidate 2 freeze is incomplete or changed")

    native_records = [{**record,
        "feedback_path": record.get("evaluator_feedback"),
        "feedback_digest": record.get("evaluator_feedback_digest"),
        "feedback_digest_ack": record.get("feedback_digest_ack"),
        "build": {**record.get("build", {}),
                  "candidate_repo_digest": record.get("candidate_digest")},
    } for record in records]
    delivered = [row for row in attestation.get("events", [])
                 if isinstance(row, dict) and row.get("event") == "feedback_delivered"]
    claimed_native = attestation.get("native_evidence")
    _require(isinstance(claimed_native, dict), "native Builder evidence missing")
    native = verify_native(run, native_records, delivered,
                           claimed_native.get("observed_stream_prefixes", []))
    _require(native == claimed_native and native.get("valid") is True
             and native.get("native_turn_completed") is True
             and native.get("native_termination", {}).get("successful_terminal") is True
             and _reconnects_recovered(native),
             "native Builder evidence changed, is unrecovered, or is incomplete")
    source_files = native.get("source_files")
    _require(isinstance(source_files, list) and source_files,
             "native Builder stream reference missing")
    segment_streams = readiness_segment_streams(run, native)
    stream = segment_streams[-1]
    _require(all(sha(item) == ref.get("sha256")
                 for item, ref in zip(segment_streams, source_files)),
             "native Builder stream changed")
    turns = [event["usage"] for item in segment_streams
             for event in events(item.read_bytes())
             if event.get("type") == "turn.completed"]
    _require(len(turns) == 1, "one uninterrupted native Builder turn required")

    writer = Writer(run, destination)
    cleanup_raw = writer.raw(cleanup_path)
    attestation_raw = writer.raw(attestation_path)
    native_ref = writer.raw(stream)
    evidence: dict[str, Any] = {
        "schema_version": "agentswe-edit-readiness-bundle-v2",
        "profile": PROFILE, "task": "claude", "run_id": run.name,
        "current_binding": trusted_binding, "public_rounds": [],
        "judges": {}, "usage": {},
    }
    segment_refs = {'native_logs': [writer.raw(v) for v in segment_streams[:-1]] + [native_ref],
                    'builder_segments': writer.raw(run / 'builder_segments.json')} \
        if len(segment_streams) > 1 else {}
    evidence["builder_native"] = writer.receipt("builder_native",
        builder_session_id=session, model="deepseek-flash", effort="max",
        state="terminal", thread_ids=[session], native_log=native_ref,
        native_log_format="codex-harbor-mixed-jsonl-v1",
        source_evidence=attestation_raw, **segment_refs)
    evidence["usage"]["builder"] = writer.receipt("usage/builder",
        role="builder", transport="native_codex_direct", builder_broker_started=False,
        native_completed_turns=1, native_reported_usage=turns,
        actual_upstream_requests=None, complete_provider_billing_claimed=False,
        native_usage_complete=True,
        known_tokens=sum(turn["input_tokens"] + turn["output_tokens"] for turn in turns),
        builder_session_id=session, native_log=native_ref)

    final_public_stats = read(run / "pilot_public_lower_broker_stats.json")
    discarded_public = _discarded_public_attempts(lifecycle)
    discarded_ids: list[str] = []
    observed_ledger: list[dict[str, Any]] = []
    public_ids: list[str] = []
    previous_delivery = None
    preceding_feedback = None
    product_digests: list[str] = []
    repository_digests: list[str] = []
    for number, (record, round_path) in enumerate(zip(records, round_paths), 1):
        delivery = Path(str(record.get("delivery_path", run / "candidates" /
                                         f"submission_{number:03d}")))
        _require(delivery.is_dir() and not delivery.is_symlink()
                 and set(path.name for path in delivery.iterdir()) == set(FILES)
                 and all((delivery / name).is_file() and not (delivery / name).is_symlink()
                         for name in FILES), "accepted Claude delivery changed")
        delivery_value = tree_digest(delivery)
        _require(record.get("delivery_digest") == delivery_value
                 and record.get("accepted") is True
                 and record.get("round_consumed") is True
                 and record.get("submission_number") == number
                 and record.get("builder_session_id") == session
                 and record.get("current_binding") == trusted_binding
                 and record.get("public_inventory") == ["dev_001"],
                 "accepted public round binding is incomplete")
        report = read(delivery / "run_report.json")
        metadata = {"builder_session_id": session, "submission_number": number,
                    "revision_of_candidate_digest": previous_delivery,
                    "feedback_digest": preceding_feedback}
        _require(all(key in report and report[key] == value
                     for key, value in metadata.items()),
                 "delivery run_report revision metadata mismatch")
        if number == 2:
            edit = read(delivery / "edit_report.json")
            _require(isinstance(edit.get("feedback_response"), str)
                     and bool(edit["feedback_response"].strip()),
                     "Candidate 2 feedback explanation missing")
            _require(record.get("feedback_digest_ack") == preceding_feedback
                     and record.get("feedback", {}).get("feedback_consumed") is True
                     and record.get("feedback", {}).get("feedback_digest") == preceding_feedback,
                     "Candidate 2 did not consume exact Candidate 1 feedback")

        materialized = lifecycle / f"candidate_{number:03d}"
        repository_digest = candidate_tree_digest(materialized)
        product = materialized / "plugins" / "policy-provenance-ledger"
        product_digest = candidate_tree_digest(product)
        build = record.get("build", {})
        _require(materialized.is_dir() and product.is_dir()
                 and repository_digest == record.get("candidate_digest") == build.get("candidate_digest")
                 and build.get("build_valid") is True
                 and build.get("patch_sha256") == sha(delivery / "solution.patch"),
                 "materialized Claude source/build changed")
        product_digests.append(product_digest)
        repository_digests.append(repository_digest)

        dev = record.get("dev")
        _require(isinstance(dev, list) and len(dev) == 1
                 and isinstance(dev[0], dict) and dev[0].get("case_id") == "dev_001",
                 "public readiness inventory must be exactly dev_001")
        measurement_path = lifecycle / f"round_{number:03d}" / "dev_001" / "measurement.json"
        _require(read(measurement_path) == dev[0], "public measurement changed")
        verdict, _ = execution_verdict(dev[0], "dev_001", repository_digest)
        _require(verdict.get("classification") in {"scoreable", "candidate_zero"}
                 and dev[0].get("infrastructure_invalid") is not True,
                 "public round is infrastructure-invalid or unscoreable")
        for attempt in [row for row in discarded_public if row["round"] == number]:
            _require(not set(attempt["request_ids"]) & (set(public_ids) | set(discarded_ids)),
                     "discarded public attempt reuses a request identity")
            observed_ledger = _chain_ledger(observed_ledger, attempt["before"],
                                            attempt["after"], label=attempt["label"])
            discarded_ids.extend(attempt["request_ids"])
        broker = dev[0].get("broker", {})
        ids = _ledger_ids(broker.get("before", {}), broker.get("after", {}),
                          label=f"public round {number}")
        _require(not set(ids) & (set(public_ids) | set(discarded_ids)),
                 "public request reused across rounds")
        public_ids.extend(ids)
        observed_ledger = _chain_ledger(observed_ledger, broker.get("before", {}),
                                        broker.get("after", {}),
                                        label=f"public round {number}")
        _require(final_public_stats.get("request_ledger", [])[:len(observed_ledger)]
                 == observed_ledger,
                 "public broker terminal ledger differs from execution snapshots")

        feedback_path = Path(str(record.get("evaluator_feedback", "")))
        feedback_ref = {**writer.raw(feedback_path), "digest_algorithm": "sha256-bytes-v1"}
        _require(record.get("evaluator_feedback_digest") == feedback_ref["sha256"],
                 "issued evaluator feedback changed")
        for name in FILES:
            writer.raw(delivery / name)
        result_ref = writer.receipt(f"public/{number}/result", case="dev_001",
            candidate_digest=delivery_value, state="terminal",
            classification="execution_valid", source_evidence=writer.raw(measurement_path))
        execution_fields: dict[str, Any] = {
            **metadata, "candidate_digest": delivery_value, "case": "dev_001",
            "state": "terminal", "classification": "execution_valid",
            "current_binding": trusted_binding, "transport": "complete",
            "build_exit_code": 0,
            "materialized_source_digest_before_build": product_digest,
            "materialized_source_digest_after_build": product_digest,
            "materialized_repository_digest": repository_digest,
            "result": result_ref, "feedback_sha256": feedback_ref["sha256"],
            "request_ids": ids, "source_round": writer.raw(round_path),
            "source_execution": writer.raw(measurement_path),
        }
        if number == 2:
            execution_fields["consumed_feedback"] = evidence["public_rounds"][0]["feedback"]
            execution_fields["consumption_native_evidence"] = attestation_raw
        evidence["public_rounds"].append({
            **metadata, "case": "dev_001", "candidate_digest": delivery_value,
            "submission_dir": "raw/" + delivery.resolve().relative_to(run).as_posix(),
            "submission_sha256": {name: sha(delivery / name) for name in FILES},
            "materialized_source_digest": product_digest,
            "materialized_repository_digest": repository_digest,
            "feedback": feedback_ref,
            "execution": writer.receipt(f"public/{number}/execution", **execution_fields),
        })
        previous_delivery = delivery_value
        preceding_feedback = feedback_ref["sha256"]

    _require(product_digests[0] != product_digests[1]
             and repository_digests[0] != repository_digests[1],
             "Candidate 2 did not change materialized product source")
    _require(repository_digests[-1] == frozen["candidate_digest"],
             "freeze does not identify materialized Candidate 2")
    evidence["freeze"] = writer.receipt("freeze",
        candidate_digest=repository_digests[-1],
        delivery_candidate_digest=previous_delivery, builder_session_id=session,
        submission_sha256=evidence["public_rounds"][-1]["submission_sha256"],
        current_binding=trusted_binding, source_evidence=writer.raw(freeze_path))

    hidden_path = run / "pilot_hidden_after_freeze" / "hidden_run.json"
    hidden = read(hidden_path)
    hidden_attestation_path = lifecycle / "hidden_after_freeze_attestation.json"
    hidden_attestation = read(hidden_attestation_path)
    summary = hidden_attestation.get("summary", {})
    ordering = hidden_attestation.get("ordering", {})
    isolation = hidden_attestation.get("isolation", {})
    _require(hidden.get("schema_version") == "agentswe-claude-hidden-run/v1"
             and hidden.get("hidden_after_freeze") is True
             and hidden.get("pilot_not_formal") is True
             and hidden.get("case_inventory") == ["test_001"]
             and hidden.get("candidate_digest") == hidden.get("candidate_digest_after")
                 == repository_digests[-1]
             and hidden.get("freeze_manifest_sha256") == sha(freeze_path)
             and hidden_attestation.get("schema_version")
                 == "agentswe-claude-hidden-after-freeze-attestation/v1"
             and summary.get("inventory_count") == 1
             and summary.get("case_spec_unavailable_count") == 0
             and summary.get("scheduled_records_used_as_results") is False
             and ordering.get("freeze_before_hidden") is True
             and ordering.get("candidate_digest_stable_after_hidden") is True
             and all(isolation.get(key) is True for key in (
                 "hidden_case_specs_not_mounted_in_candidate",
                 "evaluator_source_not_mounted", "provider_credential_not_mounted")),
             "hidden test_001 is not a complete post-freeze execution")
    hidden_records = hidden.get("cases")
    _require(isinstance(hidden_records, list) and len(hidden_records) == 1
             and hidden_records[0].get("case_id") == "test_001",
             "hidden execution record missing")
    hidden_verdict, _ = execution_verdict(hidden_records[0], "test_001",
                                           repository_digests[-1])
    _require(hidden_verdict.get("classification") in {"scoreable", "candidate_zero"},
             "hidden test_001 is infrastructure-invalid or unscoreable")
    hidden_broker = hidden.get("broker", {})
    hidden_ids = _ledger_ids(hidden_broker.get("before", {}),
                             hidden_broker.get("after", {}), label="hidden smoke")
    hidden_result = writer.receipt("hidden/result", case="test_001", state="terminal",
        candidate_digest=repository_digests[-1], source_evidence=writer.raw(hidden_path))
    evidence["hidden_smoke"] = writer.receipt("hidden", case="test_001",
        state="terminal", classification="execution_valid", transport="complete",
        readiness_only=True, builder_access=False,
        freeze_sha256=evidence["freeze"]["sha256"], result=hidden_result,
        request_ids=hidden_ids, source_evidence=writer.raw(hidden_attestation_path))

    access = _access(writer, tree_digest)
    _require(final_public_stats.get("request_ledger") == observed_ledger,
             "public broker terminal ledger differs from execution snapshots")
    _lower_usage(writer, evidence, "public_lower",
        run / "pilot_public_lower_broker_stats.json", public_ids,
        broker_normalizers["public_lower"], access, cleanup_raw, discarded_ids)
    _lower_usage(writer, evidence, "hidden_lower",
        run / "pilot_hidden_lower_broker_stats.json", hidden_ids,
        broker_normalizers["hidden_lower"], access, cleanup_raw)

    judge_sources = {
        "result": run / "result_judge_broker_stats.json",
    }
    identities = {session}
    # Code axis retired 2026-09-19 (Result-only): explicit skip receipts, accepted by v2_readiness.
    _skip_policy = {"id": "edit-code-axis-retired-2026-09-19", "evaluation_state": "skipped_by_policy", "reason": "Result-only evaluation; Code judge not dispatched"}
    _skip_source = writer.raw(run / "readiness_scoring/code_observation.json")
    evidence["usage"]["code_judge"] = writer.receipt("usage/code_judge", role="code_judge", skipped_by_policy=True,
        policy=_skip_policy, calls=0, actual_upstream_attempts=0, successes=0, failures=0, known_tokens=0,
        unknown_usage=0, in_flight=0, requests=[], source_evidence=_skip_source)
    evidence["judges"]["code"] = writer.receipt("judges/code", role="code", state="skipped_by_policy", formal=False,
        judge_session_id=None, request_id=None, freeze_sha256=evidence["freeze"]["sha256"], policy=_skip_policy,
        source_evidence=_skip_source)
    for role in ("result",):
        observation_path = run / "readiness_scoring" / (role + "_observation.json")
        observation = read(observation_path)
        request_id = observation.get("request_id")
        identity = observation.get("judge_session_id")
        _require(observation.get("run_id") == run.name
                 and observation.get("owner") == "evaluator"
                 and observation.get("role") == role
                 and observation.get("model") == "deepseek-flash"
                 and observation.get("effort") == "max"
                 and observation.get("state") == "terminal"
                 and observation.get("formal") is False
                 and isinstance(request_id, str) and request_id
                 and isinstance(identity, str) and identity and identity not in identities,
                 role + " judge observation is incomplete or not independent")
        identities.add(identity)
        input_value = writer.references(read(observation["input"]["path"]))
        output_value = writer.references(read(observation["output"]["path"]))
        # The shared judge adds `ceiling_assessments` when a score-cap contract is supplied;
        # the shared bundle validator checks the payload against a fixed key set, so the
        # ceilings live beside the payload (the raw judge output is the source evidence).
        _payload = output_value.get('payload') if isinstance(output_value.get('payload'), dict) else None
        if _payload is not None and 'ceiling_assessments' in _payload:
            output_value['ceiling_assessments'] = _payload.pop('ceiling_assessments')
        _require(input_value.get("role") == role
                 and output_value.get("role") == role
                 and output_value.get("judge_session_id") == identity
                 and isinstance(output_value.get("payload"), dict),
                 role + " judge input/output identity mismatch")
        input_value["freeze_sha256"] = evidence["freeze"]["sha256"]
        input_ref = writer.receipt("judges/" + role + "_input", **input_value)
        output_ref = writer.receipt("judges/" + role + "_output", **output_value)
        judge_fields = {**observation, "input": input_ref, "output": output_ref,
            "freeze_sha256": evidence["freeze"]["sha256"],
            "source_evidence": writer.raw(observation_path)}
        evidence["judges"][role] = writer.receipt("judges/" + role,
                                                   **judge_fields)
        _judge_usage(writer, evidence, role + "_judge", judge_sources[role],
                     request_id, broker_normalizers[role + "_judge"])

    cleanup_value = copy.deepcopy(cleanup)
    for key in ("before", "after"):
        source = cleanup_value.get(key)
        _require(isinstance(source, dict), "cleanup " + key + " reference missing")
        value = read(source["path"])
        _require(sha(source["path"]) == source.get("sha256"),
                 "cleanup " + key + " bytes changed")
        cleanup_value[key] = writer.receipt("cleanup/" + key, **value,
            source_evidence=writer.raw(source["path"]))
    stats = cleanup_value.get("stats")
    owned = cleanup_value.get("owned_resources")
    _require(isinstance(owned, list) and owned and len(owned) == len(set(owned))
             and isinstance(stats, dict) and set(stats) == set(owned),
             "cleanup owned resource stats incomplete")
    for resource in owned:
        source = stats[resource]
        value = read(source["path"])
        _require(sha(source["path"]) == source.get("sha256")
                 and value.get("resource_id") == resource,
                 "cleanup resource stats changed or mismatched")
        stats[resource] = writer.receipt("cleanup/stats/" + resource, **value,
            source_evidence=writer.raw(source["path"]))
    evidence["cleanup"] = writer.receipt("cleanup", **cleanup_value,
        source_evidence=cleanup_raw)

    manifest = writer.root / "manifest.json"
    manifest.write_text(json.dumps(evidence, sort_keys=True, indent=2,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
    return {"bundle_root": str(writer.root), "manifest_sha256": sha(manifest),
            "pipeline_ready": False, "admission_required": True,
            "provider_calls": 0, "network_calls": 0, "shared_writes": False}


def self_test() -> dict[str, Any]:
    _require(PROFILE == "single-dev-two-round-hidden-smoke-v1"
             and FILES == ("solution.patch", "edit_report.json", "run_report.json"),
             "readiness constants changed")
    return {"self_test": "PASS", "provider_calls": 0, "network_calls": 0,
            "dispatch_started": False, "shared_writes": False}
