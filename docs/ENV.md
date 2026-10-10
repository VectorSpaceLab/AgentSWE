# Configuration and host notes

All settings live in `.env` (git-ignored; start from `.env.example`) or the process environment,
which takes precedence. `python3 -m agentswe config` prints the resolved values without keys.

## Model roles

| Role | Used for | Variables |
|---|---|---|
| BUILDER | the coding agent under evaluation (Codex CLI) | `AGENTSWE_BUILDER_{BASE_URL,API_KEY,WIRE,MODEL,EFFORT}` |
| RUNTIME | the model a built or edited agent calls during evaluation, only via the evaluator broker | `AGENTSWE_RUNTIME_{...}` |
| JUDGE | Result judges, user simulators, LLM graders | `AGENTSWE_JUDGE_{...}` |
| SEARCH | Serper-compatible search (tier-2 tasks) | `AGENTSWE_SEARCH_{BASE_URL,API_KEY}` |

In Creation and Optimization runs every model call goes through a broker that holds the role's key; an Editing run
gives its Builder the key itself ("Where the keys are", below). See `docs/ENV.providers.md` for wires, effort
spellings and the protocol constants that are not configurable.

`BASE_URL` is the OpenAI-style base including `/v1`; the Responses endpoint is `BASE_URL + /responses`.
Unset `BASE_URL`, `API_KEY` or `WIRE` fall back to `AGENTSWE_DEFAULT_*`.

Keys can stay in a credential file you already have:
`AGENTSWE_DEFAULT_API_KEY=@file:/path/to/credentials.env#VARIABLE_NAME`.

### Where the keys are

- **Creation and Optimization.** The CLI writes each key a run needs to a 0600 file under
  `AGENTSWE_HOME/secrets/<run_id>/` and deletes these files when the run's controller exits. Codex, the coding
  agent under test, reaches the model through the run's builder broker (`broker/`, a container that mounts the
  BUILDER key file) with the placeholder token `broker-only-placeholder`. The RUNTIME key goes only to brokers: in
  Creation to the runtime provider broker (the JUDGE key to the judge provider broker), in Optimization to the task's
  evaluator (`evaluator.env`), whose brokers make the model calls of the agents under test. Built candidates,
  Result judges and Optimization agents under test hold placeholder tokens only.
- **Editing.** All three roles use one provider key (`agentswe run` refuses different keys). The CLI writes it to a
  single 0600 file, `AGENTSWE_HOME/secrets/editing/credential.env`, shared by the home's Editing runs;
  `agentswe result` and `agentswe stop` delete it once no other Editing run in the home is live. The lower-agent and
  Result-judge brokers mount that file read-only, and the edited product and its lower agents get the placeholder
  token `broker-only-placeholder`. The Builder has no broker: as in the paper's Editing protocol, Codex calls the
  provider with the real key (through the CONNECT proxy of "Editing runs" when the run uses it). The task's Builder
  harness writes the key to a 0600 `auth.json` (`auth_mode apikey`) in a tmpfs directory under `/dev/shm`, and
  Harbor uploads that file into the Builder container as `/tmp/codex-secrets/auth.json`. **During the Builder
  session, Codex and every command it runs in that container can read the key.** The host copy is deleted when the
  Builder session ends, and Harbor removes the container copy (best effort) after Codex exits. Each run says so in
  its `builder_transport.json`: `"transport": "native_codex_direct"`, `"credential_method": "Harbor
  CODEX_AUTH_JSON_PATH"`, `"builder_broker_started": false`.

Give BUILDER a key you can revoke, used only for AgentSWE and ideally with a spending limit; for Editing that is the
key of all three roles. Revoke it after the runs if Builder logs or workspaces leave the host.

## Search credentials

The search-enabled tasks (tier T2 in `agentswe list`: Web research, PPTX, BrowseComp) need both
`AGENTSWE_SEARCH_BASE_URL` and `AGENTSWE_SEARCH_API_KEY`; neither has a default, and the key never falls back to a
model key. For Serper:

```dotenv
AGENTSWE_SEARCH_BASE_URL=https://google.serper.dev
AGENTSWE_SEARCH_API_KEY=...            # or @file:/path/to/credentials.env#SERPER_API_KEY
```

`doctor <task>` fails a T2 task when either is missing, and `probe-roles` sends one search query through the search
broker. `AGENTSWE_SEARCH_WIRE=legacy-proxy` is only for a provider that speaks the published runs' own request shape
(`docs/ENV.providers.md`).

Built agents never receive a real search key: they call the evaluator's search broker with the
placeholder token `search-placeholder`, and the broker holds `AGENTSWE_SEARCH_API_KEY`. Note for
comparisons with the published results: in the published runs, candidates of the search-enabled
Creation tasks were given the real search token directly.

Candidates reach the broker through a per-run TLS front named `search.example.com`, whose certificate is
signed by a per-run CA. The candidate container trusts that CA through `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS`. The bundle is also bind-mounted read-only over the default trust
stores: the system bundle, the OpenSSL capath entry, and every certifi `cacert.pem` and conda `ssl/cert.pem` in the
mounted environment prefixes. A client that ignores those variables therefore verifies the front too, as every
client verified the publicly trusted endpoint in the published runs. The launch manifest's search record lists
the overridden paths and the bundle sha256.

## Editing runs

`agentswe setup` runs one at a time per `AGENTSWE_HOME`: a second setup in the same home logs "another setup is
running in <home>; waiting" and starts when the first ends. An Editing setup keeps the rendered control plane
(`editing/control`, `editing/tools`) as installed when the release renders it the same, and refuses to replace it, or
a task tree, while an Editing run that uses it is live in the home: wait for the run to finish or use another
`AGENTSWE_HOME`.

`agentswe run <editing task> --builder codex` starts a formal run directly: the five-hour Builder session, then the
six hidden cases. Before launching it checks that the task tree rendered under `AGENTSWE_HOME` still equals the
snapshot `setup` recorded and that the release template is unchanged, and refuses otherwise (run `agentswe setup`
again). `--smoke` runs the shorter readiness profile (one dev case, two submissions, one hidden case) and is a
useful first check that the host runs the whole pipeline; it is optional. A smoke keeps some of the evaluator's
exited containers for inspection; `agentswe stop <run_id>` removes them. `setup` prints `audit_readiness exit 2
(informational ...)` and smoke summaries say `admission_required: true`: both are about the readiness admission
above, which a release install does not need. A smoke's summaries say `result_axis`
"N/A"; `agentswe result` adds a `smoke` block read from the run dir: each dev round's classification and validity
(`valid`, from the tree's own validity field, which `valid_field` names), the held-out case's state and judged Result
(`result_score`, from the readiness result contract; a score elsewhere in the summaries, such as OpenHands'
`pilot_evaluation`, is a deterministic pre-judge check), the party a failure is attributed to, the contract paths,
and the reason when the run stopped at the infrastructure gate (from the summaries, or from
`builder_observer_events.jsonl` when the last submission was not consumed because of an infrastructure failure, as
in a Claude smoke that ended unfrozen). The readiness admission that the
published formal runs required (a smoke whose evidence is reviewed and admitted per task and host) is a
benchmark-construction gate and is not needed for a release install; `AGENTSWE_EDITING_REQUIRE_ADMISSION=1`
restores it.

In formal runs the Builder's model traffic leaves the host through an HTTP CONNECT proxy on `127.0.0.1:7890`, as
on the published runs' hosts: each task's Builder relay accepts only a CONNECT to the configured provider and
forwards it to that port. In `--smoke` runs only OpenWiki, OpenHands and OpenClaw use the port; the other Editing
tasks' Builders then connect to the provider directly. Before launching an Editing run, `agentswe run` checks the port. A proxy already listening there (Clash,
mihomo, gost or another HTTP CONNECT proxy) is used as is; if nothing listens, agentswe starts its own
(`agentswe/loopback_proxy.py`: standard library only, CONNECT only, loopback only, direct egress) as the transient
systemd unit `agentswe-loopback-proxy`, which keeps running for later runs (`systemctl stop agentswe-loopback-proxy`
stops it); a port held by a service that does not answer CONNECT stops the run with a message. The launch manifest
records which proxy was used (`builder_proxy`), and `agentswe doctor <editing task>` reports the port's state.

The Builder container starts through Harbor (`AGENTSWE_HOME/harbor/venv-site`) and the host's own `docker compose`
plugin; the private plugin `setup` installs for older hosts is used by Creation runs and `doctor`. Each Editing task
limits its Builder to 8 CPUs with `cpu_quota`/`cpu_period`; Harbor patch 0007 (`third_party/harbor`) keeps Harbor's
`cpus` override out of such a compose, so the container starts under any compose version (Compose 2.40 otherwise
sends both, and the Docker daemon refuses the container).

### When the Editing Builder budget ends

A formal Editing run gives the Builder 5 hours and up to 5 accepted submissions (`task.json` `protocol`:
`builder_session_sec`, `max_dev_rounds`). Development ends when the Builder exits, uses up its submissions or uses up
its time; the last accepted submission is then frozen and scored on the six held-out cases, and a Builder with no
accepted submission scores 0.

Each task runs its held-out cases only after a Builder session that ended by itself or after its last submission.
When the 5 hours run out first, the run stops before the held-out cases: the unit ends `failed` (exit status 2) and
the summary's status is `builder_integration_incomplete`, `builder_lifecycle_incomplete` or
`formal_evidence_incomplete`, depending on the task. `agentswe result` then adds a `budget_exhausted` block with
`accepted_submissions`, `latest_accepted` (its number, and its id and digest where the task records them), the rule,
and `held_out: "not run"`:

- with no accepted submission: `scored: true`, `score: 0` and `score_basis`. The 0 follows from the rule, not from
  the finalizer, so the result has no `formal_result_publishable`.
- otherwise: `scored: false`, and `next` says that this release cannot yet freeze such a run, so it has no score.

The block appears only when the last Builder segment (`builder_segment_receipt.json`) ended on Harbor's agent
timeout (`AgentTimeoutError`) or within 10 minutes of the deadline, the only native Builder evidence error is the
missing terminal event, and no held-out case has started. Some tasks record this cut as Builder exit 125 (their trial
gate refuses any Harbor exception), which still counts. Smoke runs, runs that finished normally and runs stopped by
any other infrastructure failure get no block. The two manual freeze scripts of earlier releases
(`runners/editing/tools/manual_freeze_budget_exhausted.py`, `lite_manual_freeze.py`) were bound to specific recorded
runs and could not run in an install; they have been removed.

## When an evaluation fails on infrastructure

- Creation does not resume an evaluation that fails on infrastructure. A development submission whose evaluation
  fails that way does not use up a submission, and the builder may submit again. A held-out evaluation that fails
  that way ends the run without a score; `agentswe result` reports the failure and the last infrastructure error.
- Editing resumes a Builder session that an infrastructure failure cut off, in the same Codex session, at most twice
  and only within the time left in the 5-hour budget. A development evaluation that fails on infrastructure does not
  use up a submission. Held-out cases are not re-run.
- Optimization resumes the evaluation (next section).

## Optimization runs

An Optimization run resumes an evaluation that fails on infrastructure (a Harbor environment that does not start,
for example on a host with a slow Docker Hub) without limit, as in the paper's protocol: the run waits and starts
the same evaluation again rather than scoring the failure. `agentswe status <run_id>` shows, under `infrastructure`,
the current evaluation phase, how many times it has resumed, its last infrastructure error (type, short message and
the attempt's `stderr.log`), and where the controller logs are (`<run dir>/controller_logs`). A run that keeps
resuming will not finish on its own; `agentswe stop <run_id>` ends it.

## Release assets

Some setup inputs exist only in the AgentSWE release asset store (see `THIRD_PARTY.md` for the list and
licenses): the Editing environment archives (OpenClaw runtime, Dyad dependencies; the Codex cargo home is optional
because its recipe fetches the same crates), the Terminal-Bench dependency image, repository snapshots, wheels and
data files, and the OSWorld source, kernel, initrd and task files. The store is published as the assets of one
GitHub release of this repository, which is the default:

    AGENTSWE_RELEASE_ASSETS_URL=https://github.com/VectorSpaceLab/AgentSWE/releases/download/assets-v1

`setup` downloads each file once into `AGENTSWE_HOME/cache/downloads` (resumable, through `AGENTSWE_BUILD_PROXY`
when set), checks its size and sha256 against the pin in `task.json` or `env.json`, and stops on a mismatch. It
tries the release asset store first and a file's upstream locations after it; an upstream that sends no byte before
the connection times out is skipped at once.

A GitHub release has no directories, so each file is published under a flat name derived from its pin: the first
16 hex digits of its sha256, `__`, and its file name with every character outside `[A-Za-z0-9._-]` replaced by `_`
(`osworld/fixture-downloads/eval/<id>/New Large Language Models Gold.xlsx` is
`987ada3caca9919d__New_Large_Language_Models_Gold.xlsx`). The release's `release-assets-manifest.json` maps each
asset to its release path (the path the pins and `THIRD_PARTY.md` name), sha256 and size. A copy of the store kept
as a directory tree on another server or bucket, each file at its release path, works as well: setup requests flat
names when the URL contains `/releases/download/` and release paths otherwise, and
`AGENTSWE_RELEASE_ASSETS_LAYOUT=flat` or `tree` overrides that choice.

A directory that already holds the Editing archives can be named with `AGENTSWE_ENV_ARCHIVE_DIR` instead. A pinned
file placed by hand in `AGENTSWE_HOME/cache/downloads/` is used as is when its size and sha256 match the pin and it
is named `<first 16 hex digits of its sha256>__<file name>`, the file name being the last component of the pinned
path, unchanged (unlike the flat release-asset name above, no character is replaced:
`987ada3caca9919d__New Large Language Models Gold.xlsx`). Two large inputs are not in the store and come from their
upstream hosts only: the OSWorld VM image `Ubuntu.qcow2.zip` (12.3 GB, huggingface.co) and the Terminal-Bench
`install-windows-3.11` disk image `win311.img` (archive.org). On a host that cannot reach them, place them in the
download cache under the same form of name: `b795b6cd4c69b252__Ubuntu.qcow2.zip` and `58fb76014dccf13e__win311.img`.

OSWorld's evaluator-side files (gold files and post-configuration scripts) are pinned assets too, so
`DesktopEnv.evaluate()` reads them from the attempt's cache and the host needs no access to huggingface.co. Two
OSWorld cases still need internet access from inside the VM at evaluation (test_006 and test_019; see the task's
notes).

Publishing the store (maintainers): `python3 tools/release_assets.py build --store <tree copy> --out <dir>
--repo <org>/<repo>` writes
the upload set, the pinned files under their flat names with a README, the archive license notes and texts,
`SHA256SUMS` and the manifest (`tools/release_assets.py check` lists the names and fails on a collision).
`tools/publish_release_assets.sh <org>/<repo> assets-v1 <dir>` (bash, curl and python3; Linux or macOS) creates the
release if it is missing, uploads what is not there yet, skipping assets already present with the same size and
retrying failures, and compares the release's assets with the manifest. It reads the token from `GITHUB_TOKEN` (a
fine-grained token with Contents read and write on the repository, or a classic token with the `repo` scope;
`public_repo` suffices for a public repository) or from an authenticated `gh` (`gh auth token`), and never puts it on
a command line.

## Hosts with restricted network access

| Symptom | Setting |
|---|---|
| Docker Hub unreachable or slow (image builds sit at `load metadata for docker.io/...`; Optimization environments time out while starting) | a Docker daemon registry mirror (`registry-mirrors` in `/etc/docker/daemon.json`) covers every pull and build; without one, `AGENTSWE_DOCKER_REGISTRY=<Docker Hub mirror host>` makes setup's image builds and the Optimization templates name their `docker.io` base images (pinned by digest) through that mirror. Images that setup pulls by reference (for example the OSWorld provider image) still come through the daemon |
| ghcr.io unreachable (Terminal-Bench) | `AGENTSWE_GHCR_REGISTRY=<ghcr.io mirror host>`; `AGENTSWE_DEBIAN_MIRROR` for the Debian packages of one Terminal-Bench image |
| apt or PyPI slow inside the Terminal-Bench task images | the controller builds each case's task image during the run and installs its packages there; `AGENTSWE_TERMINALBENCH_APT_MIRROR=<mirror base>` rewrites `archive.ubuntu.com`, `security.ubuntu.com` and `deb.debian.org` to `<mirror base>/ubuntu`, `/debian` and `/debian-security`, so the base must serve all three (for example `http://mirrors.tuna.tsinghua.edu.cn`); `AGENTSWE_TERMINALBENCH_PIP_INDEX=<PyPI simple index>` (default `https://pypi.org/simple`) |
| Terminal-Bench `qemu-startup` / `qemu-alpine-ssh` images (Debian 11 base) | their apt sources always come from the Debian archive, whose bullseye security updates have left deb.debian.org and regular mirrors; `AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE=<archive base>` (default `http://archive.debian.org`, serving `/debian` and `/debian-security`), for example a mirror of it at `<mirror>/debian-archive` |
| github.com, PyPI or the apt archive unreachable from the Terminal-Bench containers | 34 of the 40 held-out and 5 of the 10 dev Terminal-Bench cases keep their upstream verifier `test.sh`, which installs its test tools when it runs: curl from the apt archive, uv with astral's installer (the script from astral.sh, which redirects to releases.astral.sh; the uv release archive from github.com), then pytest and the test packages from PyPI (pypi.org, files.pythonhosted.org). A verifier that fails there runs no test; the controller records that as an infrastructure failure, which is retried and shown under `infrastructure` in `agentswe status`, not as a 0. `AGENTSWE_TERMINALBENCH_GITHUB_DOWNLOAD_BASE=<mirror of the github.com release downloads>` reaches these verifiers as `UV_INSTALLER_GITHUB_BASE_URL`, so the installer fetches `<base>/astral-sh/uv/releases/download/<version>/<archive>` (uv 0.7.13; 0.8.14 for `financial-document-processor`; the current release for `install-windows-3.11`). astral.sh, releases.astral.sh and PyPI must still be reachable (with the base set, the installer takes the uv archive only from it): `AGENTSWE_TERMINALBENCH_PIP_INDEX` does not reach the verifiers, and `AGENTSWE_TERMINALBENCH_APT_MIRROR` reaches their apt only in the 26 of these 39 images whose Dockerfile runs apt (the rewritten apt sources stay in the image) |
| PyPI downloads stall | `AGENTSWE_PIP_INDEX_URL=<PyPI mirror>/simple/` |
| conda-forge slow | `AGENTSWE_CONDA_CHANNEL=<conda-forge mirror>` (package URLs in env specs are rewritten to it) |
| npm / Node.js downloads slow | `AGENTSWE_NPM_REGISTRY`, `AGENTSWE_NODE_DIST_URL` |
| apt inside image builds slow | `AGENTSWE_APT_MIRROR=<ubuntu mirror base>`; image builds first use their pinned apt snapshot (snapshot.ubuntu.com) and retry on the mirror only if that fails (package versions may then drift; setup records it) |
| GitHub release downloads stall | `AGENTSWE_DOCKER_PLUGIN_SOURCE=ubuntu` (compose/buildx from Ubuntu packages, `AGENTSWE_UBUNTU_MIRROR`, extracted, not installed); `AGENTSWE_PBS_BASE_URL=<mirror of the python-build-standalone release downloads>` (the Optimization Python image) |
| codeload.github.com unreachable | `AGENTSWE_GITHUB_CODELOAD=<mirror>` (tau3 and PinchBench runtime images; the Editing a0 source fetches when they fall back to the commit tarball) |
| git fetches of the Editing a0 sources hang | `AGENTSWE_A0_SOURCE=codeload` fetches the pinned commit as a tarball from codeload (each file is then checked against the task's `a0/en-upstream.sha256`); `AGENTSWE_A0_GIT_TIMEOUT` (seconds, default 900) bounds the git fetch before that fallback |
| Playwright or Chrome for Testing downloads stall | `AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST=<Playwright CDN mirror>` replaces `https://cdn.playwright.dev/dbazure/download/playwright` (the browsers of the GUI, PPTX, Web, Schema and PDF task environments; Dyad's ffmpeg zip); `AGENTSWE_CFT_BASE_URL=<Chrome for Testing mirror>` replaces `https://storage.googleapis.com/chrome-for-testing-public` (the trusted-browser image's Chrome; Dyad's Chrome and headless-shell zips). The Chrome for Testing zips and Dyad's ffmpeg zip are checked against pinned sha256 digests, so a mirror cannot change them; the Creation environments' `playwright install` checks that each pinned browser revision is present, not its bytes |
| uv's Python downloads stall (Editing) | `AGENTSWE_UV_PYTHON_INSTALL_MIRROR=<python-build-standalone mirror>` |

### Behind an HTTP proxy

`AGENTSWE_BUILD_PROXY=http://<proxy>` (with `AGENTSWE_BUILD_NO_PROXY`, default `localhost,127.0.0.1`) is used for
two kinds of setup download: the environment builds (`envs/`; they then run on the host network, so a proxy on
the host's loopback works) and the pinned files setup fetches on the host with curl (release assets, the OSWorld VM
image, Terminal-Bench task assets, the large inputs some `env.json` files list). Every one of these downloads is
checked against a pinned sha256, so the proxy cannot change content. It is not used for:

- the image builds under `images/` (Docker builds on the default bridge network): give them the mirrors above
  (`AGENTSWE_DOCKER_REGISTRY`, `AGENTSWE_PBS_BASE_URL`, `AGENTSWE_GITHUB_CODELOAD`, `AGENTSWE_CFT_BASE_URL`,
  `AGENTSWE_NPM_REGISTRY`, `AGENTSWE_NODE_DIST_URL`, `AGENTSWE_PIP_INDEX_URL`);
- Docker's own pulls, which follow the daemon's proxy or registry-mirror configuration;
- setup's own tool downloads (micromamba, the compose/buildx plugins, Harbor's `pip install`), which honour the
  usual `https_proxy` / `http_proxy` variables of the shell that runs `agentswe setup`;
- model and search traffic during runs: the brokers ignore ambient proxy variables and connect directly, or
  through `AGENTSWE_EVALUATOR_PROXY_URL` (an HTTP proxy on the host's loopback only); an Editing Builder's traffic
  goes through the CONNECT proxy on `127.0.0.1:7890` (Editing runs, above).

### Downloads with fixed URLs

A few inputs listed under `downloads` in an `env.json` or `images.json` are fetched on the host from fixed URLs that
the mirror settings above do not rewrite: the PPTX environment's three conda packages (conda.anaconda.org) and two
Ubuntu packages (archive.ubuntu.com), the `pptx-libreoffice` image's LibreOffice tarball
(downloadarchive.documentfoundation.org) and one Ubuntu package, and the Lean toolchain of `formal-theorem-proving`
(github.com). They go through `AGENTSWE_BUILD_PROXY` when it is set. On a host that reaches none of these, fetch each
file elsewhere and place it in `AGENTSWE_HOME/cache/downloads/` under the plain `name` of its `downloads` entry, with
no sha256 prefix (release assets and the two upstream-only files of "Release assets" use
`<first 16 hex digits of its sha256>__<file name>` instead); setup checks it against the pinned sha256 and uses it
as is.

### Inputs pinned by version only

Release assets and every `downloads` entry are checked against a sha256, and base images are pinned by digest. A
few build inputs are pinned by version only, so an upstream republish or a mirror could change their bytes:

- the Playwright browsers of the Creation environments (`playwright install` checks each pinned revision is
  present);
- `uv` in the `opt-tau3-runtime` image (`pip install uv==<version>`);
- `python3` from apt in the test images of the `desktop-gui-automation` and `evidence-grounded-document-qa`
  templates, and `ca-certificates`/`curl` in the fetch stages of the `opt-tau3-runtime` and
  `opt-pinchbench-runtime` images (from archive.ubuntu.com, or `AGENTSWE_APT_MIRROR`); the fetch stages are
  discarded, so the runtime images themselves do not contain them.
- the Terminal-Bench task images, which the controller builds from the upstream task Dockerfiles during a run,
  as Terminal-Bench does: their apt and pip steps install the versions current at build time. Seven held-out
  cases start from floating base tags (`python:3.11`, `python:3.11-slim`, `python:3.10-slim-bookworm`,
  `debian:13.0-slim`, `debian:bullseye-slim`, `ubuntu:24.04`); `mteb-leaderboard` pins only `mteb` itself; and
  `custom-memory-heap-crash` downloads the gcc source from github.com inside its build. The cases on the pinned
  Terminal-Bench base images install their test dependencies from the pinned wheelhouse.
- the test tools of the other Terminal-Bench verifiers, installed when the verifier runs: in 34 of the 40 held-out
  and 5 of the 10 dev cases, curl from the apt archive, uv from github.com with astral's installer (uv 0.7.13;
  0.8.14 for `financial-document-processor`; whatever release is current for `install-windows-3.11`), then pytest
  and the test packages from PyPI (pinned by version except `install-windows-3.11`'s pytest); `hf-model-inference`
  and `fix-code-vulnerability` install pytest from PyPI with pip. The uv installer checks no checksum, so a mirror
  named by `AGENTSWE_TERMINALBENCH_GITHUB_DOWNLOAD_BASE` must serve the release's own files. A container that
  cannot reach these gets an infrastructure failure (retried, and shown in `agentswe status`), not a 0.

## Docker address pools

Runs create small Docker networks from address pools; give each `AGENTSWE_HOME` pools that no other workload on
the host allocates from (another AgentSWE home included) and that do not overlap the host's routes:

| Variable | Default | Used by |
|---|---|---|
| `AGENTSWE_NETWORK_POOL` | `10.246.0.0/16` | Creation runs (per-job /28 networks) |
| `AGENTSWE_OPTIMIZATION_POOL` | `198.18.0.0/15` | every Optimization run (/28 networks of the compose templates) |
| `AGENTSWE_TAU3_POOL` | `198.18.0.0/15` | tau3-retail controller |
| `AGENTSWE_PINCHBENCH_POOL` | `100.64.0.0/10` | pinchbench-openclaw controller |

The defaults are benchmarking and shared-address ranges that some networks route (DHCP host routes, VPNs such as
Tailscale use 100.64.0.0/10); `doctor <task>` warns when a pool the task uses overlaps a host route or an existing
Docker network.

## Where a run's logs are

`agentswe run` prints the run id and its launch log. `agentswe status` gives each run's directory; under it,
`controller_logs/` (Creation, Optimization) holds the evaluator's log for every attempt, and Harbor's job
directories are under `AGENTSWE_HOME/jobs/`. An Editing run's directory holds its readiness or formal records;
`agentswe result` summarizes them.

## Host checks

`agentswe doctor <task>` checks Docker, cgroup version, compose >= 2.20 and buildx (setup installs
private plugins under `AGENTSWE_HOME` if needed), disk space, the docker0 address used for the
evaluator broker, overlap of `AGENTSWE_NETWORK_POOL` (and, for Optimization tasks, of the pools above) with existing
routes and Docker networks, the configured roles, whether each role's base URL answers at all (a request without
the key, so HTTP 401 is expected), and task-specific host tools. It sends no model request: `agentswe probe-roles`
sends one short request per role with its key (and one search query when search is configured).

`AGENTSWE_MAX_CONCURRENT_RUNS` (default 4) caps live runs per `AGENTSWE_HOME` on a host; more
concurrent Optimization runs created enough per-case Docker networks to trigger a udevd storm.
