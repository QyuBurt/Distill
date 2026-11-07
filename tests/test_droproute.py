import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from distill.droproute import route_drop
from distill.formats import PKG_MAGIC, PS5_PKG_MAGIC, XDVDFS_MAGIC


def iso_with_root(*names: str) -> bytes:
    """A minimal ISO 9660 image whose root folder holds these directories."""
    pvd = bytearray(2048)
    pvd[0], pvd[1:6] = 1, b"CD001"
    root_sector = 20
    pvd[158:162] = struct.pack("<I", root_sector)
    pvd[166:170] = struct.pack("<I", 2048)
    records = bytearray()
    for n in names:
        raw = n.encode()
        rec = bytearray(33 + len(raw) + (len(raw) + 1) % 2)
        rec[0] = len(rec)
        rec[2:6] = struct.pack("<I", 30)
        rec[10:14] = struct.pack("<I", 2048)
        rec[25] = 2
        rec[32] = len(raw)
        rec[33:33 + len(raw)] = raw
        records += rec
    image = bytearray(22 * 2048 + 2048)
    image[16 * 2048:17 * 2048] = pvd
    image[root_sector * 2048:root_sector * 2048 + len(records)] = records
    return bytes(image)


class RouteDropTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def make(self, name: str, data: bytes = b"x") -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def page(self, *paths):
        t = route_drop(list(paths))
        return t.page if t else None

    def test_extensions(self):
        self.assertEqual(self.page(self.make("a.cue")), "chd")
        self.assertEqual(self.page(self.make("a.gcm")), "rvz")
        self.assertEqual(self.page(self.make("a.nsp")), "switch")
        self.assertEqual(self.page(self.make("a.nup")), "switch")
        self.assertEqual(self.page(self.make("a.bps")), "patch")
        self.assertEqual(self.page(self.make("a.ccd")), "fix")
        self.assertEqual(self.page(self.make("a.torrent")), "dl")
        self.assertIsNone(self.page(self.make("notes.txt")))

    def test_isos_are_told_apart_by_their_contents(self):
        self.assertEqual(self.page(self.make("psp.iso", iso_with_root("PSP_GAME"))), "cso")
        self.assertEqual(self.page(self.make("ps3.iso", iso_with_root("PS3_GAME"))), "ps3")
        self.assertEqual(self.page(self.make("ps2.iso", iso_with_root("SOMETHING"))), "chd")
        gc = bytearray(0x40)
        gc[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
        self.assertEqual(self.page(self.make("gc.iso", bytes(gc))), "rvz")
        xbox = bytearray(33 * 2048 + 64)
        xbox[32 * 2048:32 * 2048 + len(XDVDFS_MAGIC)] = XDVDFS_MAGIC
        self.assertEqual(self.page(self.make("xbox.iso", bytes(xbox))), "xbox")

    def test_pkg_generation(self):
        self.assertEqual(self.page(self.make("ps4.pkg", PKG_MAGIC + b"\0" * 64)), "pkg")
        self.assertEqual(self.page(self.make("ps5.pkg", PS5_PKG_MAGIC + b"\0" * 64)), "pkg5")
        self.assertIsNone(self.page(self.make("ps3.pkg", b"\x7fPKG" + b"\0" * 64)))

    def test_one_file_runs_alone_from_its_folder(self):
        cue = self.make("Game.cue")
        t = route_drop([cue])
        self.assertEqual((t.source, t.only), (self.tmp, (cue,)))

    def test_cue_dropped_with_its_bin_and_scattered_drops(self):
        cue, binf = self.make("Game.cue"), self.make("Game.bin")
        self.assertEqual(route_drop([cue, binf]).only, (cue,))  # the track file is just a companion
        other = self.make("sub/Other.cue")
        self.assertIsNone(route_drop([cue, other]))  # different folders
        self.assertIsNone(route_drop([cue, self.make("a.gcm")]))  # different pages

    def test_dat_and_bios_fill_the_report_page(self):
        t = route_drop([self.make("lib.dat")])
        self.assertEqual((t.page, t.option[0], t.step, t.source), ("report", "dat", "datcheck", None))
        t = route_drop([self.make("bios.json", b'{"ps1": {"md5": "x"}}')])
        self.assertEqual((t.page, t.option[0], t.step), ("report", "biosdb", "bioscheck"))

    def test_zip_by_contents(self):
        def zipped(name, **members):
            p = self.tmp / name
            with zipfile.ZipFile(p, "w") as z:
                for n, data in members.items():
                    z.writestr(n.replace("__", "/").replace("_dot_", "."), data)
            return p
        psp = zipped("psp.zip", game_dot_iso=iso_with_root("PSP_GAME"))
        t = route_drop([psp])
        self.assertEqual((t.page, t.archive, t.only), ("cso", True, (psp,)))
        self.assertEqual(route_drop([zipped("ps4.zip", g_dot_pkg=PKG_MAGIC + b"\0" * 64)]).page, "pkg")
        self.assertEqual(route_drop([zipped("cd.zip", g_dot_cue="x")]).page, "chd")
        self.assertEqual(route_drop([zipped("sw.zip", g_dot_nsp="x")]).switch.archive, True)
        self.assertIsNone(route_drop([zipped("docs.zip", readme_dot_txt="x")]))

    def test_folders(self):
        for n in ("A.gcm", "B.gcm", "C.wbfs"):
            self.make(f"gc/{n}")
        t = route_drop([self.tmp / "gc"])
        self.assertEqual((t.page, t.source, t.only), ("rvz", self.tmp / "gc", ()))
        self.make("ps4/Game/sce_sys/param.sfo")
        self.assertEqual(route_drop([self.tmp / "ps4"]).page, "zar")
        one = route_drop([self.tmp / "ps4" / "Game"])
        self.assertEqual((one.page, one.source, one.only), ("zar", self.tmp / "ps4", (self.tmp / "ps4" / "Game",)))

    def test_an_unpacked_ps3_game_goes_to_the_ps3_page_not_zar(self):
        for name in ("PS3_GAME/PARAM.SFO", "PS3_GAME/USRDIR/EBOOT.BIN", "PS3_DISC.SFB"):
            self.make(f"ps3/Game/{name}")
        self.assertEqual(self.page(self.tmp / "ps3" / "Game"), "ps3")  # the game's own folder
        self.assertEqual(self.page(self.tmp / "ps3"), "ps3")  # a library folder of them
        self.assertEqual(self.page(self.tmp / "ps3" / "Game" / "PS3_GAME"), "ps3")  # its PS3_GAME folder on its own
        self.assertEqual(route_drop([self.tmp / "ps3" / "Game"]).source, self.tmp / "ps3" / "Game")

    def test_a_ps3_folder_beside_ps4_ones_does_not_turn_them_into_ps3(self):
        self.make("mix/PS3Game/PS3_GAME/PARAM.SFO")
        self.make("mix/PS3Game/PS3_GAME/USRDIR/EBOOT.BIN")
        for n in ("A", "B", "C"):
            self.make(f"mix/{n}/sce_sys/param.sfo")
        self.assertEqual(self.page(self.tmp / "mix"), "zar")  # three PS4 folders outnumber the one PS3 game
        (self.tmp / "dump" / "exefs").mkdir(parents=True)
        t = route_drop([self.tmp / "dump"])
        self.assertEqual((t.page, t.switch.action), ("switch", "pack"))
        (self.tmp / "empty").mkdir()
        self.assertIsNone(route_drop([self.tmp / "empty"]))


if __name__ == "__main__":
    unittest.main()
