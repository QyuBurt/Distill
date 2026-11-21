"""Every relative import in the app points at something that exists. No Qt.

A tree assembled from files of different ages (an old widgets.py under a newer main_window.py, a module copied in without the file it
imports) fails here, by name, before it fails in the running app.
"""
from __future__ import annotations

import ast
import functools
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "distill"


@functools.lru_cache(maxsize=None)
def defined_names(path: Path) -> frozenset[str]:
    """Names a module defines or imports at its top level (including inside if / try blocks)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8", errors="ignore"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for elt in ast.walk(target):
                    if isinstance(elt, ast.Name):
                        names.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return frozenset(names)


def guarded(tree: ast.AST) -> set[int]:
    """The nodes inside a `try:` whose handler catches a failed import: an optional module (local_keys.py is one) may be absent."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                h.type is None or any(isinstance(n, ast.Name) and n.id in ("ImportError", "ModuleNotFoundError", "Exception")
                                      for n in ast.walk(h.type)) for h in node.handlers):
            skip |= {id(sub) for stmt in node.body for sub in ast.walk(stmt)}
    return skip


def problems() -> list[str]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        package = list(path.relative_to(ROOT).parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        optional = guarded(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.level) or id(node) in optional:
                continue
            base = package[: max(0, len(package) - (node.level - 1))]
            target = ROOT.joinpath(*base, *(node.module.split(".") if node.module else []))
            module = target.with_suffix(".py") if target.with_suffix(".py").exists() else target / "__init__.py"
            if not module.exists():
                found.append(f"{path.relative_to(ROOT)}: imports '{node.module}', which does not exist")
                continue
            have = defined_names(module)
            for alias in node.names:
                if alias.name != "*" and alias.name not in have and not (target.is_dir() and (target / f"{alias.name}.py").exists()):
                    found.append(f"{path.relative_to(ROOT)}: imports '{alias.name}' from '{node.module or '.'}', which does not define it")
    return found


def module_attribute_problems() -> list[str]:
    """`ripplemath.chase` where ripplemath.py has no `chase`: a module imported whole, with an attribute that is not defined in it."""
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        package = list(path.relative_to(ROOT).parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        optional = guarded(tree)
        whole: dict[str, Path] = {}  # local name -> the module file it is
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and not node.module and id(node) not in optional:
                base = ROOT.joinpath(*package[: max(0, len(package) - (node.level - 1))])
                for alias in node.names:
                    if (base / f"{alias.name}.py").exists():
                        whole[alias.asname or alias.name] = base / f"{alias.name}.py"
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in whole
                    and node.attr not in defined_names(whole[node.value.id]) and not node.attr.startswith("__")):
                found.append(f"{path.relative_to(ROOT)}: {node.value.id}.{node.attr} is used, but {whole[node.value.id].name} does not define it")
    return found


class ImportsResolve(unittest.TestCase):
    def test_every_relative_import_points_at_something_that_exists(self):
        self.assertEqual(problems(), [], "files of different ages in one tree, or a module that was not copied across")

    def test_every_module_attribute_a_file_uses_is_defined_in_that_module(self):
        self.assertEqual(module_attribute_problems(), [], "a file calls something its module does not have (an older copy of that module?)")


if __name__ == "__main__":
    unittest.main()
