# Third-party material

| Component | License | How it is used |
|---|---|---|
| Harbor 0.20.0 | Apache-2.0 | installed from PyPI; four files patched (`third_party/harbor/`) |
| Codex CLI 0.144.1 (`@openai/codex`) | Apache-2.0 | installed from npm into the builder image |
| Node.js 24.6.0 | MIT and others | official binary distribution in the builder image |
| conda-forge packages | per package | trusted task environments, pinned by URL and md5 |

Upstream systems behind each task (requirement sources, editing targets, optimisation benchmarks)
are listed with their pinned commits and licenses in each `tasks/*/*/task.json` under `upstream`.
Editing targets are never redistributed; they are fetched by commit.

## Release assets (hosted separately, fetched by sha256 at setup)

Each asset is an unmodified copy of what an upstream build step downloads, kept so that setup does not depend on
hosts that drift or that some networks cannot reach. Licenses are those of the upstream source. Assets are named
here by their release path; the GitHub release publishes each under a flat name (`docs/ENV.md`, Release assets),
and its `release-assets-manifest.json` maps one to the other.

| Asset | Upstream source | License |
|---|---|---|
| Editing environment archives (OpenClaw runtime, Dyad dependencies, Codex cargo home) | packages resolved by each upstream project's lockfile | per package; inventories and the reviewed copyleft or undeclared entries in `third_party/env-archives/`, GNU license texts in `third_party/licenses/` |
| `terminalbench/images/write-compressor.tar.gz` | the paper's locked dependency image for the upstream `write-compressor` task (Ubuntu packages and the task's Python dependencies) | per package (Debian/Ubuntu and PyPI licenses) |
| `terminalbench/task-assets/*/…-<commit>.tar.gz` | snapshots of github.com/ozkl/doomgeneric, github.com/openwall/john and github.com/bottlepy/bottle at the commits the upstream Dockerfiles clone | as upstream: doomgeneric GPL-2.0, John the Ripper GPL-2.0-or-later (some bundled files carry their own licenses, listed under its `doc/`), bottle MIT; each snapshot keeps its license files |
| `terminalbench/task-assets/fix-code-vulnerability/flit-wheels.tar.gz` | public PyPI wheels for flit 3.12.0 and its dependencies | per wheel metadata |
| `terminalbench/task-assets/alpine-3.19/alpine-extended-3.19.0-x86_64.iso` | dl-cdn.alpinelinux.org, as the upstream qemu tasks download it | Alpine Linux distribution terms (sources at alpinelinux.org) |
| `terminalbench/task-assets/train-fasttext/*.parquet` | huggingface.co/datasets/Yelp/yelp_review_full, as the upstream Dockerfile downloads it | the dataset's terms (Yelp); upstream Terminal-Bench also ships a derived test file in its repository |
| `terminalbench/task-assets/common/uv-0.12.1` | the astral-sh/uv 0.12.1 release binary | MIT OR Apache-2.0 |
| `osworld/osworld-source-091f5ef1.tar` | xlang-ai/OSWorld at 091f5ef1 with its two submodules | Apache-2.0 |
| `osworld/boot/{vmlinuz,initrd.img}-6.5.0-15-generic` | extracted from `/boot` of the pinned OSWorld Ubuntu VM image | Linux kernel GPL-2.0 (Ubuntu kernel sources at ubuntu.com) |
| `osworld/fixture-downloads/**` | task files the OSWorld task configs download (setup and evaluator files, mostly from the `xlangai` datasets on the Hugging Face Hub) | as published by OSWorld upstream |
