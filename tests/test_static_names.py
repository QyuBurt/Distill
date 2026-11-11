"""Catches the kind of mistake the UI tests can't without Qt: a name used in a function that nothing defines or imports (a forgotten
import after a refactor crashes the page the first time it opens). Reads the source; imports nothing."""
import builtins
import symtable
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "distill"
# Known and harmless: unreachable code after a return (a stray line, never run).
KNOWN = {("ui/emulator_page.py", "_em_released_note", "e")}


def undefined(path: Path) -> list[tuple[str, str]]:
    top = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    module = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    allowed, found = module | set(dir(builtins)) | {"__file__", "__name__", "__doc__"}, []

    def walk(table) -> None:
        for sym in table.get_symbols():
            if table.get_type() != "module" and sym.is_global() and sym.is_referenced() and sym.get_name() not in allowed:
                found.append((table.get_name(), sym.get_name()))
        for child in table.get_children():
            walk(child)
    for child in top.get_children():
        walk(child)
    return found


class NoUndefinedNames(unittest.TestCase):
    def test_every_name_a_function_uses_is_defined_or_imported(self):
        problems = []
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            problems += [f"{rel}: {fn}() uses {name!r}" for fn, name in undefined(path) if (rel, fn, name) not in KNOWN]
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
