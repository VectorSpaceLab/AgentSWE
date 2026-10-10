#!/usr/bin/env python3
"""Validate exact-byte bundle, claim/manifest linkage, and static offline viewer safety."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.ids: list[str] = []
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}
        self.nonself_attributes: list[str] = []
        self.external_elements: list[str] = []
        self.inline_styles: list[str] = []
        self.in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"] or "")
            self.attributes_by_id[values["id"] or ""] = values
        for key in ("src", "href", "action", "poster", "data", "srcset"):
            value = values.get(key)
            if not value:
                continue
            references = [item.strip().split()[0] for item in value.split(",")] if key == "srcset" else [value.strip()]
            for reference in references:
                if reference.startswith(("#", "data:", "blob:", "about:")):
                    continue
                self.nonself_attributes.append(f"{tag}[{key}]={reference}")
        if tag in {"script", "link", "iframe", "object", "embed"}:
            source = values.get("src") or values.get("href") or values.get("data")
            if source and not source.startswith(("data:", "blob:")):
                self.external_elements.append(f"{tag}:{source}")
        if values.get("style"):
            self.inline_styles.append(values["style"] or "")
        for key, value in attrs:
            if value and key != "style" and not key.startswith("on") and re.search(r"(?i)url\s*\(", value):
                self.inline_styles.append(value)
        if tag == "style":
            self.in_style = True
        if tag == "meta" and (values.get("http-equiv") or "").lower() == "refresh":
            self.nonself_attributes.append(f"meta[refresh]={values.get('content', '')}")
        if tag == "base":
            self.nonself_attributes.append(f"base[href]={values.get('href', '')}")

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.inline_styles.append(data)


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid {path.name}: {exc}")
        return None


def bundle_entries(value: Any, errors: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema_version") != "4.0" or not isinstance(value.get("sources"), list):
        errors.append('source_bundle.json must be schema_version "4.0" with a sources array')
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(value["sources"]):
        if not isinstance(entry, dict):
            errors.append(f"bundle source[{index}] is not an object")
            continue
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in entries:
            errors.append(f"bundle source[{index}] has missing/duplicate source_id")
            continue
        entries[source_id] = entry
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    case_dir, output_dir = args.case_dir.resolve(), args.output_dir.resolve()
    errors: list[str] = []
    claims = load_json(output_dir / "claims_and_citations.json", errors)
    bundle = load_json(output_dir / "source_bundle.json", errors)
    manifest = load_json(output_dir / "review_manifest.json", errors)
    review_path = output_dir / "review.html"
    try:
        review = review_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"invalid review.html: {exc}")
        review = ""

    claim_map: dict[str, dict[str, Any]] = {}
    evidence_map: dict[str, dict[str, Any]] = {}
    if isinstance(claims, dict) and isinstance(claims.get("claims"), list):
        for claim in claims["claims"]:
            if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
                continue
            claim_map[claim["claim_id"]] = claim
            for bucket in ("supporting_evidence", "contradicting_evidence"):
                for evidence in claim.get(bucket, []) if isinstance(claim.get(bucket), list) else []:
                    if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str):
                        evidence_map[evidence["evidence_id"]] = {**evidence, "claim_id": claim["claim_id"]}

    entries = bundle_entries(bundle, errors)
    assets_dir = case_dir / "assets"
    assets = {
        path.relative_to(assets_dir).as_posix(): path
        for path in assets_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    cited_sources = {evidence.get("source") for evidence in evidence_map.values() if isinstance(evidence.get("source"), str)}
    if set(entries) != cited_sources:
        errors.append(f"bundle sources must exactly equal cited sources; bundled={sorted(entries)}, cited={sorted(cited_sources)}")
    for source_id, entry in entries.items():
        asset = assets.get(source_id)
        if asset is None:
            errors.append(f"bundle source {source_id!r} is not an active-case asset")
            continue
        required_entry = {"source_id", "mime_type", "sha256", "encoding", "bytes_base64"}
        missing_entry = sorted(required_entry - entry.keys())
        if missing_entry:
            errors.append(f"bundle source {source_id!r} lacks required fields: {', '.join(missing_entry)}")
            continue
        expected_mime = {
            ".html": "text/html",
            ".csv": "text/csv",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(asset.suffix.lower())
        if entry.get("mime_type") != expected_mime:
            errors.append(f"bundle source {source_id!r} has incorrect MIME type")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"bundle source {source_id!r} has invalid SHA-256")
            continue
        if entry.get("encoding") != "base64" or not isinstance(entry.get("bytes_base64"), str):
            errors.append(f"bundle source {source_id!r} lacks exact base64 bytes")
            continue
        try:
            decoded = base64.b64decode(entry["bytes_base64"], validate=True)
        except (ValueError, UnicodeError) as exc:
            errors.append(f"bundle source {source_id!r} base64 failure: {exc}")
            continue
        original = asset.read_bytes()
        expected = hashlib.sha256(original).hexdigest()
        if decoded != original:
            errors.append(f"bundle source {source_id!r} does not preserve exact asset bytes")
        if digest != expected:
            errors.append(f"bundle source {source_id!r} SHA-256 mismatch")
        if entry["bytes_base64"] not in review:
            errors.append(f"review.html does not embed exact bundled bytes for {source_id!r}")

    if not isinstance(manifest, dict) or manifest.get("schema_version") != "4.0":
        errors.append('review_manifest.json must have schema_version "4.0"')
        claim_targets = evidence_targets = {}
    else:
        claim_targets = manifest.get("claim_targets")
        evidence_targets = manifest.get("evidence_targets")
        if not isinstance(claim_targets, dict) or not isinstance(evidence_targets, dict):
            errors.append("manifest requires claim_targets and evidence_targets objects")
            claim_targets = evidence_targets = {}
    if set(claim_targets) != set(claim_map):
        errors.append("manifest claim IDs do not exactly equal claims JSON claim IDs")
    if set(evidence_targets) != set(evidence_map):
        errors.append("manifest evidence IDs do not exactly equal claims JSON evidence IDs")

    manifest_dom_ids: list[str] = []
    for target in claim_targets.values():
        if isinstance(target, dict) and isinstance(target.get("dom_id"), str):
            manifest_dom_ids.append(target["dom_id"])
    for target in evidence_targets.values():
        if not isinstance(target, dict):
            continue
        for key in ("dom_id", "source_dom_id"):
            if isinstance(target.get(key), str):
                manifest_dom_ids.append(target[key])
    reused_manifest_dom_ids = sorted({item for item in manifest_dom_ids if manifest_dom_ids.count(item) > 1})
    if reused_manifest_dom_ids:
        errors.append("manifest DOM target IDs must be globally unique: " + ", ".join(reused_manifest_dom_ids[:10]))

    for claim_id, claim in claim_map.items():
        target = claim_targets.get(claim_id)
        if not isinstance(target, dict):
            continue
        expected_evidence = [
            evidence["evidence_id"]
            for bucket in ("supporting_evidence", "contradicting_evidence")
            for evidence in claim.get(bucket, [])
            if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str)
        ]
        if target.get("status") != claim.get("status") or target.get("confidence") != claim.get("confidence"):
            errors.append(f"claim target {claim_id!r} status/confidence differs from claims JSON")
        if target.get("evidence_ids") != expected_evidence:
            errors.append(f"claim target {claim_id!r} evidence_ids are not exact/in-order")
        if not isinstance(target.get("dom_id"), str) or not target["dom_id"]:
            errors.append(f"claim target {claim_id!r} lacks dom_id")

    for evidence_id, evidence in evidence_map.items():
        target = evidence_targets.get(evidence_id)
        if not isinstance(target, dict):
            continue
        required = {"dom_id", "source_dom_id", "source_id", "source_sha256", "relation", "claim_ids", "native_locator"}
        missing = sorted(required - target.keys())
        if missing:
            errors.append(f"evidence target {evidence_id!r} missing {', '.join(missing)}")
            continue
        if target.get("source_id") != evidence.get("source"):
            errors.append(f"evidence target {evidence_id!r} source differs from claims JSON")
        entry = entries.get(target.get("source_id"))
        if entry is None or target.get("source_sha256") != entry.get("sha256"):
            errors.append(f"evidence target {evidence_id!r} source hash mismatch")
        if target.get("relation") != evidence.get("relation"):
            errors.append(f"evidence target {evidence_id!r} relation differs from claims JSON")
        if target.get("claim_ids") != [evidence.get("claim_id")]:
            errors.append(f"evidence target {evidence_id!r} reciprocal claim_ids are not exact")
        if target.get("native_locator") != evidence.get("locator"):
            errors.append(f"evidence target {evidence_id!r} native locator differs from claims JSON")
        for key in ("dom_id", "source_dom_id"):
            if not isinstance(target.get(key), str) or not target[key]:
                errors.append(f"evidence target {evidence_id!r} lacks {key}")

    collector = IdCollector()
    try:
        collector.feed(review)
    except Exception as exc:
        errors.append(f"review.html parser failure: {exc}")
    duplicates = sorted({item for item in collector.ids if collector.ids.count(item) > 1})
    if duplicates:
        errors.append(f"review.html has duplicate DOM IDs: {', '.join(duplicates[:10])}")
    dom_ids = set(collector.ids)
    for claim_id, target in claim_targets.items():
        if isinstance(target, dict) and target.get("dom_id") not in dom_ids:
            errors.append(f"review.html lacks claim DOM target for {claim_id!r}")
    for evidence_id, target in evidence_targets.items():
        if not isinstance(target, dict):
            continue
        for key in ("dom_id", "source_dom_id"):
            if target.get(key) not in dom_ids:
                errors.append(f"review.html lacks {key} for evidence {evidence_id!r}")
        source_dom_id = target.get("source_dom_id")
        attributes = collector.attributes_by_id.get(source_dom_id, {}) if isinstance(source_dom_id, str) else {}
        if attributes.get("data-source-id") != target.get("source_id"):
            errors.append(f"review.html source target {source_dom_id!r} has wrong data-source-id")
        if attributes.get("data-source-sha256") != target.get("source_sha256"):
            errors.append(f"review.html source target {source_dom_id!r} has wrong data-source-sha256")
        native_text = attributes.get("data-native-locator")
        try:
            native_value = json.loads(native_text) if isinstance(native_text, str) else None
        except json.JSONDecodeError:
            native_value = None
        if native_value != target.get("native_locator"):
            errors.append(f"review.html source target {source_dom_id!r} has wrong data-native-locator")
    if collector.nonself_attributes or collector.external_elements:
        examples = (collector.nonself_attributes + collector.external_elements)[:5]
        errors.append("review.html contains non-self-contained element dependencies: " + ", ".join(examples))
    # Only CSS is scanned: <style> text, style attributes and url() attribute values. Script text such as
    # URL.createObjectURL(blob) is not a stylesheet reference; requests made at run time are blocked and
    # reported by validate_viewer_interaction.mjs.
    css_text = "\n".join(collector.inline_styles)
    for match in re.finditer(r"(?i)(?:url\s*\(|@import\s+)([^)\s;]+)", css_text):
        reference = match.group(1).strip("\"'")
        if not reference.startswith(("data:", "blob:", "#")):
            errors.append(f"review.html contains non-self-contained CSS reference {reference!r}")
            break
    if re.search(r"(?i)(?:https?|wss?|file):/{1,3}", review):
        errors.append("review.html contains a literal remote or file URL")
    for token in ("window.__reviewTestApi", "selectClaim", "selectEvidence", "getState"):
        if token not in review:
            errors.append(f"review.html does not expose {token}")
    for dangerous in ("fetch\\s*\\(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts"):
        if re.search(dangerous, review, re.I):
            errors.append(f"review.html contains prohibited network-capable token {dangerous}")

    result = {
        "valid": not errors,
        "errors": errors,
        "bundle_sources": sorted(entries),
        "claim_targets": len(claim_targets),
        "evidence_targets": len(evidence_targets),
        "browser_interaction_required": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
