#!/usr/bin/env python3
"""Bounded judge projection of DeepCode Result-judge evidence (2026-09-21).

Why this exists
---------------
The shared Result judge (``@@AGENTSWE_EDITING_CONTROL@@/result_judge.py``)
concatenates task input, rubric, artifact, trajectory, native facts and the private
oracle comparison into one prompt.  Its only size handling is the hard ceilings in
``bounded_text`` (result_judge.py:729-733), which *raise* rather than truncate, so an
oversized case cannot be scored at all.  On 2026-09-20 the DeepCode readiness case
``0921-fx-004`` produced a 400,405-byte prompt (131,065 input tokens) and the judge
burned its whole 64,000-token output ceiling on reasoning -- ``status="incomplete"``,
``incomplete_details.reason "max_output_tokens"``, no answer at all.  The run before
it (300,975 bytes) returned a syntactically broken answer.

Where the bytes are (measured on ``0921-fx-004``)
-------------------------------------------------
    oracle_summary                                 254,449 B  (63.5% of the prompt)
      .actual_persisted_product_state (150 files)  165,569 B  (41.4%)
      .independent_durable_mechanism_observation    15,899 B
    trajectory (stdout.jsonl, 317 records)         112,867 B  (28.2%)
      tool_completed.result_preview                 41,682 B
      tool_started.activity (restates .detail)      15,922 B
      tool_started.detail                            7,675 B
      model_usage_recorded.usage                     6,330 B
      call_id (both sides)                           7,910 B
    agent_artifact                                  12,932 B
    rubric + task_input + native evidence           17,457 B

Two measured facts shape the rules below:

  * DeepCode's trajectory has **no oversized strings** -- the longest physical line in
    ``0921-fx-004`` is under 4,000 chars and the mean record is 354 bytes.  The Codex
    tree's ``evaluator/judge_trajectory_projection.py`` (decision D6) truncates
    individual strings over 10,000 chars and would therefore save *zero* bytes here.
    DeepCode is long in the *number* of records, not in record size, so the bound has
    to act on the record axis: first/last K records verbatim, middle records reduced
    to their action/tool/result summary.
  * The oracle's ``actual_persisted_product_state`` inlines the full parsed ``value``
    of every file the product persisted (3 entries on ``0921-fx-002``, 104 on
    ``fx-003``, 150 on ``fx-004``).  ``evaluator/result_score_caps.py::build_entries``
    reads *none* of that key, nor ``independent_durable_mechanism_observation``, so
    bounding the inlined bodies leaves every evaluator-issued ceiling unchanged.

What is preserved
-----------------
  * Every trajectory record is kept, in order, with its ``id`` and its record type;
    no record is dropped.  Edge records keep every key.  Middle records keep the tool
    name, the error flag and head-truncated action/result text; they lose only the
    provider correlation id, the ``activity`` object (which restates ``detail``) and
    the per-call token breakdown (collapsed to its total).
  * Every persisted-state entry is kept with ``root``, ``relative_path`` and
    ``sha256`` in full -- the judge still sees exactly which files the product wrote
    and can still match a claim to a file.  Only an oversized inlined ``value`` body
    is replaced by a bounded rendering that states its original length and digest.
  * Every truncation carries a marker naming the omitted character count and a
    digest of the original, so the judge can distinguish "the Candidate really
    emitted N more bytes here" from "the Candidate emitted nothing".
  * A file smaller than its threshold is not projected at all: the judge is handed
    the original path, and the prompt is byte-identical to today's.
  * The originals are never modified.  Projections are written beside them inside the
    evaluator-owned run directory and are the paths handed to the shared judge, so
    ``input_manifest.json`` and ``prompt_digest`` in the score contract digest exactly
    the bytes the model saw (result_judge.py:881-885, 906-931).

Toggles
-------
``PROJECT_ORACLE = False`` restores byte-identical oracle input and leaves only the
trajectory bounded; the DeepCode ``fx-004`` prompt then lands near 315 KB instead of
near 150 KB.  The thresholds decide which cases are projected at all.  See README.md.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

RULE = "agentswe-deepcode-judge-input-projection/v1"

# --- trajectory rules -------------------------------------------------------
TRAJECTORY_THRESHOLD_BYTES = 48_000   # below this the original file is used unchanged
KEEP_FIRST_RECORDS = 20               # opening records kept verbatim (setup / first actions)
KEEP_LAST_RECORDS = 20                # closing records kept verbatim (final result / verification)
EDGE_STRING_CHARS = 2_000             # per-string cap inside the kept edges
MIDDLE_DETAIL_CHARS = 90              # per-step action summary cap in the middle band
MIDDLE_RESULT_CHARS = 100             # per-step result summary cap in the middle band
MIDDLE_KEEP_KEYS = ("type", "name", "is_error", "detail", "result_preview",
                    "text", "message", "reason", "status")
MAX_JOIN_LINES = 64                   # physical lines one split record may span

# --- oracle rules -----------------------------------------------------------
PROJECT_ORACLE = True
ORACLE_THRESHOLD_BYTES = 32_000       # below this the original file is used unchanged
ORACLE_VALUE_CHARS = 120              # cap on an inlined persisted-file body
ORACLE_VALUE_FLOOR = 220              # values shorter than this are never wrapped
ORACLE_STRING_CHARS = 800             # cap on any other string in a bounded key
ORACLE_OBSERVATION_CHARS = 4_000      # cap on a whole bounded non-state key
ORACLE_BOUNDED_KEYS = ("actual_persisted_product_state",
                       "independent_durable_mechanism_observation",
                       "observed_scientific_outputs")

MIDDLE_BAND_NOTE = (
    "Records in summarized_range below are evaluator-summarized for prompt size: each keeps its id, "
    "type, tool name, error flag and head-truncated action/result text; each loses only its provider "
    "call_id, its 'activity' object (which restates 'detail') and its per-call token breakdown "
    "(collapsed to usage_total_tokens). No record is dropped and the order is unchanged. A "
    "'[+N chars truncated ...]' marker means the Candidate really emitted N more characters there."
)


ORACLE_LEDGER_EVENTS = 32             # ordered event identities kept from one ledger


# 0921b B1: this gate is the judge-side twin of semantic_oracle._sharded_product_state
# and carried the same defect -- it required an underscore-prefixed path component, an
# unpublished naming convention (see the long note in semantic_oracle.py, and this tree's
# own surface_check_semantics: "These checks prescribe no storage layout, identifier
# format, or file naming").  Left alone it would hand a rejudged contract a higher ceiling
# with the supporting receipts and ledger still invisible to the judge.
#
# The boundary it protects is kept by SHAPE -- `_receipt_summary` and `_ledger_summary`
# both return None unless the value really is a receipt or a ledger, so the driver's
# request bodies and captured responses are still never summarised -- plus the same
# driver-narration path exclusion `semantic_oracle._driver_narration_path` uses.  Only the
# structured summary is affected; the bounded body, the path and the sha256 of every
# persisted file were already projected for every entry, product or not.

DRIVER_NARRATION_COMPONENTS = ("evidence", "logs", "log", "stdout", "stderr",
                               "narration", "report", "reports", "transcript",
                               "transcripts", "captured")
DRIVER_NARRATION_PREFIXES = ("out-", "out_", "output-", "output_", "stdout", "stderr",
                             "response-", "response_")


def _product_private_path(relative_path) -> bool:
    """A persisted file that may carry a structured summary beside its bounded body.

    Shape decides what the summary contains; this decides only that the file is not one
    the case driver wrote as its own narration.
    """
    parts = [part for part in str(relative_path or "").split("/") if part]
    if not parts:
        return False
    if any(part.startswith("_") for part in parts):
        return True
    if any(part.lower() in DRIVER_NARRATION_COMPONENTS for part in parts[:-1]):
        return False
    return not parts[-1].lower().startswith(DRIVER_NARRATION_PREFIXES)


def _receipt_summary(value):
    """The five fields a persisted operation receipt is argued from, or None.

    The 120-char bounded head of a receipt stops inside ``operation_body``; the judge is
    asked to decide C2/C3 from ``accepted``/``error.code``, so those travel explicitly.
    """
    # 123 G1 (judge-side twin of semantic_oracle._is_receipt_shard): `input/03` requirement 20
    # asks for "exit semantics", which `input/02:66-69` publishes as exit 0 <=> accepted and
    # exit 2 <=> `accepted: false` -- so a receipt recording a boolean `accepted` (agreeing with
    # its nested response) qualifies as well as one recording `exit_code`.  Same five fields out;
    # `exit_code` is None when the product did not record one, never invented.
    if not (isinstance(value, dict) and isinstance(value.get("operation_id"), str)
            and isinstance(value.get("response"), dict)):
        return None
    response = value["response"]
    top, inner = value.get("accepted"), response.get("accepted")
    if "exit_code" not in value and not (
            type(top) is bool and (type(inner) is not bool or inner is top)):
        return None
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else None
    if code is None and isinstance(value.get("error_code"), str) and value["error_code"]:
        code = value["error_code"]
    return {"operation_id": value["operation_id"],
            "action": value.get("action") if value.get("action") is not None else response.get("action"),
            "exit_code": value.get("exit_code"),
            "accepted": inner if type(inner) is bool else top,
            "error_code": code}


def _ledger_summary(value):
    """Ordered (kind, sequence, hash prefix) identities of a persisted ledger, or None."""
    if not isinstance(value, dict):
        return None
    for key in ("events", "audit", "ledger", "audit_log", "audit_events", "event_log"):
        events = value.get(key)
        if not (isinstance(events, list) and events and all(isinstance(i, dict) for i in events)):
            continue
        rows = []
        for event in events[:ORACLE_LEDGER_EVENTS]:
            kind = next((event[k] for k in ("event_type", "kind", "type", "action")
                         if isinstance(event.get(k), str)), None)
            sequence = next((event[k] for k in ("sequence", "sequence_number", "index")
                             if type(event.get(k)) is int), None)
            hashes = {k: v[:16] for k, v in event.items()
                      if isinstance(v, str) and len(v) == 64 and "hash" in k}
            rows.append({"kind": kind, "sequence": sequence, "hashes": hashes})
        return {"ledger_key": key, "event_count": len(events), "events_shown": len(rows),
                "head_hash": value.get("head_hash") if isinstance(value.get("head_hash"), str) else None,
                "events": rows}
    return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _marker(value: str, keep: int) -> str:
    raw = value.encode("utf-8", "surrogatepass")
    return (value[:keep] +
            f"\n[evaluator judge projection: original string {len(value)} chars / {len(raw)} bytes, "
            f"sha256 {_sha256_bytes(raw)}; {len(value) - keep} chars omitted here; "
            f"the Candidate really emitted the full content]")


def _short_marker(value: str, keep: int) -> str:
    raw = value.encode("utf-8", "surrogatepass")
    return value[:keep] + f" [+{len(value) - keep} chars truncated, sha256 {_sha256_bytes(raw)[:16]}]"


def _note(path: str, value: str, keep: int, truncated: list) -> None:
    raw = value.encode("utf-8", "surrogatepass")
    truncated.append({"path": path, "original_chars": len(value), "original_bytes": len(raw),
                      "sha256": _sha256_bytes(raw), "omitted_chars": len(value) - keep})


def _project_strings(value, limit: int, path: str, truncated: list):
    """Cap every string in a JSON value at ``limit`` chars, recording each cut."""
    if isinstance(value, dict):
        return {k: _project_strings(v, limit, f"{path}.{k}", truncated) for k, v in value.items()}
    if isinstance(value, list):
        return [_project_strings(v, limit, f"{path}[{i}]", truncated) for i, v in enumerate(value)]
    if isinstance(value, str) and len(value) > limit:
        _note(path, value, limit, truncated)
        return _marker(value, limit)
    return value


def _bound_json_blob(value, limit: int, path: str, truncated: list, floor: int = 0):
    """Bound the serialized size of a whole JSON subtree.

    An inlined persisted-file body may be a deep object of many short strings, so a
    per-string cap does not bound it; this caps its rendering instead.
    """
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= max(limit, floor):
        return value
    _note(path, rendered, limit, truncated)
    raw = rendered.encode("utf-8", "surrogatepass")
    return {"evaluator_bounded_value": {"chars": len(rendered), "sha256": _sha256_bytes(raw)[:16],
                                        "head": rendered[:limit]}}


def _summarize_record(row, ordinal: int, truncated: list):
    """Reduce one middle-band record to its action/tool/result summary."""
    msg = row.get("msg") if isinstance(row, dict) else None
    if not isinstance(msg, dict):
        return row
    kept: dict = {}
    for key, value in msg.items():
        if key in MIDDLE_KEEP_KEYS:
            if isinstance(value, str):
                limit = MIDDLE_DETAIL_CHARS if key in ("detail", "text", "message") else MIDDLE_RESULT_CHARS
                if len(value) > limit:
                    _note(f"[{ordinal}].msg.{key}", value, limit, truncated)
                    value = _short_marker(value, limit)
            kept[key] = value
        elif key == "usage" and isinstance(value, dict):
            kept["usage_total_tokens"] = value.get("total_tokens")
    return {"id": row.get("id"), "msg": kept}


def _parse_records(text: str):
    """Yield (line_index, consumed, row_or_None, raw_line), tolerating split records."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        row = None
        consumed = 1
        # A record whose string content carried raw newline bytes arrives split across
        # physical lines: re-join forward (bounded) before treating it as malformed.
        for extra in range(0, MAX_JOIN_LINES + 1):
            chunk = "\n".join(lines[index:index + 1 + extra])
            try:
                row = json.loads(chunk, strict=False)
            except ValueError:
                continue
            consumed = 1 + extra
            break
        yield index, consumed, row, line
        index += consumed


def _write_manifest(path: Path, manifest: dict) -> Path:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _passthrough_manifest(source: Path, kind: str, threshold: int) -> dict:
    data = Path(source).read_bytes()
    return {"schema_version": RULE, "kind": kind, "projected": False,
            "reason": f"source {len(data)} bytes is at or below the {threshold}-byte projection threshold",
            "source": str(source), "source_bytes": len(data), "source_sha256": _sha256_bytes(data),
            "projection": str(source), "projection_bytes": len(data),
            "projection_sha256": _sha256_bytes(data), "truncated_fields": []}


def project_trajectory(source: Path) -> tuple[Path, Path]:
    """Bound a DeepCode lower-agent stdout.jsonl on the record axis."""
    source = Path(source)
    original = source.read_bytes()
    manifest_path = source.with_name(source.name + ".judge-projection.json")
    if len(original) <= TRAJECTORY_THRESHOLD_BYTES:
        _write_manifest(manifest_path, _passthrough_manifest(source, "trajectory",
                                                             TRAJECTORY_THRESHOLD_BYTES))
        return source, manifest_path
    parsed = list(_parse_records(original.decode("utf-8", "surrogatepass")))
    total = len(parsed)
    first, last = KEEP_FIRST_RECORDS, max(KEEP_FIRST_RECORDS, total - KEEP_LAST_RECORDS)
    out_lines = [json.dumps({"evaluator_projection": RULE, "note": MIDDLE_BAND_NOTE,
                             "records": total, "summarized_range": [first, last - 1],
                             "source_sha256": _sha256_bytes(original)},
                            ensure_ascii=False, separators=(",", ":"))]
    truncated: list = []
    malformed: list = []
    summarized = 0
    for ordinal, (line_index, _consumed, row, line) in enumerate(parsed):
        if row is None:
            raw = line.encode("utf-8", "surrogatepass")
            entry = {"line": line_index, "original_chars": len(line),
                     "original_bytes": len(raw), "sha256": _sha256_bytes(raw)}
            malformed.append(entry)
            out_lines.append(json.dumps({"evaluator_projection": "malformed_candidate_record",
                                         "note": "the Candidate emitted a trajectory record that is not valid "
                                                 "JSON; the original bytes are retained in the source file",
                                         **entry, "head": line[:EDGE_STRING_CHARS]}, ensure_ascii=False))
            continue
        if first <= ordinal < last:
            summarized += 1
            projected = _summarize_record(row, line_index, truncated)
        else:
            projected = _project_strings(row, EDGE_STRING_CHARS, f"[{line_index}]", truncated)
        out_lines.append(json.dumps(projected, ensure_ascii=False, separators=(",", ":")))
    projected_text = ("\n".join(out_lines) + "\n").encode("utf-8", "surrogatepass")
    destination = source.with_name(source.name + ".judge-projection.jsonl")
    destination.write_bytes(projected_text)
    _write_manifest(manifest_path, {
        "schema_version": RULE, "kind": "trajectory", "projected": True,
        "source": str(source), "source_bytes": len(original), "source_sha256": _sha256_bytes(original),
        "projection": str(destination), "projection_bytes": len(projected_text),
        "projection_sha256": _sha256_bytes(projected_text),
        "records": total, "records_dropped": 0, "records_summarized": summarized,
        "summarized_range": [first, last - 1], "malformed_records": malformed,
        "rule": {"threshold_bytes": TRAJECTORY_THRESHOLD_BYTES,
                 "keep_first_records": KEEP_FIRST_RECORDS, "keep_last_records": KEEP_LAST_RECORDS,
                 "edge_string_chars": EDGE_STRING_CHARS,
                 "middle_detail_chars": MIDDLE_DETAIL_CHARS,
                 "middle_result_chars": MIDDLE_RESULT_CHARS,
                 "middle_kept_keys": list(MIDDLE_KEEP_KEYS),
                 "applies_to": "edge records keep every key with strings capped; middle records keep "
                               "id/type/name/is_error and head-truncated action/result text; no record "
                               "is dropped and the order is preserved"},
        "truncated_fields": truncated})
    return destination, manifest_path


def project_oracle(source: Path) -> tuple[Path, Path]:
    """Bound the inlined bodies of the private oracle comparison.

    Every key read by ``evaluator/result_score_caps.py::build_entries`` is left
    untouched, so the evaluator-issued ceilings keep their meaning; the caller
    re-binds the cap contract to these bytes (result_judge.py:752-755).
    """
    source = Path(source)
    original = source.read_bytes()
    manifest_path = source.with_name(source.stem + ".judge-projection.manifest.json")
    if len(original) <= ORACLE_THRESHOLD_BYTES:
        _write_manifest(manifest_path, _passthrough_manifest(source, "oracle_summary",
                                                             ORACLE_THRESHOLD_BYTES))
        return source, manifest_path
    document = json.loads(original.decode("utf-8"))
    truncated: list = []
    projected = dict(document)
    state = document.get("actual_persisted_product_state")
    if isinstance(state, list):
        bounded = []
        for i, entry in enumerate(state):
            if isinstance(entry, dict) and "value" in entry:
                entry = dict(entry)
                product_state = _product_private_path(entry.get("relative_path"))
                receipt = _receipt_summary(entry["value"]) if product_state else None
                ledger = _ledger_summary(entry["value"]) if product_state else None
                entry["value"] = _bound_json_blob(entry["value"], ORACLE_VALUE_CHARS,
                                                  f"actual_persisted_product_state[{i}].value",
                                                  truncated, floor=ORACLE_VALUE_FLOOR)
                if receipt is not None:
                    entry["evaluator_receipt_summary"] = receipt
                if ledger is not None:
                    entry["evaluator_ledger_summary"] = ledger
            bounded.append(entry)
        projected["actual_persisted_product_state"] = bounded
    for key in ORACLE_BOUNDED_KEYS:
        if key == "actual_persisted_product_state" or key not in projected:
            continue
        projected[key] = _project_strings(projected[key], ORACLE_STRING_CHARS, key, truncated)
        projected[key] = _bound_json_blob(projected[key], ORACLE_OBSERVATION_CHARS, key, truncated)
    projected["evaluator_judge_projection"] = {
        "schema_version": RULE,
        "note": "Inlined persisted-file bodies and durable-mechanism detail are bounded for prompt "
                "size. Every persisted path and its sha256 are complete, every comparison key used "
                "for scoring is unchanged, and an 'evaluator_bounded_value' wrapper means the product "
                "really persisted the full value named by its chars/sha256.",
        "source_sha256": _sha256_bytes(original), "source_bytes": len(original),
        "bounded_keys": list(ORACLE_BOUNDED_KEYS)}
    projected_text = (json.dumps(projected, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")) + "\n").encode("utf-8")
    destination = source.with_name(source.stem + ".judge-projection.json")
    destination.write_bytes(projected_text)
    _write_manifest(manifest_path, {
        "schema_version": RULE, "kind": "oracle_summary", "projected": True,
        "source": str(source), "source_bytes": len(original), "source_sha256": _sha256_bytes(original),
        "projection": str(destination), "projection_bytes": len(projected_text),
        "projection_sha256": _sha256_bytes(projected_text),
        "rule": {"threshold_bytes": ORACLE_THRESHOLD_BYTES,
                 "oracle_value_chars": ORACLE_VALUE_CHARS, "oracle_value_floor": ORACLE_VALUE_FLOOR,
                 "oracle_string_chars": ORACLE_STRING_CHARS,
                 "oracle_observation_chars": ORACLE_OBSERVATION_CHARS,
                 "bounded_keys": list(ORACLE_BOUNDED_KEYS),
                 "untouched": "every key read by evaluator/result_score_caps.py build_entries"},
        "truncated_fields": truncated})
    return destination, manifest_path


def project_case(*, trajectory: Path, oracle: Path):
    """Return (trajectory_for_judge, oracle_for_judge, [projection manifests])."""
    projected_trajectory, trajectory_manifest = project_trajectory(Path(trajectory))
    manifests = [trajectory_manifest]
    projected_oracle = Path(oracle)
    if PROJECT_ORACLE:
        projected_oracle, oracle_manifest = project_oracle(Path(oracle))
        manifests.append(oracle_manifest)
    return projected_trajectory, projected_oracle, manifests


if __name__ == "__main__":
    _t, _o, _mans = project_case(trajectory=Path(sys.argv[1]), oracle=Path(sys.argv[2]))
    for _man in _mans:
        _m = json.loads(Path(_man).read_text())
        print(f"{_m['kind']}: projected={_m['projected']} {_m['source_bytes']} -> "
              f"{_m['projection_bytes']} bytes, truncated fields {len(_m['truncated_fields'])}")
