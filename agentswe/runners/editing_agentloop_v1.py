"""Editing runner: the task's agentloop control plane (formal_one_stop) under systemd units.

setup (agentswe setup <editing task>):
  * renders runners/editing/{control,tools,state} and tasks/editing/*/tree into AGENTSWE_HOME/editing,
    replacing every @@AGENTSWE_*@@ token with a path under AGENTSWE_HOME (legacy tokens never point
    outside it), retargeting image tags to the agentswe-os/ images and unit names to agentswe-oss-*;
    readiness admissions survive, and other tasks' trees are re-rendered only when their template changed;
  * fetches the task's a0 by commit (AGENTSWE_EDITING_A0_LANG = en-upstream by default; zh-asrun is
    retained only for private paper reproduction);
  * builds host-side environments (build-host.sh) and, for DeepTutor, a fresh-install product guard;
  * rebinds the configuration registry and source snapshot; in release provenance mode (the default) binds the
    pre-repair snapshot to the release template trees, keeping the paper's as a record; then audits readiness.
run: --smoke dispatches the task's readiness canary (tools/launch_readiness.py, profile
  single-dev-two-round-hidden-smoke-v1); formal runs dispatch tools/launch_formal_task.py.
  Credentials: one 0600 file read only by the control plane's own brokers; deleted by `stop` or `result`.
  Builder egress: the task trees' Builder relay forwards CONNECT to 127.0.0.1:7890; before launching, the runner uses
  the HTTP CONNECT proxy listening there or starts the bundled one (agentswe/loopback_proxy.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import loopback_proxy, util
from ..config import REPO_ROOT, Config
from ..registry import Task
from . import editing_builder_model

FAMILY_DIR = "editing"
TASK_KEYS = {  # control-plane short task names
    "claude-policy-provenance": "claude", "aider-worktree-transaction": "aider",
    "openhands-effect-recovery": "openhands", "openclaw-channel-handoff": "openclaw",
    "codex-execution-residual": "codex", "ai-scientist-reproducibility-gate": "ai-scientist",
    "deepcode-claim-traceability": "deepcode", "deeptutor-adaptive-remediation": "deeptutor",
    "dyad-acceptance-driven": "dyad", "openwiki-change-impact": "openwiki",
}
# Claude and Dyad read their held-out specs from an evaluator-issued bundle, not from the tree: the readiness
# launcher (tools/launch_readiness.py HIDDEN_DIRS) names the paper's bundle of these labels, and the formal launcher
# copies it per run (launch_formal_task.py --issue-hidden-from). The release ships each bundle as
# tasks/editing/<id>/hidden_issued; setup installs the rendered copy where both launchers look.
ISSUED_HIDDEN = {"claude": "0920-hardened-003", "dyad": "0920-hardened-001"}


def editing_root(cfg: Config) -> Path:
    return cfg.home / "editing"


def credential_file(cfg: Config) -> Path:
    return cfg.home / "secrets" / "editing" / "credential.env"


def provenance_mode(cfg: Config) -> str:
    """release: formal gates check the installed trees against this install's own setup snapshot;
    paper: the original checks against the paper hosts' pre-repair sources (needs the paper archives)."""
    mode = cfg.get("AGENTSWE_EDITING_PROVENANCE_MODE") or "release"
    if mode not in ("release", "paper"):
        raise SystemExit("AGENTSWE_EDITING_PROVENANCE_MODE must be release or paper")
    return mode


def tokens(cfg: Config) -> dict[str, str]:
    e, h = editing_root(cfg), cfg.home
    return {
        "@@AGENTSWE_EDITING_TASKS@@": str(e / "tasks"),
        "@@AGENTSWE_EDITING_CONTROL@@": str(e / "control"),
        "@@AGENTSWE_EDITING_TOOLS@@": str(e / "tools"),
        "@@AGENTSWE_EDITING_RUNS@@": str(h / "runs" / FAMILY_DIR),
        "@@AGENTSWE_EDITING_STATE@@": str(e / "state"),
        "@@AGENTSWE_EDITING_SOURCES@@": str(e / "sources"),
        "@@AGENTSWE_CREDENTIAL_FILE@@": str(credential_file(cfg)),
        "@@AGENTSWE_ENVS@@": str(h / "envs"),
        "@@AGENTSWE_HARBOR_BIN@@": str(h / "harbor" / "venv-site" / "bin" / "harbor"),
        "@@AGENTSWE_PYTHON312_PREFIX@@": str(h / "harbor" / "python"),
        "@@AGENTSWE_PROVENANCE_MODE@@": provenance_mode(cfg),
        # Historical paths in evidence, documents and tests: rendered inside AGENTSWE_HOME so nothing
        # ever reads or writes the original hosts' evidence directories.
        "@@AGENTSWE_LEGACY_HARBOR@@": str(e / "legacy" / "harbor"),
        "@@AGENTSWE_LEGACY_DATA@@": str(e / "legacy" / "data"),
        "@@AGENTSWE_LEGACY_HOME@@": str(e / "legacy" / "home"),
        "@@AGENTSWE_LEGACY_SHARE@@": str(e / "legacy" / "share"),
        "@@AGENTSWE_LEGACY_NFS@@": str(e / "legacy" / "nfs"),
        "@@AGENTSWE_LEGACY_BENCHMARK@@": str(e / "legacy" / "benchmark"),
    }


def image_tokens(images: dict) -> dict[str, str]:
    """The Code judge image is pinned by content: the tag setup builds and the digest of its shipped context."""
    from ..setup import tree_digest
    judge = images["edit-code-judge"]
    return {"@@AGENTSWE_CODE_JUDGE_IMAGE@@": judge["tag"],
            "@@AGENTSWE_CODE_JUDGE_CONTEXT_SHA256@@": tree_digest(REPO_ROOT / judge["context"])}


def retargets(images: dict) -> dict[str, str]:
    return {
        "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812": images["builder-codex"]["tag"],
        "agentswe/edit-candidate-python311:0826": images["edit-candidate-python311"]["tag"],
        "'agentswe-edit-%s-%s'": "'agentswe-oss-edit-%s-%s'",
        "f'agentswe-formal-{task}-{a.label}'": "f'agentswe-oss-formal-{task}-{a.label}'",
    }


def built_image_tokens(s) -> dict[str, str]:
    """Image ids that trees pin (OpenHands isolated_runtime.PINNED_IMAGE, Dyad build_preflight.IMAGE): the paper pinned
    the id of its own builder image; a release install pins the id of the builder image its setup built."""
    record = s.state.get("images", {}).get("builder-codex", {})
    return {"@@AGENTSWE_BUILDER_CODEX_IMAGE_ID@@": record.get("id", "")} if record.get("id") else {}


# .env settings a host-side environment build may need (build-host.sh reads them from its environment)
HOST_BUILD_CONFIG = ("AGENTSWE_CONDA_CHANNEL", "AGENTSWE_NPM_REGISTRY", "AGENTSWE_NODE_DIST_URL",
                     "AGENTSWE_ENV_ARCHIVE_DIR", "AGENTSWE_BUILD_PROXY", "AGENTSWE_BUILD_NO_PROXY",
                     "AGENTSWE_CFT_BASE_URL", "AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST")  # Dyad: Chrome for Testing, ffmpeg
# .env settings the a0 fetch (a0/fetch.sh) reads: source (git or codeload), git timeout, codeload mirror
A0_FETCH_CONFIG = ("AGENTSWE_A0_SOURCE", "AGENTSWE_A0_GIT_TIMEOUT", "AGENTSWE_GITHUB_CODELOAD")
ENV_ARCHIVE_RELEASE_DIR = "editing/env-archives"  # the archives' release path (a tree store's directory)


def pinned_env_archives(meta: dict) -> list[tuple[str, str, int | None, bool]]:
    """(file name, sha256, size, optional) of the pinned archives an env.json names: `archive` (with its content
    manifest) or `archives`."""
    items = []
    one = meta.get("archive")
    if one:
        items.append((one["name"], one["sha256"], one.get("size"), False))
        if one.get("manifest"):
            items.append((one["manifest"], one["manifest_sha256"], None, False))
    for a in meta.get("archives", []):
        items.append((a["name"], a["sha256"], a.get("bytes") or a.get("size"), bool(a.get("optional"))))
    return items


def stage_env_archives(cfg: Config, meta: dict) -> Path | None:
    """A directory holding the env's pinned archives for build-host.sh (AGENTSWE_ENV_ARCHIVE_DIR), or None to leave
    the setting as configured. An operator directory that already holds every archive is used as is; otherwise each
    archive is fetched from the release asset store (release path editing/env-archives/<name>, or its flat name in a
    GitHub release: optimization_assets.release_asset_url; cached by sha256 under AGENTSWE_HOME/cache/downloads, size
    and sha256 enforced) and linked into
    AGENTSWE_HOME/cache/env-archives. An optional archive that no source delivers is skipped (build-host.sh then uses
    its recipe); a missing required one stops setup."""
    from .optimization_assets import _cached, _place, release_asset_url

    items = pinned_env_archives(meta)
    local = cfg.get("AGENTSWE_ENV_ARCHIVE_DIR")
    if not items or (local and all((Path(local) / name).is_file() for name, *_ in items)):
        return None
    release = (cfg.get("AGENTSWE_RELEASE_ASSETS_URL") or "").rstrip("/")
    if not release:
        return None  # build-host.sh names what is missing, or falls back to its recipe
    stage = cfg.home / "cache" / "env-archives"
    for name, sha, size, optional in items:
        if local and (Path(local) / name).is_file():
            _place(Path(local) / name, stage / name)
            continue
        try:
            url = release_asset_url(cfg, f"{ENV_ARCHIVE_RELEASE_DIR}/{name}", sha)
            cached = _cached(cfg, name, sha, [url], size)
        except SystemExit:
            if optional:
                util.log(f"{name}: not in the release asset store; build-host.sh uses its recipe")
                continue
            raise
        _place(cached, stage / name)
    return stage


def write_private_roots(cfg: Config) -> Path:
    """Record where this install keeps evaluator-private material, for evaluators that cap a model-authored artifact
    naming a private path (Dyad evaluator/result_score_caps.py PRIVATE_PREFIXES). The paper hosts kept all of it
    under /home/<user> and /data/<user>; a release install keeps it under AGENTSWE_HOME, next to the .env in use,
    next to every @file: credential that .env references, for the private migration archive (which is outside the public release)."""
    def narrow(path: Path) -> str:  # a file directly under / or /etc is named itself, not its whole directory
        return str(path.parent if len(path.parent.parts) > 2 else path)
    roots = {str(cfg.home), narrow((cfg.env_file or REPO_ROOT / ".env").resolve()), "/run/secrets"}
    for key, value in cfg.values.items():
        if key.endswith(("_API_KEY", "_TOKEN")) and isinstance(value, str) and value.startswith("@file:"):
            roots.add(narrow(Path(value[len("@file:"):].partition("#")[0]).expanduser().resolve()))
    path = editing_root(cfg) / "private_roots.json"
    util.write_json(path, {"schema_version": "agentswe-private-roots/v1", "roots": sorted(roots)})
    return path


CONTROL_STATE = ("readiness_admissions",)  # written by the control plane at run time; survives re-rendering


def render_tree(src: Path, dst: Path, mapping: dict[str, str], keep: tuple[str, ...] = ()) -> int:
    held = dst.parent / (dst.name + ".keep")
    if dst.exists():
        shutil.rmtree(held, ignore_errors=True)
        for name in keep:
            if (dst / name).exists():
                held.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst / name), held / name)
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if held.is_dir():
        for item in held.iterdir():
            shutil.move(str(item), dst / item.name)
        held.rmdir()
    changed = 0
    for p in dst.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        data = p.read_bytes()
        if b"@@AGENTSWE_" not in data and not any(k.encode() in data for k in mapping if not k.startswith("@@")):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for k, v in mapping.items():
            text = text.replace(k, v)
        p.write_text(text, encoding="utf-8")
        changed += 1
    return changed


def write_fresh_guard(cfg: Config, tree: Path) -> Path:
    """DeepTutor refuses to dispatch without a product guard; a fresh install has no prior products."""
    guard_py, protocol_py = tree / "agentloop" / "prior_product_guard.py", tree / "agentloop" / "protocol.py"
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    value = {"schema_version": "deeptutor-prior-product-guard/v1", "task": "deeptutor",
             "digest_algorithm": "agentloop.protocol.tree_digest",
             "guard_digest_algorithm": "deeptutor-product-files/v1", "guard_algorithm_sha256": sha(guard_py),
             "algorithm": {"path": str(protocol_py), "sha256": sha(protocol_py)},
             "fresh_install": True, "runs": [], "products": [], "created_at": util.now()}
    path = editing_root(cfg) / "state" / "deeptutor-prior-product-guard.json"
    util.write_json(path, value)
    return path


RELEASE_SNAPSHOT = r"""
import json, sys
from pathlib import Path
control, out = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(control))
import audit_readiness as audit
templates = json.loads(sys.argv[3])
paper = json.loads(Path(sys.argv[4]).read_text())
snapshot = {k: v for k, v in paper.items() if k != "tasks"}
snapshot.update(provenance_mode="release", paper_provenance="not available in release",
                paper_record="pre_repair_tree_snapshot.paper.json", tasks={})
for task, row in paper["tasks"].items():
    template = Path(templates[task])
    snapshot["tasks"][task] = dict(row, source={"path": str(template), "digest": audit.tree_digest(template),
                                                "kind": "release template tree (unrendered)"})
out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
"""


def write_release_source_snapshot(e: Path) -> None:
    """Release provenance: the 'original source' of each task is the release's own template tree.

    The paper's pre-repair snapshot names source packages on the paper hosts, which no install has; it is kept
    verbatim as pre_repair_tree_snapshot.paper.json (a disclosed record, never checked here), and the snapshot the
    audit and the per-task formal gate read binds each task to its unrendered template in this checkout, so a
    formal launch requires the release tree to be unchanged since setup. The release snapshot is built from the
    paper record and replaces the installed one only when it differs (a live run may be reading it).
    """
    control = e / "control"
    current, paper = control / "pre_repair_tree_snapshot.json", control / "pre_repair_tree_snapshot.paper.json"
    if (util.read_json(current, {}) or {}).get("provenance_mode") != "release" or not paper.is_file():
        shutil.copyfile(current, paper)  # setup has just written the paper template there
    templates = {key: str(REPO_ROOT / "tasks" / "editing" / tid / "tree") for tid, key in TASK_KEYS.items()}
    out = e / "state" / "pre_repair_tree_snapshot.release.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    util.run(["/usr/bin/python3", "-B", "-c", RELEASE_SNAPSHOT, str(control), str(out), json.dumps(templates),
              str(paper)], cwd=str(control))
    if current.is_file() and current.read_bytes() == out.read_bytes():
        out.unlink()
        util.log("release provenance: pre-repair snapshot unchanged")
        return
    os.replace(out, current)
    util.log("release provenance: pre-repair snapshot bound to the release template trees (paper record kept)")


def ensure_uv(s) -> Path:
    uv = s.home / "tools" / "uv-pkg" / "bin" / "uv"
    if not uv.exists():
        py = s.ensure_harbor_python()
        util.log("installing uv")
        util.run([str(py / "bin" / "python3"), "-m", "pip", "install", "-q", "--index-url", s.cfg.get("AGENTSWE_PIP_INDEX_URL"),
                  "--target", str(s.home / "tools" / "uv-pkg"), "uv==0.12.3"], capture=False)
    return uv


LIVE_UNIT_STATES = ("active", "activating")
# control/ entries that are not part of the render: run-time state (CONTROL_STATE), the state templates and what
# rebind, the release snapshot and the audit make of them, and the registry lock
RENDER_EXTRAS = ("pre_repair_tree_snapshot.paper.json", "configuration_delta_registry.lock")
RENDER_IGNORED = ("__pycache__", ".pytest_cache")


def live_runs(cfg: Config) -> dict[str, str]:
    """{run id: task id} of this home's Editing runs whose systemd unit is active or activating."""
    found = {}
    root = cfg.home / "runs" / FAMILY_DIR
    for mf in sorted(root.glob("*.launch.json")) if root.is_dir() else []:
        m = util.read_json(mf, {}) or {}
        if isinstance(m, dict) and m.get("unit") and _unit_state(m["unit"]) in LIVE_UNIT_STATES:
            found[str(m.get("run_id") or mf.name)] = str(m.get("task"))
    return found


def _refuse_live(what: str, runs, cfg: Config) -> None:
    raise SystemExit(f"{what}, but Editing run(s) {', '.join(sorted(runs))} are live in {cfg.home}; "
                     "wait for them to finish or use another AGENTSWE_HOME")


def _render_entries(root: Path, skip_top: set[str]) -> dict[str, tuple] | None:
    """What a rendered directory holds (path -> file digest, link target or dir), without run-time extras."""
    if not root.is_dir() or root.is_symlink():
        return None
    entries: dict[str, tuple] = {}
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        top = base == root
        dirnames[:] = sorted(d for d in dirnames if d not in RENDER_IGNORED and not (top and d in skip_top))
        for name in [*dirnames, *filenames]:
            if top and name in skip_top or name.endswith((".pyc", ".rebind-tmp")):
                continue
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                entries[rel] = ("dir",)
            else:
                entries[rel] = ("file", util.sha256_file(path))
    return entries


def _install_render(staged: Path, dst: Path, keep: tuple[str, ...]) -> None:
    """Swap a staged render in for dst, carrying dst's run-time state (keep) over."""
    if dst.exists():
        for name in keep:
            if (dst / name).exists() or (dst / name).is_symlink():
                shutil.move(str(dst / name), staged / name)
        old = dst.with_name(f".{dst.name}.old")
        shutil.rmtree(old, ignore_errors=True)
        os.rename(dst, old)
        os.rename(staged, dst)
        shutil.rmtree(old)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staged, dst)


def _stage_builder_switch(stage: Path, e: Path, builder_key: dict, cfg: Config) -> None:
    """A non-default Builder edits control/ and tools/ after rendering (editing_builder_model); apply the same edits
    to the staged render so an unchanged install compares equal. Tree edits are left to the real apply."""
    link = stage / "tasks"
    try:
        if (e / "tasks").is_dir():
            link.symlink_to(e / "tasks")
        window = cfg.get("AGENTSWE_EDITING_BUILDER_CONTEXT_WINDOW")
        planned = editing_builder_model.plan(builder_key["model"], builder_key["effort"], stage, REPO_ROOT,
                                             int(window) if window else None)
    except editing_builder_model.SwitchError:
        return  # the real apply below reports it
    finally:
        if link.is_symlink():
            link.unlink()
    for path, text in planned["files"].items():
        if path.parts[:len(stage.parts) + 1] in (stage.parts + ("control",), stage.parts + ("tools",)):
            path.write_text(text, encoding="utf-8")


def render_control_plane(cfg: Config, s, e: Path, mapping: dict[str, str], live: dict[str, str], builder_key: dict,
                         switched: bool) -> None:
    """control/, tools/ and the state templates, replaced only when this release renders them differently, and then
    never under a live Editing run (which imports the judge broker and finalizer modules from control/)."""
    # control/ and tools/: render into a staging dir and keep the installed one untouched when it is the same render
    state_dir = REPO_ROOT / "runners" / "editing" / "state"
    templates = {p.name: p for p in sorted(state_dir.glob("*")) if p.is_file()}
    skip = {*CONTROL_STATE, *templates, *RENDER_EXTRAS}
    stage = e / ".render-staging"
    shutil.rmtree(stage, ignore_errors=True)
    for sub in ("control", "tools"):
        render_tree(REPO_ROOT / "runners" / "editing" / sub, stage / sub, mapping)
    if switched:
        _stage_builder_switch(stage, e, builder_key, cfg)
    changed = [sub for sub in ("control", "tools")
               if _render_entries(stage / sub, skip) != _render_entries(e / sub, skip)]
    # state templates (configuration registry, coverage matrix, gate, snapshots) live where the control plane reads
    # them; rebind, the release snapshot and the audit then rewrite some, so a template counts as unchanged when the
    # file still holds it or it is the one setup last wrote there
    written = s.state.setdefault("editing_control_templates", {})
    pending = {}
    for name, p in templates.items():
        text = p.read_text(encoding="utf-8")
        for k, v in mapping.items():
            text = text.replace(k, v)
        digest = hashlib.sha256(text.encode()).hexdigest()
        target = e / "control" / name
        recorded = written.get(name) == digest and target.is_file() and (
            name != "pre_repair_tree_snapshot.json" or (e / "control" / RENDER_EXTRAS[0]).is_file())
        if "control" in changed or not (recorded or (target.is_file() and target.read_text(encoding="utf-8") == text)):
            pending[name] = (text, digest)
    if (changed or pending) and live:
        shutil.rmtree(stage, ignore_errors=True)
        _refuse_live("the Editing control plane changed (" + ", ".join(
            [f"{sub}/" for sub in changed] + ([f"{len(pending)} state template(s)"] if pending else [])) + ")",
            live, cfg)
    for sub in ("control", "tools"):
        if sub in changed:
            _install_render(stage / sub, e / sub, CONTROL_STATE if sub == "control" else ())
        else:
            util.log(f"editing/{sub} unchanged; kept as installed")
    shutil.rmtree(stage, ignore_errors=True)
    for name, (text, digest) in pending.items():
        (e / "control" / name).write_text(text, encoding="utf-8")
        written[name] = digest


def setup(cfg: Config, task: Task, s) -> None:
    images = s.images()["images"]
    e = editing_root(cfg)
    mapping = {**tokens(cfg), **retargets(images), **image_tokens(images), **built_image_tokens(s)}
    # A live Editing run reads the rendered control plane and its task's tree: nothing it uses is replaced under it.
    live = live_runs(cfg)
    own = [run for run, t in live.items() if t == task.id]
    if own:
        _refuse_live(f"setup of {task.id} re-renders its task tree and a0", own, cfg)
    builder = cfg.role("BUILDER", "EDITING")
    builder_key = {"model": builder.model or editing_builder_model.DEFAULT_MODEL,
                   "effort": builder.effort or editing_builder_model.DEFAULT_EFFORT}
    switched = (builder_key["model"], builder_key["effort"]) != (editing_builder_model.DEFAULT_MODEL,
                                                                 editing_builder_model.DEFAULT_EFFORT)
    render_control_plane(cfg, s, e, mapping, live, builder_key, switched)
    (e / "state").mkdir(parents=True, exist_ok=True)
    # Every task tree must exist (rebind and the audit walk all ten), but re-rendering one that is already
    # set up would discard its fetched a0 and invalidate its readiness admission. Render a tree only for the
    # task being set up, or when its template or the token mapping changed; a previously set-up task whose
    # tree had to be re-rendered loses its setup record and must be set up again.
    from ..setup import tree_digest
    rendered = s.state.setdefault("editing_trees", {})
    # A non-default Builder model or effort is part of every rendered tree (Builder-only sites, editing_builder_model);
    # the default leaves the digest as it was, so existing installs are not re-rendered (which would drop fetched a0s).
    digest_input = {**mapping, "@builder@": builder_key} if switched else mapping
    mapping_digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode()).hexdigest()
    plan = []
    for t in sorted((REPO_ROOT / "tasks" / "editing").iterdir()):
        if not (t / "tree").is_dir():
            continue
        parts = [tree_digest(t / "tree"), mapping_digest]
        tree_overlay = t / "profiles" / cfg.profile / "tree"
        if tree_overlay.is_dir():  # profile overlay (files whose Builder-visible bytes differ in this profile)
            parts.append(f"{cfg.profile}:{tree_digest(tree_overlay)}")
        if (t / "hidden_issued").is_dir():
            parts.append(tree_digest(t / "hidden_issued"))
        digest = ":".join(parts)
        dest = e / "tasks" / t.name / "tree"
        if t.name != task.id and rendered.get(t.name) == digest and dest.is_dir():
            continue
        plan.append((t, digest, dest, tree_overlay))
    busy = [run for run, t in live.items() if t in {item[0].name for item in plan}]
    if busy:
        _refuse_live("task tree(s) " + ", ".join(sorted({live[r] for r in busy})) + " must be re-rendered", busy, cfg)
    for t, digest, dest, tree_overlay in plan:
        render_tree(t / "tree", dest, mapping)
        if tree_overlay.is_dir():
            staging = e / "tasks" / t.name / ".profile-overlay"
            render_tree(tree_overlay, staging, mapping)
            for f in sorted(staging.rglob("*")):
                if f.is_file() or f.is_symlink():
                    target = dest / f.relative_to(staging)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.unlink(missing_ok=True)
                    shutil.move(str(f), target)
            shutil.rmtree(staging)
        if (t / "hidden_issued").is_dir():
            render_tree(t / "hidden_issued", e / "tasks" / t.name / "hidden_issued", mapping)
        rendered[t.name] = digest
        if t.name != task.id and s.state.get("tasks", {}).pop(t.name, None):
            util.log(f"warning: {t.name} was re-rendered (template or paths changed); run `agentswe setup {t.name}` again")
    s.save()
    key = TASK_KEYS[task.id]
    if key in ISSUED_HIDDEN:
        src = e / "tasks" / task.id / "hidden_issued"
        if not src.is_dir():
            raise SystemExit(f"{task.id}: tasks/editing/{task.id}/hidden_issued is missing")
        issued = (cfg.home / "runs" / FAMILY_DIR / "formal" / "evaluator-issued" / key
                  / f"0905-edit-codex-xhigh-{ISSUED_HIDDEN[key]}-{key}")
        if issued.exists():
            shutil.rmtree(issued)
        issued.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, issued)
        util.log(f"installed the evaluator-issued hidden bundle at {issued}")
    rc = task.data.get("runner_config", {})
    a0 = rc.get("a0")
    if a0:
        # The public release is English-only. Keep the paper language as an explicit opt-in for
        # private reproduction, but never make a fresh install silently fetch the Chinese overlay.
        lang = cfg.get("AGENTSWE_EDITING_A0_LANG") or "en-upstream"
        if lang != "en-upstream":
            raise SystemExit("AGENTSWE_EDITING_A0_LANG must be en-upstream for the public release")
        dest = e / "tasks" / task.id / a0["dest"]
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["AGENTSWE_PROFILE"] = cfg.profile  # fetch.sh selects the profile's as-run a0 bytes
        env.update({k: cfg.get(k) for k in A0_FETCH_CONFIG if cfg.get(k)})
        util.log(f"fetching a0 ({lang}) into {dest}")
        util.run(["bash", str(task.dir / a0["fetch"]), str(dest), "--lang", lang, "--verify"], env=env, capture=False)
    # Evaluators run git (apply, diff, status) inside the rendered tree and its a0; neither may resolve to an
    # enclosing work tree (the home check covers AGENTSWE_HOME; this covers what was actually rendered).
    tree = e / "tasks" / task.id / "tree"
    for path in [tree, *([e / "tasks" / task.id / a0["dest"]] if a0 else [])]:
        if not path.is_dir():
            continue
        top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=False)
        if top.returncode == 0:
            raise SystemExit(f"{path} resolves to the git work tree {top.stdout.strip()}; evaluators' git commands "
                             "would act on that repository. Move AGENTSWE_HOME out of it.")
    for env_name in task.data.get("envs", []):
        spec_dir, meta = s.find_env_spec(env_name)
        if meta.get("build_on") == "host":
            target = cfg.home / "envs" / meta["name"]
            # env.json "ready_file": the path a completed build leaves behind (default: a venv's python)
            if not (target / meta.get("ready_file", "venv/bin/python")).exists():
                env = os.environ.copy()
                env.update({"AGENTSWE_HOME": str(cfg.home), "UV": str(ensure_uv(s)),
                            "PIP_INDEX_URL": cfg.get("AGENTSWE_PIP_INDEX_URL"),
                            "NPM_REGISTRY": cfg.get("AGENTSWE_NPM_REGISTRY"),
                            "NODE_DIST_URL": cfg.get("AGENTSWE_NODE_DIST_URL")})
                env.update({k: cfg.get(k) for k in HOST_BUILD_CONFIG if cfg.get(k)})
                staged_archives = stage_env_archives(cfg, meta)
                if staged_archives is not None:
                    env["AGENTSWE_ENV_ARCHIVE_DIR"] = str(staged_archives)
                if meta.get("kind") == "conda":
                    s.ensure_micromamba()
                    env["MICROMAMBA"] = str(s.micromamba)
                env["PYTHONPATH"] = str(s.home / "tools" / "uv-pkg")
                if cfg.get("AGENTSWE_UV_PYTHON_INSTALL_MIRROR"):
                    env["UV_PYTHON_INSTALL_MIRROR"] = cfg.get("AGENTSWE_UV_PYTHON_INSTALL_MIRROR")
                util.log(f"building host environment {meta['name']}")
                util.run(["bash", str(spec_dir / "build-host.sh"), str(target)], env=env, capture=False)
            # env.json "tree_link": a path inside the rendered tree the evaluator reads the environment from
            # (OpenWiki's sibling .runtime). Re-created after every render; never replaces a real directory.
            if meta.get("tree_link"):
                link = e / "tasks" / task.id / "tree" / meta["tree_link"]
                if link.is_symlink():
                    link.unlink()
                elif link.exists():
                    raise SystemExit(f"{link} exists and is not a link; refusing to replace it")
                link.symlink_to(target)
            # env.json "tree_links": [{"path": <in the rendered tree>, "env_path": <inside the environment>}], for
            # evaluators that reach part of an environment through links inside a0 (the paper's Dyad a0 carried
            # node_modules links into its prepared dependencies). Re-created after every a0 fetch, like tree_link.
            for item in meta.get("tree_links", []):
                link = e / "tasks" / task.id / "tree" / item["path"]
                if link.is_symlink():
                    link.unlink()
                elif link.exists():
                    raise SystemExit(f"{link} exists and is not a link; refusing to replace it")
                if not link.parent.is_dir():
                    raise SystemExit(f"{link.parent} is missing; tree_links point into the fetched a0")
                link.symlink_to(target / item["env_path"])
    if TASK_KEYS[task.id] == "deeptutor":
        write_fresh_guard(cfg, e / "tasks" / task.id / "tree")
    write_private_roots(cfg)
    if switched:
        window = cfg.get("AGENTSWE_EDITING_BUILDER_CONTEXT_WINDOW")
        try:
            report = editing_builder_model.apply(builder_key["model"], builder_key["effort"], e, REPO_ROOT,
                                                 int(window) if window else None)
        except editing_builder_model.SwitchError as exc:
            raise SystemExit(str(exc))
        util.write_json(e / "state" / "builder_switch.json", report)
        util.log(f"Editing Builder switched to {builder_key['model']}/{builder_key['effort']}: "
                 f"{len(report['edits'])} Builder-only edits (state/builder_switch.json)")
    util.log("rebinding the configuration registry and source snapshot")
    util.run(["/usr/bin/python3", "-B", str(e / "tools" / "rebind.py"), "--apply"], capture=False, cwd=str(e / "control"))
    if provenance_mode(cfg) == "release":
        write_release_source_snapshot(e)
    r = util.run(["/usr/bin/python3", "-B", str(e / "control" / "audit_readiness.py")], check=False, cwd=str(e / "control"))
    util.log("audit_readiness exit %d (informational; dispatch re-checks the per-task gate)" % r.returncode)


def _unit_state(unit: str) -> str:
    return util.out(["systemctl", "show", "-p", "ActiveState", "--value", unit])


# Every task tree's Builder relay (control/direct_harbor_builder.existing_proxy_for_builder) forwards the Builder
# container's CONNECT to this loopback proxy, as on the published runs' hosts. OpenWiki, OpenHands and OpenClaw always
# use it; the other one-stops use it for formal runs (their --builder-proxy default). The trees stay as published.
BUILDER_PROXY = loopback_proxy.DEFAULT_LISTEN
PROXY_UNIT = "agentswe-loopback-proxy"
REMOVAL_WAIT_SECONDS = 30  # `stop`: an --rm container the daemon is still removing (Docker 29)


def _proxy_unit_name() -> str:
    """agentswe-loopback-proxy, or a distinct name when a unit by that name exists but does not serve the port (a
    host's own proxy unit that is stopped, or an earlier bundled proxy that died)."""
    load = util.out(["systemctl", "show", "-p", "LoadState", "--value", PROXY_UNIT + ".service"])
    if load in ("", "not-found"):
        return PROXY_UNIT
    return f"{PROXY_UNIT}-{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')}"


def ensure_builder_proxy(wait: float = 15.0) -> dict:
    """The HTTP CONNECT proxy the Builder relay needs on 127.0.0.1:7890: a listener already there that answers CONNECT
    (the host's own proxy, such as Clash, mihomo or gost) is used as is; with nothing there, the bundled proxy starts as a
    transient systemd unit and keeps running for later runs; a port held by anything else stops the run."""
    host, port = BUILDER_PROXY
    address = f"{host}:{port}"
    state, detail = loopback_proxy.probe(host, port)
    if state == "proxy":
        return {"address": address, "source": "pre-existing listener", "probe": detail}
    if state == "other":
        raise SystemExit(f"{detail}, so it is not an HTTP CONNECT proxy. The Editing Builder relay sends the Builder's "
                         f"provider traffic through an HTTP CONNECT proxy on {address}: stop the service that holds "
                         "the port, and agentswe starts its own; a proxy of your own there (Clash, mihomo, gost) "
                         "is used as is.")
    script = Path(loopback_proxy.__file__).resolve()
    unit = _proxy_unit_name()
    python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    started = util.run(["systemd-run", f"--unit={unit}", "--collect", "--property=Restart=on-failure",
                        "--description=AgentSWE loopback HTTP CONNECT proxy for the Builder relay (bundled)",
                        python, "-I", "-B", str(script), "--listen", address], check=False)
    if started.returncode != 0:
        raise SystemExit(f"could not start the bundled loopback proxy ({unit}): {(started.stdout or '').strip()}")
    deadline = time.monotonic() + wait
    while True:
        state, detail = loopback_proxy.probe(host, port)
        if state == "proxy" or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if state != "proxy":
        raise SystemExit(f"the bundled loopback proxy {unit}.service did not serve {address} ({detail}); "
                         f"see journalctl -u {unit}")
    util.log(f"started the bundled loopback CONNECT proxy {unit}.service on {address}")
    return {"address": address, "source": "bundled", "unit": unit + ".service", "script": str(script),
            "script_sha256": util.sha256_file(script), "probe": detail}


def editing_roles(cfg: Config) -> dict:
    roles = {n: cfg.role(n, "EDITING") for n in ("BUILDER", "RUNTIME", "JUDGE")}
    keys = {r.api_key for r in roles.values()}
    if len(keys) != 1 or any(r.wire != "responses" for r in roles.values()):
        raise SystemExit("the Editing control plane uses one Responses provider key for builder, lower agent and "
                         "judge; configure all three roles on the same provider for now")
    return roles


def write_credential(cfg: Config) -> Path:
    """The control plane's 0600 credential file, from the .env roles (read only by its own brokers; never printed)."""
    b = editing_roles(cfg)["BUILDER"]
    cred = credential_file(cfg)
    util.write_secret(cred, {"DEEPSEEK_API_KEY": b.api_key, "OPENAI_API_KEY": b.api_key})
    return cred


def run(cfg: Config, task: Task, *, builder: str, seed: int, smoke: bool, label: str | None) -> dict:
    state = util.read_json(cfg.home / "state" / "setup.json", {}) or {}
    if task.id not in state.get("tasks", {}):
        raise SystemExit(f"run `agentswe setup {task.id}` first")
    b = editing_roles(cfg)["BUILDER"]
    key = TASK_KEYS[task.id]
    e = editing_root(cfg)
    proxy = ensure_builder_proxy()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()
    tag = f"oss-{'smoke' if smoke else 'formal'}-s{seed}-{stamp}" if not label else f"{label}-{stamp}"
    cred = write_credential(cfg)
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))
           and k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    env["AGENTSWE_BUILDER_BASE_URL"] = b.base_url
    env["AGENTSWE_PROFILE"] = cfg.profile
    if smoke:
        cmd = ["/usr/bin/python3", "-B", str(e / "tools" / "launch_readiness.py"), "--task", key, "--tag", tag,
               "--proxy", ""]
        unit = f"agentswe-oss-edit-{key}-{tag}"
    else:
        cmd = ["/usr/bin/python3", "-B", str(e / "tools" / "launch_formal_task.py"), "--task", key, "--label", tag]
        # The readiness admission is a benchmark-construction gate (a real Builder canary run per task and host);
        # a release install launches a formal run after the same source-integrity checks, without it. Maintainers
        # can require it (AGENTSWE_EDITING_REQUIRE_ADMISSION=1), as the paper's formal cells did.
        if (cfg.get("AGENTSWE_EDITING_REQUIRE_ADMISSION") or "").strip() not in ("1", "true", "yes"):
            cmd += ["--integrity-only"]
        if key in ISSUED_HIDDEN:
            cmd += ["--issue-hidden-from", ISSUED_HIDDEN[key]]
        unit = f"agentswe-oss-formal-{key}-{tag}"
    p_root = cfg.home / "runs" / FAMILY_DIR
    p_root.mkdir(parents=True, exist_ok=True)
    log = p_root / f"{task.short}-{tag}.launch.log"
    with open(log, "w") as fh:
        r = subprocess.run(cmd, env=env, cwd=str(e / "control"), stdout=fh, stderr=subprocess.STDOUT, timeout=900)
    text = log.read_text()
    if r.returncode != 0:
        cred.unlink(missing_ok=True)
        raise SystemExit(f"launcher failed ({r.returncode}); see {log}\n" + text[-1500:])
    run_dir = next((line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("run   =")), "")
    manifest = {"run_id": f"e-{task.short}-{tag}", "task": task.id, "family": task.family,
                "mode": "smoke" if smoke else "formal", "comparable": not smoke, "host": socket.gethostname(),
                "started_at": util.now(), "unit": unit + ".service", "run_dir": run_dir, "log": str(log),
                "pid": 0, "builder": {"profile": builder, "model": b.model, "effort": b.effort},
                "builder_proxy": proxy,
                "a0_lang": cfg.get("AGENTSWE_EDITING_A0_LANG"),
                "repo_commit": util.out(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])}
    util.write_json(p_root / f"{manifest['run_id']}.launch.json", manifest)
    return manifest


def _orchestrator_log(run_dir: Path | None) -> Path | None:
    """A formal run's control-plane log, where a one-stop that stops at startup says why (launch_formal_task.py:
    <formal root>/launch_control/<launch id>/<task>/orchestrator.log next to codex_xhigh/<task>/<launch id>-<task>)."""
    if run_dir is None or len(run_dir.parents) < 3:
        return None
    task = run_dir.parent.name
    if not run_dir.name.endswith("-" + task):
        return None
    path = run_dir.parents[2] / "launch_control" / run_dir.name[: -len(task) - 1] / task / "orchestrator.log"
    return path if path.is_file() else None


def status(cfg: Config, launch: dict) -> dict:
    unit_state = _unit_state(launch["unit"])
    run_dir = Path(launch["run_dir"]) if launch.get("run_dir") else None
    summaries = sorted(run_dir.glob("*summary*.json")) if run_dir and run_dir.is_dir() else []
    log = _orchestrator_log(run_dir)
    return {"run_id": launch["run_id"], "unit": launch["unit"], "unit_state": unit_state,
            "alive": unit_state in ("active", "activating"), "run_dir": str(run_dir) if run_dir else None,
            "summaries": [p.name for p in summaries],
            "exit_status": util.out(["systemctl", "show", "-p", "ExecMainStatus", "--value", launch["unit"]]),
            "orchestrator_log": str(log) if log else None}


def _release_credential(cfg: Config, launch: dict) -> None:
    """The credential file is shared by every run of this home: remove it only when no other run here is live."""
    for mf in (cfg.home / "runs" / FAMILY_DIR).glob("*.launch.json"):
        other = util.read_json(mf, {}) or {}
        if other.get("run_id") != launch.get("run_id") and other.get("unit") and \
                _unit_state(other["unit"]) in ("active", "activating"):
            return
    credential_file(cfg).unlink(missing_ok=True)


def _run_owned(info: dict, run_dir: str) -> bool:
    """A container belongs to the run when a mount source or its compose working directory is under the run dir."""
    labels = (info.get("Config") or {}).get("Labels") or {}
    paths = [m.get("Source", "") for m in info.get("Mounts") or []]
    paths.append(labels.get("com.docker.compose.project.working_dir", ""))
    return any(path == run_dir or path.startswith(run_dir.rstrip("/") + "/") for path in paths if path)


def _remove_run_objects(cfg: Config, launch: dict) -> list[str]:
    """Containers (and then their now-empty compose networks) owned by the run, identified by mounts or compose
    working directory under its run dir, never by name. Each removal is logged in <home>/DELETIONS.log."""
    run_dir = str(launch.get("run_dir") or "")
    if not run_dir or not Path(run_dir).is_absolute():
        return []
    ids = util.out(["docker", "ps", "-aq", "--no-trunc"]).split()
    owned, projects = [], set()
    for cid in ids:
        raw = util.out(["docker", "inspect", cid])
        try:
            info = json.loads(raw)[0]
        except (ValueError, IndexError):
            continue
        if _run_owned(info, run_dir):
            owned.append(cid)
            project = ((info.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project")
            if project:
                projects.add(project)
    removed, removing = [], []
    for cid in owned:
        r = util.run(["docker", "rm", "-f", cid], check=False)
        if r.returncode == 0:
            removed.append(f"container {cid[:12]}")
        elif "already in progress" in (r.stdout or "").lower():
            removing.append(cid)  # an --rm container the daemon is removing (Docker 29 does it asynchronously)
    # Wait for those before the network pass: a network still holding a container being removed is not empty.
    pending, deadline = list(removing), time.monotonic() + REMOVAL_WAIT_SECONDS
    while pending and time.monotonic() < deadline:
        pending = [cid for cid in pending if util.run(["docker", "inspect", cid], check=False).returncode == 0]
        if pending:
            time.sleep(1)
    removed += [f"container {cid[:12]} (removed by the daemon)" for cid in removing if cid not in pending]
    for project in sorted(projects):
        for net in util.out(["docker", "network", "ls", "-q", "--filter",
                             f"label=com.docker.compose.project={project}"]).split():
            busy = util.out(["docker", "network", "inspect", net, "--format", "{{len .Containers}}"])
            if busy.strip() == "0" and util.run(["docker", "network", "rm", net], check=False).returncode == 0:
                removed.append(f"network {net[:12]} ({project})")
    if removed:
        with open(cfg.home / "DELETIONS.log", "a") as log:
            for item in removed:
                log.write(f"{util.now()} stop {launch['run_id']}: removed {item}\n")
    return removed


# Where a smoke (readiness) run keeps its outcome. A smoke has no formal_aggregation.json, so its summaries say
# result_axis "N/A": the held-out score is in the readiness result contract; the dev rounds and the held-out case are
# in each task tree's own records. The ten trees keep them in these places (read from their readiness one-stops):
#   dev rounds: controller_state.json `records` (DeepTutor, DeepCode, OpenWiki under lifecycle/), dev_lifecycle.json
#     `records` or a bare list (AI-Scientist, Aider, Dyad, OpenClaw, OpenHands under lifecycle/; Codex at the top),
#     or one lifecycle/round_NNN.json per round (Claude); each round's cases under `dev_results`, `dev_cases` or
#     `dev` (a dict by case id or a list), a case being the record itself or a path to its result.json;
#   held-out: a hidden-after-freeze attestation (`cases` dict or list, or `results`), Codex's and Aider's
#     readiness_hidden_attestation.json (`case` + `result`), or OpenClaw's pilot hidden suite (summary.json
#     `pilot_hidden`, pilot_hidden[_retry_NNN]/summary.json `cases` by case id; lifecycle/hidden_result.json by case id);
#     OpenHands lists {case_id, result_path} with `classifications` by case id.
#   validity: each tree names its own verdict on the case's result (SMOKE_VALIDITY, first present; `valid_field` says
#     which); infrastructure validity is infra_valid / infrastructure_invalid, or OpenClaw's native_case errors.
#   infrastructure gate: the summaries' status or classification, else (Claude before the 2026-10-10 tree) a last
#     submission the evaluator did not consume for an infrastructure failure, in builder_observer_events.jsonl.
SMOKE_RECORD_FILES = ("controller_state.json", "lifecycle/controller_state.json", "dev_lifecycle.json",
                      "lifecycle/dev_lifecycle.json")
SMOKE_ROUND_FILES = "lifecycle/round_[0-9]*.json"
SMOKE_HIDDEN_FILES = ("hidden/hidden-after-freeze-attestation.json", "hidden-after-freeze-attestation.json",
                      "lifecycle/hidden-after-freeze-attestation.json", "lifecycle/hidden_after_freeze_attestation.json",
                      "lifecycle/pilot-hidden-after-freeze-attestation.json", "pilot_hidden_after_freeze_attestation.json",
                      "readiness_hidden_attestation.json", "pilot_hidden/summary.json", "lifecycle/hidden_result.json")
SMOKE_VALIDITY = (("valid",), ("execution", "valid"), ("validity_gate",), ("artifact_validation", "valid"),
                  ("artifact_contract", "valid"), ("failure_attribution", "candidate_result_valid"),
                  ("lower_result_valid",))
SMOKE_OBSERVER_EVENTS = "builder_observer_events.jsonl"
SMOKE_CONTRACTS = {"result": "readiness_scoring/result/result_score_contract.json",
                   "code": "readiness_scoring/code/code_score_contract.json"}
SMOKE_SCORE_NOTE = ("held_out[].result_score is the judged Result from the readiness result contract; scores elsewhere "
                    "in `summaries` (pilot_evaluation, agentloop or dev scores) are the evaluator's deterministic "
                    "pre-judge checks, not the Result; `valid` is each tree's own validity verdict on the case, from the "
                    "field `valid_field` names")


def _in_run(run_dir: Path, recorded) -> Path | None:
    """A path that one of the run's own files records, resolved inside this run dir: a relative path is joined to it,
    an absolute one under it is kept, and one under a run dir of the same name elsewhere (the run as it was recorded,
    before the directory was copied or moved) is re-rooted here. Anything else is not read."""
    if not isinstance(recorded, str) or not recorded:
        return None
    path = Path(recorded)
    if ".." in path.parts:
        return None
    if not path.is_absolute():
        return run_dir / path
    if path == run_dir or run_dir in path.parents:
        return path
    parts = path.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == run_dir.name:
            return run_dir.joinpath(*parts[i + 1:])
    return None


def _read_in_run(run_dir: Path, recorded) -> tuple[Path | None, object]:
    path = _in_run(run_dir, recorded)
    return (path, util.read_json(path)) if path and path.is_file() else (None, None)


def _case_record(run_dir: Path, case) -> dict:
    """A case as recorded, merged with the execution record it points to (a result.json path): the record's fields win
    when the recorded entry does not carry the classification itself (Aider and Codex dev cases, OpenHands held-out
    cases); otherwise the recorded entry wins and the record only adds fields (AI-Scientist's launcher_result.json)."""
    base = case if isinstance(case, dict) else {}
    ref = case if isinstance(case, str) else \
        base.get("result") if isinstance(base.get("result"), str) else base.get("result_path")
    if isinstance(ref, str) and ref.endswith(".json"):
        _, loaded = _read_in_run(run_dir, ref)
        if isinstance(loaded, dict):
            return {**loaded, **base} if "classification" in base else {**base, **loaded}
    return dict(base)


def _infrastructure_valid(case: dict) -> bool | None:
    for value in (case, case.get("failure_attribution"), case.get("execution")):
        if isinstance(value, dict):
            if isinstance(value.get("infra_valid"), bool):
                return value["infra_valid"]
            if isinstance(value.get("infrastructure_invalid"), bool):
                return not value["infrastructure_invalid"]
    native = case.get("native_case")  # OpenClaw: the evaluator lists the case's infrastructure errors
    if isinstance(native, dict) and isinstance(native.get("infrastructure_errors"), list):
        return not native["infrastructure_errors"]
    return None


def _case_validity(case: dict) -> tuple[bool | None, str | None]:
    """The tree's own validity verdict on the case's result, and the field it came from (SMOKE_VALIDITY)."""
    for path in SMOKE_VALIDITY:
        value = case
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, bool):
            return value, ".".join(path)
    return None, None


def _failure_party(case: dict, valid: bool | None = None) -> tuple[str | None, str | None]:
    """Who the evaluator attributes the case's failure to, and why: failure_attribution, else classification_axis;
    Dyad records `attribution.owner` (the candidate for a candidate_failure, else the evaluator/provider, also for a
    valid case, which has no failure) and `failure_class`. A failure_attribution with fatal false records no failure
    (Claude names the candidate as the owner of a successful outcome too), so it has no failure party; nor does a
    case whose own verdict is valid fall back to classification_axis (AI-Scientist writes "candidate" there on
    every case, valid ones included)."""
    attribution = case.get("failure_attribution") if isinstance(case.get("failure_attribution"), dict) else {}
    if attribution.get("fatal") is False:
        return None, None
    party = attribution.get("party") or (case.get("classification_axis") if valid is not True else None)
    owner = case.get("attribution") if isinstance(case.get("attribution"), dict) else {}
    if not party and owner.get("owner") and case.get("classification") != "valid":
        party = owner["owner"]
    return party, attribution.get("reason") or case.get("failure_class")


def _cases_of(value: dict, keys: tuple[str, ...]) -> list[tuple[str, object]] | None:
    """(case id, entry) from the first of `keys` that holds a dict by case id or a list of case records."""
    for key in keys:
        cases = value.get(key)
        if isinstance(cases, dict):
            return [(str(k), v) for k, v in cases.items()]
        if isinstance(cases, list):
            return [(str(v.get("case_id", i)) if isinstance(v, dict) else str(i), v) for i, v in enumerate(cases)]
    return None


def _smoke_records(run_dir: Path, one_stop: dict) -> tuple[list[dict], str | None]:
    named = one_stop.get("dev_lifecycle") if isinstance(one_stop.get("dev_lifecycle"), str) else None
    for rel in (named, *SMOKE_RECORD_FILES):
        path, value = _read_in_run(run_dir, rel) if rel else (None, None)
        records = value.get("records") if isinstance(value, dict) else value
        if isinstance(records, list) and any(isinstance(r, dict) for r in records):
            return [r for r in records if isinstance(r, dict)], str(path)
    rounds = sorted(run_dir.glob(SMOKE_ROUND_FILES))
    records = [r for r in (util.read_json(p) for p in rounds) if isinstance(r, dict)]
    return (records, str(rounds[0].parent)) if records else ([], None)


def _smoke_dev_rounds(run_dir: Path, one_stop: dict) -> tuple[list[dict], str | None]:
    """Each accepted dev round's public case(s) as the controller recorded them."""
    records, source = _smoke_records(run_dir, one_stop)
    rows = []
    for number, record in enumerate(records, 1):
        for case_id, entry in _cases_of(record, ("dev_results", "dev_cases", "dev")) or []:
            case = _case_record(run_dir, entry)
            execution = case.get("execution") if isinstance(case.get("execution"), dict) else {}
            valid, valid_field = _case_validity(case)
            row = {"round": record.get("round") or record.get("submission_number") or number,
                   "case_id": str(case.get("case_id") or case_id), "classification": case.get("classification"),
                   "valid": valid, "valid_field": valid_field, "infrastructure_valid": _infrastructure_valid(case)}
            candidate = case.get("candidate_classification") or execution.get("candidate_classification")
            if candidate:
                row["candidate_classification"] = candidate
            if isinstance(case.get("reason"), str) and case["reason"]:
                row["reason"] = case["reason"]
            rows.append(row)
    return rows, source


def _hidden_cases(run_dir: Path, value) -> list[dict]:
    if not isinstance(value, dict):
        return []
    items = _cases_of(value, ("cases", "results"))
    if items is None and isinstance(value.get("case"), str) and isinstance(value.get("result"), dict):
        items = [(value["case"], value["result"])]  # Codex and Aider readiness_hidden_attestation.json
    if items is None and value and all(re.fullmatch(r"test_\d+", str(k)) and isinstance(v, dict)
                                       for k, v in value.items()):
        items = [(str(k), v) for k, v in value.items()]  # OpenClaw lifecycle/hidden_result.json
    classifications = value.get("classifications") if isinstance(value.get("classifications"), dict) else {}
    cases = []
    for case_id, entry in items or []:
        case = _case_record(run_dir, entry)
        case["case_id"] = str(case.get("case_id") or case_id)
        if not case.get("classification") and classifications.get(case["case_id"]):
            case["classification"] = classifications[case["case_id"]]
        cases.append(case)
    return cases


def _smoke_held_out(run_dir: Path, summaries: dict) -> tuple[list[dict], str | None]:
    """The held-out case(s): from the attestation the summaries name, else the first of the trees' own places."""
    one_stop = summaries.get("one_stop_summary.json") if isinstance(summaries.get("one_stop_summary.json"), dict) else {}
    summary = summaries.get("summary.json") if isinstance(summaries.get("summary.json"), dict) else {}
    named = [(one_stop.get("hidden") or {}).get("summary") if isinstance(one_stop.get("hidden"), dict) else None,
             summary.get("hidden_attestation") if isinstance(summary.get("hidden_attestation"), str) else None,
             (summary.get("hidden") or {}).get("attestation_path") if isinstance(summary.get("hidden"), dict) else None]
    # OpenClaw: the one-stop's record of the (last) pilot hidden suite, then a retried suite's own summary
    inline = [(f"{run_dir / 'summary.json'}#pilot_hidden", summary.get("pilot_hidden"))]
    retries = sorted(run_dir.glob("pilot_hidden_retry_[0-9][0-9][0-9]/summary.json"), reverse=True)
    sources = [*named, *inline, *(str(p) for p in retries), *SMOKE_HIDDEN_FILES]
    for source in sources:
        if isinstance(source, tuple):
            path, value = source
        else:
            path, value = _read_in_run(run_dir, source) if source else (None, None)
        cases = _hidden_cases(run_dir, value)
        if cases:
            break
    else:
        return [], None
    rows = []
    for case in cases:
        valid, valid_field = _case_validity(case)
        party, reason = _failure_party(case, valid)
        rows.append({"case_id": case["case_id"], "classification": case.get("classification"),
                     "valid": valid, "valid_field": valid_field, "infrastructure_valid": _infrastructure_valid(case),
                     "failure_party": party, "failure_reason": reason})
    return rows, str(path)


def _smoke_contracts(run_dir: Path) -> dict[str, Path]:
    """The readiness judges' contracts: as readiness_judge_smoke.json names them, else at their standard paths, else
    in a per-case directory of the same scoring dir (Claude: readiness_scoring/result/test_001/)."""
    smoke = util.read_json(run_dir / "readiness_judge_smoke.json", {}) or {}
    found = {}
    for role, default in SMOKE_CONTRACTS.items():
        named = smoke.get(f"{role}_contract") if isinstance(smoke, dict) else None
        path = _in_run(run_dir, named.get("path")) if isinstance(named, dict) else None
        per_case = sorted((run_dir / default).parent.glob(f"test_*/{Path(default).name}"))
        for candidate in (path, run_dir / default, *per_case[:1]):
            if candidate is not None and candidate.is_file():
                found[role] = candidate
                break
    return found


def _unconsumed_infrastructure_submissions(run_dir: Path) -> list[dict]:
    """The Builder observer's record (Claude, Dyad) of submissions the evaluator did not consume because of an
    infrastructure failure, when the session's last submission outcome is one of them: the run then ended unfrozen
    because of that failure, not because of the Builder (Claude's last_submission_infrastructure_failure)."""
    path = run_dir / SMOKE_OBSERVER_EVENTS
    if not path.is_file():
        return []
    outcomes = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") in ("submission_finished", "submission_not_consumed"):
            outcomes.append(event)
    infrastructure = [e for e in outcomes if e.get("event") == "submission_not_consumed"
                      and "infrastructure" in str(e.get("classification") or "")]
    if not infrastructure or outcomes[-1] is not infrastructure[-1]:
        return []
    keys = ("at", "candidate_number", "classification", "error_type", "error_detail")
    return [{k: e.get(k) for k in keys if e.get(k) is not None} for e in infrastructure]


def _event_reason(event: dict) -> str | None:
    detail = ": ".join(str(event[k]) for k in ("error_type", "error_detail") if event.get(k))
    return detail or event.get("classification")


def _infrastructure_gate(summaries: dict, run_dir: Path | None = None) -> dict | None:
    """A smoke that stopped at the infrastructure gate says so in its summary (status or classification), with why;
    a run whose last submission was not consumed for an infrastructure failure says so in the Builder observer's
    events (the Claude tree before 2026-10-10 summarized such a run as latest_candidate_freeze_gate_failure)."""
    events = _unconsumed_infrastructure_submissions(run_dir) if run_dir else []
    observed = _event_reason(events[-1]) if events else None
    names = ("summary.json", "one_stop_summary.json", "readiness_summary.json")
    for name in names:
        summary = summaries.get(name)
        if not isinstance(summary, dict):
            continue
        labels = [str(summary.get(k) or "") for k in ("status", "classification")]
        if any("infrastructure" in label for label in labels):
            gate = {"summary": name, "status": summary.get("status"), "classification": summary.get("classification"),
                    "reason": summary.get("error") or summary.get("reason") or observed}
            if events:
                gate["unconsumed_submissions"] = events
            return gate
    if events:
        summary = next((summaries[n] for n in names if isinstance(summaries.get(n), dict)), {})
        return {"summary": None, "source": str(run_dir / SMOKE_OBSERVER_EVENTS), "status": summary.get("status"),
                "classification": events[-1].get("classification"),
                "summary_classification": summary.get("classification"), "reason": observed,
                "unconsumed_submissions": events}
    return None


def _smoke_status(summaries: dict) -> str | None:
    for name in ("summary.json", "one_stop_summary.json", "readiness_summary.json"):
        summary = summaries.get(name)
        if isinstance(summary, dict) and summary.get("status"):
            return summary["status"]
    readiness = summaries.get("readiness_summary.json")  # Aider and Codex: {complete, errors, ...}
    if isinstance(readiness, dict) and isinstance(readiness.get("complete"), bool):
        return "readiness_complete" if readiness["complete"] else "readiness_incomplete"
    return None


def smoke_outcome(run_dir: Path, summaries: dict) -> dict:
    """What a smoke run found, from the run dir's own files: each dev round's classification and validity, the
    held-out case's state and judged Result (from the readiness result contract), who a failure is attributed to,
    the contract path(s), and the infrastructure-gate reason when the run stopped there."""
    one_stop = summaries.get("one_stop_summary.json") if isinstance(summaries.get("one_stop_summary.json"), dict) else {}
    rounds, rounds_source = _smoke_dev_rounds(run_dir, one_stop)
    held_out, held_out_source = _smoke_held_out(run_dir, summaries)
    contracts = _smoke_contracts(run_dir)
    result_contract = util.read_json(contracts["result"], {}) if "result" in contracts else {}
    result_contract = result_contract if isinstance(result_contract, dict) else {}
    scored = result_contract.get("case_id")
    if scored and not any(row["case_id"] == scored for row in held_out):
        held_out.append({"case_id": scored, "classification": None, "valid": None, "infrastructure_valid": None,
                         "failure_party": None, "failure_reason": None})
    for row in held_out:
        contract = result_contract if result_contract and result_contract.get("case_id") in (row["case_id"], None) \
            else {}
        row["evaluation_state"] = contract.get("evaluation_state")
        row["result_state"] = contract.get("result_state")
        row["result_score"] = contract.get("result_score")
        row["result_score_source"] = "result_score_contract" if contract else None
        row["contract_valid"] = contract.get("contract_valid")
        # without a contract, the case's state is how its execution was classified
        row["state"] = contract.get("result_state") or contract.get("evaluation_state") or row["classification"]
    parties = sorted({row["failure_party"] for row in held_out if row.get("failure_party")})
    outcome = {"status": _smoke_status(summaries), "dev_rounds": rounds, "held_out": held_out,
               "failure_party": parties[0] if len(parties) == 1 else (parties or None),
               "contracts": {role: str(path) for role, path in contracts.items()},
               "sources": {"dev_rounds": rounds_source, "held_out": held_out_source},
               "note": SMOKE_SCORE_NOTE}
    gate = _infrastructure_gate(summaries, run_dir)
    if gate:
        outcome["infrastructure_gate"] = gate
    return outcome


# A formal run whose Builder the time budget cut off. The protocol (task.json `protocol`: max_dev_rounds 5,
# builder_session_sec 18000) ends development when the Builder exits, uses up its accepted submissions or uses up its
# time; the last accepted submission is frozen and scored on the held-out cases, and a Builder with no accepted
# submission delivers nothing and scores 0. The task trees freeze and start the held-out cases only after a Builder
# session that ended with a successful terminal event (or was cut after its last submission), so a session the budget
# cut off stops at the tree's lifecycle gate with no held-out case and no score. budget_cut_state() recognizes such a
# run from its own files (read only); result() reports it as `budget_exhausted`. The cut, the gate and nothing after:
#   the last Builder segment in builder_segment_receipt.json (control/builder_segments.py) is an infrastructure_cut
#     with Harbor's AgentTimeoutError (its agent timeout; Harbor then exits 0), or ended within
#     BUDGET_DEADLINE_MARGIN_SECONDS of the Builder deadline (the outer deadline: exit 124), and no segment followed it;
#   the strict native Builder evidence has exactly one error, the missing terminal event; the tree recorded the
#     Builder's exit as 0 or 124, or as 125 only because the Builder's Harbor trial ended on AgentTimeoutError (Codex's
#     trial attestation, and the pre-agent gate of DeepCode, Dyad and OpenHands, refuse a trial with any exception),
#     with the resource proof valid; the tree's summary has its lifecycle gate status;
#   no held-out evidence and no formal_aggregation.json.
BUDGET_DEADLINE_MARGIN_SECONDS = 600.0
BUDGET_SEGMENT_RECEIPT = "builder_segment_receipt.json"
BUDGET_TERMINAL_ERROR = "native Builder has no successful terminal event"
# tree: (summary, its lifecycle gate status, strict native evidence (file, key; None: the whole file),
#        accepted submission records (file, or a glob of one record per file; key, None: the whole document))
BUDGET_CUT_LAYOUTS = {
    "aider": ("summary.json", "builder_integration_incomplete", ("builder_session_attestation.json", "native_evidence"),
              ("builder_session_attestation.json", "candidate_records")),
    "ai-scientist": ("summary.json", "builder_integration_incomplete",
                     ("builder_session_attestation.json", "native_evidence"), ("lifecycle/dev_lifecycle.json", "records")),
    "claude": ("summary.json", "builder_integration_incomplete", ("builder_session_attestation.json", "native_evidence"),
               ("builder_session_attestation.json", "candidate_records")),
    "codex": ("one_stop_summary.json", "builder_lifecycle_incomplete", ("one_stop_summary.json", "native_evidence"),
              ("one_stop_summary.json", "dev_lifecycle")),
    "deepcode": ("summary.json", "builder_lifecycle_incomplete",
                 ("builder_session_attestation.json", "native_attestation"),
                 ("lifecycle/dev_feedback_candidate_[0-9][0-9][0-9].json", None)),
    "deeptutor": ("summary.json", "formal_evidence_incomplete", ("builder_native_attestation.json", None),
                  ("lifecycle/controller_state.json", "records")),
    "dyad": ("summary.json", "builder_lifecycle_incomplete", ("builder_session_attestation.json", "native_attestation"),
             ("lifecycle/dev_lifecycle.json", None)),
    "openclaw": ("summary.json", "builder_lifecycle_incomplete", ("builder_session_attestation.json", "native_evidence"),
                 ("builder_session_attestation.json", "submissions")),
    "openhands": ("summary.json", "builder_integration_incomplete",
                  ("builder_session_attestation.json", "native_evidence"),
                  ("builder_session_attestation.json", "candidate_records")),
    "openwiki": ("summary.json", "builder_lifecycle_incomplete", ("builder_session_attestation.json", "native_evidence"),
                 ("builder_session_attestation.json", "candidate_records")),
}
# What the trees write once the held-out cases start (attestations, case dirs, the fresh held-out broker's first
# stats) or are aggregated; none of it exists at the lifecycle gate.
BUDGET_AFTER_GATE = ("formal_aggregation.json", "formal_scoring", "hidden", "hidden_after_freeze",
                     "hidden-after-freeze-attestation.json", "hidden_after_freeze_attestation.json",
                     "lifecycle/hidden-after-freeze-attestation.json", "lifecycle/hidden_after_freeze_attestation.json",
                     "lifecycle/hidden", "lifecycle/evaluations/hidden", "evaluations/hidden", "hidden-result.json",
                     "lifecycle/hidden-result.json", "hidden_result.json", "lifecycle/hidden_result.json",
                     "hidden_broker_initial.json", "hidden_broker_before.json", "hidden_lower_broker_initial.json")
BUDGET_PREAGENT_GATE = "builder_preagent_gate_attestation.json"
BUDGET_PREAGENT_TRIAL_ERROR = "Harbor trial failed or never started the native Agent"
BUDGET_CODEX_GATE_ERRORS = ("Builder native feedback evidence incomplete: ", "Builder Harbor failed: ",
                            "Builder did not produce an accepted Candidate")
BUDGET_RECORD_NUMBER = ("submission_number", "source_submission", "submission", "number", "round", "candidate_number")
BUDGET_RECORD_ID = ("submission_id", "source_submission_id", "candidate_id")
# Editing tasks for which `agentswe freeze <run_id>` freezes a budget-cut run and runs its held-out cases
# (editing_budget_freeze.py, runners/editing/tools/budget_freeze.py); the other trees have no freeze command yet.
BUDGET_FREEZE_TASKS: frozenset[str] = frozenset({"aider", "deeptutor", "openwiki"})
BUDGET_RULE = ("Development ends when the Builder exits, uses up its 5 accepted submissions or uses up its 5-hour "
               "budget. The last accepted submission is frozen and scored on the held-out cases; a Builder with no "
               "accepted submission delivers nothing and scores 0.")
BUDGET_DOCS = "docs/ENV.md, section \"When the Editing Builder budget ends\""


def _run_task_key(run_dir: Path) -> str | None:
    """The tree a formal run dir belongs to (<formal root>/codex_xhigh/<task>/<launch id>-<task>)."""
    if run_dir.parent.name in BUDGET_CUT_LAYOUTS and run_dir.name.endswith("-" + run_dir.parent.name):
        return run_dir.parent.name
    return next((key for key in sorted(BUDGET_CUT_LAYOUTS, key=len, reverse=True)
                 if run_dir.name.endswith("-" + key)), None)


def _last_builder_segment(run_dir: Path) -> tuple[dict, dict] | None:
    """(the receipt, the last segment whose trial was promoted), when that segment ended the Builder session."""
    receipt = util.read_json(run_dir / BUDGET_SEGMENT_RECEIPT)
    attempts = receipt.get("attempts") if isinstance(receipt, dict) else None
    segments = [a for a in attempts if isinstance(a, dict) and a.get("promoted")] if isinstance(attempts, list) else []
    if not segments or not isinstance(receipt.get("builder_deadline_epoch"), (int, float)):
        return None
    last = segments[-1]
    if (last.get("decision") or {}).get("resume") is True:
        return None  # a resume was decided: the session did not end with this segment
    return receipt, last


def _budget_cut_segment(receipt: dict, last: dict) -> dict | None:
    exception = last.get("harbor_exception") if isinstance(last.get("harbor_exception"), dict) else {}
    ended = last.get("ended_at_epoch")
    left = receipt["builder_deadline_epoch"] - ended if isinstance(ended, (int, float)) else None
    timed_out = last.get("exit_reason") == "infrastructure_cut" and exception.get("exception_type") == "AgentTimeoutError"
    at_deadline = (left is not None and left <= BUDGET_DEADLINE_MARGIN_SECONDS
                   and last.get("exit_reason") in ("infrastructure_cut", "outer_timeout"))
    if last.get("exit_code") not in (0, 124) or not (timed_out or at_deadline):
        return None
    return {"segment": last.get("segment_index"), "exit_reason": last.get("exit_reason"),
            "harbor_exit_code": last.get("exit_code"), "harbor_exception": exception.get("exception_type"),
            "ended_seconds_before_deadline": round(left, 1) if left is not None else None,
            "budget_seconds": receipt.get("budget_seconds")}


def _codex_budget_exit(summary: dict, cut: dict) -> bool:
    """Codex's gate: its errors are the native one, the Harbor exit and (none accepted) the missing Candidate; a 125
    is the budget cut only when every trial exception is AgentTimeoutError (or, at the outer deadline, none was
    recorded) and the resource and cleanup proofs are valid."""
    errors = summary.get("gate_errors")
    if not isinstance(errors, list) or not all(isinstance(e, str) and e.startswith(BUDGET_CODEX_GATE_ERRORS)
                                               for e in errors):
        return False
    builder = summary.get("builder") if isinstance(summary.get("builder"), dict) else {}
    code = builder.get("exit_code")
    if code in (0, 124):
        return True
    invalid = builder.get("infrastructure_invalid") if isinstance(builder.get("infrastructure_invalid"), dict) else {}
    if code != 125 or invalid.get("resources_valid") is not True or not (
            invalid.get("cleanup_complete") is True or invalid.get("cleanup_retained_terminal") is True):
        return False
    trials = invalid.get("trials") if isinstance(invalid.get("trials"), list) else []
    types = {(t.get("exception_info") or {}).get("exception_type") for t in trials
             if isinstance(t, dict) and t.get("exception_info")}
    if types:
        return types == {"AgentTimeoutError"}
    left = cut.get("ended_seconds_before_deadline")
    return left is not None and left <= BUDGET_DEADLINE_MARGIN_SECONDS


def _trial_timeout_125(run_dir: Path) -> bool:
    """DeepCode, Dyad and OpenHands record 125 when the Builder's Harbor trial has any exception: it is the budget cut
    when that trial started the Agent, its only exception is AgentTimeoutError and the resource proof is valid."""
    resources = util.read_json(run_dir / "builder_resource_attestation.json")
    gate = util.read_json(run_dir / BUDGET_PREAGENT_GATE)
    if not (isinstance(resources, dict) and resources.get("valid") is True and isinstance(gate, dict)):
        return False
    trial = gate.get("trial") if isinstance(gate.get("trial"), dict) else {}
    return (gate.get("errors") == [BUDGET_PREAGENT_TRIAL_ERROR]
            and (trial.get("exception_info") or {}).get("exception_type") == "AgentTimeoutError"
            and bool(trial.get("agent_setup")) and bool(trial.get("agent_execution")))


def _accepted_record(record) -> bool:
    """A submission the tree's controller accepted and finished evaluating: not refused, not left non-consuming
    (an infrastructure attempt, a completion after the freeze), not still running."""
    if not isinstance(record, dict) or record.get("accepted") is False or record.get("post_freeze_superseded"):
        return False
    if any(record.get(key) is False for key in ("consumed", "submission_consumed", "round_consumed")):
        return False
    state = str(record.get("state") or "")
    return state != "running" and not any(word in state for word in ("infrastructure", "unresolved", "in_progress"))


def _budget_records(run_dir: Path, spec: tuple[str, str | None]) -> tuple[list[dict], str | None]:
    """The accepted submissions in the order the tree recorded them, and where they were read."""
    name, key = spec
    if any(ch in name for ch in "*?["):
        paths = sorted(run_dir.glob(name))
        records = [util.read_json(p) for p in paths]
        source = str(run_dir / name) if paths else None
    else:
        value = util.read_json(run_dir / name)
        records = value.get(key) if isinstance(value, dict) and key else value
        if isinstance(records, dict) and isinstance(records.get("records"), list):
            records = records["records"]
        source = str(run_dir / name) if value is not None else None
    return [r for r in records if _accepted_record(r)] if isinstance(records, list) else [], source


def _submission_label(record: dict) -> dict:
    label = {}
    number = next((record[k] for k in BUDGET_RECORD_NUMBER if isinstance(record.get(k), int)), None)
    if number is not None:
        label["submission"] = number
    ident = next((record[k] for k in BUDGET_RECORD_ID if isinstance(record.get(k), str) and record[k]), None)
    if ident:
        label["id"] = ident
    if isinstance(record.get("candidate_digest"), str) and record["candidate_digest"]:
        label["digest"] = record["candidate_digest"]
    return label


def budget_cut_state(run_dir: Path, *, task: str | None = None, unit_state: str | None = None) -> dict | None:
    """What a formal Editing run the Builder budget cut off left behind, or None for any other run (read only).

    `task` is the control-plane task name (TASK_KEYS; default: from the run dir's name); `unit_state` the run's
    systemd ActiveState (a live unit is never a cut). Returns the tree, the gate status and summary, the cut segment,
    and the distinct accepted submissions with the latest one (its number, id and digest where the tree records them).
    """
    if unit_state in LIVE_UNIT_STATES or not run_dir.is_dir():
        return None
    key = task or _run_task_key(run_dir)
    layout = BUDGET_CUT_LAYOUTS.get(key or "")
    if layout is None or (run_dir / "readiness_current_binding.json").exists():
        return None  # not an Editing tree, or a readiness (smoke) run
    if any((run_dir / rel).exists() for rel in BUDGET_AFTER_GATE):
        return None
    summary_name, gate_status, (native_file, native_key), records_spec = layout
    summary = util.read_json(run_dir / summary_name)
    if not isinstance(summary, dict) or summary.get("status") != gate_status:
        return None
    hidden = summary.get("hidden")
    if isinstance(hidden, dict) and hidden.get("status") not in (None, "not_started"):
        return None
    segment = _last_builder_segment(run_dir)
    cut = _budget_cut_segment(*segment) if segment else None
    if cut is None:
        return None
    document = util.read_json(run_dir / native_file)
    native = document.get(native_key) if isinstance(document, dict) and native_key else document
    if not isinstance(native, dict) or native.get("errors") != [BUDGET_TERMINAL_ERROR]:
        return None
    if key == "codex":
        if not _codex_budget_exit(summary, cut):
            return None
        recorded = (summary.get("builder") or {}).get("exit_code") if isinstance(summary.get("builder"), dict) else None
    else:
        builder = document.get("builder_process") if isinstance(document.get("builder_process"), dict) else {}
        recorded = next((v for v in (native.get("builder_exit_code"), document.get("builder_exit_code"),
                                     builder.get("exit_code"), summary.get("builder_exit_code"))
                         if isinstance(v, int)), None)
        if recorded not in (None, 0, 124) and not (recorded == 125 and _trial_timeout_125(run_dir)):
            return None
    if isinstance(recorded, int):
        cut["recorded_exit_code"] = recorded  # the Builder exit the tree recorded (125 for a refused trial)
    records, source = _budget_records(run_dir, records_spec)
    distinct = {record.get("candidate_digest") or json.dumps(_submission_label(record), sort_keys=True)
                for record in records}
    return {"task": key, "status": gate_status, "summary": summary_name, "builder": cut,
            "accepted_submissions": len(distinct),
            "latest_accepted": _submission_label(records[-1]) if records else None, "records": source}


def budget_exhausted_report(state: dict, run_id: str) -> dict:
    """The `budget_exhausted` block of `agentswe result`: the protocol's rule and what it means for this run."""
    block = {"accepted_submissions": state["accepted_submissions"], "latest_accepted": state["latest_accepted"],
             "rule": BUDGET_RULE, "held_out": "not run"}
    if state["accepted_submissions"] == 0:
        block.update(scored=True, score=0, score_basis="No submission was accepted before the Builder budget ended; "
                                                       "the protocol scores this as 0.")
    else:
        block["scored"] = False
        block["next"] = (f"agentswe freeze {run_id}: freeze the latest accepted submission and run the held-out cases"
                         if state["task"] in BUDGET_FREEZE_TASKS else
                         f"A freeze command is not yet available for this task, so this run has no score; see "
                         f"{BUDGET_DOCS}.")
    block.update(builder=state["builder"], gate_status=state["status"], summary=state["summary"])
    return block


def derived_freeze_reason(run_dir: Path, summaries: dict) -> dict | None:
    """How a frozen formal run's development ended, when one_stop_summary.json records the freeze with no reason
    (some trees' freeze manifests carry none): a budget freeze names itself in manual_freeze/manual_freeze_record.json;
    otherwise a Builder that exited 0 without timing out (builder_process.json) ended development itself."""
    freeze = (summaries.get("one_stop_summary.json") or {}).get("freeze") \
        if isinstance(summaries.get("one_stop_summary.json"), dict) else None
    if not isinstance(freeze, dict) or not freeze.get("digest") or freeze.get("reason"):
        return None
    record = util.read_json(run_dir / "manual_freeze" / "manual_freeze_record.json")
    if isinstance(record, dict) and record.get("reason"):
        return {"reason": record["reason"], "source": "manual_freeze/manual_freeze_record.json"}
    process = util.read_json(run_dir / "builder_process.json")
    if isinstance(process, dict) and process.get("exit_code") == 0 and process.get("timed_out") is False:
        return {"reason": "builder_exit", "source": "builder_process.json"}
    return None


def result(cfg: Config, launch: dict) -> dict | None:
    st = status(cfg, launch)
    if st["alive"]:
        return None
    _release_credential(cfg, launch)
    run_dir = Path(st["run_dir"]) if st["run_dir"] else None
    data = {}
    for p in sorted(run_dir.glob("*summary*.json")) if run_dir and run_dir.is_dir() else []:
        data[p.name] = util.read_json(p)
    res = {"run_id": launch["run_id"], "task": launch["task"], "family": launch["family"], "mode": launch["mode"],
           "comparable": launch["comparable"], "builder": launch["builder"], "unit_state": st["unit_state"],
           "exit_status": st["exit_status"], "summaries": data}
    # The headline number: every formal one-stop writes formal_aggregation.json (result_axis: score, case_scores).
    aggregation = util.read_json(run_dir / "formal_aggregation.json") if run_dir else None
    if isinstance(aggregation, dict):
        res["formal_result_publishable"] = aggregation.get("formal_result_publishable")
        res["result_axis"] = aggregation.get("result_axis")
    if launch.get("mode") == "formal" and run_dir and run_dir.is_dir():
        reason = derived_freeze_reason(run_dir, data)
        if reason:
            res["freeze_reason"] = reason
    # A smoke's summaries say result_axis "N/A" and combined_score null; its outcome is in the run's own files.
    if launch.get("mode") == "smoke" and run_dir and run_dir.is_dir():
        res["smoke"] = smoke_outcome(run_dir, data)
    # A formal run whose Builder the time budget cut off stops at its tree's lifecycle gate without a score; say what
    # the protocol makes of it (0 when no submission was accepted).
    if launch.get("mode") == "formal" and run_dir:
        cut = budget_cut_state(run_dir, task=TASK_KEYS.get(launch["task"]), unit_state=st["unit_state"])
        if cut:
            res["budget_exhausted"] = budget_exhausted_report(cut, launch["run_id"])
    # A run that stopped before writing its summaries (for example a one-stop refusing to start) says why here.
    if st["orchestrator_log"] and (not data or st["unit_state"] == "failed"):
        res["orchestrator_log"] = st["orchestrator_log"]
        res["orchestrator_log_tail"] = Path(st["orchestrator_log"]).read_text(errors="replace").splitlines()[-20:]
    util.write_json(Path(launch["log"]).with_name(launch["run_id"] + ".result.json"), res)
    return res


def stop(cfg: Config, launch: dict) -> None:
    util.run(["systemctl", "stop", launch["unit"]], check=False)
    removed = _remove_run_objects(cfg, launch)
    if removed:
        util.log(f"removed {len(removed)} docker objects owned by {launch['run_id']} (see DELETIONS.log)")
    _release_credential(cfg, launch)


def freeze(cfg: Config, launch: dict, *, stage: str = "check", apply: bool = False, operator: str | None = None) -> int:
    """`agentswe freeze`: the budget freeze of a formal run cut at its time budget (editing_budget_freeze.py)."""
    from .editing_budget_freeze import freeze as budget_freeze
    return budget_freeze(cfg, launch, stage=stage, apply=apply, operator=operator)
