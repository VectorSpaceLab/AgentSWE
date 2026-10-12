"""Harbor patch 0007 (site profile): a task compose that sets cpu_quota/cpu_period for `main` gets no resources-override
`cpus`; every other compose still does (stdlib unittest).

The static half checks the patch set's own pins: the shipped site and pristine docker.py match manifest.json, the
expected tree lists, the series patch and the site tree digest. The behavioural half needs a harbor 0.20.0
interpreter (AGENTSWE_TEST_HARBOR_PYTHON, or $AGENTSWE_HOME/harbor/venv-site/bin/python): it overlays the pristine
and the site docker.py on a copy of that install's harbor package and runs tests/check_harbor_behavior.py, which only
writes Harbor's resources override; no Docker command runs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARBOR = ROOT / "third_party" / "harbor"
PATCHES = HARBOR / "patches"
REL = "harbor/environments/docker/docker.py"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected(profile: str) -> dict[str, str]:
    rows = {}
    for line in (PATCHES / "expected" / f"{profile}.sha256").read_text().splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            rows[rel] = digest
    return rows


class PatchSetPins(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((PATCHES / "manifest.json").read_text())
        self.site_file = PATCHES / "files" / "site" / REL
        self.pristine_file = PATCHES / "files" / "pristine" / REL

    def test_manifest_entries_match_the_shipped_files(self):
        site = self.manifest["profiles"]["site"]["files"][REL]
        pristine = self.manifest["profiles"]["pristine"]["files"][REL]
        self.assertEqual(site["sha256"], sha(self.site_file))
        self.assertEqual(site["upstream_sha256"], sha(self.pristine_file))
        self.assertEqual(site["patches"], ["0007"])
        self.assertEqual(pristine["sha256"], sha(self.pristine_file))
        patch = next(p for p in self.manifest["patches"] if p["id"] == "0007")
        self.assertEqual((patch["path"], patch["profiles"]), (REL, ["site"]))

    def test_expected_trees(self):
        self.assertEqual(expected("pristine")[REL], sha(self.pristine_file))
        self.assertEqual(expected("site")[REL], sha(self.site_file))
        for profile in ("creation", "creation-0902", "creation-glm-0903"):
            self.assertEqual(expected(profile)[REL], sha(self.pristine_file), profile)
        rows = expected("site")
        tree = hashlib.sha256("".join(f"{d}  {r}\n" for r, d in sorted(rows.items())).encode()).hexdigest()
        self.assertEqual(self.manifest["profiles"]["site"]["tree_sha256"], tree)
        self.assertEqual(set(rows), set(expected("pristine")))
        changed = sorted(r for r in rows if rows[r] != expected("pristine")[r])
        self.assertEqual(changed, sorted(self.manifest["profiles"]["site"]["files"]))

    def test_series_patch_turns_pristine_into_site(self):
        patch = next(p for p in self.manifest["patches"] if p["id"] == "0007")
        series = (PATCHES / patch["file"]).read_text().splitlines(keepends=True)
        body = series[series.index(f"--- a/{REL}\n") + 2:]
        lines = self.pristine_file.read_text().splitlines(keepends=True)
        offset, added, removed, i = 0, 0, 0, 0
        while i < len(body):
            header = body[i]
            self.assertTrue(header.startswith("@@ -"), header)
            start = int(header.split()[1][1:].split(",")[0]) - 1 + offset
            old, new = [], []
            i += 1
            while i < len(body) and not body[i].startswith("@@ "):
                tag, text = body[i][0], body[i][1:]
                if tag in " -":
                    old.append(text)
                if tag in " +":
                    new.append(text)
                added += tag == "+"
                removed += tag == "-"
                i += 1
            self.assertEqual(lines[start:start + len(old)], old, header)
            lines[start:start + len(old)] = new
            offset += len(new) - len(old)
        self.assertEqual("".join(lines), self.site_file.read_text())
        self.assertEqual((patch["added_lines"], patch["removed_lines"]), (added, removed))


def harbor_python() -> Path | None:
    candidates = [os.environ.get("AGENTSWE_TEST_HARBOR_PYTHON")]
    if os.environ.get("AGENTSWE_HOME"):
        candidates.append(str(Path(os.environ["AGENTSWE_HOME"]) / "harbor" / "venv-site" / "bin" / "python"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


@unittest.skipIf(harbor_python() is None, "no harbor 0.20.0 interpreter (AGENTSWE_TEST_HARBOR_PYTHON)")
class DockerCpusBehaviour(unittest.TestCase):
    def observe(self, profile: str) -> str:
        python = harbor_python()
        installed = Path(subprocess.run([str(python), "-B", "-c", "import harbor, os; print(os.path.dirname(harbor.__file__))"],
                                        capture_output=True, text=True, check=True).stdout.strip())
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "harbor"
            shutil.copytree(installed, overlay, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(PATCHES / "files" / profile / REL, Path(tmp) / REL)
            result = subprocess.run([str(python), "-B", str(HARBOR / "tests" / "check_harbor_behavior.py"), "--observe-only"],
                                    capture_output=True, text=True, env={**os.environ, "PYTHONPATH": tmp}, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            observed = json.loads(result.stdout)
            self.assertTrue(observed["harbor_file"].startswith(tmp), observed["harbor_file"])
            return observed["docker_cpus"]

    def test_site_yields_cpus_only_to_a_task_quota(self):
        self.assertEqual(self.observe("site"), "plain:cpus;quota:no-cpus")

    def test_pristine_writes_cpus_for_both(self):
        self.assertEqual(self.observe("pristine"), "plain:cpus;quota:cpus")


if __name__ == "__main__":
    unittest.main()
