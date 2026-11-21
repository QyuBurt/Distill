"""The shared widgets have the methods other files call on them. No Qt (it reads widgets.py, it does not import it).

This is the 'Expander has no attribute set_summary' mistake: an older widgets.py under newer pages that call it.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "distill" / "ui"
# (class in widgets.py, the members other files rely on)
EXPECTED = {
    "ExpanderHeader": {"set_summary"},
    "Expander": {"set_summary", "add"},
    "ToolRow": {"set_state", "set_info"},
    "RunBar": {"set_info", "set_value", "_sync", "_tick", "_moving"},
    "SideCard": {"set_glass", "set_backdrop", "glass"},  # the frosted sidebar main_window drives
}


def members(class_name: str) -> set[str]:
    tree = ast.parse((UI / "widgets.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            found = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for n in ast.walk(node):  # attributes set in __init__ (self.chip = ...)
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) and n.value.id == "self":
                    found.add(n.attr)
            return found
    return set()


class SharedWidgetApi(unittest.TestCase):
    def test_each_shared_widget_has_what_the_pages_call(self):
        for cls, needed in EXPECTED.items():
            with self.subTest(cls=cls):
                self.assertFalse(needed - members(cls), f"widgets.py: {cls} is missing {sorted(needed - members(cls))}")

    def test_the_tool_row_has_the_controls_the_settings_page_drives(self):
        self.assertTrue({"chip", "version", "update_btn"} <= members("ToolRow"))

    def test_everything_that_calls_set_summary_is_calling_something_that_has_it(self):
        callers = [p.name for p in UI.glob("*.py") if ".set_summary(" in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertTrue(callers)  # the pages that fold a card and say what it is set to
        self.assertIn("set_summary", members("Expander"))


if __name__ == "__main__":
    unittest.main()
