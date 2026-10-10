#!/usr/bin/env python3
"""Evaluator-owned dynamic action service for Aider lower-agent cases."""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class CaseRuntime:
    def __init__(self, spec: dict[str, Any], root: Path, state_path: Path) -> None:
        self.spec, self.root, self.state_path = spec, root, state_path
        self.nonce = secrets.token_hex(12)
        self.case_id = str(spec.get("case_id", ""))
        self.case_task = str(spec.get("task_input", ""))
        self.case_task_digest = hashlib.sha256(self.case_task.encode("utf-8")).hexdigest()
        scenario_value = spec.get("scenario_asset") if isinstance(spec.get("scenario_asset"), dict) else {}
        self.scenario_asset = scenario_value
        self.scenario_digest = hashlib.sha256(
            json.dumps(self.scenario_asset, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.crash_armed = False
        # D49 (2026-09-21): crash_armed flips when the fixture DECIDES to inject;
        # crash_injected records that a `crash` directive was actually handed to the
        # candidate adapter.  The harness uses the False->True edge of this flag to
        # recognise the one turn in which the evaluator itself crashed the product.
        self.crash_injected = False
        self.lock = threading.RLock()
        self.invocations: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.product_observations: list[dict[str, Any]] = []
        self._command_log_high_water = 0
        self.repo = root / "repo"
        self.state_dir = root / "state"
        self.repo_specs = self._case_repo_specs()
        self.repo_paths: dict[str, Path] = {}
        self.base_oids: dict[str, str] = {}
        self._make_repo()
        self.preservation_baseline = self._capture_preservation_baseline()
        self._initial_inventory_cache: dict[str, Any] | None = None
        self._initial_inventory()
        self._persist()

    def _case_repo_specs(self) -> list[dict[str, Any]]:
        """Choose an observable fixture topology for this case.

        These are evaluator-created inputs, not expected results.  The private
        terminal oracle remains in ``state_path``, which is outside the
        Candidate bind mount.
        """
        names = self.scenario_asset.get("repositories")
        if not isinstance(names, list) or "root" not in names:
            names = {
                # The public dev worlds carry the component repository their
                # own manifests already declare
                # (dev_cases/dev_00N/assets/manifest.json#repositories.component,
                # gitlink_path), laid out the way the hidden worlds are, so the
                # dev channel is no longer topology-blind: before this, every
                # dev execution ran a single-repository world and no public
                # feedback round could exercise -- or even observe --
                # cross-repository behaviour, while every hidden world has two
                # or three participants.  dev_002 carries two components so at
                # least one public round has the same shape as test_001 and
                # test_003.  dev_001 stays at two repositories because it is
                # the case the readiness profile runs for two rounds.
                "dev_001": ["root", "component"],
                "dev_002": ["root", "component-a", "component-b"],
                "test_001": ["root", "component-a", "component-b"],
                "test_002": ["root", "component"],
                "test_003": ["root", "shape-component", "nested-component"],
                "test_004": ["root", "component"],
                "test_005": ["root", "component"],
                "test_006": ["root", "component"],
            }.get(self.case_id, ["root"])
        names = [str(name) for name in names]
        return [{"id": name, "role": "root" if name == "root" else "component"} for name in names]

    def _git(self, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=self.repo, text=True, capture_output=True, check=False)
        if done.returncode:
            raise RuntimeError(done.stderr[-600:])
        return done.stdout.strip()

    def _git_at(self, repo: Path, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
        if done.returncode:
            raise RuntimeError(done.stderr[-600:])
        return done.stdout.strip()

    def _make_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for item in self.repo_specs:
            repo = self.repo if item["id"] == "root" else self.repo / "components" / item["id"]
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            self.repo_paths[item["id"]] = repo
            self._git_at(repo, "config", "user.name", "AgentSWE dynamic fixture")
            self._git_at(repo, "config", "user.email", "fixture@example.invalid")
            if item["id"] == "root":
                (repo / "transaction.txt").write_text(f"root-base:{self.nonce}\n", encoding="utf-8")
                (repo / "README.case").write_text("dynamic root repository\n", encoding="utf-8")
                paths = ["transaction.txt", "README.case"]
            else:
                (repo / "component.txt").write_text(f"{item['id']}-base:{self.nonce}\n", encoding="utf-8")
                paths = ["component.txt"]
            self._git_at(repo, "add", *paths)
            self._git_at(repo, "commit", "-q", "-m", "dynamic base")
            self.base_oids[item["id"]] = self._git_at(repo, "rev-parse", "HEAD")
            # Dirty-state and conflict fixtures are deliberately created after
            # the base OID is captured, so the lower product must observe and
            # preserve them rather than mistaking them for the clean base.
            if self.case_id == "test_005" and item["id"] == "component":
                (repo / "component.txt").write_text(f"{item['id']}-dirty:{self.nonce}\n", encoding="utf-8")
                (repo / "dirty-untracked.txt").write_text("foreign dirty bytes\n", encoding="utf-8")
                (repo / "mode-preserved.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                (repo / "mode-preserved.sh").chmod(0o755)
                os.symlink("component.txt", repo / "component-link")
            if self.case_id == "test_003" and item["id"] in {"shape-component", "nested-component"}:
                (repo / "mode-preserved.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                (repo / "mode-preserved.sh").chmod(0o755)
                os.symlink("component.txt", repo / "component-link")
                # Shape preservation is a clean-base requirement in this case,
                # not an undeclared dirty-snapshot obstruction.
                self._git_at(repo, 'add', 'mode-preserved.sh', 'component-link')
                self._git_at(repo, 'commit', '-q', '-m', 'tracked baseline entry shapes')
                self.base_oids[item['id']] = self._git_at(repo, 'rev-parse', 'HEAD')
            if self.case_id == "test_002" and item["id"] == "component":
                atomic_json(repo / ".git" / "aider" / "transactions" / "abandoned-worktree.json", {
                    "schema_version": 3, "owner": "interrupted-worker", "object_visibility": "quarantined",
                    "cleanup_pending": True, "fixture_only": True,
                })
        components = [item for item in self.repo_specs if item['id'] != 'root']
        for item in components:
            name = item['id']
            relative = 'components/' + name
            # Real mode-160000 index entries and .gitmodules replace the old
            # merely nested directories. No expected transaction result is
            # seeded; these are the actual initial repository relationships.
            self._git_at(self.repo, 'config', '--file', '.gitmodules', f'submodule.{name}.path', relative)
            self._git_at(self.repo, 'config', '--file', '.gitmodules', f'submodule.{name}.url', './' + relative)
            self._git_at(self.repo, 'config', f'submodule.{name}.url', '/case-work/repo/' + relative)
            self._git_at(self.repo, 'update-index', '--add', '--cacheinfo',
                         f"160000,{self.base_oids[name]},{relative}")
        if components:
            self._git_at(self.repo, 'add', '.gitmodules')
            self._git_at(self.repo, 'commit', '-q', '-m', 'register actual component gitlinks')
            self.base_oids['root'] = self._git_at(self.repo, 'rev-parse', 'HEAD')
        self.base_oid = self.base_oids["root"]
        if self.case_id == 'test_005':
            # The reverse case promises exact restoration in both participants,
            # including the root; previously only the component was dirty.
            (self.repo / 'transaction.txt').write_text(f'root-dirty:{self.nonce}\n', encoding='utf-8')
            (self.repo / 'dirty-untracked.txt').write_text('foreign root dirty bytes\n', encoding='utf-8')
        worker = self.root / "worker.py"
        # The evaluator-owned worker/test command appends one append-only row per
        # actual execution. Nothing about the command's success or failure
        # changes; the row is the evaluator's independent record of how many
        # times each declared command really ran, which is how "commands,
        # hooks, filters and commits complete at most once" is observed instead
        # of being taken from the response.
        worker.write_text(
            "import os,sys\n"
            "from pathlib import Path\n"
            "try:\n"
            "    _row='\\t'.join([sys.argv[1] if len(sys.argv)>1 else '',\n"
            "        os.environ.get('AIDER_REPOSITORY_ID',''), os.environ.get('AIDER_SUBTASK_ID',''),\n"
            "        os.environ.get('AIDER_PLAN_ID',''), os.environ.get('AIDER_WORKTREE','')])\n"
            "    with open(Path(__file__).resolve().parent/'command_invocations.log','a',encoding='utf-8') as _h:\n"
            "        _h.write(_row+'\\n')\n"
            "except OSError:\n"
            "    pass\n"
            "worktree=Path(os.environ['AIDER_WORKTREE'])\n"
            "repo_id=os.environ.get('AIDER_REPOSITORY_ID','root')\n"
            "name='transaction.txt' if repo_id=='root' else 'component.txt'\n"
            "p=worktree/name\n"
            "if sys.argv[1]=='worker': p.write_text(p.read_text()+os.environ['AIDER_PLAN_ID']+'\\n')\n"
            "elif sys.argv[1]=='test': raise SystemExit(0 if p.is_file() else 1)\n",
            encoding="utf-8",
        )
        if self.case_id in {"test_004", "test_006"}:
            locked = self.repo_paths.get("component", self.repo)
            admission = locked / ".git" / "aider" / "transactions" / "admission.json"
            atomic_json(admission, {
                "schema_version": 3,
                "transaction_id": f"foreign-{self.nonce[:10]}",
                "expires": int(time.time() * 1000) + 120000,
                "fence": 7,
                "owner": "foreign-writer",
            })
        if self.case_id == "test_004":
            component = self.repo_paths["component"]
            (component / "foreign-author.txt").write_text("preserve external author\n", encoding="utf-8")
            (component / "rename-source.txt").write_text("foreign rename source\n", encoding="utf-8")
            atomic_json(component / ".git" / "aider" / "transactions" / "foreign-ref.json", {
                "schema_version": 3, "owner": "foreign-writer", "ref": "refs/heads/foreign-release",
                "generation": 11, "fixture_only": True,
            })
        if self.case_id == "test_006":
            component = self.repo_paths.get("component", self.repo)
            atomic_json(component / ".git" / "aider" / "transactions" / "global-decision.json", {
                "schema_version": 3, "decision_digest": "corrupt-foreign-digest", "generation": 0,
                "fixture_only": True,
            })
            atomic_json(self.repo / ".git" / "aider" / "transactions" / "alias-guard.json", {
                "schema_version": 3, "repository_alias": str(component), "fixture_only": True,
            })

    def plan(self) -> dict[str, Any]:
        case_id = self.case_id
        # Integration order names subtasks; publication order names repositories.
        # Every real component precedes its parent even in blocked/guard worlds,
        # so an invalid evaluator plan cannot preempt the intended fault.
        ordered = [item['id'] for item in self.repo_specs if item['id'] != 'root'] + ['root']
        links = [
            {"parent_repository_id": "root", "child_repository_id": item["id"], "path": f"components/{item['id']}"}
            for item in self.repo_specs if item["id"] != "root"
        ]
        repositories, subtasks, integration_tests = [], [], []
        for item in self.repo_specs:
            identifier = item["id"]
            filename = "transaction.txt" if identifier == "root" else "component.txt"
            repositories.append({
                "id": identifier,
                "path": "/case-work/repo" if identifier == "root" else f"/case-work/repo/components/{identifier}",
                "base_revision": self.base_oids[identifier],
                "base_state_policy": "snapshot" if case_id == "test_005" else "require_clean",
                "role": item["role"],
            })
            depends = [] if identifier != "root" else [f"edit-{child}" for child in ordered if child != "root"]
            subtasks.append({
                "id": f"edit-{identifier}", "repository_id": identifier, "depends_on": depends,
                "allowed_paths": [filename],
                "worker": {"argv": ["python3", "/case-work/worker.py", "worker"]},
                "test": {"argv": ["python3", "/case-work/worker.py", "test"]},
            })
            integration_tests.append({"repository_id": identifier, "command": {"argv": ["python3", "/case-work/worker.py", "test"]}})
        return {
            "repositories": repositories, "integration_order": ['edit-' + identifier for identifier in ordered],
            "subtasks": subtasks, "integration_tests": integration_tests,
            "publication": {
                "targets": [{"repository_id": item["id"], "ref": "refs/heads/main", "expected_oid": self.base_oids[item["id"]]} for item in self.repo_specs],
                "participant_order": ordered, "links": links,
                "commit_messages": [{"repository_id": item["id"], "message": f"dynamic {case_id} transaction"} for item in self.repo_specs],
                "on_ref_drift": "abort", "after_decision": "roll_forward",
            },
        }

    def max_workers(self) -> int:
        return 3 if self.case_id == "test_001" else 1

    def owner(self) -> dict[str, Any]:
        coordination = self.responses.get("create", {}).get("coordination", {})
        return {"id": coordination.get("owner_id", "agent-owner"), "token": coordination.get("lease_token"), "fence": coordination.get("fence")}

    @staticmethod
    def _path_state(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            return {"kind": "symlink", "target": os.readlink(path)}
        if path.is_file():
            return {
                "kind": "file",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        if path.is_dir():
            return {"kind": "directory"}
        return {"kind": "missing"}

    def _capture_preservation_baseline(self) -> dict[str, dict[str, Any]]:
        paths_by_case = {
            "test_003": (
                "components/shape-component/mode-preserved.sh",
                "components/shape-component/component-link",
                "components/nested-component/mode-preserved.sh",
                "components/nested-component/component-link",
            ),
            "test_004": (
                "components/component/foreign-author.txt",
                "components/component/rename-source.txt",
                "components/component/.git/aider/transactions/foreign-ref.json",
                "components/component/.git/aider/transactions/admission.json",
            ),
            "test_005": (
                "transaction.txt",
                "dirty-untracked.txt",
                "components/component/component.txt",
                "components/component/dirty-untracked.txt",
                "components/component/mode-preserved.sh",
                "components/component/component-link",
            ),
            "test_006": (
                "components/component/.git/aider/transactions/global-decision.json",
                ".git/aider/transactions/alias-guard.json",
                "components/component/.git/aider/transactions/admission.json",
            ),
        }
        paths = paths_by_case.get(self.case_id, ())
        return {relative: self._path_state(self.repo / relative) for relative in paths}

    def _preservation_unchanged(self, case_id: str) -> bool:
        if case_id != self.case_id:
            return False
        return all(
            self._path_state(self.repo / relative) == state
            for relative, state in self.preservation_baseline.items()
        )

    def _admission_inventory(self) -> dict[str, str]:
        """Coordinator files under each repository's Git common directory.

        ``aider/transactions/`` is the only coordinator-owned location allowed
        inside a participant (02_interface_and_delivery). Reading it directly is
        how admission acquisition, release and foreign-receipt preservation are
        observed without trusting the response.
        """
        rows: dict[str, str] = {}
        for identifier, repo in self.repo_paths.items():
            base = repo / ".git" / "aider" / "transactions"
            try:
                if not base.is_dir() or base.is_symlink():
                    continue
                for item in sorted(base.rglob("*")):
                    if len(rows) >= 512:
                        break
                    if item.is_symlink() or not item.is_file():
                        continue
                    data = item.read_bytes()[: 1024 * 1024]
                    rows[identifier + ":" + item.relative_to(base).as_posix()] = hashlib.sha256(data).hexdigest()
            except (OSError, ValueError):
                continue
        return rows

    def _ref_inventory(self) -> dict[str, list[str]]:
        rows: dict[str, list[str]] = {}
        for identifier, repo in self.repo_paths.items():
            try:
                listing = self._git_at(repo, "for-each-ref", "--format=%(refname) %(objectname)")
                rows[identifier] = sorted(line for line in listing.splitlines() if line.strip())
            except (OSError, RuntimeError):
                rows[identifier] = ["observation_error"]
        return rows

    def _worktree_inventory(self) -> dict[str, list[str]]:
        rows: dict[str, list[str]] = {}
        for identifier, repo in self.repo_paths.items():
            try:
                listing = self._git_at(repo, "worktree", "list", "--porcelain")
                rows[identifier] = sorted(line.split(" ", 1)[1] for line in listing.splitlines()
                                          if line.startswith("worktree "))
            except (OSError, RuntimeError):
                rows[identifier] = ["observation_error"]
        return rows

    def _gitlink_bindings(self) -> dict[str, dict[str, str]]:
        """Actual ``160000`` entries of each published root ref and its checkout."""
        rows: dict[str, dict[str, str]] = {}
        for source in ("refs/heads/main", "HEAD"):
            entries: dict[str, str] = {}
            for item in self.repo_specs:
                identifier = item["id"]
                if identifier == "root":
                    continue
                try:
                    line = self._git_at(self.repo, "ls-tree", source, "components/" + identifier)
                    parts = line.split()
                    entries[identifier] = parts[2] if len(parts) >= 3 and parts[0] == "160000" else "absent"
                except (OSError, RuntimeError, IndexError):
                    entries[identifier] = "observation_error"
            rows[source] = entries
        return rows

    def _command_log(self) -> dict[str, Any]:
        """Evaluator-side tally of real worker/test executions."""
        path = self.root / "command_invocations.log"
        summary: dict[str, Any] = {"present": False, "rows": 0, "bytes": 0, "counts": {}, "truncated_since_start": False}
        try:
            data = path.read_bytes()
        except OSError:
            return summary
        summary["present"] = True
        summary["bytes"] = len(data)
        counts: dict[str, int] = {}
        rows = 0
        for line in data.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            rows += 1
            fields = line.split("\t")
            kind = fields[0] if fields else ""
            repository = fields[1] if len(fields) > 1 else ""
            subtask = fields[2] if len(fields) > 2 else ""
            counts[kind + "|" + repository + "|" + subtask] = counts.get(kind + "|" + repository + "|" + subtask, 0) + 1
        summary["rows"] = rows
        summary["counts"] = counts
        summary["truncated_since_start"] = len(data) < self._command_log_high_water
        self._command_log_high_water = max(self._command_log_high_water, len(data))
        return summary

    def _object_store_state(self) -> dict[str, Any]:
        """The content-addressed byte store the published contract requires.

        ``sha256:<hex>`` names bytes at ``state_dir/objects/sha256/<hex>`` and
        those bytes must hash to ``<hex>`` (02_interface_and_delivery.md).
        """
        base = self.state_dir / "objects" / "sha256"
        summary: dict[str, Any] = {"present": False, "files": 0, "mismatched": 0}
        try:
            if base.is_dir() and not base.is_symlink():
                summary["present"] = True
                inspected = 0
                for item in sorted(base.iterdir()):
                    if inspected >= 64:
                        break
                    if item.is_symlink() or not item.is_file():
                        continue
                    inspected += 1
                    data = item.read_bytes()[: 8 * 1024 * 1024]
                    if hashlib.sha256(data).hexdigest() != item.name:
                        summary["mismatched"] += 1
                summary["files"] = inspected
        except (OSError, ValueError):
            pass
        digests = self._ledger_digests()
        summary["ledger_digests"] = len(digests)
        summary["resolved_digests"] = sum(1 for value in digests if (base / value).is_file())
        return summary

    def _ledger_digests(self) -> list[str]:
        try:
            raw = (self.state_dir / "ledger.json").read_bytes()[: 8 * 1024 * 1024]
        except OSError:
            return []
        return sorted({match.decode("ascii") for match in re.findall(rb"sha256:([0-9a-f]{64})", raw)})

    def _object_store_valid(self) -> bool | None:
        """``None`` until the product actually builds a candidate to store."""
        observations = range(len(self.product_observations))
        built = any(self._observation(index).get("candidate_oids_reported")
                    for index in observations) or self._publication_verified()
        if not built:
            return None
        store = self._observation(len(self.product_observations) - 1).get("object_store") or {}
        if not store.get("present") or not store.get("files") or store.get("mismatched"):
            return False
        total = int(store.get("ledger_digests") or 0)
        if not total:
            return True
        return int(store.get("resolved_digests") or 0) * 2 >= total

    def _response_candidate_oids(self, response: dict[str, Any]) -> dict[str, list[str]]:
        rows: dict[str, list[str]] = {}
        entries = response.get("repositories")
        if not isinstance(entries, list):
            return rows
        for item in entries:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id")
            if identifier not in self.repo_paths:
                continue
            found = [str(item.get(key)) for key in ("candidate_commit", "tree_oid")
                     if isinstance(item.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", str(item.get(key)))]
            if found:
                rows[str(identifier)] = found
        return rows

    def _candidate_objects_visible(self, response: dict[str, Any]) -> bool:
        """True when a reported candidate object is already in a participant ODB."""
        for identifier, oids in self._response_candidate_oids(response).items():
            repo = self.repo_paths.get(identifier)
            if repo is None:
                continue
            for oid in oids:
                try:
                    done = subprocess.run(["git", "cat-file", "-e", oid + "^{object}"], cwd=repo,
                                          text=True, capture_output=True, check=False, timeout=20)
                except (OSError, subprocess.SubprocessError):
                    continue
                if done.returncode == 0:
                    return True
        return False

    def _has_action_sequence(self, sequence: list[str]) -> bool:
        cursor = 0
        for action in (item.get("action") for item in self.invocations):
            if cursor < len(sequence) and action == sequence[cursor]:
                cursor += 1
        return cursor == len(sequence)

    def _has_any_action_after(self, names: set[str], prior: str) -> bool:
        seen_prior = False
        for item in self.invocations:
            action = item.get("action")
            if action == prior:
                seen_prior = True
            elif seen_prior and action in names:
                return True
        return False

    def adapter_request(self, action: str) -> dict[str, Any] | None:
        if action in {"inspect", "attest"}:
            return None
        owner = self.owner()
        if action == "create":
            owner = {"id": "agent-owner", "token": None, "fence": None}
        crash = None
        if action == "run" and not self.crash_armed:
            if self.case_id in {"test_002", "test_003"}:
                # Arm before the adapter starts.  A SIGKILL can prevent the
                # later /record request, so the evaluator must still know the
                # injected crash was consumed and must not inject it again.
                self.crash_armed = True
                self._persist()
            if self.case_id == "test_002":
                crash = {"after": "federation_prepared", "occurrence": 1, "mode": "sigkill"}
            elif self.case_id == "test_003":
                crash = {"after": "participant_refs_committed", "occurrence": 1, "mode": "sigkill"}
            if crash is not None:
                # Exactly once per case (guarded by crash_armed above): the evaluator
                # has now delivered its own crash directive to the adapter.  Persist it
                # so the fact survives the SIGKILL it is about to cause.
                self.crash_injected = True
                self._persist()
        return {"schema_version": 3, "operation": action, "repo": "/case-work/repo", "state_dir": "/case-work/state", "plan_id": f"{self.case_id}-{self.nonce[:8]}", "plan": self.plan() if action == "create" else None, "max_workers": self.max_workers() if action == "run" else None, "crash": crash, "coordinator": None if action == "status" else owner, "lease_seconds": None if action == "status" else 2}

    def public_state(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "run_nonce": self.nonce,
            "available_actions": self.spec["allowed_actions"],
            "recorded_actions": [x["action"] for x in self.invocations],
            "repositories": [{"id": item["id"], "role": item["role"], "base_revision": self.base_oids[item["id"]]} for item in self.repo_specs],
            "fixture_facts": {
                "repository_count": len(self.repo_specs), "max_workers": self.max_workers(),
                "has_dirty_snapshot": self.case_id == "test_005",
                "foreign_writer_present": self.case_id == "test_004",
                "admission_guard_present": self.case_id in {"test_004", "test_006"},
                "case_task_digest": self.case_task_digest,
                "scenario_digest": self.scenario_digest,
                "crash_armed": self.crash_armed,
            },
        }

    def _private_predicates(self) -> dict[str, Any]:
        """Evaluator-only expectations, never returned by the action client."""
        mapping = {
            "test_001": {"terminal": "committed", "repository_count": 3, "primary_axis": "admission_and_bounded_dag_ownership", "required_action_sequence": ["inspect", "create", "run"], "required_action_any": ["status", "attest"]},
            "test_002": {"terminal": "committed_after_recovery", "repository_count": 2, "primary_axis": "quarantine_closure_and_exactly_once_cleanup", "required_action_sequence": ["inspect", "create", "run", "recover"], "required_action_any": ["status", "attest"]},
            "test_003": {"terminal": "committed_after_prefix_recovery", "repository_count": 3, "primary_axis": "durable_decision_prefix_recovery", "required_action_sequence": ["inspect", "create", "run", "recover"], "required_action_any": ["status", "attest"]},
            "test_004": {"terminal": "blocked_without_foreign_mutation", "repository_count": 2, "primary_axis": "component_conflict_ownership", "required_action_sequence": ["inspect", "create"], "required_action_any": ["status", "attest"]},
            "test_005": {"terminal": "rollback_or_bounded_obstruction", "repository_count": 2, "primary_axis": "reverse_decision_dirty_state", "required_action_sequence": ["inspect", "create"], "required_action_any": ["rollback", "status", "attest"]},
            "test_006": {"terminal": "integrity_report_without_unsafe_mutation", "repository_count": 2, "primary_axis": "integrity_status_compatibility", "required_action_sequence": ["inspect"], "required_action_any": ["status", "attest"]},
        }
        return mapping.get(self.case_id, {"terminal": "case_specific", "repository_count": len(self.repo_specs), "primary_axis": self.spec.get("scenario")})

    def _latest_response(self, action: str) -> dict[str, Any]:
        value = self.responses.get(action)
        return value if isinstance(value, dict) else {}

    def _has_bound_ledger(self, action: str) -> bool:
        response = self._latest_response(action)
        ledger = response.get("ledger") if isinstance(response.get("ledger"), dict) else {}
        observation = next((item for item in reversed(self.product_observations)
                            if item.get('action') == action), {})
        return (
            ledger.get("schema_version") == 3
            and isinstance(response.get('transaction_id'), str) and response.get('transaction_id') not in {'', 'invalid'}
            and response.get('plan_id') == f'{self.case_id}-{self.nonce[:8]}'
            and isinstance(ledger.get("generation"), int) and ledger.get("generation", 0) >= 1
            and isinstance(ledger.get("digest"), str) and ledger.get("digest", "").startswith("sha256:")
            and observation.get('durable_ledger_valid') is True
        )

    def _observe_product_state(self, action: str, response: dict[str, Any]) -> dict[str, Any]:
        """Read real post-action bytes and refs; never infer them from /record claims."""
        observation: dict[str, Any] = {'action': action, 'durable_ledger_valid': False,
                                      'publication_refs_match': False, 'repositories': {}}
        ledger_path = self.state_dir / 'ledger.json'
        try:
            if (self.state_dir.is_symlink() or ledger_path.is_symlink()
                    or ledger_path.stat().st_size > 8 * 1024 * 1024):
                raise ValueError('unsafe or oversized durable ledger')
            raw = ledger_path.read_bytes()
            value = json.loads(raw)
            observation.update(durable_ledger_valid=isinstance(value, dict) and value.get('schema_version') == 3,
                               ledger_sha256=hashlib.sha256(raw).hexdigest(), ledger_size=len(raw),
                               commit_decision_recorded=self._ledger_commit_decision_recorded(value))
        except (OSError, ValueError, TypeError):
            pass
        for identifier, repo in self.repo_paths.items():
            try:
                observation['repositories'][identifier] = {
                    'head_oid': self._git_at(repo, 'rev-parse', 'HEAD'),
                    'worktrees': self._git_at(repo, 'worktree', 'list', '--porcelain'),
                }
            except (OSError, RuntimeError) as exc:
                observation['repositories'][identifier] = {'observation_error': type(exc).__name__}
        try:
            observation['admissions'] = self._admission_inventory()
            observation['refs'] = self._ref_inventory()
            observation['worktrees_listed'] = self._worktree_inventory()
            observation['gitlinks'] = self._gitlink_bindings()
            observation['command_log'] = self._command_log()
            observation['object_store'] = self._object_store_state()
            observation['preservation_unchanged'] = self._preservation_unchanged(self.case_id)
            observation['bases_unchanged'] = self._repository_bases_unchanged()
            observation['candidate_oids_reported'] = self._response_candidate_oids(response)
            observation['candidate_objects_visible'] = self._candidate_objects_visible(response)
            observation['response_state'] = response.get('state')
            decision = response.get('decision') if isinstance(response.get('decision'), dict) else {}
            observation['decision_state'] = decision.get('state')
            cleanup = response.get('cleanup') if isinstance(response.get('cleanup'), dict) else {}
            observation['claimed_cleanup_state'] = cleanup.get('state')
            observation['claimed_admissions_released'] = cleanup.get('admissions_released')
        except Exception as exc:  # observation must never break the case service
            observation['independent_observation_error'] = type(exc).__name__ + ': ' + str(exc)
        publication = response.get('publication') if isinstance(response.get('publication'), dict) else {}
        targets = publication.get('targets')
        expected = self.plan()['publication']['targets']
        try:
            actual = {(row['repository_id'], row['ref']): row for row in targets}
            observation['publication_refs_match'] = bool(
                len(actual) == len(expected) and all(
                    (row['repository_id'], row['ref']) in actual
                    and actual[(row['repository_id'], row['ref'])].get('final_oid') == self._git_at(
                        self.repo_paths[row['repository_id']], 'rev-parse', '--verify', row['ref'])
                    and actual[(row['repository_id'], row['ref'])].get('final_oid') != row['expected_oid']
                    for row in expected))
        except (OSError, RuntimeError, KeyError, TypeError, ValueError):
            pass
        return observation

    def _repository_bases_unchanged(self) -> bool:
        try:
            return all(self._git_at(self.repo_paths[item["id"]], "rev-parse", "HEAD") == self.base_oids[item["id"]]
                       for item in self.repo_specs)
        except (OSError, RuntimeError):
            return False

    def _fixture_preservation(self, case_id: str) -> bool:
        component = self.repo_paths.get("component")
        if case_id == "test_003":
            return all(
                (self.repo_paths[item].joinpath("component-link").is_symlink()
                 and self.repo_paths[item].joinpath("mode-preserved.sh").stat().st_mode & 0o111)
                for item in ("shape-component", "nested-component")
            ) and self._preservation_unchanged(case_id)
        if case_id == "test_004":
            return bool(component and (component / "foreign-author.txt").read_text(encoding="utf-8") == "preserve external author\n"
                        and (component / ".git/aider/transactions/foreign-ref.json").is_file()
                        and self._preservation_unchanged(case_id))
        if case_id == "test_005":
            return bool(component and (component / "component.txt").read_text(encoding="utf-8").startswith("component-dirty:")
                        and (component / "dirty-untracked.txt").read_text(encoding="utf-8") == "foreign dirty bytes\n"
                        and (component / "component-link").is_symlink()
                        and (component / "mode-preserved.sh").stat().st_mode & 0o111
                        and self._preservation_unchanged(case_id))
        return True

    def _initial_inventory(self) -> dict[str, Any]:
        if getattr(self, "_initial_inventory_cache", None) is None:
            self._initial_inventory_cache = {
                "refs": self._ref_inventory(),
                "admissions": self._admission_inventory(),
                "worktrees_listed": self._worktree_inventory(),
            }
        return self._initial_inventory_cache

    def _adapter_invocations(self) -> list[int]:
        """Indexes of invocations that really reached the product adapter."""
        return [index for index, item in enumerate(self.invocations)
                if item.get("action") in {"create", "status", "run", "recover", "rollback"}]

    def _observation(self, index: int) -> dict[str, Any]:
        if 0 <= index < len(self.product_observations):
            value = self.product_observations[index]
            return value if isinstance(value, dict) else {}
        return {}

    def _durable_face(self, index: int) -> dict[str, Any]:
        """The durable facts a read-only or repeated request must not change."""
        observation = self._observation(index) if index >= 0 else self._initial_inventory()
        return {
            "refs": observation.get("refs", self._initial_inventory()["refs"]),
            "admissions": observation.get("admissions", self._initial_inventory()["admissions"]),
            "ledger_sha256": observation.get("ledger_sha256"),
            "gitlinks": observation.get("gitlinks"),
        }

    def _bounded_error_reported(self, response: dict[str, Any]) -> bool:
        reason = response.get("reason")
        errors = response.get("errors")
        return bool((isinstance(reason, str) and reason.strip())
                    or (isinstance(errors, list) and errors))

    def _last_adapter_action(self) -> str:
        indexes = self._adapter_invocations()
        return str(self.invocations[indexes[-1]].get("action")) if indexes else ""

    def _publication_verified(self) -> bool:
        """A publication the evaluator confirmed in real refs and gitlinks."""
        published = [index for index in range(len(self.product_observations))
                     if self._observation(index).get("publication_refs_match") is True]
        if not published:
            return False
        observation = self._observation(published[-1])
        entries = (observation.get("gitlinks") or {}).get("refs/heads/main") or {}
        for item in self.repo_specs:
            identifier = item["id"]
            if identifier == "root":
                continue
            try:
                child = self._git_at(self.repo_paths[identifier], "rev-parse", "--verify", "refs/heads/main")
            except (OSError, RuntimeError):
                return False
            if entries.get(identifier) != child:
                return False
        return True

    def _first_publication_index(self) -> int:
        for index in range(len(self.product_observations)):
            if self._observation(index).get("publication_refs_match") is True:
                return index
        return len(self.product_observations)

    def _fail_closed_evidenced(self) -> bool:
        """A refusal that the evaluator can prove changed nothing durable."""
        action = self._last_adapter_action()
        if not action:
            return False
        response = self._latest_response(action)
        if str(response.get("state")) not in {"blocked", "aborted", "refused"}:
            return False
        indexes = self._adapter_invocations()
        observation = self._observation(indexes[-1]) if indexes else {}
        # The refusal itself may be reported by strictly read-only `status`,
        # which must not write a ledger; binding therefore accepts the durable
        # ledger any earlier operation of this transaction really produced.
        bound = any(self._has_bound_ledger(str(self.invocations[index].get("action")))
                    for index in indexes)
        return bool(
            observation.get("bases_unchanged") is True
            and observation.get("preservation_unchanged") is not False
            and bound
            and self._bounded_error_reported(response)
        )

    def _read_only_actions_pure(self) -> bool:
        """`status` is strictly read-only: it must leave no durable trace."""
        for index, item in enumerate(self.invocations):
            if item.get("action") != "status":
                continue
            if self._durable_face(index) != self._durable_face(index - 1):
                return False
        return True

    def _repeat_actions_idempotent(self) -> bool:
        """An exact retry after a settled response repeats no durable effect."""
        settled: dict[str, int] = {}
        for index, item in enumerate(self.invocations):
            action = str(item.get("action"))
            if action in settled:
                before, after = self._observation(index - 1), self._observation(index)
                if before.get("refs") != after.get("refs"):
                    return False
                rows_before = (before.get("command_log") or {}).get("rows")
                rows_after = (after.get("command_log") or {}).get("rows")
                if isinstance(rows_before, int) and isinstance(rows_after, int) and rows_after != rows_before:
                    return False
            if str(item.get("state")) in {"committed", "blocked", "aborted", "rolled_back", "refused"}:
                settled[action] = index
        return True

    def _commands_executed_at_most_once(self) -> bool | None:
        """Real worker/test executions counted by the evaluator-owned command.

        ``None`` means the product never executed a declared command, so the
        evidence for this obligation does not exist and nothing is asserted.
        """
        log = (self._observation(len(self.product_observations) - 1).get("command_log") or {})
        if not log.get("present") or not log.get("rows"):
            return None
        if log.get("truncated_since_start"):
            return False
        counts = log.get("counts") if isinstance(log.get("counts"), dict) else {}
        for key, value in counts.items():
            if key.split("|")[2:] and key.split("|")[2] and value != 1:
                return False
        return int(log.get("rows") or 0) <= 3 * max(1, len(self.repo_specs))

    # D51 (2026-09-21).  A decision this case service really observed, never a
    # candidate claim on its own: either the durable ledger the evaluator read
    # itself carries the global commit decision, or the candidate reports a
    # decided transaction *and* the evaluator's own ref inventory already shows
    # a participant promoted to a reported candidate.
    POSTDECISION_DECISION_STATES = {"commit", "committed", "complete"}

    @staticmethod
    def _ledger_commit_decision_recorded(ledger: Any) -> bool:
        """True once the durable ledger carries the global commit decision.

        02_interface_and_delivery.md#commit-decision-and-cross-repository-publication step 3 *is* the global
        commit decision: it syncs an immutable decision object and the
        ``commit_decision_recorded`` ledger event.  Only step 4 then promotes
        and commits participant refs.  These are bytes at
        ``state_dir/ledger.json`` that the evaluator reads itself.
        """
        if not isinstance(ledger, dict):
            return False
        events = ledger.get("events")
        if not isinstance(events, list):
            return False
        return any(isinstance(item, dict) and item.get("kind") == "commit_decision_recorded"
                   for item in events)

    def _participant_ref_promoted(self, index: int) -> bool:
        """True when a participant's real ref already equals a reported candidate.

        Evaluator-observed, from the ref inventory this case service took for
        itself.  Public refs and ordinary object databases stay unchanged
        through ``repository_prepared`` and ``federation_prepared``
        (02_interface_and_delivery.md#commit-decision-and-cross-repository-publication steps 1-2), so a
        participant ref that already points at a reported candidate can only
        exist at or after step 4, i.e. after the global commit decision.
        """
        observation = self._observation(index)
        reported = observation.get("candidate_oids_reported")
        refs = observation.get("refs")
        if not isinstance(reported, dict) or not isinstance(refs, dict):
            return False
        for identifier, oids in reported.items():
            rows = refs.get(identifier)
            if not isinstance(rows, list) or not isinstance(oids, list):
                continue
            seen = {str(row).split(" ")[-1] for row in rows}
            if seen & {str(oid) for oid in oids if isinstance(oid, str)}:
                return True
        return False

    def _first_postdecision_index(self) -> int:
        """Index of the first observation taken after the global commit decision."""
        for index in range(len(self.product_observations)):
            observation = self._observation(index)
            if observation.get("commit_decision_recorded") is True:
                return index
            if (str(observation.get("decision_state") or "") in self.POSTDECISION_DECISION_STATES
                    and self._participant_ref_promoted(index)):
                return index
        return len(self.product_observations)

    def _predecision_quarantine_respected(self) -> bool:
        # D51 (2026-09-21).  The published obligation
        # (02_interface_and_delivery.md#quarantine-objects-and-global-prepare, "before the global commit decision") is
        # scoped to the global commit decision, so the scan stops at the first
        # observation taken after that decision.  The former proxy -- the first
        # observation carrying a complete publication response -- is kept as an
        # upper bound, so this window can only shrink, never grow: a real
        # pre-decision leak is still a violation.  Without this, an evaluator
        # crash injected after ``participant_refs_committed`` (:533, test_003)
        # destroys the publication response the proxy waits for and forces a
        # compliant candidate to trip c7.
        limit = min(self._first_postdecision_index(), self._first_publication_index())
        for index in range(min(limit, len(self.product_observations))):
            if self._observation(index).get("candidate_objects_visible") is True:
                return False
        return True

    def _foreign_state_preserved(self) -> bool:
        return all(self._observation(index).get("preservation_unchanged") is not False
                   for index in range(len(self.product_observations)))

    def _owned_resources_released(self) -> bool | None:
        """No transaction-owned admission receipt, branch or worktree survives.

        ``None`` until the transaction actually settles; an in-flight rollout is
        not evidence of a leak.
        """
        action = self._last_adapter_action()
        if not action or str(self._latest_response(action).get("state")) not in self.SETTLED_STATES:
            return None
        indexes = self._adapter_invocations()
        observation = self._observation(indexes[-1]) if indexes else {}
        initial = self._initial_inventory()
        admissions = observation.get("admissions")
        worktrees = observation.get("worktrees_listed")
        if not isinstance(admissions, dict) or not isinstance(worktrees, dict):
            return None
        if set(admissions) - set(initial["admissions"]):
            return False
        for identifier, listed in worktrees.items():
            if set(listed or []) - set(initial["worktrees_listed"].get(identifier, [])):
                return False
        return True

    SETTLED_STATES = {"committed", "blocked", "aborted", "rolled_back", "refused"}

    def _terminal_receipt_observed(self) -> bool:
        """Some settled response really carried a per-repository receipt.

        02_interface_and_delivery.md requires the response to report the
        per-repository admission receipt, candidate/prepare/quarantine state and
        target receipts.  A settled envelope with an empty ``repositories`` list
        is a hollow refusal, not a receipt, whatever its prose says.
        """
        wanted = {item["id"] for item in self.repo_specs}
        for action, response in self.responses.items():
            if not isinstance(response, dict):
                continue
            if str(response.get("state")) not in self.SETTLED_STATES:
                continue
            rows = response.get("repositories")
            if not isinstance(rows, list):
                continue
            if {str(row.get("id")) for row in rows if isinstance(row, dict)} == wanted:
                return True
        return False

    def _rollback_verified(self) -> bool:
        return bool("rollback" in [item.get("action") for item in self.invocations]
                    and self._has_bound_ledger("rollback")
                    and self._fixture_preservation(self.case_id))

    def semantic_comparison(self) -> dict[str, Any]:
        actions = [item.get("action") for item in self.invocations]
        responses = self.responses
        terminal_states = [str((value or {}).get("state")) for value in responses.values() if isinstance(value, dict)]
        committed = any(value.get('state') == 'committed' and any(
            item.get('action') == action and item.get('publication_refs_match') is True
            for item in self.product_observations)
            for action, value in responses.items() if isinstance(value, dict))
        blocked = any(state == "blocked" for state in terminal_states)
        publication_verified = self._publication_verified()
        fail_closed = self._fail_closed_evidenced()
        expected_terminal = str(self._private_predicates().get("terminal", ""))
        foreign_preserved = self._foreign_state_preserved()
        if expected_terminal.startswith("committed"):
            # The observed world in these cases supports publication; the
            # declared roll-forward rule makes a bare refusal the wrong answer.
            terminal_correct = publication_verified
        elif expected_terminal in {"blocked_without_foreign_mutation",
                                   "integrity_report_without_unsafe_mutation"}:
            terminal_correct = bool(fail_closed and foreign_preserved and not publication_verified)
        else:
            terminal_correct = bool((self._rollback_verified() or fail_closed) and foreign_preserved)
        checks: dict[str, bool] = {
            "dynamic_case_binding": bool(self.case_task_digest and self.scenario_digest and self.nonce),
            "observed_product_action": bool(actions),
            "no_duplicate_run_after_crash": actions.count("run") <= 1 or self.case_id not in {"test_002", "test_003"},
            "required_action_sequence": self._has_action_sequence(self._private_predicates().get("required_action_sequence", [])),
            "required_followup_observation": self._has_any_action_after(set(self._private_predicates().get("required_action_any", [])), self._private_predicates().get("required_action_sequence", ["inspect"])[-1]),
            "durable_transaction_observed": any(
                self._observation(index).get("durable_ledger_valid") is True
                for index in range(len(self.product_observations))),
            "publication_independently_verified": publication_verified,
            "fail_closed_evidenced": fail_closed,
            "safe_terminal_or_refusal": terminal_correct,
            "read_only_status_left_no_durable_change": self._read_only_actions_pure(),
            "repeat_requests_repeated_no_effect": self._repeat_actions_idempotent(),
            "predecision_quarantine_respected": self._predecision_quarantine_respected(),
            "foreign_state_preserved": foreign_preserved,
            "terminal_receipt_observed": self._terminal_receipt_observed(),
        }
        commands_once = self._commands_executed_at_most_once()
        if commands_once is not None:
            checks["commands_executed_at_most_once"] = commands_once
        released = self._owned_resources_released()
        if released is not None:
            checks["owned_resources_released"] = released
        object_store = self._object_store_valid()
        if object_store is not None:
            checks["content_addressed_object_store_valid"] = object_store
        if self.case_id == "test_001":
            checks.update({
                "topology_matches_private_case": len(self.repo_specs) == 3,
                "publication_and_cleanup_evidence": self._has_bound_ledger("attest") or self._has_bound_ledger("status"),
            })
        elif self.case_id == "test_002":
            checks.update({
                "crash_was_armed_once": self.crash_armed,
                "recovery_observed": "recover" in actions and self._has_bound_ledger("recover") and checks["required_action_sequence"],
                "cleanup_or_preservation_evidence": bool(
                    (self._latest_response("recover").get("cleanup") or {}).get("state")
                ),
            })
        elif self.case_id == "test_003":
            checks.update({
                "topology_matches_private_case": {item["id"] for item in self.repo_specs} == {"root", "shape-component", "nested-component"},
                "recovery_observed": "recover" in actions and self._has_bound_ledger("recover") and checks["required_action_sequence"],
                "foreign_modes_and_links_preserved": self._fixture_preservation("test_003"),
            })
        elif self.case_id == "test_004":
            checks.update({
                "foreign_writer_fixture_present": self._fixture_preservation("test_004"),
                "safe_refusal_or_disjoint_completion": terminal_correct and checks["required_action_sequence"],
            })
        elif self.case_id == "test_005":
            checks.update({
                "dirty_snapshot_fixture_present": self._fixture_preservation("test_005"),
                "rollback_affordance_observed": (self._rollback_verified() or (blocked and checks["required_followup_observation"])),
                "dirty_bytes_and_modes_preserved": self._fixture_preservation("test_005"),
            })
        elif self.case_id == "test_006":
            checks.update({
                "integrity_guard_fixture_present": (self.repo_paths.get("component", self.repo) / ".git/aider/transactions/global-decision.json").is_file(),
                "non_mutating_status_path": bool(not committed and checks["read_only_status_left_no_durable_change"]
                                                 and checks["required_followup_observation"]),
                "base_refs_unchanged": self._repository_bases_unchanged() and self._preservation_unchanged("test_006"),
            })
        return {
            "schema_version": "agentswe-aider-semantic-comparison-v2",
            "case_id": self.case_id,
            "primary_axis": self._private_predicates()["primary_axis"],
            "task_digest": self.case_task_digest,
            "runtime_facts": {"run_nonce": self.nonce, "repository_ids": [item["id"] for item in self.repo_specs], "base_oids": self.base_oids},
            "observed_actions": actions,
            "observed_terminal_states": terminal_states,
            "independent_product_observations": self.product_observations,
            "expected_terminal_class": (
                "publication" if expected_terminal.startswith("committed")
                else "fail_closed" if expected_terminal in {"blocked_without_foreign_mutation",
                                                            "integrity_report_without_unsafe_mutation"}
                else "reverse_or_bounded_obstruction"),
            "checks": checks,
            "oracle_visibility": "evaluator-only predicate summary; private expected terminal state is withheld",
        }

    def record(self, action: str, response: dict[str, Any], exit_code: int) -> dict[str, Any]:
        with self.lock:
            self.responses[action] = response
            self.product_observations.append(self._observe_product_state(action, response))
            self.invocations.append({"action": action, "exit_code": exit_code, "state": response.get("state"), "ledger_digest": (response.get("ledger") or {}).get("digest")})
            self._persist()
            return self.public_state()

    def _persist(self) -> None:
        atomic_json(self.state_path, {
            "schema_version": "agentswe-aider-case-state-v1",
            "case_id": self.case_id,
            "scenario": self.spec["scenario"],
            "run_nonce": self.nonce,
            "base_oid": self.base_oid,
            "base_oids": self.base_oids,
            "case_task_digest": self.case_task_digest,
            "scenario_digest": self.scenario_digest,
            "crash_armed": self.crash_armed,
            "crash_injected": self.crash_injected,
            "expected_action_family": self.spec["allowed_actions"],
            "visible": self.public_state(),
            "invocations": self.invocations,
            "responses": self.responses,
            "product_observations": self.product_observations,
            "semantic_comparison": self.semantic_comparison(),
            "oracle": {
                "stored_evaluator_side_only": True,
                "private_predicates": self._private_predicates(),
            },
        })

    def client_source(self, endpoint: str) -> str:
        # D49 (2026-09-21): `start_new_session=True` below.  The published contract
        # (input/02_interface_and_delivery.md:15, input/03_requirements_and_constraints.md:30)
        # tells the candidate that a `sigkill` injection terminates the PROCESS GROUP.
        # This client used to launch the adapter in the action client's own group, which
        # is the lower agent's group and the lower container's PID-1 group, so a literal
        # killpg(getpgrp()) killed the agent as well (exit 247 = SystemExit(-9) & 0xFF via
        # harness/transport_sandbox.py:216,238) and the oracle's post-crash `recover`
        # (case_runtime.py private predicates for test_002/test_003) became unreachable.
        # The adapter now leads its own session, so the contract-mandated process-group
        # kill bounds itself to the product process tree the contract is about.
        lines = [
            "#!/usr/bin/env python3", "import argparse,json,os,subprocess,tempfile,urllib.request", f"ENDPOINT=os.environ.get('AGENTSWE_CASE_SERVICE_URL', {endpoint!r})",
            "p=argparse.ArgumentParser(); p.add_argument('action'); a=p.parse_args()",
            "def post(path,obj):",
            "    req=urllib.request.Request(ENDPOINT+path,data=json.dumps(obj).encode(),headers={'Content-Type':'application/json'},method='POST')",
            "    return json.loads(urllib.request.urlopen(req,timeout=150).read())",
            "start=post('/action',{'action':a.action}); request=start.get('adapter_request')",
            "if request is None:",
            "    final=post('/record',{'action':a.action,'exit_code':0,'response':{'state':'observed','public_state':start.get('public_state')}}); print(json.dumps(final)); raise SystemExit(0)",
            "with tempfile.TemporaryDirectory(dir='/tmp') as td:",
            "    rp=os.path.join(td,'request.json'); sp=os.path.join(td,'response.json'); open(rp,'w').write(json.dumps(request))",
            "    done=subprocess.run([os.environ.get('AIDER_LOWER_PYTHON','python3'),'-m','aider.worktree_plan_adapter','--request',rp,'--response',sp],cwd='/case-work/repo',text=True,capture_output=True,start_new_session=True)",
            "    response=json.load(open(sp)) if os.path.isfile(sp) else {}",
            "    final=post('/record',{'action':a.action,'exit_code':done.returncode,'response':response}); final['adapter_stdout_tail']=done.stdout[-1000:]; final['adapter_stderr_tail']=done.stderr[-1000:]; print(json.dumps(final)); raise SystemExit(0)",
        ]
        return "\n".join(lines) + "\n"


class RuntimeServer:
    def __init__(self, runtime: CaseRuntime) -> None:
        self.runtime, self.server, self.port = runtime, None, 0

    def start(self) -> None:
        runtime = self.runtime
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None: return
            def reply(self, status: int, value: object) -> None:
                body=json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
            def body(self) -> dict[str, Any]: return json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
            def do_POST(self) -> None:  # noqa: N802
                try:
                    value=self.body(); action=str(value.get("action",""))
                    if self.path=="/action":
                        if action not in runtime.spec["allowed_actions"]: self.reply(409,{"error":"action_not_allowed"}); return
                        self.reply(200,{"public_state":runtime.public_state(),"adapter_request":runtime.adapter_request(action)}); return
                    if self.path=="/record":
                        response=value.get("response") if isinstance(value.get("response"),dict) else {}
                        self.reply(200,{"public_state":runtime.record(action,response,int(value.get("exit_code",1))),"adapter_response":response}); return
                    self.reply(404,{"error":"not_found"})
                except Exception as exc: self.reply(500,{"error":f"{type(exc).__name__}: {exc}"})
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler); self.port=int(self.server.server_address[1]); threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def stop(self) -> None:
        if self.server: self.server.shutdown(); self.server.server_close()
