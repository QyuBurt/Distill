"""A method defined twice in one class silently replaces the first: v1.2.0 shipped two `DownloadMixin._dl_after_run` and the
later one (different arguments) broke the end of every download. Nothing fails until that code path runs, so check the source.
No Qt needed."""
from __future__ import annotations

import ast
import collections
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "distill"


def _is_accessor_or_overload(fn: ast.FunctionDef) -> bool:
    for d in fn.decorator_list:
        if isinstance(d, ast.Attribute) and d.attr in {"setter", "getter", "deleter", "register"}:
            return True  # property pairs and singledispatch registrations share a name on purpose
        if isinstance(d, ast.Name) and d.id == "overload":
            return True
    return False


class NoDuplicateMethods(unittest.TestCase):
    def test_no_class_defines_a_method_twice(self):
        clashes = []
        for path in sorted(ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                names = collections.Counter(
                    f.name for f in cls.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_accessor_or_overload(f))
                clashes += [f"{path.relative_to(ROOT.parent)}: {cls.name}.{n} defined {k} times"
                            for n, k in names.items() if k > 1]
        self.assertEqual(clashes, [])


if __name__ == "__main__":
    unittest.main()
