# Configuration profiles

`AGENTSWE_PROFILE` selects one of two configurations:

| profile | Builder-visible bytes | role models (this file, below `.env`) |
|---|---|---|
| `paper` (default when unset) | the bytes the paper runs received | `paper.env`: Creation runtime gpt-5.6-sol medium, judge xhigh; Editing runtime and judge on the DeepSeek API (Flash, high / max); Optimization runtime and evaluator gpt-5.6-sol |
| `lite-v1.1` | the bytes the paper's Lite runs received | `lite-v1.1.env`: deepseek-flash for every role (runtime high, judge max) |

With `AGENTSWE_PROFILE` unset, tasks use the paper bytes and the built-in smoke defaults (deepseek-flash for every
role). Such a smoke validates that a task runs; its scores are not comparable with the paper, and the full-set tasks'
Builder-visible runtime description then describes the paper runtime, not the model actually configured.

Smoke default runtime effort: in that case the Creation runtime runs deepseek-flash **without reasoning** (effort
`explicit-none`, sent to the provider as `none`). Candidates written for the paper runtime set output caps of a few
thousand tokens, which flash at effort `high` spends on reasoning. This applies only when no profile is set and you
have not configured the Creation runtime's model, provider or effort (setting `AGENTSWE_DEFAULT_BASE_URL` counts as
configuring the provider, even to the built-in URL; `docs/ENV.providers.md` lists the variables); both profiles keep
their configured effort (the paper's Lite Creation runs used `high`). Effort values: `none` / `off` / `unset` leave the effort
out of the request (for models without the setting); `explicit-none` sends the literal `none`. The same rule sets the
OSWorld vision broker's effort (`AGENTSWE_OSWORLD_EFFORT`, otherwise the paper's `high`) to `explicit-none` when no
profile is set and you have not configured that effort or the Optimization runtime's model or provider: at `high`,
reasoning took about 40% of each action call's tokens and the smoke episodes ran out of their 100,000-token budget
before finishing. The launch manifest records the setting (`config_env`) and each case's broker stats the effort sent
(`reasoning_effort`).

Setting `AGENTSWE_PROFILE` explicitly loads `profiles/<profile>.env` below your `.env`. The profile files name models
and efforts per family (`AGENTSWE_<FAMILY>_<ROLE>_MODEL` / `_EFFORT`); provider URLs and keys always come from your
`.env` (`AGENTSWE_<FAMILY>_<ROLE>_BASE_URL` / `_API_KEY`, else `AGENTSWE_<ROLE>_*`, else `AGENTSWE_DEFAULT_*`).
Check the wiring with one request per role before any run: `agentswe probe-roles --family creation` (and
`editing`, `optimization`).

Model ids: on the official DeepSeek API, `deepseek-flash` is DeepSeek-V4.1-Flash, the model the paper used for the
Editing runtime and judge ("deepseek-v4.1-flash"); this is a provider alias whose target may change (verified
2026-10-03). `agentswe probe-roles --family editing` reports whether the provider answered under the configured name
(`model_echo`) and, when it named another model, that name (`reported_model`); DeepSeek answers with the alias
itself, so the probe cannot show which model the alias currently targets.

In every profile the provider gateway's name in Builder-visible text is replaced by a neutral `GATEWAY` alias
(endpoint `https://gateway.example.com`); semantics are unchanged.

Task overlays: `tasks/<family>/<id>/profiles/<profile>/` holds the files whose bytes differ in that profile, laid
out like the task directory (for example `builder_package/input/04_resources.md`). The runners stage the task with
the overlay applied under `$AGENTSWE_HOME/profiles/<profile>/<task id>/`; Editing renders it into the task tree.
