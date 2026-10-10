#!/usr/bin/env python3
"""Build evaluator-only source-grounded bundles for validator regression tests."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import zlib
from pathlib import Path

from case_truth import build_truth


HERE = Path(__file__).resolve().parent
COUNTERS = ("local_reads", "browser_navigations", "gateway", "gateway_image", "serper", "web_retrieval", "retries")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def screenshot_png(path: Path, seed_text: str) -> None:
    """Create a deterministic page-like regression image unique to a real state target."""
    width, height = 960, 540; seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16); raw = bytearray()
    bars = [80 + ((seed >> (i * 3)) & 127) for i in range(18)]
    for y in range(height):
        raw.append(0)
        for x in range(width):
            r = g = b = 247
            if y < 54: r, g, b = 32, 45, 61
            elif 90 <= y < 118 and 55 <= x < 55 + bars[0] * 4: r, g, b = 45, 58, 72
            else:
                line = (y - 145) // 20
                if 0 <= line < len(bars) and 58 <= x < 58 + bars[line] * 3 and 145 + line * 20 <= y < 151 + line * 20: r, g, b = 67, 78, 89
                if 700 <= x < 900 and 105 <= y < 420 and ((x + y + seed) % 29) < 3: r, g, b = 126, 151, 174
            raw.extend((r, g, b))
    def chunk(kind, payload): return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)


def build(case_dir: Path, output: Path) -> None:
    case_id = case_dir.name; spec = json.loads((HERE / "oracles.json").read_text())["cases"][case_id]; truth = build_truth(case_dir, spec)
    if output.exists(): shutil.rmtree(output)
    output.mkdir(parents=True)
    assets = sorted(p for p in (case_dir / "assets").rglob("*") if p.is_file())
    sources = []
    for i, path in enumerate(assets, 1):
        canonical = path.relative_to(case_dir).as_posix(); depth = "browser_rendered" if spec["workflow"]["browser"] and path.suffix.lower() in {".html", ".png"} else "local_file"
        sources.append({"source_id": f"src-{i:04d}", "path": canonical, "access_depth": depth, "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    source_by_path = {x["path"]: x for x in sources}

    events = []; event_for = {}; counters = {k: 0 for k in COUNTERS}
    def add(action: str, target: str, state: str | None = None, screenshot: bool = False):
        if (action, target) in event_for: return event_for[(action, target)]
        if action in {"read_local", "ocr"}: counters["local_reads"] += 1
        if spec["workflow"]["browser"] and action in {"open", "set_mode", "load_more", "visit_detail"}: counters["browser_navigations"] += 1
        event = {"seq": len(events) + 1, "action_id": f"act-{len(events)+1:04d}", "action": action, "locator": target, "outcome": "blocked" if action == "policy_block" else "invalid_json_observed" if action == "parse_failure" else "verified" if action == "checksum" else "ok", "state": state or (events[-1]["state"] if events else "started"), "counters": dict(counters)}
        if screenshot:
            shot = f"screenshots/state-{len([x for x in events if x.get('screenshot')])+1:02d}.png"; screenshot_png(output / shot, f"{case_id}:{state}:{target}"); event["screenshot"] = shot
        events.append(event); event_for[(action, target)] = event
        return event

    for step in truth.get("state_steps", []): add(step["action"], step["target"], step["state"], step.get("screenshot", False))

    if case_id == "test_005":
        root = "assets/recovery/"
        ordered = [
            ("read_local", root + "manifest.json"), ("read_local", root + "primary/shard-a.json"), ("read_local", root + "primary/shard-b.json"),
            ("parse_failure", root + "primary/shard-b.json"), ("read_local", root + "fallback/shard-b.snapshot.json"), ("checksum", root + "fallback/shard-b.snapshot.json"),
            ("read_local", root + "fallback/journal.ndjson"),
        ]
        ordered += [("apply_patch", x) for x in truth["ordered_targets"]["apply_patch"]]
        ordered += [("read_local", root + "primary/shard-c.json"), ("policy_block", truth["targets"]["policy_block"][0])]
        ordered += [("merge", x) for x in truth["targets"]["merge"]]
        state_map = {("parse_failure", root + "primary/shard-b.json"): "primary:degraded", ("checksum", root + "fallback/shard-b.snapshot.json"): "fallback:verified", ("apply_patch", truth["ordered_targets"]["apply_patch"][-1]): "journal:applied", ("merge", truth["targets"]["merge"][-1]): "dataset:complete"}
        for action, target in ordered: add(action, target, state_map.get((action, target)))
    else:
        for action, targets in truth.get("targets", {}).items():
            for target in targets: add(action, target)
        if not truth.get("state_steps"):
            for state in truth.get("required_states", []): add("state_check", "state:" + state, state)

    for action, minimum in truth.get("screenshot_action_min", {}).items():
        candidates = [x for x in events if x["action"] == action]
        for event in candidates[:minimum]:
            if "screenshot" not in event:
                shot = f"screenshots/state-{len([x for x in events if x.get('screenshot')])+1:02d}.png"; screenshot_png(output / shot, f"{case_id}:{event['state']}:{event['locator']}"); event["screenshot"] = shot

    def event_for_rule(accepted: dict):
        event = event_for.get((accepted["action"], accepted["target"]))
        if not event: raise KeyError((accepted["action"], accepted["target"]))
        return event

    field_evidence = []
    for row in spec["records"]:
        rid = row[spec["identity_field"]]
        for field, value in row.items():
            accepted = truth["fields"][rid]["/" + field][0]; source = source_by_path[accepted["path"]]; event = event_for_rule(accepted)
            locator = f"{accepted['path']}#record={rid};field=/{field}"
            item = {"record_id": rid, "field": "/" + field, "value": value, "source_id": source["source_id"], "locator": locator, "access_depth": source["access_depth"], "method": accepted["method"], "action_id": event["action_id"]}
            if accepted["method"] == "ocr": item.update({"locator": accepted["path"] + "#xywh=0,0,720,220", "confidence": 0.99})
            field_evidence.append(item)

    def decision_event(req: dict):
        rid = req["record_id"]
        for action in ("merge", "apply_update", "apply_patch", "exclude"):
            for (a, target), event in event_for.items():
                if a == action and rid in target: return event
        for obs in req["observations"]:
            for action in ("read_local", "open", "load_more", "visit_detail"):
                if (action, obs["path"]) in event_for: return event_for[(action, obs["path"])]
        raise KeyError(req)

    conflicts = []
    for n, req in enumerate(truth.get("conflicts", []), 1):
        event = decision_event(req); obs = []
        for expected in req["observations"]:
            item = {"source_id": source_by_path[expected["path"]]["source_id"], "value": expected["value"]}
            for key in ("observed_at", "sequence", "validity", "status"):
                if key in expected: item[key] = expected[key]
            obs.append(item)
        conflicts.append({"conflict_id": f"conflict-{n:04d}", "record_id": req["record_id"], "field": req["field"], "observations": obs, "selected": req["selected"], "reason": "Applied the request's stable identity, timestamp, validity, source-priority, or sequence rule.", "action_id": event["action_id"]})

    exclusions = []
    for token in truth.get("exclusions", []):
        event = next(x for (action, target), x in event_for.items() if action == "exclude" and token in target)
        exclusions.append({"identifier": token, "reason": "Excluded by the request's tombstone or unresolved-identity rule.", "action_id": event["action_id"]})

    evidence = {"sources": sources, "records": [{"record_id": r[spec["identity_field"]]} for r in spec["records"]], "field_evidence": field_evidence, "conflicts": conflicts, "uncertainties": []}
    summary = {"case_id": case_id, "workflow_mode": "browser" if spec["workflow"]["browser"] else "local_file", "completed": True, "completed_states": truth["required_states"], "reached_record_ids": [r[spec["identity_field"]] for r in spec["records"]], "excluded_observations": exclusions, "limitations": []}
    final = events[-1]["counters"] if events else {k: 0 for k in COUNTERS}
    report = {"status": "ok", "artifact_paths": ["records.json", "evidence.json", "interaction_trace.json", "session_summary.json", "run_report.json"], "errors": [], "usage": {k: final[k] for k in ("gateway", "gateway_image", "serper", "web_retrieval")}, "retries": final["retries"], "elapsed_seconds": 1.0, "local_reads": final["local_reads"], "browser_navigations": final["browser_navigations"]}
    write_json(output / "records.json", spec["records"]); write_json(output / "evidence.json", evidence); write_json(output / "interaction_trace.json", events); write_json(output / "session_summary.json", summary); write_json(output / "run_report.json", report)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--case-dir", type=Path, required=True); ap.add_argument("--output-dir", type=Path, required=True); args = ap.parse_args(); build(args.case_dir.resolve(), args.output_dir.resolve())


if __name__ == "__main__": main()
