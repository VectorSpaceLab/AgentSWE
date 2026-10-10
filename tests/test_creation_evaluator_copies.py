"""Creation adapters stage an eval or candidate task by copying an adapter template (`*-template`) and then copying
evaluator-owned files over it (`shutil.copy2(benchmark / "evaluator" / ..., task_dir / ...)`). The copied file is
the one that runs; a template file at the same destination is never used. When the two copies drift, a change made
to the template's callers ships against the other copy: the PDF eval's run_eval.py and verify_score.py called a
3-argument `validate_quality_review` while the evaluator copied the 2-argument benchmark file over it, and every
quality review crashed with a TypeError.

    python3 -m unittest tests/test_creation_evaluator_copies.py   (standard library; no bytecode in task trees)

1. Every copy site in a Creation adapter is found by reading adapter.py (loops over literal names and `glob` are
   expanded); a copy that cannot be resolved fails the test instead of being skipped.
2. A template file at a copy destination must be byte-identical to the file copied over it.
3. Every function a template script takes from an evaluator module (a sibling file loaded with importlib through
   `Path(__file__).with_name(NAME)`, a file under the mounted /evaluator, or `from NAME import f`) must exist, with a
   signature that accepts each call, in the file that is actually staged there.
"""
from __future__ import annotations

import ast
import copy
import inspect
import types
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CREATION = ROOT / "tasks" / "creation"
TASKS = sorted(p for p in CREATION.iterdir() if (p / "adapter" / "adapter.py").is_file())
ROOTS = ("benchmark", "task_dir", "ADAPTER_DIR")
COPIES = ("copy", "copy2", "copyfile", "copytree")


def resolve(node, env):
    """Path expression -> list of (root, parts); root None for a relative path. None when not resolvable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [(None, PurePosixPath(node.value).parts)]
    if isinstance(node, ast.Name):
        return [(node.id, ())] if node.id in ROOTS else env.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = resolve(node.left, env), resolve(node.right, env)
        if not left or not right or any(root is not None for root, _ in right):
            return None
        return [(root, parts + more) for root, parts in left for _, more in right]
    if isinstance(node, ast.Attribute) and node.attr == "name":
        base = resolve(node.value, env)
        return [(None, parts[-1:]) for _, parts in base] if base else None
    return None


def concrete(task: Path, root: str, parts: tuple) -> Path:
    base = {"benchmark": task / "benchmark", "ADAPTER_DIR": task / "adapter"}[root]
    return base.joinpath(*parts)


def loop_values(task: Path, node, env):
    if isinstance(node, (ast.Tuple, ast.List)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [(None, PurePosixPath(e.value).parts) for e in node.elts]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "glob"
            and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)):
        base = resolve(node.func.value, env)
        if base and len(base) == 1 and base[0][0] in ("benchmark", "ADAPTER_DIR"):
            root, parts = base[0]
            found = sorted(concrete(task, root, parts).glob(node.args[0].value))
            return [(root, parts + p.relative_to(concrete(task, root, parts)).parts) for p in found]
    return None


def owned(node, state) -> bool:
    """The expression is built from an evaluator-owned root (benchmark, ADAPTER_DIR) or a name derived from one."""
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    return bool(names & {"benchmark", "ADAPTER_DIR"} or names & state["tainted"])


def copy_calls(stmt):
    for node in ast.walk(stmt):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in COPIES
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "shutil" and len(node.args) >= 2):
            yield node


def scan(task: Path, stmts, env, state, sites, unresolved):
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.For):
            values = loop_values(task, stmt.iter, env) if isinstance(stmt.target, ast.Name) else None
            if values is None and owned(stmt.iter, state):
                state["tainted"] |= {n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name)}
            for value in values if values is not None else [None]:
                inner = dict(env)
                if value is None:
                    if isinstance(stmt.target, ast.Name):
                        inner.pop(stmt.target.id, None)
                else:
                    inner[stmt.target.id] = [value]
                scan(task, stmt.body, inner, state, sites, unresolved)
            scan(task, stmt.orelse, env, state, sites, unresolved)
            continue
        if isinstance(stmt, (ast.If, ast.While, ast.With, ast.Try)):
            for block in ("body", "orelse", "finalbody"):
                scan(task, getattr(stmt, block, []), env, state, sites, unresolved)
            for handler in getattr(stmt, "handlers", []):
                scan(task, handler.body, env, state, sites, unresolved)
            continue
        for call in copy_calls(stmt):
            src, dst = resolve(call.args[0], env), resolve(call.args[1], env)
            if src is None or dst is None or len(src) != len(dst):
                if owned(call.args[0], state):  # an evaluator-owned source this reader cannot follow
                    unresolved.append(f"{task.name}: adapter.py:{call.lineno}")
                continue
            for (s_root, s_parts), (d_root, d_parts) in zip(src, dst):
                if d_root != "task_dir" or s_root not in ("benchmark", "ADAPTER_DIR"):
                    continue
                if s_root == "ADAPTER_DIR" and len(s_parts) == 1 and s_parts[0].endswith("-template") and not d_parts:
                    state["template"] = s_parts[0]
                    continue
                if state.get("template") is None:
                    unresolved.append(f"{task.name}: adapter.py:{call.lineno} copies before staging a template")
                    continue
                sites.append({"task": task, "line": call.lineno, "template": state["template"],
                              "source": concrete(task, s_root, s_parts), "destination": PurePosixPath(*d_parts)})
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            value = resolve(stmt.value, env)
            if value is None:
                env.pop(stmt.targets[0].id, None)
            else:
                env[stmt.targets[0].id] = value
            if owned(stmt.value, state):
                state["tainted"].add(stmt.targets[0].id)


def copy_sites():
    sites, unresolved = [], []
    for task in TASKS:
        tree = ast.parse((task / "adapter" / "adapter.py").read_text(encoding="utf-8"))
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef):
                scan(task, fn.body, {}, {"tainted": set()}, sites, unresolved)
    return sites, unresolved


SITES, UNRESOLVED = copy_sites()


def staged_path(task: Path, template: str, rel: PurePosixPath) -> Path:
    """The file a staged task holds at <template>/<rel>: the last copy over it, else the template's own file."""
    winner = task / "adapter" / template / Path(*rel.parts)
    for site in SITES:
        if site["task"] == task and site["template"] == template:
            if site["destination"] == rel:
                winner = site["source"]
            elif rel.is_relative_to(site["destination"]) and site["source"].is_dir():
                winner = site["source"].joinpath(*rel.relative_to(site["destination"]).parts)
    return winner


def mounts_evaluator(task: Path) -> bool:
    """The adapter bind-mounts benchmark/evaluator at /evaluator in the staged task."""
    return any('benchmark / "evaluator"' in line and '"target": "/evaluator"' in line
               for line in (task / "adapter" / "adapter.py").read_text(encoding="utf-8").splitlines())


def module_calls(script: Path):
    """(module, function, positional args or None, keyword names, has *args/**kwargs, line) for each use of an
    evaluator module by a template script. module is a sibling file name loaded with spec_from_file_location(...,
    Path(__file__).with_name(NAME)), "/evaluator/NAME" loaded from the mounted evaluator, or "import:NAME.py" for
    `from NAME import f` (positional args None: the import itself needs the name)."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def sibling(node, paths):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "with_name"
                and node.args and isinstance(node.args[0], ast.Constant)):
            return node.args[0].value
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path"
                and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).startswith("/evaluator/")):
            return node.args[0].value
        return paths.get(node.id) if isinstance(node, ast.Name) else None

    def bindings(scope, loaders):
        paths, specs, modules = {}, {}, {}
        body = [n for n in ast.walk(scope) if isinstance(n, ast.Assign)] if scope is not tree else \
            [n for n in tree.body if isinstance(n, ast.Assign)]
        for node in sorted(body, key=lambda n: (n.lineno, n.col_offset)):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            name, value = node.targets[0].id, node.value
            if sibling(value, paths):
                paths[name] = sibling(value, paths)
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr == "spec_from_file_location" and len(value.args) >= 2:
                    specs[name] = sibling(value.args[1], paths)
                elif value.func.attr == "module_from_spec" and value.args and isinstance(value.args[0], ast.Name):
                    if specs.get(value.args[0].id):
                        modules[name] = specs[value.args[0].id]
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in loaders:
                modules[name] = loaders[value.func.id]
        return modules

    def shape(node):
        star = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
        return len(node.args), tuple(k.arg for k in node.keywords if k.arg), star, node.lineno

    loaders = {}
    for scope in scopes[1:]:
        modules = bindings(scope, {})
        for node in ast.walk(scope):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id in modules:
                loaders[scope.name] = modules[node.value.id]
    calls, imported = [], {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                imported[alias.asname or alias.name] = ("import:" + node.module + ".py", alias.name)
                calls.append(("import:" + node.module + ".py", alias.name, None, (), False, node.lineno))
    for scope in scopes:
        modules = bindings(scope, loaders)
        nodes = ast.walk(scope) if scope is not tree else (n for s in tree.body if not isinstance(
            s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in ast.walk(s))
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in modules):
                calls.append((modules[node.func.value.id], node.func.attr, *shape(node)))
            elif isinstance(node.func, ast.Name) and node.func.id in imported:
                calls.append((*imported[node.func.id], *shape(node)))
    return calls


def staged_module(task: Path, template: str, rel_dir: PurePosixPath, module: str):
    """The staged file behind a module_calls() module, or None when it is not an evaluator module."""
    if module.startswith("/evaluator/"):
        return task / "benchmark" / "evaluator" / module[len("/evaluator/"):] if mounts_evaluator(task) else None
    if module.startswith("import:"):
        name = module[len("import:"):]
        here = staged_path(task, template, rel_dir / name)
        if here.is_file():
            return here
        copied = {s["source"] for s in SITES if s["task"] == task and s["template"] == template
                  and s["destination"].name == name}  # e.g. tests/validators/ put on sys.path
        return copied.pop() if len(copied) == 1 else None
    return staged_path(task, template, rel_dir / module)


def module_level(body):
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            for block in ("body", "orelse", "finalbody"):
                yield from module_level(getattr(node, block, []))
            for handler in getattr(node, "handlers", []):
                yield from module_level(handler.body)


def stub_signature(module: Path, name: str):
    """Signature of a module-level function, parsed (defaults replaced by None); None when the name is bound some
    other way (import, assignment, class), False when the module does not define it."""
    other = False
    for node in module_level(ast.parse(module.read_text(encoding="utf-8")).body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            args = copy.deepcopy(node.args)
            args.defaults = [ast.Constant(None) for _ in args.defaults]
            args.kw_defaults = [None if d is None else ast.Constant(None) for d in args.kw_defaults]
            for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if a is not None:
                    a.annotation = None
            scope: dict = {}
            exec(f"def _f({ast.unparse(args)}): pass", scope)  # a parsed signature; no task code runs
            return inspect.signature(scope["_f"])
        names = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.asname or a.name.split(".")[0] for a in node.names]
        if name in names or (isinstance(node, ast.ClassDef) and node.name == name):
            other = None
    return other


class CreationEvaluatorCopies(unittest.TestCase):
    def test_every_copy_site_is_resolved(self):
        self.assertEqual(UNRESOLVED, [])
        found = {(s["task"].name, s["template"], str(s["destination"])) for s in SITES}
        for expected in (("scientific-pdf-translation", "eval-template", "solution/evidence_bundle.py"),
                         ("scientific-pdf-translation", "eval-template", "tests/evidence_bundle.py"),
                         ("document-to-editable-pptx", "eval-template", "solution/evidence_bundle.py"),
                         ("web-research-report", "task-template", "tests/evaluate_case.py"),
                         ("database-analytics", "task-template", "tests/validators/trusted_evidence.py")):
            self.assertIn(expected, found)

    def test_template_files_equal_the_files_copied_over_them(self):
        for site in SITES:
            source = site["source"]
            if not source.exists():  # adapters copy optional evaluator files only when present
                continue
            target = site["task"] / "adapter" / site["template"] / Path(*site["destination"].parts)
            pairs = [(source, target)] if source.is_file() else [
                (f, target / f.relative_to(source)) for f in sorted(source.rglob("*")) if f.is_file()]
            for src, dst in pairs:
                if dst.is_file():
                    with self.subTest(task=site["task"].name, line=site["line"], file=str(dst.relative_to(ROOT))):
                        self.assertEqual(dst.read_bytes(), src.read_bytes(),
                                         f"{dst.relative_to(ROOT)} differs from {src.relative_to(ROOT)}, which "
                                         f"adapter.py:{site['line']} copies over it")

    def test_template_calls_bind_in_the_staged_modules(self):
        checked = set()
        for task in TASKS:
            for template in sorted(p for p in (task / "adapter").iterdir() if p.name.endswith("-template")):
                for script in sorted(template.rglob("*.py")):
                    rel_dir = PurePosixPath(script.parent.relative_to(template).as_posix())
                    for name, function, n_args, keywords, star, line in module_calls(script):
                        module = staged_module(task, template.name, rel_dir, name)
                        if module is None:
                            continue
                        where = f"{script.relative_to(ROOT)}:{line} {name} {function}"
                        with self.subTest(call=where, staged=str(module.relative_to(ROOT))):
                            self.assertTrue(module.is_file(), f"{where}: nothing is staged at {name}")
                            signature = stub_signature(module, function)
                            self.assertIsNot(signature, False, f"{where}: {module.relative_to(ROOT)} has no {function}")
                            if signature is not None and n_args is not None and not star:
                                try:
                                    signature.bind(*range(n_args), **{k: None for k in keywords})
                                except TypeError as exc:
                                    self.fail(f"{where}: {module.relative_to(ROOT)} {function}{signature}: {exc}")
                            checked.add((task.name, script.name, function))
        self.assertGreater(len(checked), 20)

    def test_staged_pdf_module_scores_a_quality_review_in_both_formats(self):
        module_path = staged_path(CREATION / "scientific-pdf-translation", "eval-template",
                                  PurePosixPath("solution/evidence_bundle.py"))
        self.assertEqual(module_path, CREATION / "scientific-pdf-translation/benchmark/evaluator/evidence_bundle.py")
        reader = types.ModuleType("staged_pdf_evidence")  # executed from source: no __pycache__ in the benchmark
        reader.__file__ = str(module_path)
        exec(compile(module_path.read_text(encoding="utf-8"), str(module_path), "exec"), reader.__dict__)
        units = [f"u{i}" for i in range(10)]
        bundle = {"source_pages": [{"page": 1, "units": [{"id": u} for u in units]}],
                  "target_pages": [{"page": 1}], "alignment": {"functional": True}, "viewer": {"functional": True}}
        review = {"unit_coverage": [{"source_unit": u, "status": "translated", "evidence": "p1"} for u in units],
                  "ceilings": {n: {"applies": False, "evidence": "not observed", "source_units": [], "target_pages": []}
                               for n in reader.CEILINGS}}
        for mode in ("release", "exact"):
            with self.subTest(judge_format=mode):
                evaluated = copy.deepcopy(review)
                decision = reader.validate_quality_review(evaluated, bundle, mode)  # as run_eval.py calls it
                self.assertEqual(decision["maximum_total"], 100)
                self.assertEqual(reader.validate_quality_review(copy.deepcopy(evaluated), bundle, mode), decision)


if __name__ == "__main__":
    unittest.main()
