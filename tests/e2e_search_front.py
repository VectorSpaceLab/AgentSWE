#!/usr/bin/env python3
"""End-to-end check of the Creation search path against a fake Serper upstream (needs docker; no real provider).

    python3 tests/e2e_search_front.py

Starts a fake upstream on the docker0 address, the run's search broker (real image, real broker code) holding a
random fake key, the per-run CA + leaf for search.example.com, and a compose project made of the TLS front (as
add_search_front configures it, then runtime_contract.prepare_compose as staging writes it) plus a Candidate-like
main service on the Candidate image. The main service mounts two setup-built environments read-only at Candidate
prefixes: the Web env (requests) and the QA env (httpx), from AGENTSWE_HOME/envs (run `agentswe setup
web-research-report` first; it builds both).

Every client below must reach the broker through the front:
  - urllib, default context, with the trust-store variables (system python3)
  - urllib, ssl.create_default_context(), variables removed (system python3: image system bundle)
  - urllib, ssl.create_default_context(), variables removed (env python: the prefix's OpenSSL cafile)
  - requests.post with default settings (env python)
  - requests.Session() with trust_env=False (env python: certifi)
  - requests.post with the variables removed (env python: certifi)
  - httpx.post with default settings and httpx.Client(trust_env=False) (QA env python: certifi)
Also checked: broker accounting equals the number of authorised calls, the upstream saw the key from the broker, a
wrong client token is refused, a client that trusts only an unrelated CA is rejected (TLS is really verified), the
key is absent from the client's environment and visible files, and nothing was written into the mounted envs.
Everything it starts is removed at the end.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runners" / "creation"))
from agentswe import config as agentswe_config  # noqa: E402
from agentswe.doctor import docker_ip  # noqa: E402
from agentswe.runners import creation_harbor_v1 as runner  # noqa: E402
import candidate_broker_protocol as protocol  # noqa: E402
import runtime_contract  # noqa: E402

WEB_TARGET = "/opt/agentswe/benchmark/agent-create-0804/envs/web-research-report-agent-v2"
QA_TARGET = "/opt/agentswe/benchmark/envs/evidence-grounded-document-qa-agent-v2"
TRUST_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")

# One script, run by several interpreters; argv[1] names the client, argv[2] the token.
PROBE = r'''
import json, os, ssl, sys, urllib.request
URL = "https://search.example.com/serp_search_v1"
mode, token = sys.argv[1], sys.argv[2]
body = {"query": "agentswe " + mode, "page": 1, "search_type": "search", "token": token}
def urllib_call(context=None):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=context) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
try:
    if mode == "urllib-env":
        status, payload = urllib_call()
    elif mode == "urllib-default-context":
        status, payload = urllib_call(ssl.create_default_context())
    elif mode == "urllib-unrelated-ca":
        status, payload = urllib_call(ssl.create_default_context(cafile="/unrelated-ca.pem"))
    elif mode.startswith("requests"):
        import requests
        if mode == "requests-session-trust-env-false":
            s = requests.Session(); s.trust_env = False
            r = s.post(URL, json=body, timeout=30)
        else:
            r = requests.post(URL, json=body, timeout=30)
        status, payload = r.status_code, r.json() if r.ok else None
    elif mode.startswith("httpx"):
        import httpx
        if mode == "httpx-client-trust-env-false":
            with httpx.Client(trust_env=False, timeout=30) as c:
                r = c.post(URL, json=body)
        else:
            r = httpx.post(URL, json=body, timeout=30)
        status, payload = r.status_code, r.json() if r.is_success else None
    else:
        raise SystemExit("unknown mode " + mode)
    print(json.dumps({"mode": mode, "status": status, "fake": "fake result" in json.dumps(payload)}))
except Exception as e:
    print(json.dumps({"mode": mode, "status": type(e).__name__, "error": str(e)[:200]}))
'''

DRIVER = r'''
import json, os, subprocess, sys
key, token = sys.argv[1], "search-placeholder"
web, qa = sys.argv[2], sys.argv[3]
trust_vars = %s
plain = {k: v for k, v in os.environ.items() if k not in trust_vars}
# The system interpreter runs without the env prefixes' LD_LIBRARY_PATH (prepare_compose adds it for the Candidate),
# so its default context really is the image's OpenSSL with the image's system bundle.
system = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
system_plain = {k: v for k, v in system.items() if k not in trust_vars}
runs = [
    ("urllib-env", "python3", system, token),
    ("urllib-default-context", "python3", system_plain, token),
    ("urllib-default-context", web + "/bin/python", plain, token),
    ("requests-default", web + "/bin/python", None, token),
    ("requests-session-trust-env-false", web + "/bin/python", None, token),
    ("requests-default", web + "/bin/python", plain, token),
    ("httpx-default", qa + "/bin/python", None, token),
    ("httpx-client-trust-env-false", qa + "/bin/python", None, token),
    ("urllib-env", "python3", system, "not-the-placeholder"),
    ("urllib-unrelated-ca", "python3", system, token),
]
rows = []
for mode, python, env, tok in runs:
    done = subprocess.run([python, "-I", "-B", "/probe.py", mode, tok], capture_output=True, text=True, env=env,
                          timeout=120)
    try:
        row = json.loads(done.stdout.strip().splitlines()[-1])
    except Exception:
        row = {"mode": mode, "status": "no-output", "error": (done.stderr or "")[-300:]}
    row.update({"python": "system" if python == "python3" else python.split("/")[-3],
                "trust_vars": env is None or "SSL_CERT_FILE" in env, "token_ok": tok == token})
    rows.append(row)
env_hit = any(key in v for v in os.environ.values())
file_hits = []
for top in ("/etc", "/opt", "/run", "/tmp", "/root", "/home"):
    for d, _, files in os.walk(top):
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.path.isfile(p) and os.path.getsize(p) < 2_000_000 and key.encode() in open(p, "rb").read():
                    file_hits.append(p)
            except OSError:
                pass
print("RESULT " + json.dumps({"rows": rows, "env_hit": env_hit, "file_hits": file_hits,
                              "trust": os.environ.get("SSL_CERT_FILE")}))
''' % (repr(TRUST_VARS),)


def tree_state(root: Path) -> str:
    """Digest of names, sizes and mtimes (no bytecode or file is written into the envs)."""
    h = hashlib.sha256()
    for dirpath, dirnames, files in os.walk(root):
        dirnames.sort()
        for name in sorted(files):
            p = Path(dirpath) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            h.update(f"{p.relative_to(root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def main() -> int:
    host = docker_ip()
    home = agentswe_config.load().home
    web_env, qa_env = home / "envs" / "web-research-report-agent-v2", home / "envs" / "evidence-grounded-document-qa-agent-v2"
    for env, lib in ((web_env, "requests"), (qa_env, "httpx")):
        if not any(env.glob(f"lib/python3*/site-packages/{lib}")):
            print(f"FAIL setup-built env with {lib} missing: {env} (run `agentswe setup web-research-report`)")
            return 1
    before = {str(env): tree_state(env) for env in (web_env, qa_env)}
    fake_key = "fake-search-key-" + secrets.token_hex(8)
    seen: list[dict] = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            seen.append({"path": self.path, "key": self.headers.get("X-API-KEY"), "q": body.get("q")})
            payload = json.dumps({"organic": [{"title": "fake result", "link": "https://example.org/"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    upstream = http.server.ThreadingHTTPServer((host, 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp(prefix="agentswe-search-e2e-"))
    project = "agentswe-oss-searche2e-" + secrets.token_hex(4)
    compose = work / "environment" / "docker-compose.yaml"
    cid = None
    try:
        env_file = work / ".env"
        env_file.write_text(f"AGENTSWE_SEARCH_BASE_URL=http://{host}:{upstream.server_port}\n"
                            f"AGENTSWE_SEARCH_API_KEY={fake_key}\nAGENTSWE_SEARCH_WIRE=serper\n")
        cfg = agentswe_config.load(env_file)
        images = json.loads((ROOT / "images" / "images.json").read_text())["images"]
        front_image, main_image = images["builder-codex"]["tag"], images["task-base"]["tag"]
        key_file = work / "search.key"
        key_file.write_text(f"AGENTSWE_SEARCH_API_KEY={fake_key}\n")
        key_file.chmod(0o600)
        port = runner.free_port(host)
        cid = runner.start_search_broker(cfg, project, host, port, key_file, work / "ledger", front_image)
        certs = runner.make_search_certificates(work / "search-front")
        unrelated = runner.make_search_certificates(work / "unrelated")["ca"]
        record = work / "search_trust.jsonl"
        os.environ["AGENTSWE_SEARCH_FRONT"] = json.dumps({
            "image": front_image, "broker_host": host, "broker_port": port,
            "script": str(ROOT / "runners" / "creation" / "search_front.py"),
            "cert": certs["cert"], "key": certs["key"], "ca_bundle": certs["ca_bundle"],
            "ca": certs["ca"], "ca_subject_hash": certs["ca_subject_hash"], "trust_record": str(record)})
        (work / "probe.py").write_text(PROBE)
        (work / "driver.py").write_text(DRIVER)
        bind = lambda source, target: {"type": "bind", "source": str(source), "target": target, "read_only": True}  # noqa: E731
        main = {"image": main_image, "command": ["python3", "-I", "/driver.py", fake_key, WEB_TARGET, QA_TARGET],
                "volumes": [bind(work / "probe.py", "/probe.py"), bind(work / "driver.py", "/driver.py"),
                            bind(unrelated, "/unrelated-ca.pem"), bind(web_env, WEB_TARGET), bind(qa_env, QA_TARGET)],
                "depends_on": ["agentswe-search-front"]}
        services = {"main": main}
        protocol.add_search_front(services, main)
        compose.parent.mkdir(parents=True)
        config = runtime_contract.prepare_compose(compose, {"services": services})
        compose.write_text(json.dumps(config))
        mounted = [v["target"] for v in config["services"]["main"]["volumes"]]
        up = subprocess.run(["docker", "compose", "-p", project, "-f", str(compose), "up", "--abort-on-container-exit",
                             "--exit-code-from", "main"], capture_output=True, text=True, timeout=900)
        line = next((l for l in up.stdout.splitlines() if "RESULT " in l), None)
        if line is None:
            print(up.stdout[-3000:], up.stderr[-2000:])
            return 1
        result = json.loads(line.split("RESULT ", 1)[1])
        stats = json.loads((work / "ledger" / "search_stats.json").read_text()) \
            if (work / "ledger" / "search_stats.json").is_file() else {}
        rows = result["rows"]
        authorised = [r for r in rows if r["token_ok"] and r["mode"] != "urllib-unrelated-ca"]
        checks = {}
        for r in authorised:
            label = f"{r['mode']} ({r['python']}, trust variables {'set' if r['trust_vars'] else 'removed'})"
            checks[f"reaches the broker: {label}"] = r["status"] == 200 and r.get("fake") is True
        bad = next(r for r in rows if not r["token_ok"])
        unrelated_row = next(r for r in rows if r["mode"] == "urllib-unrelated-ca")
        runtime = stats.get("runtime") or {}
        lines = [json.loads(x) for x in record.read_text().splitlines()] if record.is_file() else []
        env_targets = [t for t in (lines[0]["targets"] if lines else []) if t.startswith("/opt/agentswe/benchmark/")]
        checks.update({
            "wrong client token refused": bad["status"] == 401,
            "client trusting only an unrelated CA rejected": unrelated_row["status"] in (
                "URLError", "SSLError", "SSLCertVerificationError"),
            "upstream saw the key from the broker on every authorised call":
                len(seen) == len(authorised) and all(s["key"] == fake_key for s in seen),
            "broker accounted exactly the authorised calls":
                runtime.get("successful_calls") == len(authorised) and runtime.get("calls") == len(authorised),
            "key absent from client env": not result["env_hit"],
            "key absent from client files": not result["file_hits"],
            "trust store variables injected": result["trust"] == protocol.SEARCH_TRUST_BUNDLE,
            "system bundle overridden": "/etc/ssl/certs/ca-certificates.crt" in mounted,
            "certifi of both envs overridden under every alias": all(
                any(t.startswith(alias + "/") and t.endswith("certifi/cacert.pem") for t in env_targets)
                for alias in (WEB_TARGET, QA_TARGET, WEB_TARGET.replace("/agent-create-0804/envs/", "/envs/"))),
            "trust record written": len(lines) == 1 and lines[0]["bundle_sha256"] == certs["ca_bundle_sha256"],
            "nothing written into the mounted envs": before == {str(e): tree_state(e) for e in (web_env, qa_env)},
        })
        for name, passed in checks.items():
            print(("PASS " if passed else "FAIL ") + name)
        for r in rows:
            if r["status"] != 200:
                print("  client:", json.dumps(r)[:300])
        print("broker stats:", json.dumps(runtime)[:300])
        return 0 if all(checks.values()) else 1
    finally:
        subprocess.run(["docker", "compose", "-p", project, "-f", str(compose), "down", "-v", "--remove-orphans"],
                       capture_output=True, text=True)
        if cid:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
        upstream.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
