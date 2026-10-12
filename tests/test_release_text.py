"""Public release text: metadata and docs carry no host or archive details, provenance keeps file digests only,
Lite references match the README, and documented behaviour matches the code (stdlib unittest, no network)."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASKS = sorted(ROOT.glob("tasks/*/*/task.json"))


def public_text_files() -> list[Path]:
    globs = ["README.md", "README_zh.md", "docs/*.md", "profiles/README.md", "third_party/harbor/README.md",
             "third_party/harbor/patches/manifest.json", "images/images.json", "images/*/Dockerfile",
             "tasks/*/*/task.json", "tasks/*/*/a0/a0.json", "tasks/*/*/env/env.json", "provenance/**/*.json"]
    return sorted({p for g in globs for p in ROOT.glob(g)})


class NoInternalDetails(unittest.TestCase):
    PATTERNS = [
        re.compile(r"\bhosts? \d{2,3}\b"),              # a host named by its number
        re.compile(r"\bon \d{2,3} and \d{2,3}\b"),
        re.compile(r"\brebuilt on \d"),
        re.compile(r"\d{4}-\d{2}-\d{2} \(\d{2,3}\)"),    # a date tagged with a host number
        re.compile(r"\bTrack [A-Z]{1,2}\b"),             # internal work-track names
        re.compile(r"\u00a7"),                           # internal document sections
        re.compile(r"anonymous repo"),
        re.compile(r", [0-9a-f]{7}\)"),                  # a short commit hash in a note
        re.compile(r"\brerun|\bretries started\b"),      # run history
    ]

    def test_public_text_files(self):
        for path in public_text_files():
            text = path.read_text(encoding="utf-8")
            for pattern in self.PATTERNS:
                match = pattern.search(text)
                self.assertIsNone(match, f"{path.relative_to(ROOT)}: {match and match.group(0)!r}")

    def test_a0_records_name_only_public_files(self):
        for path in sorted(ROOT.glob("tasks/editing/*/a0/a0.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertLessEqual(set(data["files"]), {"fetch", "en_manifest", "subset"}, path)
            self.assertNotIn("private", path.read_text(encoding="utf-8").lower(), path)


class ProvenanceDigestsOnly(unittest.TestCase):
    def test_paper_exact_files_hold_digests_only(self):
        files = sorted((ROOT / "provenance").rglob("*.json"))
        self.assertTrue(files)
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"schema", "source", "note", "shared", "tasks"}, path)
            digests = list(data["shared"].values())
            for task, entry in data["tasks"].items():
                self.assertEqual(set(entry), {"files", "digests"}, task)
                self.assertEqual(entry["files"], len(entry["digests"]), task)
                digests += entry["digests"].values()
            self.assertTrue(all(HEX64.match(d) for d in digests), path)


class LiteReferences(unittest.TestCase):
    def readme_lite(self, name: str) -> dict[str, float]:
        """short name -> DeepSeek-V4.1-Flash column of the README's Lite table."""
        values = {}
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            short = re.search(r"[(\uff08]`([a-z0-9]+)`[)\uff09]", line)
            if len(cells) == 5 and short:
                values[short.group(1)] = float(cells[3])
        return values

    def test_lite_reference_matches_readme(self):
        for readme in ("README.md", "README_zh.md"):
            table = self.readme_lite(readme)
            self.assertEqual(len(table), 8, readme)
            for path in TASKS:
                data = json.loads(path.read_text(encoding="utf-8"))
                ref = data.get("lite_reference")
                if not data.get("lite"):
                    self.assertIsNone(ref, path)
                    continue
                self.assertIsNotNone(ref, path)
                self.assertLessEqual(abs(ref["mean_at_3"] - table[data["short"]]), 0.051, path)


class DocumentedBehaviour(unittest.TestCase):
    def test_creation_freeze_instruction_is_noted(self):
        for path in sorted(ROOT.glob("tasks/creation/*/adapter/one_stop.py")):
            if "strictly greater than 60, or when" in path.read_text(encoding="utf-8"):
                notes = json.loads((path.parents[1] / "task.json").read_text(encoding="utf-8"))["notes"]
                self.assertTrue(any(n.startswith("Builder-visible text vs. evaluation") for n in notes), path)

    def test_editing_release_docs_come_from_the_tree(self):
        for path in sorted(ROOT.glob("tasks/editing/*/task.json")):
            language = json.loads(path.read_text(encoding="utf-8"))["language"]
            self.assertTrue(language["release_task_docs"].startswith("tree/"), path)

    def test_env_md_names_the_relay_host_and_creation_settings(self):
        relay = (ROOT / "runners/editing/control/direct_harbor_builder.py").read_text(encoding="utf-8")
        hosts = re.search(r"provider\.hostname not in \(([^)]*)\)", relay).group(1)
        self.assertEqual(re.findall(r"'([^']+)'", hosts), ["api.deepseek.com"])
        env = (ROOT / "docs/ENV.md").read_text(encoding="utf-8")
        self.assertIn("accepts only a CONNECT to `api.deepseek.com:443`", env)
        for name in ("AGENTSWE_JUDGE_FORMAT", "AGENTSWE_EVAL_RESUME_ENABLED", "AGENTSWE_EVAL_RESUME_MAX_ATTEMPTS"):
            self.assertIn(name, env)


if __name__ == "__main__":
    unittest.main()
