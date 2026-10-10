"""The Edit re-finalization tools ship with the layout module they import (stdlib unittest).

runners/editing/tools/refinalize_run.py and rejudge_case.py import their shared layout facts from edit_run_layouts.py,
which the release did not ship: both died with ModuleNotFoundError before doing anything. The module is the paper
tool with its two host paths replaced by render tokens."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "runners" / "editing" / "tools"


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "edit_run_layouts" for alias in node.names}


class RefinalizeTools(unittest.TestCase):
    def test_every_imported_name_is_defined(self):
        tree = ast.parse((TOOLS / "edit_run_layouts.py").read_text())
        defined = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        defined |= {target.id for node in tree.body if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        for tool in ("refinalize_run.py", "rejudge_case.py"):
            names = imported_names(TOOLS / tool)
            self.assertTrue(names, tool)
            self.assertEqual(names - defined, set(), tool)

    def test_paths_are_render_tokens(self):
        source = (TOOLS / "edit_run_layouts.py").read_text()
        self.assertIn('SHARED_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")', source)
        self.assertIn('LAUNCH_CONTROL = Path("@@AGENTSWE_EDITING_RUNS@@/formal/launch_control")', source)

    def test_rendered_tools_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools"
            tools.mkdir()
            for name in ("edit_run_layouts.py", "refinalize_run.py", "rejudge_case.py"):
                text = (TOOLS / name).read_text()
                text = re.sub(r"@@AGENTSWE_[A-Z0-9_]+@@", tmp, text)
                (tools / name).write_text(text)
            for name in ("refinalize_run", "rejudge_case"):
                probe = subprocess.run([sys.executable, "-B", "-c", f"import sys; sys.argv=['x', '--help']; "
                                        f"sys.path.insert(0, {str(tools)!r}); import {name}"],
                                       capture_output=True, text=True, timeout=60)
                self.assertNotIn("ModuleNotFoundError", probe.stderr, name)
                self.assertEqual(probe.returncode, 0, probe.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
