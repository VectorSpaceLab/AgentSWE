#!/usr/bin/env python3
"""Construction-time consistency, parser, provenance, and leakage audit."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path

from case_truth import build_truth


ROOT = Path(__file__).resolve().parents[1]


class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.refs=[]
    def handle_starttag(self, tag, attrs):
        data=dict(attrs)
        for key in ("href","src"):
            if key in data: self.refs.append(data[key])


def png_chunks(path: Path):
    data=path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), f"bad PNG signature: {path}"
    pos=8; chunks=[]; width=height=0
    while pos+12<=len(data):
        n=struct.unpack(">I",data[pos:pos+4])[0]; kind=data[pos+4:pos+8]; payload=data[pos+8:pos+8+n]
        crc=struct.unpack(">I",data[pos+8+n:pos+12+n])[0]
        assert (zlib.crc32(kind+payload)&0xffffffff)==crc, f"bad PNG CRC: {path}"
        chunks.append(kind.decode());
        if kind==b"IHDR": width,height=struct.unpack(">II",payload[:8])
        pos += 12+n
        if kind==b"IEND": break
    assert width>=300 and height>=150, f"undersized raster: {path}"
    return chunks


def main() -> int:
    errors=[]; checks=[]
    def require(cond, message):
        if cond: checks.append(message)
        else: errors.append(message)
    inputs=sorted((ROOT/"input").glob("*.md"))
    require([p.name for p in inputs]==["01_task_goal.md","02_interface_and_delivery.md","03_requirements_and_constraints.md","04_resources.md"],"exactly four builder input documents")
    devs=sorted(p for p in (ROOT/"dev_cases").iterdir() if p.is_dir()); tests=sorted(p for p in (ROOT/"test_cases").iterdir() if p.is_dir())
    require(len(devs)==2,"exactly two development cases"); require(len(tests)==6,"exactly six hidden cases")
    oracle=json.loads((ROOT/"evaluator/oracles.json").read_text())["cases"]
    require(set(oracle)=={p.name for p in devs+tests},"oracle case ids match runtime case ids")
    corrupt=(ROOT/"test_cases/test_005/assets/recovery/primary/shard-b.json").resolve()
    for case in devs+tests:
        files=[p for p in case.rglob("*") if p.is_file()]
        require((case/"input.md").is_file(),f"{case.name}: input exists")
        forbidden=[p for p in files if re.search(r"oracle|answer|expected|rubric|judge|score",p.name,re.I)]
        require(not forbidden,f"{case.name}: runtime tree has no oracle/scoring files")
        text=(case/"input.md").read_text()
        fences=re.findall(r"```json\s*(\{.*?\})\s*```",text,re.S)
        require(len(fences)==1,f"{case.name}: exactly one JSON Schema fence")
        if fences:
            try: parsed=json.loads(fences[0]); require(parsed==oracle[case.name]["schema"],f"{case.name}: prompt schema matches oracle")
            except Exception: errors.append(f"{case.name}: invalid fenced schema")
        require(str(len(oracle[case.name]["records"])) in text,f"{case.name}: expected entity count stated in request")
        for p in files:
            if p.suffix==".json" and p.resolve()!=corrupt:
                try: json.loads(p.read_text())
                except Exception as exc: errors.append(f"invalid JSON {p.relative_to(ROOT)}: {exc}")
            elif p.suffix==".ndjson":
                try:
                    for line in p.read_text().splitlines(): json.loads(line)
                except Exception as exc: errors.append(f"invalid NDJSON {p.relative_to(ROOT)}: {exc}")
            elif p.suffix in {".html",".htm"}:
                try:
                    parser=Parser(); parser.feed(p.read_text())
                    for raw in parser.refs:
                        if raw.startswith(("http://","https://","data:","javascript:","#")): continue
                        target=(p.parent/raw.split("?",1)[0].split("#",1)[0]).resolve()
                        require(target.is_file(),f"{p.relative_to(ROOT)}: local HTML reference resolves: {raw}")
                except Exception as exc: errors.append(f"invalid HTML parse {p.relative_to(ROOT)}: {exc}")
            elif p.suffix==".png":
                try:
                    chunks=png_chunks(p); require(not ({"tEXt","zTXt","iTXt"}&set(chunks)),f"{p.relative_to(ROOT)}: PNG has no text metadata")
                except Exception as exc: errors.append(str(exc))
    # Intentionally corrupt recovery resource must fail ordinary parsing and have a valid declared fallback hash.
    try: json.loads(corrupt.read_text()); errors.append("recovery shard unexpectedly parses")
    except json.JSONDecodeError: checks.append("recovery shard is genuinely malformed JSON")
    manifest=json.loads((ROOT/"test_cases/test_005/assets/recovery/manifest.json").read_text())
    route=manifest["shards"][1]["on_parse_or_checksum_failure"]; fallback=ROOT/"test_cases/test_005/assets/recovery"/route["snapshot"]
    require(hashlib.sha256(fallback.read_bytes()).hexdigest()==route["sha256"],"recovery fallback checksum matches manifest")
    require(corrupt.stat().st_size>700,"degraded primary resource is substantial, not metadata-only")
    # Public/hidden byte-hash separation.
    dev_hashes={hashlib.sha256(p.read_bytes()).hexdigest():p for c in devs for p in (c/"assets").rglob("*") if p.is_file()}
    collisions=[]
    for c in tests:
        for p in (c/"assets").rglob("*"):
            if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() in dev_hashes: collisions.append((dev_hashes[hashlib.sha256(p.read_bytes()).hexdigest()],p))
    require(not collisions,"no hidden runtime asset is byte-identical to a development asset")
    # Hidden identities must not appear in public Builder material or development cases.
    public_blob=b"\n".join(p.read_bytes() for p in inputs+[q for c in devs for q in c.rglob("*") if q.is_file()])
    leaked=[]
    for c in tests:
        ident=oracle[c.name]["identity_field"]
        for row in oracle[c.name]["records"]:
            if row[ident].encode() in public_blob: leaked.append(row[ident])
    require(not leaked,"hidden record identities do not leak into public Builder material")
    public_text="\n".join(p.read_text(errors="ignore") for p in inputs+[c/"input.md" for c in devs])
    require(not re.search(r"\btest_00[1-6]\b|ScrapeGraphAI|Scrapegraph-ai",public_text,re.I),"Builder-facing documents do not expose hidden case ids or source-repository identity")
    # Runtime raster-only truths must not occur in text, metadata, or filenames
    # elsewhere in the same active case.  Case-scoping avoids irrelevant numeric
    # coincidences in another synthetic corpus while enforcing the actual leak boundary.
    leaks=[]
    for case_id, spec in oracle.items():
        case = ROOT/("dev_cases" if case_id.startswith("dev_") else "test_cases")/case_id
        case_blob_parts=[]
        for p in case.rglob("*"):
            if p.is_file() and p.suffix!=".png":
                case_blob_parts.append(p.name)
                try: case_blob_parts.append(p.read_text(errors="ignore"))
                except Exception: pass
        blob="\n".join(case_blob_parts)
        by_id={r[spec["identity_field"]]:r for r in spec["records"]}
        for rule in spec["ocr_fields"]:
            value=by_id[rule["record_id"]][rule["field"].lstrip("/")]
            if str(value) in blob: leaks.append(f"{rule['record_id']}{rule['field']}")
    require(not leaks,"OCR-scored values have no duplicated runtime text truth")
    # Source-grounded evaluator truth and feasibility.
    for case in devs+tests:
        spec=oracle[case.name]
        try: truth=build_truth(case,spec)
        except Exception as exc:
            errors.append(f"{case.name}: evaluator truth construction failed: {type(exc).__name__}: {exc}"); continue
        expected_fields={(r[spec["identity_field"]],"/"+field) for r in spec["records"] for field in r}
        truth_fields={(rid,field) for rid,fields in truth["fields"].items() for field in fields}
        require(truth_fields==expected_fields,f"{case.name}: every expected record field has evaluator source truth")
        for rid,fields in truth["fields"].items():
            for field,rules in fields.items():
                require(bool(rules),f"{case.name}: {rid}{field} has at least one accepted source rule")
                for item in rules: require((case/item["path"]).is_file(),f"{case.name}: accepted field source exists: {item['path']}")
        for conflict in truth["conflicts"]:
            require(len(conflict["observations"])>=2,f"{case.name}: conflict {conflict['record_id']}{conflict['field']} has competing observations")
            require(any(not x["value"]==conflict["selected"] for x in conflict["observations"]),f"{case.name}: conflict {conflict['record_id']}{conflict['field']} contains a losing value")
            for item in conflict["observations"]: require((case/item["path"]).is_file(),f"{case.name}: conflict source exists: {item['path']}")
        derived={}
        for step in truth["state_steps"]: derived[step["action"]]=derived.get(step["action"],0)+1
        for action,targets in truth["targets"].items(): derived[action]=derived.get(action,0)+len(targets)
        require(derived==spec["workflow"]["required_action_counts"],f"{case.name}: oracle action minima equal source-grounded truth")
        require(truth["required_states"]==spec["workflow"]["required_states"],f"{case.name}: oracle state order equals source-grounded truth")
        request=(case/"input.md").read_text()
        require(str(spec["workflow"]["max_local_reads"]) in request,f"{case.name}: local-work budget is public in the request")
        if truth["exclusions"]:
            blob="\n".join(p.read_text(errors="ignore") for p in (case/"assets").rglob("*") if p.is_file() and p.suffix!=".png")
            for token in truth["exclusions"]: require(token in blob,f"{case.name}: required exclusion identity is grounded in runtime sources: {token}")

    # Feasibility: runtime file cardinality and required browser interactions fit
    # the declared per-case local-work envelope with ample room for validation.
    for case in devs+tests:
        spec=oracle[case.name]; asset_count=sum(1 for p in (case/"assets").rglob("*") if p.is_file())
        require(asset_count<=spec["workflow"]["max_local_reads"],f"{case.name}: asset corpus fits declared local-read envelope")
        if spec["workflow"]["browser"]:
            actions=spec["workflow"]["required_action_counts"]
            navigations=sum(actions.get(k,0) for k in ("open","set_mode","load_more","visit_detail"))
            require(navigations<=spec["workflow"]["max_local_reads"],f"{case.name}: required browser interactions fit declared action envelope")
            pagination_states=1+actions.get("load_more",0)
            require(3<=pagination_states<=5,f"{case.name}: pagination/load-more state count is within 3-5")
            detail_files=[p for folder in (case/"assets").rglob("*") if folder.is_dir() and folder.name in {"details","profiles"} for p in folder.iterdir() if p.is_file()]
            signatures=set()
            for p in detail_files:
                if p.suffix.lower() in {".json",".vcf"}: signatures.add(p.suffix.lower())
                elif p.suffix.lower()==".html":
                    body=p.read_text(errors="ignore")
                    marker=next((m for m in ("<table","itemscope","role=\"group\"","<dl","<article","<main") if m in body),"html-other")
                    signatures.add("html:"+marker)
            require(len(signatures)>=3,f"{case.name}: at least three detail/profile templates or formats")
    # JavaScript syntax checks when Node is available.
    node=shutil.which("node")
    if node:
        for p in [p for c in devs+tests for p in c.rglob("*.js")]:
            proc=subprocess.run([node,"--check",str(p)],capture_output=True,text=True)
            if proc.returncode: errors.append(f"javascript syntax {p.relative_to(ROOT)}: {proc.stderr.strip()}")
        checks.append("all JavaScript assets pass node --check")
    else: checks.append("node unavailable; JavaScript syntax check skipped")
    # Rubric sums and hygiene.
    rubric=(ROOT/"evaluator/rubric.md").read_text(); weights=[int(x) for x in re.findall(r"^## \d+\..*?— (\d+) points$",rubric,re.M)]
    require(sum(weights)==100 and 4<=len(weights)<=7,"final-artifact rubric has 4-7 dimensions totaling 100")
    code=(ROOT/"evaluator/code_rubric.md").read_text(); code_weights=[int(x) for x in re.findall(r"^### \d+\..*?— (\d+) points$",code,re.M)]
    require(len(code_weights)==8 and sum(code_weights)==100,"code rubric has eight dimensions totaling 100")
    code_ids={"interface_lifecycle":15,"requirement_mechanism_coverage":20,"analysis_evidence_integrity":15,"safety_privacy_side_effects":15,"recovery_honest_failure":10,"testability_observability":10,"maintainability_generalization":10,"resource_discipline":5}
    for key,maximum in code_ids.items(): require(re.search(rf'"{re.escape(key)}"\s*:\s*\{{[^}}]*"max"\s*:\s*{maximum}',code) is not None,f"code rubric shared id/max present: {key}/{maximum}")
    obsolete_ids=["interface_"+"artifact_lifecycle","request_"+"workflow_mechanisms","validation_"+"evidence_integrity","authorization_"+"privacy_safety","recovery_"+"truthful_failure"]
    require(not any(key in code for key in obsolete_ids),"code rubric contains no obsolete dimension ids")
    all_files=[p for p in ROOT.rglob("*") if p.is_file()]
    require(not [p for p in all_files if p.suffix==".pyc" or "__pycache__" in p.parts],"no generated bytecode in benchmark tree")
    debris=[p for p in all_files if p.name in {"REPAIR_NOTES.md"} or "CHANGELOG" in p.parts or "meta/tools" in p.as_posix() or ".construction-tmp" in p.parts]
    require(not debris,"no construction notes, generators, or temporary validation outputs remain")
    stale=[]
    for p in all_files:
        if p.resolve() == Path(__file__).resolve():
            continue
        predecessor_slug="schema-guided-web-extraction-agent-hard-v"+"3"
        predecessor_label=r"\bv"+"3"+r"\b"
        if p.suffix.lower() in {".md",".py",".json"} and re.search(re.escape(predecessor_slug)+"|"+predecessor_label,p.read_text(errors="ignore"),re.I): stale.append(p)
    require(not stale,"no stale predecessor paths or version references remain")
    marker_hits=[]
    for p in all_files:
        if p.resolve() == Path(__file__).resolve():
            continue
        if p.suffix.lower() in {".md",".py",".json",".js",".html",".vcf",".ndjson"}:
            if re.search(r"\b(TODO|TBD|FIXME)\b",p.read_text(errors="ignore")): marker_hits.append(p)
    require(not marker_hits,"no unfinished-work markers")
    result={"valid":not errors,"checks_passed":len(checks),"errors":errors,"case_counts":{k:len(v["records"]) for k,v in oracle.items()},"runtime_files":sum(1 for c in devs+tests for p in c.rglob("*") if p.is_file())}
    print(json.dumps(result,indent=2))
    return 0 if not errors else 2


if __name__=="__main__": raise SystemExit(main())
