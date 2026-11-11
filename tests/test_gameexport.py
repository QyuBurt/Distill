import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from distill import gameexport as ge
from distill import games as gm


def entry(path, system="SNES", region="USA"):
    return SimpleNamespace(name=Path(path).name, system=system, kind="SFC", region=region, size=1, modified=1.0, folder="", path=str(path))


class ExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "playlists"
        self.store = gm.GameStore(self.root / "games.db")
        self.zelda = str(self.root / "Zelda (USA).sfc")
        self.sonic = str(self.root / "Sonic (USA).md")
        self.ps2 = str(self.root / "Ico (USA).iso")
        self.store.sync([entry(self.zelda), entry(self.sonic, "Mega Drive"), entry(self.ps2, "PlayStation 2")], str(self.root))
        snes = self.root / "snes9x_libretro.dll"
        gpgx = self.root / "genesis_plus_gx_libretro.dll"
        self.setup = gm.RetroSetup(self.root / "retroarch.exe", {"snes9x": snes, "genesis_plus_gx": gpgx},
                                   {"snes9x": {"display_name": "Nintendo - SNES / SFC (Snes9x - Current)", "systemname": "Super Nintendo Entertainment System", "categories": "Emulator"},
                                    "genesis_plus_gx": {"display_name": "Sega - MS/GG/MD/CD (Genesis Plus GX)", "systemname": "Sega MS/GG/MD/CD", "categories": "Emulator"}})

    def games(self):
        return {g.system: g for g in self.store.games()}

    def lpl(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_one_playlist_per_platform_named_like_retroarchs_own(self):
        report = ge.export_retroarch(self.store, self.out, self.setup)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(names, ["Nintendo - Super Nintendo Entertainment System.lpl", "Sega - Mega Drive - Genesis.lpl"])
        data = self.lpl("Nintendo - Super Nintendo Entertainment System.lpl")
        (item,) = data["items"]
        self.assertEqual((item["path"], item["label"], item["core_name"], item["db_name"]),
                         (self.zelda, "Zelda (USA)", "Nintendo - SNES / SFC (Snes9x - Current)", "Nintendo - Super Nintendo Entertainment System.lpl"))
        self.assertTrue(item["core_path"].endswith("snes9x_libretro.dll"))
        self.assertEqual(report.left_out, {"play in another emulator": 1})  # no RetroArch core plays the PS2 game here
        self.assertIn("Wrote 2 playlists", report.summary())

    def test_favourites_and_collections_get_their_own_playlists(self):
        g = self.games()["SNES"]
        self.store.set_favorite(g.id, True)
        cid = self.store.create_collection("Best: of/all")
        self.store.add_to_collection(cid, [g.id, self.games()["Mega Drive"].id])
        ge.export_retroarch(self.store, self.out, self.setup)
        self.assertEqual(len(self.lpl("Distill Favourites.lpl")["items"]), 1)
        self.assertEqual(len(self.lpl("Distill - Best of all.lpl")["items"]), 2)

    def test_an_existing_playlist_keeps_what_it_has_and_only_gains_new_games(self):
        self.out.mkdir()
        existing = self.out / "Nintendo - Super Nintendo Entertainment System.lpl"
        mine = {"version": "1.5", "items": [{"path": self.zelda.upper(), "label": "My name", "core_path": "/x/mine.dll", "core_name": "Mine", "crc32": "AB|crc",
                                             "db_name": "x.lpl"}, {"path": "/other/Metroid.sfc", "label": "Metroid"}]}
        existing.write_text(json.dumps(mine), encoding="utf-8")
        self.store.sync([entry(self.zelda), entry(self.root / "Mario (USA).sfc"), entry(self.sonic, "Mega Drive")], str(self.root))
        ge.export_retroarch(self.store, self.out, self.setup)
        items = json.loads(existing.read_text(encoding="utf-8"))["items"]
        self.assertEqual([i["label"] for i in items], ["My name", "Metroid", "Mario (USA)"])  # the player's entries untouched; the new game appended
        self.assertEqual(items[0]["core_name"], "Mine")
        again = ge.export_retroarch(self.store, self.out, self.setup)
        self.assertEqual(sum(p.added for p in again.playlists), 0)  # a second export adds nothing
        self.assertEqual(len(json.loads(existing.read_text(encoding="utf-8"))["items"]), 3)

    def test_a_playlist_it_cannot_read_is_left_alone(self):
        self.out.mkdir()
        old = self.out / "Sega - Mega Drive - Genesis.lpl"
        old.write_text("/roms/a.md\nA\nDETECT\nDETECT\n00000000|crc\nx.lpl\n", encoding="utf-8")
        report = ge.export_retroarch(self.store, self.out, self.setup)
        self.assertTrue(old.read_text(encoding="utf-8").startswith("/roms/a.md"))
        self.assertIn("was left alone", report.summary())

    def test_a_game_set_to_its_own_emulator_is_left_out_and_a_chosen_core_is_used(self):
        z = self.games()["SNES"]
        self.store.set_emulator(z.id, "pcsx2")
        self.assertEqual(ge.export_retroarch(self.store, self.out, self.setup).left_out["play in another emulator"], 2)
        self.store.set_emulator(z.id, "retroarch:snes9x")
        self.assertEqual(ge.retro_entry(self.store.get(z.id), self.setup)[1], self.setup.core_label("snes9x"))

    def test_without_retroarch_set_up_retroarch_picks_the_core(self):
        ge.export_retroarch(self.store, self.out, None)
        (item,) = self.lpl("Sony - PlayStation 2.lpl")["items"]
        self.assertEqual((item["core_path"], item["core_name"]), ("DETECT", "DETECT"))

    def test_file_names_and_platform_names(self):
        self.assertEqual(ge.playlist_name('A/B: "C"?'), "A B C")
        self.assertEqual(ge.db_name("Sega Genesis"), "Sega - Mega Drive - Genesis")  # any spelling of the platform
        self.assertEqual(ge.db_name("Unknown thing"), "")

    def test_a_playlist_is_written_whole_or_not_at_all(self):
        ge.export_retroarch(self.store, self.out, self.setup)
        self.assertEqual([p for p in self.out.iterdir() if p.suffix != ".lpl"], [])  # no temporary files left behind


class PlaylistFolderTests(unittest.TestCase):
    def test_the_cfg_says_where_playlists_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "retroarch.cfg"
            cfg.write_text('playlist_directory = ":\\\\lists"\n', encoding="utf-8")
            self.assertEqual(gm.retro_playlist_dir(root / "retroarch.exe", SimpleNamespace(config=cfg)), root / "lists")
            self.assertEqual(gm.retro_playlist_dir(root / "retroarch.exe", SimpleNamespace(config=None)), root / "playlists")
