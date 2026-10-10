#!/usr/bin/env python3
"""Positive, compatibility, negative, and mutation tests for strict validation."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKE = ROOT / "evaluator/make_reference_bundle.py"
VALIDATE = ROOT / "evaluator/validate_case.py"


def run(case: Path, out: Path, result: Path) -> dict:
    proc = subprocess.run(["python3", str(VALIDATE), "--case-dir", str(case), "--output-dir", str(out), "--result", str(result)], capture_output=True, text=True)
    data = json.loads(result.read_text()); assert (proc.returncode == 0) == data["valid"], (proc.returncode, data, proc.stderr); return data


def mutate(path: Path, fn) -> None:
    data = json.loads(path.read_text()); fn(data); path.write_text(json.dumps(data, indent=2) + "\n")


def expect_reject(observed: dict, name: str, case: Path, out: Path, result: Path, prefixes: tuple[str, ...]) -> None:
    data = run(case, out, result); assert not data["valid"], (name, data)
    assert any(any(error.startswith(prefix) for prefix in prefixes) for error in data["errors"]), (name, data["errors"]); observed[name] = "rejected"


def main() -> int:
    observed = {}
    with tempfile.TemporaryDirectory(prefix="schema-v4-validator-") as td:
        tmp = Path(td); bases = {}
        for case in sorted(list((ROOT / "dev_cases").glob("dev_*")) + list((ROOT / "test_cases").glob("test_*"))):
            out = tmp / case.name; subprocess.run(["python3", str(MAKE), "--case-dir", str(case), "--output-dir", str(out)], check=True)
            data = run(case, out, tmp / f"{case.name}.json"); assert data["valid"], data; observed[f"reference_{case.name}"] = "pass"; bases[case.name] = out

        dev1 = ROOT / "dev_cases/dev_001"
        alt_ids = tmp / "alt-conflict-ids"; shutil.copytree(bases["dev_001"], alt_ids)
        mutate(alt_ids / "evidence.json", lambda ev: [x.update({"conflict_id": f"candidate-{i:03d}"}) for i, x in enumerate(ev["conflicts"], 1)])
        data = run(dev1, alt_ids, tmp / "alt-conflict-ids.json"); assert data["valid"], data; observed["candidate_chosen_conflict_ids"] = "pass"

        loopback = tmp / "loopback-source"; shutil.copytree(bases["dev_001"], loopback)
        def loopback_source(ev):
            source = next(x for x in ev["sources"] if x["path"] == "assets/site/details/d-01.html"); source["path"] = "http://localhost/details/d-01.html"
        mutate(loopback / "evidence.json", loopback_source)
        data = run(dev1, loopback, tmp / "loopback-source.json"); assert data["valid"], data; observed["documented_loopback_source"] = "pass"

        wrong_id = tmp / "wrong-id"; shutil.copytree(bases["dev_001"], wrong_id)
        mutate(wrong_id / "records.json", lambda rows: rows[0].__setitem__("asset_id", "AU-9999"))
        expect_reject(observed, "wrong_identity", dev1, wrong_id, tmp / "wrong-id.json", ("missing_record", "extra_record", "field_evidence"))

        wrong_schema = tmp / "wrong-schema"; shutil.copytree(bases["dev_001"], wrong_schema)
        mutate(wrong_schema / "records.json", lambda rows: rows[0].__setitem__("stock", "twelve"))
        expect_reject(observed, "wrong_schema", dev1, wrong_schema, tmp / "wrong-schema.json", ("schema_type",))

        wrong_source = tmp / "wrong-source"; shutil.copytree(bases["dev_001"], wrong_source)
        def swap_source(ev):
            item = next(x for x in ev["field_evidence"] if x["field"] == "/material"); item["source_id"] = next(x["source_id"] for x in ev["sources"] if x["path"].endswith("official-prices.json")); item["access_depth"] = "local_file"; item["method"] = "json"
        mutate(wrong_source / "evidence.json", swap_source)
        expect_reject(observed, "unrelated_real_source", dev1, wrong_source, tmp / "wrong-source.json", ("field_evidence_unsupported",))

        placeholder_action = tmp / "placeholder-action"; shutil.copytree(bases["dev_001"], placeholder_action)
        mutate(placeholder_action / "interaction_trace.json", lambda events: [x.update({"locator": "record"}) for x in events if x["action"] == "visit_detail"])
        expect_reject(observed, "placeholder_action_locator", dev1, placeholder_action, tmp / "placeholder-action.json", ("trace_placeholder_locator", "required_action_target_missing"))

        wrong_link = tmp / "wrong-link"; shutil.copytree(bases["dev_001"], wrong_link)
        def relink(ev):
            item = next(x for x in ev["field_evidence"] if x["field"] == "/material"); item["action_id"] = next(x["action_id"] for x in json.loads((wrong_link / "interaction_trace.json").read_text()) if x["action"] == "read_local")
        mutate(wrong_link / "evidence.json", relink)
        expect_reject(observed, "wrong_action_evidence", dev1, wrong_link, tmp / "wrong-link.json", ("field_evidence_unsupported",))

        fake_conflict = tmp / "fake-conflict"; shutil.copytree(bases["dev_001"], fake_conflict)
        def flatten_conflict(ev):
            c = ev["conflicts"][0]; c["field"] = "/decision"; c["observations"] = [{"source_id": c["observations"][0]["source_id"], "value": "losing"}, {"source_id": c["observations"][1]["source_id"], "value": "selected"}]; c["selected"] = "selected"
        mutate(fake_conflict / "evidence.json", flatten_conflict)
        expect_reject(observed, "fabricated_conflict_placeholder", dev1, fake_conflict, tmp / "fake-conflict.json", ("required_conflict_semantics",))

        missing_record_edges = tmp / "missing-record-edges"; shutil.copytree(bases["dev_001"], missing_record_edges)
        mutate(missing_record_edges / "evidence.json", lambda ev: ev["records"].pop())
        expect_reject(observed, "missing_evidence_record", dev1, missing_record_edges, tmp / "missing-record-edges.json", ("evidence_records_identity_set",))

        wrong_state = tmp / "wrong-state"; shutil.copytree(bases["dev_001"], wrong_state)
        mutate(wrong_state / "interaction_trace.json", lambda events: events[1].update({"state": "catalog:4"}))
        expect_reject(observed, "wrong_state_transition", dev1, wrong_state, tmp / "wrong-state.json", ("required_state_mismatch", "required_state_missing_or_out_of_order"))

        duplicate_shot = tmp / "duplicate-shot"; shutil.copytree(bases["dev_001"], duplicate_shot)
        shots = sorted((duplicate_shot / "screenshots").glob("*.png")); shutil.copyfile(shots[0], shots[1])
        expect_reject(observed, "duplicate_screenshot_pixels", dev1, duplicate_shot, tmp / "duplicate-shot.json", ("screenshot_duplicate_pixels",))

        ocr_case = ROOT / "test_cases/test_001"
        bad_ocr_method = tmp / "bad-ocr-method"; shutil.copytree(bases["test_001"], bad_ocr_method)
        mutate(bad_ocr_method / "evidence.json", lambda ev: next(x for x in ev["field_evidence"] if x["method"] == "ocr").update({"method": "dom"}))
        expect_reject(observed, "wrong_ocr_method", ocr_case, bad_ocr_method, tmp / "bad-ocr-method.json", ("field_evidence_unsupported",))

        bad_ocr_region = tmp / "bad-ocr-region"; shutil.copytree(bases["test_001"], bad_ocr_region)
        mutate(bad_ocr_region / "evidence.json", lambda ev: next(x for x in ev["field_evidence"] if x["method"] == "ocr").update({"locator": "image", "confidence": 0.9}))
        expect_reject(observed, "missing_ocr_region", ocr_case, bad_ocr_region, tmp / "bad-ocr-region.json", ("ocr_evidence_region_confidence",))

        copied_source = tmp / "copied-source-shot"; shutil.copytree(bases["test_001"], copied_source)
        source_png = next((ROOT / "test_cases/test_001/assets/site/labels").glob("*.png")); first_shot = next((copied_source / "screenshots").glob("*.png")); shutil.copyfile(source_png, first_shot)
        expect_reject(observed, "copied_source_as_screenshot", ocr_case, copied_source, tmp / "copied-source-shot.json", ("screenshot_copied_source_image",))

        sensor_case = ROOT / "test_cases/test_004"
        missing_exclusion = tmp / "missing-exclusion"; shutil.copytree(bases["test_004"], missing_exclusion)
        mutate(missing_exclusion / "session_summary.json", lambda x: x["excluded_observations"].pop())
        expect_reject(observed, "missing_required_exclusion", sensor_case, missing_exclusion, tmp / "missing-exclusion.json", ("summary_exclusion_missing",))

        recovery_case = ROOT / "test_cases/test_005"
        patch_order = tmp / "patch-order"; shutil.copytree(bases["test_005"], patch_order)
        def reverse_patch_events(events):
            positions = [i for i, x in enumerate(events) if x["action"] == "apply_patch"]; a, b = positions[0], positions[-1]; events[a]["locator"], events[b]["locator"] = events[b]["locator"], events[a]["locator"]
        mutate(patch_order / "interaction_trace.json", reverse_patch_events)
        expect_reject(observed, "wrong_patch_sequence", recovery_case, patch_order, tmp / "patch-order.json", ("required_action_order",))

        static_shot = tmp / "static-shot"; shutil.copytree(bases["test_005"], static_shot); (static_shot / "screenshots").mkdir(); shutil.copyfile(next((bases["dev_001"] / "screenshots").glob("*.png")), static_shot / "screenshots/fake.png")
        mutate(static_shot / "interaction_trace.json", lambda events: events[0].update({"screenshot": "screenshots/fake.png"}))
        expect_reject(observed, "static_fabricated_screenshot", recovery_case, static_shot, tmp / "static-shot.json", ("static_case_fabricated_screenshots",))

        over_budget = tmp / "over-budget"; shutil.copytree(bases["test_005"], over_budget)
        mutate(over_budget / "run_report.json", lambda report: report.update({"local_reads": 9, "browser_navigations": 1}))
        mutate(over_budget / "interaction_trace.json", lambda events: events[-1]["counters"].update({"local_reads": 9, "browser_navigations": 1}))
        expect_reject(observed, "combined_local_work_budget", recovery_case, over_budget, tmp / "over-budget.json", ("case_local_work_budget", "static_browser_navigation"))

    print(json.dumps(observed, indent=2, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
