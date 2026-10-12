# Codex 0.144.1 builder configuration

`gen_codex_config.py` turns a builder model + provider type into the two files Codex reads:

* `model_catalog.json` — the AgentSWE-Lite v1 catalog (Codex's built-in table with GPT-5.6 Sol's
  `multi_agent_version` nulled, plus `deepseek-flash` and Qwen entries cloned from `gpt-5.5`). A
  model not in it is cloned from `gpt-5.5` with the same overrides (freeform `apply_patch`,
  `shell_command`, no multi-agent, text input, low/high/max levels) and needs a context window.
* `config.toml` — provider block with `wire_api = "responses"`, `model_catalog_json`, and
  `[features] multi_agent = false`.

Both multi-agent switches are mandatory: the catalog's `multi_agent_version = null` removes the v2
collaboration tools and the TOML switch removes `multi_agent_v1`; either alone still exposes
`spawn_agent` (measured), which yields extra rollouts and invalid runs. Without a catalog entry Codex
silently falls back to generic metadata with **no `apply_patch`** — the tool gap of the paper's
non-GPT Editing/Optimization builders.

```bash
# broker in front (default; Codex sees a placeholder token in AGENTSWE_BUILDER_BROKER_TOKEN)
python gen_codex_config.py --model deepseek-flash --effort max \
    --base-url http://172.17.0.1:18114/v1 --out-dir <run>/codex_home
# Chat-only provider: same, with --wire chat (the broker translates)
python gen_codex_config.py --model Qwen/Qwen3.6-35B-A3B --effort max --wire chat \
    --base-url http://172.17.0.1:18114/v1 --out-dir <run>/codex_home
# preflight check (the runner refuses to start on failure)
python gen_codex_config.py --verify <run>/codex_home
```

`codex_config_manifest.json` records the catalog/config sha256 and `lite_identical` (true when the
catalog is byte-identical to the Lite catalog `17ec93cb…`, i.e. the run used the Lite tool surface).
`known_models.json` holds context windows for non-built-in models; `profiles.json` is a proposal for
`agentswe run --builder <profile>`.

Notes
* Cloned entries keep `gpt-5.5`'s `base_instructions` ("based on GPT-5"), as in Lite: every builder
  gets the same Codex system prompt. This is deliberate and must be stated with results.
* A cloned entry's `context_window` must equal the provider's context minus the completion cap the
  broker sends (`AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS`, default 65536) on Chat providers.
* Tests: `python -m unittest tests.test_gen_codex_config` (14, offline); they include regenerating
  the Lite catalog byte for byte from the shipped base; regenerating it from the 117b no-multi-agent
  base (Lite models added in order) was checked once and also gives `17ec93cb…`.
