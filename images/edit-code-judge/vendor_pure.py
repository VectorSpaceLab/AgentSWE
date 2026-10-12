#!/usr/bin/env python3
"""Vendor the pure-Python modules of the pinned wheels into <out> (binary
extensions excluded, no dist-info), check every file against expected-files.json
and write dependency_manifest.json in the paper-era format."""
import hashlib, json, sys, zipfile
from pathlib import Path

wheels, out, expected = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(Path(sys.argv[3]).read_text())
modules = {"requests": "requests", "certifi": "certifi", "charset_normalizer": "charset-normalizer", "idna": "idna", "urllib3": "urllib3"}
packages = []
for whl in sorted(wheels.glob("*.whl")):
    with zipfile.ZipFile(whl) as z:
        for n in z.namelist():
            top = n.split("/")[0]
            if top in modules and not n.endswith((".so", "/")) and "__pycache__" not in n:
                dest = out / n; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(z.read(n))
files = [{"path": str(p.relative_to(out)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
         for p in sorted(out.rglob("*")) if p.is_file()]
got = {f["path"]: f["sha256"] for f in files}; want = {f["path"]: f["sha256"] for f in expected["files"]}
if got != want:
    diff = sorted(set(got.items()) ^ set(want.items()))
    sys.exit(f"vendored files differ from expected-files.json: {diff[:6]}")
manifest = {"packages": [{**p, "source": "pypi:" + p["distribution"] + "==" + p["version"]} for p in expected["packages"]],
            "files": files, "network_requests": 0, "credential_values_recorded": False}
(out / "dependency_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"vendored {len(files)} files")
