"""Create runtime contract v2026-09-09. No changes to cases or candidates.

All aliases bind the SAME run-private source. Trusted verifier aliases bind
only the separate trusted source. Generated source patches are local to a new
job, carry provenance, and never overwrite the benchmark or historical jobs.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "create-infra-2026-09-09-v1"
ROOT = Path(__file__).resolve().parent
PREFIX = "/opt/agentswe/benchmark"
DOCUMENTED_NAMES = {
    "authorized-vulnerability-validation": "authorized-vulnerability-validation-agent-v3",
    "database-analytics": "database-analytics-agent-hard-v4",
    "desktop-gui-automation": "desktop-gui-automation-agent-v2",
    "document-to-editable-pptx": "document-to-editable-pptx-agent-v2",
    "evidence-grounded-document-qa": "evidence-grounded-document-qa-agent-hard-v4",
    "formal-theorem-proving": "formal-theorem-proving-agent-v4",
    "repository-bug-repair": "repository-bug-repair-agent-hard-v4",
    "schema-guided-web-extraction": "schema-guided-web-extraction-agent-hard-v4",
    "scientific-pdf-translation": "scientific-pdf-translation-agent-v2",
    "web-research-report": "web-research-report-agent-v2",
}
PASSTHROUGH = (
    "AGENTSWE_RESPONSES_BASE_URL", "GATEWAY_RESPONSES_ENDPOINT", "OPENAI_BASE_URL",
    "AGENTSWE_GATEWAY_ENDPOINT", "AGENTSWE_REQUIRED_MODEL",
    "AGENTSWE_REQUIRED_REASONING_EFFORT", "AGENTSWE_RUNTIME_PREFIX",
    "AGENTSWE_EVALUATION_ID", "AGENTSWE_CASE_ID", "PLAYWRIGHT_BROWSERS_PATH",
    "FONTCONFIG_PATH", "FONTCONFIG_FILE", "AGENTSWE_FONT_DIRS",
    "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "OPENAI_API_KEY", "GATEWAY_API_KEY",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shared_source_digests() -> dict[str, str]:
    return {f"shared/{name}": sha(ROOT / name) for name in (
        "runtime_contract.py", "candidate_broker_protocol.py", "responses_broker.py", "broker_v2.py", "network_allocation.py", "trusted_browser.py", "agentswe_hosts.py"
    )}


def environment_aliases(source: Path, target: str) -> set[str]:
    aliases = {target}
    name = Path(target).name if target != "/opt/agentswe-trusted-runtime" else source.name
    # Conda scripts and fontconfig contain the installation prefix, even when
    # the host folder is renamed to builder_workspace/environment.
    for executable in ("pip", "pip3", "playwright"):
        p = source / "bin" / executable
        try:
            with p.open("rb") as stream:
                line = stream.readline(4096).decode("utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        match = re.match(r"#!(" + re.escape(PREFIX) + r"/(?:agent-create-0804/)?envs/[^/\s]+)/bin/", line)
        if match:
            aliases.add(match.group(1))
            name = Path(match.group(1)).name
    for task, documented in DOCUMENTED_NAMES.items():
        if name.startswith(task + "-agent-") or Path(target).name.startswith(task + "-agent-"):
            aliases.add(f"{PREFIX}/envs/{documented}")
    for alias in tuple(aliases):
        if alias.startswith(PREFIX + "/agent-create-0804/envs/"):
            aliases.add(alias.replace("/agent-create-0804/envs/", "/envs/"))
        elif alias.startswith(PREFIX + "/envs/"):
            aliases.add(alias.replace("/envs/", "/agent-create-0804/envs/"))
    return aliases


def documented_prefix(package: Path, fallback: str | None = None) -> str:
    text = (package / "input" / "04_resources.md").read_text()
    paths = set(re.findall(re.escape(PREFIX) + r"/(?:agent-create-0804/)?envs/[A-Za-z0-9_-]+", text))
    if not paths and fallback:
        return fallback
    if len(paths) != 1:
        raise RuntimeError(f"Environment contract requires one documented prefix: {sorted(paths)}")
    return paths.pop()


def patch_runtime_source(source: str, kind: str) -> str:
    """Small, guarded AST-located edits; unknown source shape fails closed."""
    tree = ast.parse(source)
    if kind == "renderer":
        nodes = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "DEFAULT_LO_ROOT" for t in n.targets)]
        if len(nodes) != 1:
            raise RuntimeError("Unsupported PPTX renderer: DEFAULT_LO_ROOT assignment missing")
        n = nodes[0]
        lines = source.splitlines(keepends=True)
        lines[n.lineno - 1:n.end_lineno] = ['DEFAULT_LO_ROOT = Path("/tools/libreoffice/root")\n']
        return "".join(lines)
    if kind == "gui":
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "isolated_environment"]
        if len(nodes) != 1:
            raise RuntimeError("Unsupported GUI harness: isolated_environment missing")
        returns = [n for n in ast.walk(nodes[0]) if isinstance(n, ast.Return)
                   and isinstance(n.value, ast.Name) and n.value.id == "environment"]
        if len(returns) != 1:
            raise RuntimeError("Unsupported GUI isolated environment return")
        lines = source.splitlines(keepends=True)
        i = returns[0].lineno - 1
        addition = (
            f"    # {VERSION}: keep only evaluator-owned runtime/API settings.\n"
            f"    for key in {PASSTHROUGH!r}:\n"
            "        if key in os.environ:\n"
            "            environment[key] = os.environ[key]\n"
        )
        lines[i:i] = [addition]
        patched = "".join(lines)
        # Drain both pipes while checking resource limits. Waiting for poll()
        # first deadlocks once either stream fills, even for a short traceback.
        old = "            time.sleep(0.25)\n        stdout, stderr = candidate.communicate()"
        new = ("            try:\n"
               "                candidate.communicate(timeout=0.25)\n"
               "            except subprocess.TimeoutExpired:\n"
               "                pass\n"
               "        stdout, stderr = candidate.communicate()")
        if "candidate = subprocess.Popen(" in patched:
            if patched.count(old) != 1:
                raise RuntimeError("Unsupported GUI candidate output-drain loop")
            patched = patched.replace(old, new)
        return patched
    raise ValueError(kind)


def generated_source(source: Path, compose_path: Path, kind: str) -> Path:
    content = patch_runtime_source(source.read_text(), kind)
    directory = compose_path.parent / "runtime_support"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    target.write_text(content)
    target.with_suffix(".provenance.json").write_text(json.dumps({
        "version": VERSION, "original": str(source.resolve()),
        "original_sha256": sha(source), "patched_sha256": sha(target),
        "patch": kind, "benchmark_unchanged": True,
    }, indent=2) + "\n")
    return target.resolve()


def prepare_compose(path: Path, value: object) -> object:
    if path.name != "docker-compose.yaml" or not isinstance(value, dict):
        return value
    config = copy.deepcopy(value)
    for service in config.get("services", {}).values():
        volumes = service.get("volumes", [])
        env = service.setdefault("environment", {})
        env["AGENTSWE_INFRA_VERSION"] = VERSION
        existing = {v.get("target"): v for v in volumes if isinstance(v, dict)}
        if "/run/secrets/agentswe.env" in existing:
            # Result-judge jobs: endpoint, model and effort come from the evaluator configuration;
            # the key itself stays in the mounted credential file.
            for key in ("AGENTSWE_JUDGE_RESPONSES_URL", "AGENTSWE_JUDGE_MODEL", "AGENTSWE_JUDGE_EFFORT"):
                if os.environ.get(key):
                    env.setdefault(key, os.environ[key])
        if path.parent.name == "tests":
            # Explicit runtime bindings prevent a reused verifier image from
            # silently executing the pre-fix Python/scripts baked into it.
            for script in sorted(path.parent.iterdir()):
                target = "/tests/" + script.name
                if script.suffix in {".py", ".sh"} and script.is_file() and target not in existing:
                    if script.suffix == ".sh":
                        script.chmod(script.stat().st_mode | 0o111)
                    volume = {"type": "bind", "source": str(script.resolve()),
                              "target": target, "read_only": True}
                    volumes.append(volume)
                    existing[target] = volume
        bindings = [v for v in list(existing.values()) if
                    v.get("target") == "/opt/agentswe-trusted-runtime" or
                    re.fullmatch(re.escape(PREFIX) + r"/(?:agent-create-0804/)?envs/[^/.]+", str(v.get("target", "")))]
        for volume in bindings:
            source, target = Path(volume["source"]), volume["target"]
            for alias in sorted(environment_aliases(source, target)):
                if alias in existing:
                    other = existing[alias]
                    if Path(other["source"]).resolve() != source.resolve() or bool(other.get("read_only")) != bool(volume.get("read_only")):
                        raise RuntimeError(f"Conflicting environment mount: {alias}")
                    continue
                mount = dict(volume, target=alias)
                volumes.append(mount)
                existing[alias] = mount
            # Builder and Candidate discover packages, fonts and browsers in
            # their own private prefix; trusted verifier gets its own prefix.
            env.setdefault("AGENTSWE_RUNTIME_PREFIX", target)
            if (source / "lib").is_dir():
                library = target + "/lib"
                current = env.get("LD_LIBRARY_PATH", "").split(":")
                if library not in current:
                    env["LD_LIBRARY_PATH"] = ":".join([library, *filter(None, current)])
            if (source / "browsers").is_dir():
                env.setdefault("PLAYWRIGHT_BROWSERS_PATH", target + "/browsers")
            if (source / "etc/fonts/fonts.conf").is_file():
                env.setdefault("FONTCONFIG_PATH", target + "/etc/fonts")
                env.setdefault("FONTCONFIG_FILE", target + "/etc/fonts/fonts.conf")
        if "/builder-package" in existing:
            package = Path(existing["/builder-package"]["source"])
            documented = documented_prefix(package, env.get("AGENTSWE_RUNTIME_PREFIX"))
            if documented not in existing or existing[documented].get("read_only"):
                raise RuntimeError("Documented Builder prefix is not the writable private mount")
            env["AGENTSWE_RUNTIME_PREFIX"] = documented
        for volume in list(volumes):
            if not isinstance(volume, dict):
                continue
            target = volume.get("target")
            if target == "/evaluator/render_pptx.py":
                volume["source"] = str(generated_source(Path(volume["source"]), path, "renderer"))
                # Trusted renderer only; no augmentation of Candidate baselines.
                bundle = Path(os.environ.get("AGENTSWE_PPTX_RENDERER_LIBS",
                    str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "deps" / "runtime-dependencies-v1" / "pptx" / "lib")))
                manifest = bundle.parent.parent / "manifest.json"
                if not manifest.is_file() or not bundle.is_dir():
                    raise RuntimeError("Trusted PPTX dependency bundle is missing")
                records = json.loads(manifest.read_text())["files"]
                expected = {Path(name).name: row["sha256"] for name,row in records.items() if name.startswith("pptx/lib/")}
                if not expected or {p.name:sha(p) for p in bundle.iterdir()} != expected:
                    raise RuntimeError("Trusted PPTX dependency bundle digest mismatch")
                library_target = "/opt/agentswe-pptx-renderer-libs"
                volumes.append({"type":"bind", "source":str(bundle), "target":library_target, "read_only":True})
                env["LD_LIBRARY_PATH"] = library_target + ":" + env.get("LD_LIBRARY_PATH", "")
            elif target == "/harness/run_case.py":
                volume["source"] = str(generated_source(Path(volume["source"]), path, "gui"))
        if ("/submission" in existing and "AGENTSWE_RESPONSES_BASE_URL" in env
                and not str(env["AGENTSWE_RESPONSES_BASE_URL"]).startswith("http://model-relay:")):
            case = next((Path(t).name for t in existing if str(t).startswith("/active-case/")), "unknown")
            task_parent = path.parent.parent
            evaluation = task_parent.parent.parent.name
            base = env["AGENTSWE_RESPONSES_BASE_URL"].split("/v1/", 1)[0]
            endpoint = base + "/context/" + urllib.parse.quote(evaluation, safe="") + "/" + urllib.parse.quote(case, safe="") + "/v1/responses"
            env.update({"AGENTSWE_RESPONSES_BASE_URL": endpoint,
                        "GATEWAY_RESPONSES_ENDPOINT": endpoint, "AGENTSWE_GATEWAY_ENDPOINT": endpoint,
                        "OPENAI_BASE_URL": endpoint.removesuffix("/responses"),
                        "AGENTSWE_CASE_ID": case, "AGENTSWE_EVALUATION_ID": evaluation})
    pool = os.environ.get("AGENTSWE_CREATE_NETWORK_POOL")
    if pool:
        from network_allocation import allocate
        state_dir = Path(os.environ.get("AGENTSWE_CREATE_NETWORK_STATE", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "network_allocations")))
        subnet, owner = allocate(path, pool, state_dir)
        if config.get("networks", {}).get("default", {}).get("external"):
            raise RuntimeError("Cannot replace an external Docker network")
        network = config.setdefault("networks", {}).setdefault("default", {})
        if network.get("ipam", {}).get("config") not in (None, [{"subnet": subnet}]):
            raise RuntimeError("Conflicting explicit Create network allocation")
        network.setdefault("ipam", {})["config"] = [{"subnet": subnet}]
        network.setdefault("labels", {}).update({"agentswe.infra.owner": owner, "agentswe.infra.version": VERSION})
    return config


def audit_environment_mounts(volumes: list[dict], private: Path, primary: str) -> set[str]:
    expected = environment_aliases(private, primary)
    found = {v.get("target"): v for v in volumes if v.get("target") in expected}
    if set(found) != expected:
        raise RuntimeError("Missing environment aliases in Builder visibility audit")
    for volume in found.values():
        if Path(volume["source"]).resolve() != private.resolve() or volume.get("read_only"):
            raise RuntimeError("Builder environment alias is not the same writable private copy")
    return expected


def assert_runtime_health(config_path: Path, run_dir: Path) -> None:
    """Run before result judging: verifier/provider failure is NOT a zero."""
    config = json.loads(config_path.read_text())
    job = Path(config["jobs_dir"]) / config["job_name"]
    failures = []
    if not (job / "result.json").is_file():
        failures.append({"path": str(job), "error": "Harbor job result is missing"})
    for result in job.glob("*/result.json"):
        record = json.loads(result.read_text())
        if record.get("exception_info"):
            failures.append({"path": str(result), "error": "Harbor trial infrastructure exception",
                             "exception_type": record["exception_info"].get("exception_type")})
    for result in job.glob("*/verifier/score_contract.json"):
        record = json.loads(result.read_text())
        if record.get("infrastructure_error") or record.get("contract_valid") is False:
            failures.append({"path": str(result), "error": record.get("infrastructure_error", "invalid_contract")})
    port = os.environ.get("AGENTSWE_CANDIDATE_BROKER_PORT")
    if port and "eval" not in config_path.stem:
        url = f"http://{os.environ.get('AGENTSWE_BROKER_HOST', '127.0.0.1')}:{int(port)}/events?evaluation_id=" + urllib.parse.quote(run_dir.name, safe="")
        request = urllib.request.Request(url, headers={"Authorization": "Bearer stats-only-placeholder"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                events = json.load(response)
        except Exception as exc:
            raise RuntimeError(f"Candidate broker telemetry unavailable: {type(exc).__name__}") from None
        (run_dir / "candidate_api_events.json").write_text(json.dumps(events, indent=2) + "\n")
        broker_failures = []
        for event in events:
            if event.get("classification") in {"provider_failure", "transport_failure", "broker_failure", "delivery_failure"}:
                broker_failures.append({"case_id": event.get("case_id"), "request_id": event.get("request_id"),
                                 "error": event["classification"], "upstream_status": event.get("upstream_status")})
        if broker_failures:
            # Policy of the paper's final Creation evaluation: broker/provider failures withhold only the affected cases.
            # The adapter drops those cases before the Eval job; the driver replays them later.
            # A case whose only broker failures are delivery_failure (the Candidate itself hung up, for example at its
            # own timeout) is judged on what the Candidate produced, as in the paper's evaluation: Candidate
            # behaviour, not evaluator infrastructure. It is recorded, not withheld.
            (run_dir / "broker_infrastructure_failures.json").write_text(json.dumps(broker_failures, indent=2) + "\n")
            kinds: dict[str, set] = {}
            for failure in broker_failures:
                kinds.setdefault(str(failure.get("case_id")), set()).add(failure["error"])
            delivery_only = sorted(c for c, k in kinds.items() if k == {"delivery_failure"})
            withheld = sorted(c for c in kinds if c not in delivery_only)
            if delivery_only:
                (run_dir / "candidate_delivery_failures.json").write_text(json.dumps(delivery_only) + "\n")
            if withheld:
                (run_dir / "infra_failed_cases.json").write_text(json.dumps(withheld) + "\n")
    if failures:
        (run_dir / "runtime_infrastructure_failure.json").write_text(json.dumps(failures, indent=2) + "\n")
        raise RuntimeError("Create runtime infrastructure failure; score withheld: " + json.dumps(failures))
