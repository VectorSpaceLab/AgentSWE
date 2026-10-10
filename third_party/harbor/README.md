# Harbor 0.20.0 + AgentSWE patches

AgentSWE runs on [Harbor](https://pypi.org/project/harbor/0.20.0/) **0.20.0** (Apache-2.0),
pinned by wheel sha256 (`VERSION`). The paper runs used a handful of local modifications.
This directory reproduces them **byte for byte** on a pristine install, so no vendored copy
of Harbor is needed.

## Profiles

A *profile* is the exact content of the `harbor/` package used by a set of runs.
Every profile differs from the wheel in at most four files; the other 392 files are untouched.

| profile | files changed vs 0.20.0 | used by |
|---|---|---|
| `site` | `agents/installed/codex.py` (0001), `agents/terminus_2/tmux_session.py` (0002), `environments/docker/docker.py` (0007) | Editing (all), Optimization (all; plus the runner-side resume overlay, see below), Creation builders that ran without an overlay |
| `creation` (default) | `site` + `agents/installed/base.py` (0003), `codex.py` (0004), `trial/single_step.py` (0005) | Creation held-out scoring, Creation Discussion runs, AgentSWE-Lite Creation |
| `creation-0902` | `site` + `single_step.py` (0005) | historical: Creation Qwen builders |
| `creation-glm-0903` | `creation-0902` + `agents/installed/claude_code.py` (0006) | historical: GLM-5.3 + Claude Code builder |
| `pristine` | none | restore / reference |

`site` in the `creation*` rows means `site` as the paper ran it (0001, 0002): 0007 is a release fix in
`site` only (below).

Because `codex.py` differs between `site` and `creation`, reproduce the paper exactly with
**two Harbor environments**: `venv-site` (Editing, Optimization) and `venv-creation` (Creation).
`patches/manifest.json` → `profiles.*.used_by` lists the runs behind each profile.

## Patches (`patches/series/`, review form; the installer copies full files)

| id | file | what | category | upstream |
|---|---|---|---|---|
| 0001 | codex.py | upload an explicit provider `config.toml` from `CODEX_CONFIG_TOML_PATH` | AgentSWE feature | native `Codex(config=...)` since 0.21.0 |
| 0002 | tmux_session.py | `env K=V tmux new-session` instead of `-e` (tmux < 3.2 in TerminalBench images) | correctness | not fixed in 0.23.0 |
| 0003 | base.py | Codex "stream disconnected before completion" → `ApiConnectionClosedError` | correctness | not in 0.23.0 |
| 0004 | codex.py | resume the single root Codex thread by id instead of `resume --last` | correctness | not in 0.23.0 |
| 0005 | single_step.py | opt-in in-place builder resume after transient API/network errors (`AGENTSWE_INFRA_RESUME_*`) | AgentSWE feature | none (job retries start a fresh trial) |
| 0006 | claude_code.py | `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` from the agent env | AgentSWE feature | equivalent since 0.21.0 / 0.22.0 |
| 0007 | docker.py | no resources-override `cpus` for a `main` service whose task compose sets `cpu_quota`/`cpu_period` | correctness (release) | candidate |

0007 is not in the paper trees. Every Editing task writes its Builder compose with
`cpu_quota: 800000` / `cpu_period: 100000` (8 CPUs, checked by the task's `builder_resources.py`), and
Harbor's resources override adds `cpus: 8` from `task.toml`. Docker Compose 2.40 sends `cpus` as
NanoCPUs next to the CPU period, and the Docker daemon refuses the container ("Conflicting options:
Nano CPUs and CPU Period cannot both be set"). Compose v2.5.0 (the system plugin on hosts 123 and 27)
ignores `cpus`, and since the daemon refuses the combination, every Editing Builder that has started so
far ran with quota/period only (NanoCPUs 0). With 0007 every compose version starts that container. A
compose without `cpu_quota`/`cpu_period` (every Creation and Optimization one) still gets `cpus`.

None of the patches touches verifier, reward or scoring code. They affect how the builder
is configured (0001, 0006), how a TerminalBench session starts (0002), and what happens
after a transient provider failure during the builder phase (0003-0005), and whether a builder
container that sets its own CPU quota starts (0007).

Optimization's builder resume is **not** a venv patch: `runners/optimization/harbor_overlay/`
(`sitecustomize.py`, `optimization_harbor_resume.py`, `harbor/trial/single_step.py`) is put on
`PYTHONPATH` and activates only when `OPTIMIZATION_INFRA_RESUME=1`, exactly as in the paper runs.

## Install

```bash
python3 -m pip download harbor==0.20.0 --no-deps -d wheels/
echo "$(sed -n 's/^wheel_sha256=//p' VERSION)  wheels/harbor-0.20.0-py3-none-any.whl" | sha256sum -c -
uv venv --python 3.12 "$AGENTSWE_HOME/harbor/venv-creation"
VIRTUAL_ENV="$AGENTSWE_HOME/harbor/venv-creation" uv pip install \
    wheels/harbor-0.20.0-py3-none-any.whl -c constraints-0.20.0.txt
python3 harbor_patch.py apply  --venv "$AGENTSWE_HOME/harbor/venv-creation" --profile creation
python3 harbor_patch.py verify --venv "$AGENTSWE_HOME/harbor/venv-creation" --profile creation
```

`constraints-0.20.0.txt` pins the 83 packages of the paper hosts' Harbor environment
(identical on all hosts). Without it a fresh resolve today pulls newer `litellm`, `openai`,
`filelock` (3 → 4) and more, which is a silent environment change.

`apply` refuses to touch a tree with any byte it does not recognise (every file is checked
against the wheel or a known profile), is idempotent, and can switch profiles or restore
`pristine`. `verify --root <dir>` checks any directory that contains `harbor/`.
`tests/check_harbor_behavior.py --expect <profile>` exercises the patched behaviour without
Docker or a model.

## Egress sidecar on hosts without Docker Hub

Harbor builds its egress-control sidecar (`FROM gogost/gost@sha256:...`) on first use.
Where Docker Hub is unreachable, pre-seed it instead of patching Harbor:

```bash
"$AGENTSWE_HOME/harbor/venv-creation/bin/python" tools/seed_egress_sidecar.py --registry "$AGENTSWE_DOCKER_REGISTRY"
```

It builds the same digest-pinned base through the mirror and tags it with the exact
content-addressed name Harbor looks up.

## Moving to a newer Harbor

0001 and 0006 have native equivalents from 0.21.0/0.22.0; 0002-0004 are upstream candidates;
0005 is meant to move into the AgentSWE Codex agent class. A Harbor upgrade changes builder
execution and must be treated as a protocol change (re-run readiness, report separately).
