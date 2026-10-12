#!/usr/bin/env python3
"""Dependency-free parsers and native-locator checks for the v4 fixtures."""

from __future__ import annotations

import csv
import html
import math
import re
import struct
import zlib
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile


PDF_WIDTH = 612.0
PDF_HEIGHT = 792.0
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
SOURCE_LOCATOR_KINDS = {
    ".html": {"html_element"},
    ".csv": {"csv_cell"},
    ".xlsx": {"xlsx_cell"},
    ".pdf": {"pdf_rect"},
    ".docx": {"docx_paragraph", "docx_cell"},
    ".svg": {"svg_element"},
    ".png": {"image_rect"},
    ".md": {"byte_range"},
    ".txt": {"byte_range"},
}


def normalize_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return " ".join(value.replace("\u2212", "-").split()).strip()


def overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2, bx2, by2 = ax1 + aw, ay1 + ah, bx1 + bw, by1 + bh
    width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    height = max(0.0, min(ay2, by2) - max(ay1, by1))
    return width * height


def normalized_rect(locator: dict[str, Any]) -> tuple[float, float, float, float]:
    values = tuple(locator.get(key) for key in ("x", "y", "width", "height"))
    if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
        raise ValueError("rectangle coordinates must be finite numbers")
    rect = tuple(float(value) for value in values)
    if rect[0] < 0 or rect[1] < 0 or rect[2] <= 0 or rect[3] <= 0:
        raise ValueError("rectangle must have non-negative origin and positive size")
    if rect[0] + rect[2] > 1.000001 or rect[1] + rect[3] > 1.000001:
        raise ValueError("rectangle exceeds normalized source bounds")
    return rect


def pdf_unescape(value: bytes) -> str:
    text = value.decode("latin-1", "replace")
    text = text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
    return text


def pdf_runs(path: Path) -> list[list[dict[str, Any]]]:
    raw = path.read_bytes()
    pages: list[list[dict[str, Any]]] = []
    pattern = re.compile(
        rb"BT\s+/F\d+\s+([0-9.]+)\s+Tf\s+1\s+0\s+0\s+1\s+([0-9.]+)\s+([0-9.]+)\s+Tm\s+\((.*?)\)\s+Tj\s+ET",
        re.S,
    )
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        try:
            stream = zlib.decompress(stream)
        except zlib.error:
            pass
        runs = []
        for size_raw, x_raw, y_raw, text_raw in pattern.findall(stream):
            size, x, y = float(size_raw), float(x_raw), float(y_raw)
            text = pdf_unescape(text_raw)
            width = max(size * 0.45, len(text) * size * 0.53)
            height = size * 1.25
            runs.append(
                {
                    "text": text,
                    "rect": (x / PDF_WIDTH, (PDF_HEIGHT - y - height) / PDF_HEIGHT, width / PDF_WIDTH, height / PDF_HEIGHT),
                    "x": x,
                    "y": y,
                    "size": size,
                }
            )
        if runs:
            pages.append(runs)
    if not pages:
        raise ValueError(f"no positioned text found in PDF {path.name}")
    return pages


def docx_content(path: Path) -> tuple[list[str], list[list[list[str]]]]:
    with ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(f"{W_NS}body")
    if body is None:
        raise ValueError("DOCX has no body")
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    for child in body:
        if child.tag == f"{W_NS}p":
            paragraphs.append("".join(child.itertext()))
        elif child.tag == f"{W_NS}tbl":
            rows: list[list[str]] = []
            for row in child.findall(f"{W_NS}tr"):
                rows.append(["".join(cell.itertext()) for cell in row.findall(f"{W_NS}tc")])
            tables.append(rows)
    return paragraphs, tables


def xlsx_content(path: Path) -> dict[str, dict[str, str]]:
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships.findall(f"{PR_NS}Relationship")
        }
        sheets: dict[str, dict[str, str]] = {}
        sheet_parent = workbook.find(f"{S_NS}sheets")
        if sheet_parent is None:
            raise ValueError("XLSX has no sheets")
        for sheet in sheet_parent.findall(f"{S_NS}sheet"):
            name = sheet.attrib["name"]
            rel_id = sheet.attrib[f"{R_NS}id"]
            target = targets[rel_id]
            member = target if target.startswith("xl/") else f"xl/{target}"
            root = ET.fromstring(archive.read(member))
            values: dict[str, str] = {}
            for cell in root.findall(f".//{S_NS}c"):
                ref = cell.attrib.get("r")
                if not ref:
                    continue
                if cell.attrib.get("t") == "inlineStr":
                    values[ref] = "".join(cell.itertext())
                else:
                    value = cell.find(f"{S_NS}v")
                    values[ref] = "" if value is None or value.text is None else value.text
            sheets[name] = values
    return sheets


def csv_cells(raw: bytes) -> list[list[dict[str, Any]]]:
    """Return parsed cell values and exact raw byte spans for RFC4180-style UTF-8 CSV."""
    rows: list[list[dict[str, Any]]] = []
    row: list[dict[str, Any]] = []
    index = 0
    field_start = 0
    in_quotes = False
    while index < len(raw):
        byte = raw[index]
        if byte == 34:
            if in_quotes and index + 1 < len(raw) and raw[index + 1] == 34:
                index += 2
                continue
            in_quotes = not in_quotes
            index += 1
            continue
        if not in_quotes and byte in (44, 10, 13):
            field_end = index
            field_raw = raw[field_start:field_end]
            decoded = field_raw.decode("utf-8")
            parsed = next(csv.reader([decoded]))[0] if decoded else ""
            content_start, content_end = field_start, field_end
            if field_raw.startswith(b'"') and field_raw.endswith(b'"') and len(field_raw) >= 2:
                content_start += 1
                content_end -= 1
            row.append({"value": parsed, "byte_start": content_start, "byte_end": content_end})
            if byte == 44:
                field_start = index + 1
                index += 1
                continue
            rows.append(row)
            row = []
            if byte == 13 and index + 1 < len(raw) and raw[index + 1] == 10:
                index += 1
            field_start = index + 1
        index += 1
    if field_start < len(raw) or row:
        field_raw = raw[field_start:]
        decoded = field_raw.decode("utf-8")
        parsed = next(csv.reader([decoded]))[0] if decoded else ""
        content_start, content_end = field_start, len(raw)
        if field_raw.startswith(b'"') and field_raw.endswith(b'"') and len(field_raw) >= 2:
            content_start += 1
            content_end -= 1
        row.append({"value": parsed, "byte_start": content_start, "byte_end": content_end})
        rows.append(row)
    return rows


def png_rgb(path: Path) -> tuple[int, int, bytes]:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    index = 8
    width = height = None
    compressed = bytearray()
    saw_iend = False
    while index < len(raw):
        if index + 12 > len(raw):
            raise ValueError("truncated PNG chunk header")
        length = struct.unpack(">I", raw[index : index + 4])[0]
        kind = raw[index + 4 : index + 8]
        if index + 12 + length > len(raw):
            raise ValueError("truncated PNG chunk")
        data = raw[index + 8 : index + 8 + length]
        expected_crc = struct.unpack(">I", raw[index + 8 + length : index + 12 + length])[0]
        actual_crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError(f"PNG chunk {kind.decode('latin-1', 'replace')} has invalid CRC")
        index += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if (depth, color_type, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ValueError("validator supports only non-interlaced 8-bit RGB PNG")
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            saw_iend = True
            break
    if width is None or height is None or not saw_iend:
        raise ValueError("PNG missing IHDR")
    decoded = zlib.decompress(bytes(compressed))
    stride = width * 3
    if len(decoded) != height * (stride + 1):
        raise ValueError("PNG decompressed scanline length is inconsistent with IHDR")
    output = bytearray()
    prior = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = decoded[position]
        position += 1
        scan = bytearray(decoded[position : position + stride])
        position += stride
        if filter_type == 1:
            for i in range(stride):
                scan[i] = (scan[i] + (scan[i - 3] if i >= 3 else 0)) & 255
        elif filter_type == 2:
            for i in range(stride):
                scan[i] = (scan[i] + prior[i]) & 255
        elif filter_type != 0:
            raise ValueError(f"unsupported PNG filter {filter_type}")
        output.extend(scan)
        prior = scan
    return width, height, bytes(output)


def svg_viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    viewbox = root.attrib.get("viewBox")
    if viewbox:
        values = [float(value) for value in viewbox.replace(",", " ").split()]
        if len(values) == 4:
            return tuple(values)  # type: ignore[return-value]
    return 0.0, 0.0, float(root.attrib["width"]), float(root.attrib["height"])


def element_bbox(element: ET.Element) -> tuple[float, float, float, float]:
    tag = element.tag.rsplit("}", 1)[-1]
    number = lambda key, default=0.0: float(re.sub(r"[^0-9.+-]", "", element.attrib.get(key, str(default))) or default)
    if tag == "rect":
        return number("x"), number("y"), number("width"), number("height")
    if tag == "circle":
        cx, cy, radius = number("cx"), number("cy"), number("r")
        return cx - radius, cy - radius, 2 * radius, 2 * radius
    if tag == "line":
        x1, y1, x2, y2 = number("x1"), number("y1"), number("x2"), number("y2")
        return min(x1, x2), min(y1, y2), max(abs(x2 - x1), 1.0), max(abs(y2 - y1), 1.0)
    if tag == "text":
        text = "".join(element.itertext())
        x, y = number("x"), number("y")
        return x, y - 16, max(8.0, len(text) * 8.0), 18.0
    if tag in {"polyline", "polygon"}:
        values = [float(value) for value in re.findall(r"[-+]?[0-9]*\.?[0-9]+", element.attrib.get("points", ""))]
        points = list(zip(values[0::2], values[1::2]))
        if not points:
            raise ValueError("SVG polyline has no points")
        xs, ys = zip(*points)
        return min(xs), min(ys), max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0)
    if tag == "path":
        values = [float(value) for value in re.findall(r"[-+]?[0-9]*\.?[0-9]+", element.attrib.get("d", ""))]
        points = list(zip(values[0::2], values[1::2]))
        if not points:
            raise ValueError("SVG path has no coordinate pairs")
        xs, ys = zip(*points)
        return min(xs), min(ys), max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0)
    child_boxes = [element_bbox(child) for child in element if isinstance(child.tag, str)]
    if child_boxes:
        x1 = min(box[0] for box in child_boxes)
        y1 = min(box[1] for box in child_boxes)
        x2 = max(box[0] + box[2] for box in child_boxes)
        y2 = max(box[1] + box[3] for box in child_boxes)
        return x1, y1, x2 - x1, y2 - y1
    raise ValueError(f"cannot derive geometry for SVG {tag}")


@dataclass
class ValidationResult:
    kind: str
    detail: str


class SourceInspector:
    def __init__(self, case_dir: Path):
        self.case_dir = case_dir.resolve()
        assets = self.case_dir / "assets"
        if not assets.is_dir():
            raise ValueError("case has no assets directory")
        self.assets = {
            path.relative_to(assets).as_posix(): path
            for path in assets.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def source(self, source_id: str) -> Path:
        if source_id not in self.assets:
            raise ValueError(f"unknown or unsafe source {source_id!r}")
        return self.assets[source_id]

    def validate(self, source_id: str, locator: dict[str, Any], observation: str) -> ValidationResult:
        path = self.source(source_id)
        if not isinstance(locator, dict):
            raise ValueError("locator must be an object")
        kind = locator.get("kind")
        allowed = SOURCE_LOCATOR_KINDS.get(path.suffix.lower())
        if allowed is None:
            raise ValueError(f"unsupported source type {path.suffix.lower()!r}")
        if kind not in allowed:
            raise ValueError(
                f"locator kind {kind!r} is not source-native for {path.suffix.lower()}; "
                f"expected one of {sorted(allowed)}"
            )
        if kind == "byte_range":
            return self._byte_range(path, locator, observation)
        if kind == "html_element":
            return self._html_element(path, locator, observation)
        if kind == "csv_cell":
            return self._csv_cell(path, locator, observation)
        if kind == "xlsx_cell":
            return self._xlsx_cell(path, locator, observation)
        if kind == "pdf_rect":
            return self._pdf_rect(path, locator, observation)
        if kind == "docx_paragraph":
            return self._docx_paragraph(path, locator, observation)
        if kind == "docx_cell":
            return self._docx_cell(path, locator, observation)
        if kind == "svg_element":
            return self._svg_element(path, locator, observation)
        if kind == "image_rect":
            return self._image_rect(path, locator)
        raise ValueError(f"unsupported locator kind {kind!r}")

    @staticmethod
    def _range(locator: dict[str, Any], length: int) -> tuple[int, int]:
        start, end = locator.get("byte_start"), locator.get("byte_end")
        if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= length):
            raise ValueError("byte range is outside exact source bytes")
        return start, end

    def _byte_range(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        raw = path.read_bytes()
        start, end = self._range(locator, len(raw))
        selected = raw[start:end].decode("utf-8", "strict")
        if observation and normalize_text(observation) not in normalize_text(selected):
            raise ValueError("observation is not contained in selected byte range")
        return ValidationResult("byte_range", f"bytes {start}:{end}")

    def _html_element(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        raw = path.read_bytes()
        start, end = self._range(locator, len(raw))
        element_id = locator.get("element_id")
        if not isinstance(element_id, str) or not element_id:
            raise ValueError("HTML locator has no element_id")
        selected = raw[start:end].decode("utf-8", "strict")
        if not re.search(rf"\bid\s*=\s*(['\"]){re.escape(element_id)}\1", selected):
            raise ValueError("HTML byte range does not contain the requested element ID")
        full = raw.decode("utf-8", "strict")
        matches = list(re.finditer(rf"<([A-Za-z][A-Za-z0-9:-]*)\b[^>]*\bid\s*=\s*(['\"]){re.escape(element_id)}\2[^>]*>", full))
        if len(matches) != 1:
            raise ValueError("HTML element ID is absent or not unique")
        match = matches[0]
        tag = match.group(1)
        closing = re.search(rf"</{re.escape(tag)}\s*>", full[match.end() :], re.I)
        if closing is None:
            raise ValueError("HTML identified element has no closing tag")
        actual_start = len(full[: match.start()].encode("utf-8"))
        actual_end_chars = match.end() + closing.end()
        actual_end = len(full[:actual_end_chars].encode("utf-8"))
        if (start, end) != (actual_start, actual_end):
            raise ValueError("HTML byte range does not equal the identified element's exact outer bytes")
        if observation and normalize_text(observation) not in normalize_text(selected):
            raise ValueError("observation is not contained in selected HTML element")
        return ValidationResult("html_element", element_id)

    def _csv_cell(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        raw = path.read_bytes()
        rows = csv_cells(raw)
        row_index, column_index = locator.get("row"), locator.get("column")
        if not isinstance(row_index, int) or not isinstance(column_index, int) or row_index < 1 or column_index < 1:
            raise ValueError("CSV row/column must be one-based positive integers")
        try:
            cell = rows[row_index - 1][column_index - 1]
        except IndexError as exc:
            raise ValueError("CSV cell is outside parsed table") from exc
        start, end = self._range(locator, len(raw))
        if (start, end) != (cell["byte_start"], cell["byte_end"]):
            raise ValueError("CSV byte range does not equal the parsed field span")
        header = locator.get("header")
        if not isinstance(header, str) or not rows or column_index > len(rows[0]) or rows[0][column_index - 1]["value"] != header:
            raise ValueError("CSV header does not match column")
        if observation and normalize_text(observation) not in normalize_text(cell["value"]):
            raise ValueError("observation is not contained in CSV cell")
        return ValidationResult("csv_cell", f"R{row_index}C{column_index}")

    def _xlsx_cell(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        sheets = xlsx_content(path)
        sheet = locator.get("sheet")
        if not isinstance(sheet, str) or sheet not in sheets:
            raise ValueError("XLSX sheet does not exist")
        refs: list[str]
        if isinstance(locator.get("cell"), str):
            if not re.fullmatch(r"[A-Z]+[1-9]\d*", locator["cell"]):
                raise ValueError("XLSX cell must be an uppercase A1 reference")
            refs = [locator["cell"]]
        elif isinstance(locator.get("range"), str) and ":" in locator["range"]:
            start, end = locator["range"].split(":", 1)
            match_start = re.fullmatch(r"([A-Z]+)(\d+)", start)
            match_end = re.fullmatch(r"([A-Z]+)(\d+)", end)
            if not match_start or not match_end:
                raise ValueError("XLSX range must use uppercase A1 references")

            def column_index(name: str) -> int:
                value = 0
                for character in name:
                    value = value * 26 + ord(character) - 64
                return value

            def column_name(index: int) -> str:
                value = ""
                while index:
                    index, remainder = divmod(index - 1, 26)
                    value = chr(65 + remainder) + value
                return value

            start_column, end_column = column_index(match_start.group(1)), column_index(match_end.group(1))
            start_row, end_row = int(match_start.group(2)), int(match_end.group(2))
            if start_column > end_column or start_row > end_row or start_row < 1:
                raise ValueError("XLSX range must be a forward contiguous rectangle")
            refs = [
                f"{column_name(column)}{row}"
                for row in range(start_row, end_row + 1)
                for column in range(start_column, end_column + 1)
            ]
        else:
            raise ValueError("XLSX locator requires cell or range")
        values = []
        for ref in refs:
            if ref not in sheets[sheet]:
                raise ValueError(f"XLSX cell {sheet}!{ref} does not exist")
            values.append(sheets[sheet][ref])
        if observation and normalize_text(observation) not in normalize_text(" ".join(values)):
            raise ValueError("observation is not contained in XLSX cell/range")
        return ValidationResult("xlsx_cell", f"{sheet}!{','.join(refs)}")

    def _pdf_rect(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        pages = pdf_runs(path)
        page = locator.get("page")
        if not isinstance(page, int) or not (1 <= page <= len(pages)):
            raise ValueError("PDF page is outside extracted page count")
        rect = normalized_rect(locator)
        candidates = [run for run in pages[page - 1] if overlap(rect, run["rect"]) > 0]
        if not candidates:
            raise ValueError("PDF rectangle does not overlap extracted positioned content")
        supporting = candidates
        if observation:
            needle = normalize_text(observation)
            supporting = [
                run
                for run in candidates
                if needle in normalize_text(run["text"]) or normalize_text(run["text"]) in needle
            ]
            combined = normalize_text(" ".join(run["text"] for run in candidates))
            if not supporting and needle not in combined:
                raise ValueError("PDF rectangle does not overlap text supporting the observation")
            if not supporting:
                supporting = candidates
        if not any(overlap(rect, run["rect"]) / max(run["rect"][2] * run["rect"][3], 1e-12) >= 0.20 for run in supporting):
            raise ValueError("PDF rectangle only grazes the supporting positioned text")
        declared_area = rect[2] * rect[3]
        supporting_area = sum(run["rect"][2] * run["rect"][3] for run in supporting)
        if declared_area > max(0.12, supporting_area * 8):
            raise ValueError("PDF rectangle is materially broader than the supporting positioned text")
        return ValidationResult("pdf_rect", f"page {page}, {len(candidates)} text run(s)")

    def _docx_paragraph(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        paragraphs, _ = docx_content(path)
        index = locator.get("paragraph")
        if not isinstance(index, int) or not (0 <= index < len(paragraphs)):
            raise ValueError("DOCX paragraph index is outside body paragraphs")
        if observation and normalize_text(observation) not in normalize_text(paragraphs[index]):
            raise ValueError("observation is not contained in DOCX paragraph")
        return ValidationResult("docx_paragraph", f"paragraph {index}")

    def _docx_cell(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        _, tables = docx_content(path)
        table, row, column = (locator.get(key) for key in ("table", "row", "column"))
        if not all(isinstance(value, int) and value >= 0 for value in (table, row, column)):
            raise ValueError("DOCX table/row/column must be zero-based non-negative integers")
        try:
            value = tables[table][row][column]
        except IndexError as exc:
            raise ValueError("DOCX table-cell locator is outside parsed table") from exc
        if observation and normalize_text(observation) not in normalize_text(value):
            raise ValueError("observation is not contained in DOCX table cell")
        return ValidationResult("docx_cell", f"table {table}, row {row}, column {column}")

    def _svg_element(self, path: Path, locator: dict[str, Any], observation: str) -> ValidationResult:
        root = ET.fromstring(path.read_bytes())
        element_id = locator.get("element_id")
        matches = [element for element in root.iter() if element.attrib.get("id") == element_id]
        if len(matches) != 1:
            raise ValueError("SVG element ID is absent or not unique")
        element = matches[0]
        vx, vy, vw, vh = svg_viewbox(root)
        x, y, width, height = element_bbox(element)
        actual = ((x - vx) / vw, (y - vy) / vh, width / vw, height / vh)
        declared = normalized_rect(locator)
        shared_area = overlap(actual, declared)
        if shared_area <= 0:
            raise ValueError("SVG locator rectangle does not overlap identified element geometry")
        declared_area = declared[2] * declared[3]
        actual_area = max(actual[2] * actual[3], 1e-9)
        if shared_area / actual_area < 0.20:
            raise ValueError("SVG locator only grazes the identified element geometry")
        if declared_area > max(actual_area * 12, 0.35):
            raise ValueError("SVG locator is materially broader than identified element")
        text = normalize_text("".join(element.itertext()))
        if observation and text and normalize_text(observation) not in text and text not in normalize_text(observation):
            raise ValueError("observation is not consistent with SVG element text")
        return ValidationResult("svg_element", str(element_id))

    def _image_rect(self, path: Path, locator: dict[str, Any]) -> ValidationResult:
        width, height, pixels = png_rgb(path)
        x, y, rect_width, rect_height = normalized_rect(locator)
        left = max(0, min(width - 1, int(x * width)))
        top = max(0, min(height - 1, int(y * height)))
        right = max(left + 1, min(width, math.ceil((x + rect_width) * width)))
        bottom = max(top + 1, min(height, math.ceil((y + rect_height) * height)))
        colors = set()
        step_x = max(1, (right - left) // 40)
        step_y = max(1, (bottom - top) // 40)
        for yy in range(top, bottom, step_y):
            for xx in range(left, right, step_x):
                offset = (yy * width + xx) * 3
                colors.add(pixels[offset : offset + 3])
                if len(colors) >= 3:
                    break
            if len(colors) >= 3:
                break
        if len(colors) < 2:
            raise ValueError("raster region contains no observable visual variation")
        if rect_width * rect_height > 0.50:
            raise ValueError("raster target is materially broader than a specific visual region")
        return ValidationResult("image_rect", f"pixels {left},{top} to {right},{bottom}")
