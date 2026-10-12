"""Hard-coded source-file digests in the Editing trees must match the files the release ships.

Several evaluators pin the sha256 of a source file next to its path (``X = Path(...)`` and ``X_SHA256 = "<hex>"``)
and refuse to run when the file changes. The release edits some files (scrubs, path tokens), so a pin copied from
the paper silently never matches again: DeepCode's code_identity_scope.py pinned control/code_eval.py, the
provider-name scrub changed two lines of that file, and every DeepCode readiness run failed after freeze and hidden.

For every such pair this test resolves the pinned path in the repository template, requires that the file carries
no ``@@AGENTSWE_*@@`` render token (otherwise its rendered digest depends on the install path and no constant can
match), and requires its sha256 to equal the pin.
"""
from __future__ import annotations

import ast
import hashlib
import re
import unittest
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TREES = REPO / "tasks" / "editing"
TOKEN_ROOTS = {
    "@@AGENTSWE_EDITING_CONTROL@@": REPO / "runners" / "editing" / "control",
    "@@AGENTSWE_EDITING_TOOLS@@": REPO / "runners" / "editing" / "tools",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RENDER_TOKEN = re.compile(rb"@@AGENTSWE_[A-Z0-9_]+@@")


def _path_expr(node: ast.AST, tree_root: Path, module: Path) -> Path | None:
    """Evaluate the small path expressions the trees use: Path("..."), ROOT / "a/b", Path(__file__).resolve().parents[n]."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            text = arg.value
            for token, root in TOKEN_ROOTS.items():
                if text.startswith(token):
                    return root / text[len(token):].lstrip("/")
            if "@@" in text:
                return None
            return Path(text)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_expr(node.left, tree_root, module)
        if left is not None and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return left / node.right.value
    if isinstance(node, ast.Name) and node.id == "ROOT":
        return tree_root
    return None


def pinned_pairs():
    for tree in sorted(p for p in TREES.glob("*/tree") if p.is_dir()):
        for module in sorted(tree.rglob("*.py")):
            if "input" in module.relative_to(tree).parts[:1]:
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # invalid escapes in some modules' string literals
                    parsed = ast.parse(module.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            paths, pins = {}, {}
            for stmt in parsed.body:
                if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                    continue
                name, value = stmt.targets[0].id, stmt.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and HEX64.match(value.value):
                    pins[name] = value.value
                else:
                    resolved = _path_expr(value, tree, module)
                    if resolved is not None:
                        paths[name] = resolved
            for name, digest in pins.items():
                base = name[:-len("_SHA256")] if name.endswith("_SHA256") else None
                if base and base in paths:
                    yield module.relative_to(REPO).as_posix(), name, paths[base], digest


class EditingSourcePins(unittest.TestCase):
    def test_pairs_found(self):
        self.assertTrue(list(pinned_pairs()), "no X / X_SHA256 pin pairs found; the scanner is broken")

    def test_pinned_sources_match_shipped_bytes(self):
        problems = []
        for module, name, path, digest in pinned_pairs():
            if not path.is_file():
                problems.append(f"{module}: {name} names {path} which the release does not ship")
                continue
            data = path.read_bytes()
            if RENDER_TOKEN.search(data):
                problems.append(f"{module}: {name} pins {path.relative_to(REPO)}, which carries render tokens")
            elif hashlib.sha256(data).hexdigest() != digest:
                problems.append(f"{module}: {name} = {digest[:12]} but {path.relative_to(REPO)} is "
                                f"{hashlib.sha256(data).hexdigest()[:12]} (re-pin after review, keep the old value)")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
