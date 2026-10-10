#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

EVALUATOR = Path(__file__).resolve().parents[1]
ROOT = EVALUATOR.parent
sys.path.insert(0, str(EVALUATOR))

from source_native import (  # noqa: E402
    SourceInspector,
    csv_cells,
    docx_content,
    element_bbox,
    pdf_runs,
    svg_viewbox,
    xlsx_content,
)


class SourceNativeTests(unittest.TestCase):
    def test_plain_text_byte_ranges_use_exact_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            assets = case / "assets"
            assets.mkdir(parents=True)
            raw = "alpha\nβeta evidence\nomega\n".encode("utf-8")
            (assets / "notes.txt").write_bytes(raw)
            start = raw.index("βeta evidence".encode("utf-8"))
            end = start + len("βeta evidence".encode("utf-8"))
            inspector = SourceInspector(case)
            result = inspector.validate(
                "notes.txt",
                {"kind": "byte_range", "byte_start": start, "byte_end": end},
                "βeta evidence",
            )
            self.assertEqual(result.kind, "byte_range")
            with self.assertRaisesRegex(ValueError, "not contained"):
                inspector.validate(
                    "notes.txt",
                    {"kind": "byte_range", "byte_start": 0, "byte_end": 5},
                    "βeta evidence",
                )

    def test_corrupt_png_crc_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp) / "case"
            assets = case / "assets"
            assets.mkdir(parents=True)
            raw = bytearray((ROOT / "test_cases" / "test_001" / "assets" / "north_pier.png").read_bytes())
            raw[16] ^= 1
            (assets / "corrupt.png").write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "invalid CRC"):
                SourceInspector(case).validate(
                    "corrupt.png",
                    {"kind": "image_rect", "x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6},
                    "visual region",
                )

    def test_every_runtime_asset_parses_and_has_a_valid_native_locator(self) -> None:
        seen: set[str] = set()
        cases = sorted((ROOT / "dev_cases").iterdir()) + sorted((ROOT / "test_cases").iterdir())
        for case in cases:
            inspector = SourceInspector(case)
            for source_id, path in inspector.assets.items():
                suffix = path.suffix.lower()
                if suffix == ".html":
                    raw = path.read_bytes()
                    text = raw.decode("utf-8")
                    match = re.search(r"<([a-z0-9]+)\s+id=\"([^\"]+)\"[^>]*>.*?</\1>", text, re.I | re.S)
                    self.assertIsNotNone(match, path)
                    start, end = match.span()
                    observation = re.sub(r"<[^>]+>", " ", match.group(0))
                    locator = {"kind": "html_element", "element_id": match.group(2), "byte_start": start, "byte_end": end}
                elif suffix == ".csv":
                    raw = path.read_bytes()
                    rows = csv_cells(raw)
                    self.assertGreaterEqual(len(rows), 2)
                    cell = rows[1][0]
                    locator = {
                        "kind": "csv_cell",
                        "row": 2,
                        "column": 1,
                        "header": rows[0][0]["value"],
                        "byte_start": cell["byte_start"],
                        "byte_end": cell["byte_end"],
                    }
                    observation = cell["value"]
                elif suffix == ".xlsx":
                    sheets = xlsx_content(path)
                    sheet = next(iter(sheets))
                    cell = next(iter(sheets[sheet]))
                    locator = {"kind": "xlsx_cell", "sheet": sheet, "cell": cell}
                    observation = sheets[sheet][cell]
                elif suffix == ".pdf":
                    run = pdf_runs(path)[0][0]
                    x, y, width, height = run["rect"]
                    locator = {"kind": "pdf_rect", "page": 1, "x": x, "y": y, "width": min(width, 1 - x), "height": height}
                    observation = run["text"]
                elif suffix == ".docx":
                    paragraphs, tables = docx_content(path)
                    if tables and tables[0] and tables[0][0]:
                        locator = {"kind": "docx_cell", "table": 0, "row": 0, "column": 0}
                        observation = tables[0][0][0]
                    else:
                        locator = {"kind": "docx_paragraph", "paragraph": 0}
                        observation = paragraphs[0]
                elif suffix == ".svg":
                    root = ET.fromstring(path.read_bytes())
                    element = next(item for item in root.iter() if item.attrib.get("id") and item.tag.rsplit("}", 1)[-1] in {"rect", "circle", "line", "text", "polyline", "path"})
                    vx, vy, vw, vh = svg_viewbox(root)
                    x, y, width, height = element_bbox(element)
                    locator = {
                        "kind": "svg_element",
                        "element_id": element.attrib["id"],
                        "x": max(0.0, (x - vx) / vw),
                        "y": max(0.0, (y - vy) / vh),
                        "width": min(width / vw, 1.0),
                        "height": min(height / vh, 1.0),
                    }
                    observation = "".join(element.itertext()) or "visual SVG element"
                elif suffix == ".png":
                    locator = {"kind": "image_rect", "x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6}
                    observation = "non-uniform visual region"
                else:
                    self.fail(f"unhandled runtime asset {path}")
                result = inspector.validate(source_id, locator, observation)
                self.assertEqual(result.kind, locator["kind"])
                seen.add(suffix)
        self.assertEqual(seen, {".csv", ".docx", ".html", ".pdf", ".png", ".svg", ".xlsx"})

    def test_rectangular_xlsx_range_is_validated_cell_by_cell(self) -> None:
        inspector = SourceInspector(ROOT / "test_cases" / "test_005")
        result = inspector.validate(
            "vendor_offer.xlsx",
            {"kind": "xlsx_cell", "sheet": "Phase II R4", "range": "B2:D2"},
            "turbine nameplate rating 4.2 MW per turbine",
        )
        self.assertEqual(result.kind, "xlsx_cell")

    def test_invalid_native_targets_are_rejected(self) -> None:
        pdf_case = ROOT / "dev_cases" / "dev_002"
        pdf_inspector = SourceInspector(pdf_case)
        with self.assertRaisesRegex(ValueError, "does not overlap"):
            pdf_inspector.validate(
                "release_protocol.pdf",
                {"kind": "pdf_rect", "page": 1, "x": 0.90, "y": 0.90, "width": 0.05, "height": 0.05},
                "Larkspur Cold-Chain Release Protocol v3.1",
            )
        with self.assertRaisesRegex(ValueError, "outside parsed table"):
            pdf_inspector.validate(
                "release_log.docx",
                {"kind": "docx_cell", "table": 0, "row": 999, "column": 0},
                "sample_utc",
            )
        svg_case = ROOT / "dev_cases" / "dev_001"
        svg_inspector = SourceInspector(svg_case)
        with self.assertRaisesRegex(ValueError, "absent or not unique"):
            svg_inspector.validate(
                "inverter_map.svg",
                {"kind": "svg_element", "element_id": "not-present", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1},
                "visual element",
            )
        root = ET.fromstring((svg_case / "assets" / "inverter_map.svg").read_bytes())
        point = next(element for element in root.iter() if element.attrib.get("id") == "point-42c")
        vx, vy, vw, vh = svg_viewbox(root)
        x, y, _, _ = element_bbox(point)
        with self.assertRaisesRegex(ValueError, "only grazes"):
            svg_inspector.validate(
                "inverter_map.svg",
                {
                    "kind": "svg_element",
                    "element_id": "point-42c",
                    "x": (x - vx) / vw,
                    "y": (y - vy) / vh,
                    "width": 0.00001,
                    "height": 0.00001,
                },
                "field point",
            )
        raw = (svg_case / "assets" / "commissioning_report.html").read_bytes()
        with self.assertRaisesRegex(ValueError, "does not contain"):
            svg_inspector.validate(
                "commissioning_report.html",
                {"kind": "html_element", "element_id": "scope", "byte_start": 0, "byte_end": min(40, len(raw))},
                "This report covers Trial B",
            )
        with self.assertRaisesRegex(ValueError, "not source-native"):
            svg_inspector.validate(
                "commissioning_report.html",
                {"kind": "byte_range", "byte_start": 0, "byte_end": len(raw)},
                "This report covers Trial B",
            )
        first_run = pdf_runs(pdf_case / "assets" / "release_protocol.pdf")[0][0]
        with self.assertRaisesRegex(ValueError, "materially broader"):
            pdf_inspector.validate(
                "release_protocol.pdf",
                {"kind": "pdf_rect", "page": 1, "x": 0, "y": 0, "width": 1, "height": 1},
                first_run["text"],
            )
        with self.assertRaisesRegex(ValueError, "only grazes"):
            pdf_inspector.validate(
                "release_protocol.pdf",
                {
                    "kind": "pdf_rect",
                    "page": 1,
                    "x": first_run["rect"][0],
                    "y": first_run["rect"][1],
                    "width": 0.00001,
                    "height": 0.00001,
                },
                first_run["text"],
            )
        with self.assertRaisesRegex(ValueError, "materially broader"):
            SourceInspector(ROOT / "test_cases" / "test_001").validate(
                "north_pier.png",
                {"kind": "image_rect", "x": 0.04, "y": 0.04, "width": 0.92, "height": 0.92},
                "critical markers",
            )
        csv_case = ROOT / "test_cases" / "test_002"
        csv_inspector = SourceInspector(csv_case)
        csv_rows = csv_cells((csv_case / "assets" / "tap_events.csv").read_bytes())
        csv_cell = csv_rows[1][0]
        with self.assertRaisesRegex(ValueError, "does not equal the parsed field span"):
            csv_inspector.validate(
                "tap_events.csv",
                {
                    "kind": "csv_cell",
                    "row": 2,
                    "column": 1,
                    "header": "journey",
                    "byte_start": csv_cell["byte_start"],
                    "byte_end": csv_cell["byte_end"] + 1,
                },
                csv_cell["value"],
            )
        xlsx_inspector = SourceInspector(ROOT / "test_cases" / "test_005")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            xlsx_inspector.validate(
                "vendor_offer.xlsx",
                {"kind": "xlsx_cell", "sheet": "Phase II R4", "cell": "Z999"},
                "missing value",
            )


if __name__ == "__main__":
    unittest.main()
