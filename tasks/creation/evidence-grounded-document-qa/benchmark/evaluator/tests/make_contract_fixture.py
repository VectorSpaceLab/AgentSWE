#!/usr/bin/env python3
"""Create a minimal dev-only output package for validator regression tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from html import escape
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    case = args.case_dir.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = case / "assets" / "commissioning_report.html"
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    match = re.search(r'<p id="scope">.*?</p>', text, re.S)
    if not match:
        raise SystemExit("dev fixture source element not found")
    start, end = match.span()
    observation = re.sub(r"<[^>]+>", " ", match.group(0)).strip()
    digest = hashlib.sha256(raw).hexdigest()
    locator = {"kind": "html_element", "element_id": "scope", "byte_start": start, "byte_end": end}
    claim = {
        "claim_id": "fixture-claim",
        "claim": "The selected passage defines the report scope.",
        "status": "supported",
        "confidence": "high",
        "supporting_evidence": [
            {
                "evidence_id": "fixture-evidence",
                "source": "commissioning_report.html",
                "locator": locator,
                "quote_or_observation": observation,
                "relation": "supports",
            }
        ],
        "contradicting_evidence": [],
    }
    claims = {"schema_version": "4.0", "claims": [claim]}
    bundle = {
        "schema_version": "4.0",
        "sources": [
            {
                "source_id": "commissioning_report.html",
                "mime_type": "text/html",
                "sha256": digest,
                "encoding": "base64",
                "bytes_base64": base64.b64encode(raw).decode("ascii"),
            }
        ],
    }
    manifest = {
        "schema_version": "4.0",
        "claim_targets": {
            "fixture-claim": {
                "dom_id": "claim-fixture-claim",
                "status": "supported",
                "confidence": "high",
                "evidence_ids": ["fixture-evidence"],
            }
        },
        "evidence_targets": {
            "fixture-evidence": {
                "dom_id": "evidence-fixture-evidence",
                "source_dom_id": "source-fixture-evidence",
                "source_id": "commissioning_report.html",
                "source_sha256": digest,
                "relation": "supports",
                "claim_ids": ["fixture-claim"],
                "native_locator": locator,
            }
        },
    }
    embedded = json.dumps(bundle).replace("</", "<\\/")
    locator_text = escape(json.dumps(locator, sort_keys=True, separators=(",", ":")))
    observation_text = escape(observation)
    source_data_uri = f"data:text/html;base64,{bundle['sources'][0]['bytes_base64']}"
    review = f"""<!doctype html><html><head><style>[aria-selected="true"] {{background:#d9f1e3;outline:2px solid #22794f;}}</style></head><body>
    <button id="claim-fixture-claim" aria-selected="false">claim</button>
    <button id="evidence-fixture-evidence" aria-selected="false">evidence</button>
    <section id="source-fixture-evidence" aria-selected="false" data-source-id="commissioning_report.html" data-source-sha256="{digest}" data-native-locator="{locator_text}">
      <p>commissioning_report.html</p><p>{digest}</p>
      <pre>{locator_text}</pre><blockquote>{observation_text}</blockquote>
      <a data-source-download="true" download="commissioning_report.html" href="{source_data_uri}">open exact bundled source bytes</a>
    </section>
    <script type="application/json" id="source-bundle">{embedded}</script>
    <script>
    const manifest={json.dumps(manifest)};
    let state={{selected_claim_id:null,selected_evidence_id:null,highlighted_claim_ids:[],highlighted_evidence_ids:[],status:null,confidence:null,relation:null,source_id:null,source_sha256:null,native_locator:null,url_fragment:""}};
    function mark(id,on){{const el=document.getElementById(id);if(el){{el.setAttribute('aria-selected',on?'true':'false');el.classList.toggle('is-selected',on);}}}}
    function choose(claimId,evidenceId){{const ct=manifest.claim_targets[claimId],et=manifest.evidence_targets[evidenceId];document.querySelectorAll('[aria-selected]').forEach(el=>mark(el.id,false));mark(ct.dom_id,true);mark(et.dom_id,true);mark(et.source_dom_id,true);location.hash=`claim=${{encodeURIComponent(claimId)}}&evidence=${{encodeURIComponent(evidenceId)}}`;state={{selected_claim_id:claimId,selected_evidence_id:evidenceId,highlighted_claim_ids:[claimId],highlighted_evidence_ids:[...ct.evidence_ids],status:ct.status,confidence:ct.confidence,relation:et.relation,source_id:et.source_id,source_sha256:et.source_sha256,native_locator:et.native_locator,url_fragment:location.hash}};return state;}}
window.__reviewTestApi={{selectClaim:(id)=>choose(id,manifest.claim_targets[id].evidence_ids[0]),selectEvidence:(id)=>choose(manifest.evidence_targets[id].claim_ids[0],id),getState:()=>state}};
</script></body></html>"""
    (output / "answer.md").write_text("# Fixture answer\n\nThe selected passage defines the report scope.\n", encoding="utf-8")
    (output / "claims_and_citations.json").write_text(json.dumps(claims, indent=2) + "\n", encoding="utf-8")
    (output / "source_bundle.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    (output / "review_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "review.html").write_text(review, encoding="utf-8")
    report = {
        "status": "ok",
        "artifact_paths": ["answer.md", "claims_and_citations.json", "review.html", "review_manifest.json", "source_bundle.json", "run_report.json"],
        "errors": [],
        "llm_requests": 0,
        "gateway_image_requests": 0,
        "serper_requests": 0,
        "web_retrieval": {},
        "elapsed_seconds": 0.01,
    }
    (output / "run_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
