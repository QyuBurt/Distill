"""Downloads and tool jobs (conversions, Switch, Organise) each have a run of their own, so one can go while the other does. These read the
source to keep it that way: the download side must not reach into the conversion's state, nor the conversion's into the download's."""
import ast
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "distill" / "ui"

# the conversion run's state and widgets
TOOL_SLOT = {"runner", "pause_event", "is_paused", "_t0", "_paused_total", "_pause_t", "_eta_tick", "_stopping", "pb_total", "lbl_eta",
             "lbl_phase", "lbl_run_info", "lbl_total_prog", "_set_bar", "_active_secs", "_update_ui_state", "_cleanup_ui", "_refresh_eta",
             "_begin_run_label", "_run_label", "_run_page", "_set_settings_visible", "_launch"}
# the download run's state and widgets
DL_SLOT = {"dl_runner", "dl_paused", "dl_stopping", "dl_pause_event", "_dl_t0", "_dl_paused_total", "_dl_pause_t", "_dl_eta_tick",
           "dl_pb", "dl_eta", "_dl_page", "_dl_label", "_dl_prog_text", "_set_dl_bar", "_dl_active_secs", "_dl_cleanup_ui", "_refresh_dl"}
# the conversion run's own methods
TOOL_METHODS = {"_launch", "_cleanup_ui", "_refresh_eta", "_update_ui_state", "_set_bar", "_retry", "_show_summary", "_active_secs"}


def self_attrs(node: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"}


def parse(name: str) -> ast.Module:
    return ast.parse((UI / name).read_text(encoding="utf-8"))


class RunSplit(unittest.TestCase):
    def test_the_download_pages_never_touch_the_conversions_run(self):
        for name in ("download_page.py", "soundtrack_page.py"):
            self.assertEqual(sorted(self_attrs(parse(name)) & TOOL_SLOT), [], name)

    def test_the_conversions_run_never_touches_the_downloads(self):
        for fn in (n for n in ast.walk(parse("run_panel.py")) if isinstance(n, ast.FunctionDef) and n.name in TOOL_METHODS):
            self.assertEqual(sorted(self_attrs(fn) & DL_SLOT), [], fn.name)

    def test_nothing_still_asks_whether_the_conversion_run_is_a_download(self):
        for path in sorted(UI.glob("*.py")):
            self.assertNotIn("isinstance(self.runner, DownloadRunner)", path.read_text(encoding="utf-8"), path.name)

    def test_the_download_runs_state_is_set_up_with_the_window(self):
        init = next(f for c in parse("main_window.py").body if isinstance(c, ast.ClassDef)
                    for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "__init__")
        stored = {n.attr for n in ast.walk(init) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                  and n.value.id == "self" and isinstance(n.ctx, ast.Store)}
        for attr in ("dl_runner", "dl_paused", "dl_stopping", "dl_pause_event", "_dl_t0", "_dl_paused_total", "_dl_pause_t",
                     "_dl_eta_tick", "_dl_page", "_dl_label", "_dl_prog_text"):
            self.assertIn(attr, stored, attr)

    def test_the_download_runners_are_all_given_the_downloads_own_pause_and_emitter(self):
        src = (UI / "download_page.py").read_text(encoding="utf-8")
        for runner in ("TorBoxRunner", "DebridTorrentRunner", "TorrentRunner", "DownloadRunner"):
            start = src.index(f"runner = {runner}(")  # the assignment itself: "TorrentRunner(" alone would also match inside "DebridTorrentRunner("
            block = src[start:start + 260]
            self.assertIn("self._dl_emit", block, runner)
            self.assertIn("self.dl_pause_event", block, runner)
        self.assertNotIn("self.ui_q.put, self.pause_event", src)


if __name__ == "__main__":
    unittest.main()
