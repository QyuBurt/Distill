"""Builds the real window off-screen. Skipped when PySide6 isn't installed (CI installs it)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

try:
    import PySide6  # noqa: F401
    HAVE_QT = not os.environ.get("DISTILL_SKIP_UI_TESTS")  # set by build_exe.bat and CI where window tests can't run reliably
except ImportError:  # pragma: no cover
    HAVE_QT = False

if sys.platform != "win32":  # Windows' offscreen plugin has no fonts and can crash; the normal one works without showing windows
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from distill import soundtracks as st
from tests.helpers import IsolatedSoundtrackFiles, WindowTestCase
from tests import helpers  # noqa: E402,F401


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class WindowSmokeTests(WindowTestCase, IsolatedSoundtrackFiles):
    def setUp(self):
        # Slots invoked after construction can also start cover/network workers.
        workers = mock.patch("threading.Thread.start", lambda self: None)
        workers.start()
        self.addCleanup(workers.stop)
        super().setUp()

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        import distill.tools as tools
        from distill.ui.main_window import DistillWindow

        with mock.patch.object(tools.ToolManager, "run", lambda self: None, create=True), \
                mock.patch("threading.Thread.start", lambda self: None):  # no network, no background workers
            return DistillWindow()

    def test_close_keeps_production_hard_exit_local_to_the_window_module(self):
        from distill.ui import main_window
        real_exit = os._exit
        win = self.make_window()
        with mock.patch.object(main_window, "os", wraps=os) as local_os:
            local_os._exit = mock.Mock()
            win.close()
            local_os._exit.assert_called_once_with(0)
            self.assertIs(os._exit, real_exit)

    def test_window_fixture_intercepts_exit_without_patching_global_os(self):
        from distill.ui import main_window
        self.assertIsNot(main_window.os, os)
        self.assertIs(main_window.os._exit, self.window_exit)
        self.make_window().close()
        self.window_exit.assert_called_once_with(0)

    def test_window_theme_bindings_return_to_baseline(self):
        from distill.ui.theme import T
        import shiboken6

        baseline = sum(shiboken6.isValid(ref()) for ref, _ in T._bound.values() if ref() is not None)
        bound_baseline = len(T._bound)
        for _ in range(3):
            win = self.make_window()
            with mock.patch("distill.ui.main_window.os", wraps=os) as local_os:
                local_os._exit = mock.Mock()
                win.close()
            # Exercise the same teardown the fixture uses between tests.
            self.cleanup_widgets()
            live = sum(shiboken6.isValid(ref()) for ref, _ in T._bound.values() if ref() is not None)
            self.assertEqual(live, baseline)
            self.assertEqual(len(T._bound), bound_baseline)

    def test_window_builds_with_every_page(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("Distill", win.windowTitle())
        self.assertGreater(win.stack.count(), 5)

    def test_smooth_wheel_glides_toward_an_accumulating_target_and_passes_on_at_the_ends(self):
        from PySide6.QtWidgets import QListWidget

        from distill.ui import motion

        view = QListWidget()
        self.addCleanup(view.close)
        sm = motion.smooth_scroll(view)
        bar = view.verticalScrollBar()
        bar.setRange(0, 1000)
        self.assertFalse(sm.nudge(1))  # already at the top: the wheel is left for the page around it
        self.assertTrue(sm.nudge(-1))
        self.assertEqual(sm.target, motion.SmoothWheel.STEP)
        self.assertTrue(sm.nudge(-2))  # notches that arrive mid-glide add up
        self.assertEqual(sm.target, 3 * motion.SmoothWheel.STEP)
        sm._anim.stop()
        bar.setValue(1000)
        self.assertFalse(sm.nudge(-1))  # at the bottom

    def test_an_emulator_icon_keeps_no_tint_or_outline_after_a_restyle(self):
        """The monogram's coloured fill and border are bound to the theme; a picture must replace that binding, or the next restyle
        (a theme switch, start-up) draws the tint and outline behind the icon again."""
        import tempfile
        from pathlib import Path

        from PySide6.QtGui import QColor, QImage

        from distill.ui.emu_cards import EmulatorCard, EmulatorTile
        from distill.ui.theme import T

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        picture = QImage(64, 64, QImage.Format.Format_ARGB32)
        picture.fill(QColor(255, 0, 0, 255))
        path = Path(td.name) / "icon.png"
        self.assertTrue(picture.save(str(path), "PNG"))
        for widget, attr in ((EmulatorCard("a", "Alpha", "System"), "tile"), (EmulatorTile("b", "Beta", "System"), "mono")):
            self.addCleanup(widget.deleteLater)
            widget.set_logo(str(path))
            T.restyle()
            css = getattr(widget, attr).styleSheet()
            self.assertNotIn("rgba", css)
            self.assertNotIn("1px", css)

    def test_recomps_page_lists_catalog_filters_by_library_and_opens_a_project(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_rc_ui_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(rc, "ROOT", Path(td.name)), \
                mock.patch.object(rc, "cache_root", return_value=Path(td.name)):
            win = self.make_window()
            self.addCleanup(win.close)
            self.assertIn("recomps", win.pages)
            with mock.patch("threading.Thread.start", lambda self: None):  # no GitHub, no cover downloads
                win._nav_click("recomps")
                self.assertGreater(win.rc_list.count(), 3)
                self.assertTrue(win.bar_wrap.isHidden())  # no batch to start on this page
                first = win._rc_catalog[0]
                win._rc_show_detail(first.id)
                self.assertEqual(win.rc_views.currentIndex(), 1)
                self.assertEqual(win.rcd_title.text(), first.name)
                self.assertFalse(win.rcd_main.isEnabled())  # no release known yet: nothing to install
                # a library report that holds the Ocarina of Time dump -> that project is "likely", the filter hides the rest
                from distill.library import LibraryEntry
                entry = LibraryEntry("Zelda - Ocarina of Time (USA).z64", "Nintendo 64", "Z64", "USA", 10, 0.0, "", "/roms/oot.z64")
                win.org["report"].findings = [SimpleNamespace(data=[entry])]
                found = rc.match_library(win._rc_catalog, [entry])
                win._apply_events([("recomp_owned", found, "")])
                self.assertTrue(win.rc_owned_only.isChecked())
                from distill.ui.recomp_page import ID_ROLE
                shown = {win.rc_list.item(i).data(ID_ROLE) for i in range(win.rc_list.count())}
                self.assertEqual(shown, {"ship-of-harkinian"})
                win._rc_back()
                self.assertEqual(win.rc_views.currentIndex(), 0)

    def test_emulators_page_lists_groups_links_installs_and_filters(self):
        import tempfile
        from pathlib import Path

        from distill import emulators as em
        from distill import recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_em_ui_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(rc, "ROOT", Path(td.name)), \
                mock.patch.object(rc, "cache_root", return_value=Path(td.name)):
            win = self.make_window()
            self.addCleanup(win.close)
            self.assertIn("emus", win.pages)
            with mock.patch("threading.Thread.start", lambda self: None):  # no GitHub, no scan
                win._nav_click("emus")
                self.assertGreater(len(win.em_cards), 2)
                self.assertTrue(win.bar_wrap.isHidden())
                first = win._em_catalog[0]
                win._em_show_detail(first.id)
                self.assertEqual(win.em_views.currentIndex(), 1)
                self.assertEqual(win.emd_title.text(), first.name)
                self.assertFalse(win.emd_main.isEnabled())  # no release known yet
                self.assertEqual(win.emd_main.text(), "Install")
                win._apply_events([("emu_latest", first.id, (rc.Release("v1", "v1", "", "", False, "", ()), {"name": "a.zip", "url": "u", "size": 1, "digest": ""}))])
                self.assertTrue(win.emd_main.isEnabled())  # a release is known and nothing is installed: Install must be clickable
                win._em_back()
                # a link made by a scan shows up as installed and the filter can show only those
                exe = Path(td.name) / "PCSX2" / "pcsx2-qt.exe"
                exe.parent.mkdir()
                exe.write_bytes(b"MZ")
                win.em_store.set_installed(rc.Installed("pcsx2", "v1", str(exe.parent), "pcsx2-qt.exe", 0.0, "folder"))
                win.em_filter.setCurrentIndex(1)
                self.assertEqual([e.name for e in win._em_visible()], ["PCSX2"])
                self.assertEqual(list(win.em_cards), ["pcsx2"])
                # a newer release arrives: the list says so, and Update all appears
                win._apply_events([("emu_latest", "pcsx2", (rc.Release("v2", "v2", "", "", False, "", ()), {"name": "p.7z", "url": "u", "size": 1, "digest": ""}))])
                self.assertEqual(len(win._em_updatable()), 1)
                self.assertFalse(win.em_all_btn.isHidden() and False)
                win._em_show_detail("pcsx2")
                self.assertEqual(win.emd_main.text(), "Update")
                self.assertEqual(win.emd_remove.text(), "Unlink")
                self.assertIsNotNone(em)

    def test_linux_source_build_requires_a_folder_and_the_result_is_discoverable(self):
        import tempfile
        from pathlib import Path
        from distill import emulators as em, recomps as rc
        from distill.ui import emulator_page

        td = tempfile.TemporaryDirectory(prefix="distill_source_ui_")
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._em_store = rc.Store(root / "state.json")
        e = em.Emulator("ares", "ares", "Multi", repo="ares-emulator/ares", build="ares", exe=r"(^|/)ares$")
        win._em_by_id = {e.id: e}
        win._em_current = e.id
        win._em_latest[e.id] = (rc.Release("source", "ares", "", "", False, "", ()), {})

        def completed_build(key, live, version):
            exe = live / "bin/ares"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"\x7fELF" + b"\0" * 64)
            exe.chmod(0o755)
            return live / "build-from-source.sh"

        with mock.patch.object(rc, "platform_key", return_value="linux"), \
                mock.patch.object(emulator_page.builds, "start", side_effect=completed_build) as start, \
                mock.patch.object(emulator_page.QMessageBox, "information") as info:
            win._em_build_from_source()
            start.assert_not_called()
            self.assertIn("Choose an Emulators folder", info.call_args.args[2])
            em.set_emulators_root(win.em_store, root / "Emulators")
            win._em_build_from_source()
            start.assert_called_once()
            self.assertEqual(start.call_args.args[2], "")
            self.assertIn("Find installed", info.call_args.args[2])
            with mock.patch.object(em, "default_roots", return_value=[]):
                found = em.scan_all(win.em_store, [e], "linux")
            self.assertIn(e.id, found)
            em.apply_scan(win.em_store, found)
            self.assertEqual(win.em_store.installed(e.id).exe_path(), root / "Emulators/ares/bin/ares")

    def test_refused_linux_source_build_cannot_start_from_detail_or_card(self):
        import tempfile
        from pathlib import Path
        from distill import emulators as em, recomps as rc
        from distill.ui import emulator_page

        td = tempfile.TemporaryDirectory(prefix="distill_refused_build_")
        self.addCleanup(td.cleanup)
        win = self.make_window()
        self.addCleanup(win.close)
        win._em_store = rc.Store(Path(td.name) / "state.json")
        entry = em._emulator({"id": "kytyps5", "name": "KytyPS5", "repo": "KytyPS5/KytyPS5",
                              "trusted_owner": "KytyPS5", "build": "ares"}, "linux")
        self.assertFalse(entry.available)
        win._em_catalog = [entry]
        win._em_by_id = {entry.id: entry}
        win._em_latest[entry.id] = (rc.Release("source", "source", "", "", False, "", ()),
                                   {"name": "built from source", "url": "", "size": 0})
        with mock.patch.object(rc, "platform_key", return_value="linux"), \
                mock.patch.object(emulator_page.builds, "start") as start, \
                mock.patch.object(em.EmulatorInstaller, "live_for") as live, \
                mock.patch.object(emulator_page.QMessageBox, "warning") as warning, \
                mock.patch.object(emulator_page.QMessageBox, "information") as info, \
                mock.patch.object(win, "_em_start_install") as download:
            win._em_show_detail(entry.id)
            self.assertTrue(win.emd_build.isHidden())
            self.assertFalse(win.emd_main.isEnabled())
            win._em_build_from_source()
            self.assertEqual(warning.call_args.args[2], entry.notes)
            warning.reset_mock()
            win._em_card_action(entry.id, "update")
            self.assertEqual(warning.call_args.args[2], entry.notes)
            start.assert_not_called()
            live.assert_not_called()
            download.assert_not_called()
            info.assert_not_called()

    def test_linux_source_builds_are_excluded_from_card_and_bulk_download_updates(self):
        import tempfile
        from pathlib import Path
        from distill import emulators as em, recomps as rc
        from distill.ui import emulator_page

        td = tempfile.TemporaryDirectory(prefix="distill_source_updates_")
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._em_store = rc.Store(root / "state.json")
        exe = root / "ares"
        exe.write_bytes(b"\x7fELF")
        e = em.Emulator("ares", "ares", "Multi", repo="ares-emulator/ares", build="ares")
        win.em_store.set_installed(rc.Installed(e.id, "v1", str(root), exe.name))
        win._em_catalog = [e]
        win._em_by_id = {e.id: e}
        win._em_latest = {e.id: (rc.Release("v2", "v2", "", "", False, "", ()), {"url": ""})}
        card = mock.Mock()
        win.em_cards = {e.id: card}
        with mock.patch.object(rc, "platform_key", return_value="linux"), \
                mock.patch.object(emulator_page.QMessageBox, "question") as question:
            self.assertEqual(win._em_updatable(), [])
            win._em_apply_card(e)
            self.assertFalse(card.apply.call_args.kwargs["can_update"])
            win._em_update_all()
            question.assert_not_called()
            with mock.patch.object(win, "_em_show_detail") as detail, \
                    mock.patch.object(win, "_em_build_from_source") as build, \
                    mock.patch.object(win, "_em_start_install") as download:
                win._em_card_action(e.id, "update")
            detail.assert_called_once_with(e.id)
            build.assert_called_once()
            download.assert_not_called()

    def test_linux_linked_and_scanned_project64_launches_through_wine_without_changing_ownership(self):
        import tempfile
        from pathlib import Path
        from distill import emulators as em, recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_linked_wine_")
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "Emulators"
        root.mkdir()
        win = self.make_window()
        self.addCleanup(win.close)
        win._em_store = rc.Store(Path(td.name) / "state.json")
        em.set_emulators_root(win.em_store, root)
        e = em.Emulator("project64", "Project64", "Nintendo 64", wine=True, exe=r"(^|/)Project64\.exe$")
        win._em_by_id = {e.id: e}
        with mock.patch.object(rc, "platform_key", return_value="linux"), \
                mock.patch.object(em.shutil, "which", return_value="/usr/bin/wine"):
            for source, linked in (("link", "file"), ("scan", "folder")):
                with self.subTest(source=source):
                    folder = root if source == "link" else root / "Project64"
                    folder.mkdir(exist_ok=True)
                    exe = folder / "Project64.exe"
                    exe.write_bytes(b"MZ")
                    save = folder / "keep.sav"
                    save.write_bytes(b"user save")
                    if source == "link":
                        em.link(win.em_store, e, exe, "linux")
                    else:
                        with mock.patch.object(em, "default_roots", return_value=[]):
                            em.apply_scan(win.em_store, em.scan_all(win.em_store, [e], "linux"))
                    installed = win.em_store.installed(e.id)
                    self.assertEqual(installed.linked, linked)
                    before = sorted(folder.iterdir())
                    with mock.patch.object(em.subprocess, "Popen") as process:
                        win._em_launch_id(e.id)
                    self.assertEqual(process.call_args.args[0], ["wine", str(exe)])
                    self.assertEqual(process.call_args.kwargs["cwd"], str(folder))
                    self.assertEqual(win.em_store.installed(e.id), installed)
                    self.assertEqual(sorted(folder.iterdir()), before)
                    self.assertFalse((folder / em.LAUNCHER).exists())
                    em.uninstall(win.em_store, e)
                    self.assertEqual(exe.read_bytes(), b"MZ")
                    self.assertEqual(save.read_bytes(), b"user save")

    def owned_flatpak_window(self):
        import queue
        import tempfile
        from pathlib import Path
        from distill import emulators as em, recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_remove_flatpak_")
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win.ui_q = queue.Queue()
        win._em_store = rc.Store(root / "state.json")
        em.set_emulators_root(win.em_store, root / "Emulators")
        e = em.Emulator("mame", "MAME", "Arcade", flatpak="org.mamedev.MAME")
        live = root / "Emulators/MAME"
        live.mkdir(parents=True)
        (live / em.LAUNCHER).write_text("#!/bin/sh\n")
        save = live / "keep.sav"
        save.write_bytes(b"user save")
        installed = rc.Installed(e.id, "0.289", str(live), em.LAUNCHER)
        win.em_store.data["flatpak_installs"] = {e.id: {"app": e.flatpak, "owned": True}}
        win.em_store.set_installed(installed)
        win._em_catalog = [e]
        win._em_by_id = {e.id: e}
        win._em_current = e.id
        win.em_views.setCurrentIndex(1)
        return win, e, installed, save

    def test_owned_flatpak_removal_uses_a_guarded_worker_and_delivers_success_or_failure(self):
        from distill import emulators as em, recomps as rc
        from distill.ui import emulator_page

        for fail in (True, False):
            with self.subTest(failure=fail):
                win, e, installed, save = self.owned_flatpak_window()
                result = mock.Mock(returncode=1 if fail else 0, stderr=b"locked" if fail else b"", stdout=b"")
                with mock.patch.object(rc, "platform_key", return_value="linux"), \
                        mock.patch.object(emulator_page.QMessageBox, "question", return_value=emulator_page.QMessageBox.StandardButton.Yes) as question, \
                        mock.patch.object(emulator_page.threading, "Thread") as worker, \
                        mock.patch.object(em, "uninstall", wraps=em.uninstall) as uninstall, \
                        mock.patch.object(em.subprocess, "run", return_value=result), \
                        mock.patch.object(win, "_em_say", wraps=win._em_say) as say:
                    win._em_remove()
                    uninstall.assert_not_called()
                    worker.assert_called_once_with(target=win._em_start_remove, args=(e,), daemon=True)
                    worker.return_value.start.assert_called_once()
                    self.assertFalse(win.emd_remove.isEnabled())
                    self.assertFalse(win.emd_main.isEnabled())
                    win._em_remove()
                    win._em_begin_install(e)
                    worker.assert_called_once()
                    question.assert_called_once()
                    self.assertFalse(win._em_busy[e.id].is_set())
                    say.reset_mock()
                    worker.call_args.kwargs["target"](*worker.call_args.kwargs["args"])
                    say.assert_not_called()  # Workers queue results instead of touching Qt widgets.
                    self.assertIn(e.id, win._em_busy)
                    event = win.ui_q.get_nowait()
                    self.assertEqual(event[0], "emu_removed")
                    win._em_event(event)
                    self.assertNotIn(e.id, win._em_busy)
                    self.assertNotIn(e.id, win._em_removing)
                    self.assertNotIn(e.id, win._em_prog)
                    if fail:
                        self.assertIn("locked", win.emd_msg.text())
                        self.assertTrue(win.emd_remove.isEnabled())
                        self.assertEqual(rc.Store(win.em_store.path).installed(e.id), installed)
                        self.assertTrue(win.em_store.data["flatpak_installs"][e.id]["owned"])
                        self.assertEqual(save.read_bytes(), b"user save")
                    else:
                        self.assertEqual(win.emd_msg.text(), "Uninstalled.")
                        self.assertIsNone(rc.Store(win.em_store.path).installed(e.id))
                        self.assertFalse(installed.exe_path().exists())

    def test_windows_flatpak_removal_retains_the_existing_synchronous_path(self):
        from distill import emulators as em, recomps as rc
        from distill.ui import emulator_page

        win, e, _installed, _save = self.owned_flatpak_window()
        with mock.patch.object(rc, "platform_key", return_value="windows"), \
                mock.patch.object(emulator_page.QMessageBox, "question", return_value=emulator_page.QMessageBox.StandardButton.Yes), \
                mock.patch.object(emulator_page.threading, "Thread") as worker, \
                mock.patch.object(em, "uninstall", wraps=em.uninstall) as uninstall:
            win._em_remove()
        uninstall.assert_called_once_with(win.em_store, e)
        worker.assert_not_called()
        self.assertEqual(win.emd_msg.text(), "Uninstalled.")

    def test_emulators_page_shows_compact_tiles_by_default_and_can_switch_to_cards(self):
        import tempfile
        from pathlib import Path

        from distill import recomps as rc
        from distill.ui.emu_cards import EmulatorCard, EmulatorTile

        td = tempfile.TemporaryDirectory(prefix="distill_em_view_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(rc, "ROOT", Path(td.name)), \
                mock.patch.object(rc, "cache_root", return_value=Path(td.name)):
            win = self.make_window()
            self.addCleanup(win.close)
            with mock.patch("threading.Thread.start", lambda self: None):
                win._nav_click("emus")
                self.assertTrue(win.em_view_tiles.isChecked())  # compact is the default
                self.assertFalse(win.em_view_cards.isChecked())
                ids = list(win.em_cards)
                self.assertGreater(len(ids), 2)
                self.assertTrue(all(isinstance(c, EmulatorTile) for c in win.em_cards.values()))
                win.em_view_cards.click()
                self.assertEqual(list(win.em_cards), ids)  # the same emulators, in the same order, as cards
                self.assertTrue(all(isinstance(c, EmulatorCard) for c in win.em_cards.values()))
                win.em_filter.setCurrentIndex(3)  # a filter still applies in the other look
                self.assertTrue(all(isinstance(c, EmulatorCard) for c in win.em_cards.values()))
                win.em_filter.setCurrentIndex(0)
                win.em_view_tiles.click()
                self.assertEqual(list(win.em_cards), ids)
                self.assertTrue(all(isinstance(c, EmulatorTile) for c in win.em_cards.values()))

    def test_an_emulator_tile_shows_status_and_the_buttons_that_apply(self):
        from PySide6.QtGui import QColor

        from distill.ui.emu_cards import EmulatorTile

        tile = EmulatorTile("pcsx2", "PCSX2", "PlayStation 2")
        got = []
        tile.action.connect(lambda rid, kind: got.append((rid, kind)))
        tile.apply(text="Update available", colour=QColor("#e0a030"), facts="v1.7  ·  12 games", can_launch=True, can_update=True,
                   busy=False, tip="Notes")
        self.assertIn("Update available", tile.sub.text())
        self.assertIn("PlayStation 2", tile.sub.text())  # the system follows the status, so it is the part that clips
        self.assertFalse(tile.launch.isHidden())
        self.assertFalse(tile.update_btn.isHidden())
        self.assertIn("12 games", tile.toolTip())  # what the tile has no room for is in the tooltip
        tile.update_btn.click()
        tile.launch.click()
        self.assertEqual(got, [("pcsx2", "update"), ("pcsx2", "launch")])
        tile.apply(text="Not installed", colour=QColor("#95a1b4"), facts="", can_launch=False, can_update=False, busy=False)
        self.assertTrue(tile.launch.isHidden())
        self.assertTrue(tile.update_btn.isHidden())
        tile.apply(text="Update available", colour=QColor("#e0a030"), facts="", can_launch=True, can_update=True, busy=True)
        self.assertFalse(tile.launch.isEnabled())  # nothing to click while an install is running
        self.assertFalse(tile.update_btn.isEnabled())
        tile.set_busy_text("Downloading 42%")
        self.assertIn("Downloading 42%", tile.sub.text())

    def test_the_emulator_view_choice_is_remembered_between_runs(self):
        first = self.make_window()
        self.addCleanup(first.close)
        first.em_view_cards.click()
        first._settings_save()
        second = self.make_window()
        self.addCleanup(second.close)
        self.assertTrue(second.em_view_cards.isChecked())
        self.assertFalse(second.em_view_tiles.isChecked())

    def test_forks_sit_under_their_emulator_and_install_on_their_own(self):
        import tempfile
        from pathlib import Path

        from distill import emulators as em
        from distill import recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_fork_ui_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(rc, "ROOT", Path(td.name)), \
                mock.patch.object(rc, "cache_root", return_value=Path(td.name)), \
                mock.patch.object(em, "forks_manifest", lambda: Path(td.name) / "forks.json"):
            win = self.make_window()
            self.addCleanup(win.close)
            with mock.patch("threading.Thread.start", lambda self: None):
                win._nav_click("emus")
                parent = next(e for e in win._em_catalog if e.id == "kytyps5")
                fork = em.add_fork(parent, "Jetsku/KytyPS5", "", "Astro Bot work", accepted=True, catalog=win._em_catalog)
                win._em_reload_forks()
                win._em_fill()
                self.assertNotIn(fork.id, win.em_cards)  # a fork is not a card of its own
                self.assertIn("kytyps5", win.em_cards)
                win._em_show_detail("kytyps5")
                self.assertIn(fork.id, win._em_fork_rows)
                self.assertFalse(win.emd_forks_card.isHidden())
                row = win._em_fork_rows[fork.id]
                self.assertFalse(row.buttons["install"].isEnabled())  # no release known yet
                latest = (rc.Release("u59", "u59", "", "2026-10-03", False, "", ()), {"name": "k.zip", "url": "https://github.com/Jetsku/KytyPS5/x/k.zip", "size": 1, "digest": ""})
                win._apply_events([("emu_latest", fork.id, latest)])
                self.assertTrue(win._em_fork_rows[fork.id].buttons["install"].isEnabled())
                self.assertEqual(win._em_updatable(), [])  # nothing installed, so nothing to update
                win._em_show_detail(fork.id)  # the fork's own page
                self.assertEqual(win.emd_back.text(), "← KytyPS5")
                self.assertTrue(win.emd_forks_card.isHidden())
                self.assertFalse(win.emd_unfork.isHidden())
                self.assertEqual(win.emd_main.text(), "Install")
                win._em_back()
                self.assertEqual(win._em_current, "kytyps5")
                # an installed fork by an author the person accepted by hand is never part of Update all
                exe = Path(td.name) / "KytyPS5 (Jetsku)" / "launcher.exe"
                exe.parent.mkdir()
                exe.write_bytes(b"MZ")
                win.em_store.set_installed(rc.Installed(fork.id, "u58", str(exe.parent), "launcher.exe", 0.0, ""))
                self.assertEqual(win._em_updatable(), [])
                win._em_back()
                win.em_filter.setCurrentIndex(1)
                self.assertEqual(list(win.em_cards), ["kytyps5"])  # a fork installed, so its emulator shows under Installed

    def test_soundtracks_page_lists_matches_tracks_and_downloads_ticked(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("sound", win.pages)
        win.st_names.setPlainText("Persona 5\nObscure Game")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        self.assertEqual(win.st_tree.topLevelItemCount(), 2)
        good = st.Album("archive.org", "p5", "Persona 5 Original Soundtrack", "", 1.0)
        win._st_on_found(0, st.GameResult(win._st_rows[0].result.request, [good]))
        win._st_on_found(1, st.GameResult(win._st_rows[1].result.request, []))
        files = [RemoteFile("Persona 5/soundtrack/01 - Opening.flac", "https://x/1", 10),
                 RemoteFile("Persona 5/soundtrack/02 - Battle.flac", "https://x/2", 20)]
        win._st_on_tracks(0, good, files, "")
        top = win.st_tree.topLevelItem(0)
        self.assertEqual(top.childCount(), 2)
        self.assertEqual(top.child(0).text(0), "01 - Opening.flac")
        self.assertEqual(top.text(2), "2 tracks, 30 B")
        self.assertEqual([r.item.text(0) for r in win._st_ticked()], ["Persona 5"])
        self.assertTrue(win.st_tree.topLevelItem(1).text(3).endswith("Nothing found"))
        started = []
        with mock.patch.object(type(win), "_dl_start_rows", lambda self, rows: started.append(rows)):
            win._st_download()
        self.assertEqual(started, [[0, 1]])
        self.assertEqual([f.name for f in win.dl_files],
                         ["Soundtracks/Persona 5/01 - Opening.flac", "Soundtracks/Persona 5/02 - Battle.flac"])
        self.assertTrue(win.dl_dest.text())  # no Save to given: the Downloads folder

    def test_clicking_the_cover_opens_a_larger_preview(self):
        from PySide6.QtGui import QColor, QPixmap
        from distill.ui import now_playing

        win = self.make_window()
        self.addCleanup(win.close)
        np = win.now_playing
        shown = []
        with mock.patch.object(now_playing.ArtPreview, "exec", lambda self: shown.append(self)):
            np.show_large_art()  # no cover yet: nothing opens
            self.assertEqual(shown, [])
            pix = QPixmap(100, 100)
            pix.fill(QColor("red"))
            np.set_art(pix)
            np.show_large_art()
        self.assertEqual(len(shown), 1)
        self.assertGreater(shown[0].layout().itemAt(0).widget().pixmap().width(), 52)

    def test_download_and_soundtrack_pages_lay_their_setup_cards_side_by_side(self):
        from distill.ui.emu_cards import TwoColumn

        win = self.make_window()
        self.addCleanup(win.close)
        for key in ("dl", "sound"):
            self.assertEqual(len(win.pages[key].findChildren(TwoColumn)), 1, key)  # setup in two columns, the list under them
        self.assertEqual((win.dl_table.placeholder_icon, win.st_tree.placeholder_icon), ("download", "music"))  # empty lists say what to do

    def test_a_tool_path_the_person_chose_is_remembered_but_one_the_app_found_is_not(self):
        import tempfile
        from pathlib import Path

        td = tempfile.TemporaryDirectory(prefix="distill_toolpath_")
        self.addCleanup(td.cleanup)
        exe = Path(td.name) / "hactoolnet.exe"
        exe.write_bytes(b"MZ")
        first = self.make_window()
        self.addCleanup(first.close)
        first._tool_path_found("nup", "./tools/nup/abc1234/nup.py")  # found by the app: never saved
        first._set_tool_text("hactoolnet", str(exe), user=True)  # picked by the person
        first._settings_save()
        second = self.make_window()
        self.addCleanup(second.close)
        self.assertEqual(second._tool_text("hactoolnet"), str(exe))
        self.assertNotIn("abc1234", second._tool_text("nup"))
        second._tool_path_found("hactoolnet", "./tools/hactoolnet/v0.19.0/hactoolnet.exe")  # a later auto-find leaves the choice alone
        self.assertEqual(second._tool_text("hactoolnet"), str(exe))

    def test_api_keys_live_in_general_settings_and_are_hidden_while_typed(self):
        import tempfile
        from pathlib import Path

        from PySide6.QtWidgets import QLineEdit

        from distill import apikeys

        td = tempfile.TemporaryDirectory(prefix="distill_apikeys_ui_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(apikeys, "FILE", Path(td.name) / "keys.json"):
            apikeys._cache = None
            win = self.make_window()
            self.addCleanup(win.close)
            self.assertEqual(set(win.api_keys.edits), {"sgdb", "github"})
            for edit in win.api_keys.edits.values():
                self.assertEqual(edit.echoMode(), QLineEdit.EchoMode.Password)
            seen = []
            win.api_keys.changed.connect(seen.append)
            win.api_keys.edits["github"].setText(" ghp_x ")
            win.api_keys._save()
            self.assertEqual((apikeys.get("github"), seen), ("ghp_x", ["github"]))
            apikeys._cache = None

    def test_options_and_layout_are_remembered_between_runs(self):
        first = self.make_window()
        first.tabs["chd"].source.setText("D:/roms")
        first.chd_compression.setCurrentText("Fast")
        first.thread_budget.setValue(1)
        first.notify_check.setChecked(False)
        first._show_page("rvz")
        first._settings_save()
        second = self.make_window()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertEqual(second.tabs["chd"].source.text(), "D:/roms")
        self.assertEqual(second.chd_compression.currentData(), "fast")
        self.assertEqual(second.thread_budget.value(), first.thread_budget.value())
        self.assertFalse(second.notify_check.isChecked())
        self.assertEqual(second.current_page, "rvz")

    def test_banner_shows_a_message_with_an_action_and_hides_after_it(self):
        win = self.make_window()
        self.addCleanup(win.close)
        called = []
        win._toast("Saved 3 tracks.", "ok", 0, ("Open folder", lambda: called.append(1)))
        self.assertFalse(win.banner.isHidden())
        win.banner._run_action()
        self.assertEqual(called, [1])
        self.assertTrue(win.banner.isHidden())

    def test_goto_filters_pages_and_pages_have_shortcut_tooltips(self):
        from distill.ui.palette import GoToDialog

        win = self.make_window()
        self.addCleanup(win.close)
        entries = win._goto_entries()
        self.assertIn("chd", [k for _l, k in entries])
        dlg = GoToDialog(entries, win)
        dlg.edit.setText("sound")
        self.assertEqual(dlg.list.count(), 1)
        from PySide6.QtCore import Qt

        self.assertEqual(dlg.list.item(0).data(Qt.ItemDataRole.UserRole), "sound")
        from PySide6.QtGui import QShortcut
        # CHD is no longer the first sidebar page; verify its actual shortcut.
        shortcuts = {s.key().toString(): s for s in win.findChildren(QShortcut)}
        self.assertIn("Ctrl+5", shortcuts)
        self.assertIn("Ctrl+5", win.nav_items["chd"].toolTip())

    def test_title_shows_progress_while_running_and_settings_search_filters(self):
        win = self.make_window()
        self.addCleanup(win.close)
        base = win.windowTitle()
        win.is_processing = True
        win._set_bar(50, 100)
        self.assertTrue(win.windowTitle().startswith("50%"))
        win.is_processing = False
        win._title_progress(None)
        self.assertEqual(win.windowTitle(), base)
        win.settings_search.setText("zzzz-no-such-setting")  # nothing matches: every card hides, no error
        win.settings_search.setText("parallel")
        win.settings_search.setText("")

    def test_volume_slider_and_mute_are_remembered(self):
        win = self.make_window()
        self.addCleanup(win.close)
        seen = []
        win.now_playing.volumeChanged.connect(lambda v, m: seen.append((round(v, 2), m)))
        win.now_playing._volume.setValue(30)
        self.assertEqual(seen[-1], (0.3, False))
        win.now_playing._mute.setChecked(True)
        self.assertEqual(seen[-1], (0.3, True))
        win.now_playing._volume.setValue(50)  # moving the slider while muted unmutes
        self.assertEqual(seen[-1], (0.5, False))
        self.assertEqual((win._st_volume, win._st_muted), (0.5, False))
        self.assertEqual(st.load_prefs()["volume"], 0.5)

    def test_soundtrack_status_gets_a_symbol_and_colour(self):
        win = self.make_window()
        self.addCleanup(win.close)
        from PySide6.QtWidgets import QTreeWidgetItem

        item = QTreeWidgetItem(["Game", "", "", "", ""])
        win._st_status(item, "Nothing found")
        self.assertTrue(item.text(3).startswith("\u2717"))
        win._st_status(item, "Ready")
        self.assertTrue(item.text(3).startswith("\u2713"))
        win._st_status(item, "")
        self.assertEqual(item.text(3), "")

    def test_start_button_drifts_only_while_running_and_holds_when_paused(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")  # the landing page hides the run panel
        win.show()
        btn = win.start_btn
        btn.setEnabled(True)  # tool-check workers are stubbed by this fixture
        self.assertTrue(btn.isVisible())
        btn.set_state(True, False)
        self.assertTrue(btn._tick_timer.isActive())
        before = btn._phase
        btn._tick()
        self.assertNotEqual(btn._phase, before)
        btn.set_state(True, True)  # paused: still glowing, no longer moving
        self.assertFalse(btn._tick_timer.isActive())
        self.assertGreater(btn._glow_goal, 0.3)
        btn.set_state(False, False)
        self.assertFalse(btn._tick_timer.isActive())
        self.assertEqual(btn._glow_goal, 0.30)

    def test_reduce_motion_keeps_the_glow_but_stops_every_animation(self):
        from distill.ui.motion import Motion

        win = self.make_window()
        self.addCleanup(win.close)
        self.addCleanup(setattr, Motion, "reduce", False)
        win.show()
        win.reduce_motion.setChecked(True)
        win.start_btn.set_state(True, False)
        self.assertFalse(win.start_btn._tick_timer.isActive())  # no drift
        self.assertEqual(win.start_btn._glow_goal, 1.0)  # but still glowing
        box = win.chd_compression  # a stage switch still works, it just does not animate
        box.setCurrentIndex(1)
        self.assertEqual(box.currentIndex(), 1)

    def _shown_window(self):
        """A window that is really on screen with motion on (the animated paths only run then)."""
        from distill.ui.motion import Motion

        win = self.make_window()
        self.addCleanup(win.close)
        self.addCleanup(setattr, Motion, "reduce", False)
        Motion.reduce = False
        win.show()
        return win

    @staticmethod
    def _finish(anim) -> None:
        anim.setCurrentTime(anim.duration())

    def test_switching_pages_dissolves_the_old_page_away(self):
        win = self._shown_window()
        target = next(k for k in win.pages if k != win.current_page)
        win._show_page(target)
        veil = win.stack._veil
        self.assertIsNotNone(veil)
        self.assertTrue(veil.isVisible())  # the old page is laid over the new one ...
        self.assertEqual(win.stack.currentWidget(), win.pages[target])  # ... which is already the live page
        self._finish(win.stack._veil_anim)
        self.assertFalse(veil.isVisible())  # and it is gone again when the fade ends

    def test_page_switch_is_a_plain_cut_when_motion_is_reduced(self):
        from distill.ui.motion import Motion

        win = self._shown_window()
        Motion.reduce = True
        win._show_page(next(k for k in win.pages if k != win.current_page))
        self.assertIsNone(win.stack._veil)

    def test_selected_nav_item_eases_in_and_out(self):
        win = self._shown_window()
        item = next(i for i in win.nav_items.values() if not i.selected)
        item.selected = True
        self.assertTrue(item.selected)
        self.assertLess(item._sel, 1.0)  # on its way, not there yet
        self._finish(item._sel_anim)
        self.assertEqual(item._sel, 1.0)
        item.selected = False
        self._finish(item._sel_anim)
        self.assertEqual(item._sel, 0.0)

    def test_fade_shows_and_hides_without_a_cut_and_is_instant_when_reduced(self):
        from PySide6.QtWidgets import QPushButton
        from distill.ui import motion

        win = self._shown_window()
        btn = QPushButton("x", win)
        btn.hide()
        motion.fade(btn, True)
        self.assertTrue(btn.isVisible())  # visible at once, fading in
        self.assertIsNotNone(btn.graphicsEffect())
        self._finish(btn._fade_anim)
        self.assertIsNone(btn.graphicsEffect())  # the effect is dropped when it is done
        motion.fade(btn, False)
        self.assertTrue(btn.isVisible())  # still there while it fades out
        self._finish(btn._fade_anim)
        self.assertFalse(btn.isVisibleTo(win))
        motion.Motion.reduce = True
        motion.fade(btn, True)
        self.assertTrue(btn.isVisibleTo(win))
        self.assertIsNone(btn.graphicsEffect())

    def test_progress_bar_glides_forward_but_resets_at_once(self):
        from PySide6.QtWidgets import QProgressBar
        from distill.ui import motion

        win = self._shown_window()
        bar = QProgressBar(win)
        bar.setRange(0, 1000)
        bar.show()
        motion.ease_value(bar, 600)
        self.assertLess(bar.value(), 600)  # on its way
        self._finish(bar._ease_anim)
        self.assertEqual(bar.value(), 600)
        motion.ease_value(bar, 0)  # a new run starts again from nothing
        self.assertEqual(bar.value(), 0)

    def test_banner_slides_open_and_shut(self):
        win = self._shown_window()
        win._toast("Saved.", "ok", 0)
        self.assertTrue(win.banner.isVisible())
        win.banner._dismiss()
        self.assertTrue(win.banner.isVisible())  # closing, not gone yet
        self._finish(win.banner._slide_anim)
        self.assertTrue(win.banner.isHidden())

    def test_soundtrack_setup_folds_away_as_soon_as_a_search_begins(self):
        win = self._shown_window()
        win._show_page("sound")
        self.assertTrue(win.st_games_card.header.isChecked())
        self.assertTrue(win.st_dest_card.header.isChecked())
        win.st_names.setPlainText("Persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()  # no result has come back: the fold is the sign that something is under way
        self.assertFalse(win.st_games_card.header.isChecked())
        self.assertFalse(win.st_dest_card.header.isChecked())

    def test_an_empty_soundtrack_search_folds_nothing(self):
        win = self._shown_window()
        win._show_page("sound")
        win.st_names.setPlainText("   ")
        win._st_search()
        self.assertTrue(win.st_games_card.header.isChecked())
        self.assertTrue(win.st_dest_card.header.isChecked())

    def test_a_second_soundtrack_search_folds_again_after_the_cards_were_reopened(self):
        win = self._shown_window()
        win._show_page("sound")
        win.st_names.setPlainText("Persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
            win.st_games_card.header.setChecked(True)  # the person reopens the list to change a name
            win._st_search()
        self.assertFalse(win.st_games_card.header.isChecked())

    def test_soundtrack_rows_ripple_in_and_then_all_show(self):
        win = self._shown_window()
        win._show_page("sound")
        win.st_names.setPlainText("Persona 5\nNier\nCeleste")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        reveal = win._st_reveal
        self.assertEqual(sorted(reveal._alpha), [0, 1, 2])  # all three start unseen, then appear in turn
        for anim in list(reveal._anims):
            anim.start()
            self._finish(anim)
        self.assertEqual(reveal._alpha, {})  # fully shown, nothing left to paint specially

    def test_checkboxes_bounce_when_ticked_and_settle_at_rest(self):
        from distill.ui.widgets import AnimCheckBox

        win = self.make_window()
        self.addCleanup(win.close)
        win.show()
        box = AnimCheckBox("x", win)
        box.show()
        box.setChecked(True)
        box._frame(0.3)  # mid-bounce: bigger than rest
        self.assertGreater(box._scale(), 1.0)
        box._frame(1.0)
        self.assertEqual(box._scale(), 1.0)
        self.assertEqual(box._t, 1.0)
        box.setChecked(False)
        box._frame(0.3)  # unticking shrinks first
        self.assertLess(box._scale(), 1.0)

    def test_tool_list_sits_in_a_rounded_well_with_no_square_fill(self):
        from PySide6.QtWidgets import QScrollArea, QWidget
        from distill.ui.theme import T

        win = self.make_window()
        self.addCleanup(win.close)
        well = win.findChild(QWidget, "navWell")
        self.assertIsNotNone(well)
        self.assertRegex(T.qss(), r"QWidget#navWell \{[^}]*border-radius: 1[0-9]px")
        scroll = well.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        self.assertFalse(scroll.widget().autoFillBackground())  # QScrollArea.setWidget turns this on; it paints a square
        self.assertFalse(scroll.viewport().autoFillBackground())

    def test_checkbox_has_room_for_its_bounce_and_focus_ring(self):
        from distill.ui.widgets import AnimCheckBox, AnimRadio

        win = self.make_window()
        self.addCleanup(win.close)
        bare, labelled, radio = AnimCheckBox("", win), AnimCheckBox("Tick me", win), AnimRadio("Pick me", win)
        bare._kind = 1  # measure the real bounce: the biggest the box ever gets while it is being ticked
        biggest = max((setattr(bare, "_u", u / 100) or bare._scale()) for u in range(100))
        overshoot = bare.BOX * (biggest - 1) / 2
        self.assertGreater(overshoot, 1.0)  # the bounce really does reach past the box
        self.assertGreaterEqual(bare.PAD, overshoot + 0.5)  # ... and the widget leaves room for it, border included
        self.assertEqual(bare.sizeHint().width(), bare.BOX + 2 * bare.PAD)  # equal room either side of a bare tick
        self.assertEqual(labelled.PAD, radio.PAD)  # ticks and radios line up when stacked
        self.assertGreater(labelled.sizeHint().width(), bare.sizeHint().width())

    def test_theme_tokens_drive_the_painted_controls(self):
        from distill.ui.theme import T

        qss = T.qss()
        for token in (T.disabled_fg, T.ctl_edge, T.ctl_on, T.btn[0], T.btn_hover[0], T.btn_down[0], T.btn_off[0]):
            self.assertIn(token.name(), qss)
        start, end = T.wash(70, 24)
        self.assertEqual((start.alpha(), end.alpha()), (70, 24))
        self.assertEqual(start.name(), T.accent.name())

    def test_track_rows_tick_and_the_game_box_follows(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile
        from PySide6.QtCore import Qt

        win = self.make_window()
        self.addCleanup(win.close)
        win.st_names.setPlainText("Persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        good = st.Album("archive.org", "p5", "Persona 5 Original Soundtrack", "", 1.0)
        win._st_on_found(0, st.GameResult(win._st_rows[0].result.request, [good]))
        files = [RemoteFile(f"Persona 5/soundtrack/0{i} - T.flac", f"https://x/{i}", 10) for i in (1, 2, 3)]
        win._st_on_tracks(0, good, files, "")
        row = win._st_rows[0]
        self.assertEqual(len(win._st_checked_files(row)), 3)
        row.item.setExpanded(True)
        win._st_attach_tracks(row.item)
        widget = win.st_tree.itemWidget(row.item.child(1), 0)
        widget.check.setChecked(False)  # untick one track
        self.assertEqual(row.item.checkState(0), Qt.CheckState.PartiallyChecked)
        self.assertEqual(len(win._st_checked_files(row)), 2)
        self.assertEqual(win._st_ticked(), [row])  # still counted: some of it is ticked
        row.item.setCheckState(0, Qt.CheckState.Unchecked)  # the game's box clears them all
        self.assertEqual(win._st_checked_files(row), [])

    def _game_with_tracks(self, win, count: int = 5):
        """A game with `count` found tracks, shown on the Soundtrack page of a window that is on screen."""
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win._show_page("sound")
        win.st_names.setPlainText("Persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        good = st.Album("archive.org", "p5", "Persona 5 Original Soundtrack", "", 1.0)
        win._st_on_found(0, st.GameResult(win._st_rows[0].result.request, [good]))
        files = [RemoteFile(f"Persona 5/soundtrack/{i:02d} - T.flac", f"https://x/{i}", 10) for i in range(1, count + 1)]
        win._st_on_tracks(0, good, files, "")
        return win._st_rows[0]

    def test_opening_a_game_writes_its_tracks_in_from_the_top(self):
        win = self._shown_window()
        row = self._game_with_tracks(win, 5)
        slide, tree = win._st_slide, win.st_tree
        row.item.setExpanded(True)  # the page makes the rows' widgets, then the slide starts
        fold = slide._folds.get(row.item)
        self.assertIsNotNone(fold)  # it animates: the rows are not just there
        first, last = row.item.child(0), row.item.child(4)
        # two steps in, the first row is part-way open while the last has not begun, and the row's widget agrees
        slide._apply(row.item, fold, slide.STEP * 2)
        self.assertGreater(first.sizeHint(0).height(), 0)
        self.assertEqual(last.sizeHint(0).height(), 0)
        first_widget = tree.itemWidget(first, 0)
        self.assertEqual(first_widget.sizeHint().height(), first.sizeHint(0).height())  # a tree sizes a row by the taller
        tree.doItemsLayout()
        self.assertLess(tree.visualItemRect(first).height(), win._st_slide.row_height)  # genuinely shorter, not just faded
        # later rows start later, and the whole thing ends with every row normal
        slide._apply(row.item, fold, slide.STEP * 4 + slide.ROW_MS)
        self.assertEqual(last.sizeHint(0).height(), slide.row_height)
        self._finish(fold.anim)
        self.assertNotIn(row.item, slide._folds)
        widget = tree.itemWidget(first, 0)
        self.assertIsNone(widget._fold_h)  # the rows are back to normal for next time
        self.assertIsNone(widget.graphicsEffect())

    def test_closing_a_game_folds_the_tracks_up_without_fading_first(self):
        win = self._shown_window()
        row = self._game_with_tracks(win, 5)
        slide, tree = win._st_slide, win.st_tree
        row.item.setExpanded(True)
        self._finish(slide._folds[row.item].anim)
        self.assertTrue(row.item.isExpanded())
        row.item.setExpanded(False)  # Qt closes it at once; the slide puts it back and folds it away
        fold = slide._folds.get(row.item)
        self.assertIsNotNone(fold)
        self.assertTrue(row.item.isExpanded())  # held open while the rows shrink
        child = row.item.child(2)
        slide._apply(row.item, fold, slide.COLLAPSE_MS * 0.5)
        height = child.sizeHint(0).height()
        self.assertTrue(0 < height < slide.row_height)  # half way: shorter, and still there
        self.assertEqual(tree.itemWidget(child, 0).sizeHint().height(), height)
        self._finish(fold.anim)
        self.assertFalse(row.item.isExpanded())  # only now really closed
        self.assertEqual(child.sizeHint(0).height(), slide.row_height)  # and its rows are back to normal for next time

    def test_tracks_open_and_close_instantly_when_motion_is_reduced(self):
        from distill.ui.motion import Motion

        win = self._shown_window()
        row = self._game_with_tracks(win, 3)
        Motion.reduce = True
        row.item.setExpanded(True)
        self.assertEqual(win._st_slide._folds, {})
        row.item.setExpanded(False)
        self.assertFalse(row.item.isExpanded())

    def test_a_long_album_only_animates_the_rows_in_view(self):
        from PySide6.QtWidgets import QApplication

        win = self._shown_window()
        row = self._game_with_tracks(win, 255)
        slide, tree = win._st_slide, win.st_tree
        row.item.setExpanded(True)
        fold = slide._folds.get(row.item)
        self.assertIsNotNone(fold)
        self.assertLess(fold.hi, 255)  # only the rows near the top are folded open ...
        self.assertFalse(row.item.child(0).isHidden())
        self.assertTrue(row.item.child(254).isHidden())  # ... the far ones wait out of the layout, so a frame stays cheap
        slide._apply(row.item, fold, slide.STEP * 2)
        self.assertEqual(row.item.child(254).sizeHint(0).height(), slide.row_height)  # nothing is done to those
        self.assertIsNotNone(tree.itemWidget(row.item.child(0), 0))  # the rows in view have their widgets at once
        for _ in range(200):  # and the rest follow in small batches
            QApplication.processEvents()
        self.assertTrue(all(tree.itemWidget(row.item.child(i), 0) is not None for i in range(255)))
        self._finish(fold.anim)
        self.assertNotIn(row.item, slide._folds)
        self.assertFalse(any(row.item.child(i).isHidden() for i in range(255)))  # every row is back when it is over

    def test_closing_a_long_album_folds_only_the_rows_in_view(self):
        win = self._shown_window()
        row = self._game_with_tracks(win, 255)
        slide = win._st_slide
        row.item.setExpanded(True)
        self._finish(slide._folds[row.item].anim)
        row.item.setExpanded(False)
        fold = slide._folds.get(row.item)
        self.assertIsNotNone(fold)
        self.assertTrue(row.item.isExpanded())  # held open while the rows in view fold away
        self.assertLess(fold.hi, 255)
        self.assertTrue(row.item.child(200).isHidden())
        self._finish(fold.anim)
        self.assertFalse(row.item.isExpanded())
        self.assertFalse(any(row.item.child(i).isHidden() for i in range(255)))  # ready for the next opening
        self.assertEqual(row.item.child(200).sizeHint(0).height(), slide.row_height)

    def _scan_results(self, win, key: str, games: int, details: int = 2) -> None:
        from distill.organize import Finding

        win._show_page(key)
        win._org_show(key, [Finding(f"Game {i}", "1", children=[(f"file {j}", "1", "x") for j in range(details)])
                            for i in range(games)], "", "")

    def test_scan_results_ripple_in_and_their_details_fold_open(self):
        win = self._shown_window()
        self._scan_results(win, "fix", 2)
        st = win.org["fix"]
        self.assertEqual(sorted(st.reveal._alpha), [0, 1])  # the results ripple in, as the soundtrack list does
        item = st.tree.topLevelItem(0)
        item.setExpanded(True)
        fold = st.slide._folds.get(item)
        self.assertIsNotNone(fold)  # opening a finding folds its details open, as opening a game does
        self.assertFalse(st.tree.uniformRowHeights())  # rows may differ in height while it folds
        st.slide._apply(item, fold, st.slide.STEP)
        first = item.child(0)
        self.assertTrue(0 < first.sizeHint(0).height() < st.slide.row_height)
        self.assertEqual(item.child(1).sizeHint(0).height(), 0)  # the next one has not begun
        self._finish(fold.anim)
        self.assertTrue(st.tree.uniformRowHeights())  # equal again once it is over
        self.assertFalse(first.sizeHint(0).isValid())  # back to the height the style gives it

    def test_closing_a_finding_folds_its_details_away(self):
        win = self._shown_window()
        self._scan_results(win, "fix", 1)
        st = win.org["fix"]
        item = st.tree.topLevelItem(0)
        item.setExpanded(True)
        self._finish(st.slide._folds[item].anim)
        item.setExpanded(False)
        fold = st.slide._folds.get(item)
        self.assertIsNotNone(fold)
        self.assertTrue(item.isExpanded())
        self._finish(fold.anim)
        self.assertFalse(item.isExpanded())

    def test_expand_all_only_animates_the_findings_in_view(self):
        win = self._shown_window()
        self._scan_results(win, "fix", 200)
        st = win.org["fix"]
        win._org_expand("fix", True)
        self.assertTrue(all(st.tree.topLevelItem(i).isExpanded() for i in range(200)))  # all of them open ...
        self.assertTrue(0 < len(st.slide._folds) < 200)  # ... but only those in view are folded open
        for fold in list(st.slide._folds.values()):
            self._finish(fold.anim)
        self.assertEqual(st.slide._folds, {})

    def test_patcher_results_open_at_once_and_ripple_in(self):
        from distill.organize import Finding

        win = self._shown_window()
        win._show_page("patch")
        st = win.org["patch"]
        st.findings = [Finding("Game", "0", children=[("inside.zip", "", "")])]
        win._patch_fill("patch", cascade=True)
        self.assertTrue(st.tree.topLevelItem(0).isExpanded())  # every game starts open ...
        self.assertEqual(st.slide._folds, {})  # ... without each one folding open
        self.assertEqual(sorted(st.reveal._alpha), [0])  # the rows ripple in instead
        for anim in list(st.reveal._anims):
            anim.start()
            self._finish(anim)
        self.assertEqual(st.reveal._alpha, {})
        win._patch_fill("patch")  # a redraw after a hand-made match does not replay it
        self.assertEqual(st.reveal._alpha, {})

    def test_downloader_list_ripples_in_and_archive_org_extras_are_left_out(self):
        from distill.downloads import RemoteFile

        win = self._shown_window()
        win._show_page("dl")
        files = [RemoteFile(f"f{i}.bin", f"https://x/{i}", 10) for i in range(5)]
        files[3].extra = files[4].extra = True  # archive.org extras (thumbnails, metadata) are never listed
        win._dl_populate(files, [])
        self.assertEqual(sorted(win._dl_reveal._alpha), [0, 1, 2])  # the three shown ripple in
        self.assertEqual(win._dl_visible(), [0, 1, 2])
        self.assertFalse(hasattr(win, "dl_extras"))
        self.assertIn("2 archive.org extras left out", win.dl_status.text())

    def test_linux_format_filter_ignores_events_after_its_table_is_deleted(self):
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtWidgets import QTableView
        import shiboken6
        from distill.ui.ytdlp_picker import FormatRows

        # Invoke the actual filter on a surviving Python wrapper, as Qt does during teardown.
        table = QTableView()
        rows = FormatRows.__new__(FormatRows)
        QObject.__init__(rows)
        rows.table = table
        shiboken6.delete(table)
        with mock.patch("distill.ui.ytdlp_picker.sys.platform", "linux"):
            self.assertFalse(rows.eventFilter(rows, QEvent(QEvent.Type.Resize)))

    def test_a_video_row_opens_to_a_format_and_quality_choice(self):
        from distill import ytdlp
        from distill.downloads import RemoteFile
        from distill.ui.ytdlp_picker import PANEL_H

        win = self._shown_window()
        win._show_page("dl")
        video = RemoteFile("Song [abc]", "ytdlp:https://y/watch?v=abc", None, "", None, False, True, "yt-dlp")
        plain = RemoteFile("f.bin", "https://x/f.bin", 10)
        with mock.patch.object(ytdlp, "find_exe", return_value=None):  # nothing to ask which qualities exist
            win._dl_populate([video, plain], [])
            rows = win.dl_formats
            base = win.dl_table.verticalHeader().defaultSectionSize()
            rows.toggle([1])  # an ordinary file has nothing to choose
            self.assertEqual(rows._open, {})
            rows.toggle([0])
            entry = rows._open[id(video)]
            self._finish(entry.anim)
            self.assertEqual(win.dl_table.rowHeight(0), base + PANEL_H)  # the row really is taller
            self.assertTrue(entry.panel.isVisibleTo(win.dl_table.viewport()))
            panel = entry.panel
            self.assertEqual(panel.spec(), "mp4:best")  # starts from the Settings quality
            panel.kind.setCurrentIndex(panel.kind.findData("mp3"))  # MP3 audio: bit rates instead of heights
            self.assertEqual(video.fmt, "mp3:320")
            self.assertEqual([panel.quality.itemText(i) for i in range(panel.quality.count())][:2], ["320 kbps", "256 kbps"])
            panel.quality.setCurrentIndex(panel.quality.findData(128))
            self.assertEqual(video.fmt, "mp3:128")
            panel.kind.setCurrentIndex(panel.kind.findData("mp4"))  # back to video: heights again, best first
            self.assertEqual(video.fmt, "mp4:best")
            panel.quality.setCurrentIndex(panel.quality.findData(720))
            self.assertEqual(video.fmt, "mp4:720")
            rows.toggle([0])  # closing folds the row back up and drops the panel
            self._finish(entry.anim)
            self.assertEqual(rows._open, {})
            self.assertEqual(win.dl_table.rowHeight(0), base)
            self.assertEqual(video.fmt, "mp4:720")  # the choice stays on the row

    def test_apply_to_all_sets_every_video_row_but_not_other_files(self):
        from distill import ytdlp
        from distill.downloads import RemoteFile

        win = self._shown_window()
        win._show_page("dl")
        a = RemoteFile("A [1]", "ytdlp:https://y/1", None, "", None, False, True, "yt-dlp")
        b = RemoteFile("B [2]", "ytdlp:https://y/2", None, "", None, False, True, "yt-dlp")
        plain = RemoteFile("f.bin", "https://x/f.bin", 10)
        with mock.patch.object(ytdlp, "find_exe", return_value=None):
            win._dl_populate([a, plain, b], [])
            win.dl_formats.toggle([0])
            panel = win.dl_formats._open[id(a)].panel
            panel.kind.setCurrentIndex(panel.kind.findData("mp3"))
            panel.apply_all.click()
        self.assertEqual((a.fmt, b.fmt, plain.fmt), ("mp3:320", "mp3:320", ""))

    def test_sound_page_start_button_saves_the_ticked_soundtracks(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("sound")
        self.assertEqual(win.start_btn.text(), "Save Selected Soundtracks")
        self.assertFalse(hasattr(win, "st_download_btn"))

    def test_quick_search_filters_tools_and_their_groups(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win.nav_search.setText("torrent")
        self.assertFalse(win.nav_items["dl"].isHidden())
        self.assertTrue(win.nav_items["chd"].isHidden())
        self.assertTrue(win.nav_groups["ORGANISE LIBRARY"]["header"].isHidden())
        win.nav_search.setText("")
        self.assertFalse(win.nav_items["chd"].isHidden())
        self.assertFalse(win.nav_groups["ORGANISE LIBRARY"]["header"].isHidden())

    def test_page_header_shows_the_designed_title_and_chip(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("cso")
        self.assertEqual(win.page_title.text(), "CSO Compression (PSP)")
        self.assertEqual(win.page_chip.text(), "PSP")
        self.assertIn("maxcso", win.page_blurb.toolTip())  # the app's own description stays one hover away

    def test_card_titles_pick_their_icons_and_inter_loads(self):
        from distill.ui import icons
        from distill.ui.assets import load_fonts

        self.assertEqual(icons.icon_for_title("Folders"), "folder-open")
        self.assertIsNone(icons.icon_for_title("Logins"))
        self.assertFalse(icons.pixmap("search", "#ffffff", 16).isNull())
        self.assertEqual(load_fonts(), ("Inter 18pt", "Outfit", "JetBrains Mono"))

    def test_seek_bar_wave_ripples_while_playing_and_settles_flat_when_paused(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win.show()
        win.now_playing.show()  # playback normally reveals the sidebar player
        slider = win.now_playing._slider
        self.assertTrue(slider.isVisible())
        slider.setRange(0, 1000)
        slider.setValue(400)
        slider.set_moving(True)
        self.assertTrue(slider._timer.isActive())
        for _ in range(60):
            slider._tick()
        self.assertGreater(slider._amp, 0.9)
        slider.set_moving(False)  # paused: eases flat, then stops redrawing
        for _ in range(120):
            slider._tick()
        self.assertEqual(slider._amp, 0.0)
        self.assertFalse(slider._timer.isActive())
        self.assertAlmostEqual(slider._fraction(), 0.4)

    def test_stage_switch_works_like_a_combo_and_shows_the_stage_description(self):
        win = self.make_window()
        self.addCleanup(win.close)
        sw = win.chd_compression
        self.assertEqual(sw.count(), 4)
        self.assertEqual(sw.currentData(), "default")
        seen = []
        sw.currentTextChanged.connect(seen.append)
        sw.setCurrentText("Fastest")
        self.assertEqual((sw.currentIndex(), sw.currentData(), seen), (2, "fastest", ["Fastest"]))
        self.assertIn("Quickest", sw.description())
        sw.setCurrentIndex(9)  # out of range: ignored
        self.assertEqual(sw.currentIndex(), 2)
        self.assertEqual(win.io_mode.findData("ssd"), 2)

    def test_numeric_stage_switches_stand_in_for_spin_boxes(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual(win.thread_budget.value(), win.sys_threads)  # starts on "All"
        win.thread_budget.setValue(1)
        self.assertEqual(win.thread_budget.value(), min(c for c in (win.thread_budget.currentData(),)))
        seen = []
        win.dl_threads.valueChanged.connect(seen.append)
        win.dl_threads.setValue(6)  # snaps to the nearest stop (4, the lower on a tie)
        self.assertIn(win.dl_threads.value(), (4, 8))
        self.assertEqual(win.dl_threads.value(), win.dl_threads.currentData())

    def test_step_switch_steps_with_its_chevrons_and_keeps_stage_switch_behaviour(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from distill.ui.widgets import StageSwitch, StepSwitch

        sw = StepSwitch()
        self.assertIsInstance(sw, StageSwitch)  # so it is saved and restored like any other switch
        for n in (1, 2, 3):
            sw.addItem(f"{n} conn", n, f"{n} at once")
        seen = []
        sw.valueChanged.connect(seen.append)
        track, mid = sw._track, sw._track.height() // 2
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=QPoint(track.width() - 5, mid))  # right chevron
        self.assertEqual((sw.value(), seen), (2, [2]))
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=QPoint(track.width() // 2, mid))  # the value: no change
        self.assertEqual(sw.value(), 2)
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=QPoint(5, mid))  # left chevron
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=QPoint(5, mid))  # already at the first value: stays
        self.assertEqual(sw.value(), 1)
        self.assertEqual(sw.description(), "1 at once")

    def test_download_speed_and_connections_use_steppers(self):
        from distill.ui.widgets import StepSwitch

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIsInstance(win.rules_panel.overall, StepSwitch)
        self.assertIsInstance(win.rules_panel.segments, StepSwitch)

    def test_settings_pages_share_one_width_and_fit_the_window(self):
        win = self.make_window()
        self.addCleanup(win.close)
        dlg = win.settings_dlg
        dlg.resize(dlg.minimumSize())  # the narrowest the window can be: the hardest case to fit
        dlg.show()
        self.addCleanup(dlg.hide)
        widths = set()
        for index in range(win.settings_stack.count()):
            win.settings_list.setCurrentRow(index)
            for _ in range(3):  # a scrollbar appearing changes the width once more
                self.app.processEvents()
            scroll = win.settings_stack.widget(index)
            self.assertEqual(scroll.widget().width(), scroll.viewport().width(), f"page {index} runs off the window")
            # A visible scrollbar consumes content space; compare the content
            # width plus its actual scrollbar geometry, not the stack's width.
            bar = scroll.verticalScrollBar()
            widths.add(scroll.widget().width() + (bar.width() if bar.isVisible() else 0))
        self.assertEqual(len(widths), 1, f"settings pages differ in width: {sorted(widths)}")

    def test_romm_switch_changes_mode_and_keeps_the_description(self):
        from distill import soundtracks as st

        win = self.make_window()
        self.addCleanup(win.close)
        with mock.patch.object(st, "save_prefs"):
            win.st_mode.setChecked(False)
            self.assertEqual(win._st_mode(), st.SAVE_STANDARD)
            self.assertIn("Off:", win.st_mode_hint.text())
            self.assertTrue(win.st_dest.isEnabled())
            win.st_mode.setChecked(True)
            self.assertEqual(win._st_mode(), st.SAVE_ROMM)
            self.assertIn("RomM:", win.st_mode_hint.text())
            self.assertFalse(win.st_dest.isEnabled())

    def test_removing_a_game_hides_it_and_unticks_it(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        win.st_names.setPlainText("Persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        good = st.Album("archive.org", "p5", "Persona 5 Original Soundtrack", "", 1.0)
        win._st_on_found(0, st.GameResult(win._st_rows[0].result.request, [good]))
        win._st_on_tracks(0, good, [RemoteFile("Persona 5/soundtrack/01 - A.flac", "https://x/1", 10)], "")
        self.assertEqual(len(win._st_ticked()), 1)
        win._st_remove_games([win._st_rows[0]])
        self.assertEqual(win._st_ticked(), [])
        self.assertTrue(win.st_tree.topLevelItem(0).isHidden())

    def test_multi_disc_soundtrack_gets_a_header_per_disc_not_a_prefix_on_every_track(self):
        from PySide6.QtCore import Qt
        from distill import soundtracks as st
        from distill.downloads import RemoteFile
        from distill.ui import soundtrack_page

        win = self.make_window()
        self.addCleanup(win.close)
        win.st_names.setPlainText("Super Mario Odyssey")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        album = st.Album("archive.org", "smo", "Super Mario Odyssey OST", "", 1.0)
        win._st_on_found(0, st.GameResult(win._st_rows[0].result.request, [album]))
        files = [RemoteFile(f"Super Mario Odyssey/soundtrack/Disc {d}/0{n} - T{d}.{n}.flac", f"https://x/{d}{n}", 10)
                 for d in (1, 2) for n in (1, 2)]
        win._st_on_tracks(0, album, files, "")
        top = win.st_tree.topLevelItem(0)
        kinds = [bool(top.child(i).data(0, soundtrack_page.HEADER_ROLE)) for i in range(top.childCount())]
        self.assertEqual(kinds, [True, False, False, True, False, False])  # header, 2 tracks, header, 2 tracks
        self.assertEqual([top.child(i).text(0) for i in (0, 1, 3)], ["Disc 1", "01 - T1.1.flac", "Disc 2"])
        self.assertEqual(top.child(0).text(2), "2 tracks, 20 B")
        self.assertFalse(bool(top.child(0).flags() & Qt.ItemFlag.ItemIsSelectable))
        self.assertEqual(top.text(2), "4 tracks, 40 B")  # headers are not tracks
        self.assertEqual([f.url for f in win._st_checked_files(win._st_rows[0])], [f.url for f in files])
        win._st_track_ticked(top.child(1), False)  # unticking one of four tracks: partly ticked, headers not counted
        self.assertEqual(top.checkState(0), Qt.CheckState.PartiallyChecked)
        win._st_remove_tracks([(win._st_rows[0], [top.child(4), top.child(5)])])  # every track of disc 2 removed
        self.assertEqual([top.child(i).text(0) for i in range(top.childCount())], ["01 - T1.1.flac", "02 - T1.2.flac"])

    def _win_with_three_tracks(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile
        from distill.ui import soundtrack_page

        if not soundtrack_page.AUDIO_OK:
            self.skipTest("QtMultimedia is not installed")
        win = self.make_window()
        self.addCleanup(win.close)
        win.st_names.setPlainText("Persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        album = st.Album("archive.org", "p5", "Persona 5 Original Soundtrack", "", 1.0)
        win._st_on_found(0, st.GameResult(win._st_rows[0].result.request, [album]))
        files = [RemoteFile(f"Persona 5/soundtrack/0{n} - T{n}.flac", f"https://x/{n}", 10) for n in (1, 2, 3)]
        win._st_on_tracks(0, album, files, "")
        win._st_player = mock.MagicMock()
        win._st_player.position.return_value = 0
        return win, album, files

    def test_playing_a_track_previews_it_and_playing_again_stops(self):
        from distill.ui import soundtrack_page

        win, _album, _files = self._win_with_three_tracks()
        top, player = win.st_tree.topLevelItem(0), win._st_player
        self.assertEqual(top.child(0).data(0, soundtrack_page.URL_ROLE), "https://x/1")
        win._st_play_child(top)  # a game row is not a track: nothing plays
        player.play.assert_not_called()
        win._st_play_child(top.child(1))
        self.assertEqual(player.setSource.call_args[0][0].toString(), "https://x/2")
        self.assertEqual((win._st_index, len(win._st_queue)), (1, 3))  # the whole album is the queue
        self.assertTrue(win.now_playing.isVisibleTo(win))
        win._st_play_child(top.child(1))  # the same track again stops
        self.assertEqual(win._st_index, -1)
        self.assertFalse(win.now_playing.isVisibleTo(win))

    def test_sidebar_player_steps_through_the_album(self):
        win, _album, _files = self._win_with_three_tracks()
        top, player = win.st_tree.topLevelItem(0), win._st_player
        win._st_play_child(top.child(0))
        win.now_playing.skipForward.emit()
        self.assertEqual(win._st_index, 1)
        self.assertEqual(player.setSource.call_args[0][0].toString(), "https://x/2")
        player.position.return_value = 10_000  # well into the track: previous restarts it
        win.now_playing.skipBack.emit()
        player.setPosition.assert_called_with(0)
        self.assertEqual(win._st_index, 1)
        player.position.return_value = 500  # near the start: previous goes to the track before
        win.now_playing.skipBack.emit()
        self.assertEqual(win._st_index, 0)
        win._st_step(1)
        win._st_step(1)
        win._st_step(1)  # off the end of the album: stops and hides
        self.assertEqual(win._st_index, -1)
        self.assertFalse(win.now_playing.isVisibleTo(win))

    def test_sidebar_player_pauses_seeks_and_stops(self):
        from PySide6.QtMultimedia import QMediaPlayer

        win, _album, _files = self._win_with_three_tracks()
        player = win._st_player
        win._st_play_child(win.st_tree.topLevelItem(0).child(0))
        player.playbackState.return_value = QMediaPlayer.PlaybackState.PlayingState
        win.now_playing.togglePlay.emit()
        player.pause.assert_called_once()
        player.playbackState.return_value = QMediaPlayer.PlaybackState.PausedState
        win.now_playing.togglePlay.emit()
        self.assertEqual(player.play.call_count, 2)  # once to start, once to resume
        win.now_playing.seekTo.emit(42_000)
        player.setPosition.assert_called_with(42_000)
        win.now_playing.stopped.emit()
        self.assertEqual(win._st_index, -1)

    def test_playback_carries_on_when_the_tracklist_is_rebuilt_or_searched_again(self):
        win, album, files = self._win_with_three_tracks()
        win._st_play_child(win.st_tree.topLevelItem(0).child(0))
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()  # clears the tree, but the queue is plain data
        self.assertEqual(win._st_index, 0)
        win._st_on_found(0, __import__("distill.soundtracks", fromlist=["x"]).GameResult(win._st_rows[0].result.request, [album]))
        win._st_on_tracks(0, album, files, "")
        win._show_page("sound")
        win.show()
        top = win.st_tree.topLevelItem(0)
        win.st_tree.expandItem(top)  # itemExpanded attaches and synchronises the lazy track rows
        self.app.processEvents()
        child = top.child(0)
        row = win.st_tree.itemWidget(child, 0)
        self.assertIsNotNone(row)
        self.assertTrue(row._current)
        row.play._anim.setCurrentTime(row.play._anim.duration())
        self.assertTrue(row.play.playing)  # the embedded play button now carries this state
        self.assertEqual(win._st_url_playing(), "https://x/1")

    def test_cover_art_is_fetched_cached_and_shown_on_the_card(self):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QPixmap
        from distill.ui import soundtrack_page

        win, album, files = self._win_with_three_tracks()
        album.art = "https://archive.org/services/img/p5"
        started = []
        with mock.patch("threading.Thread.start", lambda self: started.append(self._args)):
            win._st_on_tracks(0, album, files, "")  # rebuilding also requests the album cover
            top = win.st_tree.topLevelItem(0)
            self.assertEqual(top.child(0).data(0, soundtrack_page.ART_ROLE), album.art)
            win._st_play_child(top.child(0))
            win._st_play_child(top.child(0))  # stops
            win._st_play_child(top.child(1))  # same album again: still one fetch in flight
        self.assertEqual(started, [(album.art,)])
        self.assertTrue(win.now_playing._art.isVisibleTo(win.now_playing))  # the placeholder while it loads
        pix = QPixmap(40, 20)
        pix.fill(QColor("red"))
        data, buf = QByteArray(), QBuffer()
        buf.setBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.save(buf, "PNG")
        win._st_on_art(album.art, bytes(data))
        self.assertIn(album.art, win._st_art_cache)
        self.assertFalse(win.now_playing._art.pixmap().isNull())
        with mock.patch("threading.Thread.start", lambda self: started.append("again")):
            win._st_play_child(top.child(2))  # cached: shown at once, nothing fetched
        self.assertEqual(len(started), 1)
        self.assertFalse(win.now_playing._art.pixmap().isNull())

    def test_failed_cover_fetch_hides_the_placeholder_and_tracks_without_art_show_none(self):
        win, album, files = self._win_with_three_tracks()
        album.art = "https://archive.org/services/img/gone"
        with mock.patch("threading.Thread.start", lambda self: None):
            # Rebuilding the album requests its cover before playback begins.
            win._st_on_tracks(0, album, files, "")
            top = win.st_tree.topLevelItem(0)
            win._st_play_child(top.child(0))
        win._st_on_art(album.art, b"")  # the fetch came back empty
        self.assertFalse(win.now_playing._art.isVisibleTo(win.now_playing))
        album.art = ""
        win._st_on_tracks(0, album, files, "")
        win._st_play_child(win.st_tree.topLevelItem(0).child(1))
        self.assertFalse(win.now_playing._art.isVisibleTo(win.now_playing))

    def test_now_playing_widget_reports_clicks_and_shows_state(self):
        from distill.ui.now_playing import clock, NowPlaying

        np = NowPlaying()
        self.addCleanup(np.deleteLater)
        got = []
        for name in ("skipBack", "togglePlay", "skipForward", "stopped"):
            getattr(np, name).connect(lambda n=name: got.append(n))
        np.show_track("01 - Opening.flac", "Persona 5")
        np.set_duration(185_000)
        np.set_position(65_000)
        self.assertEqual(np._time.text(), "1:05 / 3:05")
        np.set_can_skip(False, False)
        self.assertFalse(np._forward.isEnabled())
        for b in (np._back, np._play, np._forward, np._stop):
            b.click() if b.isEnabled() else None
        self.assertEqual(got, ["skipBack", "togglePlay", "stopped"])
        self.assertEqual(clock(59_999), "0:59")

    def test_save_folder_is_shared_between_files_and_soundtracks_pages(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win.dl_dest.setText("D:/Games")
        self.assertEqual(win.st_dest.text(), "D:/Games")
        win.st_dest.setText("E:/Roms")
        self.assertEqual(win.dl_dest.text(), "E:/Roms")

    def test_soundtracks_page_runs_on_the_download_panel(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("sound")
        self.assertEqual(win._current_mode(), "dl")
        self.assertEqual(win.start_btn.text(), "Save Selected Soundtracks")

    def test_fixed_height_text_boxes_do_not_make_their_cards_grow(self):
        """A fixed-height text box that still has an expanding size policy makes its card claim the page's spare
        height (a huge empty top card), so both download pages' text boxes must be vertically fixed."""
        from PySide6.QtWidgets import QSizePolicy

        win = self.make_window()
        self.addCleanup(win.close)
        for box in (win.dl_url_text, win.st_names):
            self.assertEqual(box.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Fixed)

    def test_organise_pages_are_grouped_into_steps(self):
        from distill.organize import Finding

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual(list(win.org["fix"].step_boxes), ["tidy", "multi", "playlists", "discfix"])
        self.assertEqual(list(win.org["extras"].step_boxes), ["cleanup", "dupes", "onegr"])
        self.assertEqual(list(win.org["fix"].step_cards), list(win.org["fix"].step_boxes))  # one card per step
        self.assertEqual(win.org["bysystem"].step_boxes, {})
        self.assertIn("permanent", win.org["extras"].opts)  # shared by both steps, shown once
        win._org_show("fix", [Finding("A")], "", "", "multi")
        self.assertEqual(win.org["fix"].tool.key, "multi")
        self.assertIn("Step 2 of 4", win.org["fix"].steps_label.text())
        self.assertEqual([k for k, c in win.org["fix"].step_cards.items() if c._current], ["multi"])  # its card lights up
        win.org["fix"].step_boxes["multi"].setChecked(False)  # a switched-off step greys out its card's content
        self.assertFalse(win.org["fix"].step_cards["multi"].content.isEnabled())

    def test_torbox_marker_is_a_chip_only_when_a_key_is_saved(self):
        from distill.downloads import LOGINS

        win = self.make_window()
        self.addCleanup(win.close)
        with mock.patch.dict(LOGINS, {"torbox": {"apikey": ""}}):
            win._refresh_markers()
            self.assertEqual(win.markers._clickable, frozenset())
        with mock.patch.dict(LOGINS, {"torbox": {"apikey": "key"}}):
            win._refresh_markers()
            self.assertEqual(win.markers._clickable, frozenset({"TorBox"}))
            self.assertIn("TorBox", [name for name, _s, _t in win.markers._items])

    def test_every_column_of_the_cores_list_can_be_dragged(self):
        from PySide6.QtWidgets import QHeaderView

        win = self.make_window()
        self.addCleanup(win.close)
        head = win.emd_cores_tree.header()
        for col in range(win.emd_cores_tree.columnCount()):  # Qt can't drag the edge of a stretching column
            self.assertEqual(head.sectionResizeMode(col), QHeaderView.ResizeMode.Interactive, col)

    def test_start_button_says_what_is_running(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")
        self.assertEqual(win.start_btn.text(), "Start CHD Compression")
        win.is_processing = True
        win._begin_run_label("chd")
        self.assertEqual(win.start_btn.text(), "Compressing...")
        win._show_page("dl")  # moving to another page doesn't change what the running job's button says
        self.assertEqual(win.start_btn.text(), "Compressing...")
        win.is_processing = False
        win._update_start_text()
        self.assertEqual(win.start_btn.text(), "Start Download")
        win.is_processing = True
        win._begin_run_label("dl")
        self.assertEqual(win.start_btn.text(), "Downloading...")
        win.is_processing = False
        win._show_page("fix")
        win._org_set_busy(True, "Scanning...")
        self.assertEqual(win.start_btn.text(), "Scanning...")
        win._org_set_busy(False)
        self.assertEqual(win.start_btn.text(), win.org["fix"].tool.apply_label)

    def test_run_buttons_start_in_the_right_state(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertFalse(win.start_btn.isEnabled())  # waiting for the stubbed tool checks
        self.assertFalse(win.pause_btn.isEnabled())
        self.assertFalse(win.stop_btn.isEnabled())
        for key in tuple(win._pending_tools):
            win._tool_ready(key)
        self.assertTrue(win.start_btn.isEnabled())

    def test_converter_pages_are_compact_and_list_pages_are_not(self):
        win = self.make_window()
        self.addCleanup(win.close)
        for mode in win.MODES:
            with self.subTest(page=mode):
                self.assertTrue(win.pages[mode].property("compact"))
        self.assertFalse(win.pages["dl"].property("compact"))  # lists fill the space instead

    def test_expander_puts_its_content_in_the_foldable_body(self):
        from PySide6.QtWidgets import QLabel

        from distill.ui.widgets import Expander

        card, label = Expander("Options", expanded=True), QLabel("inside")
        card.add(label)
        self.assertIs(label.parent(), card.body)
        card.header.setChecked(False)
        self.assertTrue(card.body.isHidden())
        card.header.setChecked(True)
        self.assertFalse(card.body.isHidden())

    def test_linked_expanders_fold_and_unfold_together(self):
        from distill.ui.widgets import Expander, link_expanders

        left, right = Expander("Links", expanded=True), Expander("Save", expanded=True)
        link_expanders(left, right)
        left.header.setChecked(False)
        self.assertFalse(right.header.isChecked())
        right.header.setChecked(True)
        self.assertTrue(left.header.isChecked())

    def test_the_soundtrack_cards_fold_together(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win.st_dest_card.header.setChecked(False)
        self.assertFalse(win.st_games_card.header.isChecked())
        win.st_games_card.header.setChecked(True)
        self.assertTrue(win.st_dest_card.header.isChecked())

    def test_the_drop_badge_paints_with_and_without_motion(self):
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QWidget

        from distill.ui import motion
        from distill.ui.dropglow import DropGlow

        host = QWidget()
        host.resize(600, 400)
        glow = DropGlow(host)
        glow.resize(600, 400)
        glow._fade = 1.0
        glow._back = QPixmap(600, 400)
        glow._back.fill()
        self.addCleanup(setattr, motion.Motion, "reduce", motion.Motion.reduce)
        for reduce in (False, True):
            motion.Motion.reduce = reduce
            shot = QPixmap(600, 400)
            shot.fill()
            painter = QPainter(shot)
            glow._paint_badge(painter, glow.rect().center())
            painter.end()
            self.assertTrue(any(shot.toImage().pixelColor(x, 200).rgb() != 0xFFFFFFFF for x in range(250, 350)))  # the tile is there

    def test_recomp_cover_flight_helpers_are_safe_with_nothing_open(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIsNone(win._rc_poster_box("no-such-game"))
        self.assertEqual(win._rc_hold_id, "")
        win._rc_cancel_flight()  # nothing in the air: a no-op
        win._rc_back()  # already on the grid: nothing to fly back from
        self.assertEqual(win._rc_hold_id, "")

    def test_stack_pins_compact_pages_to_their_content(self):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from distill.ui.widgets import SizedStack

        def page(compact: bool) -> QWidget:
            w = QWidget()
            QVBoxLayout(w).addWidget(QLabel("row"))
            w.setProperty("compact", compact)
            return w

        stack = SizedStack()
        tight, loose = page(True), page(False)
        stack.addWidget(tight)
        stack.addWidget(loose)
        stack.setCurrentWidget(tight)
        self.assertEqual(stack.minimumHeight(), stack.maximumHeight())
        self.assertLess(stack.maximumHeight(), SizedStack._MAX)
        stack.setCurrentWidget(loose)
        self.assertEqual((stack.minimumHeight(), stack.maximumHeight()), (0, SizedStack._MAX))

    def test_themes_compute_without_a_display(self):
        from distill.ui.theme import T

        T.refresh()
        self.assertTrue(T.accent.isValid())

    def test_icon_is_a_real_image(self):
        from distill.ui.icon import app_icon_pixmap

        pix = app_icon_pixmap()
        self.assertFalse(pix.isNull())
        self.assertGreaterEqual(pix.width(), 128)

    def test_torrent_search_dialog_lists_sorts_and_adds(self):
        from distill import torrentsearch as ts
        from distill.ui.torrent_search import ResultItem, TorrentSearchDialog

        dlg = TorrentSearchDialog()
        self.addCleanup(dlg.close)
        dlg.show_results([ts.SearchResult("Small swarm", "a" * 40, 10, 5, 1, sources=["Nyaa"]),
                          ts.SearchResult("Big swarm", "b" * 40, 2048, 900, 40, sources=["Torrents-CSV"]),
                          ts.SearchResult("Dead", "c" * 40, 1, 0, 0, sources=["Nyaa"])], ["Prowlarr: down"])
        names = [dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())]
        self.assertEqual(names[:2], ["Big swarm", "Small swarm"])  # most seeders first, 900 above 5 as numbers
        dead = next(dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())
                    if dlg.tree.topLevelItem(i).text(0) == "Dead")
        self.assertTrue(dead.isHidden())  # no seeders: hidden until the box is cleared
        dlg.hide_dead.setChecked(False)
        self.assertFalse(dead.isHidden())
        self.assertIn("Prowlarr: down", dlg.status.text())
        got: list = []
        dlg.addLinks.connect(got.extend)
        dlg._added(["magnet:?xt=urn:btih:" + "b" * 40], [])
        self.assertEqual(got, ["magnet:?xt=urn:btih:" + "b" * 40])
        self.assertIsInstance(dlg.tree.topLevelItem(0), ResultItem)

    def test_downloads_page_has_a_search_button_that_opens_the_dialog(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertTrue(win.dl_search_btn.isEnabled())
        win._dl_search_torrents()
        self.assertTrue(win._torrent_search.isVisible())
        win._torrent_search.close()

    def test_album_picker_popup_has_no_square_frame_behind_its_rounded_list(self):
        from PySide6.QtCore import Qt
        from distill.ui.soundtrack_page import AlbumCombo

        combo = AlbumCombo()
        self.addCleanup(combo.deleteLater)
        popup = combo.view().window()
        self.assertTrue(popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(popup.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(popup.objectName(), "comboPopup")
        self.assertIn("border: none", popup.styleSheet())  # the style's own square outline is switched off too

    def test_every_combo_gets_the_rounded_popup_once_the_hook_is_installed(self):
        from PySide6.QtWidgets import QApplication, QComboBox
        from distill.ui.popup import install_soft_popups

        install_soft_popups(QApplication.instance())
        install_soft_popups(QApplication.instance())  # a second call is ignored
        combo = QComboBox()
        self.addCleanup(combo.deleteLater)
        combo.addItems(["a", "b"])
        combo.ensurePolished()  # what Qt does just before a widget is first shown
        self.assertEqual(combo.view().window().objectName(), "comboPopup")

    def test_game_rows_are_tall_enough_for_the_album_picker(self):
        from distill.ui.soundtrack_page import GAME_ROW_H
        from distill.ui.theme import T

        self.assertGreaterEqual(GAME_ROW_H, 48)  # picker = text + 16px padding + border, inside 10px of item padding
        self.assertNotIn("inlineCombo", T.qss())  # an override rule on the picker made it square and tiny: don't bring it back

    def test_icon_follows_the_look(self):
        from distill.ui.icon import app_icon_pixmap
        from distill.ui.theme import T

        was = T.dark
        try:
            T.dark = True
            dark = app_icon_pixmap(128).toImage()
            T.dark = False
            bright = app_icon_pixmap(128).toImage()
        finally:
            T.dark = was
        self.assertNotEqual(dark, bright)  # the light look gets the bright colourway
        self.assertEqual(bright, app_icon_pixmap(128, dark=False).toImage())


    def test_update_screen_fills_cancels_and_cannot_be_cancelled_once_installing(self):
        from distill.ui.update_screen import UpdateScreen

        screen = UpdateScreen(None, "Distill v9.9")
        self.addCleanup(screen.deleteLater)
        asked = []
        screen.cancelled.connect(lambda: asked.append(1))
        screen.set_progress(12 << 20, 48 << 20)  # a quarter of the download is 70% of that: 17% of the logo
        self.assertEqual(screen.percent, "17%")
        self.assertIn("12.0 of 48.0 MB", screen.status)
        screen.set_progress(48 << 20, 48 << 20)
        screen.set_installing()  # the download is its share of the fill; the install window does the rest
        self.assertEqual((screen.percent, screen.can_cancel()), ("70%", False))
        screen.set_progress(1, 48 << 20)  # a late chunk must not drag the screen back
        self.assertEqual(screen.percent, "70%")
        screen.set_install_progress(1.0)
        self.assertEqual(screen.percent, "100%")
        screen.reject()
        self.assertEqual(asked, [])  # too late to cancel
        screen.dismiss()
        self.assertEqual(asked, [])  # and a dismissal is not a cancel

    def test_update_screen_logo_is_grey_then_fills_and_cancel_is_reported(self):
        from PySide6.QtGui import QColor

        from distill.ui import motion
        from distill.ui.update_screen import FillLogo, UpdateScreen, greyscale
        from distill.ui.icon import app_icon_pixmap

        grey = greyscale(app_icon_pixmap(128)).toImage()
        for x, y in ((20, 20), (64, 64), (100, 100)):  # every opaque pixel has no colour of its own
            c = QColor(grey.pixel(x, y))
            self.assertEqual((c.red(), c.green()), (c.green(), c.blue()))
        self.assertEqual(grey.pixelColor(0, 0).alpha(), 0)  # the rounded corners stay transparent
        was = motion.Motion.reduce
        motion.Motion.reduce = True  # no easing: the fraction is set at once
        self.addCleanup(setattr, motion.Motion, "reduce", was)
        logo = FillLogo(120)
        self.addCleanup(logo.deleteLater)
        logo.set_fraction(0.4)
        self.assertEqual(logo.fraction(), 0.4)
        logo.set_fraction(3)
        self.assertEqual(logo.fraction(), 1.0)
        screen = UpdateScreen(None, "Distill v9.9")
        self.addCleanup(screen.deleteLater)
        asked = []
        screen.cancelled.connect(lambda: asked.append(1))
        screen.reject()
        self.assertEqual(asked, [1])


    def test_update_logo_is_grey_above_the_fill_and_coloured_below_it(self):
        from distill.ui import motion
        from distill.ui.update_screen import FillLogo

        was = motion.Motion.reduce
        motion.Motion.reduce = True  # no easing, no ripple: the picture is exactly the fraction
        self.addCleanup(setattr, motion.Motion, "reduce", was)
        logo = FillLogo(240)
        self.addCleanup(logo.deleteLater)
        logo.set_fraction(0.5)
        img = logo.grab().toImage()
        scale = img.width() / 240
        above = img.pixelColor(int(40 * scale), int(60 * scale))  # inside the rounded square, well above the halfway edge
        below = img.pixelColor(int(40 * scale), int(190 * scale))  # and well below it
        self.assertEqual((above.red(), above.green()), (above.green(), above.blue()))  # grey
        self.assertNotEqual((below.red(), below.green()), (below.green(), below.blue()))  # colour


    def test_softened_picture_is_grey_and_a_little_blurred(self):
        from PySide6.QtGui import QColor, QPainter, QPixmap

        from distill.ui.veil import softened

        pix = QPixmap(60, 40)
        pix.fill(QColor(220, 20, 20))
        p = QPainter(pix)
        p.fillRect(30, 0, 30, 40, QColor(20, 20, 220))  # red left, blue right
        p.end()
        out = softened(pix, 3.0).toImage()
        self.assertEqual((out.width(), out.height()), (60, 40))
        for x in (2, 29, 31, 58):
            c = out.pixelColor(x, 20)
            self.assertTrue(c.red() == c.green() == c.blue(), (x, c.getRgb()))  # no colour left
        left, edge, right = out.pixelColor(2, 20).red(), out.pixelColor(29, 20).red(), out.pixelColor(58, 20).red()
        self.assertNotEqual(left, right)  # the two halves still differ
        self.assertTrue(min(left, right) < edge < max(left, right), (left, edge, right))  # but the seam is soft, not a hard step

    def test_update_screen_greys_the_app_behind_it_and_lets_go_when_it_closes(self):
        from PySide6.QtWidgets import QWidget

        from distill.ui import veil as veil_mod
        from distill.ui.update_screen import UpdateScreen

        host = QWidget()
        host.resize(300, 200)
        host.show()  # the real app's host is visible before UpdateScreen grabs it
        self.app.processEvents()
        self.addCleanup(host.close)
        self.addCleanup(setattr, veil_mod.motion.Motion, "reduce", veil_mod.motion.Motion.reduce)
        veil_mod.motion.Motion.reduce = True  # no fades: the veil is there and gone at once
        screen = UpdateScreen(host, "Distill v9.9")
        screen.show()
        veil = screen._veil
        self.assertIsNotNone(veil)
        self.assertEqual((veil.width(), veil.height()), (300, 200))
        self.assertEqual(veil.opacity(), 1.0)
        screen.dismiss()
        self.assertIsNone(screen._veil)

    def test_an_update_screen_with_no_window_behind_it_needs_no_veil(self):
        from distill.ui.update_screen import UpdateScreen

        screen = UpdateScreen(None, "Distill v9.9")
        screen.show()
        self.assertIsNone(screen._veil)
        screen.dismiss()

    def test_update_screen_is_only_the_logo(self):
        from PySide6.QtCore import Qt

        from distill.ui.update_screen import LOGO, UpdateScreen

        screen = UpdateScreen(None, "Distill v9.9")
        self.addCleanup(screen.deleteLater)
        self.assertEqual((screen.width(), screen.height()), (LOGO, LOGO))  # the window is the logo: no margins, no labels, no button
        self.assertTrue(screen.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(screen.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(len(screen.findChildren(type(screen.logo))), 1)
        self.assertEqual(screen.logo.geometry().getRect(), (0, 0, LOGO, LOGO))
        screen.set_progress(12 << 20, 48 << 20)
        self.assertIn("17%", screen.logo.toolTip())  # the detail is one hover away
        self.assertIn("cancel", screen.logo.toolTip().lower())
        screen.set_installing()
        self.assertNotIn("cancel", screen.logo.toolTip().lower())  # nothing to cancel any more

    def test_update_screen_describes_itself_for_the_install_window(self):
        from pathlib import Path

        from distill.ui.update_screen import UpdateScreen

        screen = UpdateScreen(None, "Distill v9.9")
        self.addCleanup(screen.deleteLater)
        screen.show()
        screen.set_progress(48 << 20, 48 << 20)
        screen.set_installing()
        spec = screen.handoff_spec()
        self.assertAlmostEqual(spec["start"], 0.7)
        self.assertGreaterEqual(spec["w"], 240)
        self.assertEqual(spec["w"], spec["h"])  # the window is the logo, so it is square
        self.assertTrue(Path(spec["logo"]).is_file())
        self.assertNotIn("status_text", spec)  # no text to carry over


if __name__ == "__main__":
    unittest.main()
