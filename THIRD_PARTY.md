# Third-party material

AgentSWE's own code and task files are under Apache-2.0 (`LICENSE`). This file lists the third-party software the
evaluation uses, the upstream material this repository contains, and the upstream copies in the release asset store.
License texts that apply to copies here are in `LICENSE` (Apache-2.0) and `third_party/licenses/`.

## Software the evaluation uses

| Component | License | How it is used |
|---|---|---|
| Harbor 0.20.0 | Apache-2.0 | installed from PyPI; seven patches to six files (`codex.py`, `tmux_session.py`, `docker.py`, `base.py`, `single_step.py`, `claude_code.py`), at most four in one profile (`third_party/harbor/`); `runners/optimization/harbor_overlay/harbor/trial/single_step.py` is a modified copy of Harbor's file for the Optimization builder resume |
| Codex CLI 0.144.1 (`@openai/codex`) | Apache-2.0 | installed from npm into the builder image |
| Node.js 24.6.0 | MIT and others | official binary distribution in the builder image |
| conda-forge packages | per package | trusted task environments, pinned by URL and md5 |

## Upstream material in this repository

Each task's `task.json` names its upstream repository, pinned commit and license under `upstream`.

Optimization tasks evaluate on upstream benchmarks and keep the upstream task material they need in the repository:

| Material | Upstream (pinned commit) | License |
|---|---|---|
| `tasks/optimization/browsecomp/encrypted/` (the BrowseComp rows, encrypted as upstream distributes them) and the BrowseComp grader template in `runners/optimization/eval-template/solution/run_eval.py` | openai/simple-evals (652c89d0) | MIT; notice in `third_party/licenses/upstream-MIT-notices.txt` |
| `tasks/optimization/tau3-retail/` (retail task ids and digests) | sierra-research/tau2-bench (668d3bcd) | MIT; notice in `third_party/licenses/upstream-MIT-notices.txt` |
| `tasks/optimization/pinchbench-openclaw/` (task ids, categories and digests) | pinchbench/skill (819384ae) | MIT; notice in `third_party/licenses/upstream-MIT-notices.txt` |
| `tasks/optimization/terminalbench/benchmark/evaluator/native_tasks/` (50 task trees, byte-identical to upstream, including the `train-fasttext` test file that upstream derives from the Yelp review dataset) and the task instructions in the benchmark | laude-institute/terminal-bench (d28711d0) | Apache-2.0 |
| OSWorld task configs (`tasks/optimization/osworld/benchmark/evaluator/task_metadata.json`, `runners/optimization/optimization_native/osworld_images/task_metadata.json`) and the task instructions in the case inputs | xlang-ai/OSWorld (091f5ef1) | Apache-2.0 |

Editing targets are not in the repository or the release: setup fetches each by commit. Two Editing tasks ship a
reference solution patch against their target, which is a modification of the upstream code under its license:
`tasks/editing/dyad-acceptance-driven/tree/solution.patch` (dyad-sh/dyad c7896a72, Apache-2.0; the patch does not
touch `src/pro`) and `tasks/editing/deeptutor-adaptive-remediation/tree/delivery/solution.patch` (HKUDS/DeepTutor
37c3db6d, Apache-2.0 at that commit).

Creation tasks take their requirements from an upstream project and copy no upstream code or assets:

| Creation task | Requirement source (pinned commit) | License |
|---|---|---|
| `authorized-vulnerability-validation` | usestrix/strix (b313d78f) | Apache-2.0 |
| `database-analytics` | vanna-ai/vanna (365d0617) | MIT |
| `desktop-gui-automation` | bytedance/UI-TARS-desktop (c2ad42e3) | Apache-2.0 |
| `document-to-editable-pptx` | hugohe3/ppt-master (17d4d86c) | MIT |
| `evidence-grounded-document-qa` | Future-House/paper-qa (d7675d7b) | Apache-2.0 |
| `formal-theorem-proving` | lean-dojo/LeanCopilot (no commit recorded) | MIT |
| `repository-bug-repair` | SWE-agent/SWE-agent (3ea751c0) | MIT |
| `schema-guided-web-extraction` | ScrapeGraphAI/Scrapegraph-ai (27d9d289) | MIT |
| `scientific-pdf-translation` | PDFMathTranslate/PDFMathTranslate (44c4d5b3) | AGPL-3.0 |
| `web-research-report` | MiroMindAI/miroflow (fca7b4a7) | Apache-2.0 |

## Release assets (hosted separately, fetched by sha256 at setup)

Each asset is an unmodified copy of what an upstream build step downloads, kept so that setup does not depend on
hosts that drift or that some networks cannot reach. Licenses are those of the upstream source. Assets are named
here by their release path; the GitHub release publishes each under a flat name (`docs/ENV.md`, Release assets),
and its `release-assets-manifest.json` maps one to the other. Some pinned inputs are not in the store and come from
their upstream hosts only (`docs/ENV.md`, Release assets): the OSWorld VM image, the Terminal-Bench
`install-windows-3.11` disk image, the Yelp review files of `train-fasttext`, and three OSWorld VLC task files.

| Asset | Upstream source | License |
|---|---|---|
| Editing environment archives (OpenClaw runtime, Dyad dependencies, Codex cargo home) | packages resolved by each upstream project's lockfile | per package; inventories and the reviewed copyleft or undeclared entries in `third_party/env-archives/`, GNU license texts in `third_party/licenses/` |
| `terminalbench/images/write-compressor.tar.gz` | the paper's locked dependency image for the upstream `write-compressor` task (Ubuntu packages and the task's Python dependencies) | per package (Debian/Ubuntu and PyPI licenses) |
| `terminalbench/task-assets/*/…-<commit>.tar.gz` | snapshots of github.com/ozkl/doomgeneric, github.com/openwall/john and github.com/bottlepy/bottle at the commits the upstream Dockerfiles clone | as upstream: doomgeneric GPL-2.0, John the Ripper GPL-2.0-or-later (some bundled files carry their own licenses, listed under its `doc/`), bottle MIT; each snapshot is the complete source tree and keeps its license files |
| `terminalbench/task-assets/fix-code-vulnerability/flit-wheels.tar.gz` | public PyPI wheels for flit 3.12.0 and its dependencies | per wheel metadata |
| `terminalbench/task-assets/alpine-3.19/alpine-extended-3.19.0-x86_64.iso` | dl-cdn.alpinelinux.org, as the upstream qemu tasks download it | Alpine Linux distribution terms (sources at alpinelinux.org) |
| `terminalbench/task-assets/common/uv-0.12.1` | the astral-sh/uv 0.12.1 release binary | MIT OR Apache-2.0 |
| `osworld/osworld-source-091f5ef1.tar` | xlang-ai/OSWorld at 091f5ef1 with its two submodules | Apache-2.0 |
| `osworld/boot/{vmlinuz,initrd.img}-6.5.0-15-generic` | copied unmodified from `/boot` of the pinned OSWorld Ubuntu 22.04 VM image | GPL-2.0 (kernel) and the licenses of the packages in the initrd; source below |
| `osworld/fixture-downloads/**` | task files the OSWorld task configs download (setup and evaluator files, mostly from the `xlangai` datasets on the Hugging Face Hub) | as published by OSWorld upstream |

### Source of the OSWorld kernel and initrd

- The kernel is Ubuntu's `linux-image-6.5.0-15-generic` (`6.5.0-15-generic #15~22.04.1-Ubuntu`), built from the
  Ubuntu source package `linux-hwe-6.5` version `6.5.0-15.15~22.04.1` (signed by `linux-signed-hwe-6.5` of the same
  version). Its complete corresponding source is that package,
  https://launchpad.net/ubuntu/+source/linux-hwe-6.5/6.5.0-15.15~22.04.1, or
  `apt-get source linux-hwe-6.5=6.5.0-15.15~22.04.1` on Ubuntu 22.04.
- The initrd was generated by `initramfs-tools` in the VM image and holds files from its Ubuntu 22.04 packages: the
  kernel modules of the same `linux-hwe-6.5` version, BusyBox `1:1.30.1-7ubuntu3`, udev from systemd
  `249.11-0ubuntu3.11`, and kmod, klibc, util-linux, e2fsprogs, ntfs-3g, fuse3, lvm2 (dmsetup), isc-dhcp-client,
  glibc, linux-firmware and the Intel and AMD microcode packages at the versions installed in the image (its
  `/var/lib/dpkg/status`). The corresponding source of each is the Ubuntu source package of that version at
  https://launchpad.net/ubuntu/+source/, or `apt-get source <package>=<version>` on Ubuntu 22.04.

The GPL-2.0 text is `third_party/licenses/GPL-2.0.txt`, published with the release as `GPL-2.0.txt`.
