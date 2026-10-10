#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASKS = (
    "claude", "aider", "openhands", "openclaw", "codex",
    "ai-scientist", "deepcode", "deeptutor", "dyad", "openwiki",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


CASE_IDS = tuple(f"test_{index:03d}" for index in range(1, 7))
CODE_AXIS_RETIREMENT_ID = "edit-code-axis-retired-2026-09-19"
TERMINAL_STATUSES = ("completed", "lifecycle_complete", "formal_evidence_complete")
PROFILE_SEGMENT = "codex_xhigh"


def read_json_or_none(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def resolve_under(run_dir: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else run_dir / candidate


def resolve_lifecycle(run_dir: Path, summary: dict[str, Any]) -> tuple[Any, str]:
    """Return (lifecycle document, how it was found)."""
    raw = summary.get("dev_lifecycle")
    if isinstance(raw, list):
        return raw, "summary.dev_lifecycle[list]"
    if isinstance(raw, str) and raw:
        path = resolve_under(run_dir, raw)
        if path.is_file():
            return read_json_or_none(path), f"summary.dev_lifecycle->{path.name}"
        return None, f"summary.dev_lifecycle->{path} (missing)"
    return None, "summary.dev_lifecycle absent/null"


def accepted_dev_rounds(run_dir: Path, summary: dict[str, Any]) -> tuple[int | None, str]:
    document, origin = resolve_lifecycle(run_dir, summary)
    if isinstance(document, list):
        return len(document), origin
    if isinstance(document, dict):
        for key in ("records", "accepted_submissions", "candidate_submissions"):
            if isinstance(document.get(key), list):
                return len(document[key]), f"{origin}.{key}"
    freeze = as_dict(summary.get("freeze"))
    source_submission = freeze.get("source_submission")
    if isinstance(source_submission, int) and not isinstance(source_submission, bool):
        return source_submission, "summary.freeze.source_submission"
    dev_gate = as_dict(freeze.get("dev_gate"))
    accepted = dev_gate.get("accepted_rounds")
    if isinstance(accepted, int) and not isinstance(accepted, bool):
        return accepted, "summary.freeze.dev_gate.accepted_rounds"
    return None, origin


def dev_passed_is_automatic_freeze(run_dir: Path, summary: dict[str, Any]) -> tuple[Any, str]:
    if "dev_passed_is_automatic_freeze" in summary:
        return summary["dev_passed_is_automatic_freeze"], "summary"
    document, origin = resolve_lifecycle(run_dir, summary)
    if isinstance(document, dict) and "dev_passed_is_automatic_freeze" in document:
        return document["dev_passed_is_automatic_freeze"], origin
    return None, "no producer"


def freeze_reason(summary: dict[str, Any]) -> Any:
    freeze = as_dict(summary.get("freeze"))
    for key in ("reason", "freeze_reason"):
        if freeze.get(key) is not None:
            return freeze[key]
    return None


def hidden_case_ids(summary: dict[str, Any], aggregation: dict[str, Any]) -> tuple[list[str] | None, str]:
    hidden = summary.get("hidden")
    if isinstance(hidden, dict):
        inventory = hidden.get("inventory")
        if isinstance(inventory, list):
            return [str(item) for item in inventory], "summary.hidden.inventory"
        if inventory is None and hidden and all(str(key).startswith("test_") for key in hidden):
            return sorted(str(key) for key in hidden), "summary.hidden[case-keyed]"
        case_ids = hidden.get("case_ids")
        if isinstance(case_ids, list):
            return [str(item) for item in case_ids], "summary.hidden.case_ids"
    if isinstance(summary.get("hidden_inventory"), list):
        return [str(item) for item in summary["hidden_inventory"]], "summary.hidden_inventory"
    hidden_summary = summary.get("hidden_summary")
    if isinstance(hidden_summary, dict) and isinstance(hidden_summary.get("case_ids"), list):
        return [str(item) for item in hidden_summary["case_ids"]], "summary.hidden_summary.case_ids"
    if isinstance(aggregation.get("selected_cases"), list):
        return [str(item) for item in aggregation["selected_cases"]], "aggregation.selected_cases"
    return None, "no producer"


def code_axis_receipt(code: dict[str, Any]) -> tuple[bool, str]:
    """The Code axis is retired: verify the skip receipt instead of a score."""
    if not code:
        return False, "code_axis receipt absent"
    state = code.get("evaluation_state")
    if state != "skipped_by_policy":
        return False, f"code_axis evaluation_state={state!r} (expected 'skipped_by_policy')"
    identifier = code.get("id")
    if identifier is None:
        identifier = as_dict(code.get("policy")).get("id")
    if identifier != CODE_AXIS_RETIREMENT_ID:
        return False, f"code_axis retirement id={identifier!r} (expected {CODE_AXIS_RETIREMENT_ID!r})"
    if code.get("code_score") is not None or code.get("score") is not None:
        return False, "code_axis carries a score although the axis is retired"
    return True, f"skipped_by_policy/{CODE_AXIS_RETIREMENT_ID}"


def cleanup_attestation_path(run_dir: Path, summary: dict[str, Any]) -> Path:
    pointer = summary.get("cleanup_attestation")
    if isinstance(pointer, str) and pointer:
        return resolve_under(run_dir, pointer)
    if isinstance(pointer, dict) and isinstance(pointer.get("path"), str):
        return resolve_under(run_dir, pointer["path"])
    return run_dir / "cleanup_attestation.json"


def cleanup_complete(record: dict[str, Any]) -> tuple[bool, str]:
    for key in ("completed", "cleanup_complete"):
        if key in record:
            return record[key] is True, f"{key}={record.get(key)!r}"
    if "all_started_containers_absent" in record:
        ok = (
            record.get("all_started_containers_absent") is True
            and record.get("unrelated_containers_touched") is False
        )
        return ok, (
            "all_started_containers_absent="
            f"{record.get('all_started_containers_absent')!r}/"
            f"unrelated_containers_touched={record.get('unrelated_containers_touched')!r}"
        )
    return False, "no completion key"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("@@AGENTSWE_EDITING_RUNS@@/formal/formal_results_manifest.json"))
    args = parser.parse_args()
    launch = read_json(args.launch_manifest.resolve())
    errors: list[str] = []
    if launch.get("selected_profiles") != ["codex_xhigh"]:
        errors.append("launch profile scope is not codex_xhigh_only")
    branches = launch.get("branches")
    if not isinstance(branches, list) or len(branches) != 10:
        errors.append("launch manifest does not contain exactly ten branches")
        branches = []
    records: list[dict[str, Any]] = []
    for branch in branches:
        if not isinstance(branch, dict):
            errors.append("non-object branch record")
            continue
        task = str(branch.get("task"))
        run_dir = Path(str(branch.get("run_dir", "")))
        summary_path = run_dir / "one_stop_summary.json"
        aggregation_path = run_dir / "formal_aggregation.json"
        if not summary_path.is_file():
            errors.append(f"{task}: missing one_stop_summary.json")
            continue
        if not aggregation_path.is_file():
            errors.append(f"{task}: missing formal_aggregation.json")
            continue
        summary = read_json(summary_path)
        aggregation = read_json(aggregation_path)
        cleanup_path = cleanup_attestation_path(run_dir, summary)
        result = as_dict(aggregation.get("result_axis"))
        summary_result = as_dict(summary.get("result_axis"))
        code = as_dict(aggregation.get("code_axis")) or as_dict(summary.get("code_axis"))
        task_errors: list[str] = []
        notes: list[str] = []

        # -- profile scope: the run directory's own profile segment -------
        if PROFILE_SEGMENT not in run_dir.parts:
            task_errors.append(f"run_dir is not under the {PROFILE_SEGMENT} profile root")

        # -- Result axis: the finalizer's own aggregation is authoritative
        publishable = aggregation.get("formal_result_publishable")
        result_reasons = aggregation.get("result_reasons")
        if not isinstance(result_reasons, list):
            result_reasons = aggregation.get("reasons") if isinstance(aggregation.get("reasons"), list) else []
        if publishable is not True:
            detail = "; ".join(str(item) for item in result_reasons) or "no reason recorded"
            task_errors.append(f"formal_result_publishable={publishable!r} ({detail})")
        case_scores = result.get("case_scores")
        if not isinstance(result.get("score"), (int, float)) or isinstance(result.get("score"), bool):
            task_errors.append(f"Result axis carries no numeric score (result_axis={aggregation.get('result_axis')!r})")
        if result.get("maximum") != 100:
            task_errors.append(f"Result axis maximum={result.get('maximum')!r} (expected 100)")
        if not isinstance(case_scores, dict) or sorted(case_scores) != sorted(CASE_IDS):
            task_errors.append("Result axis case_scores is not exactly the six hidden cases")
        if summary_result and summary_result != result:
            task_errors.append("one_stop_summary.result_axis disagrees with formal_aggregation.result_axis")

        # -- status --------------------------------------------------------
        status = summary.get("status")
        if status not in TERMINAL_STATUSES:
            if publishable is True and isinstance(summary.get("post_run_repair"), dict):
                notes.append(
                    f"status={status!r} predates the post-run Result-judge repair "
                    f"({as_dict(summary['post_run_repair']).get('tool')}); "
                    "formal_aggregation.json is the authority"
                )
            else:
                task_errors.append(f"status={status!r}")

        # -- accepted dev rounds ------------------------------------------
        rounds, rounds_origin = accepted_dev_rounds(run_dir, summary)
        if rounds is None:
            task_errors.append(f"accepted dev rounds unresolvable ({rounds_origin})")
        elif not 1 <= rounds <= 10:
            task_errors.append(f"accepted dev rounds outside 1..10 ({rounds} via {rounds_origin})")

        # -- dev_passed_is_automatic_freeze: checked where it has a producer
        dpaf, dpaf_origin = dev_passed_is_automatic_freeze(run_dir, summary)
        if dpaf_origin == "no producer":
            notes.append("dev_passed_is_automatic_freeze: no producer in this tree")
        elif dpaf is not False:
            task_errors.append(f"dev_passed automatic freeze mismatch ({dpaf!r} via {dpaf_origin})")

        # -- hidden inventory ---------------------------------------------
        case_ids, case_ids_origin = hidden_case_ids(summary, aggregation)
        if case_ids is None or sorted(case_ids) != sorted(CASE_IDS):
            task_errors.append(f"hidden inventory mismatch ({case_ids!r} via {case_ids_origin})")

        # -- Code axis: retired; verify the skip receipt --------------------
        code_ok, code_detail = code_axis_receipt(code)
        if not code_ok:
            task_errors.append(f"Code-axis retirement receipt invalid: {code_detail}")
        if aggregation.get("code_score_publishable") is True:
            task_errors.append("code_score_publishable is True although the Code axis is retired")

        # -- combined score -------------------------------------------------
        if summary.get("combined_score") is not None or aggregation.get("combined_score") is not None:
            task_errors.append("combined_score must be null")

        # -- cleanup attestation -------------------------------------------
        cleanup_sha = None
        if not cleanup_path.is_file():
            task_errors.append(f"cleanup attestation missing ({cleanup_path})")
            cleanup_rule = "file missing"
        else:
            cleanup_sha = sha256(cleanup_path)
            cleanup_ok, cleanup_rule = cleanup_complete(read_json(cleanup_path))
            if not cleanup_ok:
                task_errors.append(f"cleanup attestation incomplete ({cleanup_rule})")

        errors.extend(f"{task}: {message}" for message in task_errors)
        records.append({
            "task": task,
            "run_dir": str(run_dir),
            "summary": str(summary_path),
            "summary_sha256": sha256(summary_path),
            "aggregation": str(aggregation_path),
            "aggregation_sha256": sha256(aggregation_path),
            "cleanup_attestation": str(cleanup_path),
            "cleanup_attestation_sha256": cleanup_sha,
            "cleanup_rule": cleanup_rule,
            "status": status,
            "accepted_dev_rounds": rounds,
            "accepted_dev_rounds_source": rounds_origin,
            "freeze_reason": freeze_reason(summary),
            "hidden_case_ids": case_ids,
            "hidden_case_ids_source": case_ids_origin,
            "result_publishable": publishable is True,
            "result_score": result.get("score"),
            "result_case_scores": case_scores if isinstance(case_scores, dict) else None,
            "code_axis_state": code.get("evaluation_state"),
            "code_axis_receipt": code_detail,
            "code_score": None,
            "combined_score": None,
            "notes": notes,
            "errors": task_errors,
        })
    if sorted(item["task"] for item in records) != sorted(TASKS):
        errors.append("result task inventory mismatch")
    result_scores = [item["result_score"] for item in records if isinstance(item.get("result_score"), (int, float)) and not isinstance(item.get("result_score"), bool)]
    code_scores = [item["code_score"] for item in records if isinstance(item.get("code_score"), (int, float)) and not isinstance(item.get("code_score"), bool)]
    value = {
        "schema_version": "agentswe-0905-edit-formal-results-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile_scope": "codex_xhigh_only",
        "selected_profiles": ["codex_xhigh"],
        "branch_count": len(records),
        "publishable": not errors and len(records) == 10,
        "tasks": records,
        "result_axis_mean_across_tasks": round(sum(result_scores) / len(result_scores), 4) if len(result_scores) == 10 else None,
        "code_axis_mean_across_tasks": round(sum(code_scores) / len(code_scores), 4) if len(code_scores) == 10 else None,
        "combined_score": None,
        "errors": errors,
        "launch_manifest": str(args.launch_manifest.resolve()),
        "launch_manifest_sha256": sha256(args.launch_manifest.resolve()),
        "credential_values_recorded": False,
    }
    write_json(args.output.resolve(), value)
    print(json.dumps({"publishable": value["publishable"], "branch_count": len(records), "errors": errors}, indent=2, ensure_ascii=False))
    return 0 if value["publishable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
