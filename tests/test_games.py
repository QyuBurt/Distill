import os
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from distill import games as gm
from distill import recomps as rc


def entry(path, system="SNES", name=None, kind="SFC", region="USA", size=1024, modified=1.0, folder=""):
    return SimpleNamespace(name=name or Path(path).name, system=system, kind=kind, region=region, size=size, modified=modified, folder=folder, path=str(path))


class TitleTests(unittest.TestCase):
    def test_titles_are_cleaned_from_file_names(self):
        self.assertEqual(gm.clean_title("Legend of Zelda, The - Ocarina of Time (USA) (Rev 1).z64"), "The Legend of Zelda - Ocarina of Time")
        self.assertEqual(gm.clean_title("Super Mario Bros. 3 (USA).nes"), "Super Mario Bros. 3")
        self.assertEqual(gm.clean_title("Final Fantasy VII (Disc 1) (USA).cue"), "Final Fantasy VII")
        self.assertEqual(gm.clean_title("sonic_the_hedgehog [!].md"), "sonic the hedgehog")
        self.assertEqual(gm.clean_title("Dr. Mario.nes"), "Dr. Mario")
        self.assertEqual(gm.clean_title("Game, A.sfc"), "A Game")
        self.assertEqual(gm.clean_title("(USA).nes"), "(USA)")  # nothing but a tag: the plain name beats an empty title

    def test_ids_follow_the_path_not_its_spelling(self):
        self.assertEqual(gm.game_id("C:\\Roms\\SNES\\Mario.sfc"), gm.game_id("c:/roms/snes/mario.sfc"))
        self.assertNotEqual(gm.game_id("a.sfc"), gm.game_id("b.sfc"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")

    def test_a_scan_adds_updates_and_removes_games_and_keeps_what_the_player_did(self):
        a, b = entry("/r/Mario (USA).sfc"), entry("/r/Zelda, The (USA).sfc")
        self.assertEqual(self.store.sync([a, b], "/r"), (2, 0))
        mario = next(g for g in self.store.games() if g.title == "Mario")
        self.store.set_favorite(mario.id, True)
        self.store.set_title(mario.id, "Super Mario")
        self.store.record_play(mario.id, 100.0)
        self.assertEqual(self.store.sync([a], "/r"), (0, 1))  # the Zelda file is gone
        self.assertEqual([g.title for g in self.store.games()], ["Mario"])
        kept = self.store.get(mario.id)
        self.assertEqual((kept.name, kept.favorite, kept.play_count, kept.last_played), ("Super Mario", True, 1, 100.0))
        self.assertEqual(self.store.sync([a, entry("/r/Metroid.sfc")], "/r"), (1, 0))

    def test_a_playlist_takes_over_from_its_discs_and_keeps_what_the_player_did(self):
        d1, d2 = entry("/r/FF7 (Disc 1).chd", system="PlayStation", kind="CHD"), entry("/r/FF7 (Disc 2).chd", system="PlayStation", kind="CHD")
        self.store.sync([d1, d2], "/r")
        one, two = (self.store.get(gm.game_id(p)) for p in ("/r/FF7 (Disc 1).chd", "/r/FF7 (Disc 2).chd"))
        self.store.set_favorite(one.id, True)
        self.store.set_title(two.id, "Final Fantasy VII")
        self.store.add_playtime(one.id, 600)
        self.store.add_playtime(two.id, 300)
        self.store.record_play(one.id, 50.0)
        cid = self.store.create_collection("RPGs")
        self.store.add_to_collection(cid, [one.id])
        playlist = entry("/r/FF7.m3u", system="PlayStation", kind="CHD", name="FF7.m3u")
        playlist.discs, playlist.members = 2, ("/r/FF7 (Disc 1).chd", "/r/FF7 (Disc 2).chd")
        self.assertEqual(self.store.sync([playlist], "/r"), (1, 2))
        (game,) = self.store.games()
        self.assertEqual((game.discs, game.favorite, game.play_seconds, game.name, game.last_played), (2, True, 900.0, "Final Fantasy VII", 50.0))
        self.assertEqual([g.id for g in self.store.games(collection=cid)], [game.id])
        self.assertIsNone(self.store.get(one.id))

    def test_the_lookups_a_scan_and_a_removal_make_have_indexes(self):
        import sqlite3
        db = sqlite3.connect(self.store.path)
        try:
            indexes = {r[1] for r in db.execute("SELECT * FROM sqlite_master WHERE type = 'index'")}
        finally:
            db.close()
        self.assertTrue({"games_system", "games_root", "collection_games_gid"} <= indexes)

    def test_an_old_games_file_gets_the_disc_column(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        import sqlite3
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE games (id TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL, system TEXT NOT NULL, kind TEXT, "
                   "region TEXT, size INTEGER, modified REAL, folder TEXT, root TEXT, added REAL)")
        db.execute("INSERT INTO games VALUES ('g1', '/r/a.sfc', 'A', 'SNES', 'SFC', 'USA', 1, 1, '', '/r', 1)")
        db.commit()
        db.close()
        (game,) = gm.GameStore(path).games()
        self.assertEqual((game.title, game.discs), ("A", 0))

    def test_another_folder_is_not_touched_by_a_scan_of_this_one(self):
        self.store.sync([entry("/a/x.sfc")], "/a")
        self.store.sync([entry("/b/y.sfc")], "/b")
        self.store.sync([], "/a")
        self.assertEqual([g.title for g in self.store.games()], ["y"])

    def test_listing_filters_orders_and_counts(self):
        self.store.sync([entry("/r/Zed.sfc"), entry("/r/Alpha.sfc"), entry("/r/Beta.gba", system="Game Boy Advance"),
                         entry("/r/Odd.xyz", system=gm.UNKNOWN)], "/r")
        self.assertEqual([g.title for g in self.store.games()], ["Alpha", "Beta", "Odd", "Zed"])
        self.assertEqual(self.store.systems(), [("Game Boy Advance", 1), ("SNES", 2), (gm.UNKNOWN, 1)])  # the unplaced ones last
        self.assertEqual([g.title for g in self.store.games(system="SNES")], ["Alpha", "Zed"])
        self.assertEqual([g.title for g in self.store.games(query="adv")], ["Beta"])  # the system name is searched too
        alpha = self.store.games(query="alpha")[0]
        self.store.set_favorite(alpha.id, True)
        self.store.record_play(alpha.id, 5.0)
        self.store.record_play(self.store.games(query="zed")[0].id, 9.0)
        self.assertEqual([g.title for g in self.store.games(favorites=True)], ["Alpha"])
        self.assertEqual([g.title for g in self.store.games(recent=True)], ["Zed", "Alpha"])  # latest first
        self.assertEqual(self.store.totals(), (4, 1, 2))

    def test_defaults_and_the_library_folder_are_remembered(self):
        self.assertEqual(self.store.default_for("SNES"), "")
        self.store.set_default("SNES", "retroarch:bsnes")
        self.store.set_library_root("/roms")
        again = gm.GameStore(self.store.path)
        self.assertEqual((again.default_for("SNES"), again.library_root()), ("retroarch:bsnes", "/roms"))


class RetroArchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.exe = self.dir / "retroarch.exe"
        self.exe.write_bytes(b"x")
        self.rom = self.dir / "Mario.sfc"
        self.rom.write_bytes(b"rom")
        self.cores = {n: self.dir / f"{n}_libretro.dll" for n in ("bsnes", "snes9x", "mgba", "fceumm")}
        self.infos = {"mgba": {"systemname": "Game Boy Advance", "categories": "Emulator"},
                      "fceumm": {"systemname": "Nintendo Entertainment System / Famicom", "categories": "Emulator"},
                      "mystery": {"systemname": "Super Nintendo Entertainment System", "categories": "Emulator"}}
        self.setup = gm.RetroSetup(self.exe, self.cores, self.infos)
        self.game = gm.Game("g1", "Mario", "SNES", str(self.rom))

    def test_recommended_cores_come_first_then_any_core_whose_info_covers_the_system(self):
        self.assertEqual(gm.preferred_cores("SNES")[:2], ("snes9x", "bsnes"))
        self.assertEqual(gm.preferred_cores("Some New Console"), ())
        installed = {**self.cores, "mystery": self.dir / "mystery_libretro.dll", "tool": self.dir / "tool_libretro.dll"}
        infos = {**self.infos, "tool": {"systemname": "Super Nintendo Entertainment System", "categories": "Utility"}}
        self.assertEqual(gm.cores_for("SNES", installed, infos), ["snes9x", "bsnes", "mystery"])  # a utility core is not an emulator
        self.assertEqual(gm.cores_for("NES", self.cores, self.infos), ["fceumm"])

    def test_the_command_names_the_core_and_the_game(self):
        cmd = gm.retroarch_command(self.exe, self.cores["snes9x"], self.rom)
        self.assertEqual(cmd, [str(self.exe), "-L", str(self.cores["snes9x"]), str(self.rom), "-f"])
        self.assertNotIn("-f", gm.retroarch_command(self.exe, self.cores["snes9x"], self.rom, fullscreen=False))
        app = gm.retroarch_command(Path("/Applications/RetroArch.app"), Path("/c/x.dylib"), Path("/r/g.sfc"))
        self.assertEqual(app[0], str(Path("/Applications/RetroArch.app/Contents/MacOS/RetroArch")))

    def test_launch_plan_uses_the_game_then_the_system_then_the_best_core(self):
        plan = gm.plan_launch(self.game, "", self.setup)
        self.assertEqual((plan.cmd[2], plan.cwd), (str(self.cores["snes9x"]), str(self.dir)))
        self.assertTrue(plan.label.startswith("RetroArch · "))
        self.assertEqual(gm.plan_launch(self.game, "", self.setup, default="retroarch:bsnes").cmd[2], str(self.cores["bsnes"]))
        self.assertEqual(gm.plan_launch(self.game, "retroarch:snes9x", self.setup, default="retroarch:bsnes").cmd[2], str(self.cores["snes9x"]))

    def test_launch_problems_are_worded_for_the_player(self):
        with self.assertRaisesRegex(gm.GameError, "missing"):
            gm.plan_launch(gm.Game("g2", "Gone", "SNES", str(self.dir / "gone.sfc")), "", self.setup)
        with self.assertRaisesRegex(gm.GameError, "isn't set up"):
            gm.plan_launch(self.game, "", None)
        with self.assertRaisesRegex(gm.GameError, "no core installed for PlayStation"):
            gm.plan_launch(gm.Game("g3", "Crash", "PlayStation", str(self.rom)), "", self.setup)
        with self.assertRaisesRegex(gm.GameError, "isn't installed in RetroArch any more"):
            gm.plan_launch(self.game, "retroarch:gone", self.setup)
        with self.assertRaisesRegex(gm.GameError, "isn't installed"):
            gm.plan_launch(self.game, "dolphin:", self.setup)

    def test_infos_are_read_from_the_info_folder(self):
        info = self.dir / "info"
        info.mkdir()
        (info / "mgba_libretro.info").write_text('display_name = "Nintendo - Game Boy Advance (mGBA)"\nsystemname = "Game Boy Advance"\n', encoding="utf-8")
        got = gm.read_infos(info, ["mgba", "none"])
        self.assertEqual(list(got), ["mgba"])
        self.assertEqual(got["mgba"]["systemname"], "Game Boy Advance")
        self.assertEqual(gm.read_infos(None, ["mgba"]), {})
        self.assertEqual(gm.RetroSetup(self.exe, {}, got).core_label("mgba"), "Nintendo - Game Boy Advance (mGBA)")


LB_SEARCH = ('<a href="/games/details/3108-super-mario-world">Super Mario World</a> <span>Super Nintendo Entertainment System</span>'
             '<a href="/games/details/162266-super-mario-world">Super Mario World</a> <span>Nintendo Wii</span>')
LB_PAGE = ("<h1>Super Mario World</h1><h2>Overview</h2><p>Mario is having a vacation in Dinosaur Land when he learns that Princess Peach "
           "has been kidnapped by the evil King Koopa.</p><h3>Developers</h3><div>Nintendo EAD</div><h3>Publishers</h3><div>Nintendo</div>"
           "<h3>Genres</h3><div>Platform</div><div>Action</div><h3>Release Date</h3><div>1990-11-21</div><h3>Platform</h3><div>Super Nintendo</div>"
           "<h3>Alternate Names</h3><ul><li>Super Mario Bros. 4</li></ul>")


class DetailsTests(unittest.TestCase):
    def test_a_launchbox_page_gives_overview_makers_genres_and_date(self):
        d = gm.parse_launchbox_details(LB_PAGE)
        self.assertIn("vacation in Dinosaur Land", d.overview)
        self.assertEqual((d.developers, d.publishers), ("Nintendo EAD", "Nintendo"))
        self.assertEqual(d.genres, "Platform, Action")  # 'Platform' is a genre here, not the console label
        self.assertEqual(d.released, "1990-11-21")
        self.assertNotIn("Super Mario Bros. 4", d.genres + d.released)
        self.assertTrue(gm.parse_launchbox_details("<p>nothing</p>").empty())

    def test_details_are_searched_by_title_and_console_and_cached(self):
        game = gm.Game("gx", "Super Mario World", "SNES", "/r/smw.sfc")
        calls = []

        def fake_get(url, timeout=15):
            calls.append(url)
            return (LB_SEARCH if "results" in url else LB_PAGE).encode()

        with tempfile.TemporaryDirectory() as tmp:
            store = rc.Store(Path(tmp) / "state.json")
            with mock.patch.object(rc, "_get", fake_get):
                first = gm.launchbox_details(game, store)
                n = len(calls)
                again = gm.launchbox_details(game, store)
            self.assertEqual(first.developers, "Nintendo EAD")
            self.assertEqual(again, first)
            self.assertEqual(len(calls), n)  # the second time came from the cache
            self.assertTrue(calls[1].endswith("/3108"))  # the SNES entry, not the Wii one, is read first

    def test_a_shim_speaks_launchbox_console_names(self):
        s = gm.shim(gm.Game("gy", "Sonic", "Mega Drive", "/r/s.md"))
        self.assertEqual((s.system, s.game, s.id), ("Sega Genesis", "Sonic", "gy"))
        self.assertEqual(gm.shim(gm.Game("gz", "X", gm.UNKNOWN, "/r/x")).system, "")


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")
        self.store.sync([entry("/r/A.sfc"), entry("/r/B.sfc"), entry("/r/C.gba", system="Game Boy Advance")], "/r")
        self.ids = {g.title: g.id for g in self.store.games()}

    def test_the_edit_window_saves_details_and_a_platform_that_moves_the_game(self):
        self.store.update_details(self.ids["A"], title="Alpha", system="Mega Drive", developer="Dev", publisher="Pub", year="1994", genre="Action")
        g = self.store.get(self.ids["A"])
        self.assertEqual((g.name, g.system, g.developer, g.publisher, g.year, g.genre), ("Alpha", "Mega Drive", "Dev", "Pub", "1994", "Action"))
        self.assertEqual(self.store.systems(), [("Game Boy Advance", 1), ("Mega Drive", 1), ("SNES", 1)])
        self.assertEqual([x.title for x in self.store.games(system="Mega Drive")], ["A"])
        self.store.update_details(self.ids["A"], system="", year=None)  # '' goes back to the scan's platform; None leaves a field alone
        g = self.store.get(self.ids["A"])
        self.assertEqual((g.system, g.year, g.developer), ("SNES", "1994", "Dev"))
        self.assertEqual([x.title for x in self.store.games(query="dev")], ["A"])

    def test_a_scan_keeps_the_players_platform_and_details(self):
        self.store.update_details(self.ids["B"], system="PSP", developer="Dev")
        self.store.sync([entry("/r/A.sfc"), entry("/r/B.sfc")], "/r")
        g = self.store.get(self.ids["B"])
        self.assertEqual((g.system, g.developer), ("PSP", "Dev"))

    def test_many_games_at_once(self):
        both = [self.ids["A"], self.ids["B"]]
        self.store.set_favorite_many(both, True)
        self.assertEqual(self.store.totals()[1], 2)
        self.store.set_system_many(both, "NES")
        self.assertEqual(self.store.systems(), [("Game Boy Advance", 1), ("NES", 2)])
        self.assertEqual(self.store.hide([self.ids["A"]]), 1)
        self.assertEqual([g.title for g in self.store.games()], ["B", "C"])
        self.assertEqual(self.store.totals(), (2, 1, 0))
        self.store.sync([entry("/r/A.sfc"), entry("/r/B.sfc"), entry("/r/C.gba", system="Game Boy Advance")], "/r")
        self.assertEqual([g.title for g in self.store.games()], ["B", "C"])  # a scan doesn't bring a removed game back

    def test_an_old_games_db_gets_the_new_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "games.db"
            db = sqlite3.connect(str(path))
            db.execute("CREATE TABLE user (id TEXT PRIMARY KEY, custom_title TEXT DEFAULT '', favorite INTEGER DEFAULT 0, last_played REAL DEFAULT 0, "
                       "play_count INTEGER DEFAULT 0, emulator TEXT DEFAULT '')")
            db.commit()
            db.close()
            store = gm.GameStore(path)
            store.sync([entry("/r/A.sfc")], "/r")
            store.update_details(store.games()[0].id, developer="X")
            self.assertEqual(store.games()[0].developer, "X")

    def test_an_unplaced_game_takes_the_platform_its_library_folder_is_named_for(self):
        self.assertEqual(gm.folder_platform("D:\\Roms\\PS2"), "PlayStation 2")
        self.assertEqual(gm.folder_platform("/roms/Game Boy Advance/"), "Game Boy Advance")
        self.assertEqual(gm.folder_platform("/roms"), "")
        self.store.sync([entry("/p/Odd.chd", system=gm.UNKNOWN), entry("/p/Known.sfc")], "/p", fallback_system="PlayStation 2")
        by = {g.title: g.system for g in self.store.games()}
        self.assertEqual((by["Odd"], by["Known"]), ("PlayStation 2", "SNES"))  # what the scan could tell still wins


class FolderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")

    def test_several_library_folders(self):
        self.assertEqual(self.store.library_roots(), [])
        self.assertTrue(self.store.add_library_root("D:\\Roms"))
        self.assertTrue(self.store.add_library_root("E:\\More"))
        self.assertFalse(self.store.add_library_root("D:\\Roms"))
        self.assertEqual(self.store.library_roots(), ["D:\\Roms", "E:\\More"])
        self.assertEqual(self.store.library_root(), "D:\\Roms")

    def test_an_older_single_folder_setting_is_the_first_folder(self):
        self.store.set_setting("library_root", "/old")
        self.assertEqual(self.store.library_roots(), ["/old"])
        self.store.add_library_root("/new")
        self.assertEqual(self.store.library_roots(), ["/old", "/new"])

    def test_removing_a_folder_drops_only_its_games_and_a_scan_of_one_leaves_the_other(self):
        self.store.add_library_root("/a")
        self.store.add_library_root("/b")
        self.store.sync([entry("/a/One.sfc")], "/a")
        self.store.sync([entry("/b/Two.sfc"), entry("/b/Three.sfc")], "/b")
        self.store.sync([entry("/a/One.sfc")], "/a")  # scanning /a again doesn't touch /b's games
        self.assertEqual(self.store.root_counts(), {"/a": 1, "/b": 2})
        self.assertEqual(self.store.remove_library_root("/b"), 2)
        self.assertEqual([g.title for g in self.store.games()], ["One"])
        self.assertEqual(self.store.library_roots(), ["/a"])


class LaunchAndTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rom = Path(self.tmp.name) / "My Game (USA).iso"
        self.rom.write_bytes(b"x")
        self.game = gm.Game("g1", "My Game", "PlayStation 2", str(self.rom))

    def test_standalone_emulators_are_found_by_platform_and_only_when_installed(self):
        from types import SimpleNamespace as NS
        catalog = [NS(id="retroarch", name="RetroArch", systems=()), NS(id="pcsx2", name="PCSX2", systems=("PlayStation 2",)),
                   NS(id="dolphin", name="Dolphin", systems=("GameCube", "Wii"))]
        have = {"pcsx2": Path("/emu/pcsx2.exe"), "dolphin": Path("/emu/Dolphin.exe")}
        found = gm.standalone_for("PlayStation 2", catalog, have.get)
        self.assertEqual(list(found), ["pcsx2"])
        self.assertEqual(gm.standalone_for("playstation 2", catalog, lambda i: None), {})

    def test_an_emulator_with_no_systems_list_still_plays_the_platform_its_page_names(self):
        from types import SimpleNamespace as NS
        catalog = [NS(id="mame", name="MAME", system="Arcade", systems=()), NS(id="retroarch", name="RetroArch", system="Multi-system", systems=())]
        found = gm.standalone_for("Arcade", catalog, {"mame": Path("/emu/mame.exe")}.get)
        self.assertEqual(list(found), ["mame"])

    def test_the_real_manifest_gives_every_standalone_emulator_a_platform(self):
        from distill import emulators as emm
        for e in emm.load_catalog():
            if e.id != "retroarch":
                self.assertTrue(e.systems or e.system, e.id)
        mame = next(e for e in emm.load_catalog() if e.id == "mame")
        self.assertIn("mame", gm.standalone_for("Arcade", [mame], lambda i: Path("/emu/mame.exe")))

    def test_the_command_is_built_from_the_template_with_paths_kept_whole(self):
        s = gm.Standalone("pcsx2", "PCSX2", Path("/emu/pcsx2.exe"))
        plan = gm.plan_launch(self.game, "", None, emus={"pcsx2": s})  # no RetroArch, no pick: the emulator that plays it
        self.assertEqual(plan.cmd, ["/emu/pcsx2.exe".replace("/", os.sep), "-batch", "-fullscreen", "--", str(self.rom)])
        self.assertEqual(plan.label, "PCSX2")
        mine = gm.Standalone("pcsx2", "PCSX2", Path("/emu/pcsx2.exe"), '--game "{rom}" --x')
        self.assertEqual(gm.plan_launch(self.game, "pcsx2", None, emus={"pcsx2": mine}).cmd[1:], ["--game", str(self.rom), "--x"])
        self.assertEqual(gm.launch_args("-f", self.game), ["-f", str(self.rom)])  # a template without the game gets the file added
        self.assertEqual(gm.launch_args("{name} -rompath {dir}", self.game), ["My Game (USA)", "-rompath", str(self.rom.parent)])
        self.assertEqual(gm.launch_args('"unbalanced', self.game), [str(self.rom)])

    def test_an_emulator_that_cannot_read_a_playlist_is_given_the_first_disc(self):
        folder = Path(self.tmp.name)
        d1, d2, m3u = folder / "FF7 (Disc 1).chd", folder / "FF7 (Disc 2).chd", folder / "FF7.m3u"
        d1.write_bytes(b"x")
        d2.write_bytes(b"x")
        m3u.write_text("FF7 (Disc 1).chd\nFF7 (Disc 2).chd\n")
        game = gm.Game("gm3", "FF7", "PlayStation", str(m3u), discs=2)
        pcsx2 = gm.Standalone("pcsx2", "PCSX2", Path("/emu/pcsx2.exe"))
        duck = gm.Standalone("duckstation", "DuckStation", Path("/emu/duck.exe"))
        self.assertEqual(gm.plan_launch(game, "pcsx2", None, emus={"pcsx2": pcsx2}).cmd[-1], str(d1))
        self.assertEqual(gm.plan_launch(game, "duckstation", None, emus={"duckstation": duck}).cmd[-1], str(m3u))

    def test_a_pick_that_is_not_installed_says_so(self):
        with self.assertRaisesRegex(gm.GameError, "isn't installed"):
            gm.plan_launch(self.game, "dolphin", None, emus={})

    def test_retroarch_still_comes_first_when_it_has_a_core(self):
        cores = {"pcsx2": self.rom}  # any file will do for a core
        setup = gm.RetroSetup(Path("/ra/retroarch.exe"), cores, {})
        s = gm.Standalone("pcsx2", "PCSX2", Path("/emu/pcsx2.exe"))
        with mock.patch.object(gm, "cores_for", lambda system, installed, infos=None: ["pcsx2"]):
            self.assertTrue(gm.plan_launch(self.game, "", setup, emus={"pcsx2": s}).label.startswith("RetroArch"))
        with mock.patch.object(gm, "cores_for", lambda system, installed, infos=None: []):
            self.assertEqual(gm.plan_launch(self.game, "", setup, emus={"pcsx2": s}).label, "PCSX2")

    def test_playtime_is_added_up_and_worded(self):
        store = gm.GameStore(Path(self.tmp.name) / "g.db")
        store.sync([entry("/r/A.sfc")], "/r")
        gid = store.games()[0].id
        store.add_playtime(gid, 600)
        store.add_playtime(gid, 3000)
        g = store.get(gid)
        self.assertEqual((g.play_seconds, g.last_session), (3600.0, 3000.0))
        self.assertEqual([gm.fmt_playtime(x) for x in (5, 300, 3600, 3900, 7200)], ["under a minute", "5 min", "1 h", "1 h 05 min", "2 h"])

    def test_a_session_is_timed_but_a_launcher_handing_over_is_not(self):
        got = []

        class Proc:
            def wait(self):
                return 0

        gm.watch_session(Proc(), got.append).join(2)
        self.assertEqual(got, [])  # ended at once: not a game
        with mock.patch.object(gm, "MIN_SESSION", 0.0):
            gm.watch_session(Proc(), got.append).join(2)
        self.assertEqual(len(got), 1)


class FilterCollectionVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = gm.GameStore(Path(self.tmp.name) / "games.db")
        self.store.sync([entry("/r/A (USA).sfc"), entry("/r/A (Europe).sfc"), entry("/r/B.sfc"), entry("/r/C.gba", system="Game Boy Advance")], "/r")
        self.by = {Path(g.path).name: g for g in self.store.games()}

    def test_filters_and_what_they_offer(self):
        a, b = self.by["A (USA).sfc"], self.by["B.sfc"]
        self.store.update_details(a.id, year="1994", genre="Platform, Action", developer="Nintendo")
        self.store.update_details(b.id, year="1998", genre="Action", developer="Capcom")
        self.store.record_play(b.id)
        names = lambda **f: sorted(g.title for g in self.store.games(filters=f))
        self.assertEqual(names(genre="action"), ["A", "B"])
        self.assertEqual(names(genre="Platform"), ["A"])
        self.assertEqual(names(decade="1990s", developer="capcom"), ["B"])
        self.assertEqual(names(played="played"), ["B"])
        self.assertEqual(len(names(played="unplayed")), 3)
        self.assertEqual(names(genre=""), ["A", "A", "B", "C"])  # an empty filter doesn't count
        facets = self.store.facets()
        self.assertEqual(dict(facets["genre"]), {"Platform": 1, "Action": 2})
        self.assertEqual(facets["genre"][0], ("Action", 2))
        self.assertEqual(facets["decade"], [("1990s", 2)])
        self.assertEqual(gm.decade_of("2004"), "2000s")
        self.assertEqual(gm.decade_of(""), "")

    def test_collections(self):
        a, b = self.by["B.sfc"], self.by["C.gba"]
        cid = self.store.create_collection("  Co-op   nights ")
        self.assertEqual(self.store.create_collection("co-op NIGHTS"), cid)  # the same name is the same collection
        self.assertEqual(self.store.create_collection("  "), 0)
        self.store.add_to_collection(cid, [a.id, b.id])
        self.store.add_to_collection(cid, [a.id])  # again: no duplicate
        self.assertEqual(self.store.collections(), [(cid, "Co-op nights", 2)])
        self.assertEqual(sorted(g.title for g in self.store.games(collection=cid)), ["B", "C"])
        self.store.remove_from_collection(cid, [a.id])
        self.assertEqual([g.title for g in self.store.games(collection=cid)], ["C"])
        self.store.hide([b.id])
        self.assertEqual(self.store.collections()[0][2], 0)  # a game taken out of the library doesn't count
        self.store.rename_collection(cid, "Finish this year")
        self.assertEqual(self.store.collections()[0][1], "Finish this year")
        self.store.delete_collection(cid)
        self.assertEqual(self.store.collections(), [])
        self.assertEqual(len(self.store.games()), 3)  # the games stay

    def test_smart_collections_follow_the_library(self):
        a, b = self.by["A (USA).sfc"], self.by["B.sfc"]
        self.store.update_details(a.id, year="1994", genre="RPG", developer="Nintendo")
        self.store.update_details(b.id, year="1996", genre="RPG")
        self.store.update_details(self.by["A (Europe).sfc"].id, year="1994", genre="RPG")
        rules = {"system": "SNES", "filters": {"genre": "RPG", "decade": "1990s", "played": "unplayed", "missing": True, "bogus": "x"}, "junk": 1}
        self.assertEqual(gm.clean_rules(rules), {"system": "SNES", "filters": {"genre": "RPG", "decade": "1990s", "played": "unplayed"}})
        self.assertEqual(self.store.save_smart_collection("  Old   RPGs ", rules), 1)
        sid = self.store.save_smart_collection("old rpgs", {"filters": {"genre": "RPG"}})  # same name: rules replaced
        self.assertEqual(self.store.save_smart_collection("Old RPGs", rules), sid)
        self.assertEqual(self.store.save_smart_collection("", rules), 0)
        self.assertEqual(self.store.save_smart_collection("Nothing", {"filters": {"genre": ""}}), 0)  # matches everything: not a collection
        self.assertEqual(sorted(g.title for g in self.store.games(smart=sid)), ["A", "A", "B"])
        self.store.record_play(b.id)  # B is played now: it leaves by itself
        self.assertEqual(sorted(g.title for g in self.store.games(smart=sid)), ["A", "A"])
        self.assertEqual(self.store.smart_collections(), [(sid, "Old RPGs", 2)])
        self.assertEqual([g.title for g in self.store.games(smart=sid, query="nintendo")], ["A"])  # the search box narrows it further
        self.store.rename_smart_collection(sid, "Classic RPGs")
        self.assertEqual(self.store.smart_collections()[0][1], "Classic RPGs")
        self.store.delete_smart_collection(sid)
        self.assertEqual(self.store.smart_collections(), [])
        self.assertEqual(self.store.smart_rules(sid), {})
        self.assertEqual(len(self.store.games()), 4)  # the games stay

    def test_bulk_edit_sets_details_on_many_games(self):
        ids = [self.by["B.sfc"].id, self.by["C.gba"].id]
        self.store.update_details(ids[0], developer="Old", year="1990", rating=4, notes="keep")
        self.assertEqual(self.store.update_details_many(ids, genre="  RPG ", developer="Square", publisher="Enix", year="1995"), 2)
        for gid in ids:
            g = self.store.get(gid)
            self.assertEqual((g.genre, g.developer, g.publisher, g.year), ("RPG", "Square", "Enix", "1995"))
        kept = self.store.get(ids[0])
        self.assertEqual((kept.rating, kept.notes), (4, "keep"))  # what isn't set stays
        self.store.update_details_many(ids, genre="Action")  # one field only: the others stay
        self.assertEqual(self.store.get(ids[1]).developer, "Square")
        self.store.update_details_many(ids, year="95")  # not four digits: ignored
        self.assertEqual(self.store.get(ids[0]).year, "1995")
        self.store.update_details_many(ids, publisher="")  # '' clears
        self.assertEqual(self.store.get(ids[1]).publisher, "")
        self.assertEqual(self.store.update_details_many([], genre="x"), 0)
        self.assertEqual(self.store.update_details_many(ids), 0)
        self.assertEqual(self.store.get(self.by["A (USA).sfc"].id).genre, "")  # games not in the list are untouched
        self.assertEqual(len(self.store.games(filters={"genre": "Action"})), 2)  # and the filters see the new values

    def test_versions_of_one_game_share_a_poster(self):
        groups = gm.group_versions(self.store.games())
        self.assertEqual(len(groups), 3)
        shown, versions = next((s, v) for s, v in groups if s.title == "A")
        self.assertEqual(len(versions), 2)
        self.store.set_favorite_many([self.by["A (Europe).sfc"].id], True)
        shown, _ = next((s, v) for s, v in gm.group_versions(self.store.games()) if s.title == "A")
        self.assertEqual(Path(shown.path).name, "A (Europe).sfc")  # a favourite is the one shown
        self.assertIn("A (Europe).sfc", gm.version_label(shown))

    def test_missing_files(self):
        real = Path(self.tmp.name) / "Here.sfc"
        real.write_bytes(b"x")
        g_here = gm.Game("h", "Here", "SNES", str(real))
        g_gone = gm.Game("g", "Gone", "SNES", str(real.with_name("Gone.sfc")))
        self.assertEqual(gm.missing_ids([g_here, g_gone]), {"g"})


class CoverForgetTests(unittest.TestCase):
    def test_a_cover_found_by_distill_is_forgotten_but_one_the_player_picked_stays(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rc, "art_dir", lambda: Path(tmp)):
            game = gm.Game("g9", "Zelda", "SNES", "/r/Zelda.sfc")
            (Path(tmp) / "g9.jpg").write_bytes(b"x")
            (Path(tmp) / "g9.src").write_text("other", encoding="utf-8")
            self.assertTrue(gm.forget_cover(game))
            self.assertEqual(list(Path(tmp).glob("g9.*")), [])
            (Path(tmp) / "g9.jpg").write_bytes(b"x")
            (Path(tmp) / "g9.src").write_text(rc.CUSTOM_SRC, encoding="utf-8")
            self.assertFalse(gm.forget_cover(game))
            self.assertTrue((Path(tmp) / "g9.jpg").exists())


class FirmwareTests(unittest.TestCase):
    def test_required_bios_files_missing_from_the_system_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = Path(tmp) / "system"
            (system / "dc").mkdir(parents=True)
            (system / "dc" / "dc_boot.bin").write_bytes(b"x")
            (system / "extra").mkdir()
            (system / "extra" / "scph5501.bin").write_bytes(b"x")  # put in a sub-folder by hand
            info = {"firmware_count": "4", "firmware0_path": "dc\\dc_boot.bin", "firmware0_opt": "false",
                    "firmware1_path": "dc/dc_flash.bin", "firmware1_opt": "false", "firmware1_desc": "Flash",
                    "firmware2_path": "scph5501.bin", "firmware2_opt": "false", "firmware3_path": "extra_optional.bin", "firmware3_opt": "true"}
            setup = gm.RetroSetup(Path("/ra/retroarch.exe"), {"flycast": Path("/x")}, {"flycast": info}, system)
            self.assertEqual([f.path for f in gm.missing_firmware(setup, "flycast")], ["dc/dc_flash.bin"])
            self.assertEqual(gm.missing_firmware(setup, "unknown_core"), [])
            self.assertEqual(gm.missing_firmware(gm.RetroSetup(Path("/ra/r.exe"), {}, {}), "flycast"), [])

    def test_the_system_folder_comes_from_retroarch_cfg_or_sits_beside_the_program(self):
        from types import SimpleNamespace as NS
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "retroarch.exe"
            cfg = Path(tmp) / "retroarch.cfg"
            cfg.write_text('system_directory = ":\\bios"\n', encoding="utf-8")
            self.assertEqual(gm.retro_system_dir(exe, NS(config=cfg)), Path(tmp) / "bios")
            cfg.write_text('system_directory = "default"\n', encoding="utf-8")
            self.assertEqual(gm.retro_system_dir(exe, NS(config=cfg)), Path(tmp) / "system")
            self.assertEqual(gm.retro_system_dir(exe, NS(config=None)), Path(tmp) / "system")


class ToolHandOffTests(unittest.TestCase):
    def test_games_are_handed_to_the_tools_as_files_a_playlist_as_its_discs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("FF7 (Disc 1).chd", "FF7 (Disc 2).chd", "Zelda.sfc"):
                (root / name).write_bytes(b"x")
            (root / "FF7.m3u").write_text("FF7 (Disc 1).chd\nFF7 (Disc 2).chd\n# a comment\nmissing.chd\n", encoding="utf-8")
            ff7 = gm.Game("a", "Final Fantasy VII", "PlayStation", str(root / "FF7.m3u"))
            zelda = gm.Game("b", "Zelda", "SNES", str(root / "Zelda.sfc"))
            self.assertEqual(gm.tool_files([ff7, zelda]), [root / "FF7 (Disc 1).chd", root / "FF7 (Disc 2).chd", root / "Zelda.sfc"])
            self.assertEqual(gm.tool_files([zelda, zelda]), [root / "Zelda.sfc"])  # once each

    def test_the_folder_for_the_fix_tool_is_the_one_the_games_share(self):
        a = gm.Game("a", "A", "SNES", str(Path("/roms/snes/A.sfc")))
        b = gm.Game("b", "B", "SNES", str(Path("/roms/snes/hacks/B.sfc")))
        c = gm.Game("c", "C", "GBA", str(Path("/roms/gba/C.gba")))
        self.assertEqual(gm.common_folder([a]), Path("/roms/snes"))
        self.assertEqual(gm.common_folder([a, b]), Path("/roms/snes"))
        self.assertEqual(gm.common_folder([a, c]), Path("/roms"))
        self.assertIsNone(gm.common_folder([]))

    def test_the_soundtrack_box_gets_one_name_per_game(self):
        games = [gm.Game("a", "Zelda", "SNES", "/r/Zelda (USA).sfc", region="USA"), gm.Game("b", "Zelda", "SNES", "/r/Zelda (Europe).sfc", region="Europe"),
                 gm.Game("c", "Chrono Trigger", "SNES", "/r/CT.sfc")]
        self.assertEqual(gm.soundtrack_names(games), ["Chrono Trigger", "Zelda"])


class SortAndMatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")
        self.store.sync([entry("/r/B.sfc"), entry("/r/A.sfc"), entry("/r/C.gba", system="Game Boy Advance")], "/r")
        self.ids = {g.title: g.id for g in self.store.games()}

    def names(self, **kw):
        return [g.title for g in self.store.games(**kw)]

    def test_every_sort(self):
        self.store.update_details(self.ids["A"], year="1995")
        self.store.update_details(self.ids["B"], year="1990")
        self.assertEqual(self.names(sort="name"), ["A", "B", "C"])
        self.assertEqual(self.names(sort="name_desc"), ["C", "B", "A"])
        self.assertEqual(self.names(sort="year_new"), ["A", "B", "C"])  # no year goes last
        self.assertEqual(self.names(sort="year_old"), ["B", "A", "C"])
        self.assertEqual(self.names(sort="platform"), ["C", "A", "B"])
        self.store.record_play(self.ids["B"], 100.0)
        self.store.record_play(self.ids["B"], 200.0)
        self.store.record_play(self.ids["C"], 300.0)
        self.assertEqual(self.names(sort="plays")[0], "B")
        self.assertEqual(self.names(sort="played")[:2], ["C", "B"])
        self.assertEqual(set(self.names(sort="added")), {"A", "B", "C"})
        self.assertEqual(self.names(recent=True), ["C", "B"])
        self.assertEqual(set(gm.SORTS), {"name", "name_desc", "year_new", "year_old", "added", "played", "plays", "platform", "rating", "status"})

    def test_a_match_fills_only_what_is_missing(self):
        self.store.update_details(self.ids["A"], developer="Mine")
        m = gm.Match("A", "SNES", "Theirs", "Pub", "1994", "Action")
        self.assertTrue(gm.fill_from_match(self.store, self.store.get(self.ids["A"]), m))
        g = self.store.get(self.ids["A"])
        self.assertEqual((g.developer, g.publisher, g.year, g.genre, g.system), ("Mine", "Pub", "1994", "Action", "SNES"))
        self.assertFalse(gm.fill_from_match(self.store, g, m))

    def test_a_match_never_moves_a_placed_game_but_places_an_unplaced_one(self):
        wii = gm.Match("A", "Wii", "D", "P", "2008", "G")
        snes = gm.Match("A", "SNES", "D", "P", "1994", "G")
        game = self.store.get(self.ids["A"])
        self.assertIsNone(gm.best_match(game, lambda t, s: [wii]))
        self.assertEqual(gm.best_match(game, lambda t, s: [wii, snes]).system, "SNES")
        self.store.sync([entry("/p/Odd.chd", system=gm.UNKNOWN)], "/p")
        odd = next(g for g in self.store.games() if g.title == "Odd")
        self.assertEqual(gm.best_match(odd, lambda t, s: [wii, snes]).system, "Wii")
        gm.fill_from_match(self.store, odd, wii)
        self.assertEqual(self.store.get(odd.id).system, "Wii")


class LookupTests(unittest.TestCase):
    def test_a_launchbox_page_becomes_a_match_with_the_platform_in_distills_words(self):
        m = gm.parse_launchbox_match(LB_PAGE.replace("Super Nintendo", "Super Nintendo Entertainment System"), "3108")
        self.assertEqual((m.title, m.system, m.developer, m.publisher, m.year, m.genre), ("Super Mario World", "SNES", "Nintendo EAD", "Nintendo", "1990", "Platform, Action"))
        self.assertEqual(gm.system_from_launchbox("Sega Genesis"), "Mega Drive")
        self.assertEqual(gm.system_from_launchbox("Odd Machine"), "Odd Machine")

    def test_search_lists_the_games_on_the_chosen_platform_first(self):
        wii = LB_PAGE.replace("Super Nintendo", "Nintendo Wii")
        snes = LB_PAGE.replace("Super Nintendo", "Super Nintendo Entertainment System")

        def fake_get(url, timeout=15):
            if "results" in url:
                return LB_SEARCH.encode()
            return (wii if url.endswith("162266") else snes).encode()

        with mock.patch.object(rc, "_get", fake_get):
            found = gm.launchbox_search("Super Mario World", "SNES")
            self.assertEqual([m.system for m in found], ["SNES", "Wii"])
            self.assertEqual(gm.launchbox_search("  "), [])

    def test_a_game_listed_only_under_a_longer_name_is_found_through_its_platform(self):
        import urllib.parse
        plain = "".join(f'<a href="/games/details/{100 + i}-resident-evil-2">Resident Evil 2</a> Windows ' + "x " * 400 for i in range(15))
        ps1 = '<a href="/games/details/222-resident-evil-2-dual-shock-ver">Resident Evil 2 - Dual Shock Ver.</a> Sony Playstation'
        ps1_page = LB_PAGE.replace("Super Nintendo", "Sony Playstation")

        def fake_get(url, timeout=15):
            if "/details/" in url:
                return ps1_page.encode() if url.endswith("222") else LB_PAGE.replace("Super Nintendo", "Windows").encode()
            return (plain + ps1 if urllib.parse.quote("Resident Evil 2 Sony Playstation") in url else plain).encode()

        with mock.patch.object(rc, "_get", fake_get):
            ids, _slugs = gm.lb_candidates("Resident Evil 2", "PlayStation")
            self.assertIn("222", ids)  # the plain search never lists it; the one with the platform does
            found = gm.launchbox_search("Resident Evil 2", "PlayStation")
            self.assertEqual(found[0].system, "PlayStation")  # and the entry on the game's own platform comes first

    def test_box_art_choices_come_from_the_entries_the_title_could_mean(self):
        box = '<img src="https://images.launchbox-app.com/%s.jpg" alt="Box - Front">'

        def fake_get(url, timeout=15):
            if "/images/" in url:
                return (box % ("a" * 8 + "-bbbb-cccc-dddd-" + url[-3:].rjust(12, "0"))).encode()
            return b'<a href="/games/details/301-zelda">Zelda</a> <a href="/games/details/302-zelda">Zelda</a>'

        with mock.patch.object(rc, "_get", fake_get):
            urls = gm.launchbox_cover_urls("Zelda", "")
            self.assertEqual(len(urls), 2)
            self.assertTrue(all(u.startswith("https://images.launchbox-app.com/") for u in urls))

    def test_an_unreachable_launchbox_is_an_error_not_an_empty_list(self):
        import urllib.error

        def down(url, timeout=15):
            raise urllib.error.URLError("offline")

        with mock.patch.object(rc, "_get", down):
            self.assertRaises(gm.GameError, gm.launchbox_search, "Zelda")


class PersonalFieldTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")
        self.store.sync([entry("/r/Alpha.sfc"), entry("/r/Beta.sfc"), entry("/r/Gamma.sfc"), entry("/r/Delta.sfc")], "/r")
        self.ids = {g.title: g.id for g in self.store.games()}

    def rate(self, title, stars, status=""):
        self.store.update_details(self.ids[title], rating=stars, status=status)

    def test_a_rating_a_status_and_notes_are_saved_clamped_and_cleared(self):
        self.store.update_details(self.ids["Alpha"], rating=4, status="playing", notes="  stuck at the water temple \n")
        g = self.store.get(self.ids["Alpha"])
        self.assertEqual((g.rating, g.status, g.notes), (4, "playing", "stuck at the water temple"))
        self.store.update_details(self.ids["Alpha"], rating=99)
        self.assertEqual(self.store.get(self.ids["Alpha"]).rating, 5)  # five stars at most
        self.store.update_details(self.ids["Alpha"], rating=-2, status="not a status")
        g = self.store.get(self.ids["Alpha"])
        self.assertEqual((g.rating, g.status, g.notes), (0, "", "stuck at the water temple"))  # unknown status = none; the notes were not touched
        self.store.update_details(self.ids["Alpha"], notes="")
        self.assertEqual(self.store.get(self.ids["Alpha"]).notes, "")
        self.assertEqual(self.store.get(self.ids["Beta"]).rating, 0)  # other games are never touched

    def test_the_games_can_be_sorted_by_rating_and_by_status(self):
        self.rate("Alpha", 3, "completed")
        self.rate("Beta", 5, "plan")
        self.rate("Gamma", 3, "playing")
        names = lambda sort: [g.title for g in self.store.games(sort=sort)]  # noqa: E731
        self.assertEqual(names("rating"), ["Beta", "Alpha", "Gamma", "Delta"])  # best first, ties by name, unrated last
        self.assertEqual(names("status"), ["Beta", "Gamma", "Alpha", "Delta"])  # Plan to play, Playing, Completed, then no status
        self.assertIn("rating", gm.SORTS)
        self.assertIn("status", gm.SORTS)

    def test_the_games_can_be_filtered_by_rating_and_by_status(self):
        self.rate("Alpha", 3, "completed")
        self.rate("Beta", 5, "plan")
        self.rate("Gamma", 4, "completed")
        names = lambda **f: sorted(g.title for g in self.store.games(filters=f))  # noqa: E731
        self.assertEqual(names(rating="4"), ["Beta", "Gamma"])  # four stars or more
        self.assertEqual(names(rating="5"), ["Beta"])
        self.assertEqual(names(rating="unrated"), ["Delta"])
        self.assertEqual(names(status="completed"), ["Alpha", "Gamma"])
        self.assertEqual(names(status="none"), ["Delta"])
        self.assertEqual(names(status="completed", rating="4"), ["Gamma"])
        self.assertEqual(names(rating="", status=""), ["Alpha", "Beta", "Delta", "Gamma"])  # empty filters don't count

    def test_the_notes_can_be_found_with_the_search_box(self):
        self.store.update_details(self.ids["Beta"], notes="Needs the grapple beam for the secret room")
        self.assertEqual([g.title for g in self.store.games(query="grapple")], ["Beta"])

    def test_a_playlist_keeps_the_best_rating_the_first_status_and_the_notes_of_its_discs(self):
        d1, d2 = entry("/r/FF7 (Disc 1).chd", system="PlayStation", kind="CHD"), entry("/r/FF7 (Disc 2).chd", system="PlayStation", kind="CHD")
        self.store.sync([d1, d2], "/r")
        one, two = (self.store.get(gm.game_id(p)) for p in ("/r/FF7 (Disc 1).chd", "/r/FF7 (Disc 2).chd"))
        self.store.update_details(one.id, rating=3, status="playing", notes="disc 1 notes")
        self.store.update_details(two.id, rating=5)
        playlist = entry("/r/FF7.m3u", system="PlayStation", kind="CHD", name="FF7.m3u")
        playlist.discs, playlist.members = 2, ("/r/FF7 (Disc 1).chd", "/r/FF7 (Disc 2).chd")
        self.store.sync([playlist], "/r")
        game = next(g for g in self.store.games() if g.discs == 2)
        self.assertEqual((game.rating, game.status, game.notes), (5, "playing", "disc 1 notes"))

    def test_an_older_library_file_gets_the_new_columns_when_it_is_opened(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "old.db"
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE user (id TEXT PRIMARY KEY, custom_title TEXT DEFAULT '', favorite INTEGER DEFAULT 0, last_played REAL DEFAULT 0, "
                         "play_count INTEGER DEFAULT 0, emulator TEXT DEFAULT '');")
        db.commit()
        db.close()
        store = gm.GameStore(path)
        store.sync([entry("/r/Old.sfc")], "/r")
        store.update_details(store.games()[0].id, rating=2, status="dropped", notes="meh")
        g = store.games()[0]
        self.assertEqual((g.rating, g.status, g.notes), (2, "dropped", "meh"))


class PlatformHealthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "system").mkdir()
        psx = {"systemname": "Sony - PlayStation", "categories": "Emulator"}
        self.setup = gm.RetroSetup(
            self.root / "retroarch.exe", {"pcsx_rearmed": self.root / "a.dll", "beetle_psx_hw": self.root / "b.dll"},
            {"pcsx_rearmed": {**psx, "display_name": "PCSX ReARMed"},
             "beetle_psx_hw": {**psx, "display_name": "Beetle PSX HW", "firmware_count": "2", "firmware0_path": "scph5501.bin", "firmware0_desc": "PS1 US BIOS",
                               "firmware0_opt": "false", "firmware1_path": "extra.bin", "firmware1_opt": "true"}},
            self.root / "system")
        self.duck = {"duckstation": gm.Standalone("duckstation", "DuckStation", self.root / "duck.exe")}

    def health(self, default="", setup="unset", emus=None, system="PlayStation"):
        return gm.platform_health(system, 3, default, self.setup if setup == "unset" else setup, self.duck if emus is None else emus)

    def test_a_platform_with_nothing_installed_for_it_cannot_start_a_game(self):
        h = self.health(setup=None, emus={}, system="Sega Saturn")
        self.assertEqual((h.level, h.emulator, h.label), ("bad", "", ""))
        self.assertEqual(h.summary, "Sega Saturn: nothing installed")
        self.assertIn("Install an emulator", h.problems[0][1])

    def test_a_cores_missing_bios_is_reported_until_the_file_is_there(self):
        h = self.health("retroarch:beetle_psx_hw", emus={})
        self.assertEqual((h.level, h.emulator, h.core, h.summary), ("warn", "retroarch", "beetle_psx_hw", "PlayStation: BIOS missing"))
        self.assertEqual([f.path for f in h.firmware], ["scph5501.bin"])  # the optional file isn't asked for
        self.assertIn("scph5501.bin", h.problems[0][1])
        (self.root / "system" / "scph5501.bin").write_bytes(b"bios")
        h = self.health("retroarch:beetle_psx_hw", emus={})
        self.assertEqual((h.level, h.summary, h.firmware), ("ok", "PlayStation: ready", []))

    def test_a_bios_put_in_a_sub_folder_by_hand_counts(self):
        (self.root / "system" / "psx").mkdir()
        (self.root / "system" / "psx" / "scph5501.bin").write_bytes(b"bios")
        self.assertEqual(self.health("retroarch:beetle_psx_hw", emus={}).level, "ok")

    def test_automatic_uses_the_best_core_and_a_standalone_default_wins(self):
        auto = self.health()
        self.assertEqual((auto.emulator, auto.core, auto.label), ("retroarch", "pcsx_rearmed", "RetroArch · PCSX ReARMed"))
        chosen = self.health("duckstation")
        self.assertEqual((chosen.level, chosen.emulator, chosen.label), ("ok", "duckstation", "DuckStation"))
        self.assertEqual(self.health("duckstation", setup=None).label, "DuckStation")  # no RetroArch at all

    def test_a_default_that_is_not_installed_any_more_is_flagged_and_something_else_is_used(self):
        for gone in ("pcsx2", "retroarch:mednafen_psx"):
            h = self.health(gone)
            self.assertEqual((h.level, h.summary, h.emulator), ("warn", "PlayStation: default emulator missing", "retroarch"))
        h = self.health("retroarch:pcsx_rearmed", setup=None)  # RetroArch was removed
        self.assertEqual((h.emulator, h.label, h.level), ("duckstation", "DuckStation", "warn"))

    def test_the_choices_list_the_cores_then_the_emulators_of_their_own(self):
        self.assertEqual(gm.platform_choices("PlayStation", self.setup, self.duck),
                         [("retroarch:pcsx_rearmed", "RetroArch · PCSX ReARMed"), ("retroarch:beetle_psx_hw", "RetroArch · Beetle PSX HW"), ("duckstation", "DuckStation")])
        self.assertEqual(gm.platform_choices("Sega Saturn", None, {}), [])

    def test_the_overview_lists_what_needs_attention_first_and_skips_unplaced_games(self):
        (self.root / "system" / "scph5501.bin").write_bytes(b"bios")
        counts = [("Game Boy", 4), ("PlayStation", 3), ("Sega Saturn", 1), (gm.UNKNOWN, 2)]
        rows = gm.health_overview(counts, lambda s: "retroarch:beetle_psx_hw" if s == "PlayStation" else "", self.setup, lambda s: {})
        self.assertEqual([(h.system, h.level) for h in rows], [("Game Boy", "bad"), ("Sega Saturn", "bad"), ("PlayStation", "ok")])
        self.assertEqual([h.games for h in rows], [4, 1, 3])

    def test_launch_options_fall_back_to_distills_own(self):
        self.assertEqual(gm.launch_options("duckstation", ""), ("-batch -fullscreen -- {rom}", "-batch -fullscreen -- {rom}"))
        self.assertEqual(gm.launch_options("duckstation", "-nogui {rom}"), ("-nogui {rom}", "-batch -fullscreen -- {rom}"))
        self.assertEqual(gm.launch_options("someemu", ""), ("{rom}", "{rom}"))


if __name__ == "__main__":
    unittest.main()


class ResumeFromSaveStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roms = self.root / "roms" / "snes"
        self.roms.mkdir(parents=True)
        self.rom = self.roms / "Super Game (USA).sfc"
        self.rom.write_bytes(b"x")
        self.states = self.root / "states"
        self.states.mkdir()
        self.core = self.root / "snes9x_libretro.dll"
        self.core.write_bytes(b"x")
        self.game = gm.Game("g1", "Super Game", "SNES", str(self.rom))

    def setup_with(self, **kw):
        return gm.RetroSetup(self.root / "retroarch.exe", {"snes9x": self.core}, {"snes9x": {"corename": "Snes9x"}}, None, **kw)

    def touch(self, path: Path, age: float = 0.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"s")
        when = time.time() - age
        os.utime(path, (when, when))
        return path

    def test_the_newest_state_comes_first_and_slots_are_read_from_the_name(self):
        self.touch(self.states / "Super Game (USA).state", age=300)
        self.touch(self.states / "Super Game (USA).state3", age=10)
        self.touch(self.states / "Super Game (USA).state1", age=100)
        found = gm.find_save_states(self.setup_with(state_dir=self.states), "snes9x", self.rom)
        self.assertEqual([st.slot for st in found], [3, 1, 0])

    def test_thumbnails_auto_states_and_other_games_are_not_states(self):
        self.touch(self.states / "Super Game (USA).state.png")
        self.touch(self.states / "Super Game (USA).state.auto")
        self.touch(self.states / "Super Game (USA) (Rev 1).state")
        self.touch(self.states / "Other.state")
        self.assertEqual(gm.find_save_states(self.setup_with(state_dir=self.states), "snes9x", self.rom), [])

    def test_states_are_found_beside_the_game_when_no_folder_is_set(self):
        self.touch(self.roms / "Super Game (USA).state2")
        found = gm.find_save_states(self.setup_with(), "snes9x", self.rom)
        self.assertEqual([st.slot for st in found], [2])

    def test_states_sorted_into_core_and_content_folders_are_found_without_regard_to_case(self):
        self.touch(self.states / "SNES9X" / "Super Game (USA).state1")
        self.touch(self.states / "snes" / "Super Game (USA).state4")  # the game's folder
        found = gm.find_save_states(self.setup_with(state_dir=self.states, states_by_core=True, states_by_content=True), "snes9x", self.rom)
        self.assertEqual(sorted(st.slot for st in found), [1, 4])

    def test_the_command_asks_retroarch_for_the_slot_only_when_resuming(self):
        plain = gm.retroarch_command(Path("/ra/retroarch.exe"), Path("/ra/c.dll"), Path("/g.sfc"))
        self.assertNotIn("-e", plain)
        resumed = gm.retroarch_command(Path("/ra/retroarch.exe"), Path("/ra/c.dll"), Path("/g.sfc"), state_slot=3)
        self.assertEqual(resumed[-2:], ["-e", "3"])

    def test_plan_launch_resumes_from_the_newest_state_and_says_so_when_there_is_none(self):
        setup = self.setup_with(state_dir=self.states)
        with self.assertRaises(gm.GameError):
            gm.plan_launch(self.game, "", setup, resume=True)
        self.touch(self.states / "Super Game (USA).state2")
        plan = gm.plan_launch(self.game, "", setup, resume=True)
        self.assertEqual(plan.cmd[-2:], ["-e", "2"])
        self.assertNotIn("-e", gm.plan_launch(self.game, "", setup).cmd)  # Play still starts fresh

    def test_resume_is_refused_for_an_emulator_of_its_own(self):
        s = gm.Standalone("pcsx2", "PCSX2", Path("/emu/pcsx2.exe"))
        with self.assertRaises(gm.GameError):
            gm.plan_launch(self.game, "pcsx2", None, emus={"pcsx2": s}, resume=True)
        self.assertIsNone(gm.resume_state(self.game, "pcsx2", self.setup_with(state_dir=self.states), emus={"pcsx2": s}))

    def test_resume_state_agrees_with_the_plan(self):
        setup = self.setup_with(state_dir=self.states)
        self.assertIsNone(gm.resume_state(self.game, "", setup))
        self.touch(self.states / "Super Game (USA).state1")
        found = gm.resume_state(self.game, "", setup)
        self.assertEqual(found.slot, 1)
        self.assertIsNone(gm.resume_state(self.game, "", None))
        self.assertIsNone(gm.resume_state(self.game, "retroarch:notinstalled", setup))

    def test_the_cfg_says_where_states_go(self):
        from types import SimpleNamespace as NS
        cfg = self.root / "retroarch.cfg"
        cfg.write_text('savestate_directory = ":\\states"\nsort_savestates_enable = "true"\nsort_savestates_by_content_enable = "false"\n', encoding="utf-8")
        folder, by_core, by_content = gm.retro_state_options(self.root / "retroarch.exe", NS(config=cfg))
        self.assertEqual(folder, self.root / "states")
        self.assertEqual((by_core, by_content), (True, False))
        self.assertEqual(gm.retro_state_options(self.root / "retroarch.exe", NS(config=None)), (None, False, False))


class AgoTests(unittest.TestCase):
    def test_how_long_ago_in_words(self):
        now = 1_000_000_000.0
        for seconds, text in ((10, "just now"), (5 * 60, "5 min ago"), (3 * 3600, "3 h ago"), (30 * 3600, "yesterday"), (4 * 86400, "4 days ago"),
                              (21 * 86400, "3 weeks ago"), (90 * 86400, "3 months ago"), (800 * 86400, "2 years ago")):
            self.assertEqual(gm.fmt_ago(now - seconds, now), text)
        self.assertEqual(gm.fmt_ago(0, now), "")


class PlatformSpellingTests(unittest.TestCase):
    def test_any_spelling_of_a_platform_gets_distills_name(self):
        for spelling in ("Mega Drive", "Sega Mega Drive", "Sega Genesis", "Genesis", "Mega Drive / Genesis", "SEGA - Mega Drive - Genesis", " sega  mega drive "):
            self.assertEqual(gm.canonical_system(spelling), "Mega Drive", spelling)
        self.assertEqual(gm.canonical_system("Sega CD"), "Sega CD")
        self.assertEqual(gm.canonical_system("Super Nintendo Entertainment System"), "SNES")
        self.assertEqual(gm.canonical_system("Some Odd Platform"), "Some Odd Platform")
        self.assertEqual(gm.canonical_system(""), "")

    def test_a_mega_drive_game_finds_its_cores_whatever_its_platform_is_called(self):
        installed = {"genesis_plus_gx": Path("/c/a.dll"), "picodrive": Path("/c/b.dll")}
        infos = {"genesis_plus_gx": {"systemname": "Sega MS/GG/MD/CD", "categories": "Emulator"},
                 "picodrive": {"systemname": "Sega MS/MD/CD/32X", "categories": "Emulator"}}
        for spelling in ("Mega Drive", "Sega Mega Drive", "Genesis", "Mega Drive / Genesis"):
            self.assertEqual(gm.cores_for(spelling, installed, infos), ["genesis_plus_gx", "picodrive"], spelling)
            self.assertEqual(gm.preferred_cores(spelling), ("genesis_plus_gx", "picodrive"), spelling)

    def test_an_emulator_of_its_own_is_found_for_any_spelling(self):
        from types import SimpleNamespace as NS
        catalog = [NS(id="blastem", name="BlastEm", system="Mega Drive", systems=("Mega Drive",))]
        found = gm.standalone_for("Sega Genesis", catalog, {"blastem": Path("/e/b.exe")}.get)
        self.assertEqual(list(found), ["blastem"])


def mk(gid, title, **kw):
    return gm.Game(id=gid, title=title, system=kw.pop("system", "SNES"), path=kw.pop("path", f"/r/{gid}.sfc"), **kw)


class RandomTests(unittest.TestCase):
    def test_a_random_pick_comes_from_the_games_given_and_avoids_the_last_one(self):
        games = [mk("a", "A"), mk("b", "B"), mk("c", "C")]
        seen = {gm.pick_random(games, avoid="a").id for _ in range(60)}
        self.assertEqual(seen, {"b", "c"})

    def test_the_only_game_is_picked_even_if_it_was_the_last(self):
        self.assertEqual(gm.pick_random([mk("a", "A")], avoid="a").id, "a")

    def test_nothing_to_pick_from_is_none(self):
        self.assertIsNone(gm.pick_random([]))


class RecentlyAddedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")
        self.store.sync([entry("/r/Old (USA).sfc"), entry("/r/Mid (USA).sfc"), entry("/r/New (USA).sfc")], "/r")
        now = time.time()
        with self.store._db() as db:
            for name, days in (("Old", 90), ("Mid", 10), ("New", 1)):
                db.execute("UPDATE games SET added = ? WHERE title = ?", (now - days * 86400, name))

    def test_only_games_found_lately_count_and_the_newest_come_first(self):
        self.assertEqual([g.title for g in self.store.games(added=True)], ["New", "Mid"])
        self.assertEqual(self.store.recent_added(), 2)

    def test_another_sort_is_kept_and_the_pill_combines_with_the_platform(self):
        self.assertEqual([g.title for g in self.store.games(added=True, sort="name_desc")], ["New", "Mid"])
        self.assertEqual(self.store.games(added=True, system="Mega Drive"), [])

    def test_a_game_taken_out_of_the_library_is_not_counted(self):
        self.store.hide([gm.game_id("/r/New (USA).sfc")])
        self.assertEqual(self.store.recent_added(), 1)


class ListColumnTests(unittest.TestCase):
    def setUp(self):
        self.games = [
            mk("a", "Alpha", year="1994", rating=3, status="playing", play_seconds=600, last_played=100.0, added=5.0, genre="RPG"),
            mk("b", "Bravo", year="1991", rating=5, status="completed", play_seconds=7200, last_played=300.0, added=9.0, genre="Action", system="Mega Drive"),
            mk("c", "Charlie", year="", rating=0, status="", play_seconds=0, last_played=0.0, added=1.0, genre=""),
            mk("d", "Delta", year="1994", rating=3, status="plan", play_seconds=60, last_played=200.0, added=7.0, genre="action"),
        ]

    def names(self, column, descending=False):
        return [g.name for g in gm.sort_by_column(self.games, column, descending)]

    def test_every_column_has_a_heading_and_a_starting_direction(self):
        for key, (head, first) in gm.LIST_COLUMNS.items():
            self.assertTrue(head)
            self.assertIn(first, ("asc", "desc"))

    def test_title_sorts_both_ways(self):
        self.assertEqual(self.names("name"), ["Alpha", "Bravo", "Charlie", "Delta"])
        self.assertEqual(self.names("name", True), ["Delta", "Charlie", "Bravo", "Alpha"])

    def test_year_runs_both_ways_with_undated_games_last_and_ties_alphabetical(self):
        self.assertEqual(self.names("year"), ["Bravo", "Alpha", "Delta", "Charlie"])
        self.assertEqual(self.names("year", True), ["Alpha", "Delta", "Bravo", "Charlie"])

    def test_rating_and_time_played_leave_the_empty_ones_last_whichever_way(self):
        self.assertEqual(self.names("rating", True), ["Bravo", "Alpha", "Delta", "Charlie"])
        self.assertEqual(self.names("rating"), ["Alpha", "Delta", "Bravo", "Charlie"])
        self.assertEqual(self.names("play_seconds", True), ["Bravo", "Alpha", "Delta", "Charlie"])

    def test_progress_follows_the_order_the_statuses_are_listed(self):
        self.assertEqual(self.names("status"), ["Delta", "Alpha", "Bravo", "Charlie"])  # plan, playing, completed, none

    def test_dates_sort_by_the_time_not_the_words(self):
        self.assertEqual(self.names("last_played", True), ["Bravo", "Delta", "Alpha", "Charlie"])
        self.assertEqual(self.names("added", True), ["Bravo", "Delta", "Alpha", "Charlie"])

    def test_text_columns_ignore_case_and_put_blanks_last(self):
        self.assertEqual(self.names("genre"), ["Bravo", "Delta", "Alpha", "Charlie"])  # Action, action (a tie: by title), RPG, none
        self.assertEqual(self.names("system"), ["Bravo", "Alpha", "Charlie", "Delta"])  # Mega Drive, then SNES (ties by title)

    def test_an_unknown_column_is_by_title(self):
        self.assertEqual(self.names("nonsense"), ["Alpha", "Bravo", "Charlie", "Delta"])

    def test_cells_say_what_the_game_has(self):
        g = self.games[1]
        self.assertEqual(gm.column_text(g, "rating"), "★★★★★")
        self.assertEqual(gm.column_text(g, "status"), "Completed")
        self.assertEqual(gm.column_text(g, "play_seconds"), "2 h")
        self.assertEqual(gm.column_text(g, "last_played", now=300.0 + 3 * 86400), "3 days ago")
        empty = self.games[2]
        self.assertEqual([gm.column_text(empty, c) for c in ("rating", "status", "play_seconds", "last_played", "year", "genre")], [""] * 6)


class PlaytimeStatsTests(unittest.TestCase):
    def test_time_is_added_up_by_platform_most_first(self):
        games = [mk("a", "A", play_seconds=600), mk("b", "B", play_seconds=1200), mk("c", "C", system="GBA", play_seconds=7200), mk("d", "D"),
                 mk("e", "E", system="Mega Drive")]
        rows = gm.playtime_by_platform(games)
        self.assertEqual([(r.system, r.seconds, r.games, r.total) for r in rows], [("GBA", 7200, 1, 1), ("SNES", 1800, 2, 3)])
        self.assertAlmostEqual(rows[0].share(sum(r.seconds for r in rows)), 0.8)

    def test_equal_times_go_alphabetical_and_nothing_played_is_empty(self):
        games = [mk("a", "A", system="NES", play_seconds=60), mk("b", "B", system="GBA", play_seconds=60)]
        self.assertEqual([r.system for r in gm.playtime_by_platform(games)], ["GBA", "NES"])
        self.assertEqual(gm.playtime_by_platform([mk("a", "A")]), [])
        self.assertEqual(gm.PlatformTime("X", 0, 0, 0).share(0), 0.0)


class DatCheckTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        import threading
        from distill import datverify as dv
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.stop = threading.Event()
        good, other = b"good rom data", b"hacked rom data"
        (self.dir / "Mario.sfc").write_bytes(good)
        (self.dir / "mario copy.sfc").write_bytes(good)
        (self.dir / "Zelda (USA).sfc").write_bytes(other)  # named like the DAT's game, different contents
        (self.dir / "Mystery.sfc").write_bytes(b"who knows")
        (self.dir / "Disc.cue").write_text("FILE x.bin BINARY")
        rom = dv.DatRom("Super Mario (USA).sfc", len(good), sha1=hashlib.sha1(good).hexdigest())
        zelda = dv.DatRom("Zelda (USA).sfc", 5, sha1="0" * 40)
        self.dat = dv.Dat("Test DAT", [dv.DatGame("Super Mario (USA)", [rom]), dv.DatGame("Zelda (USA)", [zelda])])
        self.games = [mk(n, n, path=str(self.dir / f)) for n, f in (("mario", "Mario.sfc"), ("copy", "mario copy.sfc"), ("zelda", "Zelda (USA).sfc"),
                                                                   ("mystery", "Mystery.sfc"), ("disc", "Disc.cue"), ("gone", "missing.sfc"))]

    def test_games_are_sorted_into_what_the_dat_says_of_them(self):
        r = gm.verify_games(self.games, self.dat, self.stop)
        self.assertEqual(sorted(r.ok), ["copy", "mario"])
        self.assertEqual(r.bad, ["zelda"])
        self.assertEqual(r.unknown, ["mystery"])
        self.assertEqual(r.unreadable, ["gone"])
        self.assertEqual(r.skipped, ["disc"])
        self.assertEqual(r.checked, 5)

    def test_a_file_with_the_right_content_but_another_name_is_reported_with_the_dats_name(self):
        r = gm.verify_games(self.games, self.dat, self.stop)
        self.assertEqual(r.renamed, {"mario": "Super Mario (USA).sfc", "copy": "Super Mario (USA).sfc"})

    def test_the_summary_says_only_what_there_is(self):
        self.assertEqual(gm.DatCheck(ok=["a"]).summary(), "1 match.")
        r = gm.verify_games(self.games, self.dat, self.stop)
        self.assertEqual(r.summary(), "2 matches, 1 differ from the DAT, 1 not in the DAT, 1 unreadable, 1 skipped.")
