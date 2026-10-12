#!/usr/bin/env python3
"""Deterministic OOXML, manifest, notes, and render-evidence validator."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def slide_number(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parent = PurePosixPath(source_part).parent
    components: list[str] = []
    for part in (parent / target).parts:
        if part == "..":
            if components:
                components.pop()
        elif part not in {".", ""}:
            components.append(part)
    return "/".join(components)


def xml_text(root: ET.Element) -> str:
    return " ".join(node.text or "" for node in root.findall(".//a:t", NS))


def workbook_cells(data):
    """Expose stored workbook values/formulas for comparison with chart caches."""
    result = {}
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        names = set(book.namelist())
        strings = []
        if 'xl/sharedStrings.xml' in names:
            strings = [''.join(t.text or '' for t in item.findall('.//s:t', ns))
                       for item in ET.fromstring(book.read('xl/sharedStrings.xml')).findall('s:si', ns)]
        for name in sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)):
            cells = []
            for cell in ET.fromstring(book.read(name)).findall('.//s:c', ns):
                value = cell.findtext('s:v', namespaces=ns)
                if cell.get('t') == 's' and value is not None:
                    value = strings[int(value)]
                if cell.get('t') == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//s:t', ns))
                cells.append({'cell': cell.get('r'), 'value': value, 'formula': cell.findtext('s:f', namespaces=ns)})
            result[name] = cells
    return result


def parse_json(path: Path, errors: list[str], label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}: top level is not an object")
        return {}
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expectations", type=Path, default=Path(__file__).with_name("case_expectations.json"))
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    expectations = json.loads(args.expectations.read_text(encoding="utf-8"))
    expected = expectations[args.case_id]
    output = args.output_dir.resolve()
    deck = output / "deck.pptx"
    manifest_path = output / "source_manifest.json"
    run_report_path = output / "run_report.json"
    errors: list[str] = []
    fatal: list[str] = []
    warnings: list[str] = []
    evidence: dict[str, object] = {}
    infrastructure_error = None

    if not deck.is_file():
        fatal.append("missing deck.pptx")
    else:
        try:
            with zipfile.ZipFile(deck) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    fatal.append(f"corrupt ZIP member: {bad_member}")
                names = set(archive.namelist())
                presentation = ET.fromstring(archive.read('ppt/presentation.xml'))
                relationships = ET.fromstring(archive.read('ppt/_rels/presentation.xml.rels'))
                slide_map = {r.get('Id'): resolve_target('ppt/presentation.xml', r.get('Target', ''))
                             for r in relationships.findall('pr:Relationship', NS)}
                slide_names = [slide_map[node.get('{' + NS['r'] + '}id')]
                               for node in presentation.findall('p:sldIdLst/p:sldId', NS)]
                if not slide_names:
                    fatal.append('deck has no slides in presentation order')
                slide_details = []
                slide_texts: dict[int, str] = {}
                native_charts = 0
                native_tables = 0
                alt_descriptions = 0
                unresolved_relationships: list[str] = []
                external_relationships: list[str] = []
                notes_slides: list[int] = []
                for number, slide_name in enumerate(slide_names, 1):
                    root = ET.fromstring(archive.read(slide_name))
                    slide_texts[number] = xml_text(root)
                    detail = {'slide': number, 'part': slide_name, 'text': slide_texts[number],
                              'slide_xml': ET.tostring(root, encoding='unicode'),
                              'tables': [[[xml_text(cell) for cell in row.findall('a:tc', NS)]
                                          for row in table.findall('a:tr', NS)] for table in root.findall('.//a:tbl', NS)],
                              'charts': [], 'notes': [], 'text_runs': []}
                    for run in root.findall('.//a:r', NS):
                        properties = run.find('a:rPr', NS)
                        detail['text_runs'].append({'text': run.findtext('a:t', default='', namespaces=NS),
                            'properties': ET.tostring(properties, encoding='unicode') if properties is not None else None})
                    native_tables += len(root.findall(".//a:tbl", NS))
                    alt_descriptions += sum(
                        1 for node in root.findall(".//p:cNvPr", NS)
                        if (node.get("descr") or "").strip()
                    )
                    rel_name = str(PurePosixPath(slide_name).parent / "_rels" / (PurePosixPath(slide_name).name + ".rels"))
                    if rel_name in names:
                        rel_root = ET.fromstring(archive.read(rel_name))
                        for relation in rel_root.findall("pr:Relationship", NS):
                            target = relation.get("Target", "")
                            mode = relation.get("TargetMode")
                            rel_type = relation.get("Type", "")
                            if mode == "External":
                                external_relationships.append(target)
                                continue
                            resolved = resolve_target(slide_name, target)
                            if resolved not in names:
                                unresolved_relationships.append(f"{slide_name} -> {resolved}")
                            if rel_type.endswith("/chart"):
                                native_charts += 1
                                chart = {'part': resolved}
                                if resolved in names:
                                    chart_root = ET.fromstring(archive.read(resolved))
                                    chart['cache_and_formula_xml'] = ET.tostring(chart_root, encoding='unicode')
                                    chart_rels = str(PurePosixPath(resolved).parent / '_rels' / (PurePosixPath(resolved).name + '.rels'))
                                    chart['workbooks'] = []
                                    if chart_rels in names:
                                        for rel in ET.fromstring(archive.read(chart_rels)).findall('pr:Relationship', NS):
                                            embedded = resolve_target(resolved, rel.get('Target', ''))
                                            if embedded.endswith('.xlsx') and embedded in names:
                                                data = archive.read(embedded)
                                                try:
                                                    cells = workbook_cells(data)
                                                except Exception as exc:
                                                    cells = {'parse_error': str(exc)}
                                                    errors.append('embedded chart workbook cannot be inspected: ' + embedded)
                                                chart['workbooks'].append({'part': embedded, 'sha256': hashlib.sha256(data).hexdigest(), 'cells': cells})
                                detail['charts'].append(chart)
                            if rel_type.endswith("/notesSlide"):
                                notes_slides.append(number)
                                if resolved in names:
                                    detail['notes'].append(xml_text(ET.fromstring(archive.read(resolved))))
                    slide_details.append(detail)
                all_text = "\n".join(slide_texts.values())
                for token in expected["required_tokens"]:
                    if token.casefold() not in all_text.casefold():
                        warnings.append(f"required token not found: {token}")
                if len(slide_names) != expected["slide_count"]:
                    errors.append(f"slide count {len(slide_names)} != {expected['slide_count']}")
                missing_notes = sorted(set(expected["notes_slides"]) - set(notes_slides))
                if missing_notes:
                    warnings.append(f"missing notes relationships on slides {missing_notes}")
                if native_charts < expected["min_native_charts"]:
                    warnings.append(f"native charts {native_charts} < {expected['min_native_charts']}")
                if native_tables < expected["min_native_tables"]:
                    warnings.append(f"native tables {native_tables} < {expected['min_native_tables']}")
                if unresolved_relationships:
                    errors.extend(f"unresolved relationship: {item}" for item in unresolved_relationships)
                evidence["ooxml"] = {
                    "canvas": dict(presentation.find('p:sldSz', NS).attrib) if presentation.find('p:sldSz', NS) is not None else None,
                    "slide_count": len(slide_names),
                    "slides": slide_details,
                    "slide_text_characters": {str(k): len(v) for k, v in slide_texts.items()},
                    "native_charts": native_charts,
                    "native_tables": native_tables,
                    "alt_descriptions": alt_descriptions,
                    "notes_slides": sorted(set(notes_slides)),
                    "external_relationships": external_relationships,
                    "unresolved_relationships": unresolved_relationships,
                }
        except (OSError, KeyError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            fatal.append(f"deck parse: {exc}")

    manifest = parse_json(manifest_path, errors, "source_manifest.json")
    run_report = parse_json(run_report_path, errors, "run_report.json")
    sources = manifest.get("sources", []) if isinstance(manifest, dict) else []
    claims = manifest.get("claims", []) if isinstance(manifest, dict) else []
    if not isinstance(sources, list) or not sources:
        warnings.append("manifest has no nonempty sources array")
    if not isinstance(claims, list) or not claims:
        warnings.append("manifest has no nonempty claims array")
    if expected["closed_corpus"] and manifest.get("research_scope") != "closed_corpus":
        warnings.append("closed-corpus case has wrong research_scope")
    usage = run_report.get("usage", {}) if isinstance(run_report, dict) else {}
    if not isinstance(usage, dict):
        errors.append('run_report usage is not an object')
        usage = {}
    if expected["closed_corpus"]:
        for key in ("serper", "web_retrieval"):
            count = usage.get(key, 0)
            if type(count) is int and count > 0:
                fatal.append(f"closed-corpus nonzero {key}: {count}")
            elif type(count) is not int or count < 0:
                errors.append(f'invalid reporting count for {key}')
    evidence["manifest"] = {
        "source_count": len(sources) if isinstance(sources, list) else None,
        "claim_count": len(claims) if isinstance(claims, list) else None,
        "research_scope": manifest.get("research_scope") if isinstance(manifest, dict) else None,
    }
    evidence["run_report"] = {"status": run_report.get("status"), "usage": usage}

    if args.render_dir:
        render_dir = args.render_dir.resolve()
        report_path = render_dir / 'render_report.json'
        try:
            rendered = json.loads(report_path.read_text())
            evidence['render'] = rendered
            if rendered.get('evaluation_state') == 'fatal_zero':
                fatal.extend(rendered.get('errors', []))
            elif rendered.get('evaluation_state') == 'infrastructure_error':
                infrastructure_error = rendered.get('infrastructure_error') or 'trusted renderer infrastructure failed'
            elif rendered.get('evaluation_state') != 'scoreable' or rendered.get('valid') is not True:
                infrastructure_error = 'invalid trusted renderer report contract'
        except (OSError, ValueError, AttributeError) as exc:
            infrastructure_error = 'trusted renderer report unavailable: ' + str(exc)

    result = {
        "schema_version": "1.0",
        "case_id": args.case_id,
        "valid": not infrastructure_error and not fatal and not errors,
        "validity_gate": not infrastructure_error and not fatal,
        "evaluation_state": 'infrastructure_error' if infrastructure_error else 'fatal_zero' if fatal else 'scoreable',
        "infrastructure_error": infrastructure_error,
        "fatal_errors": fatal,
        "quality_errors": errors,
        "errors": fatal,
        "warnings": warnings,
        "evidence": evidence,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 70 if infrastructure_error else 0 if not fatal else 1


if __name__ == "__main__":
    raise SystemExit(main())
