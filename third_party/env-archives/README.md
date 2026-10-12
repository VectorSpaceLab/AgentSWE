# Third-party contents of the Editing environment archives

Three Editing tasks install part of their paper environment from a pinned archive (`env/env.json`,
`env/build-host.sh`; taken from `AGENTSWE_ENV_ARCHIVE_DIR` or fetched by `setup` from the release asset store,
where an archive's release path is `editing/env-archives/<name>` and its GitHub release asset is
`<first 16 hex digits of its sha256>__<name>`; the Codex cargo home is optional, its recipe fetches the same
crates). Other Editing environments are rebuilt from public registries by their recipes.
The archives hold unmodified third-party packages as resolved by each upstream project's lockfile; they are
distributed as release assets, not in this repository. This directory lists what they contain.

| Archive | Task | Size | sha256 | Contents | Most common licenses |
|---|---|---|---|---|---|
| `openclaw-channel-handoff-ledger-edit-v1.runtime.tar.zst` | openclaw-channel-handoff | 1426 MB | `1c8100b53446d7b4…` | 1471 npm packages | MIT 1082, Apache-2.0 155, ISC 120, BSD-3-Clause 35, BSD-2-Clause 22 |
| `dyad-cycle-006-deps.tar.zst` | dyad-acceptance-driven | 398 MB | `f5f0ae6df4c5dd1c…` | 1515 npm packages | MIT 1258, ISC 91, Apache-2.0 88, BSD-2-Clause 23, BSD-3-Clause 18 |
| `codex-project-memory-edit-v1.cargo-home.tar.zst` | codex-execution-residual | 511 MB | `629db2d6482a2fcd…` | 1248 cargo packages | MIT OR Apache-2.0 535, MIT 247, Apache-2.0 OR MIT 88, None 81, Apache-2.0 65 |

`<archive>.licenses.json` lists every package (ecosystem, name, version, declared license, path inside the
archive) and a `review` list of packages whose declaration is copyleft, dual or missing. Inventory method: every
`node_modules/<name>/package.json` (`license`, or the legacy `licenses` array), every `Cargo.toml` under the
cargo registry and git sources, every Python `*.dist-info/METADATA`; read from the archives as published
(2026-10-06).

## Packages reviewed

| Archive | Package | Declared license | Note |
|---|---|---|---|
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | openclaw@None | (none declared) | the `plugin-sdk` stub of OpenClaw itself inside its built `dist/extensions` (OpenClaw is MIT) |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | @img/sharp-libvips-linux-x64@1.3.2 | LGPL-3.0-or-later | prebuilt libvips binary; LGPL-3.0-or-later per package.json and README; no license file in the package (texts in `third_party/licenses/`); source: https://github.com/lovell/sharp-libvips and https://github.com/libvips/libvips |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | @shinyoshiaki/jspack@0.0.6 | (none declared) | no `license` field; the package ships a LICENSE file |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | codec-parser@2.5.0 | LGPL-3.0-or-later | LGPL-3.0-or-later, JavaScript source shipped as is with its LICENSE |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | dompurify@3.4.12 | (MPL-2.0 OR Apache-2.0) | dual MPL-2.0 OR Apache-2.0; used under Apache-2.0 |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | jszip@3.10.1 | (MIT OR GPL-3.0-or-later) | dual MIT OR GPL-3.0-or-later; used under MIT |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | libsignal@6.0.0 | GPL-3.0 | GPL-3.0, JavaScript source shipped as is with its LICENSE; an aggregate in the archive, not linked into this repository's code |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | lightningcss@1.33.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | lightningcss-linux-x64-gnu@1.33.0 | MPL-2.0 | prebuilt native binary of lightningcss (MPL-2.0) with its LICENSE; source: https://github.com/parcel-bundler/lightningcss |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | mediabunny@1.51.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | rx.mini@1.4.0 | (none declared) | no license declared in package.json, README or files; redistributed unmodified as upstream OpenClaw's lockfile pins it |
| `openclaw-channel-handoff-ledger-edit-v1.runtime` | web-push@3.6.7 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `dyad-cycle-006-deps` | @dyad-sh/supabase-management-js@1.0.1 | (none declared) | no license declared and no license file; redistributed unmodified as upstream Dyad's lockfile pins it |
| `dyad-cycle-006-deps` | @img/sharp-libvips-linux-x64@1.2.0 | LGPL-3.0-or-later | as above (LGPL-3.0-or-later; texts in `third_party/licenses/`; source: https://github.com/lovell/sharp-libvips) |
| `dyad-cycle-006-deps` | @img/sharp-libvips-linuxmusl-x64@1.2.0 | LGPL-3.0-or-later | as above (LGPL-3.0-or-later; texts in `third_party/licenses/`; source: https://github.com/lovell/sharp-libvips) |
| `dyad-cycle-006-deps` | @vercel/sdk@1.18.0 | (none declared) | no `license` field; the package ships a LICENSE file |
| `dyad-cycle-006-deps` | browser-assert@1.2.1 | (none declared) | no `license` field; the package ships a LICENSE file |
| `dyad-cycle-006-deps` | we@None | (none declared) | test fixture of `get-package-info` (`get-package-info/test/node_modules/we`), not a separate component |
| `dyad-cycle-006-deps` | lightningcss@1.30.1 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `dyad-cycle-006-deps` | lightningcss-linux-x64-gnu@1.30.1 | MPL-2.0 | prebuilt native binary of lightningcss (MPL-2.0) with its LICENSE; source: https://github.com/parcel-bundler/lightningcss |
| `dyad-cycle-006-deps` | lightningcss-linux-x64-musl@1.30.1 | MPL-2.0 | prebuilt native binary of lightningcss (MPL-2.0) with its LICENSE; source: https://github.com/parcel-bundler/lightningcss |
| `dyad-cycle-006-deps` | emitter-component@1.1.2 | (none declared) | no license declared and no license file; redistributed unmodified as upstream Dyad's lockfile pins it |
| `dyad-cycle-006-deps` | underscore@1.4.4 | (none declared) | no `license` field; the package ships a LICENSE file (MIT) |
| `codex-project-memory-edit-v1.cargo-home` | nucleo@0.5.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | nucleo-matcher@0.3.1 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | option-ext@0.2.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | r-efi@5.3.0 | MIT OR Apache-2.0 OR LGPL-2.1-or-later | triple MIT OR Apache-2.0 OR LGPL-2.1-or-later; used under MIT or Apache-2.0 |
| `codex-project-memory-edit-v1.cargo-home` | r-efi@6.0.0 | MIT OR Apache-2.0 OR LGPL-2.1-or-later | triple MIT OR Apache-2.0 OR LGPL-2.1-or-later; used under MIT or Apache-2.0 |
| `codex-project-memory-edit-v1.cargo-home` | self_cell@1.2.2 | Apache-2.0 OR GPL-2.0-only | dual Apache-2.0 OR GPL-2.0-only; used under Apache-2.0 |
| `codex-project-memory-edit-v1.cargo-home` | symphonia@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-bundle-mp3@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-common@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-core@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-format-isomp4@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-format-mkv@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-format-ogg@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-format-riff@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | symphonia-metadata@0.6.0 | MPL-2.0 | MPL-2.0 (file-level copyleft), source shipped as is with its LICENSE |
| `codex-project-memory-edit-v1.cargo-home` | 81 crates without a license field | (none declared) | sub-crates inside three git checkouts (test data, benches, fuzz targets and internal tools: rules_rust 78, nucleo 2, tungstenite-rs 1), covered by their repository's license |

Every package is redistributed unmodified, in the form its upstream publishes (JavaScript and Rust sources;
the sharp-libvips packages are upstream's prebuilt binaries). The GPL-3.0 and LGPL-3.0 components are separate
packages in an aggregate archive and are not linked into this repository's code. Their license texts travel
with each package except sharp-libvips, whose LGPL-3.0 terms are stated in its package.json and README; the
GNU license texts (copied from the `libsignal` and `codec-parser` packages in the OpenClaw archive) are in
`third_party/licenses/` and accompany the archives in the release asset store.
