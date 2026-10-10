#!/usr/bin/env python3
"""Generate the deterministic synthetic multimodal corpora for Document QA v4.

The script uses only the Python standard library. It writes exclusively below
this benchmark directory, removes copied v3 runtime assets before generation,
and never creates per-case oracle files.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import struct
import zlib
from html import escape
from io import StringIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]


def asset_dir(case: str) -> Path:
    group, name = case.split("/", 1)
    path = ROOT / group / name / "assets"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def write_text(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8", newline="\n")


def write_csv(path: Path, rows: list[list[object]]) -> None:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def zip_write(archive: ZipFile, name: str, data: str) -> None:
    info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data.encode("utf-8"))


def pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, pages: list[list[tuple]]) -> None:
    """Create a text/vector PDF with stable coordinates and extractable text."""
    objects: list[bytes | None] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    kids: list[int] = []
    for items in pages:
        commands: list[str] = []
        for item in items:
            kind = item[0]
            if kind == "text":
                _, x, y, size, value, bold = item
                font = "F2" if bold else "F1"
                commands.append(
                    f"BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm ({pdf_escape(value)}) Tj ET"
                )
            elif kind == "line":
                _, x1, y1, x2, y2, width = item
                commands.append(f"{width} w {x1} {y1} m {x2} {y2} l S")
            elif kind == "rect":
                _, x, y, width, height, r, g, b = item
                commands.append(f"{r} {g} {b} rg {x} {y} {width} {height} re f 0 0 0 rg")
        stream = "\n".join(commands).encode("latin-1")
        content_obj = len(objects) + 1
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_obj = len(objects) + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                f"/Contents {content_obj} 0 R >>"
            ).encode("ascii")
        )
        kids.append(page_obj)
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in kids)}] "
        f"/Count {len(kids)} >>"
    ).encode("ascii")
    output = b"%PDF-1.4\n%synthetic-v4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        assert obj is not None
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    output += b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    path.write_bytes(output)


def docx_paragraph(text: str, bold: bool = False) -> str:
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def docx_table(rows: list[list[object]]) -> str:
    rendered_rows = []
    for row in rows:
        cells = []
        for value in row:
            cells.append(
                "<w:tc><w:tcPr/><w:p><w:r><w:t xml:space=\"preserve\">"
                + escape(str(value))
                + "</w:t></w:r></w:p></w:tc>"
            )
        rendered_rows.append("<w:tr>" + "".join(cells) + "</w:tr>")
    return "<w:tbl><w:tblPr/><w:tblGrid/>" + "".join(rendered_rows) + "</w:tbl>"


def make_docx(path: Path, blocks: list[tuple[str, object]]) -> None:
    body: list[str] = []
    for kind, value in blocks:
        if kind == "heading":
            body.append(docx_paragraph(str(value), bold=True))
        elif kind == "paragraph":
            body.append(docx_paragraph(str(value)))
        elif kind == "table":
            body.append(docx_table(value))
        else:
            raise ValueError(kind)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body) + "<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        zip_write(archive, "[Content_Types].xml", content_types)
        zip_write(archive, "_rels/.rels", rels)
        zip_write(archive, "word/document.xml", document)


def column_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def make_xlsx(path: Path, sheets: list[tuple[str, list[list[object]]]]) -> None:
    sheet_xml: list[str] = []
    for _, rows in sheets:
        rendered_rows = []
        for row_index, row in enumerate(rows, 1):
            cells = []
            for column_index, value in enumerate(row, 1):
                ref = f"{column_name(column_index)}{row_index}"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                        f"{escape(str(value))}</t></is></c>"
                    )
            rendered_rows.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            "<sheetData>" + "".join(rendered_rows) + "</sheetData></worksheet>"
        )
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(sheets, 1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + overrides
        + "</Types>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        zip_write(archive, "[Content_Types].xml", content_types)
        zip_write(archive, "_rels/.rels", root_rels)
        zip_write(archive, "xl/workbook.xml", workbook)
        zip_write(archive, "xl/_rels/workbook.xml.rels", rels)
        for index, xml in enumerate(sheet_xml, 1):
            zip_write(archive, f"xl/worksheets/sheet{index}.xml", xml)


class Raster:
    def __init__(self, width: int, height: int, color: tuple[int, int, int] = (245, 247, 249)):
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(color)

    def rect(self, x: int, y: int, width: int, height: int, color: tuple[int, int, int]) -> None:
        for yy in range(y, y + height):
            for xx in range(x, x + width):
                self.set(xx, yy, color)

    def circle(self, cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
        radius2 = radius * radius
        for yy in range(cy - radius, cy + radius + 1):
            for xx in range(cx - radius, cx + radius + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= radius2:
                    self.set(xx, yy, color)

    def line(self, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int], width: int = 1) -> None:
        dx, dy = abs(x2 - x1), -abs(y2 - y1)
        sx, sy = (1 if x1 < x2 else -1), (1 if y1 < y2 else -1)
        error = dx + dy
        while True:
            self.rect(x1 - width // 2, y1 - width // 2, width, width, color)
            if x1 == x2 and y1 == y2:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x1 += sx
            if twice <= dx:
                error += dx
                y1 += sy

    def write_png(self, path: Path) -> None:
        raw = b"".join(
            b"\x00" + bytes(self.pixels[y * self.width * 3 : (y + 1) * self.width * 3])
            for y in range(self.height)
        )

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(raw, 9))
        png += chunk(b"IEND", b"")
        path.write_bytes(png)


def generate_dev_001() -> None:
    out = asset_dir("dev_cases/dev_001")
    write_text(
        out / "commissioning_report.html",
        """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Alder Creek BESS commissioning report</title></head>
<body>
<h1 id="report-title">Alder Creek storage commissioning report, issue 2</h1>
<p id="scope">This report covers Trial B on 2026-04-18. Trial A and the similarly named Alder Ridge project are archival distractors.</p>
<section><h2 id="acceptance-heading">Acceptance rule</h2>
<p id="rte-rule">For Trial B, round-trip efficiency is the sum of delivered AC energy divided by the sum of charging AC energy for rows marked valid. Warm-up and maintenance rows are excluded. The contractual minimum is 85.0 percent.</p>
<p id="peak-rule">Coincident peak reduction is baseline grid import minus observed grid import on the row having the highest baseline import.</p></section>
<section><h2 id="revision-heading">Revision and scope</h2>
<p id="revision-rule">Issue 2 supersedes the draft issue. Commissioning acceptance uses AC-side values and includes auxiliary loads.</p>
<p id="missing-aging">No annual capacity-fade or twelve-month degradation measurement was performed in this commissioning campaign.</p></section>
<section><h2 id="distractor-heading">Archive note</h2>
<p id="distractor">Alder Ridge Trial B used a different inverter family and a draft threshold of 83 percent; it is not part of this decision.</p></section>
</body></html>
""",
    )
    rows = [["timestamp", "trial", "status", "charge_mwh", "delivered_mwh", "baseline_grid_mw", "observed_grid_mw"]]
    values = [
        ("08:00", "warmup", 1.2, 0.80, 6.8, 6.4),
        ("08:15", "valid", 2.5, 2.10, 7.1, 6.0),
        ("08:30", "valid", 2.5, 2.15, 7.4, 6.2),
        ("08:45", "valid", 2.5, 2.15, 7.8, 6.3),
        ("09:00", "valid", 2.5, 2.10, 8.1, 6.4),
        ("09:15", "maintenance", 0.8, 0.50, 8.3, 6.7),
        ("09:30", "valid", 2.5, 2.20, 8.6, 6.1),
        ("09:45", "valid", 2.5, 2.15, 8.4, 6.2),
        ("10:00", "valid", 2.5, 2.15, 7.9, 6.1),
        ("10:15", "valid", 2.5, 2.10, 7.2, 5.9),
        ("10:30", "warmup", 1.0, 0.75, 6.7, 6.3),
    ]
    for timestamp, status, charge, delivered, baseline, observed in values:
        rows.append([f"2026-04-18T{timestamp}:00Z", "B", status, charge, delivered, baseline, observed])
    write_csv(out / "hourly_dispatch.csv", rows)
    write_text(
        out / "inverter_map.svg",
        """
<svg xmlns="http://www.w3.org/2000/svg" width="720" height="420" viewBox="0 0 720 420">
  <title id="chart-title">Inverter efficiency and enclosure temperature</title>
  <rect id="plot-background" x="60" y="35" width="600" height="310" fill="#ffffff" stroke="#222"/>
  <line id="x-axis" x1="60" y1="345" x2="660" y2="345" stroke="#111"/>
  <line id="y-axis" x1="60" y1="35" x2="60" y2="345" stroke="#111"/>
  <text id="x-label" x="285" y="392">enclosure temperature (C)</text>
  <text id="y-label" x="8" y="25">AC efficiency (%)</text>
  <circle id="point-25c" cx="190" cy="95" r="9" fill="#25855a"/>
  <text id="label-25c" x="155" y="78">25 C: 88%</text>
  <circle id="point-42c" cx="520" cy="165" r="9" fill="#c46718"/>
  <text id="label-42c" x="470" y="148">42 C field enclosure</text>
  <path id="trend-line" d="M190 95 L520 165" fill="none" stroke="#315f9f" stroke-width="4"/>
  <rect id="caption-box" x="80" y="245" width="555" height="72" fill="#eef4fb" stroke="#315f9f"/>
  <text id="caption-cause" x="96" y="274">At 42 C, cooling-fan draw and higher switching resistance</text>
  <text id="caption-cause-2" x="96" y="299">reduce AC efficiency under the field enclosure condition.</text>
</svg>
""",
    )


def generate_dev_002() -> None:
    out = asset_dir("dev_cases/dev_002")
    make_pdf(
        out / "release_protocol.pdf",
        [
            [
                ("text", 54, 742, 18, "Larkspur Cold-Chain Release Protocol v3.1", True),
                ("text", 54, 705, 12, "Section 1 - Scope", True),
                ("text", 54, 680, 10, "The protocol applies to six-hour vaccine-container qualification runs.", False),
                ("text", 54, 660, 10, "Recorded temperatures must be corrected using a calibration offset valid on the run date.", False),
                ("text", 54, 620, 10, "The Larkspur food warehouse protocol is unrelated and uses different limits.", False),
            ],
            [
                ("text", 54, 742, 18, "Section 2 - Release decision", True),
                ("text", 54, 704, 11, "Corrected temperature = recorded temperature + signed calibration offset.", False),
                ("text", 54, 678, 11, "A pallet passes only if every corrected reading is at or below 8.0 C.", False),
                ("text", 54, 652, 11, "A mean below the limit cannot override a single excursion.", False),
                ("text", 54, 626, 11, "If any linked probe lacks a valid calibration, place the pallet on hold.", False),
                ("text", 54, 584, 11, "Report each pallet mean to one decimal place after applying offsets.", False),
            ],
            [
                ("text", 54, 742, 18, "Section 3 - Evidence boundaries", True),
                ("text", 54, 704, 11, "Release logs establish temperature performance only.", False),
                ("text", 54, 678, 11, "This protocol contains no product shelf-life extension rule.", False),
                ("text", 54, 652, 11, "Packaging color and courier route do not affect the temperature calculation.", False),
                ("text", 54, 610, 10, "Revision note: v3.1 supersedes v3.0 for runs after 2026-01-01.", False),
            ],
        ],
    )
    readings = [["sample_utc", "pallet", "probe", "recorded_c", "operator_note"]]
    pallet_a = [7.1, 7.4, 7.6, 7.2, 7.5, 7.3]
    pallet_b = [7.2, 7.5, 7.9, 8.0, 7.8, 7.6]
    for index, value in enumerate(pallet_a):
        readings.append([f"2026-02-14T{10+index:02d}:00:00Z", "LP-A", "PX-17", value, "sealed container"])
    for index, value in enumerate(pallet_b):
        readings.append([f"2026-02-14T{10+index:02d}:00:00Z", "LP-B", "PX-22", value, "sealed container"])
    make_docx(
        out / "release_log.docx",
        [
            ("heading", "Larkspur qualification run LC-204"),
            ("paragraph", "Run date: 2026-02-14. Both pallets completed the full six-hour hold."),
            ("table", readings),
            ("paragraph", "The log records no shelf-life study and no product-potency assay."),
            ("paragraph", "Operator sign-off: M. Chen, qualification lead."),
        ],
    )
    make_xlsx(
        out / "calibration_register.xlsx",
        [
            (
                "Offsets",
                [
                    ["probe", "offset_c", "valid_from", "valid_through", "certificate"],
                    ["PX-17", 0.2, "2026-01-01", "2026-06-30", "CAL-881"],
                    ["PX-22", 0.3, "2026-01-15", "2026-03-31", "CAL-914"],
                    ["PX-31", -0.1, "2025-01-01", "2025-12-31", "expired distractor"],
                ],
            ),
            (
                "Certificate notes",
                [
                    ["certificate", "status", "scope"],
                    ["CAL-881", "signed", "PX-17 temperature offset"],
                    ["CAL-914", "signed", "PX-22 temperature offset"],
                    ["CAL-772", "withdrawn", "warehouse humidity only"],
                ],
            ),
        ],
    )


def generate_test_001() -> None:
    out = asset_dir("test_cases/test_001")
    make_pdf(
        out / "inspection_manual.pdf",
        [
            [
                ("text", 54, 744, 18, "North Harbor Pier Coating Manual, revision 5", True),
                ("text", 54, 706, 11, "Section 1 - Decision scope", True),
                ("text", 54, 680, 10, "Evaluate North Pier zones N1 through N4. South Harbor records are distractors.", False),
                ("text", 54, 654, 10, "Use inspected surface area as the weighting basis for coating-loss measurements.", False),
            ],
            [
                ("text", 54, 744, 18, "Section 3 - Quantitative criterion", True),
                ("text", 54, 706, 11, "Area-weighted mean loss = sum(area x loss) / sum(area).", False),
                ("text", 54, 678, 11, "The quantitative criterion passes at or below 0.35 mm.", False),
                ("text", 54, 650, 11, "Use the latest signed amendment for any invalidated measurement row.", False),
            ],
            [
                ("text", 54, 744, 18, "Section 4 - Visual criterion", True),
                ("text", 54, 706, 11, "Five or more critical red-marker defects require repair regardless of mean loss.", False),
                ("text", 54, 678, 11, "Use the inspector's image-key classifications when counting critical defects.", False),
                ("text", 54, 650, 11, "The overall decision passes only when both quantitative and visual criteria pass.", False),
            ],
            [
                ("text", 54, 744, 18, "Section 6 - Limits of inference", True),
                ("text", 54, 706, 11, "This inspection does not estimate remaining coating service life.", False),
                ("text", 54, 678, 11, "A contractor marketing claim is not a measured durability result.", False),
            ],
        ],
    )
    make_xlsx(
        out / "survey_results.xlsx",
        [
            (
                "North Pier",
                [
                    ["zone", "area_m2", "loss_mm", "row_status"],
                    ["N1", 120, 0.28, "valid"],
                    ["N2", 80, 0.41, "valid"],
                    ["N3", 60, 0.52, "superseded by amendment A-17"],
                    ["N4", 40, 0.22, "valid"],
                ],
            ),
            (
                "South Harbor",
                [
                    ["zone", "area_m2", "loss_mm", "row_status"],
                    ["S1", 500, 0.62, "different facility"],
                    ["S2", 300, 0.55, "different facility"],
                ],
            ),
        ],
    )
    make_docx(
        out / "signed_amendment.docx",
        [
            ("heading", "Signed field amendment A-17"),
            ("paragraph", "The N3 gauge was zeroed against the wrong shim. This signed amendment supersedes the N3 workbook row."),
            ("table", [["zone", "replacement_loss_mm", "status"], ["N3", 0.31, "approved replacement"]]),
            ("paragraph", "No other North Pier row is changed. Signed 2026-05-09 by inspection authority K. Rao."),
        ],
    )
    write_text(
        out / "photo_legend.html",
        """
<!doctype html><html><head><meta charset="utf-8"><title>North Pier image key</title></head><body>
<h1 id="key-title">Photograph NP-44 annotation key</h1>
<p id="orientation">The image covers North Pier panel N2. Marker positions are annotations placed over visible defects, not measurements of defect area.</p>
<p id="red-rule">Red circular markers identify critical coating breaks verified by the inspector.</p>
<p id="blue-rule">Blue circular markers identify cosmetic staining and must not be counted as critical defects.</p>
<p id="image-scope">Count each separated circular marker once. The grey bolt heads are unannotated structure.</p>
</body></html>
""",
    )
    image = Raster(720, 440, (184, 190, 196))
    image.rect(40, 55, 640, 330, (146, 154, 162))
    for x in range(90, 681, 100):
        image.circle(x, 90, 10, (90, 96, 102))
        image.circle(x, 350, 10, (90, 96, 102))
    for point in [(145, 150), (250, 205), (355, 132), (465, 260), (585, 185)]:
        image.circle(*point, 15, (211, 38, 45))
        image.circle(*point, 6, (255, 235, 235))
    for point in [(205, 300), (520, 320), (620, 120)]:
        image.circle(*point, 14, (42, 104, 190))
        image.circle(*point, 5, (230, 240, 255))
    image.write_png(out / "north_pier.png")


def generate_test_002() -> None:
    out = asset_dir("test_cases/test_002")
    write_text(
        out / "fare_policy.html",
        """
<!doctype html><html><head><meta charset="utf-8"><title>Metrovale fare policy</title></head><body>
<h1 id="policy-title">Metrovale adult stored-value fare policy, issue 7</h1>
<p id="base-rule">Adult base fare is 2.40 credits for travel within one zone and 3.60 credits when a journey crosses a zone boundary.</p>
<p id="transfer-rule">A second boarding within 45 minutes of the prior exit receives a 1.20-credit transfer discount. Apply the rule to elapsed time, not scheduled time.</p>
<p id="peak-rule">Weekday boardings from 07:00 through 09:00 incur a 0.80-credit peak supplement unless a signed route exception applies.</p>
<p id="refund-rule">Refund equals charged amount minus fare due; do not floor a negative difference into a refund.</p>
<p id="child-gap">The child-fare schedule is not included in this packet, so child journeys cannot be priced from these sources.</p>
<p id="revision-order">Signed service directives supersede this policy; unsigned customer-service emails do not.</p>
</body></html>
""",
    )
    write_text(
        out / "zone_map.svg",
        """
<svg xmlns="http://www.w3.org/2000/svg" width="800" height="360" viewBox="0 0 800 360">
 <title id="map-title">Metrovale route and fare zones</title>
 <rect id="zone-a" x="40" y="50" width="330" height="250" fill="#e9f4ff" stroke="#246"/>
 <rect id="zone-b" x="430" y="50" width="330" height="250" fill="#fff1df" stroke="#642"/>
 <text id="zone-a-label" x="165" y="80">ZONE A</text><text id="zone-b-label" x="555" y="80">ZONE B</text>
 <circle id="station-oak" cx="120" cy="180" r="16" fill="#315f9f"/><text id="label-oak" x="92" y="215">Oak</text>
 <circle id="station-pine" cx="290" cy="180" r="16" fill="#315f9f"/><text id="label-pine" x="260" y="215">Pine</text>
 <circle id="station-river" cx="510" cy="180" r="16" fill="#c46718"/><text id="label-river" x="478" y="215">River</text>
 <circle id="station-hill" cx="680" cy="180" r="16" fill="#c46718"/><text id="label-hill" x="655" y="215">Hill</text>
 <line id="route-n" x1="120" y1="180" x2="680" y2="180" stroke="#222" stroke-width="6"/>
 <text id="route-n-label" x="340" y="155">Route N</text>
</svg>
""",
    )
    make_docx(
        out / "signed_route_directive.docx",
        [
            ("heading", "Signed service directive SD-19"),
            ("paragraph", "Effective 2026-03-01, Route N transfer eligibility is extended from 45 to 60 minutes during signal reconstruction."),
            ("table", [["route", "exception", "effective"], ["N", "peak supplement waived", "2026-03-01 through 2026-06-30"]]),
            ("paragraph", "All base-zone fares remain unchanged. Signed by the fare authority on 2026-02-24."),
        ],
    )
    write_csv(
        out / "tap_events.csv",
        [
            ["journey", "rider_class", "event", "station", "timestamp_local", "charged_credits", "route"],
            ["J-101", "adult", "board", "Oak", "2026-04-06 07:20", 4.40, "N"],
            ["J-101", "adult", "exit", "River", "2026-04-06 07:42", "", "N"],
            ["J-102", "adult", "board", "River", "2026-04-06 08:30", 3.60, "N"],
            ["J-102", "adult", "exit", "Hill", "2026-04-06 08:42", "", "N"],
            ["J-201", "adult", "board", "Pine", "2026-04-06 10:05", 3.60, "N"],
            ["J-201", "adult", "exit", "Hill", "2026-04-06 10:35", "", "N"],
            ["J-202", "adult", "board", "Hill", "2026-04-06 11:29", 3.60, "N"],
            ["J-202", "adult", "exit", "River", "2026-04-06 11:41", "", "N"],
            ["J-301", "child", "board", "Oak", "2026-04-06 12:00", 1.20, "N"],
            ["J-301", "child", "exit", "Pine", "2026-04-06 12:18", "", "N"],
            ["D-9", "adult", "board", "Oak", "2026-02-10 08:00", 4.40, "S"],
            ["D-9", "adult", "exit", "Hill", "2026-02-10 08:31", "", "S"],
        ],
    )


def generate_test_003() -> None:
    out = asset_dir("test_cases/test_003")
    make_docx(
        out / "assay_method.docx",
        [
            ("heading", "Orchid assay method OM-6"),
            ("paragraph", "Calculate concentration as (sample absorbance - approved blank) / slope, then multiply by the sample dilution factor."),
            ("table", [["sample", "wells", "dilution_factor"], ["Orchid-K", "B05 and B06", 4], ["Control-Q", "B07 and B08", 2]]),
            ("paragraph", "Average valid replicate concentrations. Do not substitute an invalidated well."),
            ("table", [["quality_control", "acceptable_mg_per_l"], ["Control-Q", "8.0 to 12.0"]]),
            ("paragraph", "The method contains no stability result after 30 days."),
        ],
    )
    rows = [["well", "sample", "absorbance", "run_flag"]]
    for well, sample, absorbance, flag in [
        ("A01", "blank-1", 0.068, "blank"),
        ("A02", "blank-2", 0.072, "blank"),
        ("B05", "Orchid-K", 0.320, "sample"),
        ("B06", "Orchid-K", 0.330, "sample"),
        ("B07", "Control-Q", 0.225, "invalidate by RN-8"),
        ("B08", "Control-Q", 0.195, "valid"),
        ("C01", "Iris-M distractor", 0.610, "different assay"),
    ]:
        rows.append([well, sample, absorbance, flag])
    for index in range(9, 49):
        rows.append([f"D{index:02d}", "wash/background", round(0.04 + (index % 7) * 0.003, 3), "not a requested sample"])
    write_csv(out / "plate_reader.csv", rows)
    write_text(
        out / "standard_curve.svg",
        """
<svg xmlns="http://www.w3.org/2000/svg" width="760" height="460" viewBox="0 0 760 460">
 <title id="curve-title">Orchid OM-6 standard curve</title>
 <rect id="curve-plot" x="80" y="45" width="610" height="330" fill="white" stroke="#222"/>
 <line id="curve-x-axis" x1="80" y1="375" x2="690" y2="375" stroke="#111"/>
 <line id="curve-y-axis" x1="80" y1="45" x2="80" y2="375" stroke="#111"/>
 <text id="curve-x-label" x="300" y="425">concentration (mg/L)</text>
 <text id="curve-y-label" x="12" y="28">absorbance after blank</text>
 <path id="fit-line" d="M100 355 L650 80" fill="none" stroke="#2768a8" stroke-width="4"/>
 <text id="fit-equation" x="420" y="120">slope = 0.025 absorbance per mg/L</text>
 <text id="fit-r2" x="420" y="145">R2 = 0.998</text>
 <circle id="std-4" cx="210" cy="300" r="7" fill="#25855a"/>
 <circle id="std-8" cx="320" cy="245" r="7" fill="#25855a"/>
 <circle id="std-12" cx="430" cy="190" r="7" fill="#25855a"/>
</svg>
""",
    )
    make_pdf(
        out / "revision_notice.pdf",
        [
            [
                ("text", 54, 744, 18, "Signed revision notice RN-8", True),
                ("text", 54, 704, 11, "The approved lot-specific blank for plate O-441 is 0.070 absorbance units.", False),
                ("text", 54, 676, 11, "Do not use the draft universal blank of 0.050 for this plate.", False),
                ("text", 54, 648, 11, "RN-8 supersedes worksheet header note OM-6-draft.", False),
            ],
            [
                ("text", 54, 744, 18, "Well validity correction", True),
                ("text", 54, 704, 11, "Control-Q well B07 is invalid because of a confirmed pipette skip.", False),
                ("text", 54, 676, 11, "Control-Q well B08 remains valid and may be evaluated alone.", False),
                ("text", 54, 648, 11, "No Orchid-K well is invalidated by this notice.", False),
            ],
        ],
    )


def generate_test_004() -> None:
    out = asset_dir("test_cases/test_004")
    make_pdf(
        out / "bathymetry_report.pdf",
        [
            [
                ("text", 54, 744, 18, "Mesa Reservoir bathymetric survey 2026", True),
                ("text", 54, 706, 11, "Section 1 - Survey scope", True),
                ("text", 54, 678, 10, "Four cross-sections T1 through T4 define three consecutive reaches.", False),
                ("text", 54, 652, 10, "Cross-sectional deposited areas are supplied in the survey CSV.", False),
            ],
            [
                ("text", 54, 744, 18, "Section 2 - Volume method", True),
                ("text", 54, 706, 11, "For each reach, volume = length x (upstream area + downstream area) / 2.", False),
                ("text", 54, 678, 11, "Total deposited volume is the sum of all reach volumes.", False),
                ("text", 54, 650, 11, "Draft dry bulk density: 1.52 tonnes per cubic metre; see signed field correction.", False),
            ],
            [
                ("text", 54, 744, 18, "Section 4 - Management threshold", True),
                ("text", 54, 706, 11, "Mechanical removal planning begins when estimated dry mass exceeds 50,000 tonnes.", False),
                ("text", 54, 678, 11, "Round final volume and mass to the nearest whole unit.", False),
                ("text", 54, 650, 11, "The threshold is operational, not a contaminant-toxicity standard.", False),
            ],
            [
                ("text", 54, 744, 18, "Section 6 - Evidence limits", True),
                ("text", 54, 706, 11, "No sediment chemistry or contaminant concentration was sampled.", False),
                ("text", 54, 678, 11, "No laboratory inference may be added to this physical-volume survey.", False),
            ],
        ],
    )
    write_csv(
        out / "survey_points.csv",
        [
            ["transect", "chainage_m", "deposited_area_m2", "quality"],
            ["T1", 0, 42, "accepted"],
            ["T2", 280, 58, "accepted"],
            ["T3", 610, 96, "accepted"],
            ["T4", 940, 70, "accepted"],
            ["M-T3", 620, 140, "Mesa Creek tributary distractor"],
        ],
    )
    write_text(
        out / "field_correction.html",
        """
<!doctype html><html><head><meta charset="utf-8"><title>Mesa field correction</title></head><body>
<h1 id="correction-title">Signed field correction FC-3</h1>
<p id="density-correction">Core drying records establish a dry bulk density of 1.25 tonnes per cubic metre, replacing the draft 1.52 value in the report.</p>
<p id="priority">FC-3 is signed and has priority over the draft density printed on report page 2.</p>
<p id="panel-order">The raster panels run left to right as T1, T2, T3, and T4. Deep red indicates the highest deposition-intensity class.</p>
<p id="visual-limit">The color image is qualitative and must not replace the CSV values in the volume calculation.</p>
</body></html>
""",
    )
    image = Raster(820, 360, (242, 244, 247))
    colors = [(248, 205, 112), (242, 151, 72), (178, 28, 36), (225, 91, 56)]
    for index, color in enumerate(colors):
        x = 35 + index * 195
        image.rect(x, 50, 165, 245, (220, 225, 230))
        for band in range(7):
            shade = tuple(max(0, min(255, channel + (band - 3) * 8)) for channel in color)
            image.rect(x + 10, 65 + band * 30, 145, 25, shade)
        image.line(x + 10, 315, x + 155, 315, (30, 35, 40), 3)
    image.write_png(out / "transect_intensity.png")


def generate_test_005() -> None:
    out = asset_dir("test_cases/test_005")
    write_text(
        out / "archive_index.html",
        """
<!doctype html><html><head><meta charset="utf-8"><title>Juniper procurement archive</title></head><body>
<h1 id="archive-title">Juniper procurement archive index</h1>
<p id="entity-rule">Juniper Ridge Phase II is the onshore wind project in Rowan County. Juniper Bay is an offshore project, and Juniper Ridge Phase I is a separate completed phase.</p>
<p id="revision-rule">For Phase II, revision R4 supersedes R3 and R2. A signed addendum supersedes unsigned correspondence; board-approved minutes establish turbine count.</p>
<p id="currency-rule">Use the offer currency and total project price from the selected Phase II R4 commercial sheet without currency conversion.</p>
<p id="missing-bond">The archive index contains no approved decommissioning-bond amount for Phase II.</p>
</body></html>
""",
    )
    make_pdf(
        out / "board_minutes.pdf",
        [
            [
                ("text", 48, 748, 17, "Juniper Energy Board Minutes - compiled archive", True),
                ("text", 48, 710, 11, "Page 1: Phase I closeout", True),
                ("text", 48, 682, 10, "Phase I operated 18 turbines. This completed phase is not the procurement under review.", False),
                ("text", 48, 654, 10, "A maintenance interval of 9 months applies only to Phase I gearboxes.", False),
            ],
            [
                ("text", 48, 748, 17, "Page 2: Juniper Bay offshore concept", True),
                ("text", 48, 710, 10, "The Bay concept considered 24 offshore units and a 15-year service package.", False),
                ("text", 48, 682, 10, "No Bay figure applies to Rowan County or Juniper Ridge Phase II.", False),
            ],
            [
                ("text", 48, 748, 17, "Page 3: Phase II preliminary R2", True),
                ("text", 48, 710, 10, "R2 considered 16 turbines; the resolution was tabled and later superseded.", False),
                ("text", 48, 682, 10, "An unsigned appendix discussed a 33 percent capacity factor.", False),
            ],
            [
                ("text", 48, 748, 17, "Page 4: Phase II approved R4 resolution", True),
                ("text", 48, 710, 11, "Resolution JR-II-R4 authorizes procurement of 14 turbines for Rowan County.", False),
                ("text", 48, 682, 10, "The commercial rating and price remain in the vendor workbook.", False),
                ("text", 48, 654, 10, "Approved by recorded vote 7-1 on 2026-06-18.", False),
            ],
            [
                ("text", 48, 748, 17, "Page 5: Matters deferred", True),
                ("text", 48, 710, 10, "Warranty terms were delegated to the signed commercial addendum.", False),
                ("text", 48, 682, 10, "The decommissioning bond was deferred to a future resolution.", False),
            ],
        ],
    )
    make_xlsx(
        out / "vendor_offer.xlsx",
        [
            (
                "Phase II R4",
                [
                    ["line", "item", "unit_value", "units", "status"],
                    [1, "turbine nameplate rating", 4.2, "MW per turbine", "firm"],
                    [2, "total project price", 168000000, "USD", "firm"],
                    [3, "construction allowance", 7000000, "USD", "included in total"],
                    [4, "offshore corrosion kit", 0, "not applicable", "Juniper Bay distractor"],
                ],
            ),
            (
                "Phase II R3",
                [
                    ["line", "item", "unit_value", "units", "status"],
                    [1, "turbine nameplate rating", 4.0, "MW per turbine", "superseded"],
                    [2, "total project price", 151000000, "USD", "superseded"],
                ],
            ),
            (
                "Juniper Bay",
                [
                    ["line", "item", "unit_value", "units", "status"],
                    [1, "turbine nameplate rating", 12, "MW per turbine", "different project"],
                    [2, "total project price", 840000000, "USD", "different project"],
                ],
            ),
        ],
    )
    make_docx(
        out / "signed_addendum.docx",
        [
            ("heading", "Juniper Ridge Phase II signed commercial addendum A4"),
            ("paragraph", "This addendum applies to revision R4 and supersedes draft commercial correspondence."),
            ("table", [["term", "approved_value", "scope"], ["net capacity factor", "38 percent", "annual-energy estimate"], ["major component warranty", "12 years", "gearbox and generator"]]),
            ("paragraph", "Expected annual energy uses 8,760 hours per year and no additional availability multiplier."),
            ("paragraph", "Signed by both purchaser and vendor on 2026-06-20."),
        ],
    )
    email_rows = [["message_id", "project_label", "revision", "sender_status", "statement"]]
    distractors = [
        ("Juniper Ridge Phase I", "R1", "unsigned", "a proposed eight-month inspection interval after closeout"),
        ("Juniper Bay", "concept", "unsigned", "twenty-two offshore units with an unapproved service option"),
        ("Juniper Ridge Phase II", "R2", "unsigned", "15 turbines and a speculative 34 percent capacity factor"),
        ("Juniper Ridge Phase II", "R3", "unsigned", "4.1 MW machines at an unapproved draft price"),
    ]
    for index in range(1, 81):
        project, revision, status, statement = distractors[index % len(distractors)]
        email_rows.append([f"MSG-{index:03d}", project, revision, status, statement + f"; archive thread {index}"])
    email_rows.append(["MSG-081", "Juniper Ridge Phase II", "R4", "unsigned", "A salesperson estimates 35 percent capacity factor and a 10-year warranty"])
    write_csv(out / "email_export.csv", email_rows)


def generate_test_006() -> None:
    out = asset_dir("test_cases/test_006")
    write_text(
        out / "firmware_release.html",
        """
<!doctype html><html><head><meta charset="utf-8"><title>Tidepool firmware note</title></head><body>
<h1 id="release-title">Tidepool humidity firmware release 2.3</h1>
<p id="correction-rule">For devices H-7 and H-8 after the 2.3 activation time, corrected humidity equals logged humidity minus 2 percentage points.</p>
<p id="threshold-rule">An alarm interval begins at the first corrected reading at or above 80 percent and ends at the timestamp of the first corrected reading below 80 percent.</p>
<p id="duration-rule">Treat consecutive five-minute samples as a continuous interval; report elapsed minutes, not the number of samples.</p>
<p id="panel-display-rule">Until the next panel reboot, the physical alarm lights continue to evaluate uncorrected logged humidity; the corrected dashboard uses release-2.3 values.</p>
<p id="scope-gap">Release 2.3 does not diagnose battery condition or establish the physical cause of an alarm.</p>
<p id="distractor">Tidepool salinity firmware 2.3-S uses a different correction and is unrelated.</p>
</body></html>
""",
    )
    make_docx(
        out / "operator_handoff.docx",
        [
            ("heading", "Signed operator handoff TH-12"),
            ("paragraph", "Firmware 2.3 was activated at 2026-07-02 10:00 UTC for H-7 and H-8."),
            (
                "table",
                [
                    ["device", "panel_position", "clock_adjustment", "panel_basis_at_photo"],
                    ["H-7", "left", "none", "unrebooted: uncorrected logged humidity"],
                    ["H-8", "right", "add 5 minutes to logged timestamp", "rebooted: corrected humidity"],
                ],
            ),
            ("paragraph", "The panel photograph was taken at 10:16 UTC. Red means active alarm; green means normal."),
            ("paragraph", "No battery test was performed during the handoff."),
        ],
    )
    rows = [["device", "logged_timestamp_utc", "logged_humidity_pct", "firmware", "quality"]]
    h7 = [77, 79, 83, 85, 81, 78, 77]
    h8 = [78, 79, 80, 81, 83, 79, 78]
    for index, value in enumerate(h7):
        rows.append(["H-7", f"2026-07-02 09:{55 + index * 5:02d}" if index == 0 else f"2026-07-02 10:{(index - 1) * 5:02d}", value, "2.3", "accepted"])
    # H-8 timestamps are five minutes slow and require the signed handoff correction.
    for index, value in enumerate(h8):
        minute = 55 + index * 5
        hour = 9 + minute // 60
        minute %= 60
        rows.append(["H-8", f"2026-07-02 {hour:02d}:{minute:02d}", value, "2.3", "accepted"])
    for index in range(1, 25):
        rows.append(["S-3", f"2026-07-01 08:{index:02d}", 48 + index % 4, "2.3-S", "salinity-station distractor"])
    write_csv(out / "event_log.csv", rows)
    write_text(
        out / "dashboard.svg",
        """
<svg xmlns="http://www.w3.org/2000/svg" width="840" height="460" viewBox="0 0 840 460">
 <title id="dashboard-title">Tidepool corrected-humidity dashboard</title>
 <rect id="plot" x="80" y="45" width="690" height="330" fill="white" stroke="#222"/>
 <line id="threshold-line" x1="80" y1="185" x2="770" y2="185" stroke="#b21f2d" stroke-width="3"/>
 <text id="threshold-label" x="635" y="170">alarm threshold</text>
 <polyline id="h7-series" points="100,250 200,220 300,170 400,140 500,200 600,240 700,255" fill="none" stroke="#315f9f" stroke-width="5"/>
 <polyline id="h8-series" points="100,240 200,225 300,210 400,200 500,170 600,225 700,240" fill="none" stroke="#25855a" stroke-width="5"/>
 <text id="h7-label" x="115" y="90">H-7 corrected series</text>
 <text id="h8-label" x="115" y="115">H-8 corrected series after clock alignment</text>
 <text id="time-axis" x="315" y="425">aligned UTC time, five-minute samples</text>
</svg>
""",
    )
    image = Raster(640, 360, (64, 70, 76))
    image.rect(70, 55, 500, 250, (30, 34, 38))
    image.rect(105, 95, 180, 170, (88, 96, 104))
    image.rect(355, 95, 180, 170, (88, 96, 104))
    image.circle(195, 175, 42, (210, 35, 45))
    image.circle(445, 175, 42, (36, 170, 88))
    image.circle(195, 175, 15, (255, 205, 205))
    image.circle(445, 175, 15, (210, 255, 225))
    image.write_png(out / "panel_photo.png")


def write_provenance() -> None:
    records = []
    for case_group in ("dev_cases", "test_cases"):
        for case in sorted((ROOT / case_group).iterdir()):
            assets = case / "assets"
            for path in sorted(assets.iterdir()):
                if path.is_file():
                    records.append(
                        {
                            "case": f"{case_group}/{case.name}",
                            "asset": path.name,
                            "bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "provenance": "synthetic; generated locally by meta/generate_assets.py",
                            "license": "CC0-1.0 benchmark fixture",
                        }
                    )
    (ROOT / "meta" / "asset_provenance.json").write_text(
        json.dumps({"schema_version": "4.0", "assets": records}, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    generate_dev_001()
    generate_dev_002()
    generate_test_001()
    generate_test_002()
    generate_test_003()
    generate_test_004()
    generate_test_005()
    generate_test_006()
    write_provenance()
    print(json.dumps({"status": "ok", "asset_count": len(json.loads((ROOT / 'meta' / 'asset_provenance.json').read_text())['assets'])}))


if __name__ == "__main__":
    main()
