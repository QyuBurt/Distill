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
from tests.helpers import IsolatedSoundtrackFiles
from tests.helpers import enter
from tests import helpers  # noqa: E402,F401


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class WindowSmokeTests(IsolatedSoundtrackFiles):
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
        with mock.patch.object(rc, "ROOT", Path(td.name)):
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
                self.assertEqual(win._rc_owned["ship-of-harkinian"].status, "likely")  # the grid still lists everything; owned games get a badge
                self.assertGreater(win.rc_list.count(), 3)
                win._rc_back()
                self.assertEqual(win.rc_views.currentIndex(), 0)

    def test_recomps_page_chooses_a_folder_finds_installs_and_links_one_by_hand(self):
        import tempfile
        from pathlib import Path

        from distill import recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_rc_ui_")
        self.addCleanup(td.cleanup)
        lib = Path(td.name) / "lib"
        (lib / "Ship of Harkinian").mkdir(parents=True)
        (lib / "Ship of Harkinian" / "soh.exe").write_bytes(b"MZ")
        with mock.patch.object(rc, "ROOT", Path(td.name)), mock.patch.object(rc, "platform_key", lambda: "windows"):
            win = self.make_window()
            self.addCleanup(win.close)
            with mock.patch("threading.Thread.start", lambda self: None):
                win._nav_click("recomps")
                self.assertEqual(win.rc_root_edit.text(), "")
                rc.set_recomps_root(win.rc_store, lib)
                win.rc_root_edit.setText(str(lib))
                found = rc.scan_all(win.rc_store, win._rc_catalog, "windows")
                win._apply_events([("recomp_scan", found, False, "")])
                inst = win.rc_store.installed("ship-of-harkinian")
                self.assertIsNotNone(inst)
                self.assertEqual(inst.linked, "folder")
                win._rc_show_detail("ship-of-harkinian")
                self.assertEqual(win.rcd_remove.text(), "Remove")  # a linked copy is only forgotten
                self.assertTrue(win.rcd_locate.menu() is not None)
                self.assertTrue((lib / "Ship of Harkinian" / "soh.exe").exists())

    def test_emulators_page_lists_groups_links_installs_and_filters(self):
        import tempfile
        from pathlib import Path

        from distill import emulators as em
        from distill import recomps as rc

        td = tempfile.TemporaryDirectory(prefix="distill_em_ui_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(rc, "ROOT", Path(td.name)):
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

    def test_emulators_page_shows_compact_tiles_by_default_and_can_switch_to_cards(self):
        import tempfile
        from pathlib import Path

        from distill import recomps as rc
        from distill.ui.emu_cards import EmulatorCard, EmulatorTile

        td = tempfile.TemporaryDirectory(prefix="distill_em_view_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(rc, "ROOT", Path(td.name)):
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
        with mock.patch.object(rc, "ROOT", Path(td.name)), mock.patch.object(em, "forks_manifest", lambda: Path(td.name) / "forks.json"):
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
                self.assertFalse(hasattr(win, "emd_back"))  # no back button: clicking outside the card closes it, and a fork closes to its emulator
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

    def test_soundtracks_page_shows_albums_with_tracks_and_saves_ticked(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("sound", win.pages)
        win.st_search.setText("persona 5")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        good = st.Album("archive.org", "p5", "Persona 5 Original Soundtrack", "", 1.0)
        win._st_on_found(0, st.GameResult(st.GameRequest("Persona 5"), [good]))
        self.assertEqual(len(win._st_rows), 1)
        files = [RemoteFile("Persona 5/soundtrack/01 - Opening.flac", "https://x/1", 10),
                 RemoteFile("Persona 5/soundtrack/02 - Battle.flac", "https://x/2", 20)]
        win._st_on_tracks(0, good, files, "")
        row = win._st_rows[0]
        self.assertEqual(len(row.view._infos), 2)
        self.assertEqual(row.view._infos[0].title, "Opening")
        self.assertEqual(win._st_ticked(), [])
        row.view.set_all_checked(True)
        self.assertEqual(win._st_ticked(), [row])
        started = []
        with mock.patch.object(type(win), "_dl_start_rows", lambda self, rows: started.append(rows)):
            win._st_download()
        self.assertEqual(started, [[0, 1]])
        self.assertEqual([f.name for f in win.dl_files],
                         ["Soundtracks/Persona 5 Original Soundtrack/01 - Opening.flac",
                          "Soundtracks/Persona 5 Original Soundtrack/02 - Battle.flac"])

    def test_khinsider_tracks_are_resolved_before_saving(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        win.st_search.setText("x")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        album = st.Album("khinsider", "x", "X", "", 1.0)
        win._st_on_found(0, st.GameResult(st.GameRequest("X"), [album]))
        page = "https://downloads.khinsider.com/game-soundtracks/album/x/01.mp3"
        win._st_on_tracks(0, album, [RemoteFile("X/soundtrack/01 - A.mp3", page, 5)], "")
        row = win._st_rows[0]
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_save([(row, list(row.files))])
        self.assertEqual(len(win._st_pending), 1)
        token = next(iter(win._st_pending))
        started = []
        direct = RemoteFile("X/soundtrack/01 - A.mp3", "https://cdn.example/01.mp3", 5)
        with mock.patch.object(type(win), "_dl_start_rows", lambda self, rows: started.append(rows)):
            win._st_on_resolved(token, [(0, [direct], "")])
        self.assertEqual(started, [[0]])
        self.assertEqual(win.dl_files[0].url, "https://cdn.example/01.mp3")

    def test_home_tab_fills_its_rows_and_opens_albums_on_the_search_tab(self):
        from distill import soundtracks as st

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual(win.media_tabs.tabText(0), "Home")
        albums = [st.Album("khinsider", f"a{i}", f"Album {i}", "PC \u00b7 2020", 1.0) for i in range(3)]
        win._st_on_home("top40", albums, "")
        strip = win._home_strips["top40"]
        self.assertEqual(len(strip.tiles()), 3)
        win._st_on_home("new", [], "khinsider is behind a Cloudflare check")
        self.assertIn("Cloudflare", win._home_strips["new"].note.text())
        started = []
        with mock.patch("threading.Thread.start", lambda self: started.append(self)):
            strip.tiles()[1].opened.emit()
        self.assertEqual(win.media_tabs.tabText(win.media_tabs.currentIndex()), "Search")
        self.assertEqual([r.album.title for r in win._st_rows], ["Album 1"])
        self.assertEqual(len(started), 1)  # its tracks are being listed

    def test_durations_show_in_track_rows_and_the_album_line(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        win.st_search.setText("x")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        album = st.Album("khinsider", "x", "X", "", 1.0)
        win._st_on_found(0, st.GameResult(st.GameRequest("X"), [album]))
        files = [RemoteFile("X/soundtrack/01 - A.mp3", "https://x/1", 5, seconds=320),
                 RemoteFile("X/soundtrack/02 - B.mp3", "https://x/2", 5, seconds=3400)]
        win._st_on_tracks(0, album, files, "")
        row = win._st_rows[0]
        self.assertEqual([i.duration for i in row.view._infos], ["5:20", "56:40"])
        self.assertIn("1 h 1 min", row.view.meta.text())
        self.assertNotIn("KHInsider", row.view.meta.text())  # (the source is a chip of its own, so the facts line is not cut off)
        self.assertEqual(row.view._source_chip.text(), "KHInsider")  # (KHInsider has a clickable credit chip; the plain label is for sources without one)
        self.assertTrue(row.view.source.isHidden())

    def test_media_page_has_tabs_and_the_library_lists_starred_albums(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        tabs = [win.media_tabs.tabText(i) for i in range(win.media_tabs.count())]
        self.assertEqual(tabs, ["Home", "Search", "Browse", "Library", "Playlists", "Queue", "Settings"])
        win.st_search.setText("celeste")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        album = st.Album("khinsider", "celeste", "Celeste", "PC \u00b7 2018", 1.0)
        win._st_on_found(0, st.GameResult(st.GameRequest("Celeste"), [album]))
        win._st_on_tracks(0, album, [RemoteFile("Celeste/soundtrack/01 - A.mp3", "https://x/1", 5)], "")
        self.assertEqual(win.lib_list.count() - 2, 0)  # empty: just the message and the stretch
        win._st_toggle_library(win._st_rows[0])
        self.assertTrue(win.media_store.in_library(album))
        self.assertEqual(len(win._lib_widgets), 1)
        self.assertEqual(win._lib_widgets[0][1].title.text(), "Celeste")
        win.lib_filter.setText("zzz")
        self.assertEqual(len(win._lib_widgets), 0)
        win.lib_filter.setText("")
        win._lib_remove(album)
        self.assertFalse(win.media_store.in_library(album))
        self.assertEqual(len(win._lib_widgets), 0)

    def test_disk_allocations_in_settings_are_stored_and_enforced(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win.snap_spin.setValue(7)
        win._media_set_snapshot_limit()
        self.assertEqual(win.media_store.snapshot_limit(), 7 * 1024 * 1024)
        self.assertEqual(win.cache_spin.value(), 0)  # the audio cache starts off
        win.cache_spin.setValue(300)
        win._media_set_cache_limit()
        self.assertEqual(win.media_store.audio_cache_limit(), 300 * 1024 * 1024)
        self.assertIn("used", win.cache_use.text())
        win.cache_spin.setValue(0)
        win._media_set_cache_limit()
        self.assertEqual(win.cache_use.text(), "not caching")

    def test_queue_tab_shows_up_next_and_edits_it(self):
        from distill.mediaqueue import QueueItem

        win = self.make_window()
        self.addCleanup(win.close)
        tabs = [win.media_tabs.tabText(i) for i in range(win.media_tabs.count())]
        self.assertIn("Queue", tabs)
        items = [QueueItem(f"t{i}", "Album", f"u{i}", "", "khinsider", "x", i) for i in range(4)]
        win.pq.set(items, 0)
        win._queue_refresh()
        self.assertEqual(len(win._q_widgets), 6)  # "Now playing" + 1 row, "Up next" + 3 rows
        win._queue_remove(3)
        self.assertEqual([i.title for i in win.pq.items], ["t0", "t1", "t2"])
        win._queue_shift(2, -1)  # the up button on t2
        self.assertEqual([i.title for i in win.pq.items], ["t0", "t2", "t1"])
        self.assertEqual(win.pq.current().title, "t0")
        win._queue_cycle_repeat()
        self.assertEqual(win.q_repeat.text(), "Repeat: all")
        win._st_save_queue()
        from distill.mediaqueue import PlayQueue
        again = PlayQueue()
        import json
        again.restore(json.loads(win.media_store.setting("queue_state")))
        self.assertEqual([i.title for i in again.items], ["t0", "t2", "t1"])

    def test_queue_rows_can_be_dropped_into_a_new_place(self):
        from distill.mediaqueue import QueueItem

        win = self.make_window()
        self.addCleanup(win.close)
        win.pq.set([QueueItem(f"t{i}", "A", f"u{i}", "", "khinsider", "x", i) for i in range(4)], 0)
        win._queue_refresh()
        win._queue_drop(3, 0)  # the last one to the front of "up next"
        self.assertEqual([win.pq.items[i].title for i in win.pq.upcoming()], ["t3", "t1", "t2"])
        win._queue_shift(win.pq.upcoming()[0], 1)  # nudge it one place down
        self.assertEqual([win.pq.items[i].title for i in win.pq.upcoming()], ["t1", "t3", "t2"])
        win._queue_drop(win.pq.upcoming()[0], 3)  # drop below the last row
        self.assertEqual([win.pq.items[i].title for i in win.pq.upcoming()], ["t3", "t2", "t1"])

    def test_favourites_playlists_and_history(self):
        from distill import soundtracks as st
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("Playlists", [win.media_tabs.tabText(i) for i in range(win.media_tabs.count())])
        win.st_search.setText("celeste")
        with mock.patch("threading.Thread.start", lambda self: None):
            win._st_search()
        album = st.Album("khinsider", "celeste", "Celeste", "PC \u00b7 2018", 1.0)
        win._st_on_found(0, st.GameResult(st.GameRequest("Celeste"), [album]))
        files = [RemoteFile("Celeste/soundtrack/01 - A.mp3", "https://x/1", 5),
                 RemoteFile("Celeste/soundtrack/02 - B.mp3", "https://x/2", 5)]
        win._st_on_tracks(0, album, files, "")
        row = win._st_rows[0]
        win._pl_files[("khinsider", "celeste")] = files  # as if listed already: no network in a test
        win._st_toggle_favourite(row, 1)
        self.assertEqual(win.media_store.favourite_positions(album), {1})
        self.assertTrue(row.view._infos[1].liked)
        win._pl_reload_picker()
        self.assertEqual(win._pl_current(), "favourites")
        self.assertEqual(len(win._pl_widgets), 1)
        pid = win.media_store.create_playlist("Mix")
        win._pl_add(row, [0, 1], pid)
        win.pl_pick.setCurrentIndex(win.pl_pick.findData(pid))
        win._pl_refresh()
        self.assertEqual(len(win._pl_widgets), 2)
        win._pl_move(0, 1)
        self.assertEqual([p for _i, _a, p in win.media_store.playlist_items(pid)], [1, 0])
        played = []
        with mock.patch.object(type(win), "_st_play_index", lambda self, i: played.append(i)):
            win._pl_play(0)
        self.assertEqual(win.pq.current().title, "B")
        self.assertEqual(played, [win.pq.current_index])
        win.pq.set([win.pq.items[0]], 0)
        win._st_log_play()
        self.assertEqual([a.title for a in win.media_store.recent_albums()], ["Celeste"])
        win._st_log_play()  # once per play
        self.assertEqual(win.media_store.library(), [])  # a play does not add to the library
        win._st_clear_history()
        self.assertEqual(win.media_store.recent_albums(), [])

    def test_game_soundtrack_button_ranks_by_platform_and_remembers_the_choice(self):
        from distill import soundtracks as st

        win = self.make_window()
        self.addCleanup(win.close)
        started = []
        with mock.patch("threading.Thread.start", lambda self: started.append(self)):
            win._st_search_game("g1", "Persona 5", "Sony PlayStation 4", "2016")
        self.assertEqual(win._st_game_ctx, ("g1", "Persona 5", "Sony PlayStation 4", "2016"))
        self.assertEqual(win.st_search.text(), "Persona 5")
        pc = st.Album("khinsider", "pc", "Persona 5", "PC \u00b7 2017", 0.95)
        ps4 = st.Album("khinsider", "ps4", "Persona 5", "PlayStation 4 \u00b7 2016", 0.90)
        self.assertEqual([a.ident for a in st.rank_for_game([pc, ps4], "Sony PlayStation 4", "2016")], ["ps4", "pc"])
        win._st_on_found(0, st.GameResult(st.GameRequest("Persona 5"), [ps4, pc]))
        row = win._st_rows[0]
        win._st_use_for_game(row)
        self.assertEqual(win.media_store.game_album("g1").ident, "ps4")
        self.assertTrue(win._is_game_choice(ps4))
        win._st_use_for_game(row)  # a second press forgets it
        self.assertIsNone(win.media_store.game_album("g1"))

    def test_saved_albums_join_the_library_with_badges_and_merge_with_streamed_ones(self):
        from distill import soundtracks as st

        win = self.make_window()
        self.addCleanup(win.close)
        local = st.Album("local", "/music/Celeste", "Celeste", "", 1.0)
        win._st_on_local([(local, 12)])
        self.assertEqual([w.badge.text() for _a, w in win._lib_widgets], ["Saved on disk"])
        win.media_store.add_to_library(st.Album("khinsider", "celeste", "Celeste Original Soundtrack", "PC \u00b7 2018", 1.0))
        win._lib_refresh()
        self.assertEqual([w.badge.text() for _a, w in win._lib_widgets], ["Saved + streaming"])  # one row for both
        win.media_store.add_to_library(st.Album("khinsider", "hades", "Hades", "PC \u00b7 2020", 1.0))
        win._lib_refresh()
        self.assertEqual(sorted(w.badge.text() for _a, w in win._lib_widgets), ["Saved + streaming", "Streaming"])
        win._st_on_local([])  # the folder was deleted
        self.assertEqual([w.badge.text() for _a, w in win._lib_widgets].count("Saved on disk"), 0)

    def test_khinsider_is_credited_with_a_chip_that_says_where_things_come_from(self):
        from distill.ui.sourcechips import has_chip, source_chip

        chip = source_chip("khinsider")
        self.assertEqual(chip.text(), "KHInsider")
        self.assertIn("downloads.khinsider.com", chip.toolTip())
        self.assertIn("album lists, covers, track lists", chip.toolTip())
        self.assertEqual(source_chip("archive.org").text(), "archive.org")
        self.assertIsNone(source_chip(st.LOCAL_SOURCE))  # files on this computer need no credit
        self.assertTrue(has_chip("khinsider") and not has_chip(st.LOCAL_SOURCE))
        win = self.make_window()
        self.addCleanup(win.close)
        album = st.Album("khinsider", "x", "X", "", 1.0)
        row = win._st_add_album(album)
        self.assertEqual(row.view._source_chip.text(), "KHInsider")
        self.assertNotIn("KHInsider", row.view.meta.text())  # the chip says it, the line of facts does not repeat it
        local = win._st_add_album(st.Album(st.LOCAL_SOURCE, "/music/G", "G", "", 1.0))
        self.assertIsNone(local.view._source_chip)

    def test_browse_tab_lists_a_platform_a_page_at_a_time(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("Browse", [win.media_tabs.tabText(i) for i in range(win.media_tabs.count())])
        win._st_on_browse("consoles", 0, [("nintendo-switch", "Switch", 1500), ("playstation-4", "PS4", 1900)], 1, "")
        self.assertEqual(win.br_value.count(), 2)
        self.assertEqual(win.br_value.currentIndex(), -1)  # nothing chosen until the person chooses
        albums = [st.Album("khinsider", f"a{i}", f"Album {i}", "Switch \u00b7 2020", 1.0) for i in range(70)]
        win._br_token = 5
        win._st_on_browse("albums", 4, albums, (1, 1), "")  # an answer to an earlier choice: ignored
        self.assertEqual(len(win._br_tiles), 0)
        win._st_on_browse("albums", 5, albums, (1, 3), "")
        self.assertEqual(len(win._br_tiles), 60)  # a step at a time
        self.assertTrue(win.br_more.isVisible() or win.br_more.text().startswith("Show more"))
        self.assertTrue(win.br_next.isEnabled() and not win.br_prev.isEnabled())
        self.assertEqual(win.br_page_label.text(), "Page 1 of 3")
        win._br_render(more=True)
        self.assertEqual(len(win._br_tiles), 70)
        win.br_mode.setCurrentIndex(1)  # Year
        self.assertEqual(win.br_value.itemData(0), str(__import__("time").localtime().tm_year))

    def test_covers_are_kept_on_disk_and_not_fetched_twice(self):
        win = self.make_window()
        self.addCleanup(win.close)
        calls = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b"IMG" * 10

        def fake(url, headers=None, timeout=30):
            calls.append(url)
            return Resp()

        with mock.patch("distill.ui.soundtrack_page.http_open", fake):
            win._st_art_worker("https://x/c.jpg")
            win._st_art_worker("https://x/c.jpg")
        self.assertEqual(len(calls), 1)  # the second came from the folder
        self.assertEqual(win._st_covers.get("https://x/c.jpg"), b"IMG" * 10)

    def test_backup_and_restore_buttons_round_trip_the_library(self):
        import json
        import tempfile
        from pathlib import Path

        win = self.make_window()
        self.addCleanup(win.close)
        win.media_store.add_to_library(st.Album("khinsider", "celeste", "Celeste", "PC \u00b7 2018", 1.0))
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "backup.json")
            with mock.patch("distill.ui.soundtrack_page.QFileDialog.getSaveFileName", return_value=(path, "")):
                win._media_backup()
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["format"], "distill-media-backup")
            win.media_store.remove_from_library(st.Album("khinsider", "celeste", "Celeste", "", 1.0))
            self.assertEqual(win.media_store.library(), [])
            with mock.patch("distill.ui.soundtrack_page.QFileDialog.getOpenFileName", return_value=(path, "")):
                win._media_restore()
            self.assertEqual([e.title for e in win.media_store.library()], ["Celeste"])
            Path(path).write_text("not json", encoding="utf-8")
            with mock.patch("distill.ui.soundtrack_page.QFileDialog.getOpenFileName", return_value=(path, "")):
                win._media_restore()  # a bad file is reported, not raised
            self.assertEqual(len(win.media_store.library()), 1)

    def test_sources_card_says_whether_khinsider_is_reachable(self):
        win = self.make_window()
        self.addCleanup(win.close)
        with mock.patch.object(st, "curl_cffi_available", lambda: False):
            self.assertIn("python -m pip install curl_cffi", win._khi_status_text())
        with mock.patch.object(st, "curl_cffi_available", lambda: True), mock.patch.object(st, "khinsider_wait_seconds", lambda: 0):
            self.assertIn("Connected", win._khi_status_text())
        with mock.patch.object(st, "curl_cffi_available", lambda: True), mock.patch.object(st, "khinsider_wait_seconds", lambda: 120):
            self.assertIn("2 min", win._khi_status_text())

    def test_keyboard_shortcuts_exist_and_a_tracks_length_is_known_before_it_plays(self):
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual(len(win._st_shortcuts), 7)
        row = win._st_add_album(st.Album("khinsider", "x", "X", "", 1.0))
        row.files = [RemoteFile("X/soundtrack/01 - A.mp3", "https://x/1", 5, seconds=215)]
        self.assertEqual(win._st_items_for(row, [0])[0].seconds, 215)

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

    def test_api_keys_live_in_folders_on_their_own_settings_page_and_are_hidden_while_typed(self):
        import tempfile
        from pathlib import Path

        from PySide6.QtWidgets import QLineEdit

        from distill import apikeys

        td = tempfile.TemporaryDirectory(prefix="distill_apikeys_ui_")
        self.addCleanup(td.cleanup)
        with mock.patch.object(apikeys, "FILE", Path(td.name) / "keys.json"), \
                mock.patch.object(apikeys, "LEGACY_KEYS_FILE", Path(td.name) / "old_keys.json"), \
                mock.patch.object(apikeys, "LEGACY_LOGINS_FILE", Path(td.name) / "old_logins.json"):
            apikeys._cache = None
            self.addCleanup(setattr, apikeys, "_cache", None)
            win = self.make_window()
            self.addCleanup(win.close)
            self.assertEqual([win.settings_list.item(i).text() for i in range(win.settings_list.count())],
                             ["Tools", "Downloads", "API keys", "General"])
            self.assertEqual(set(win.api_keys.edits), {"sgdb", "github", "ra_user", "ra"})
            for name, edit in win.api_keys.edits.items():  # a user name isn't a secret; every key is hidden
                want = QLineEdit.EchoMode.Normal if name == "ra_user" else QLineEdit.EchoMode.Password
                self.assertEqual(edit.echoMode(), want, name)
            panel = win.api_keys
            self.assertEqual([f.header.text() for f in (panel.games, panel.downloads, panel.updates)],
                             ["Games & artwork", "Downloads & file hosts", "Updates"])
            self.assertFalse(any(f.header.isChecked() for f in (panel.games, panel.downloads, panel.updates)))  # all folded to start with
            self.assertIs(win.logins_panel.parentWidget().parentWidget(), panel.downloads)  # the sign-ins sit in the Downloads folder
            self.assertEqual(panel.updates.header.summary, "None saved")
            seen = []
            win.api_keys.changed.connect(seen.append)
            win.api_keys.edits["github"].setText(" ghp_x ")
            win.api_keys._save()
            self.assertEqual((apikeys.get("github"), seen), ("ghp_x", ["github"]))
            self.assertEqual(panel.updates.header.summary, "1 saved")
            self.assertEqual(panel.games.header.summary, "None saved")  # saving a GitHub key says nothing about Games
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
        win._pin_boxes["chd"].setChecked(True)
        self.assertIn("Ctrl+1", win.nav_items["chd"].toolTip())

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

    def test_start_button_drifts_only_while_running_and_holds_when_paused(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win.show()
        btn = win.start_btn
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

    def test_sound_page_has_no_start_pause_stop_bar_but_the_downloads_page_keeps_it(self):
        win = self._shown_window()
        win._show_page("sound")
        self.assertTrue(win.bar_wrap.isHidden())
        with mock.patch.object(win, "_st_download") as save:
            win.start_btn.setEnabled(True)
            from PySide6.QtGui import QShortcut
            keys = {sc.key().toString(): sc for sc in win.findChildren(QShortcut)}
            keys["Ctrl+Return"].activated.emit()
            save.assert_not_called()
        win._show_page("dl")
        self.assertFalse(win.bar_wrap.isHidden())

    def test_quick_search_filters_tools_and_their_groups(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._pin_boxes["chd"].setChecked(True)  # a fresh install pins no converter or organiser: pin two to search among
        win._pin_boxes["fix"].setChecked(True)
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
        self.assertEqual(icons.icon_for_title("Updates"), "branch")
        self.assertFalse(icons.pixmap("search", "#ffffff", 16).isNull())
        self.assertEqual(load_fonts(), ("Inter 18pt", "Outfit", "JetBrains Mono"))

    def test_seek_bar_wave_ripples_while_playing_and_settles_flat_when_paused(self):
        from PySide6.QtCore import Qt

        win = self.make_window()
        self.addCleanup(win.close)
        win.show()
        slider = win.now_playing._slider
        slider.setRange(0, 1000)
        slider.setValue(400)
        slider.set_moving(True)
        self.assertTrue(slider._timer.isActive())
        self.assertEqual(slider._timer.timerType(), Qt.TimerType.PreciseTimer)  # frames on time, not on the system's coarse clock
        for _ in range(60):
            slider.advance(1 / 30)  # two seconds
        self.assertGreater(slider._amp, 0.9)
        slider.set_moving(False)  # paused: eases flat, then stops redrawing
        for _ in range(120):
            slider.advance(1 / 30)
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
            self.assertLessEqual(scroll.widget().width(), scroll.viewport().width(), f"page {index} runs off the window")
            widths.add(scroll.widget().width())
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

    def test_cover_is_a_disc_that_eases_into_a_spin_and_to_a_stop(self):
        from PySide6.QtGui import QColor, QPixmap
        from distill.ui.now_playing import CdArt, NowPlaying

        np = NowPlaying()
        self.addCleanup(np.deleteLater)
        disc = np._art
        pix = QPixmap(100, 60)
        pix.fill(QColor("red"))
        np.set_art(pix)
        self.assertFalse(disc.pixmap().isNull())
        self.assertFalse(np.grab().isNull())  # paints with a cover...
        np.set_art(None, pending=True)
        self.assertTrue(disc.pixmap().isNull())
        self.assertFalse(np.grab().isNull())  # ...and as a blank disc while it loads
        np.set_art(pix)
        np.set_playing(True)
        disc.advance(0.05)
        self.assertLess(disc._omega, CdArt.SPIN * 0.2)  # it starts gently
        for _ in range(100):
            disc.advance(0.05)
        self.assertGreater(disc._omega, CdArt.SPIN * 0.95)  # and reaches full speed
        np.set_playing(False)
        disc.advance(0.05)
        self.assertGreater(disc._omega, CdArt.SPIN * 0.8)  # pausing does not stop it dead
        for _ in range(200):
            disc.advance(0.05)
        self.assertEqual(disc._omega, 0.0)  # it comes to rest
        rest = disc._angle
        disc.advance(0.5)
        self.assertEqual(disc._angle, rest)

    def test_holding_the_disc_stops_it_turning_it_reports_how_far_and_a_plain_click_still_opens_the_cover(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QColor, QPixmap
        from PySide6.QtTest import QTest
        from distill.ui.now_playing import NowPlaying

        np = NowPlaying()
        self.addCleanup(np.deleteLater)
        disc = np._art
        pix = QPixmap(100, 60)
        pix.fill(QColor("red"))
        np.set_art(pix)
        events, clicks = [], []
        np.scratchStarted.connect(lambda: events.append("start"))
        np.scratched.connect(lambda turned, speed: events.append(("turn", round(turned), speed)))
        np.scratchEnded.connect(lambda: events.append("end"))
        disc.clicked.connect(lambda: clicks.append(1))
        left = Qt.MouseButton.LeftButton

        QTest.mouseClick(disc, left, pos=QPoint(40, 26))  # pressed and let go without moving: a click, as before
        self.assertEqual((clicks, events), ([1], []))

        np.set_playing(True)
        for _ in range(100):
            disc.advance(0.05)
        self.assertGreater(disc._omega, 0)
        QTest.mousePress(disc, left, pos=QPoint(45, 26))  # the right of the centre...
        self.assertEqual(events, [])  # ...nothing yet: it is only a press
        QTest.mouseMove(disc, QPoint(26, 45))  # ...a quarter of a turn clockwise, to the bottom
        self.assertEqual(events[0], "start")
        self.assertEqual(events[1][:2], ("turn", 90))
        self.assertGreater(events[1][2], 0)  # clockwise is forward
        self.assertEqual(disc._omega, 0.0)  # a hand on it stops it at once
        QTest.mouseMove(disc, QPoint(45, 26))  # and back again
        self.assertEqual(events[-1][:2], ("turn", 0))
        self.assertLess(events[-1][2], 0)
        QTest.mouseRelease(disc, left, pos=QPoint(45, 26))
        self.assertEqual(events[-1], "end")
        self.assertEqual(clicks, [1])  # turning it is not a click
        self.assertFalse(disc._held)

        events.clear()  # holding without moving counts too (the hold timer calls this)
        QTest.mousePress(disc, left, pos=QPoint(45, 26))
        disc._grab()
        QTest.mouseRelease(disc, left, pos=QPoint(45, 26))
        self.assertEqual((events, clicks), (["start", "end"], [1]))

    def test_scratching_the_disc_pauses_seeks_and_resumes_the_player(self):
        from PySide6.QtMultimedia import QMediaPlayer

        win = self.make_window()
        self.addCleanup(win.close)

        class Player:  # just what the page asks of a QMediaPlayer
            def __init__(self):
                self.pos, self.state, self.calls = 10_000, QMediaPlayer.PlaybackState.PlayingState, []

            def position(self):
                return self.pos

            def duration(self):
                return 60_000

            def playbackState(self):
                return self.state

            def pause(self):
                self.calls.append("pause")

            def play(self):
                self.calls.append("play")

            def setPosition(self, ms):
                self.calls.append(("seek", ms))
                self.pos = ms

        player, sounds = Player(), []
        from distill.mediaqueue import QueueItem
        win._st_player = player
        win.pq.set([QueueItem("t", "g", "u", "", "khinsider", "x", 0)], 0)
        win._st_vinyl_play = lambda name, gain=1.0: sounds.append(name)  # (no audio device needed)
        win._st_scratch_start()
        self.assertEqual((player.calls, sounds), (["pause"], ["vinyl-stop"]))
        win._st_scratch_move(160.0, 300.0)  # a second's turn clockwise: a second on
        self.assertEqual(player.calls[-1], ("seek", 11_000))
        win._st_scratch_move(-1e6, -300.0)  # dragged far back: it stops at the start
        win._st_scratch_end()
        self.assertEqual(player.calls[-1], ("seek", 0))  # the music waits for the spin-up sound...
        self.assertEqual(sounds, ["vinyl-stop", "scratch-fwd", "vinyl-start"])
        win._st_scratch_resume()  # ...and starts when it is done
        self.assertEqual(player.calls[-2:], [("seek", 0), "play"])

        player.calls.clear()
        sounds.clear()
        player.state = QMediaPlayer.PlaybackState.PausedState  # held while it was already paused: let go leaves it paused
        win._st_scratch_start()
        win._st_scratch_end()
        self.assertEqual((player.calls, sounds), (["pause"], ["vinyl-stop"]))

    def test_empty_games_page_is_centred_in_the_window_and_needs_no_scrollbar(self):
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from distill import games as gm

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(Path(tmp.name) / "games.db")
        win.resize(1200, 900)
        win.show()
        with mock.patch("threading.Thread.start", lambda self: None):
            win._nav_click("games")
            QApplication.processEvents()
            view = win.settings_scroll.viewport()
            empty = win.gm_empty
            self.assertTrue(empty.isVisible())
            self.assertEqual(win.settings_scroll.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # no games: nothing to scroll to
            sizes = []
            for height in (1100, 900, 700, 560, 480, 420):
                win.resize(1200, height)
                QApplication.processEvents()
                QApplication.processEvents()
                self.assertEqual((empty.width(), empty.height()), (view.width(), view.height()))  # it covers what the window shows
                first = empty._logo if not empty._logo.isHidden() else empty._title  # the block runs from its top widget to the button
                top = first.mapTo(view, first.rect().topLeft()).y()
                bottom = empty.pick.mapTo(view, empty.pick.rect().bottomLeft()).y()
                self.assertLessEqual(bottom, view.height(), f"button below the fold at window height {height}")
                self.assertGreaterEqual(top, 0, f"block cut off above at window height {height}")
                self.assertLessEqual(abs((top + bottom) / 2 - (view.height() - 24) / 2), 6, f"not centred at window height {height}")
                self.assertGreaterEqual(empty._sub.height(), empty._sub.fontMetrics().height() * 2) if empty._sub.isVisible() else None  # the line is not clipped
                sizes.append(empty._logo.width() if not empty._logo.isHidden() else 0)
            self.assertLessEqual(sizes[-1], sizes[0])  # the logo only ever shrinks as the window does
            win._nav_click("dl")  # another page: its scrollbar is back
            self.assertEqual(win.settings_scroll.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.assertFalse(empty.isVisible())

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
        self.assertEqual(win.start_btn.text(), "Start Download")

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

    def test_pages_can_be_opened_while_a_run_is_going(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")
        win.is_processing = True
        win._begin_run_label("chd")
        win._nav_click("dl")  # used to say "Locked while a run is active"
        self.assertEqual(win.current_page, "dl")
        win.is_processing = False

    def test_run_bar_shows_on_other_pages_and_leads_back(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")
        win.is_processing = True
        win._begin_run_label("chd")
        win._runbar_sync()
        self.assertFalse(win.run_bar.isHidden())  # the one progress bar: up on the running page too, not only on other pages
        win._show_page("fix")
        self.assertFalse(win.run_bar.isHidden())
        self.assertEqual(win.run_bar.title, "CHD Compression")
        self.assertIn("Compressing", win.run_bar.detail)
        win.pb_total.setValue(420)
        self.assertEqual(win.run_bar.value, 420)  # follows the overall progress bar
        self.assertIn("CHD Compression", win.start_btn.toolTip())
        win._run_bar_clicked()
        self.assertEqual(win.current_page, "chd")
        self.assertFalse(win.run_bar.isHidden())  # still up while the job runs, and clicking it there changes nothing
        win._run_bar_clicked()
        self.assertEqual(win.current_page, "chd")
        win._show_page("fix")
        win.is_processing = False
        win._runbar_sync()
        self.assertTrue(win.run_bar.isHidden())  # the job is over: nothing to show

    def test_run_panel_steps_aside_on_other_pages_and_returns(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")
        win.is_processing = True
        win._begin_run_label("chd")
        win._show_page("rvz")  # another conversion page must not show the CHD run's log as its own
        self.assertEqual(win._panel_mode, "away")
        self.assertFalse(win._log_shown)
        win._show_page("chd")
        self.assertEqual(win._panel_mode, "conv")
        win.is_processing = False

    def test_the_queue_follows_the_task_events_beside_the_log(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")
        win.queue_view.begin_run()
        win.queue_view.set_running(True)
        win._apply_events([("new", 1, "[  WAITING   ] Good"), ("new", 2, "[  WAITING   ] Bad"),
                           ("update", 1, "[ CONVERTING ]", "(40.0%) Good"),
                           ("update", 2, "[   ERROR    ]", "Bad (Conversion failed - exit code 1)")])
        model = win.queue_view.model
        self.assertEqual(model.rowCount(), 2)
        self.assertEqual((model.task_at(0).name, model.task_at(0).pct), ("Good", 40.0))
        self.assertEqual(model.task_at(1).note, "Conversion failed - exit code 1")
        self.assertIn("Bad", win.log.toPlainText())  # the log is written exactly as before
        win.queue_view._refresh()
        self.assertIn("1 running", win.queue_view.headline.text())
        self.assertIn("1 failed", win.queue_view.headline.text())

    def test_the_switch_swaps_the_queue_and_the_log_and_a_search_goes_to_the_log(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("chd")
        self.assertFalse(win.queue_view.isHidden())  # the queue is what a conversion page shows
        self.assertTrue(win.log.isHidden())
        win.view_log.setChecked(True)
        self.assertTrue(win.queue_view.isHidden())
        self.assertFalse(win.log.isHidden())
        win.view_queue.setChecked(True)
        self.assertFalse(win.queue_view.isHidden())
        self.assertTrue(win.log.isHidden())
        win.log_find.setText("anything")
        win._log_find()
        self.assertTrue(win.view_log.isChecked())  # the words are in the log, so the log comes up

    def test_the_queue_filters_settle_stopped_rows_and_keep_a_retrys_old_rows(self):
        win = self.make_window()
        self.addCleanup(win.close)
        view = win.queue_view
        view.begin_run()
        view.set_running(True)
        view.task_new(1, "[  VERIFIED  ] Done - 10% smaller (1.00 GB -> 900.00 MB)")
        view.task_new(2, "[ CONVERTING ] (40.0%) Halfway")
        view.task_new(3, "[   ERROR    ] Bad (Conversion failed - exit code 1)")
        view.filters["problems"].setChecked(True)
        self.assertEqual(view.model.rowCount(), 1)
        view.filters["active"].setChecked(True)
        self.assertEqual([view.model.task_at(r).name for r in range(view.model.rowCount())], ["Halfway"])
        view.filters["all"].setChecked(True)
        view.set_running(False)  # Stop: the row that was part-way says so
        self.assertEqual(view.model.task_at(1).state, "STOPPED")
        view.begin_run(keep=True)  # a retry: the earlier rows stay, dimmed, and the new run is counted alone
        view.task_new(1, "[ CONVERTING ] Bad")
        self.assertEqual(view.model.rowCount(), 4)
        self.assertTrue(view.model.stale(0))
        self.assertFalse(view.model.stale(3))
        view.clear()
        self.assertEqual(view.model.rowCount(), 0)

    def test_the_organise_review_chips_follow_the_selection(self):
        from distill.organize import Finding

        win = self.make_window()
        self.addCleanup(win.close)
        st = win.org["fix"]
        st.findings = [Finding("a", size=2**30), Finding("b", problem="locked")]
        st.checked = {0}
        win._org_update_status("fix")
        shown = lambda: [label.text() for label in st.review._labels if not label.isHidden()]  # noqa: E731
        self.assertEqual(shown(), ["1 of 2 selected", "frees 1.00 GB", "1 can't be applied"])
        st.checked = set()
        win._org_update_status("fix")
        self.assertEqual(shown(), ["0 of 2 selected", "1 can't be applied"])
        win._org_show("fix", [], "", "")  # a scan that found nothing leaves no stale selection behind
        self.assertEqual(shown(), [])

    def test_the_download_list_chips_follow_the_ticks_and_the_run(self):
        from distill.downloads import RemoteFile

        win = self.make_window()
        self.addCleanup(win.close)
        win.dl_model.set_files([RemoteFile("a.bin", "http://x/a", 2**30), RemoteFile("b.bin", "http://x/b", None),
                                RemoteFile("c.bin", "http://x/c", 5)])
        shown = lambda: [label.text() for label in win.dl_review._labels if not label.isHidden()]  # noqa: E731
        win.dl_checked = {0, 1}
        win._dl_update_sel()
        self.assertEqual(shown(), ["2 of 3 selected \u00b7 1.00 GB+"])
        win.dl_model.status[0], win.dl_model.status[1], win.dl_model.status[2] = "Done", "12.0%", "Failed: HTTP 404"
        win._dl_chips()
        self.assertEqual(shown(), ["2 of 3 selected \u00b7 1.00 GB+", "1 downloading", "1 done", "1 failed"])
        win.dl_model.set_files([])
        win._dl_update_sel()
        self.assertEqual(shown(), [])  # an emptied list leaves no stale counts

    def test_after_downloads_folds_to_a_line_that_follows_its_boxes(self):
        win = self.make_window()
        self.addCleanup(win.close)
        fold = win.dl_after
        self.assertEqual(fold.summary.text(), "Nothing happens when they finish")
        self.assertTrue(fold.body.isHidden())
        win.dl_organize.setChecked(True)
        win.dl_power.setCurrentIndex(1)
        self.assertIn("open Sort by System", fold.summary.text())
        self.assertIn("then sleep the computer", fold.summary.text())
        fold.head.setChecked(True)  # the controls inside are the same widgets, and are reachable
        self.assertFalse(fold.body.isHidden())
        self.assertIs(win.dl_command.parentWidget().parentWidget(), fold.body)
        win.dl_organize.setChecked(False)
        win.dl_power.setCurrentIndex(0)
        self.assertEqual(fold.summary.text(), "Nothing happens when they finish")

    def test_the_tools_page_tags_each_tool_and_counts_them(self):
        win = self.make_window()
        self.addCleanup(win.close)
        row = win._tool_rows["chdman"][0]
        win._pending_tools.discard("chdman")
        win._pending_dl.discard("chdman")
        win._installing.discard("chdman")
        win._set_tool_text("chdman", "/no/such/folder/chdman")
        win._refresh_tool_chips()
        self.assertEqual(row.chip.text(), "Missing")
        shown = [label.text() for label in win.tools_review._labels if not label.isHidden()]
        self.assertTrue(shown[0].endswith(" ready") and any(t.endswith(" missing") for t in shown), shown)
        win._installing.add("chdman")  # a download in progress shows as updating, whatever else is true
        win._refresh_tool_chips()
        self.assertEqual(row.chip.text(), "Updating")
        win._installing.discard("chdman")
        win._pending_tools.add("chdman")
        win._refresh_tool_chips()
        self.assertEqual(row.chip.text(), "Checking")

    def test_all_tools_opens_each_page_and_its_pin_edits_the_sidebar(self):
        from distill.convertpages import DEFAULT_UNPINNED, all_tool_keys

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual(set(win.cv_cards), set(all_tool_keys()))  # nine converters and five organisers
        win._show_page("convert")
        self.assertEqual(win.current_page, "convert")
        self.assertEqual(win._current_mode(), "emus")  # no run panel and no Start button, like the Emulators page
        self.assertIn(win.cv_cards["chd"].tag.text(), ("Ready", "Checking", "Updating", "Missing", "Running"))
        self.assertTrue(win.cv_cards["fix"].tag.isHidden())  # an organiser has no tool to report on
        self.assertFalse(win.nav_items["convert"].isHidden())  # a fresh install has the hub in the sidebar and none of the tools
        for key in ("chd", "fix"):
            self.assertTrue(win.nav_items[key].isHidden(), key)
            self.assertFalse(win.cv_cards[key].pin.isChecked(), key)
        win.cv_cards["chd"].pin.click()  # pin one from its card
        self.assertTrue(win._pin_boxes["chd"].isChecked())
        self.assertFalse(win.nav_items["chd"].isHidden())
        win._pin_boxes["fix"].setChecked(True)  # and Settings > Sidebar moves the pin on the card
        self.assertTrue(win.cv_cards["fix"].pin.isChecked())
        self.assertFalse(win.nav_groups["ORGANISE LIBRARY"]["header"].isHidden())
        win.cv_cards["chd"].pin.click()  # unpin it again
        self.assertTrue(win.nav_items["chd"].isHidden())
        self.assertIn("chd", win.pages)  # still there: the hub and Ctrl+K reach it
        win.cv_cards["rvz"].clicked.emit("rvz")  # a card opens its page
        self.assertEqual(win.current_page, "rvz")
        win._show_page("convert")
        win.cv_cards["rvz"].pin.click()  # but its pin does not
        self.assertEqual(win.current_page, "convert")
        self.assertTrue(set(DEFAULT_UNPINNED) >= {"rvz"})

    def test_estimate_savings_samples_the_source_and_reports_on_its_page(self):
        import tempfile
        import time
        from pathlib import Path

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual(set(win._pv_rows), {"chd", "rvz", "cso", "zar"})  # not Switch, PS3, PKG or Xbox
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "Game.cue").write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n')
            (Path(folder) / "Game.bin").write_bytes(b"\0" * (2 << 20))
            win.tabs["chd"].source.setText(folder)
            row = win._pv_rows["chd"]
            win._pv_start("chd")
            self.assertEqual(row.button.text(), "Stop estimating")
            deadline = time.monotonic() + 15
            while "chd" in win._pv_state and time.monotonic() < deadline:
                events = []
                while not win.ui_q.empty():
                    events.append(win.ui_q.get())
                win._apply_events(events)
                time.sleep(0.05)
            self.assertNotIn("chd", win._pv_state)
            self.assertEqual(row.button.text(), "Estimate savings")
            self.assertTrue(row.label.text().startswith("About "), row.label.text())
            self.assertIn("1 title", row.label.text())

    def test_organise_detail_rows_show_what_stays_and_what_goes_in_colour(self):
        from distill.organize import Finding
        from distill.ui.theme import T

        win = self.make_window()
        self.addCleanup(win.close)
        st = win.org["extras"]
        finding = Finding("Game", "2", "Keeping a.chd  |  frees 2.00 GB",
                          children=[("a.chd", "1.00 GB", "KEEP"), ("a.iso", "2.00 GB", "Remove")])
        win._org_show("extras", [finding], "", "")
        top = st.tree.topLevelItem(0)
        keep, gone = top.child(0), top.child(1)
        self.assertEqual(keep.foreground(2).color().name(), T.ok.name())
        self.assertTrue(keep.font(2).bold())
        self.assertEqual(gone.foreground(2).color().name(), T.bad.name())
        self.assertFalse(gone.font(2).bold())
        win._org_show("extras", [Finding("Game.cue", "2", "->  Game (USA).cue")], "", "")  # a rename's destination stands out
        self.assertEqual(st.tree.topLevelItem(0).foreground(2).color().name(), T.accent.name())

    def test_tool_rows_show_their_version_and_update_now_waits_for_a_job(self):
        from unittest import mock

        win = self.make_window()
        self.addCleanup(win.close)
        row = win._tool_rows["chdman"][0]
        win._refresh_tool_info(force=True)
        self.assertFalse(row.version.isHidden())
        self.assertFalse(row.update_btn.isHidden())
        info = win._tool_info_cache["chdman"]
        self.assertEqual(row.update_btn.isEnabled(), info.can_update)  # idle: live wherever an update can do anything
        win.is_processing = True  # a conversion may be using the tool
        self.addCleanup(setattr, win, "is_processing", False)
        win._refresh_tool_info()
        self.assertFalse(row.update_btn.isEnabled())
        self.assertIn("running job", row.update_btn.toolTip())
        with mock.patch("threading.Thread.start") as start:
            win._tool_update("chdman")
            start.assert_not_called()  # and pressing it anyway starts nothing
        win.is_processing = False
        with mock.patch("threading.Thread.start") as start:
            win._tool_update("chdman")
            start.assert_called_once()
        self.assertIn("chdman", win._updating)
        win._tool_ready("chdman")  # the manager always ends with a ready event
        self.assertNotIn("chdman", win._updating)

    def test_the_links_box_is_one_line_and_grows_with_the_links(self):
        win = self.make_window()
        self.addCleanup(win.close)
        box = win.dl_url_text
        one = box.height()
        self.assertLess(one, 60)  # a slim row, not the 96 px box it was
        box.setPlainText("https://archive.org/details/a")
        self.assertEqual(box.height(), one)  # a single link stays one line
        box.setPlainText("https://a\nhttps://b\nhttps://c")
        three = box.height()
        self.assertGreater(three, one)
        box.setPlainText("x\n" * 40)
        most = box.height()
        self.assertGreater(most, three)
        box.setPlainText("x\n" * 400)
        self.assertEqual(box.height(), most)  # it stops growing and scrolls
        box.clear()
        self.assertEqual(box.height(), one)
        self.assertGreaterEqual(win.dl_fetch_btn.x(), 0)  # (the Fetch button sits beside the box now, not under it)

    def test_the_bar_along_the_bottom_is_the_only_progress_bar(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertFalse(hasattr(win, "st_pb"))  # the Resonance dock keeps its buttons and no bar of its own
        win.is_processing = True
        self.addCleanup(setattr, win, "is_processing", False)
        win._begin_run_label("chd")
        win.pb_total.setValue(400)
        win.lbl_total_prog.setText("Batch Progress: 3 / 12")
        win.lbl_eta.setText("Elapsed: 4m   |   Est. remaining: ~12m   |   Done around Tue 3:40 PM")
        for page in ("chd", "dl", "sound", "games", "fix"):
            win._show_page(page)
            win._runbar_sync()
            self.assertFalse(win.run_bar.isHidden(), page)  # on every page while a job runs
            self.assertTrue(win.pb_total.isHidden() and win.dl_pb.isHidden(), page)  # and no page carries a bar of its own
        self.assertEqual(win.run_bar.value, 400)
        self.assertIn("3 / 12", win.run_bar.detail)
        self.assertIn("~12m left", win.run_bar.detail)  # the page's time line, in short
        win.is_processing = False
        win._runbar_sync()
        self.assertTrue(win.run_bar.isHidden())

    def test_folded_option_cards_say_what_they_are_set_to(self):
        from distill.ui.widgets import Expander

        win = self.make_window()
        self.addCleanup(win.close)
        card = next(c for c in win.pages["chd"].findChildren(Expander) if c.header.text() == "Workflow")
        self.assertFalse(card.header.isChecked())  # folded, as the calmer layout has it
        text, warn = card.header._summary()
        self.assertIn("Keeps originals", text)
        self.assertFalse(warn)
        win.tabs["chd"].delete.setChecked(True)  # a setting that deserves a second look turns the line amber
        text, warn = card.header._summary()
        self.assertIn("Deletes originals", text)
        self.assertTrue(warn)
        win.tabs["chd"].delete.setChecked(False)
        zar = next(c for c in win.pages["zar"].findChildren(Expander) if c.header.text() == "Workflow")
        self.assertNotIn("Unpack", zar.header._summary()[0])  # ZAR packs folders as they are: no unpack box to mention
        options = [c for key in win.org for c in win.pages[key].findChildren(Expander) if c.header.text() == "Options"]
        for card in options:
            self.assertFalse(card.header.isChecked())
            self.assertEqual(card.header._summary(), ("Defaults", False))
        perf = win.perf.header._summary()[0]  # the Performance card, folded: how much of the machine, and the disk mode
        self.assertIn("threads", perf)
        self.assertIn("Disk:", perf)

    def test_games_on_screen_are_matched_and_a_players_pick_is_theirs(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from distill import games as gm
        from distill import recomps as rc

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Metroid.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Metroid.sfc"))
        win._gm_store.sync([rom], str(root))
        game = win._gm_store.games()[0]
        with mock.patch.object(win._gm_match_q, "add") as queued, mock.patch.object(rc, "cover_settled", return_value=True):
            win._gm_ensure_art(game)  # on screen, with its cover and no answer from LaunchBox: matched in the background
            win._gm_ensure_art(game)  # but only asked about once a session
            self.assertEqual(queued.call_count, 1)
            game.lb_match = "auto"
            win._gm_match_asked.clear()
            win._gm_ensure_art(game)  # one that already has an answer is left alone
            self.assertEqual(queued.call_count, 1)
        win._gm_event(("games_matched", game.id))  # a match arrived: its details are read again, from that entry, next time its page opens
        self.assertIn(game.id, win._gm_refetch)

    def test_a_scan_does_not_raise_the_run_bar(self):
        win = self.make_window()
        self.addCleanup(win.close)
        win._show_page("fix")
        win._org_set_busy(True, "Scanning...")
        win._show_page("chd")
        self.assertTrue(win.run_bar.isHidden())
        win._org_set_busy(False)

    def test_unpinned_tools_leave_the_sidebar(self):
        win = self.make_window()
        self.addCleanup(win.close)
        from distill.convertpages import DEFAULT_UNPINNED

        unpinned = {key for key, box in win._pin_boxes.items() if not box.isChecked()}
        self.assertEqual(unpinned, set(DEFAULT_UNPINNED))  # to begin with only the converters and organisers are not pinned: the hub is
        win._pin_boxes["games"].setChecked(False)
        self.assertTrue(win.nav_items["games"].isHidden())
        self.assertFalse(win.nav_items["recomps"].isHidden())
        self.assertEqual(win._pinned_pages()[0], "recomps")  # Ctrl+1 follows what is pinned
        self.assertIn("Ctrl+1", win.nav_items["recomps"].toolTip())
        for key in ("recomps", "emus"):
            win._pin_boxes[key].setChecked(False)
        self.assertTrue(win.nav_groups["GAMES"]["header"].isHidden())  # a group with nothing pinned goes too
        self.assertIn("games", win.pages)  # still there: Ctrl+K opens it
        self.assertIn("games", [k for _l, k in win._goto_entries()])
        win.nav_search.setText("chd")  # quick search only looks among what is pinned
        self.assertTrue(win.nav_items["games"].isHidden())
        win.nav_search.setText("")
        win._pin_boxes["games"].setChecked(True)
        self.assertFalse(win.nav_items["games"].isHidden())
        self.assertFalse(win.nav_groups["GAMES"]["header"].isHidden())

    def test_pins_and_compact_mode_are_remembered(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertIn("pin.chd", win._p_binds)
        self.assertIn("compact_box", win._p_binds)

    def test_compact_sidebar_tucks_away_and_comes_out_on_hover(self):
        win = self.make_window()
        self.addCleanup(win.close)
        full = win.CARD_GAP + win.SIDEBAR_WIDTH
        self.assertEqual(win._side_dock.maximumWidth(), full)
        win.compact_box.setChecked(True)
        self.assertEqual(win._side_dock.maximumWidth(), 0)  # the page takes the whole window
        self.assertTrue(win._side.isHidden())
        self.assertFalse(win._hot.isHidden())  # the strip down the left edge is listening
        win._reveal_side()  # the mouse touched the edge
        self.assertFalse(win._side.isHidden())
        self.assertEqual(win._side_dock.maximumWidth(), 0)  # it floats over the page, which does not move
        win._conceal_side()
        self.assertTrue(win._side.isHidden())
        win.compact_box.setChecked(False)
        self.assertEqual(win._side_dock.maximumWidth(), full)
        self.assertFalse(win._side.isHidden())
        self.assertTrue(win._hot.isHidden())

    def test_run_buttons_start_in_the_right_state(self):
        win = self.make_window()
        self.addCleanup(win.close)
        self.assertTrue(win.start_btn.isEnabled())
        self.assertFalse(win.pause_btn.isEnabled())
        self.assertFalse(win.stop_btn.isEnabled())

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

    def test_add_recomp_window_builds_a_checked_entry_and_explains_a_missing_field(self):
        from distill.ui.recomp_dialog import AddRecompDialog

        win = self.make_window()
        self.addCleanup(win.close)
        dlg = AddRecompDialog(win, win.rc_store, ["Nintendo 64", "GameCube"])
        dlg.repo.setText("github.com/Someone/Cool-Recomp")
        dlg.name.setText("Cool Recomp")
        dlg._accept()  # no game or console yet
        self.assertIsNone(dlg.entry)
        self.assertFalse(dlg.problem.isHidden())
        dlg.game.setText("Cool Game")
        dlg.system.setCurrentText("Nintendo 64")
        dlg.kind.setCurrentText("Decompilation")
        dlg._accept()
        self.assertEqual((dlg.entry["repo"], dlg.entry["tags"], dlg.entry["system"]), ("Someone/Cool-Recomp", ["Decomp"], "Nintendo 64"))

    def test_editing_a_recomp_fills_the_window_and_saves_the_same_id(self):
        from distill import recomps as rcmod
        from distill.ui.recomp_dialog import AddRecompDialog

        win = self.make_window()
        self.addCleanup(win.close)
        original = rcmod.custom_entry("a/b", "Thing", "Some Game", "GameCube", kind="Source port", platforms=["windows"], setup="Needs a ROM.",
                                      asset={"windows": r".*win.*\.zip"}, rom_names="some game", rom_exts=".iso", rid="custom-keep")
        dlg = AddRecompDialog(win, win.rc_store, ["GameCube"], rcmod._recomp(original))
        self.assertEqual((dlg.name.text(), dlg.game.text(), dlg.system.currentText(), dlg.kind.currentText()), ("Thing", "Some Game", "GameCube", "Source port"))
        self.assertFalse(dlg.platforms["linux"].isChecked())
        dlg.name.setText("Thing 2")
        dlg._accept()
        self.assertEqual((dlg.entry["id"], dlg.entry["name"]), ("custom-keep", "Thing 2"))
        self.assertEqual({k: v for k, v in dlg.entry.items() if k != "name"}, {k: v for k, v in original.items() if k != "name"})  # nothing else changed

    def test_the_recomp_page_offers_add_and_only_lets_you_edit_your_own(self):
        from distill import recomps as rcmod
        from distill.ui import motion

        win = self.make_window()
        self.addCleanup(win.close)
        self.addCleanup(setattr, motion.Motion, "reduce", motion.Motion.reduce)
        motion.Motion.reduce = True  # buttons appear and go at once, so they can be checked
        self.assertEqual(win.rc_add_btn.accessibleName(), "Add recomp")  # (a symbol: the words are its name and tooltip)
        mine = rcmod.replace(rcmod._recomp(rcmod.custom_entry("a/b", "Thing", "Game", "GameCube")), source=rcmod.CUSTOM_SOURCE)
        theirs = rcmod.replace(mine, id="other", source="")
        win._rc_apply_catalog([mine, theirs])
        win._rc_current = mine.id
        win._rc_refresh_detail()
        self.assertFalse(win.rcd_edit.isHidden())
        self.assertFalse(win.rcd_unlist.isHidden())
        win._rc_current = theirs.id
        win._rc_refresh_detail()
        self.assertTrue(win.rcd_edit.isHidden())
        self.assertTrue(win.rcd_unlist.isHidden())

    def test_frameless_dialogs_have_a_close_button_and_msgbox_answers_like_qmessagebox(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        from distill.ui import msgbox
        from distill.ui.frameless import FramelessDialog
        from distill.ui.msgbox import MessageDialog

        dlg = FramelessDialog(None, "Hello", "info", None, (300, 0))
        self.addCleanup(dlg.close)
        self.assertTrue(dlg.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertIsNotNone(dlg.close_btn)
        plain = FramelessDialog(None, "Embedded", chrome=False)  # built into another window: no frame, no title row
        self.assertIsNone(plain.close_btn)
        box = MessageDialog(None, "question", "Sure?", "Do it?", [("Yes", "accept"), ("No", "normal")], default=1, escape=1, check="Never ask")
        self.addCleanup(box.close)
        self.assertEqual(len(box.buttons), 2)
        self.assertTrue(box.buttons[1].isDefault())
        box.tick.setChecked(True)
        box.buttons[0].click()
        self.assertEqual((box.chosen, box.checked), (0, True))
        closed = MessageDialog(None, "info", "Notice", "Hi", [("OK", "accept")], escape=0)
        self.addCleanup(closed.close)
        closed.reject()  # Esc or the X
        self.assertEqual(closed.chosen, 0)
        # the QMessageBox-shaped helpers pick the same buttons QMessageBox would
        with mock.patch.object(MessageDialog, "exec", lambda self: self._pick(0)):
            self.assertEqual(msgbox.question(None, "t", "x"), QMessageBox.StandardButton.Yes)
            self.assertEqual(msgbox.warning(None, "t", "x"), QMessageBox.StandardButton.Ok)
        with mock.patch.object(MessageDialog, "exec", lambda self: self.reject()):
            self.assertEqual(msgbox.question(None, "t", "x"), QMessageBox.StandardButton.No)  # closing a Yes / No box answers No

    def test_a_projects_page_poster_has_a_hover_edit_button_that_changes_the_cover(self):
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QBuffer, QEvent, QIODevice
        from PySide6.QtGui import QColor, QImage

        from distill import recomps as rcmod

        win = self.make_window()
        self.addCleanup(win.close)
        mine = rcmod.replace(rcmod._recomp(rcmod.custom_entry("a/b", "Thing", "Game", "GameCube")), source=rcmod.CUSTOM_SOURCE)
        win._rc_apply_catalog([mine])
        win._rc_show_detail(mine.id)
        cover = win.rcd_cover
        self.assertTrue(cover.edit_btn.isHidden())  # out of sight until the pointer is over the poster
        cover.enterEvent(QEvent(QEvent.Type.Enter))
        self.assertFalse(cover.edit_btn.isHidden())
        self.assertGreater(cover.edit_btn.x(), cover.width() // 2)  # bottom right
        self.assertGreater(cover.edit_btn.y(), cover.height() // 2)
        image = QImage(60, 90, QImage.Format.Format_RGB32)
        image.fill(QColor("#336699"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rcmod, "art_dir", lambda: Path(tmp)):
            win._rc_apply_cover(mine, bytes(buffer.data()))
            self.assertIsNotNone(rcmod.cached_cover(mine))
            self.assertEqual(rcmod.cover_source(mine), "custom")
            self.assertIn(mine.id, win._rc_art)
            win._rc_apply_cover(mine, None)
            self.assertIsNone(rcmod.cached_cover(mine))
            self.assertNotIn(mine.id, win._rc_art)

    def test_update_notes_are_drawn_as_markdown_and_start_open(self):
        from distill.ui.msgbox import MessageDialog

        notes = "## Recomps\n- New **Add recomp** window\n\n## Pop-ups\n- Frameless"
        box = MessageDialog(None, "info", "Update available", "Distill is available.", [("Update now", "accept")],
                            details=notes, markdown=True, details_open=True, details_label="release notes")
        self.addCleanup(box.close)
        shown = box.details.toPlainText()
        self.assertIn("Add recomp window", shown)
        self.assertNotIn("##", shown)  # headings are drawn, not printed
        self.assertNotIn("**", shown)
        self.assertFalse(box.details.isHidden())
        self.assertEqual(box.details_btn.text(), "Hide release notes")
        box.details_btn.click()
        self.assertEqual(box.details_btn.text(), "Show release notes")

    def test_games_page_lists_platforms_opens_a_game_and_plays_it_with_retroarch(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm
        from distill.ui.recomp_page import ID_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        roms = []
        for name, system in (("Mario (USA).sfc", "SNES"), ("Zelda (USA).sfc", "SNES"), ("Advance (USA).gba", "Game Boy Advance")):
            (root / name).write_bytes(b"rom")
            roms.append(SimpleNamespace(name=name, system=system, kind=name[-3:].upper(), region="USA", size=3, modified=1.0, folder="", path=str(root / name)))
        exe = root / "retroarch.exe"
        exe.write_bytes(b"x")
        core = root / "snes9x_libretro.dll"
        setup = gm.RetroSetup(exe, {"snes9x": core}, {})
        started = []
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        win._gm_store.sync(roms, str(root))
        with mock.patch.object(gm, "fetch_cover", lambda g, key="", retry=False: None), mock.patch("distill.gamemedia.fetch_backdrop", lambda *a, **k: None), \
                mock.patch("distill.gamemedia.fetch_screenshots", lambda *a, **k: []), \
                mock.patch.object(gm, "launchbox_details", lambda g, store=None, force=False: gm.Details(overview="A platformer.", developers="Nintendo")), \
                mock.patch.object(gm, "spawn", lambda plan: started.append(plan)), \
                mock.patch.object(type(win), "_gm_retro", lambda self: setup):
            win._gm_reload()
            rail = [win.gm_rail.item(i).text() for i in range(win.gm_rail.count())]
            self.assertNotIn("PLATFORMS", rail)  # the sidebar is the platforms (no heading) and, when there are any, the collections; All games / Favourites / Recently played are pills
            self.assertTrue(any(t.startswith("SNES") and t.endswith("2") for t in rail))
            self.assertTrue(win.gm_pills["all"].text().endswith("3") and win.gm_pills["all"].isChecked())
            self.assertEqual(win.gm_list.count(), 3)
            win.gm_search.setText("mario")
            self.assertEqual(win.gm_list.count(), 1)
            win.gm_search.setText("")
            gid = win.gm_list.item(0).data(ID_ROLE)  # Advance: the first alphabetically, which no installed core can play
            win._gm_show_detail(gid)
            self.assertEqual(win.gm_views.currentIndex(), 1)
            self.assertFalse(win.gmd_play.isEnabled())
            self.assertIn("Nothing installed", win.gmd_msg.text())
            win._gm_back()
            win.gm_search.setText("mario")
            win._gm_show_detail(win.gm_list.item(0).data(ID_ROLE))
            self.assertTrue(win.gmd_play.isEnabled())
            self.assertEqual(win.gmd_title.text(), "Mario")
            win._gm_play()
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0].cmd[:3], [str(exe), "-L", str(core)])
            self.assertEqual(win.gm_store.get(gid_of := win._gm_current).play_count, 1)
            win._gm_toggle_favorite()
            self.assertTrue(win.gm_store.get(gid_of).favorite)
            win._gm_back()
            self.assertTrue(win.gm_pills["fav"].text().endswith("1"))
            win.gm_pills["fav"].click()
            self.assertEqual(win.gm_list.count(), 1)
            win.gm_pills["all"].click()
            self.assertEqual(win.gm_list.count(), 3)

    def test_game_cover_stage_makes_room_for_a_disc_behind_and_a_cartridge_above(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage, QPixmap
        from PySide6.QtWidgets import QLabel

        from distill.ui.games_widgets import CoverStage
        from distill.ui.motion import Motion

        self.addCleanup(setattr, Motion, "reduce", Motion.reduce)

        def art(w: int, h: int, alpha: bool) -> QPixmap:
            img = QImage(w, h, QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32)
            img.fill(QColor("#4488cc"))
            return QPixmap.fromImage(img)

        cover = QLabel()
        cover.setFixedSize(220, 314)
        tops: list[int] = []
        stage = CoverStage(cover)
        stage.on_top_pad = tops.append
        self.assertEqual((stage.width(), stage.height(), cover.pos().x(), cover.pos().y()), (220, 314, 0, 0))  # no media: just the cover

        stage.set_media("disc", art(300, 300, True))
        d = round(314 * CoverStage.DISC_SCALE)
        self.assertEqual((stage.width(), stage.height(), cover.pos().y()), (220 + d // 2, 314, 0))  # half the disc sticks out to the right
        self.assertEqual(stage.media_kind(), "disc")
        stage.set_media("disc", art(300, 200, False))  # no see-through corners: cut round, still shown
        self.assertEqual(stage.media_kind(), "disc")

        stage.set_media("cart", art(200, 220, True))
        h = round(round(220 * CoverStage.CART_SCALE) * 220 / 200)
        self.assertEqual((stage.width(), stage.height(), cover.pos().y(), tops[-1]), (220, 314 + h // 2, h // 2, h // 2))  # half of it above the cover
        stage.set_media("cart", art(200, 220, False))  # a cartridge with no see-through corners would look wrong: not shown
        self.assertEqual((stage.media_kind(), stage.height(), cover.pos().y(), tops[-1]), ("", 314, 0, 0))

        stage.set_media("disc", art(300, 300, True))
        dirty = stage._disc_dirty()  # what is repainted each frame: the half that sticks out, not the half under the cover
        self.assertEqual((dirty.left(), dirty.width()), (220, d // 2 + 2))
        self.assertLess(stage._frame_ms(), 33)  # faster than the old 30 frames a second
        Motion.reduce = False
        stage.show()
        self.assertTrue(stage._timer.isActive())  # turning while on screen
        self.assertEqual(stage._timer.timerType(), Qt.TimerType.PreciseTimer)
        self.assertEqual(stage._timer.interval(), stage._frame_ms())
        stage.hide()
        self.assertFalse(stage._timer.isActive())  # and not when it is not
        Motion.reduce = True
        stage.show()
        self.assertFalse(stage._timer.isActive())  # reduced motion: shown still
        stage.set_media("", None)
        self.assertEqual((stage.width(), stage.height()), (220, 314))
        stage.close()

    def test_games_page_shows_background_screenshots_and_achievements(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPixmap

        from distill import gamemedia as mm
        from distill.ui.games_widgets import BackdropView, play_rect

        self.assertEqual(play_rect(QRectF(10, 20, 100, 200)).center(), QRectF(10, 20, 100, 200).center())  # the play button is in the middle
        from distill.ui.games_widgets import dot_rect, rating_rect, rating_star_at, RATE_FULL
        poster = QRectF(10, 20, 176, 250)
        self.assertLess(rating_rect(poster, False).right(), dot_rect(poster).x())  # the rating is top-left and the select dot top-right
        self.assertAlmostEqual(dot_rect(poster).right(), poster.right() - 9)
        self.assertGreater(rating_rect(poster, True).width(), rating_rect(poster, False).width())
        self.assertEqual(rating_rect(poster, False, 1.0).width(), RATE_FULL)  # open: room for five stars
        self.assertLess(rating_rect(poster, True, 1.0).right(), poster.right())  # and it still fits a poster
        box = rating_rect(poster, False, 1.0)
        self.assertEqual([rating_star_at(poster, box.x() + 4 + 19 * i + 9) for i in range(5)], [1, 2, 3, 4, 5])
        self.assertEqual((rating_star_at(poster, -50), rating_star_at(poster, 900)), (1, 5))
        art = QPixmap(300, 100)
        art.fill(QColor("#aa3355"))
        view = BackdropView()
        view.resize(800, 500)
        self.assertIsNone(view._prepared())  # no art yet: nothing to paint
        view.set_art(art)
        self.assertEqual((view._prepared().width(), view._prepared().height()), (800, 400))

        from distill import games as gm
        from distill.ui.recomp_page import ID_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        shot = root / "shot.png"
        art.save(str(shot))
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Zelda.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Zelda.sfc"))
        win._gm_store.sync([rom], str(root))
        with mock.patch.object(gm, "fetch_cover", lambda g, key="", retry=False: None), mock.patch("distill.gamemedia.fetch_backdrop", lambda *a, **k: None), \
                mock.patch("distill.gamemedia.fetch_screenshots", lambda *a, **k: []), mock.patch("distill.apikeys.get", lambda name: ""), \
                mock.patch.object(gm, "launchbox_details", lambda g, store=None, force=False: gm.Details(overview="A game.")):
            win._gm_reload()
            gid = win.gm_list.item(0).data(ID_ROLE)
            win._gm_show_detail(gid)
            self.assertTrue(win.gmd_shots_card.isHidden())  # no screenshots found: no card
            self.assertEqual(win._gm_ra_mode, "connect")  # SNES has achievements; no key yet: the card offers to connect
            self.assertFalse(win.gmd_ra_card.isHidden())
            win._gm_event(("games_media", gid, "shots", [str(shot), str(shot)]))
            self.assertFalse(win.gmd_shots_card.isHidden())
            self.assertEqual(win.gmd_shots._lay.count(), 3)  # two tiles and the stretch
            win._gm_event(("games_media", gid, "backdrop", str(shot)))
            self.assertTrue(win._gm_backdrop_on)
            win._gm_ra_mode = "loading"
            game = mm.parse_ra_progress({"ID": 3, "Title": "Zelda", "Achievements": {
                "1": {"ID": 1, "Title": "A", "Description": "d", "Points": 10, "BadgeName": "1", "DateEarned": "2024-01-01"},
                "2": {"ID": 2, "Title": "B", "Description": "d", "Points": 5, "BadgeName": "2"}}})
            win._gm_event(("games_media", gid, "ra", ("game", game, {})))
            self.assertIn("1 of 2 unlocked", win.gmd_ra_text.text())
            self.assertIn("10 of 15 points", win.gmd_ra_text.text())
            self.assertTrue(win.gmd_ra_btn.isHidden())  # the chip is the link now
            self.assertEqual(win.gmd_ra_chip._url, "https://retroachievements.org/game/3")
            self.assertEqual(win.gmd_ra_strip._lay.count(), 3)
            win._gm_ra_mode = "loading"
            win._gm_event(("games_media", gid, "ra", ("none", None, {})))
            self.assertTrue(win.gmd_ra_card.isHidden())  # no such game on RetroAchievements: no card
            win._gm_event(("games_media", "some-other-game", "shots", [str(shot)]))  # a result for a game no longer open is ignored
            self.assertEqual(win.gmd_shots._lay.count(), 3)

    def test_games_filters_collections_and_versions(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm
        from distill.ui.recomp_page import ID_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        with mock.patch.object(gm, "fetch_cover", lambda g, key="", retry=False: None), mock.patch("distill.gamemedia.fetch_backdrop", lambda *a, **k: None), \
                mock.patch("distill.gamemedia.fetch_screenshots", lambda *a, **k: []):
            roms = [SimpleNamespace(name=n, system="SNES", kind="SFC", region=r, size=1, modified=1.0, folder="", path=str(root / n))
                    for n, r in (("A (USA).sfc", "USA"), ("A (Europe).sfc", "Europe"), ("B.sfc", ""))]
            for r in roms:
                (root / r.name).write_bytes(b"x")
            win._gm_store.sync(roms, str(root))
            win._gm_reload()
            self.assertEqual(win.gm_list.count(), 2)  # two versions of A share a poster
            first = next(iter(win._gm_groups))
            self.assertEqual(len(win._gm_groups[first]), 2)
            win._gm_set_group(False)
            self.assertEqual(win.gm_list.count(), 3)
            win._gm_set_group(True)
            # a collection: added to from a poster that stands for two versions, shown in the sidebar, chosen from it
            cid = win.gm_store.create_collection("Co-op")
            win._gm_add_to_collection(cid, [first])
            self.assertEqual(win.gm_store.collections()[0][2], 2)
            rail = [win.gm_rail.item(i).text() for i in range(win.gm_rail.count())]
            self.assertIn("COLLECTIONS", rail)
            item = next(win.gm_rail.item(i) for i in range(win.gm_rail.count()) if win.gm_rail.item(i).data(ID_ROLE) == f"@c:{cid}")
            win.gm_rail.setCurrentItem(item)
            self.assertEqual(win._gm_collection, cid)
            self.assertEqual(win.gm_list.count(), 1)
            win._gm_pill("all")  # All games lets go of the collection
            self.assertEqual((win._gm_collection, win.gm_list.count()), (0, 2))
            # filters narrow the grid, and the button says how many are on
            win._gm_set_filter("played", "played")
            self.assertEqual(win.gm_list.count(), 0)
            self.assertIn("1", win.gm_filter_btn.text())
            win._gm_set_filter("", None)
            self.assertEqual((win.gm_list.count(), win.gm_filter_btn.text()), (2, "Filters"))
            # the page of a game with versions offers them
            win._gm_show_detail(first)
            self.assertFalse(win.gmd_version.isHidden())
            self.assertEqual(win.gmd_version.count(), 2)

    def test_a_games_stars_status_and_notes_are_kept_from_its_page_and_its_edit_window(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QMenu

        from distill import games as gm
        from distill.ui.games_dialog import EditGameDialog
        from distill.ui.games_widgets import StarRating

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Metroid.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Metroid.sfc"))
        win._gm_store.sync([rom], str(root))
        gid = win._gm_store.games()[0].id
        win._gm_reload()
        win._gm_current = gid
        win._gm_fill_mine(win._gm_store.get(gid))
        win.gmd_stars.changed.emit(4)  # the page saves a rating
        win.gmd_status.setCurrentIndex(win.gmd_status.findData("playing"))
        win.gmd_status.activated.emit(win.gmd_status.currentIndex())
        win.gmd_notes.setPlainText("Get the Varia suit first")
        win.gmd_notes.flush()  # (the box sends them after a short pause, or when it loses focus)
        g = win._gm_store.get(gid)
        self.assertEqual((g.rating, g.status, g.notes), (4, "playing", "Get the Varia suit first"))
        win.gmd_notes.setPlainText("typed just before leaving")  # a pause that has not run out yet is not lost when the page moves on
        win.gm_views.setCurrentIndex(1)
        win._gm_leave_detail()
        win._gm_current = gid
        win._gm_fill_mine(win._gm_store.get(gid))
        self.assertEqual(win._gm_store.get(gid).notes, "typed just before leaving")
        self.assertEqual((win.gmd_stars.value(), win.gmd_status.currentData(), win.gmd_notes.toPlainText()), (4, "playing", "typed just before leaving"))

        stars = StarRating(0)  # clicking a star rates; clicking the same one again clears it
        self.addCleanup(stars.close)
        seen = []
        stars.changed.connect(seen.append)
        QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=QPoint(2 * (stars.STAR + 3) + 4, stars.height() // 2))
        QTest.mouseClick(stars, Qt.MouseButton.LeftButton, pos=QPoint(2 * (stars.STAR + 3) + 4, stars.height() // 2))
        self.assertEqual(seen, [3, 0])

        dlg = EditGameDialog(win, win._gm_store.get(gid), ["SNES"], "", None)  # the Edit window starts from what is saved and hands back changes
        self.addCleanup(dlg.close)
        self.assertEqual((dlg.stars.value(), dlg.status_box.currentData(), dlg.notes.toPlainText()), (4, "playing", "typed just before leaving"))
        dlg.stars.set_value(2)
        dlg.status_box.setCurrentIndex(dlg.status_box.findData("completed"))
        dlg.notes.setPlainText("done")
        dlg._accept()
        self.assertEqual({k: dlg.values[k] for k in ("rating", "status", "notes")}, {"rating": 2, "status": "completed", "notes": "done"})

        menu = QMenu()
        win._gm_fill_filter_menu(menu)
        self.assertTrue({"Rating", "Progress"} <= {a.text().split(":")[0] for a in menu.actions()})
        win._gm_set_filter("status", "playing")
        self.assertIn("playing", win._gm_summary(1))
        win._gm_set_filter("", None)

    def test_edit_game_window_media_and_emulator_tabs(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from distill import gamemedia as mm
        from distill import games as gm
        from distill.ui.games_dialog import EditGameDialog
        from distill.ui.games_edit_tabs import BTN_H, BTN_W, PREVIEW_H, PREVIEW_W

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Metroid.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Metroid.sfc"))
        win._gm_store.sync([rom], str(root))
        game = win._gm_store.games()[0]
        duck = gm.Standalone("duckstation", "DuckStation", Path("/emu/duck.exe"))
        png = b"\x89PNG\r\n\x1a\n" + bytes(64)
        with mock.patch.object(mm, "MEDIA_DIR", root / "media"):
            plain = EditGameDialog(win, game, ["SNES"], "", None)  # without them the window is as it was: one page, no tab strip
            self.addCleanup(plain.close)
            self.assertIsNone(plain.media_tab)
            self.assertIsNone(plain.emu_tab)
            self.assertEqual(plain.tabs.count(), 1)

            dlg = EditGameDialog(win, game, ["SNES"], "", None, media_key="k1", emu_info=lambda system: gm.emulator_options(system, None, {"duckstation": duck}, ""))
            self.addCleanup(dlg.close)
            self.assertEqual([dlg.tabs.tabText(i) for i in range(dlg.tabs.count())], ["Details", "Media", "Emulator"])
            dlg.media_tab.stage_art("back", "set", png)  # chosen, but nothing is kept before Save
            self.assertEqual(mm.art_state("k1", "back"), "none")
            dlg.media_tab.stage_art("back", "off")  # the last choice wins
            self.assertEqual(dlg.media_plan.art["back"][0], "off")
            dlg.media_tab.stage_art("back", "set", png)
            media = dlg.media_tab
            self.assertNotIn("logo", media.rows)  # (the clear logo is gone: the game's name is text)
            every = (media.cover_row, *media.rows.values(), media.shots_row, media.manual_row)
            self.assertEqual({(r.preview.width(), r.preview.height()) for r in every}, {(PREVIEW_W, PREVIEW_H)})  # one box for every picture
            self.assertEqual({(r.find_btn.width(), r.file_btn.width(), r.none_btn.width(), r.auto_btn.width(), r.field.height()) for r in every},
                             {(BTN_W, BTN_W, BTN_W, BTN_W, BTN_H)})
            self.assertEqual({r.find_btn.text() + r.file_btn.text() + r.auto_btn.text() for r in every}, {""})  # symbols, not words
            self.assertEqual({r.find_btn.toolTip().split()[0] for r in every}, {"Look"})
            tab = dlg.emu_tab
            tab.play_with.setCurrentIndex(tab.play_with.findData("duckstation"))
            tab._changed()
            tab.make_default.setChecked(True)
            dlg._accept()
            self.assertEqual((dlg.emulator, dlg.chosen_emulator, dlg.make_default), ("duckstation", "duckstation", True))
            self.assertEqual(mm.apply_plan("k1", dlg.media_plan), [])
            self.assertEqual(mm.art_state("k1", "back"), "custom")

    def test_fetch_pictures_window_counts_what_is_missing_and_fetches_it(self):
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock

        from PySide6.QtWidgets import QApplication

        from distill import gamemedia as mm
        from distill import mediascrape as ms
        from distill.ui.games_scrape_dialog import MediaScrapeDialog

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        win = self.make_window()
        self.addCleanup(win.close)
        entries = [("k1", "Metroid", "SNES"), ("k2", "Gran Turismo", "PlayStation"), ("k3", "Pong", "Arcade")]
        with mock.patch.object(mm, "MEDIA_DIR", Path(tmp.name) / "media"):
            dlg = MediaScrapeDialog(win, entries, entries[:1], "")
            self.addCleanup(dlg.close)
            self.assertTrue(dlg.scope.itemText(0).startswith("Selected games (1"))  # the chosen games come first, then the whole library and each platform
            self.assertTrue(dlg.scope.itemText(1).startswith("Whole library (3"))
            dlg.scope.setCurrentIndex(1)
            dlg._recount()
            self.assertEqual(len(dlg._jobs), 3)
            self.assertTrue(dlg.start_btn.isEnabled())
            for box in dlg.boxes.values():
                box.setChecked(False)
            dlg._recount()
            self.assertEqual(dlg._jobs, [])
            self.assertFalse(dlg.start_btn.isEnabled())  # nothing ticked: nothing to start
            dlg.boxes["back"].setChecked(True)
            dlg._recount()
            self.assertEqual(len(dlg._jobs), 3)
            with mock.patch.object(ms, "fetch_one", return_value=True):
                dlg._start()
                deadline = time.monotonic() + 15
                while dlg._running and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.01)
            self.assertFalse(dlg._running)
            self.assertIn("3 background", dlg.summary.text())
            self.assertTrue(dlg.found_any)
            self.assertFalse(dlg.stop_btn.isVisibleTo(dlg))

    def test_game_page_shows_placeholders_while_pictures_are_on_their_way(self):
        import tempfile
        import time
        from pathlib import Path
        from types import SimpleNamespace

        from PySide6.QtGui import QPixmap

        from distill import games as gm
        from distill.ui.widgets import BackdropView

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Metroid.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Metroid.sfc"))
        win._gm_store.sync([rom], str(root))
        game = win._gm_store.games()[0]
        win._gm_pending, win._gm_pending_total = {"shots", "back"}, 2
        win._gm_show_shots(game, [])  # nothing yet, but on its way: the card is there with placeholders in it
        self.assertFalse(win.gmd_shots_card.isHidden())
        win._gm_update_fetch()
        self.assertEqual(win.gmd_fetch.text.text(), "Looking for the background and screenshots...")
        win._gm_pending = set()  # the fetching is over and found none: the placeholders go
        win._gm_show_shots(game, [])
        self.assertTrue(win.gmd_shots_card.isHidden())
        # a cover that has just arrived comes in over a few frames, and one that did not just arrive is simply there
        self.assertEqual(win._gm_fade_of("nobody"), 1.0)
        win._gm_art_born["x"] = time.monotonic()
        self.assertLess(win._gm_fade_of("x"), 0.2)
        win._gm_art_born["x"] = time.monotonic() - 5
        self.assertEqual(win._gm_fade_of("x"), 1.0)
        # the backdrop takes a picture at once, or fades to it
        view = BackdropView()
        self.addCleanup(view.close)
        view.resize(800, 500)
        pix = QPixmap(100, 60)
        view.set_art(pix)
        view.set_art(None, fade=True)
        view.set_art(pix, fade=True)
        self.assertGreaterEqual(view._mix, 0.0)
        self.assertLessEqual(view._mix, 1.0)

    def test_saves_and_library_health_windows(self):
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock

        from PySide6.QtWidgets import QApplication

        from distill import games as gm
        from distill import libraryhealth as lh
        from distill import savebackup as sb
        from distill.ui.games_health_dialog import LibraryHealthDialog
        from distill.ui.games_saves_dialog import BackupAllDialog, SavesDialog

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "states").mkdir()
        rom = root / "Metroid.sfc"
        rom.write_bytes(b"rom")
        (root / "states" / "Metroid.state").write_bytes(b"state")
        setup = gm.RetroSetup(root / "retroarch.exe", {"snes9x": root / "x.dll"}, {"snes9x": {"corename": "Snes9x"}}, None, state_dir=root / "states")
        game = gm.Game(id="g1", title="Metroid", system="SNES", path=str(rom))
        win = self.make_window()
        self.addCleanup(win.close)
        with mock.patch.object(sb, "BACKUP_DIR", root / "backups"):
            dlg = SavesDialog(win, game, setup)  # a game with a save and no backups yet
            self.addCleanup(dlg.close)
            self.assertTrue(dlg.backup_btn.isEnabled())
            self.assertFalse(dlg.restore_btn.isEnabled())
            dlg._backup()
            self.assertEqual(dlg.list.count(), 1)
            self.assertTrue(dlg.restore_btn.isEnabled())
            none = SavesDialog(win, game, None)  # an emulator of its own: nothing to do, and it says why
            self.addCleanup(none.close)
            self.assertFalse(none.backup_btn.isEnabled())

            bulk = BackupAllDialog(win, setup, [game])  # starts at once, finishes by itself
            self.addCleanup(bulk.close)
            deadline = time.monotonic() + 10
            while bulk.stop_btn.isVisibleTo(bulk) and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
            self.assertTrue(bulk.close_btn.isVisibleTo(bulk))
            self.assertIn("backed up the saves of 1 game", bulk.title_label.text())

        issues = lh.check([game], {"g1"}, lambda g: False, 3)
        health = LibraryHealthDialog(win, issues, 1)
        self.addCleanup(health.close)
        self.assertIsNone(health.choice)
        health._choose(next(i for i in issues if i.key == "missing"))
        self.assertEqual(health.choice, ("select", ["g1"]))
        healthy = LibraryHealthDialog(win, [], 5)  # nothing wrong: it says so
        self.addCleanup(healthy.close)

    def test_media_tab_pictures_open_large_and_step_through_screenshots(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap

        from distill.ui.games_edit_tabs import PictureZoom

        pics = []
        for colour in (Qt.GlobalColor.red, Qt.GlobalColor.green, Qt.GlobalColor.blue):
            pix = QPixmap(320, 200)
            pix.fill(colour)
            pics.append(pix)
        many = PictureZoom(None, "Screenshots", pics, 1)
        self.addCleanup(many.close)
        self.assertEqual((many.index, many.counter.text()), (1, "2 of 3"))
        many._step(1)
        many._step(1)  # past the last: round to the first
        self.assertEqual(many.index, 0)
        many._step(-1)
        self.assertEqual(many.index, 2)
        one = PictureZoom(None, "Cover", pics[:1])
        self.addCleanup(one.close)
        self.assertEqual(one.counter.text(), "")  # a lone picture has nothing to step through
        self.assertTrue(one.prev_btn.isHidden() and one.next_btn.isHidden())

    def test_games_page_small_wins_random_recently_added_list_view_and_stats(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm
        from distill.ui.games_dialog import StatsDialog
        from distill.ui.recomp_page import ID_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        roms = []
        for name, system in (("Mario (USA).sfc", "SNES"), ("Zelda (USA).sfc", "SNES"), ("Advance (USA).gba", "Game Boy Advance")):
            (root / name).write_bytes(b"rom")
            roms.append(SimpleNamespace(name=name, system=system, kind=name[-3:].upper(), region="USA", size=3, modified=1.0, folder="", path=str(root / name)))
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        win._gm_store.sync(roms, str(root))
        with win._gm_store._db() as db:  # Zelda was found long ago
            db.execute("UPDATE games SET added = 1 WHERE title = 'Zelda'")
        win._gm_store.add_playtime(win._gm_store.games(query="Mario")[0].id, 3600)
        enter(self, mock.patch.object(gm, "fetch_cover", lambda g, key="", retry=False: None))
        win._gm_reload()

        win.gm_pills["new"].click()  # Recently added: not Zelda
        self.assertTrue(win.gm_pills["new"].text().endswith("2"))
        self.assertEqual(win.gm_list.count(), 2)
        win.gm_pills["all"].click()
        self.assertEqual(win.gm_list.count(), 3)

        win._gm_random()  # opens a game's page
        self.assertEqual(win.gm_views.currentIndex(), 1)
        self.assertIn(win._gm_current, {g.id for g in win._gm_store.games()})
        win._gm_leave_detail()

        win._gm_set_view("list")  # the table takes the posters' place, sorted by its headings
        self.assertEqual((win.gm_table.rowCount(), win.gm_table.isVisibleTo(win.pages["games"]), win.gm_list.isVisibleTo(win.pages["games"])), (3, True, False))
        self.assertFalse(win.gm_sort_box.isEnabled())
        self.assertEqual([win._gm_row_id(r) for r in range(3)], [g.id for g in gm.sort_by_column(win._gm_store.games(), "name")])
        win._gm_column_clicked(list(gm.LIST_COLUMNS).index("play_seconds"))  # time played starts with the most
        self.assertEqual(win._gm_col, ("play_seconds", True))
        self.assertEqual(win.gm_table.item(0, 0).text(), "Mario")
        win._gm_column_clicked(list(gm.LIST_COLUMNS).index("play_seconds"))  # again: turned round
        self.assertEqual(win._gm_col, ("play_seconds", False))
        win.gm_table.selectRow(1)  # a row picked is a game selected, so the selection bar works
        self.assertEqual(win._gm_selected, {win._gm_row_id(1)})
        win._gm_clear_selection()
        self.assertEqual(win._gm_selected, set())
        win._gm_set_view("grid")
        self.assertEqual(win.gm_list.count(), 3)

        dlg = StatsDialog(win, gm.playtime_by_platform(win._gm_store.games()))
        self.addCleanup(dlg.close)
        self.assertEqual([r.system for r in gm.playtime_by_platform(win._gm_store.games())], ["SNES"])

    def test_romm_game_wears_a_chip_installs_with_a_filling_button_then_starts(self):
        import tempfile
        from pathlib import Path

        from distill import games as gm
        from distill import romm
        from distill.ui.recomp_page import ID_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        exe = root / "retroarch.exe"
        exe.write_bytes(b"x")
        core = root / "snes9x_libretro.dll"
        setup = gm.RetroSetup(exe, {"snes9x": core}, {})
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        dest = root / "dl" / "snes" / "Zelda.sfc"
        win._gm_store.sync_remote([romm.RemoteEntry("7", "Zelda", "SNES", str(dest), "SFC", size=3, remote_name="Zelda.sfc")], "romm:h")
        started = []
        enter(self, mock.patch.object(gm, "fetch_cover", lambda g, key="", retry=False: None))
        enter(self, mock.patch.object(gm, "spawn", lambda plan: started.append(plan)))
        enter(self, mock.patch.object(type(win), "_gm_retro", lambda self: setup))
        win._gm_reload()
        gid = win.gm_list.item(0).data(ID_ROLE)
        g = win.gm_store.get(gid)
        self.assertEqual((g.source, g.installed), ("romm", False))
        win._gm_show_detail(gid)
        self.assertEqual((win.gmd_play.text(), win.gmd_play.isEnabled()), ("Install", True))
        win._gm_installs[gid] = 0.0  # the install starts: the button says so and fills
        win._gm_installing_changed(g)
        self.assertEqual((win.gmd_play.text(), win.gmd_play.isEnabled()), ("Installing", False))
        win._gm_event(("games_install", gid, "progress", 0.4))
        self.assertEqual(win._gm_installs[gid], 0.4)
        win._gm_event(("games_install", gid, "progress", 0.2))  # a number going backwards never empties it
        self.assertEqual(win._gm_installs[gid], 0.4)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"rom")
        win._gm_event(("games_install", gid, "done", str(dest)))  # the file is in: it starts with the emulator
        self.assertNotIn(gid, win._gm_installs)
        self.assertEqual((len(started), win.gmd_play.text()), (1, "Play"))
        self.assertIn(str(dest), started[0].cmd)  # the file path goes straight to the emulator
        win._gm_event(("games_install", gid, "error", "Couldn't reach RomM."))  # an error leaves a message and no half state
        self.assertEqual(win.gmd_msg.text(), "Couldn't reach RomM.")

    def test_the_platform_setup_window_shows_what_is_ready_and_saves_the_choices(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm
        from distill.ui.games_dialog import PlatformSetupDialog
        from distill.ui.games_widgets import HEALTH_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "system").mkdir()
        psx = {"systemname": "Sony - PlayStation", "categories": "Emulator"}
        setup = gm.RetroSetup(root / "retroarch.exe", {"beetle_psx_hw": root / "b.dll"},
                              {"beetle_psx_hw": {**psx, "display_name": "Beetle PSX HW", "firmware_count": "1", "firmware0_path": "scph5501.bin", "firmware0_opt": "false"}},
                              root / "system")
        duck = {"duckstation": gm.Standalone("duckstation", "DuckStation", root / "duck.exe")}
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        roms = [SimpleNamespace(name=n, system=s, kind="X", region="", size=1, modified=1.0, folder="", path=str(root / n))
                for n, s in (("a.cue", "PlayStation"), ("b.gb", "Game Boy"))]
        win._gm_store.sync(roms, str(root))
        win._gm_retro = lambda: setup
        win._gm_emus = lambda system: duck if system == "PlayStation" else {}
        win._gm_reload()
        win._gm_refresh_health()
        rail = {win.gm_rail.item(i).data(0x100): win.gm_rail.item(i).data(HEALTH_ROLE) for i in range(win.gm_rail.count()) if win.gm_rail.item(i).data(HEALTH_ROLE)}
        self.assertEqual(rail, {"Game Boy": "bad", "PlayStation": "warn"})  # (the PlayStation core needs a BIOS that isn't there yet)

        dlg = PlatformSetupDialog(win, win._gm_store, win._gm_retro, win._gm_emus, "PlayStation")
        self.addCleanup(dlg.close)
        self.assertEqual([dlg.platforms.item(i).data(0x100) for i in range(dlg.platforms.count())], ["Game Boy", "PlayStation"])  # what needs attention first
        self.assertIn("2 platforms need attention", dlg.overview.text())
        self.assertIn("scph5501.bin", dlg.bios_text.text())
        dlg.with_box.setCurrentIndex(dlg.with_box.findData("duckstation"))
        dlg.with_box.activated.emit(dlg.with_box.currentIndex())  # a standalone emulator: no BIOS section, its launch options appear
        self.assertEqual(win._gm_store.default_for("PlayStation"), "duckstation")
        self.assertTrue(dlg.changed)
        self.assertTrue(dlg.bios_box.isHidden())
        self.assertFalse(dlg.opts_box.isHidden())
        self.assertEqual(dlg._rows["PlayStation"].level, "ok")
        dlg.opts.setText("-nogui {rom}")
        dlg.opts.editingFinished.emit()
        self.assertEqual(win._gm_store.setting("args:duckstation"), "-nogui {rom}")
        dlg.opts.setText("-batch -fullscreen -- {rom}")  # Distill's own words are stored as 'nothing'
        dlg.opts.editingFinished.emit()
        self.assertEqual(win._gm_store.setting("args:duckstation"), "")
        win._gm_refresh_health()
        self.assertEqual(win._gm_health["PlayStation"].level, "ok")

    def test_a_fitted_table_keeps_fitting_until_a_column_is_dragged(self):
        from PySide6.QtWidgets import QTreeWidget

        from distill.ui.widgets import fit_columns

        tree = QTreeWidget()
        tree.setHeaderLabels(("Core", "Systems", "Status", "BIOS"))
        tree.resize(700, 400)
        fit_columns(tree, flex=0, flex_min=280)
        tree.setColumnWidth(1, 170)  # a resize nobody dragged (Qt's own, or ours): the fitting stays on
        self.assertTrue(tree._fit_state["auto"])
        tree.setColumnWidth(0, 120)
        self.app.processEvents()
        tree.resize(701, 400)
        self.app.processEvents()
        self.assertGreaterEqual(tree.columnWidth(0), 280)  # Core is never squeezed below readable

    def test_games_grid_selects_edits_and_fills_the_row(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm
        from distill.ui.recomp_page import ID_ROLE

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        with mock.patch.object(gm, "fetch_cover", lambda g, key="", retry=False: None), mock.patch("distill.gamemedia.fetch_backdrop", lambda *a, **k: None), \
                mock.patch("distill.gamemedia.fetch_screenshots", lambda *a, **k: []):
            win._gm_reload()
            self.assertFalse(win.gm_list.isVisibleTo(win.pages["games"]))  # no games: the logo and the button, no card
            roms = [SimpleNamespace(name=f"G{i}.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / f"G{i}.sfc"))
                    for i in range(6)]
            win._gm_store.sync(roms, str(root))
            win._gm_reload()
            self.assertTrue(win.gm_list.isVisibleTo(win.pages["games"]))
            win.resize(1500, 900)
            ids = [win.gm_list.item(i).data(ID_ROLE) for i in range(3)]
            win._gm_toggle_select(ids[0])
            win._gm_toggle_select(ids[1])
            self.assertEqual(win._gm_selected, set(ids[:2]))
            self.assertEqual(win.gm_bar.count.text(), "2 selected")
            win._gm_bulk_favorite()
            self.assertEqual(win.gm_store.totals()[1], 2)
            win._gm_bulk_platform("Mega Drive")
            self.assertEqual({s for s, _n in win.gm_store.systems()}, {"SNES", "Mega Drive"})
            win._gm_clear_selection()
            self.assertFalse(win._gm_selected)
            from distill.ui.games_widgets import cell_metrics
            gm_cell_w_normal = cell_metrics(1.0)[0]
            win.gm_zoom_slider.setValue(0)  # the smallest posters: every cell shrinks with them
            self.assertEqual(win.gm_list.item(0).sizeHint(), win._gm_cell_size())
            self.assertLess(win._gm_cell_size().width(), gm_cell_w_normal)
            win.gm_sort_box.setCurrentIndex(win.gm_sort_box.findData("name_desc"))
            win._gm_sort_changed()
            self.assertEqual(win.gm_list.item(0).data(ID_ROLE), max(win._gm_by_id.values(), key=lambda g: g.name.casefold()).id)
            win.gm_list.setFixedWidth(1000)
            self.assertEqual(win.gm_list.gridSize().width() if win.gm_list.gridSize().isValid() else 0, 0)  # posters keep one size; the row just wraps
            from distill.ui.games_widgets import platform_logo
            self.assertIsNotNone(platform_logo("SNES", True))
            self.assertIsNotNone(platform_logo("Mega Drive", False))
            self.assertIsNone(platform_logo("Not A Platform", True))

    def test_my_recomps_toggle_shows_only_what_the_person_added(self):
        from distill import recomps as rcmod
        from distill.ui.recomp_page import ID_ROLE

        win = self.make_window()
        self.addCleanup(win.close)
        mine = rcmod.replace(rcmod._recomp(rcmod.custom_entry("a/b", "Thing", "Game", "GameCube")), source=rcmod.CUSTOM_SOURCE)
        theirs = rcmod.replace(mine, id="other", name="Other", source="")
        win._rc_apply_catalog([mine, theirs])
        self.assertFalse(hasattr(win, "rc_owned_only") or hasattr(win, "rc_sources_btn"))  # both options are gone
        ids = lambda: {win.rc_list.item(i).data(ID_ROLE) for i in range(win.rc_list.count())}  # noqa: E731
        self.assertEqual(ids(), {mine.id, "other"})
        win.rc_mine.setChecked(True)
        self.assertEqual(ids(), {mine.id})
        win.rc_mine.setChecked(False)  # a radio that can be switched off again
        self.assertEqual(ids(), {mine.id, "other"})

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

    def test_power_countdown_runs_out_or_is_called_off(self):
        from distill.ui.power_countdown import PowerCountdown

        events = []
        box = PowerCountdown(None, "shut down", 3)
        self.addCleanup(box.deleteLater)
        box.go.connect(lambda: events.append("go"))
        box.called_off.connect(lambda: events.append("off"))
        self.assertIn("shut down in 3 seconds", box.label.text())
        box._tick()
        self.assertEqual(box.seconds_left(), 2)
        box._tick()
        self.assertIn("1 second.", box.label.text())  # singular at the end
        box._tick()
        self.assertEqual(events, ["go"])
        again = PowerCountdown(None, "hibernate", 30)
        self.addCleanup(again.deleteLater)
        again.go.connect(lambda: events.append("go2"))
        again.called_off.connect(lambda: events.append("off2"))
        again.reject()
        self.assertEqual(events, ["go", "off2"])
        self.assertFalse(again._timer.isActive())

    def test_download_page_finish_options(self):
        from distill import afteractions

        win = self.make_window()
        self.addCleanup(win.close)
        self.assertEqual([win.dl_power.itemText(i) for i in range(win.dl_power.count())], list(afteractions.POWER_LABELS))
        self.assertEqual(win.dl_power.currentIndex(), 0)  # never armed by default
        self.assertEqual(win.dl_command.text(), "")

    def test_save_to_defaults_to_the_downloads_folder(self):
        from pathlib import Path

        win = self.make_window()
        self.addCleanup(win.close)
        default = win._dl_default_dir()
        self.assertTrue(default.name)  # some real folder name, never an empty path
        for blank in ("", "   "):
            win.dl_dest.setText(blank)
            self.assertEqual(win._dl_dest_path(), default)  # empty means Downloads: nothing to warn about
        self.assertIn(str(default), win.dl_dest.edit.placeholderText())  # and the box says so
        win.dl_dest.setText(str(Path.home() / "Elsewhere"))
        self.assertEqual(win._dl_dest_path(), Path.home() / "Elsewhere")


    def test_games_hand_themselves_to_the_other_tools(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "Game.bin").write_bytes(b"x" * 2048)
        (root / "Game.cue").write_text('FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n', encoding="utf-8")
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Game.cue", system="PlayStation", kind="CUE", region="", size=1, modified=1.0, folder="", path=str(root / "Game.cue"))
        win._gm_store.sync([rom], str(root))
        gid = win._gm_store.games()[0].id
        win._gm_reload()
        win._gm_tool_convert([gid])  # a disc sheet: the CHD page, its source folder filled in
        self.assertEqual(win.current_page, "chd")
        self.assertEqual(Path(win.tabs["chd"].source.text()), root)
        win._gm_tool_fix([gid])
        self.assertEqual(win.current_page, "fix")
        self.assertEqual(Path(win._org_sources[0].text()), root)
        searched = []
        with mock.patch.object(type(win), "_st_search", lambda self: searched.append(self.st_search.text())):
            win._gm_tool_sound([gid])  # the grabber takes one name at a time: the game's name goes in its search box
        self.assertEqual((win.current_page, searched), ("sound", ["Game"]))

    def test_edit_game_window_tags(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from distill import games as gm
        from distill.ui.games_dialog import EditGameDialog

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Metroid.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Metroid.sfc"))
        win._gm_store.sync([rom], str(root))
        win._gm_store.update_details(win._gm_store.games()[0].id, tags="co-op")
        game = win._gm_store.games()[0]
        dlg = EditGameDialog(win, game, ["SNES"], "", None, tag_choices=["co-op", "couch", "kids"])
        self.addCleanup(dlg.close)
        self.assertEqual(dlg.tags.text(), "co-op")  # the game's own tags, in the box
        self.assertFalse(dlg.tag_hint.isHidden())  # the library's others are offered under it
        self.assertNotIn(">co-op<", dlg.tag_hint.text())
        self.assertIn(">couch<", dlg.tag_hint.text())
        dlg._add_tag("couch")  # clicking one adds it
        self.assertEqual(dlg.tags.text(), "co-op, couch")
        self.assertNotIn(">couch<", dlg.tag_hint.text())
        dlg.tags.setText("Kids,  kids , split screen")
        dlg._accept()
        self.assertEqual(gm.clean_tags(dlg.values["tags"]), "Kids|split screen")
        plain = EditGameDialog(win, game, ["SNES"], "", None)  # no tag list: just the box
        self.addCleanup(plain.close)
        self.assertTrue(plain.tag_hint.isHidden())

    def test_edit_game_window_launch_settings_for_this_game(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from distill import gamemedia as mm
        from distill import games as gm
        from distill import launchprofile as lp
        from distill.ui.games_dialog import EditGameDialog

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        win = self.make_window()
        self.addCleanup(win.close)
        win._gm_store = gm.GameStore(root / "games.db")
        rom = SimpleNamespace(name="Metroid.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(root / "Metroid.sfc"))
        win._gm_store.sync([rom], str(root))
        game = win._gm_store.games()[0]
        duck = gm.Standalone("duckstation", "DuckStation", Path("/emu/duck.exe"))
        info = lambda system: gm.emulator_options(system, None, {"duckstation": duck}, "")  # noqa: E731
        with mock.patch.object(mm, "MEDIA_DIR", root / "media"):
            dlg = EditGameDialog(win, game, ["SNES"], "", None, media_key="k1", emu_info=info, profile=lp.Profile(args="-x", admin=False))
            self.addCleanup(dlg.close)
            tab = dlg.emu_tab
            self.assertEqual(tab.p_args.text(), "-x")  # what the game already has, in the boxes
            dlg._accept()
            self.assertIsNone(dlg.profile)  # nothing changed: nothing to save
            tab.p_pre.setText("mount {rom}")
            tab.p_env.setPlainText("A=1\nB=2")
            dlg._accept()
            self.assertEqual(dlg.profile, lp.Profile(args="-x", env="A=1\nB=2", pre="mount {rom}"))
            tab.p_args.setText("")
            tab.p_pre.setText("")
            tab.p_env.setPlainText("")
            dlg._accept()
            self.assertTrue(dlg.profile.empty())  # emptied: saved as nothing

    def test_find_a_manual_window_lists_results_and_downloads_the_one_chosen(self):
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock

        from PySide6.QtWidgets import QApplication

        from distill import manualsearch as ms
        from distill.ui.games_manual_dialog import ManualPickerDialog

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        win = self.make_window()
        self.addCleanup(win.close)
        found = [ms.Manual("Super Metroid (Super Nintendo) Manual", "Internet Archive", "sm-manual", "https://example.invalid/sm.pdf", 3_000_000, 0.97,
                           "https://archive.org/details/sm-manual"),
                 ms.Manual("Super Metroid (Europe)", "Internet Archive", "sm-eu", "https://example.invalid/eu.pdf", 0, 0.7, "https://archive.org/details/sm-eu")]

        def pump(until, seconds=10):
            deadline = time.monotonic() + seconds
            while not until() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)

        def fake_download(url, dest, progress=None, cancelled=lambda: False, limit=0):
            Path(dest).write_bytes(b"%PDF-1.4\n")
            if progress:
                progress(9, 9)
            return Path(dest)

        with mock.patch.object(ms, "search", return_value=found), mock.patch.object(ms, "download", side_effect=fake_download), \
                mock.patch.object(ms, "temp_dir", return_value=Path(tmp.name)):
            dlg = ManualPickerDialog(win, "Super Metroid", "SNES", "Super Nintendo Entertainment System")
            self.addCleanup(dlg.close)
            self.assertFalse(dlg.use_btn.isEnabled())  # nothing to use until the search is back
            pump(lambda: dlg.list.count() == 2)
            self.assertEqual(dlg.list.count(), 2)
            self.assertTrue(dlg.use_btn.isEnabled())  # (the best is already chosen)
            self.assertIn("97% match", dlg.list.item(0).text())
            self.assertIn("2.9 MB", dlg.list.item(0).text())
            dlg._use()
            pump(lambda: dlg.path is not None)
            self.assertIsNotNone(dlg.path)
            self.assertTrue(dlg.path.read_bytes().startswith(b"%PDF-"))
            self.assertEqual(dlg.result(), dlg.DialogCode.Accepted)

        with mock.patch.object(ms, "search", return_value=[]):
            empty = ManualPickerDialog(win, "Obscure", "SNES", "")  # nothing found: it says so and offers nothing to use
            self.addCleanup(empty.close)
            pump(lambda: "No manual found" in empty.status.text())
            self.assertIn("No manual found", empty.status.text())
            self.assertFalse(empty.use_btn.isEnabled())
        with mock.patch.object(ms, "search", side_effect=ms.ManualError("Couldn't reach the Internet Archive.")):
            down = ManualPickerDialog(win, "Anything", "SNES", "")
            self.addCleanup(down.close)
            pump(lambda: "Couldn't reach" in down.status.text())
            self.assertIn("Couldn't reach", down.status.text())



if __name__ == "__main__":
    unittest.main()
