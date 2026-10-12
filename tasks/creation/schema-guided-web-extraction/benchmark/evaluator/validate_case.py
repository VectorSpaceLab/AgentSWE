#!/usr/bin/env python3
"""Strict deterministic validator for candidate final bundles."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import struct
import urllib.parse
import zlib
from collections import defaultdict
from pathlib import Path

from case_truth import build_truth


HERE = Path(__file__).resolve().parent
ORACLES = HERE / "oracles.json"
REQUIRED_FILES = ["records.json", "evidence.json", "interaction_trace.json", "session_summary.json", "run_report.json"]
METHODS = {"dom", "json", "vcard", "ocr", "join", "inferred"}
DEPTHS = {"local_file", "browser_rendered", "partial_page", "full_page"}
USAGE_KEYS = {"gateway", "gateway_image", "serper", "web_retrieval"}
COUNTER_KEYS = {"local_reads", "browser_navigations", "gateway", "gateway_image", "serper", "web_retrieval", "retries"}
ACTIONS = {"open", "set_mode", "load_more", "visit_detail", "read_local", "ocr", "merge", "apply_update", "parse_failure", "checksum", "apply_patch", "policy_block", "exclude", "state_check"}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"non-finite JSON number: {x}")))


def norm(value):
    if isinstance(value, float): return round(value, 8)
    if isinstance(value, list): return [norm(v) for v in value]
    if isinstance(value, dict): return {k: norm(v) for k, v in value.items()}
    return value


def same(a, b) -> bool:
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return math.isfinite(float(a)) and math.isfinite(float(b)) and math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-7)
    return norm(a) == norm(b)


def validate_instance(value, sch: dict, where: str, errors: list[str]) -> None:
    typ = sch.get("type")
    ok = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str), "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)), "integer": isinstance(value, int) and not isinstance(value, bool), "boolean": isinstance(value, bool), "null": value is None}.get(typ, True)
    if not ok: errors.append(f"schema_type:{where}:{typ}"); return
    if "const" in sch and value != sch["const"]: errors.append(f"schema_const:{where}")
    if "enum" in sch and value not in sch["enum"]: errors.append(f"schema_enum:{where}")
    if isinstance(value, str):
        if "pattern" in sch and re.fullmatch(sch["pattern"], value) is None: errors.append(f"schema_pattern:{where}")
        if sch.get("format") == "date":
            try: dt.date.fromisoformat(value)
            except ValueError: errors.append(f"schema_format_date:{where}")
        if sch.get("format") == "date-time":
            if "T" not in value: errors.append(f"schema_format_datetime:{where}")
            else:
                try: dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError: errors.append(f"schema_format_datetime:{where}")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in sch and value < sch["minimum"]: errors.append(f"schema_minimum:{where}")
    if isinstance(value, list):
        if len(value) < sch.get("minItems", 0): errors.append(f"schema_min_items:{where}")
        if sch.get("uniqueItems") and len({json.dumps(norm(v), sort_keys=True) for v in value}) != len(value): errors.append(f"schema_unique_items:{where}")
        if isinstance(sch.get("items"), dict):
            for i, item in enumerate(value): validate_instance(item, sch["items"], f"{where}/{i}", errors)
    if isinstance(value, dict):
        for key in sch.get("required", []):
            if key not in value: errors.append(f"schema_required:{where}/{key}")
        props = sch.get("properties", {})
        if sch.get("additionalProperties") is False:
            for key in value:
                if key not in props: errors.append(f"schema_additional:{where}/{key}")
        for key, child in props.items():
            if key in value: validate_instance(value[key], child, f"{where}/{key}", errors)


def png_info(path: Path) -> tuple[int, int, list[str], int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"): raise ValueError("bad signature")
    pos = 8; width = height = idat = 0; chunks = []
    while pos + 12 <= len(data):
        size = struct.unpack(">I", data[pos:pos+4])[0]; kind = data[pos+4:pos+8]; payload = data[pos+8:pos+8+size]
        if pos + size + 12 > len(data): raise ValueError("truncated chunk")
        crc = struct.unpack(">I", data[pos+8+size:pos+12+size])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc: raise ValueError("crc mismatch")
        chunks.append(kind.decode("ascii", errors="replace"))
        if kind == b"IHDR": width, height = struct.unpack(">II", payload[:8])
        if kind == b"IDAT": idat += size
        pos += size + 12
        if kind == b"IEND": break
    if not chunks or chunks[-1] != "IEND": raise ValueError("missing IEND")
    return width, height, chunks, idat


def source_path(case_dir: Path, raw: str) -> tuple[Path, str] | None:
    if not isinstance(raw, str) or not raw: return None
    parsed = urllib.parse.urlparse(raw)
    candidates = []
    if parsed.scheme in {"http", "https"}:
        if parsed.hostname not in {"127.0.0.1", "localhost"}: return None
        clean = urllib.parse.unquote(parsed.path).lstrip("/")
        candidates.extend([case_dir / "assets/site" / clean, case_dir / "assets" / clean, case_dir / clean])
    elif parsed.scheme:
        return None
    else:
        clean = urllib.parse.unquote(raw).split("?", 1)[0].split("#", 1)[0]
        candidates.extend([case_dir / clean, case_dir / "assets" / clean])
    assets = (case_dir / "assets").resolve()
    for candidate in candidates:
        try: resolved = candidate.resolve(); resolved.relative_to(assets)
        except (OSError, ValueError): continue
        if resolved.is_file() and not resolved.is_symlink(): return resolved, resolved.relative_to(case_dir).as_posix()
    return None


def output_path(output: Path, raw: str, prefix: str | None = None) -> Path | None:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute(): return None
    try: path = (output / raw).resolve(); path.relative_to(output)
    except (OSError, ValueError): return None
    if prefix and not Path(raw).as_posix().startswith(prefix): return None
    return path


def locator_matches(locator: str, target: str) -> bool:
    if not isinstance(locator, str) or not locator: return False
    loc = urllib.parse.unquote(locator).replace("\\", "/").lower()
    target_l = target.replace("\\", "/").lower()
    if target_l.startswith("control:"):
        words = [x for x in re.split(r"[^a-z0-9]+", target_l.split(":", 1)[1]) if x]
        return all(word in loc for word in words)
    if "#sequence=" in target_l:
        rid, seq = target_l.split("#sequence=", 1)
        return rid in loc and re.search(rf"(?:sequence|seq)[^0-9]*{re.escape(seq)}(?:\D|$)", loc) is not None
    if target_l.startswith("shard:"):
        token = target_l.split(":", 1)[1]
        return "shard" in loc and re.search(rf"(?:shard[^a-z0-9]*)?{re.escape(token)}(?:\D|$)", loc) is not None
    if "/" not in target_l:
        return target_l in loc
    variants = {target_l}
    for prefix in ("assets/site/", "assets/data/", "assets/recovery/", "assets/"):
        if target_l.startswith(prefix): variants.add(target_l[len(prefix):])
    return any(len(v) >= 4 and v in loc for v in variants)


def find_event(events: list[dict], action: str, target: str, used: set[int] | None = None) -> tuple[int, dict] | None:
    for i, event in enumerate(events):
        if not isinstance(event, dict): continue
        if used is not None and i in used: continue
        if event.get("action") == action and locator_matches(event.get("locator", ""), target): return i, event
    return None


def observation_matches(candidate: dict, expected: dict, sources: dict[str, dict]) -> bool:
    if not isinstance(candidate, dict) or not same(candidate.get("value"), expected["value"]): return False
    source = sources.get(candidate.get("source_id"), {})
    if source.get("canonical_path") != expected["path"]: return False
    for key in ("observed_at", "sequence", "validity", "status"):
        if key in expected and candidate.get(key) != expected[key]: return False
    return True


def excluded_contains(item, token: str) -> bool:
    return token.lower() in json.dumps(item, ensure_ascii=False, sort_keys=True).lower()


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--case-dir", type=Path, required=True); ap.add_argument("--output-dir", type=Path, required=True); ap.add_argument("--result", type=Path, required=True); args = ap.parse_args()
    case_dir = args.case_dir.resolve(); output = args.output_dir.resolve(); case_id = case_dir.name; errors: list[str] = []
    oracle_doc = load_json(ORACLES); spec = oracle_doc.get("cases", {}).get(case_id)
    if not spec:
        errors.append(f"unsupported_case:{case_id}"); spec = {"schema": {}, "identity_field": "id", "records": [], "workflow": {}, "ocr_fields": []}; truth = {"fields": {}, "conflicts": [], "targets": {}, "required_states": [], "state_steps": [], "exclusions": [], "ordered_targets": {}, "before": [], "screenshot_action_min": {}}
    else:
        try: truth = build_truth(case_dir, spec)
        except Exception as exc:
            errors.append(f"evaluator_truth_error:{type(exc).__name__}:{exc}"); truth = {"fields": {}, "conflicts": [], "targets": {}, "required_states": [], "state_steps": [], "exclusions": [], "ordered_targets": {}, "before": [], "screenshot_action_min": {}}

    values = {}
    for name in REQUIRED_FILES:
        path = output / name
        if not path.is_file() or path.is_symlink(): errors.append(f"missing:{name}"); continue
        try: values[name] = load_json(path)
        except Exception as exc: errors.append(f"invalid_json:{name}:{type(exc).__name__}")

    def sanitize(node,where=''):
        if isinstance(node,list):
            return [sanitize(item,where+'/'+str(i)) for i,item in enumerate(node)]
        if not isinstance(node,dict):return node
        copy={key:sanitize(value,where+'/'+key) for key,value in node.items()}
        string_fields={'source_id','record_id','action_id','field','method','access_depth','action','locator','state','conflict_id'}
        for key in string_fields & copy.keys():
            if not isinstance(copy[key],str):errors.append('auxiliary_type:'+where+'/'+key);copy[key]=''
        for key in ('reached_record_ids','completed_states'):
            if isinstance(copy.get(key),list) and not all(isinstance(v,str) for v in copy[key]):errors.append('auxiliary_type:'+where+'/'+key);copy[key]=[v for v in copy[key] if isinstance(v,str)]
        for key in ('counters','usage'):
            if isinstance(copy.get(key),dict):
                for field,value in copy[key].items():
                    if type(value) is not int or value<0:errors.append('auxiliary_type:'+where+'/'+key+'/'+field);copy[key][field]=0
        return copy
    for name in REQUIRED_FILES:
        if name!='records.json' and name in values:values[name]=sanitize(values[name],name)

    records = values.get("records.json", [])
    if not isinstance(records, list): errors.append("records_not_array"); records = []
    identity = spec["identity_field"]
    for i, row in enumerate(records): validate_instance(row, spec["schema"], f"records/{i}", errors)
    actual_by_id = {}
    for row in records:
        if not isinstance(row, dict): continue
        rid = row.get(identity)
        if not isinstance(rid, str) or not rid: errors.append("record_identity_missing"); continue
        if rid in actual_by_id: errors.append(f"duplicate_record_identity:{rid}")
        actual_by_id[rid] = row
    expected_by_id = {r[identity]: r for r in spec["records"]}
    for rid in sorted(set(expected_by_id) - set(actual_by_id)): errors.append(f"missing_record:{rid}")
    for rid in sorted(set(actual_by_id) - set(expected_by_id)): errors.append(f"extra_record:{rid}")
    for rid in sorted(set(expected_by_id) & set(actual_by_id)):
        for field, expected in expected_by_id[rid].items():
            if field not in actual_by_id[rid] or not same(actual_by_id[rid].get(field), expected): errors.append(f"field_mismatch:{rid}/{field}")

    trace = values.get("interaction_trace.json", []); events = trace if isinstance(trace, list) else []; action_ids = set(); action_counts = defaultdict(int); states = []; screenshot_refs = []; event_by_id = {}; previous_counters = {k: 0 for k in COUNTER_KEYS}
    if not events: errors.append("trace_empty")
    for i, event in enumerate(events, 1):
        if not isinstance(event, dict): errors.append(f"trace_event_shape:{i}"); continue
        if event.get("seq") != i: errors.append(f"trace_sequence:{i}")
        aid = event.get("action_id")
        if not isinstance(aid, str) or not aid: errors.append(f"trace_action_id:{i}")
        elif aid in action_ids: errors.append(f"trace_action_duplicate:{aid}")
        else: action_ids.add(aid); event_by_id[aid] = event
        action = event.get("action")
        if action not in ACTIONS: errors.append(f"trace_action:{i}")
        else: action_counts[action] += 1
        locator = event.get("locator")
        if not isinstance(locator, str) or not locator: errors.append(f"trace_locator:{i}")
        elif re.search(r"https?://(?!127\.0\.0\.1|localhost)", locator, re.I): errors.append(f"trace_external_locator:{i}")
        elif locator.strip().lower() in {"step", "source", "record", "decision", "assets/state"}: errors.append(f"trace_placeholder_locator:{i}")
        if not isinstance(event.get("outcome"), str) or not event.get("outcome"): errors.append(f"trace_outcome:{i}")
        state = event.get("state")
        if isinstance(state, str) and state: states.append((i - 1, state))
        shot = event.get("screenshot")
        if shot is not None:
            if output_path(output, shot, "screenshots/") is None: errors.append(f"trace_screenshot_path:{i}")
            else: screenshot_refs.append((i - 1, shot))
        counters = event.get("counters")
        if not isinstance(counters, dict) or set(counters) != COUNTER_KEYS: errors.append(f"trace_counters:{i}")
        else:
            for key in COUNTER_KEYS:
                val = counters[key]
                if isinstance(val, bool) or not isinstance(val, int) or val < previous_counters[key]: errors.append(f"trace_counter_value:{i}:{key}")
            if counters.get("gateway_image", 0) > counters.get("gateway", 0): errors.append(f"trace_counter_image_subset:{i}")
            previous_counters = dict(counters)

    used = defaultdict(set)
    for step in truth.get("state_steps", []):
        found = find_event(events, step["action"], step["target"], used[step["action"]])
        if not found: errors.append(f"required_state_action_missing:{step['action']}:{step['target']}"); continue
        idx, event = found; used[step["action"]].add(idx)
        if event.get("state") != step["state"]: errors.append(f"required_state_mismatch:{step['state']}")
        if step.get("screenshot") and not event.get("screenshot"): errors.append(f"required_state_screenshot:{step['state']}")
    for action, targets in truth.get("targets", {}).items():
        used_targets = set()
        for target in targets:
            found = find_event(events, action, target, used_targets)
            if not found: errors.append(f"required_action_target_missing:{action}:{target}")
            else: used_targets.add(found[0])
    required_states = truth.get("required_states", [])
    state_positions = []
    for state in required_states:
        pos = next((i for i, value in states if value == state and (not state_positions or i > state_positions[-1])), None)
        if pos is None: errors.append(f"required_state_missing_or_out_of_order:{state}")
        else: state_positions.append(pos)
    for action, targets in truth.get("ordered_targets", {}).items():
        positions = []
        for target in targets:
            found = find_event(events, action, target)
            if not found: continue
            positions.append(found[0])
        if len(positions) == len(targets) and positions != sorted(positions): errors.append(f"required_action_order:{action}")
    for before, after in truth.get("before", []):
        first = find_event(events, before[0], before[1]); second = find_event(events, after[0], after[1])
        if first and second and first[0] >= second[0]: errors.append(f"required_precedence:{before[0]}:{after[0]}")

    evidence = values.get("evidence.json", {}); sources_by_id = {}; canonical_sources = {}; field_evidence = []; conflicts = []; uncertainties = []
    if not isinstance(evidence, dict): errors.append("evidence_not_object")
    else:
        for key in ("sources", "records", "field_evidence", "conflicts", "uncertainties"):
            if not isinstance(evidence.get(key), list): errors.append(f"evidence_array_missing:{key}")
        for source in evidence.get("sources", []) if isinstance(evidence.get("sources"), list) else []:
            if not isinstance(source, dict): errors.append("source_shape"); continue
            sid = source.get("source_id"); raw = source.get("path")
            if not isinstance(sid, str) or not sid or sid in sources_by_id: errors.append("source_id_invalid"); continue
            resolved = source_path(case_dir, raw)
            if not resolved: errors.append(f"source_unauthorized_or_missing:{sid}"); canonical = None
            else:
                path, canonical = resolved; expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if source.get("content_sha256") != expected_hash: errors.append(f"source_hash_mismatch:{sid}")
            if source.get("access_depth") not in DEPTHS: errors.append(f"source_access_depth:{sid}")
            copy = dict(source); copy["canonical_path"] = canonical; sources_by_id[sid] = copy
            if canonical:
                if canonical in canonical_sources: errors.append(f"source_path_duplicate:{canonical}")
                canonical_sources[canonical] = sid
        field_evidence = evidence.get("field_evidence", []) if isinstance(evidence.get("field_evidence"), list) else []
        conflicts = evidence.get("conflicts", []) if isinstance(evidence.get("conflicts"), list) else []
        uncertainties = evidence.get("uncertainties", []) if isinstance(evidence.get("uncertainties"), list) else []
        rec_evidence = evidence.get("records", []) if isinstance(evidence.get("records"), list) else []
        rec_ids = [x.get("record_id") for x in rec_evidence if isinstance(x, dict) and set(x) == {"record_id"}]
        if len(rec_ids) != len(rec_evidence) or len(rec_ids) != len(set(rec_ids)) or set(rec_ids) != set(actual_by_id): errors.append("evidence_records_identity_set")

    indexed = defaultdict(list)
    for i, item in enumerate(field_evidence):
        if not isinstance(item, dict): errors.append(f"field_evidence_shape:{i}"); continue
        rid, field = item.get("record_id"), item.get("field"); indexed[(rid, field)].append(item)
        if rid not in actual_by_id: errors.append(f"field_evidence_identity:{i}")
        if not isinstance(field, str) or not field.startswith("/"): errors.append(f"field_evidence_pointer:{i}")
        source = sources_by_id.get(item.get("source_id"))
        if not source: errors.append(f"field_evidence_source:{i}")
        event = event_by_id.get(item.get("action_id"))
        if not event: errors.append(f"field_evidence_action:{i}")
        if item.get("method") not in METHODS: errors.append(f"field_evidence_method:{i}")
        if item.get("access_depth") not in DEPTHS: errors.append(f"field_evidence_depth:{i}")
        elif source and item.get("access_depth") != source.get("access_depth"): errors.append(f"field_evidence_depth_mismatch:{i}")
        if not isinstance(item.get("locator"), str) or not item.get("locator"): errors.append(f"field_evidence_locator:{i}")

    for rid, row in actual_by_id.items():
        for field, value in row.items():
            key = (rid, "/" + field); matches = []
            for item in indexed.get(key, []):
                if not same(item.get("value"), value): continue
                source = sources_by_id.get(item.get("source_id"), {}); event = event_by_id.get(item.get("action_id"), {})
                for accepted in truth.get("fields", {}).get(rid, {}).get("/" + field, []):
                    if source.get("canonical_path") == accepted["path"] and item.get("method") == accepted["method"] and event.get("action") == accepted["action"] and locator_matches(event.get("locator", ""), accepted["target"]): matches.append(item)
            if not matches: errors.append(f"field_evidence_unsupported:{rid}/{field}")
            elif any(x.get("method") == "ocr" for x in matches):
                valid_ocr = False
                for item in matches:
                    if item.get("method") != "ocr": continue
                    locator = item.get("locator", "")
                    region = re.search(r"(?i)(?:xywh|bbox|region|crop)[^0-9]*(\d+)[^0-9]+(\d+)[^0-9]+(\d+)[^0-9]+(\d+)", locator)
                    confidence = item.get("confidence"); has_uncertainty = any(isinstance(x, dict) and x.get("record_id") == rid and x.get("field") == "/" + field for x in uncertainties)
                    if region and ((isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1) or has_uncertainty): valid_ocr = True
                if not valid_ocr: errors.append(f"ocr_evidence_region_confidence:{rid}/{field}")

    conflict_by_id = {}; semantic = defaultdict(list)
    for i, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict): errors.append(f"conflict_shape:{i}"); continue
        cid = conflict.get("conflict_id")
        if not isinstance(cid, str) or not cid or cid in conflict_by_id: errors.append(f"conflict_id:{i}"); continue
        conflict_by_id[cid] = conflict; semantic[(conflict.get("record_id"), conflict.get("field"))].append(conflict)
        if conflict.get("action_id") not in action_ids: errors.append(f"conflict_action:{cid}")
        if not isinstance(conflict.get("observations"), list) or len(conflict["observations"]) < 2: errors.append(f"conflict_observations:{cid}")
        if "selected" not in conflict or not isinstance(conflict.get("reason"), str) or not conflict.get("reason"): errors.append(f"conflict_selection:{cid}")
    for req in truth.get("conflicts", []):
        candidates = [x for x in semantic.get((req["record_id"], req["field"]), []) if same(x.get("selected"), req["selected"])]
        valid = False
        for candidate in candidates:
            obs = candidate.get("observations", [])
            if all(any(observation_matches(x, expected, sources_by_id) for x in obs) for expected in req["observations"]):
                event = event_by_id.get(candidate.get("action_id"), {})
                if req["record_id"].lower() in event.get("locator", "").lower() or any(locator_matches(event.get("locator", ""), x["path"]) for x in req["observations"]): valid = True
        if not valid: errors.append(f"required_conflict_semantics:{req['record_id']}:{req['field']}")

    summary = values.get("session_summary.json", {})
    if not isinstance(summary, dict): errors.append("summary_not_object")
    else:
        if summary.get("case_id") != case_id: errors.append("summary_case_id")
        if summary.get("completed") is not True: errors.append("summary_not_completed")
        expected_mode = "browser" if spec["workflow"].get("browser") else "local_file"
        if summary.get("workflow_mode") != expected_mode: errors.append("summary_workflow_mode")
        reached = summary.get("reached_record_ids")
        if not isinstance(reached, list) or len(reached) != len(set(reached)) or set(reached) != set(expected_by_id): errors.append("summary_reached_ids")
        completed = summary.get("completed_states")
        if not isinstance(completed, list) or completed != required_states: errors.append("summary_completed_states")
        exclusions = summary.get("excluded_observations")
        if not isinstance(exclusions, list): errors.append("summary_exclusions")
        else:
            for token in truth.get("exclusions", []):
                if not any(excluded_contains(x, token) for x in exclusions): errors.append(f"summary_exclusion_missing:{token}")
            for x in exclusions:
                if isinstance(x, dict) and (not isinstance(x.get("reason"), str) or not x.get("reason") or x.get("action_id") not in action_ids): errors.append("summary_exclusion_shape")
        if not isinstance(summary.get("limitations"), list): errors.append("summary_limitations")

    min_shots = spec["workflow"].get("screenshots_min", 0); shots_dir = output / "screenshots"; shot_files = []
    if shots_dir.is_dir() and not shots_dir.is_symlink(): shot_files = [p for p in shots_dir.iterdir() if p.is_file() and not p.is_symlink()]
    if min_shots:
        refs = [x[1] for x in screenshot_refs]
        if len(set(refs)) < min_shots or len(shot_files) < min_shots: errors.append(f"screenshots_insufficient:{len(shot_files)}/{min_shots}")
        hashes = {}; asset_png_hashes = {hashlib.sha256(p.read_bytes()).hexdigest() for p in (case_dir / "assets").rglob("*.png")}
        for relpath in set(refs):
            path = output_path(output, relpath, "screenshots/")
            try:
                if path is None or not path.is_file() or path.is_symlink(): raise ValueError("missing")
                width, height, _, idat = png_info(path); digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if width < 300 or height < 150 or path.stat().st_size < 2048 or idat < 1024: errors.append(f"screenshot_low_information:{relpath}")
                if digest in hashes: errors.append(f"screenshot_duplicate_pixels:{relpath}:{hashes[digest]}")
                hashes[digest] = relpath
                if digest in asset_png_hashes: errors.append(f"screenshot_copied_source_image:{relpath}")
            except Exception: errors.append(f"screenshot_invalid:{relpath}")
        for action, minimum in truth.get("screenshot_action_min", {}).items():
            if sum(1 for i, _ in screenshot_refs if isinstance(events[i], dict) and events[i].get("action") == action) < minimum: errors.append(f"screenshot_action_missing:{action}")
    elif shot_files or screenshot_refs: errors.append("static_case_fabricated_screenshots")

    report = values.get("run_report.json", {})
    if not isinstance(report, dict): errors.append("run_report_not_object")
    else:
        if report.get("status") != "ok": errors.append("run_report_status")
        paths = report.get("artifact_paths")
        if not isinstance(paths, list): errors.append("artifact_paths")
        else:
            names = set()
            for raw in paths:
                path = output_path(output, raw)
                if path is None or not path.is_file(): errors.append("artifact_path_invalid")
                elif isinstance(raw, str): names.add(Path(raw).name)
            if not set(REQUIRED_FILES).issubset(names): errors.append("artifact_paths")
        if not isinstance(report.get("errors"), list) or report.get("errors"): errors.append("run_report_errors")
        usage = report.get("usage")
        if not isinstance(usage, dict) or set(usage) != USAGE_KEYS: errors.append("usage_shape")
        else:
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in usage.values()): errors.append("usage_values")
            if usage.get("gateway_image", 0) > usage.get("gateway", 0): errors.append("usage_image_subset")
            if usage.get("gateway", 0) > 300 or usage.get("gateway_image", 0) > 100: errors.append("usage_global_budget")
            if usage.get("serper", 0) or usage.get("web_retrieval", 0): errors.append("closed_corpus_network_use")
            if case_id == "test_005" and usage.get("gateway", 0) > 4: errors.append("case_llm_budget")
        local_reads = report.get("local_reads"); nav = report.get("browser_navigations"); retries = report.get("retries")
        for key, value in (("local_reads", local_reads), ("browser_navigations", nav), ("retries", retries)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0: errors.append(f"{key}_invalid")
        if isinstance(local_reads, int) and isinstance(nav, int) and local_reads + nav > spec["workflow"].get("max_local_reads", 200): errors.append("case_local_work_budget")
        if not spec["workflow"].get("browser") and nav != 0: errors.append("static_browser_navigation")
        if case_id == "test_005" and isinstance(retries, int) and retries > 1: errors.append("retries_case_budget")
        if not isinstance(report.get("elapsed_seconds"), (int, float)) or isinstance(report.get("elapsed_seconds"), bool) or not 0 <= report.get("elapsed_seconds", -1) <= 600: errors.append("elapsed_seconds")
        if events and isinstance(events[-1], dict) and isinstance(events[-1].get("counters"), dict):
            final = events[-1]["counters"]
            expected_counters = {"local_reads": local_reads, "browser_navigations": nav, "gateway": usage.get("gateway") if isinstance(usage, dict) else None, "gateway_image": usage.get("gateway_image") if isinstance(usage, dict) else None, "serper": usage.get("serper") if isinstance(usage, dict) else None, "web_retrieval": usage.get("web_retrieval") if isinstance(usage, dict) else None, "retries": retries}
            for key, value in expected_counters.items():
                if final.get(key) != value: errors.append(f"report_trace_counter_mismatch:{key}")

    infrastructure_errors = [e for e in errors if e.startswith(("unsupported_case:", "evaluator_truth_error:"))]
    browser_evidence = None
    if not infrastructure_errors:
        from workflow_browser import verify
        browser_evidence = verify(case_dir,output,spec,truth,args.result.parent/'browser-work')
        if browser_evidence.get('evaluation_state')=='infrastructure_error':infrastructure_errors.append('trusted browser infrastructure failed')
        elif not browser_evidence.get('valid'):errors.append('workflow_browser_incomplete')
    fatal_errors = [e for e in errors if e.startswith(("missing:records.json", "invalid_json:records.json")) or e == "records_not_array"]
    fatal_errors.extend(e for e in errors if e in {
        "closed_corpus_network_use", "usage_global_budget", "case_llm_budget",
        "case_local_work_budget", "retries_case_budget",
    })
    workflow_prefixes = (
        "trace_", "action_", "state_", "summary_", "screenshot", "required_action", "required_state", "workflow_browser",
        "missing:interaction_trace.json", "missing:session_summary.json",
        "invalid_json:interaction_trace.json", "invalid_json:session_summary.json",
        "static_case_fabricated", "static_browser_navigation",
    )
    workflow_errors = [e for e in errors if e.startswith(workflow_prefixes)]
    quality_errors = [e for e in errors if e not in infrastructure_errors and e not in fatal_errors]
    state = "infrastructure_error" if infrastructure_errors else "fatal_zero" if fatal_errors else "scoreable"
    result = {"case": case_id, "valid": not errors, "validity_gate": state == "scoreable", "evaluation_state": state,
              "errors": errors, "fatal_errors": fatal_errors, "quality_errors": quality_errors,
              "infrastructure_errors": infrastructure_errors, "hard_feature_valid": not workflow_errors,
              "hard_feature_errors": workflow_errors, "hard_feature_ceiling": 20,
              "expected_record_count": len(expected_by_id), "actual_record_count": len(records), "required_actions": {k: len(v) for k, v in truth.get("targets", {}).items()}, "observed_actions": dict(action_counts), "required_conflicts": len(truth.get("conflicts", [])), "observed_conflicts": len(conflict_by_id)}
    result['browser_evidence']=browser_evidence
    args.result.parent.mkdir(parents=True, exist_ok=True); args.result.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if not errors else 2


if __name__ == "__main__": raise SystemExit(main())
