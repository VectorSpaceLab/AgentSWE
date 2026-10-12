#!/usr/bin/env python3
"""Derive evaluator-only provenance and workflow truth from the active case assets.

This module intentionally keeps candidate-chosen IDs and locator syntax out of the
oracle.  It describes observable source paths, record/field decisions, action
targets, exclusions, and state order that are already required by each input.md.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def rel(case_dir: Path, path: Path | str) -> str:
    p = path if isinstance(path, Path) else case_dir / path
    return p.relative_to(case_dir).as_posix()


def rule(path: str, action: str, method: str | None = None, target: str | None = None) -> dict:
    suffix = Path(path).suffix.lower()
    if method is None:
        method = "vcard" if suffix == ".vcf" else "json" if suffix in {".json", ".ndjson"} else "ocr" if suffix == ".png" else "dom"
    return {"path": path, "action": action, "method": method, "target": target or path}


def observation(path: str, value, **metadata) -> dict:
    out = {"path": path, "value": value}
    out.update({k: v for k, v in metadata.items() if v is not None})
    return out


def profile(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        row = json.loads(text)
        return {"id": row["registry_id"], "name": row["canonical_name"], "city": row["city"], "affiliation": row["affiliation"], "aliases": row["aliases"], "updated": row["updated_at"]}
    if path.suffix == ".vcf":
        data = {}
        for line in text.splitlines():
            if ":" in line:
                key, value = line.split(":", 1); data[key] = value
        return {"id": data["UID"], "name": data["FN"], "city": data["ADR"].split(";")[-1], "affiliation": data["ORG"], "aliases": [x for x in data.get("NICKNAME", "").split(",") if x], "updated": data["REV"]}
    def one(pattern: str) -> str:
        match = re.search(pattern, text, re.S)
        if not match: raise ValueError(f"profile parse failed: {path}: {pattern}")
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()
    id_patterns = [r'data-registry(?:-id)?="([^"]+)"', r'name="registry-id" content="([^"]+)"', r'itemprop="identifier" value="([^"]+)"']
    pid = next((m.group(1) for pat in id_patterns if (m := re.search(pat, text, re.S))), None)
    if not pid: raise ValueError(f"profile id parse failed: {path}")
    name = one(r"<h[12]>(.*?)</h[12]>") if re.search(r"<h[12]>", text) else one(r'itemprop="name">(.*?)</')
    if 'class="city"' in text: city = one(r'class="city">(.*?)</')
    elif "data-city=" in text: city = one(r'data-city="([^"]+)"')
    elif "<dt>City</dt>" in text: city = one(r"<dt>City</dt><dd>(.*?)</dd>")
    else: city = one(r'itemprop="homeLocation">(.*?)</')
    if re.search(r'class="(?:org|affiliation)"', text): affiliation = one(r'class="(?:org|affiliation)">(.*?)</')
    elif "<dt>Affiliation</dt>" in text: affiliation = one(r"<dt>Affiliation</dt><dd>(.*?)</dd>")
    else: affiliation = one(r'itemprop="affiliation">(.*?)</')
    aliases = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<li(?: data-alias)?>(.*?)</li>", text, re.S)]
    if not aliases and "<dt>Aliases</dt>" in text: aliases = [x.strip() for x in one(r"<dt>Aliases</dt><dd>(.*?)</dd>").split("|")]
    if not aliases and 'data-kind="aliases"' in text: aliases = json.loads(one(r'data-kind="aliases">(.*?)</script>'))
    return {"id": pid, "name": name, "city": city, "affiliation": affiliation, "aliases": aliases, "updated": one(r'data-updated="([^"]+)"') if "data-updated" in text else "9999-12-31T23:59:59Z"}


def html_cards(text: str, pattern: str, keys: list[str]) -> list[dict]:
    return [dict(zip(keys, match)) for match in re.findall(pattern, text, re.S)]


def state_contract(steps: list[tuple[str, str, str, bool]]) -> list[dict]:
    return [{"action": action, "target": target, "state": state, "screenshot": screenshot} for action, target, state, screenshot in steps]


def dev_001(case: Path, spec: dict) -> dict:
    site = case / "assets/site"
    cards = []
    for row in html_cards((site / "index.html").read_text(), r'<article class="instrument" data-id="([^"]+)">.*?<h2>(.*?)</h2><a href="([^"]+)"', ["id", "name", "detail"]):
        row["source"] = "assets/site/index.html"; cards.append(row)
    for n in range(2, 5):
        path = f"assets/site/pages/page-{n}.json"
        for row in load_json(case / path): cards.append({"id": row["asset_id"], "name": row["name"], "detail": row["detail"], "source": path})
    detail = {x["id"]: "assets/site/" + x["detail"] for x in cards}
    official_path = "assets/site/feeds/official-prices.json"; reseller_path = "assets/site/feeds/reseller-prices.json"
    official = defaultdict(list); reseller = defaultdict(list)
    for row in load_json(case / official_path): official[row["asset_id"]].append(row)
    for row in load_json(case / reseller_path): reseller[row["asset_id"]].append(row)
    inventory_paths = ["assets/site/inventory/inventory-east.json", "assets/site/inventory/inventory-west.json"]
    inventory = defaultdict(list)
    for path in inventory_paths:
        for row in load_json(case / path): inventory[row["asset_id"]].append((path, row))
    fields = {}; conflicts = []
    for row in spec["records"]:
        rid = row["asset_id"]; dpath = detail[rid]
        fields[rid] = {f"/{f}": [rule(dpath, "visit_detail")] for f in ("asset_id", "name", "material", "mass_g")}
        active = [x for x in official[rid] if x["status"] == "active"]
        if active:
            chosen = max(active, key=lambda x: x["observed_at"]); price_path = official_path
            obs = [observation(official_path, x["price"], observed_at=x["observed_at"], status=x["status"]) for x in official[rid]]
            obs += [observation(reseller_path, x["price"], observed_at=x["observed_at"], validity="verified" if x["verified"] else "unverified") for x in reseller[rid]]
            conflicts.append({"record_id": rid, "field": "/price_usd", "selected": chosen["price"], "observations": obs})
        else:
            chosen = max(reseller[rid], key=lambda x: x["observed_at"]); price_path = reseller_path
        fields[rid]["/price_usd"] = [rule(price_path, "read_local")]
        inv = max(inventory[rid], key=lambda x: x[1]["observed_at"]); inv_path, inv_row = inv
        fields[rid]["/stock"] = [rule(inv_path, "read_local")]; fields[rid]["/warehouse"] = [rule(inv_path, "read_local")]
        if len(inventory[rid]) > 1:
            for field, out_field in (("stock", "/stock"), ("warehouse", "/warehouse")):
                conflicts.append({"record_id": rid, "field": out_field, "selected": inv_row[field], "observations": [observation(p, x[field], observed_at=x["observed_at"]) for p, x in inventory[rid]]})
    return {
        "fields": fields, "conflicts": conflicts, "exclusions": [],
        "state_steps": state_contract([("open", "assets/site/index.html", "catalog:1", True), ("load_more", "assets/site/pages/page-2.json", "catalog:2", True), ("load_more", "assets/site/pages/page-3.json", "catalog:3", True), ("load_more", "assets/site/pages/page-4.json", "catalog:4", True)]),
        "targets": {"visit_detail": sorted(detail.values()), "read_local": [official_path, reseller_path] + inventory_paths},
    }


def dev_002(case: Path, spec: dict) -> dict:
    site = case / "assets/site"; cards = []
    text = (site / "directory.html").read_text()
    pat = r'<div role="listitem"(?: data-person="([^"]+)")? data-as-of="([^"]+)"><b>(.*?)</b><span data-role>(.*?)</span><a href="([^"]+)"'
    for hint, ts, display, role_name, p in re.findall(pat, text, re.S): cards.append({"hint": hint or None, "observed": ts, "display": display, "role": role_name, "profile": p, "source": "assets/site/directory.html"})
    for n in range(2, 5):
        path = f"assets/site/pages/contributors-{n}.json"
        for x in load_json(case / path): cards.append({"hint": x["person_id"], "observed": x["as_of"], "display": x["display"], "role": x["role"], "profile": x["profile"], "source": path})
    profiles = {}
    for card in cards:
        path = "assets/site/" + card["profile"]; info = profile(case / path); card["id"] = info["id"]; profiles[info["id"]] = (path, info)
    by_id = defaultdict(list)
    for card in cards: by_id[card["id"]].append(card)
    fields = {}; conflicts = []
    for row in spec["records"]:
        rid = row["person_id"]; ppath, _ = profiles[rid]; selected = max(by_id[rid], key=lambda x: x["observed"])
        fields[rid] = {f"/{f}": [rule(ppath, "visit_detail")] for f in ("person_id", "canonical_name", "city", "affiliation", "aliases")}
        fields[rid]["/role"] = [rule(selected["source"], "open" if selected["source"].endswith("directory.html") else "load_more")]
        if len(by_id[rid]) > 1:
            conflicts.append({"record_id": rid, "field": "/role", "selected": selected["role"], "observations": [observation(x["source"], x["role"], observed_at=x["observed"]) for x in by_id[rid]]})
    return {
        "fields": fields, "conflicts": conflicts, "exclusions": [],
        "state_steps": state_contract([("open", "assets/site/directory.html", "directory:1", True), ("load_more", "assets/site/pages/contributors-2.json", "directory:2", True), ("load_more", "assets/site/pages/contributors-3.json", "directory:3", True), ("load_more", "assets/site/pages/contributors-4.json", "directory:4", True)]),
        "targets": {"visit_detail": sorted({"assets/site/" + x["profile"] for x in cards})},
    }


def test_001(case: Path, spec: dict) -> dict:
    site = case / "assets/site"; text = (site / "index.html").read_text(); cards = []
    initial = json.loads(re.search(r"window\.INITIAL=(\[.*?\])</script>", text, re.S).group(1))
    for x in initial: cards.append(dict(x, source="assets/site/index.html"))
    for n in range(2, 6):
        path = f"assets/site/pages/lots-{n}.json"
        for x in load_json(case / path): cards.append(dict(x, source=path))
    by_id = defaultdict(list)
    for x in cards: by_id[x["record_ref"]].append(x)
    cert_path = "assets/site/feeds/certified.json"; dist_path = "assets/site/feeds/distributor.json"
    cert = {x["record_ref"]: x for x in load_json(case / cert_path)}; dist = {x["record_ref"]: x for x in load_json(case / dist_path)}
    ocr = {(x["record_id"], x["field"]): "assets/site/" + x["image"] for x in spec["ocr_fields"]}
    fields = {}; conflicts = []
    for row in spec["records"]:
        rid = row["record_ref"]; selected_card = max(by_id[rid], key=lambda x: x["card_revision"]); dpath = "assets/site/" + selected_card["detail"]
        fields[rid] = {f"/{f}": [rule(dpath, "visit_detail")] for f in ("record_ref", "name", "material")}
        chosen_path = cert_path if cert[rid]["signature_status"] == "valid" else dist_path
        fields[rid]["/price_usd"] = [rule(chosen_path, "read_local")]
        for f in ("lot_code", "net_weight_g"): fields[rid]["/" + f] = [rule(ocr[(rid, "/" + f)], "ocr", "ocr")]
        conflicts.append({"record_id": rid, "field": "/price_usd", "selected": row["price_usd"], "observations": [observation(cert_path, cert[rid]["price"], observed_at=cert[rid]["signed_at"], validity=cert[rid]["signature_status"]), observation(dist_path, dist[rid]["price"], observed_at=dist[rid]["observed_at"], validity=dist[rid]["verification"])]})
        if len(by_id[rid]) > 1:
            conflicts.append({"record_id": rid, "field": "/name", "selected": row["name"], "observations": [observation(x["source"], x["display_name"], observed_at=x["card_revision"]) for x in by_id[rid]]})
    state = [("open", "assets/site/index.html", "mode:current", True), ("set_mode", "control:include-retained-inspection-lots", "mode:all/page:1", True)]
    state += [("load_more", f"assets/site/pages/lots-{n}.json", f"mode:all/page:{n}", True) for n in range(2, 6)]
    return {"fields": fields, "conflicts": conflicts, "exclusions": [], "state_steps": state_contract(state), "targets": {"visit_detail": sorted({"assets/site/" + x["detail"] for x in cards}), "read_local": [cert_path, dist_path], "ocr": sorted(set(ocr.values()))}}


def test_002(case: Path, spec: dict) -> dict:
    site = case / "assets/site"; cards = []
    text = (site / "people.html").read_text(); pat = r'<li(?: data-registry-hint="([^"]+)")? data-observed="([^"]+)"><b>(.*?)</b><span>(.*?)</span><a href="([^"]+)"'
    for hint, ts, display, role_name, p in re.findall(pat, text, re.S): cards.append({"hint": hint or None, "observed": ts, "display": display, "role": role_name, "profile": p, "source": "assets/site/people.html"})
    for n in range(2, 5):
        path = f"assets/site/pages/batch-{n}.json"
        for x in load_json(case / path): cards.append({"hint": x["registry_hint"], "observed": x["observed_at"], "display": x["display"], "role": x["role"], "profile": x["profile"], "source": path})
    by_id = defaultdict(list); profiles = defaultdict(list)
    for card in cards:
        path = "assets/site/" + card["profile"]; info = profile(case / path); card["id"] = info["id"]; by_id[info["id"]].append(card); profiles[info["id"]].append((path, info))
    fields = {}; conflicts = []
    for row in spec["records"]:
        rid = row["person_id"]; ppath, pinfo = max(profiles[rid], key=lambda x: x[1]["updated"]); selected_card = max(by_id[rid], key=lambda x: x["observed"])
        fields[rid] = {f"/{f}": [rule(ppath, "visit_detail")] for f in ("person_id", "canonical_name", "city", "affiliation", "aliases")}
        fields[rid]["/role"] = [rule(selected_card["source"], "open" if selected_card["source"].endswith("people.html") else "load_more")]
        if len(profiles[rid]) > 1:
            for field, key in (("/canonical_name", "name"), ("/city", "city")):
                conflicts.append({"record_id": rid, "field": field, "selected": pinfo[key], "observations": [observation(path, info[key], observed_at=info["updated"]) for path, info in profiles[rid]]})
        if len(by_id[rid]) > 1:
            conflicts.append({"record_id": rid, "field": "/role", "selected": selected_card["role"], "observations": [observation(x["source"], x["role"], observed_at=x["observed"]) for x in by_id[rid]]})
    return {"fields": fields, "conflicts": conflicts, "exclusions": [], "state_steps": state_contract([("open", "assets/site/people.html", "batch:1", True), ("load_more", "assets/site/pages/batch-2.json", "batch:2", True), ("load_more", "assets/site/pages/batch-3.json", "batch:3", True), ("load_more", "assets/site/pages/batch-4.json", "batch:4", True)]), "targets": {"visit_detail": sorted({"assets/site/" + x["profile"] for x in cards})}}


def test_003(case: Path, spec: dict) -> dict:
    site = case / "assets/site"; text = (site / "permits.html").read_text(); cards = json.loads(re.search(r"window\.ALL_PAGE_1=(\[.*?\])</script>", text, re.S).group(1))
    for n in range(2, 5): cards += load_json(site / f"pages/ledger-{n}.json")
    detail = {x["filing"]: "assets/site/" + x["detail"] for x in cards}
    amend_path = "assets/site/amendments/ledger.json"; amendments = defaultdict(list)
    for x in load_json(case / amend_path): amendments[x["filing"]].append(x)
    fields = {}; conflicts = []
    for row in spec["records"]:
        rid = row["permit_id"]; dpath = detail[rid]
        fields[rid] = {f"/{f}": [rule(dpath, "visit_detail")] for f in ("permit_id", "operator", "district")}
        if amendments[rid]:
            selected = max(amendments[rid], key=lambda x: x["sequence"]); target = f"{rid}#sequence={selected['sequence']}"
            for out_field, source_field in (("/status", "status"), ("/capacity_mw", "capacity_mw"), ("/filed_at", "filed_at")):
                fields[rid][out_field] = [rule(amend_path, "apply_update", "json", target)]
                conflicts.append({"record_id": rid, "field": out_field, "selected": selected[source_field], "observations": [observation(amend_path, x[source_field], sequence=x["sequence"]) for x in amendments[rid]]})
        else:
            for field in ("status", "capacity_mw", "filed_at"): fields[rid]["/" + field] = [rule(dpath, "visit_detail")]
    ordered_updates = [f"{x['filing']}#sequence={x['sequence']}" for x in sorted((y for rows in amendments.values() for y in rows), key=lambda x: (x["sequence"], x["filing"]))]
    return {"fields": fields, "conflicts": conflicts, "exclusions": [], "state_steps": state_contract([("open", "assets/site/permits.html", "quarter:current", True), ("set_mode", "control:all-filing-periods", "periods:all/page:1", True), ("load_more", "assets/site/pages/ledger-2.json", "periods:all/page:2", True), ("load_more", "assets/site/pages/ledger-3.json", "periods:all/page:3", True), ("load_more", "assets/site/pages/ledger-4.json", "periods:all/page:4", True)]), "targets": {"visit_detail": sorted(detail.values()), "read_local": [amend_path], "apply_update": ordered_updates}, "ordered_targets": {"apply_update": ordered_updates}}


def test_004(case: Path, spec: dict) -> dict:
    paths = [f"assets/data/snapshots/{name}" for name in ("snapshot-alpha.json", "snapshot-beta.json", "snapshot-kappa.json", "snapshot-omega.json", "snapshot-zeta.json")]
    registry_path = "assets/data/identity/registry.json"; crosswalk_path = "assets/data/identity/crosswalk.json"; cert_path = "assets/data/certified-readings.json"
    registry_rows = load_json(case / registry_path); registry = {x["sensor_id"]: x for x in registry_rows}; serial_to_id = {x["serial"]: x["sensor_id"] for x in registry_rows}; crosswalk = load_json(case / crosswalk_path)
    snapshots = defaultdict(list); unknown = []
    for path in paths:
        for row in load_json(case / path):
            rid = row.get("sensor_id") or crosswalk.get(row.get("legacy_id")) or serial_to_id.get(row.get("serial"))
            if rid: snapshots[rid].append((path, row))
            else: unknown.append((path, row))
    certs = {x["sensor_id"]: x for x in load_json(case / cert_path)}
    fields = {}; conflicts = []; retired = []
    expected_ids = {x["sensor_id"] for x in spec["records"]}
    for rid in sorted(registry):
        latest_path, latest = max(snapshots[rid], key=lambda x: x[1]["observed_at"]); cert = certs[rid]
        selected_value = cert["reading"] if cert["quality"] == "valid" else latest["reading"]
        obs = [observation(path, row["reading"], observed_at=row["observed_at"], status=row["status"]) for path, row in snapshots[rid]]
        obs.append(observation(cert_path, cert["reading"], observed_at=cert["observed_at"], validity=cert["quality"]))
        conflicts.append({"record_id": rid, "field": "/reading", "selected": selected_value, "observations": obs})
        if rid not in expected_ids:
            retired.append(rid); continue
        fields[rid] = {
            "/sensor_id": [rule(registry_path, "read_local")], "/serial": [rule(registry_path, "read_local")],
            "/status": [rule(latest_path, "read_local")],
            "/location": [rule(registry_path if registry[rid]["location"] is not None else latest_path, "read_local")],
            "/reading": [rule(cert_path if cert["quality"] == "valid" else latest_path, "read_local")],
            "/observed_at": [rule(cert_path if cert["quality"] == "valid" else latest_path, "read_local")],
        }
    exclusions = retired + [x[1]["serial"] for x in unknown]
    return {"fields": fields, "conflicts": conflicts, "exclusions": sorted(exclusions), "state_steps": [], "required_states": ["snapshots:read", "identity:resolved", "temporal:merged", "certification:applied"], "targets": {"read_local": paths + [crosswalk_path, registry_path, cert_path], "merge": sorted(registry), "exclude": sorted(exclusions)}}


def test_005(case: Path, spec: dict) -> dict:
    root = "assets/recovery/"; manifest_path = root + "manifest.json"; manifest = load_json(case / manifest_path)
    a_path = root + manifest["shards"][0]["path"]; b_path = root + manifest["shards"][1]["path"]; c_path = root + manifest["shards"][2]["path"]
    route = manifest["shards"][1]["on_parse_or_checksum_failure"]; fallback_path = root + route["snapshot"]; journal_path = root + route["journal"]
    patches = [json.loads(x) for x in (case / journal_path).read_text().splitlines() if x.strip()]; patches_by_id = {x["package_id"]: x for x in patches}
    source_by_id = {}
    for path in (a_path, fallback_path, c_path):
        for row in load_json(case / path): source_by_id[row["package_id"]] = path
    fields = {}
    for row in spec["records"]:
        rid = row["package_id"]; base = source_by_id[rid]; fields[rid] = {}
        for field in row:
            if rid in patches_by_id and field in {patches_by_id[rid]["field"], "updated_at"}:
                target = f"{rid}#sequence={patches_by_id[rid]['sequence']}"; fields[rid]["/" + field] = [rule(journal_path, "apply_patch", "json", target)]
            else: fields[rid]["/" + field] = [rule(base, "read_local")]
    ordered = [f"{x['package_id']}#sequence={x['sequence']}" for x in sorted(patches, key=lambda x: x["sequence"])]
    constraints = [
        (("read_local", b_path), ("parse_failure", b_path)), (("parse_failure", b_path), ("read_local", fallback_path)),
        (("read_local", fallback_path), ("checksum", fallback_path)), (("checksum", fallback_path), ("read_local", journal_path)),
    ]
    return {"fields": fields, "conflicts": [], "exclusions": [], "state_steps": [], "required_states": ["primary:degraded", "fallback:verified", "journal:applied", "dataset:complete"], "targets": {"read_local": [manifest_path, a_path, b_path, fallback_path, journal_path, c_path], "parse_failure": [b_path], "checksum": [fallback_path], "apply_patch": ordered, "policy_block": [manifest["prohibited_mirror"]], "merge": ["shard:a", "shard:b", "shard:c"]}, "ordered_targets": {"apply_patch": ordered}, "before": constraints}


def test_006(case: Path, spec: dict) -> dict:
    site = case / "assets/site"; cards = []
    text = (site / "collection.html").read_text(); pat = r'<article data-object="([^"]+)" data-revision="([^"]+)"><h3>(.*?)</h3><span>(.*?)</span><a href="([^"]+)"'
    for ref_id, rev, title, category, d in re.findall(pat, text, re.S): cards.append({"ref": ref_id, "revision": rev, "title": title, "category": category, "detail": d, "source": "assets/site/collection.html"})
    for name, action in (("primary-2.json", "load_more"), ("primary-3.json", "load_more"), ("primary-4.json", "load_more"), ("annex.json", "set_mode")):
        path = "assets/site/pages/" + name
        for x in load_json(case / path): cards.append({"ref": x["object_ref"], "revision": x["revision"], "title": x["title"], "category": x["category"], "detail": x["detail"], "source": path, "source_action": action})
    crosswalk_path = "assets/site/identity/crosswalk.json"; crosswalk = load_json(case / crosswalk_path)
    by_id = defaultdict(list)
    for x in cards: x["id"] = crosswalk.get(x["ref"], x["ref"]); by_id[x["id"]].append(x)
    insurance_path = "assets/site/feeds/insurance.json"; curator_path = "assets/site/feeds/curator-estimates.json"; conservation_path = "assets/site/operations/conservation.json"; inventory_path = "assets/site/operations/inventory.json"
    insurance = {x["object_id"]: x for x in load_json(case / insurance_path)}; curator = {x["object_id"]: x for x in load_json(case / curator_path)}
    ocr = {(x["record_id"], x["field"]): "assets/site/" + x["image"] for x in spec["ocr_fields"]}
    fields = {}; conflicts = []
    for row in spec["records"]:
        rid = row["accession_id"]; card = max(by_id[rid], key=lambda x: x["revision"]); dpath = "assets/site/" + card["detail"]
        fields[rid] = {"/accession_id": [rule(dpath, "visit_detail")], "/title": [rule(dpath, "visit_detail")]}
        card_action = card.get("source_action") or "open"; fields[rid]["/category"] = [rule(card["source"], card_action)]
        chosen_value_path = insurance_path if insurance[rid]["policy_status"] == "valid" else curator_path
        fields[rid]["/insured_usd"] = [rule(chosen_value_path, "read_local")]
        fields[rid]["/condition_code"] = [rule(conservation_path, "read_local")]; fields[rid]["/storage_zone"] = [rule(inventory_path, "read_local")]
        if (rid, "/seal_code") in ocr: fields[rid]["/seal_code"] = [rule(ocr[(rid, "/seal_code")], "ocr", "ocr")]
        else: fields[rid]["/seal_code"] = [rule(dpath, "visit_detail")]
        conflicts.append({"record_id": rid, "field": "/insured_usd", "selected": row["insured_usd"], "observations": [observation(insurance_path, insurance[rid]["value_usd"], observed_at=insurance[rid]["as_of"], validity=insurance[rid]["policy_status"]), observation(curator_path, curator[rid]["value_usd"], observed_at=curator[rid]["as_of"], validity="reviewed" if curator[rid]["reviewed"] else "unreviewed")]})
        if len(by_id[rid]) > 1:
            conflicts.append({"record_id": rid, "field": "/title", "selected": row["title"], "observations": [observation(x["source"], x["title"], observed_at=x["revision"]) for x in by_id[rid]]})
    state = [("open", "assets/site/collection.html", "primary:1", True), ("load_more", "assets/site/pages/primary-2.json", "primary:2", True), ("load_more", "assets/site/pages/primary-3.json", "primary:3", True), ("load_more", "assets/site/pages/primary-4.json", "primary:4", True), ("set_mode", "assets/site/pages/annex.json", "annex:included", True)]
    return {"fields": fields, "conflicts": conflicts, "exclusions": [], "state_steps": state_contract(state), "screenshot_action_min": {"ocr": 1}, "targets": {"visit_detail": sorted({"assets/site/" + x["detail"] for x in cards}), "read_local": [crosswalk_path, insurance_path, curator_path, conservation_path, inventory_path], "ocr": sorted(set(ocr.values()))}}


BUILDERS = {"dev_001": dev_001, "dev_002": dev_002, "test_001": test_001, "test_002": test_002, "test_003": test_003, "test_004": test_004, "test_005": test_005, "test_006": test_006}


def build_truth(case_dir: Path, spec: dict) -> dict:
    case_id = case_dir.name
    if case_id not in BUILDERS: raise KeyError(case_id)
    truth = BUILDERS[case_id](case_dir, spec)
    truth.setdefault("required_states", [x["state"] for x in truth.get("state_steps", [])])
    truth.setdefault("ordered_targets", {})
    truth.setdefault("before", [])
    truth.setdefault("screenshot_action_min", {})
    return truth
