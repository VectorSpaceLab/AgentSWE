#!/usr/bin/env python3
"""Oracle-free structural and reproducibility validator for v4 final artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import sys
import time
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any


ASSET_LINK_RE = re.compile(r"\[[^\]]+\]\((assets/[^)]+)\)")
SQLITE_LINK_RE = re.compile(r"\[[^\]]+\]\((assets/[^)]+\.sqlite)\)", re.IGNORECASE)
CONTROL_ID_RE = re.compile(r"id=[\"']([A-Za-z][A-Za-z0-9_-]*)[\"']")
QUERY_ID_RE = re.compile(r"q[1-9][0-9]*")
FORBIDDEN_SQL_RE = re.compile(
    r"\b(insert|update|delete|replace|create|drop|alter|pragma|attach|detach|"
    r"vacuum|reindex|analyze|load_extension|begin|commit|rollback|savepoint|release)\b",
    re.IGNORECASE,
)
REMOTE_URL_RE = re.compile(r"(?i)(?:https?:)?//[a-z0-9]")
NETWORK_API_RE = re.compile(
    r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|importScripts)\s*\(|"
    r"\bnavigator\s*\.\s*sendBeacon\s*\(",
    re.IGNORECASE,
)
CSS_EXTERNAL_RE = re.compile(r"(?is)@import\b|url\(\s*(['\"]?)(?!data:|#)[^)]+\1\s*\)")

REQUIRED = [
    "answer.json",
    "queries.json",
    "result.csv",
    "chart.json",
    "dashboard.html",
    "decision.json",
    "lineage.json",
    "run_report.json",
]
ALLOWED_CHARTS = {"bar", "line", "stacked_bar", "waterfall", "scatter", "table"}


# --- AgentSWE release compat: legacy gateway count keys (see provider_counts_compat.py) ---
_AGENTSWE_LEGACY_GATEWAY = "s" "u8"
_AGENTSWE_LEGACY_KEYS = {_AGENTSWE_LEGACY_GATEWAY + s: "gateway" + s for s in ("", "_text", "_image", "_image_requests")}


def _agentswe_neutral_provider_keys(value):
    if isinstance(value, dict):
        legacy = [k for k in value if k in _AGENTSWE_LEGACY_KEYS]
        if legacy and not any(_AGENTSWE_LEGACY_KEYS[k] in value for k in legacy):
            value = {_AGENTSWE_LEGACY_KEYS.get(k, k): v for k, v in value.items()}
        return {k: _agentswe_neutral_provider_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_agentswe_neutral_provider_keys(v) for v in value]
    return value
# --- end AgentSWE release compat ---


class OfflineDashboardChecker(HTMLParser):
    RESOURCE_ATTRIBUTES = {
        "audio": {"src"}, "embed": {"src"}, "frame": {"src"}, "iframe": {"src"},
        "img": {"src", "srcset"}, "input": {"src"}, "link": {"href"},
        "object": {"data"}, "script": {"src"}, "source": {"src", "srcset"},
        "track": {"src"}, "video": {"poster", "src"},
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.dependencies: list[str] = []
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self.inline_handlers: list[str] = []
        self.ids: set[str] = set()
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        for name in self.RESOURCE_ATTRIBUTES.get(tag.lower(), set()):
            value = attributes.get(name, "").strip()
            if value and not value.lower().startswith("data:") and not value.startswith("#"):
                self.dependencies.append(f"<{tag}> {name}={value!r}")
        if tag.lower() == "use":
            value = (attributes.get("href") or attributes.get("xlink:href") or "").strip()
            if value and not value.startswith("#"):
                self.dependencies.append(f"<use> href={value!r}")
        style = attributes.get("style", "")
        if style:
            self.inline_styles.append(style)
        self.inline_handlers.extend(value for name, value in attributes.items() if name.startswith("on") and value)
        if tag.lower() in {"script", "style"}:
            self._capture = tag.lower()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == self._capture:
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture == "script":
            self.inline_scripts.append(data)
        elif self._capture == "style":
            self.inline_styles.append(data)


def add(errors: list[str], message: str) -> None:
    errors.append(message)


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard numeric constant {value}")
            ),
        )
    except Exception as exc:
        add(errors, f"{path.name}: JSON parse failed: {exc}")
        return None


def csv_rows(path: Path, errors: list[str]) -> tuple[list[str], list[list[str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except Exception as exc:
        add(errors, f"{path}: CSV parse failed: {exc}")
        return [], []
    if not rows:
        add(errors, f"{path}: CSV has no header")
        return [], []
    header = rows[0]
    if not header or any(not value for value in header):
        add(errors, f"{path}: CSV header contains an empty column")
    if len(header) != len(set(header)):
        add(errors, f"{path}: duplicate CSV column names")
    for number, row in enumerate(rows[1:], 2):
        if len(row) != len(header):
            add(errors, f"{path}: row {number} width differs from header")
    return header, rows[1:]


def sql_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return format(value, ".15g")
    return str(value)


def scalars_equivalent(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    try:
        a = float(actual)
        b = float(expected)
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def compare_rows(actual: list[list[str]], expected: list[list[str]]) -> bool:
    return len(actual) == len(expected) and all(
        len(a) == len(b) and all(scalars_equivalent(x, y) for x, y in zip(a, b))
        for a, b in zip(actual, expected)
    )


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def json_scalar(text: str) -> Any:
    if text == "":
        return None
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", text):
        return int(text)
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+)?", text) or re.fullmatch(
        r"-?(?:0|[1-9][0-9]*)[eE][+-]?[0-9]+", text
    ):
        value = float(text)
        if math.isfinite(value):
            return value
    if text == "true":
        return True
    if text == "false":
        return False
    return text


def output_relative_file(output: Path, value: Any, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value:
        add(errors, "run_report.json artifacts entries must be nonempty strings")
        return None
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or normalized != pure.as_posix():
        add(errors, f"artifact path must be normalized and output-relative: {value!r}")
        return None
    resolved = (output / normalized).resolve()
    root = output.resolve()
    if resolved != root and root not in resolved.parents:
        add(errors, f"artifact path escapes output: {value!r}")
        return None
    if not resolved.is_file() or resolved.is_symlink():
        add(errors, f"declared artifact is missing or unsafe: {value}")
    return normalized


def encoding_fields(value: Any) -> list[str]:
    fields: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("field"), str):
            fields.append(value["field"])
        if isinstance(value.get("fields"), list):
            fields.extend(item for item in value["fields"] if isinstance(item, str))
        for child in value.values():
            if isinstance(child, (dict, list)):
                fields.extend(encoding_fields(child))
    elif isinstance(value, list):
        for child in value:
            fields.extend(encoding_fields(child))
    return fields


def validate_dashboard(path: Path, required_ids: set[str], result_header: list[str], errors: list[str]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        add(errors, f"dashboard.html: UTF-8 read failed: {exc}")
        return {"ids": [], "bytes": 0}
    if not text.strip():
        add(errors, "dashboard.html must not be empty")
        return {"ids": [], "bytes": 0}
    checker = OfflineDashboardChecker()
    try:
        checker.feed(text)
        checker.close()
    except Exception as exc:
        add(errors, f"dashboard.html: HTML parse failed: {exc}")
    if checker.dependencies:
        add(errors, f"dashboard.html is not self-contained: {checker.dependencies[0]}")
    script = "\n".join(checker.inline_scripts + checker.inline_handlers)
    style = "\n".join(checker.inline_styles)
    if REMOTE_URL_RE.search(script):
        add(errors, "dashboard.html inline code contains a remote or protocol-relative URL")
    if NETWORK_API_RE.search(script):
        add(errors, "dashboard.html contains a browser network API")
    if CSS_EXTERNAL_RE.search(style):
        add(errors, "dashboard.html contains a CSS external URL/import")
    missing_ids = sorted(required_ids - checker.ids)
    if missing_ids:
        add(errors, "dashboard.html omits case-required control ids: " + ", ".join(missing_ids))
    for column in result_header:
        if column not in text:
            add(errors, f"dashboard.html does not expose result column label {column!r}")
    if "dashboard_replay" in text:
        add(errors, "dashboard.html refers to removed dashboard_replay contract")
    return {"ids": sorted(checker.ids), "bytes": len(text.encode("utf-8"))}


def validate_details(case_input: Path, output: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    for name in REQUIRED:
        path = output / name
        if not path.is_file() or path.is_symlink():
            add(errors, f"missing or unsafe required artifact: {name}")
    if (output / "dashboard_replay.json").exists():
        add(errors, "removed artifact dashboard_replay.json must not be produced")
    if any(not (output / name).is_file() for name in ('answer.json', 'queries.json', 'result.csv')):
        return {"valid": False, "errors": errors, "warnings": warnings, "checks": checks}

    markdown = case_input.read_text(encoding="utf-8")
    linked_assets = set(ASSET_LINK_RE.findall(markdown))
    sqlite_links = SQLITE_LINK_RE.findall(markdown)
    required_controls = set(CONTROL_ID_RE.findall(markdown))
    if len(sqlite_links) != 1:
        add(errors, f"case input must link exactly one SQLite database, found {len(sqlite_links)}")
        database_path = None
    else:
        database_path = (case_input.parent / sqlite_links[0]).resolve()
        if not database_path.is_file():
            add(errors, f"linked SQLite database does not exist: {sqlite_links[0]}")

    answer = load_json(output / "answer.json", errors)
    manifest = load_json(output / "queries.json", errors)
    chart = load_json(output / "chart.json", errors)
    decision = load_json(output / "decision.json", errors)
    lineage = load_json(output / "lineage.json", errors)
    report = _agentswe_neutral_provider_keys(load_json(output / "run_report.json", errors))
    result_header, result_data = csv_rows(output / "result.csv", errors)
    objects = [answer, manifest]
    if not all(isinstance(item, dict) for item in objects):
        add(errors, "all required JSON artifacts must contain a top-level object")
        return {"valid": False, "errors": errors, "warnings": warnings, "checks": checks}
    chart = chart if isinstance(chart, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    lineage = lineage if isinstance(lineage, dict) else {}
    report = report if isinstance(report, dict) else {}

    # answer.json
    answer_required = {
        "schema_version", "status", "request_summary", "definitions", "assumptions",
        "answer", "query_steps", "quality_checks", "privacy", "limitations",
    }
    answer_missing = sorted(answer_required - set(answer))
    if answer_missing:
        add(errors, "answer.json omits fields: " + ", ".join(answer_missing))
    if answer.get("schema_version") != "1.0" or answer.get("status") not in {"answered", "insufficient_information"}:
        add(errors, "answer.json has invalid schema_version or status")
    if not isinstance(answer.get("request_summary"), str) or not answer["request_summary"].strip():
        add(errors, "answer.json request_summary must be a nonempty string")
    for key in ("definitions", "assumptions", "query_steps", "quality_checks", "limitations"):
        if not isinstance(answer.get(key), list):
            add(errors, f"answer.json {key} must be an array")
    if not isinstance(answer.get("answer"), dict) or not isinstance(answer.get("privacy"), dict):
        add(errors, "answer.json answer and privacy must be objects")
    else:
        answer_body = answer["answer"]
        if not isinstance(answer_body.get("summary"), str) or not isinstance(answer_body.get("metrics"), list) or not isinstance(answer_body.get("findings"), list):
            add(errors, "answer.json answer requires summary string plus metrics/findings arrays")
        privacy = answer["privacy"]
        if not isinstance(privacy.get("role"), str):
            add(errors, "answer.json privacy.role must be a string")
        for key in ("applied_rules", "primary_suppressions", "complementary_suppressions"):
            if not isinstance(privacy.get(key), list):
                add(errors, f"answer.json privacy.{key} must be an array")

    # queries and exact CSV replay
    if manifest.get("schema_version") != "1.0" or manifest.get("dialect") != "sqlite":
        add(errors, "queries.json must declare schema_version 1.0 and sqlite dialect")
    if sqlite_links and manifest.get("database") != sqlite_links[0]:
        add(errors, f"queries.json database must equal {sqlite_links[0]!r}")
    queries = manifest.get("queries")
    if not isinstance(queries, list):
        add(errors, "queries.json queries must be an array")
        queries = []
    ids: set[str] = set()
    query_result_paths: set[str] = set()
    executed = 0
    conn = None
    if database_path and database_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{database_path}?mode=ro&immutable=1", uri=True)
            conn.execute("PRAGMA query_only = ON")
            deadline = time.monotonic() + 15
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            denied_tables = {'account_contacts', 'worker_identity', 'compensation', 'driver_accounts', 'recipient_directory'}
            def authorize(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_READ and arg1 in denied_tables:
                    checks.setdefault('forbidden_query_references', []).append(arg1)
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {'load_extension', 'readfile', 'writefile'}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
        except Exception as exc:
            add(errors, f"cannot open active database read-only: {exc}")
    try:
        for index, query in enumerate(queries):
            label = f"queries[{index}]"
            if not isinstance(query, dict):
                add(errors, f"{label} must be an object")
                continue
            qid = query.get("id")
            if not isinstance(qid, str) or not QUERY_ID_RE.fullmatch(qid) or qid in ids:
                add(errors, f"{label} has invalid or duplicate id")
                continue
            ids.add(qid)
            sql = query.get("sql")
            if not isinstance(sql, str) or not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
                add(errors, f"{qid}: SQL must begin with SELECT or WITH")
                continue
            if FORBIDDEN_SQL_RE.search(sql):
                add(errors, f"{qid}: SQL contains a prohibited keyword")
                continue
            if not isinstance(query.get("purpose"), str) or not query["purpose"].strip():
                add(errors, f"{qid}: purpose must be nonempty")
            columns = query.get("columns")
            if not isinstance(columns, list) or not columns or any(not isinstance(column, str) or not column for column in columns) or len(columns) != len(set(columns)):
                add(errors, f"{qid}: columns must be a nonempty unique string array")
            if not is_nonnegative_int(query.get("row_count")):
                add(errors, f"{qid}: row_count must be a nonnegative integer")
            result_rel = query.get("result_file")
            expected_rel = f"results/{qid}.csv"
            if result_rel != expected_rel:
                add(errors, f"{qid}: result_file must be {expected_rel}")
                continue
            query_result_paths.add(expected_rel)
            result_path = (output / expected_rel).resolve()
            if output.resolve() not in result_path.parents or not result_path.is_file() or result_path.is_symlink():
                add(errors, f"{qid}: result file is missing or unsafe")
                continue
            header, rows = csv_rows(result_path, errors)
            if query.get("columns") != header or query.get("row_count") != len(rows):
                add(errors, f"{qid}: declared columns/row_count do not match CSV")
            if conn is not None:
                try:
                    cursor = conn.execute(sql)
                    sql_header = [column[0] for column in cursor.description or []]
                    sql_rows = [[sql_scalar(value) for value in row] for row in cursor.fetchmany(100001)]
                    if len(sql_rows) > 100000:
                        raise ValueError('query evidence exceeds bounded inspection size')
                    executed += 1
                    if sql_header != header:
                        add(errors, f"{qid}: re-executed columns do not match CSV")
                    if not compare_rows(rows, sql_rows):
                        add(errors, f"{qid}: re-executed rows/order do not match CSV")
                except Exception as exc:
                    add(errors, f"{qid}: SQL re-execution failed: {exc}")
    finally:
        if conn is not None:
            conn.close()
    if answer.get("status") == "answered" and not queries:
        add(errors, "answered run must declare at least one reproducible query")
    if isinstance(answer.get("query_steps"), list) and answer["query_steps"] != [q.get("id") for q in queries if isinstance(q, dict)]:
        add(errors, "answer.json query_steps must list manifest query ids in order")
    checks["queries_reexecuted"] = executed

    # chart/result equality
    if chart.get("schema_version") != "1.0" or chart.get("status") not in {"renderable", "not_applicable"}:
        add(errors, "chart.json has invalid schema_version or status")
    if answer.get("status") == "insufficient_information":
        if result_data:
            add(errors, "insufficient_information result.csv must have zero data rows")
        if chart.get("status") != "not_applicable":
            add(errors, "insufficient_information chart must be not_applicable")
    if chart.get("status") == "renderable":
        if chart.get("type") not in ALLOWED_CHARTS:
            add(errors, "chart.json type is not allowed")
        if not isinstance(chart.get("title"), str) or not chart["title"].strip():
            add(errors, "renderable chart requires a nonempty title")
        if chart.get("data_source") != "result.csv" or not isinstance(chart.get("encoding"), dict) or not chart["encoding"]:
            add(errors, "renderable chart must use result.csv and an encoding object")
        if not isinstance(chart.get("data"), list) or not isinstance(chart.get("notes"), list):
            add(errors, "renderable chart requires data and notes arrays")
        expected_records = [
            {column: json_scalar(value) for column, value in zip(result_header, row)}
            for row in result_data
        ]
        if chart.get("data") != expected_records:
            add(errors, "chart data does not exactly match ordered result.csv records")
        for field in encoding_fields(chart.get("encoding")):
            if field not in result_header:
                add(errors, f"chart encoding references missing result column {field!r}")
    elif chart.get("status") == "not_applicable":
        if chart.get("type") is not None or chart.get("title") is not None or chart.get("data_source") is not None:
            add(errors, "not_applicable chart must use null type/title/data_source")
        if chart.get("encoding") != {} or chart.get("series") is not None or chart.get("data") != []:
            add(errors, "not_applicable chart must have empty encoding/data and null series")
        if not isinstance(chart.get("notes"), list) or not chart["notes"]:
            add(errors, "not_applicable chart requires a nonempty notes array")

    # public decision schema
    decision_required = {
        "schema_version", "status", "decision", "selected_scope", "selected_definition",
        "confidence", "metric_names", "blockers", "limitations",
    }
    missing = sorted(decision_required - set(decision))
    if missing:
        add(errors, "decision.json omits fields: " + ", ".join(missing))
    if decision.get("schema_version") != "1.0" or decision.get("status") != answer.get("status"):
        add(errors, "decision.json schema/status must match answer.json")
    if not isinstance(decision.get("decision"), str) or not isinstance(decision.get("selected_scope"), dict) or not isinstance(decision.get("selected_definition"), dict):
        add(errors, "decision.json decision/scope/definition types are invalid")
    if decision.get("confidence") not in {"high", "medium", "low", None}:
        add(errors, "decision.json confidence is invalid")
    for key in ("metric_names", "blockers", "limitations"):
        if not isinstance(decision.get(key), list):
            add(errors, f"decision.json {key} must be an array")
    if answer.get("status") == "insufficient_information" and decision.get("metric_names"):
        add(errors, "insufficient decision must not publish requested metric names")

    # public lineage schema
    if lineage.get("schema_version") != "1.0" or not isinstance(lineage.get("mappings"), list):
        add(errors, "lineage.json requires schema_version 1.0 and mappings array")
        mappings = []
    else:
        mappings = lineage["mappings"]
    mapped_fields: set[str] = set()
    for index, mapping in enumerate(mappings):
        label = f"lineage.mappings[{index}]"
        if not isinstance(mapping, dict):
            add(errors, f"{label} must be an object")
            continue
        required = {"field", "query_ids", "source_assets", "transformation"}
        if not required <= set(mapping):
            add(errors, f"{label} omits public mapping fields")
            continue
        field = mapping.get("field")
        if not isinstance(field, str) or not field:
            add(errors, f"{label}.field must be nonempty")
        else:
            mapped_fields.add(field)
        query_ids = mapping.get("query_ids")
        if not isinstance(query_ids, list) or not query_ids or not all(isinstance(x, str) for x in query_ids) or not set(query_ids) <= ids:
            add(errors, f"{label}.query_ids must reference declared queries")
        source_assets = mapping.get("source_assets")
        if not isinstance(source_assets, list) or not source_assets or not all(isinstance(x, str) for x in source_assets) or not set(source_assets) <= linked_assets:
            add(errors, f"{label}.source_assets must reference linked active-case assets")
        if not isinstance(mapping.get("transformation"), str) or not mapping["transformation"].strip():
            add(errors, f"{label}.transformation must be nonempty")
    missing_fields = sorted(set(result_header) - mapped_fields)
    if missing_fields:
        add(errors, "lineage.json omits result columns: " + ", ".join(missing_fields))

    checks["dashboard"] = validate_dashboard(output / "dashboard.html", required_controls, result_header, errors)

    # run report
    if report.get("status") != "success" or not isinstance(report.get("artifacts"), list):
        add(errors, "run_report.json must declare success and artifacts array")
        declared: list[str] = []
    else:
        declared = []
        for value in report["artifacts"]:
            normalized = output_relative_file(output, value, errors)
            if normalized is not None:
                declared.append(normalized)
        if len(declared) != len(set(declared)):
            add(errors, "run_report.json artifacts contains duplicates")
        expected = set(REQUIRED) | query_result_paths
        omitted = sorted(expected - set(declared))
        if omitted:
            add(errors, "run_report.json omits required/generated artifacts: " + ", ".join(omitted))
        if "dashboard_replay.json" in declared:
            add(errors, "run_report.json declares removed dashboard_replay.json")
    if not isinstance(report.get("errors"), list):
        add(errors, "run_report.json errors must be an array")
    elif report.get("status") == "success" and report["errors"]:
        add(errors, "successful run_report.json must have an empty errors array")
    usage = report.get("usage")
    if not isinstance(usage, dict):
        add(errors, "run_report.json usage must be an object")
        usage = {}
    for key in ("elapsed_seconds", "peak_memory_mib"):
        if not is_number(usage.get(key)) or usage[key] < 0:
            add(errors, f"run_report.json usage.{key} must be finite nonnegative numeric")
    if not is_nonnegative_int(usage.get("sqlite_queries")):
        add(errors, "run_report.json usage.sqlite_queries must be a nonnegative integer")
    calls = usage.get("external_api_calls")
    if not isinstance(calls, dict):
        add(errors, "run_report.json external_api_calls must be an object")
        calls = {}
    for name in ("deepseek", "gateway", "serper", "web_retrieval"):
        if not is_nonnegative_int(calls.get(name)):
            add(errors, f"run_report external_api_calls.{name} must be a nonnegative integer")
    if isinstance(calls.get("deepseek"), int) and isinstance(calls.get("gateway"), int) and calls["deepseek"] + calls["gateway"] > 300:
        add(errors, "combined DeepSeek/GATEWAY calls exceed 300")
    if calls.get("serper") != 0 or calls.get("web_retrieval") != 0:
        add(errors, "closed-local case requires zero serper and web_retrieval calls")

    checks.update({
        "result_rows": len(result_data),
        "chart_rows": len(chart.get("data", [])) if isinstance(chart.get("data"), list) else None,
        "linked_assets": sorted(linked_assets),
        "required_control_ids": sorted(required_controls),
        "declared_artifacts": len(declared),
    })
    return {"valid": not errors, "errors": errors, "warnings": warnings, "checks": checks}


def validate(case_input: Path, output: Path) -> dict[str, Any]:
    from semantic_checks import calculate, compare, privacy
    infrastructure = []; fatal = []
    try:
        independent = calculate(case_input)
    except Exception as exc:
        independent = {}
        infrastructure.append('active asset semantic computation failed: ' + type(exc).__name__ + ': ' + str(exc))
    try:
        result = validate_details(case_input, output)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        result = {'valid': False, 'errors': ['malformed auxiliary artifact: ' + type(exc).__name__], 'checks': {}}
    for name in ('answer.json', 'queries.json'):
        try:
            value = json.loads((output/name).read_text())
            if not isinstance(value, dict): fatal.append(name + ' core must be an object')
        except Exception:
            fatal.append(name + ' core missing or unparseable')
    header, data = csv_rows(output/'result.csv', [])
    if not header: fatal.append('result.csv has no parseable header')
    try:
        answer = json.loads((output/'answer.json').read_text())
        if isinstance(answer, dict) and answer.get('status') == 'answered' and not result.get('checks', {}).get('queries_reexecuted'):
            fatal.append('answered bundle has no re-executable declared query')
    except Exception: pass
    if independent and header:
        result['independent_semantics'] = independent
        result['semantic_comparison'] = compare(output/'result.csv', independent)
        result['privacy_validation'] = privacy(output, independent)
        if result.get('checks',{}).get('forbidden_query_references'):
            result['privacy_validation']['privacy_dimension_zero_required']=True
            result['privacy_validation']['errors'].append('declared query references forbidden table; evaluator blocked execution')
        if not result['semantic_comparison']['matches_independent_result']:
            result.setdefault('errors', []).append('result differs from independent business calculation')
    state = 'infrastructure_error' if infrastructure else 'fatal_zero' if fatal else 'scoreable'
    result.update(evaluation_state=state, validity_gate=state=='scoreable', fatal_gate=bool(fatal), fatal_errors=fatal,
                  infrastructure_errors=infrastructure, quality_errors=result.get('errors', []))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.case_input.resolve(), args.output.resolve())
    json.dump(report, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
