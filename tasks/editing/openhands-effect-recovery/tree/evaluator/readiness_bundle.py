"""Export a terminal OpenHands v2 readiness run into an immutable bundle.

The exporter is task-local: it copies bytes only from the run directory,
never starts a provider or judge, never removes a container, and never writes
the shared registry or gate.  Admission is performed later by the single
coordinator after independent source/binding review.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

PROFILE = "single-dev-two-round-hidden-smoke-v1"
FILES = ("solution.patch", "edit_report.json", "run_report.json")


def read(path: Path):
    return json.loads(Path(path).read_bytes())


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(path: Path) -> str:
    """Match the shared v2 admission digest for regular directory artifacts."""
    root = Path(path)
    digest = hashlib.sha256()
    for child in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        name = child.relative_to(root).as_posix().encode()
        if child.is_symlink():
            kind, payload = b"L", os.readlink(child).encode()
        elif child.is_file():
            kind, payload = b"F", child.read_bytes()
        elif child.is_dir():
            kind, payload = b"D", b""
        else:
            raise ValueError("special file in OpenHands readiness tree")
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


class Writer:
    """Copy regular, run-owned bytes and produce evaluator receipts."""

    def __init__(self, run_dir: Path, destination: Path):
        self.run = Path(run_dir).resolve()
        self.root = Path(destination).resolve()
        if self.root.exists():
            raise ValueError("readiness bundle destination already exists; no replay")
        self.root.mkdir(parents=True)

    def raw(self, path: Path) -> dict:
        path = Path(path)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.run):
            raise ValueError("missing or foreign OpenHands raw evidence: " + str(path))
        relative = path.resolve().relative_to(self.run)
        if any(part in {".env", "auth.json", "builder_provider.toml", "builder_broker_provider.toml"}
               for part in relative.parts):
            raise ValueError("provider credentials cannot enter readiness bundle")
        target = self.root / "raw" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        return {"path": target.relative_to(self.root).as_posix(), "sha256": sha(target)}

    def receipt(self, name: str, **value) -> dict:
        path = self.root / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"run_id": self.run.name, "owner": "evaluator", **value},
                                   sort_keys=True, indent=2) + "\n")
        return {"path": path.relative_to(self.root).as_posix(), "sha256": sha(path)}

    def references(self, value):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and Path(value["path"]).is_absolute() and isinstance(value.get("sha256"), str):
                source = Path(value["path"])
                if sha(source) != value["sha256"]:
                    raise ValueError("referenced evaluator artifact changed")
                return {**value, **self.raw(source)}
            return {key: self.references(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.references(child) for child in value]
        return value


def _under(path: Path, root: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError("OpenHands evidence path is missing or escaped run")
    return path


def _state_and_freeze(run: Path):
    state_path = run / "lifecycle" / "lifecycle_state.json"
    if not state_path.is_file():
        state_path = run / "lifecycle" / "dev_lifecycle.json"
    freeze_path = run / "lifecycle" / "freeze_manifest.json"
    if not state_path.is_file() or not freeze_path.is_file():
        raise ValueError("terminal lifecycle state/freeze manifest missing")
    return state_path, read(state_path), freeze_path, read(freeze_path)


def _native(run: Path):
    for path in (run / "builder_session_attestation.json", run / "pilot_builder_session_attestation.json"):
        if path.is_file():
            value = read(path)
            native = value.get("native_evidence") or value.get("native")
            if native:
                return path, value, native
    raise ValueError("OpenHands native Builder attestation missing")


def _native_stream(run: Path, native: dict):
    refs = native.get("source_files") or []
    if not refs:
        raise ValueError("native Builder source stream reference missing")
    path = _under(Path(refs[0]["path"]), run)
    if sha(path) != refs[0].get("sha256"):
        raise ValueError("native Builder stream changed after terminal attestation")
    return path


def _native_segment_streams(run: Path, native: dict) -> list[Path]:
    """Ordered segment streams, terminal last; one element before any resume."""
    from harbor.native_builder_evidence import readiness_segment_streams
    refs = native.get("source_files") or []
    paths = readiness_segment_streams(run, native)
    for path, reference in zip(paths, refs):
        if sha(_under(path, run)) != reference.get("sha256"):
            raise ValueError("native Builder stream changed after terminal attestation")
    return [_under(path, run) for path in paths]


def _record_delivery(run: Path, record: dict, number: int) -> tuple[Path, Path]:
    """Resolve actual three-file delivery and materialized product paths."""
    delivery = None
    for key in ("delivery_path", "candidate_delivery_path"):
        if isinstance(record.get(key), str) and Path(record[key]).is_dir():
            delivery = Path(record[key])
            break
    if delivery is None:
        # deliveries/attempt_00N is the Nth DELIVERY, not the Nth round: a round the
        # evaluator rejected and retried left its superseded delivery in the run and
        # shifted every later one. Bind the round to the delivery whose own tree
        # digest the controller recorded for it -- the same digest this round's
        # receipt publishes as candidate_digest and the next round's run_report
        # names as revision_of_candidate_digest.
        digests = record.get("delivery_digests") or []
        recorded = digests[0] if len(digests) == 1 and isinstance(digests[0], str) else None
        if recorded:
            matches = [path for path in sorted((run / "deliveries").glob("attempt_*"))
                       if path.is_dir() and not path.is_symlink()
                       and set(item.name for item in path.iterdir()) == set(FILES)
                       and tree(path) == recorded]
            if len(matches) != 1:
                raise ValueError("OpenHands round %d recorded delivery digest matches %d deliveries"
                                 % (number, len(matches)))
            delivery = matches[0]
    if delivery is None:
        attempt = record.get("attempt") or number
        delivery = run / "deliveries" / f"attempt_{int(attempt):03d}"
    delivery = _under(delivery / "solution.patch", run).parent
    if set(p.name for p in delivery.iterdir()) != set(FILES):
        raise ValueError("OpenHands delivery must contain exactly three files")
    product = None
    candidates = sorted((run / "materialized").glob(f"candidate_{number:03d}_attempt_*"))
    if candidates:
        product = candidates[-1]
    material = record.get("build", {}).get("materialization", {})
    if product is None and isinstance(material.get("materialize_result", {}).get("output"), str):
        product = Path(material["materialize_result"]["output"])
    if product is None or not product.is_dir() or product.is_symlink() or not product.resolve().is_relative_to(run):
        raise ValueError("OpenHands materialized repository is missing")
    return delivery, product


def _lower_ledger(run: Path, role: str) -> Path:
    candidates = {
        "public_lower": (run / "brokers/lower.json", run / "public_lower_broker/broker_stats.json"),
        "hidden_lower": (run / "brokers/hidden.json", run / "hidden_lower_broker/broker_stats.json"),
    }[role]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError("OpenHands " + role + " ledger missing")


def _ledger_rows(role: str, raw: dict) -> list[str]:
    if role.endswith("lower"):
        return [row["request_sha256"] for row in raw.get("requests", [])]
    if role == "result_judge":
        return [row["request_id"] for row in raw.get("attempts", [])]
    return [row["response_id"] for row in raw.get("attempts", [])]


def _case_request_ids(run: Path, relative: Path) -> list[str]:
    """Bind every lower request captured under one concrete case execution."""
    case_root = run / "lifecycle" / relative
    if not case_root.is_dir() or case_root.is_symlink():
        raise ValueError("OpenHands case evidence directory missing: " + relative.as_posix())
    ids = []
    for path in sorted(case_root.glob("*.request.json")):
        value = read(path)
        request_id = value.get("request_sha256")
        if value.get("state") != "completed" or not isinstance(request_id, str) or not request_id:
            raise ValueError("OpenHands case contains incomplete lower request evidence")
        ids.append(request_id)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("OpenHands case lower request identities are missing or duplicated")
    return ids


_ROUND_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _round_attempt_deltas(record: dict, superseded: list, number: int) -> list[dict]:
    """Every lower broker_delta one round spent: superseded attempts, then the accepted one.

    A round the controller had to retry (an attempt classified infrastructure-invalid
    is booked accepted False / round_consumed False and appended to the lifecycle's
    `infrastructure_attempts`, and the round re-runs on a fresh snapshot) already
    spent that attempt's lower calls on the same appended-once ledger. The attempt
    record names the round it belongs to (`submission`) and carries its own
    broker_delta, so those rows are attributed to this round and to no other.
    """
    deltas = []
    for attempt in superseded:
        if attempt.get("submission") != number:
            continue
        if attempt.get("accepted") is not False or attempt.get("round_consumed") is not False:
            raise ValueError("OpenHands round %d attempt is recorded as neither rejected nor consumed" % number)
        deltas.append(((attempt.get("dev") or {}).get("dev_001") or {}).get("broker_delta") or {})
    deltas.append(((record.get("dev") or {}).get("dev_001") or {}).get("broker_delta") or {})
    return deltas


def _round_delta_field(deltas: list, key: str, number: int, what: str) -> int:
    values = [delta.get(key) for delta in deltas]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("OpenHands round %d records no lower %s" % (number, what))
    return sum(values)


def _round_request_slices(ledger: dict, records: list, superseded: list = ()) -> list[dict]:
    """Split the ordered lower ledger into one contiguous slice per round.

    The case evidence hashes the payload it sent; the broker hashes the body it
    forwarded after rewriting model and reasoning, so the two identities never
    coincide and nothing correlates them directly. The ledger is appended per
    call and the rounds run in sequence, so each round owns a contiguous slice --
    including the calls of any attempt of that round the evaluator rejected and
    retried, which precede the accepted attempt's. That is verified rather than
    assumed, twice: the whole slice's call count, success count and all three
    token totals equal the SUM of the round's recorded broker_deltas (its
    superseded attempts plus the accepted record), and the slice's tail on its
    own equals the accepted attempt's broker_delta -- which is also what proves
    the superseded rows are the leading ones.
    """
    rows = ledger.get("requests")
    if not isinstance(rows, list) or not rows:
        raise ValueError("OpenHands public ledger has no ordered requests")
    superseded = list(superseded or ())
    if any(not isinstance(attempt, dict) for attempt in superseded):
        raise ValueError("OpenHands superseded round attempts are malformed")
    numbers = set(range(1, len(records) + 1))
    if any(attempt.get("submission") not in numbers for attempt in superseded):
        raise ValueError("OpenHands superseded attempt belongs to no exported round")
    slices, offset = [], 0
    for number, record in enumerate(records, 1):
        deltas = _round_attempt_deltas(record, superseded, number)
        count = _round_delta_field(deltas, "calls", number, "call count")
        accepted_count = deltas[-1]["calls"]
        if count <= 0 or accepted_count <= 0:
            raise ValueError("OpenHands round %d records no lower call count" % number)
        chunk = rows[offset:offset + count]
        if len(chunk) != count:
            raise ValueError("OpenHands ledger is shorter than round %d claims" % number)
        # D13: a tolerated transport row reports null token counters, not zero.
        def totals_of(subset):
            return {key: sum(int(row.get(key) or 0) for row in subset) for key in _ROUND_TOKEN_KEYS}
        totals = totals_of(chunk)
        successes = sum(1 for row in chunk if row.get("ok") is True)
        if (successes != _round_delta_field(deltas, "successful_calls", number, "success count")
                or any(totals[key] != _round_delta_field(deltas, key, number, key) for key in totals)):
            raise ValueError("OpenHands round %d usage does not match its ledger slice" % number)
        accepted_chunk, superseded_chunk = chunk[count - accepted_count:], chunk[:count - accepted_count]
        accepted_totals = totals_of(accepted_chunk)
        if (sum(1 for row in accepted_chunk if row.get("ok") is True) != deltas[-1].get("successful_calls")
                or any(accepted_totals[key] != deltas[-1].get(key) for key in accepted_totals)):
            raise ValueError("OpenHands round %d accepted attempt does not own the tail of its slice" % number)
        ids = [row["request_sha256"] for row in chunk]
        if len(set(ids)) != len(ids):
            raise ValueError("OpenHands round %d slice repeats a request identity" % number)
        slices.append({"request_ids": ids,
                       "superseded_attempt_request_ids": [row["request_sha256"] for row in superseded_chunk],
                       "accepted_attempt_request_ids": [row["request_sha256"] for row in accepted_chunk]})
        offset += count
    if offset != len(rows):
        raise ValueError("OpenHands lower ledger has requests attributed to no round")
    return slices


def _terminal_cleanup(run: Path, cleanup_receipt: Path):
    cleanup = read(cleanup_receipt)
    if cleanup.get("run_id") != run.name or cleanup.get("owner") != "evaluator" or any(
            cleanup.get(k) != "terminal" for k in ("unit_state", "harbor_state", "builder_state")):
        raise ValueError("coordinator terminal cleanup proof is required before export")
    return cleanup


def _recovered_usage_fields(rows):
    """D13 (2026-09-19): tolerated upstream transport failures, recorded and never summed.

    The shared normalizer marks such a row; its usage stays unknown, so it is reported as
    unknown usage and named by identity rather than folded into known_tokens. A role with
    no tolerated row produces exactly the accounting this exporter produced before.
    """
    recovered = [r for r in rows if r.get('recovered_transport') is True]
    fields = {'unknown_usage': len(recovered), 'in_flight': 0}
    if recovered:
        fields['recovered_transport_failures'] = [{'request_id': r['request_id'], 'error': r['error']}
                                                  for r in recovered]
    return fields


def export(run_dir: Path, destination: Path, *, cleanup_receipt: Path, trusted_binding: dict,
           broker_normalizers: dict):
    run = Path(run_dir).resolve()
    cleanup_path = Path(cleanup_receipt).resolve()
    cleanup = _terminal_cleanup(run, cleanup_path)
    state_path, state, freeze_path, freeze = _state_and_freeze(run)
    native_path, attestation, native = _native(run)
    records = state.get("records") or attestation.get("candidate_records") or []
    if len(records) != 2 or freeze.get("source_submission") != 2:
        raise ValueError("OpenHands v2 readiness requires exactly two accepted rounds")
    if freeze.get("readiness_profile") != PROFILE or freeze.get("current_binding") != trusted_binding:
        raise ValueError("freeze is not bound to current OpenHands readiness source")
    if attestation.get("complete") is not True or native.get("valid") is not True or native.get("native_turn_completed") is not True:
        raise ValueError("native Builder attestation is incomplete")
    session = attestation.get("builder_session_id") or native.get("native_thread_id")
    if not isinstance(session, str) or not session:
        raise ValueError("Builder session identity missing")
    segment_streams = _native_segment_streams(run, native)
    stream = segment_streams[-1]
    event_rows = [json.loads(line) for item in segment_streams
                  for line in item.read_bytes().splitlines() if line.startswith(b'{"type":')]
    terminal_rows = [json.loads(line) for line in stream.read_bytes().splitlines() if line.startswith(b'{"type":')]
    turns = [row.get("usage") for row in event_rows if row.get("type") == "turn.completed"]
    # D13 (2026-09-19) transport-reconnect tolerance; package 109 (2026-09-21).
    # Matches the shared control plane rule in
    # @@AGENTSWE_EDITING_CONTROL@@/v2_readiness.py:99-104: a codex-CLI
    # "Reconnecting... n/N (...)" error row is a recovered transport retry, not a fault.
    # It is tolerated only on a terminal segment that actually ended on turn.completed;
    # turn.failed / thread.failed and every other error row stay fatal, and the turn
    # accounting (exactly one completed turn) above is unchanged.
    _completed_terminal = bool(terminal_rows) and terminal_rows[-1].get("type") == "turn.completed"
    _unrecovered = [row for row in terminal_rows
                    if row.get("type") in {"turn.failed", "thread.failed"} or
                    (row.get("type") == "error" and not (_completed_terminal and
                     str(row.get("message", "")).startswith("Reconnecting...")))]
    if len(turns) != 1 or _unrecovered:
        raise ValueError("native usage contains incomplete/error turn")
    writer = Writer(run, Path(destination))
    evidence = {"profile": PROFILE, "run_id": run.name, "current_binding": trusted_binding,
                "public_rounds": [], "judges": {}, "usage": {}}
    stream_ref = writer.raw(stream)
    segment_refs = {'native_logs': [writer.raw(v) for v in segment_streams[:-1]] + [stream_ref],
                    'builder_segments': writer.raw(run / 'builder_segments.json')} \
        if len(segment_streams) > 1 else {}
    evidence["builder_native"] = writer.receipt("builder_native", builder_session_id=session,
        model="deepseek-flash", effort="max", state="terminal", thread_ids=[session],
        native_log=stream_ref, native_log_format="codex-harbor-mixed-jsonl-v1",
        source_evidence=writer.raw(native_path), **segment_refs)
    native_tokens = sum(int(row["input_tokens"]) + int(row["output_tokens"]) for row in turns)
    evidence["usage"]["builder"] = writer.receipt("usage/builder", role="builder",
        transport="native_codex_direct", builder_broker_started=False, native_completed_turns=1,
        native_reported_usage=turns, actual_upstream_requests=None, complete_provider_billing_claimed=False,
        native_usage_complete=True, known_tokens=native_tokens, builder_session_id=session,
        native_log=stream_ref)
    paths = {
        "public_lower": _lower_ledger(run, "public_lower"),
        "hidden_lower": _lower_ledger(run, "hidden_lower"),
        # The judge broker runtime appends "-judge-transport" to the evidence
        # directory it is handed, and the name handed here already ends in
        # ".json", so the ledger lands in a directory literal paths keep missing.
        # Only a readiness-named transport directory qualifies: the public judge
        # writes its own beside it and must never fill the Result role.
        "result_judge": next((p for p in (
            *sorted(run.glob("brokers/readiness*-judge-transport/broker_stats.json")),
            run / "brokers/readiness-judge.json",
            run / "brokers/judge.json",
            run / "result_judge_broker/container-judge-transport/broker_stats.json",
        ) if p.is_file()), None),
    }
    role_ids = {}
    for role, path in paths.items():
        if path is None or not path.is_file():
            raise ValueError("OpenHands " + role + " ledger missing")
        raw = read(path)
        ids = _ledger_rows(role, raw)
        if not ids:
            raise ValueError("OpenHands " + role + " ledger is empty")
        role_ids[role] = ids
        rows = [{**broker_normalizers[role](raw, rid), "provider_record": writer.raw(path)} for rid in ids]
        evidence["usage"][role] = writer.receipt("usage/" + role, role=role, calls=len(rows),
            actual_upstream_attempts=sum(row["upstream_attempts"] for row in rows),
            successes=sum(row["state"] == "success" for row in rows), failures=sum(row["state"] == "failure" for row in rows),
            known_tokens=sum(row["known_tokens"] for row in rows), **_recovered_usage_fields(rows), requests=rows)
    # A round the evaluator rejected and retried spent its lower calls on this same
    # ledger; the controller records each such attempt with the round it belongs to,
    # so the round's slice owns those rows rather than leaving them unattributed.
    superseded_attempts = state.get("infrastructure_attempts")
    if superseded_attempts is None:
        superseded_attempts = attestation.get("infrastructure_attempts") or []
    round_slices = _round_request_slices(read(paths["public_lower"]), records, superseded_attempts)
    previous_delivery = previous_feedback = None
    for number, record in enumerate(records, 1):
        delivery, product = _record_delivery(run, record, number)
        delivery_digest, product_digest = tree(delivery), tree(product)
        submission_refs = {name: writer.raw(delivery / name) for name in FILES}
        feedback_path = Path(record.get("feedback_path", ""))
        if not feedback_path.is_file() or not feedback_path.resolve().is_relative_to(run) or sha(feedback_path) != record.get("feedback_digest"):
            raise ValueError("OpenHands authoritative feedback bytes changed")
        report = read(delivery / "run_report.json")
        metadata = {"builder_session_id": session, "submission_number": number,
                    "revision_of_candidate_digest": previous_delivery, "feedback_digest": previous_feedback}
        if any(report.get(key) != value for key, value in metadata.items()):
            raise ValueError("OpenHands submission metadata does not bind feedback revision")
        execution = record.get("dev", {}).get("dev_001", {})
        # openhands does not write `infrastructure_invalid` on a case; it writes a
        # classification, and the blocked/failure state on the round. Ask the
        # canonical classifier, then also require the two facts this task does
        # record -- strictly more than the absent-key test could ever check.
        from execution_contract import infrastructure_reason
        if (infrastructure_reason(execution)
                or record.get("infrastructure_blocked") is not False
                or record.get("infrastructure_failures")
                or execution.get("result_evaluation", {}).get("contract_valid") is not True):
            raise ValueError("OpenHands public execution is not infrastructure-valid")
        round_slice = round_slices[number - 1]
        request_ids = round_slice["request_ids"]
        if not set(request_ids).issubset(role_ids["public_lower"]):
            raise ValueError("OpenHands public request identity is absent from the broker ledger")
        result = writer.receipt(f"public/{number}/result", candidate_digest=delivery_digest,
            case="dev_001", state="terminal", classification="execution_valid")
        feedback_ref = {**writer.raw(feedback_path), "digest_algorithm": "sha256-bytes-v1"}
        materialized_source = record.get("build", {}).get("materialization", {}).get("materialize_result", {}).get("product_source_identity", product_digest)
        details = {**metadata, "candidate_digest": delivery_digest, "case": "dev_001", "state": "terminal",
            "classification": "execution_valid", "current_binding": trusted_binding, "transport": "complete",
            "build_exit_code": 0, "materialized_source_digest_before_build": materialized_source,
            "materialized_source_digest_after_build": materialized_source, "materialized_repository_digest": product_digest,
            "result": result, "feedback_sha256": feedback_ref["sha256"], "request_ids": request_ids,
            "superseded_attempt_request_ids": round_slice["superseded_attempt_request_ids"],
            "accepted_attempt_request_ids": round_slice["accepted_attempt_request_ids"],
            "source_state": writer.raw(state_path)}
        if number == 2:
            details["consumed_feedback"] = evidence["public_rounds"][0]["feedback"]
            details["consumption_native_evidence"] = writer.raw(native_path)
        evidence["public_rounds"].append({**metadata, "case": "dev_001", "candidate_digest": delivery_digest,
            "submission_dir": "raw/" + delivery.relative_to(run).as_posix(),
            "submission_sha256": {name: submission_refs[name]["sha256"] for name in FILES},
            "materialized_source_digest": materialized_source, "materialized_repository_digest": product_digest,
            "feedback": feedback_ref, "execution": writer.receipt(f"public/{number}/execution", **details)})
        previous_delivery, previous_feedback = delivery_digest, record["feedback_digest"]
    last_delivery, last_product = _record_delivery(run, records[-1], 2)
    evidence["freeze"] = writer.receipt("freeze", **{**freeze, "candidate_digest": tree(last_product),
        "delivery_candidate_digest": previous_delivery, "builder_session_id": session,
        "submission_sha256": {name: sha(last_delivery / name) for name in FILES}}, source_evidence=writer.raw(freeze_path))
    hidden_path = run / "lifecycle" / "pilot-hidden-after-freeze-attestation.json"
    if not hidden_path.is_file():
        hidden_path = run / "pilot-hidden-after-freeze-attestation.json"
    hidden = read(hidden_path)
    if hidden.get("expected_cases") != ["test_001"] or hidden.get("executed_cases") != ["test_001"] or hidden.get("frozen_digest_stable") is not True:
        raise ValueError("OpenHands hidden smoke is not exactly test_001 after freeze")
    case_rows = hidden.get("cases") or []
    hidden_result_path = Path(case_rows[0].get("result_path")) if case_rows else run / "hidden/test_001/case_result.json"
    # The hidden broker is started fresh after the freeze and verified at zero
    # calls, so every row in its ledger belongs to this one case; the case's own
    # broker_delta is the independent check, as for the public rounds. Case
    # evidence hashes the payload it sent and the broker hashes what it
    # forwarded, so the two identities never coincide and cannot be compared.
    hidden_case = read(hidden_result_path)
    hidden_delta = hidden_case.get("broker_delta") or {}
    hidden_rows = read(paths["hidden_lower"]).get("requests") or []
    hidden_request_ids = [row["request_sha256"] for row in hidden_rows]
    hidden_totals = {key: sum(row.get(key, 0) for row in hidden_rows)
                     for key in ("input_tokens", "output_tokens", "total_tokens")}
    if (not hidden_request_ids
            or len(set(hidden_request_ids)) != len(hidden_request_ids)
            or len(hidden_request_ids) != hidden_delta.get("calls")
            or any(hidden_totals[key] != hidden_delta.get(key) for key in hidden_totals)
            or set(hidden_request_ids) != set(role_ids["hidden_lower"])):
        raise ValueError("OpenHands hidden request identity differs from the broker ledger")
    hidden_result = writer.receipt("hidden_result", case="test_001", state="terminal", candidate_digest=tree(last_product),
        source_evidence=writer.raw(hidden_result_path))
    evidence["hidden_smoke"] = writer.receipt("hidden", case="test_001", state="terminal", classification="execution_valid",
        transport="complete", readiness_only=True, builder_access=False, freeze_sha256=evidence["freeze"]["sha256"],
        request_ids=hidden_request_ids, result=hidden_result, source_evidence=writer.raw(hidden_path))
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
        observation = run / "readiness_scoring" / (role + "_observation.json")
        if not observation.is_file():
            raise ValueError("independent OpenHands " + role + " judge observation missing")
        value = read(observation)
        inp_ref, out_ref = value.get("input"), value.get("output")
        inp = writer.references(read(Path(inp_ref["path"]))) if isinstance(inp_ref, dict) else writer.references(read(Path(inp_ref)))
        out = writer.references(read(Path(out_ref["path"]))) if isinstance(out_ref, dict) else writer.references(read(Path(out_ref)))
        inp["freeze_sha256"] = evidence["freeze"]["sha256"]
        evidence["judges"][role] = writer.receipt("judges/" + role, **{**value, "role": role,
            "model": "deepseek-flash", "effort": "max", "state": "terminal", "formal": False,
            "freeze_sha256": evidence["freeze"]["sha256"],
            "input": writer.receipt("judges/" + role + "_input", **inp),
            "output": writer.receipt("judges/" + role + "_output", **out), "source_evidence": writer.raw(observation)})
    # The coordinator owns OS/resource observations; the shared validator reads its
    # before/after/stats references as evaluator receipts, so re-issue each referenced
    # file as a receipt (run_id/owner + its own fields) with the raw bytes attached.
    def copy_cleanup_ref(reference):
        source = Path(reference["path"])
        value = read(source)
        if sha(source) != reference["sha256"]:
            raise ValueError("cleanup source reference changed")
        return writer.receipt("cleanup/" + source.stem, **value, source_evidence=writer.raw(source))
    cleanup = dict(cleanup, before=copy_cleanup_ref(cleanup["before"]), after=copy_cleanup_ref(cleanup["after"]),
                   stats={rid: copy_cleanup_ref(ref) for rid, ref in cleanup["stats"].items()})
    evidence["cleanup"] = writer.receipt(
        "cleanup", **writer.references(cleanup), source_evidence=writer.raw(cleanup_path),
    )
    manifest = writer.root / "manifest.json"
    manifest.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    return {"bundle_root": str(writer.root), "manifest_sha256": sha(manifest),
            "pipeline_ready": False, "admission_required": True, "provider_calls": 0,
            "shared_registry_written": False, "shared_gate_written": False}
