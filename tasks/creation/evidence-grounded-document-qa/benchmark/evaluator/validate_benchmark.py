#!/usr/bin/env python3
"""Deterministic construction, leakage, structure, provenance, and parser checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from source_native import csv_cells, docx_content, normalize_text, pdf_runs, png_rgb, xlsx_content


EXPECTED_INPUTS = {
    "01_task_goal.md",
    "02_interface_and_delivery.md",
    "03_requirements_and_constraints.md",
    "04_resources.md",
}
ORACLE_TOKENS = ("oracle", "answer_key", "expected_answer", "reference_output", "judge_context", "case_rubric")
CODE_DIMENSIONS = {
    "interface_lifecycle": 15,
    "requirement_mechanism_coverage": 20,
    "analysis_evidence_integrity": 15,
    "safety_privacy_side_effects": 15,
    "recovery_honest_failure": 10,
    "testability_observability": 10,
    "maintainability_generalization": 10,
    "resource_discipline": 5,
}
CODE_TOP_LEVEL = {
    "code_state",
    "code_dimensions",
    "code_raw_score",
    "code_applied_caps",
    "code_score",
    "code_major_errors",
    "code_assessment",
}


def modality(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def numeric_fragments(path: Path) -> set[str]:
    suffix = path.suffix.lower()
    values: list[str] = []
    if suffix == ".pdf":
        values = [run["text"] for page in pdf_runs(path) for run in page]
    elif suffix == ".docx":
        paragraphs, tables = docx_content(path)
        values = paragraphs + [cell for table in tables for row in table for cell in row]
    elif suffix == ".xlsx":
        values = [value for cells in xlsx_content(path).values() for value in cells.values()]
    elif suffix == ".csv":
        values = [cell["value"] for row in csv_cells(path.read_bytes()) for cell in row]
    elif suffix == ".svg":
        values = ["".join(element.itertext()) for element in ET.fromstring(path.read_bytes()).iter()]
    elif suffix in {".html", ".md", ".txt"}:
        values = re.split(r"[\n\r]+", path.read_text(encoding="utf-8"))
    fragments = set()
    for value in values:
        normalized = normalize_text(value).lower()
        if re.search(r"\d", normalized) and len(normalized.split()) >= 5:
            fragments.add(normalized)
    return fragments


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    errors: list[str] = []
    warnings: list[str] = []

    input_files = {path.name for path in (root / "input").iterdir() if path.is_file()}
    if input_files != EXPECTED_INPUTS:
        errors.append(f"input/ must contain exactly four builder docs: {sorted(input_files)}")
    dev_cases = sorted(path for path in (root / "dev_cases").iterdir() if path.is_dir())
    test_cases = sorted(path for path in (root / "test_cases").iterdir() if path.is_dir())
    if len(dev_cases) != 2:
        errors.append(f"expected 2 development cases, found {len(dev_cases)}")
    if len(test_cases) != 6:
        errors.append(f"expected 6 hidden test cases, found {len(test_cases)}")

    all_hashes: dict[str, list[str]] = {}
    public_hashes: set[str] = set()
    hidden_hashes: set[str] = set()
    hidden_signatures: dict[tuple[str, ...], str] = {}
    public_numeric: set[str] = set()
    hidden_numeric: set[str] = set()
    cross_source_repetitions: list[str] = []
    total_asset_bytes = 0
    asset_records: dict[tuple[str, str], dict] = {}
    provenance_path = root / "meta" / "asset_provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        for record in provenance.get("assets", []):
            asset_records[(record.get("case"), record.get("asset"))] = record
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid asset_provenance.json: {exc}")

    for group_name, cases in (("dev_cases", dev_cases), ("test_cases", test_cases)):
        for case in cases:
            case_key = f"{group_name}/{case.name}"
            entries = {path.name for path in case.iterdir()}
            if entries != {"input.md", "assets"}:
                errors.append(f"{case_key} must contain only input.md and assets/: {sorted(entries)}")
            input_path = case / "input.md"
            if not input_path.is_file():
                errors.append(f"{case_key} lacks input.md")
                continue
            request = input_path.read_text(encoding="utf-8")
            if len(request.split()) < 120:
                errors.append(f"{case_key}/input.md is too small to be a complete hard-case request")
            request_fragments = numeric_fragments(input_path)
            (public_numeric if group_name == "dev_cases" else hidden_numeric).update(request_fragments)
            assets_dir = case / "assets"
            assets = sorted(path for path in assets_dir.iterdir() if path.is_file()) if assets_dir.is_dir() else []
            case_fragment_sources: dict[str, str] = {}
            if len(assets) < 3:
                errors.append(f"{case_key} has fewer than three runtime assets")
            signature = tuple(sorted(modality(path) for path in assets))
            if group_name == "test_cases":
                if signature in hidden_signatures:
                    errors.append(f"hidden modality signature duplicates {hidden_signatures[signature]}: {signature}")
                hidden_signatures[signature] = case_key
            if "md" in signature:
                errors.append(f"{case_key} contains a Markdown runtime asset; v4 forbids shortcut Markdown source replicas")
            if len(set(signature)) < 3:
                errors.append(f"{case_key} lacks meaningful modality diversity: {signature}")
            for path in assets:
                if path.name not in request:
                    errors.append(f"{case_key}/input.md does not explicitly reference runtime asset {path.name}")
                lowered = path.name.lower()
                if any(token in lowered for token in ORACLE_TOKENS):
                    errors.append(f"forbidden oracle-like runtime filename {case_key}/assets/{path.name}")
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                total_asset_bytes += len(data)
                all_hashes.setdefault(digest, []).append(f"{case_key}/assets/{path.name}")
                (public_hashes if group_name == "dev_cases" else hidden_hashes).add(digest)
                fragments = numeric_fragments(path)
                (public_numeric if group_name == "dev_cases" else hidden_numeric).update(fragments)
                for fragment in fragments:
                    prior = case_fragment_sources.get(fragment)
                    if prior and prior != path.name:
                        cross_source_repetitions.append(f"{case_key}: {prior} and {path.name}: {fragment}")
                    case_fragment_sources.setdefault(fragment, path.name)
                record = asset_records.get((case_key, path.name))
                if not record:
                    errors.append(f"missing provenance record for {case_key}/assets/{path.name}")
                else:
                    if record.get("sha256") != digest or record.get("bytes") != len(data):
                        errors.append(f"stale provenance hash/size for {case_key}/assets/{path.name}")
                    if record.get("provenance") != "synthetic; generated locally by meta/generate_assets.py":
                        errors.append(f"unexpected provenance for {case_key}/assets/{path.name}")
                try:
                    suffix = path.suffix.lower()
                    if suffix == ".pdf":
                        pages = pdf_runs(path)
                        if len(pages) < 2 or any(not page for page in pages):
                            errors.append(f"PDF is not substantive/multipage: {case_key}/assets/{path.name}")
                    elif suffix == ".docx":
                        paragraphs, tables = docx_content(path)
                        if not paragraphs or not tables:
                            errors.append(f"DOCX lacks substantive paragraphs/tables: {case_key}/assets/{path.name}")
                    elif suffix == ".xlsx":
                        sheets = xlsx_content(path)
                        if len(sheets) < 2 or any(len(cells) < 4 for cells in sheets.values()):
                            errors.append(f"XLSX lacks substantive multi-sheet content: {case_key}/assets/{path.name}")
                    elif suffix == ".svg":
                        svg = ET.fromstring(data)
                        ids = [element.attrib["id"] for element in svg.iter() if "id" in element.attrib]
                        if len(ids) < 6 or len(ids) != len(set(ids)):
                            errors.append(f"SVG identifiers are sparse/duplicated: {case_key}/assets/{path.name}")
                    elif suffix == ".png":
                        width, height, pixels = png_rgb(path)
                        if width < 400 or height < 250 or len(set(pixels[index:index+3] for index in range(0, len(pixels), max(3, len(pixels)//3000 // 3 * 3)))) < 3:
                            errors.append(f"PNG is too small or visually trivial: {case_key}/assets/{path.name}")
                    elif suffix == ".html":
                        text = data.decode("utf-8")
                        ids = re.findall(r'\bid=["\']([^"\']+)["\']', text)
                        if len(ids) < 3 or len(ids) != len(set(ids)):
                            errors.append(f"HTML source lacks unique source-native IDs: {case_key}/assets/{path.name}")
                    elif suffix == ".csv":
                        lines = data.decode("utf-8").splitlines()
                        if len(lines) < 5:
                            errors.append(f"CSV is too small: {case_key}/assets/{path.name}")
                except Exception as exc:
                    errors.append(f"asset parser failure {case_key}/assets/{path.name}: {exc}")

    duplicates = {digest: paths for digest, paths in all_hashes.items() if len(paths) > 1}
    if duplicates:
        errors.append(f"duplicate runtime asset bytes detected: {duplicates}")
    overlap_hashes = public_hashes & hidden_hashes
    if overlap_hashes:
        errors.append(f"public-hidden asset hash leakage detected for {len(overlap_hashes)} hash(es)")
    if cross_source_repetitions:
        errors.append("exact numeric fact fragments repeat across runtime sources: " + " | ".join(cross_source_repetitions[:10]))
    text_overlap = public_numeric & hidden_numeric
    if text_overlap:
        errors.append("public-hidden exact numeric text overlap detected: " + " | ".join(sorted(text_overlap)[:10]))
    if len(asset_records) != sum(len(paths) for paths in all_hashes.values()):
        errors.append("provenance record count differs from runtime asset count")
    if total_asset_bytes > 12 * 1024 * 1024:
        errors.append(f"runtime assets exceed 12 MiB feasibility budget: {total_asset_bytes}")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in {"asset_provenance.json"}:
            continue
        if any(part in {"__pycache__", ".pytest_cache", "node_modules", ".cache"} for part in path.parts):
            errors.append(f"construction cache present: {path.relative_to(root)}")
        if path.name != "validate_benchmark.py" and path.suffix.lower() in {".md", ".py", ".mjs", ".html", ".json"}:
            try:
                text = path.read_text(encoding="utf-8").lower()
            except UnicodeError:
                continue
            if "todo" in text or "placeholder" in text:
                errors.append(f"TODO/placeholder text remains in {path.relative_to(root)}")

    rubric = (root / "evaluator" / "rubric.md").read_text(encoding="utf-8")
    rubric_points = [int(value) for value in re.findall(r"^## \d+\..*?— (\d+) points$", rubric, re.M)]
    if sum(rubric_points) != 100 or len(rubric_points) != 6:
        errors.append(f"final rubric must have six dimensions totaling 100; found {rubric_points}")
    code_rubric = (root / "evaluator" / "code_rubric.md").read_text(encoding="utf-8")
    code_points = [int(value) for value in re.findall(r"^## \d+\..*?— (\d+) points$", code_rubric, re.M)]
    if sum(code_points) != 100 or len(code_points) != 8:
        errors.append(f"code rubric must have eight dimensions totaling 100; found {code_points}")
    json_blocks = re.findall(r"```json\s*(\{.*?\})\s*```", code_rubric, re.S)
    if len(json_blocks) != 1:
        errors.append(f"code rubric must contain exactly one parseable JSON example object; found {len(json_blocks)}")
    else:
        try:
            example = json.loads(json_blocks[0])
        except json.JSONDecodeError as exc:
            errors.append(f"code rubric example JSON is invalid: {exc}")
        else:
            if set(example) != CODE_TOP_LEVEL:
                errors.append(f"code rubric example top-level keys differ from shared contract: {sorted(example)}")
            dimensions = example.get("code_dimensions")
            if not isinstance(dimensions, dict) or set(dimensions) != set(CODE_DIMENSIONS):
                errors.append("code rubric example does not use the exact shared eight code dimension IDs")
            else:
                for dimension, maximum in CODE_DIMENSIONS.items():
                    record = dimensions[dimension]
                    if not isinstance(record, dict) or set(record) != {"score", "max", "evidence"}:
                        errors.append(f"code rubric example dimension {dimension!r} has the wrong object shape")
                        continue
                    if record.get("max") != maximum:
                        errors.append(f"code rubric example dimension {dimension!r} must have max {maximum}")
            if not all(key in example for key in ("code_raw_score", "code_applied_caps", "code_score")):
                errors.append("code rubric example lacks required score/cap fields")

    result = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "builder_input_count": len(input_files),
        "development_case_count": len(dev_cases),
        "hidden_case_count": len(test_cases),
        "runtime_asset_count": sum(len(paths) for paths in all_hashes.values()),
        "runtime_asset_bytes": total_asset_bytes,
        "hidden_modality_signatures": {case: list(signature) for signature, case in hidden_signatures.items()},
        "public_hidden_hash_overlap": len(overlap_hashes),
        "duplicate_asset_hashes": len(duplicates),
        "cross_source_numeric_repetitions": len(cross_source_repetitions),
        "public_hidden_numeric_text_overlap": len(text_overlap),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
