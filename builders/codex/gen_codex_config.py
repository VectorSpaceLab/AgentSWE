#!/usr/bin/env python3
"""Generate a Codex 0.144.1 provider config + model catalog for any builder model.

Invariants enforced on every output (and re-checked by ``verify``):

  * the selected model has a catalog entry, so Codex never falls back to its
    generic metadata (which silently drops ``apply_patch`` and the reasoning
    field -- the cause of the paper's non-GPT Editing/Optimization tool gap);
  * that entry has ``apply_patch_tool_type = "freeform"``;
  * multi-agent tools are off by BOTH switches: the entry's
    ``multi_agent_version`` is null (removes v2 collaboration tools) AND the
    TOML has ``[features] multi_agent = false`` (removes ``multi_agent_v1``);
    either one alone still exposes spawn_agent (measured 2026-09-23, 2026-10-01);
  * ``wire_api = "responses"`` (Codex 0.144.1 refuses "chat"; a Chat-only
    provider is reached through the AgentSWE broker, provider type ``broker``);
  * no credential value is ever written: ``broker`` uses a placeholder token via
    ``env_key``; ``direct`` uses an auth command that reads a mounted 0600 file.

The base catalog is the AgentSWE-Lite v1 catalog (Codex 0.144.1 built-in table
with GPT-5.6 Sol's multi_agent_version nulled, plus deepseek-flash and Qwen
entries cloned from gpt-5.5).  For those models the output catalog is byte
identical to Lite (``lite_identical: true`` in the manifest).  Any other
non-built-in slug is cloned from gpt-5.5 with the same overrides; it then needs
a context window (``--context-window`` or ``known_models.json``).

Standard library only.  Usage:
  gen_codex_config.py --model deepseek-flash --effort max --provider-type broker \
      --base-url http://172.17.0.1:18114/v1 --out-dir <run>/codex_home
  gen_codex_config.py --verify <run>/codex_home
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BASE_CATALOG = HERE / "catalog" / "codex-0.144.1-lite-v1.json"
LITE_CATALOG_SHA256 = "17ec93cb142438979e1ea9e8a915151074335885a1f4d2e12cfbdf27e4acae29"
KNOWN_MODELS = HERE / "known_models.json"
CLONE_SOURCE = "gpt-5.5"
CONTAINER_CATALOG = "/tmp/codex-home/model_catalog.json"
BROKER_ENV_KEY = "AGENTSWE_BUILDER_BROKER_TOKEN"
BROKER_PLACEHOLDER = "broker-only-placeholder"
SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9_\-.]{16,}|experimental_bearer_token)")

# Same overrides as build_lite_catalog.py, so a regenerated entry for a
# Lite model is byte identical to the Lite catalog.
LEVELS = [
    {"effort": "low", "description": "Lighter reasoning"},
    {"effort": "high", "description": "Deeper reasoning"},
    {"effort": "max", "description": "Maximum supported reasoning depth"},
]


class GenerationError(ValueError):
    pass


def clone_entry(base_entry: dict, slug: str, *, display: str, description: str, context_window: int,
                levels: list | None = None, default_level: str = "max") -> dict:
    entry = copy.deepcopy(base_entry)
    entry.update({
        "slug": slug, "display_name": display, "description": description,
        "context_window": context_window, "max_context_window": context_window,
        "apply_patch_tool_type": "freeform", "shell_type": "shell_command",
        "input_modalities": ["text"], "supports_image_detail_original": False,
        "prefer_websockets": False, "multi_agent_version": None, "tool_mode": None,
        "use_responses_lite": False, "supports_search_tool": False,
        "supported_reasoning_levels": levels or LEVELS, "default_reasoning_level": default_level,
        "default_service_tier": None, "service_tiers": [], "additional_speed_tiers": [],
        "availability_nux": None, "upgrade": None, "priority": 90, "visibility": "list",
    })
    return entry


def dump_catalog(catalog: dict) -> str:
    # Matches build_lite_catalog.py's serialisation (indent=1, insertion order).
    return json.dumps(catalog, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def build_catalog(model: str, *, base_catalog: dict, known: dict, context_window: int | None,
                  null_all_multi_agent: bool = False) -> tuple[dict, dict]:
    catalog = copy.deepcopy(base_catalog)
    models = catalog["models"]
    by_slug = {entry["slug"]: entry for entry in models}
    notes: dict = {"cloned": [], "modified": []}
    info = known.get(model, {})
    wanted = [model] + [alias for alias in info.get("aliases", []) if alias != model]
    if "/" in model:
        short = model.rsplit("/", 1)[1]
        if short not in wanted:
            wanted.append(short)  # Codex records the short name in turn_context
    for slug in wanted:
        if slug in by_slug:
            continue
        window = context_window or info.get("context_window")
        if not isinstance(window, int) or window <= 0:
            raise GenerationError("model %r is not in the catalog; pass --context-window or add it to "
                                  "known_models.json (prompt tokens the provider accepts after reserving "
                                  "the completion cap)" % model)
        if CLONE_SOURCE not in by_slug:
            raise GenerationError("base catalog has no %s entry to clone" % CLONE_SOURCE)
        own = known.get(slug, info)  # an alias may carry its own display text
        entry = clone_entry(by_slug[CLONE_SOURCE], slug, display=own.get("display_name", slug),
                            description=own.get("description", "AgentSWE builder model %s." % slug),
                            context_window=own.get("context_window", window) if not context_window else window,
                            levels=own.get("reasoning_levels"),
                            default_level=own.get("default_reasoning_level", "max"))
        models.append(entry)
        by_slug[slug] = entry
        notes["cloned"].append(slug)
    targets = models if null_all_multi_agent else [by_slug[s] for s in wanted]
    for entry in targets:
        if entry.get("multi_agent_version") is not None:
            entry["multi_agent_version"] = None
            notes["modified"].append("%s.multi_agent_version" % entry["slug"])
    selected = by_slug[model]
    if selected.get("apply_patch_tool_type") != "freeform":
        raise GenerationError("catalog entry %r does not offer apply_patch as a freeform tool" % model)
    return catalog, notes


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON string escaping is valid TOML basic-string


def render_config(*, model: str, effort: str | None, base_url: str, provider_type: str,
                  catalog_path: str, provider_id: str = "agentswe", env_key: str = BROKER_ENV_KEY,
                  auth_command: str | None = None, request_max_retries: int | None = None,
                  stream_max_retries: int | None = None, stream_idle_timeout_ms: int | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", provider_id):
        raise GenerationError("provider id must be [A-Za-z0-9_]+")
    lines = ["# Generated by builders/codex/gen_codex_config.py -- do not edit by hand.",
             "model_provider = %s" % toml_string(provider_id),
             "model = %s" % toml_string(model)]
    if effort:
        lines.append("model_reasoning_effort = %s" % toml_string(effort))
    lines += ["model_catalog_json = %s" % toml_string(catalog_path),
              "disable_response_storage = true", "",
              "[model_providers.%s]" % provider_id,
              "name = %s" % toml_string("AgentSWE builder provider (%s)" % provider_type),
              "base_url = %s" % toml_string(base_url),
              'wire_api = "responses"',
              "requires_openai_auth = false"]
    if provider_type == "broker":
        lines.append("env_key = %s" % toml_string(env_key))
    for key, value in (("request_max_retries", request_max_retries),
                       ("stream_max_retries", stream_max_retries),
                       ("stream_idle_timeout_ms", stream_idle_timeout_ms)):
        if value is not None:
            lines.append("%s = %d" % (key, int(value)))
    if provider_type == "direct":
        if not auth_command:
            raise GenerationError("provider type 'direct' needs --auth-command (reads a mounted 0600 key file)")
        lines += ["", "[model_providers.%s.auth]" % provider_id,
                  "command = %s" % toml_string(auth_command),
                  "timeout_ms = 30000", "refresh_interval_ms = 300000"]
    lines += ["", "[features]", "multi_agent = false", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# verification (used by the runner's preflight; refuses to start on failure)
# ---------------------------------------------------------------------------
def parse_simple_toml(text: str) -> dict:
    """Parse the TOML subset this generator emits (tables, strings, ints, bools)."""
    try:
        import tomllib  # Python >= 3.11
        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass
    root: dict = {}
    table = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            table = root
            for part in line[1:-1].split("."):
                table = table.setdefault(part.strip(), {})
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value.startswith('"'):
            parsed = json.loads(value)
        elif value in ("true", "false"):
            parsed = value == "true"
        else:
            parsed = int(value)
        table[key] = parsed
    return root


def verify(config_text: str, catalog: dict) -> list[str]:
    errors = []
    try:
        config = parse_simple_toml(config_text)
    except Exception as exc:  # noqa: BLE001
        return ["config.toml does not parse: %s" % exc]
    if SECRET_PATTERN.search(config_text):
        errors.append("config.toml contains a credential-shaped value")
    model = config.get("model")
    provider = (config.get("model_providers") or {}).get(config.get("model_provider") or "", {})
    if not config.get("model_catalog_json"):
        errors.append("model_catalog_json is not set (Codex would use fallback metadata)")
    if (config.get("features") or {}).get("multi_agent") is not False:
        errors.append("[features] multi_agent = false is missing")
    if provider.get("wire_api") != "responses":
        errors.append("wire_api must be 'responses' for Codex 0.144.1")
    entries = {entry.get("slug"): entry for entry in catalog.get("models", [])}
    entry = entries.get(model)
    if entry is None:
        errors.append("model %r has no catalog entry" % model)
    else:
        if entry.get("multi_agent_version") is not None:
            errors.append("catalog entry %r has multi_agent_version=%r" % (model, entry.get("multi_agent_version")))
        if entry.get("apply_patch_tool_type") != "freeform":
            errors.append("catalog entry %r does not offer freeform apply_patch" % model)
        effort = config.get("model_reasoning_effort")
        levels = [level.get("effort") for level in entry.get("supported_reasoning_levels") or []]
        if effort and levels and effort not in levels:
            errors.append("effort %r is not a supported level of %r (%s)" % (effort, model, levels))
    return errors


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate(args: argparse.Namespace) -> dict:
    if args.wire == "chat" and args.provider_type != "broker":
        raise GenerationError("a Chat-only provider must be reached through the broker (--provider-type broker)")
    base_text = Path(args.base_catalog).read_text(encoding="utf-8")
    base_catalog = json.loads(base_text)
    known = json.loads(Path(args.known_models).read_text(encoding="utf-8")) if Path(args.known_models).is_file() else {}
    known = {k: v for k, v in known.items() if not k.startswith("_")}
    catalog, notes = build_catalog(args.model, base_catalog=base_catalog, known=known,
                                   context_window=args.context_window,
                                   null_all_multi_agent=args.null_all_multi_agent)
    catalog_text = dump_catalog(catalog)
    config_text = render_config(
        model=args.model, effort=args.effort, base_url=args.base_url, provider_type=args.provider_type,
        catalog_path=args.catalog_path, provider_id=args.provider_id, env_key=args.env_key,
        auth_command=args.auth_command, request_max_retries=args.request_max_retries,
        stream_max_retries=args.stream_max_retries, stream_idle_timeout_ms=args.stream_idle_timeout_ms)
    errors = verify(config_text, catalog)
    if errors:
        raise GenerationError("; ".join(errors))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_catalog.json").write_text(catalog_text, encoding="utf-8")
    (out / "config.toml").write_text(config_text, encoding="utf-8")
    manifest = {
        "schema_version": "agentswe-codex-config/v1", "codex_version": "0.144.1",
        "model": args.model, "effort": args.effort, "provider_type": args.provider_type,
        "upstream_wire": args.wire, "base_url": args.base_url, "catalog_path_in_container": args.catalog_path,
        "catalog_sha256": sha256(catalog_text), "config_sha256": sha256(config_text),
        "base_catalog_sha256": sha256(base_text),
        "lite_identical": sha256(catalog_text) == LITE_CATALOG_SHA256,
        "cloned_entries": notes["cloned"], "modified_fields": notes["modified"],
        "multi_agent": {"catalog_multi_agent_version": None, "features_multi_agent": False},
        "apply_patch_tool_type": "freeform",
        "client_token_env": args.env_key if args.provider_type == "broker" else None,
        "client_token_value": BROKER_PLACEHOLDER if args.provider_type == "broker" else None,
    }
    (out / "codex_config_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", type=Path, help="check an existing output directory and exit")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--provider-type", choices=("broker", "direct"), default="broker")
    parser.add_argument("--wire", choices=("responses", "chat"), default="responses",
                        help="the provider's wire (chat requires --provider-type broker)")
    parser.add_argument("--base-url", help="broker URL as seen from the Builder container, or the provider URL")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--base-catalog", default=str(DEFAULT_BASE_CATALOG))
    parser.add_argument("--known-models", default=str(KNOWN_MODELS))
    parser.add_argument("--catalog-path", default=CONTAINER_CATALOG)
    parser.add_argument("--provider-id", default="agentswe")
    parser.add_argument("--env-key", default=BROKER_ENV_KEY)
    parser.add_argument("--auth-command")
    parser.add_argument("--request-max-retries", type=int)
    parser.add_argument("--stream-max-retries", type=int)
    parser.add_argument("--stream-idle-timeout-ms", type=int)
    parser.add_argument("--null-all-multi-agent", action="store_true",
                        help="null multi_agent_version on every entry (output is then not Lite-identical)")
    parser.add_argument("--out-dir")
    args = parser.parse_args(argv)
    if args.verify:
        config_text = (args.verify / "config.toml").read_text(encoding="utf-8")
        catalog = json.loads((args.verify / "model_catalog.json").read_text(encoding="utf-8"))
        errors = verify(config_text, catalog)
        print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
        return 0 if not errors else 1
    missing = [name for name in ("model", "base_url", "out_dir") if not getattr(args, name)]
    if missing:
        parser.error("missing: " + ", ".join("--" + m.replace("_", "-") for m in missing))
    try:
        manifest = generate(args)
    except GenerationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **{k: manifest[k] for k in ("model", "catalog_sha256", "config_sha256",
                                                              "lite_identical", "cloned_entries")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
