import tempfile
import unittest
from pathlib import Path

from distill.systems import detect_system, folder_hint, leading_hint, region_of
from tests.helpers import build_iso


def raw_sectors(path: Path, iso: Path, offset: int) -> Path:
    """Re-wrap a plain ISO as raw 2352-byte CD sectors (data starts `offset` bytes in), like a .bin."""
    data = iso.read_bytes()
    out = bytearray()
    for i in range(0, len(data), 2048):
        out += b"\x00" * offset + data[i:i + 2048].ljust(2048, b"\x00") + b"\x00" * (2352 - offset - 2048)
    path.write_bytes(bytes(out))
    return path


class DetectSystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="distill_sys_")).resolve()

    def test_extensions(self):
        for name, system in (("a.gba", "Game Boy Advance"), ("a.sfc", "SNES"), ("a.cso", "PSP"), ("a.nsp", "Switch"),
                             ("a.gdi", "Dreamcast"), ("a.wbfs", "Wii")):
            self.assertEqual(detect_system(self.tmp / name), system)

    def test_discs_by_content(self):
        cases = {
            "psp": ({"PSP_GAME": {"PARAM.SFO": b"x"}}, "PSP"),
            "ps3": ({"PS3_GAME": {"PARAM.SFO": b"x"}}, "PlayStation 3"),
            "ps2": ({"SYSTEM.CNF": b"BOOT2 = cdrom0:\\SLUS_000.00;1\n"}, "PlayStation 2"),
            "ps1": ({"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_000.00;1\n"}, "PlayStation"),
        }
        for name, (tree, expected) in cases.items():
            iso = build_iso(self.tmp / f"{name}.iso", tree)
            self.assertEqual(detect_system(iso), expected, name)
            for offset in (16, 24):  # the same disc as a raw .bin (MODE1 / MODE2 sectors)
                self.assertEqual(detect_system(raw_sectors(self.tmp / f"{name}{offset}.bin", iso, offset)), expected)

    def test_gamecube_and_wii_headers(self):
        gc, wii = self.tmp / "g.iso", self.tmp / "w.iso"
        gc.write_bytes(b"\x00" * 0x1C + b"\xc2\x33\x9f\x3d" + b"\x00" * 64)
        wii.write_bytes(b"\x00" * 0x18 + b"\x5d\x1c\x9e\xa3" + b"\x00" * 68)
        self.assertEqual(detect_system(gc), "GameCube")
        self.assertEqual(detect_system(wii), "Wii")
        rvz = self.tmp / "g.rvz"
        rvz.write_bytes(b"RVZ\x01" + b"\x00" * 0x44 + (2).to_bytes(4, "big") + b"\x00" * 16)
        self.assertEqual(detect_system(rvz), "Wii")

    def test_sega_boot_headers(self):
        for text, expected in ((b"SEGA SEGASATURN ", "Sega Saturn"), (b"SEGADISCSYSTEM  ", "Sega CD"),
                                (b"SEGA SEGAKATANA ", "Dreamcast")):
            p = self.tmp / f"{expected}.iso"
            p.write_bytes(text + b"\x00" * 4096)
            self.assertEqual(detect_system(p), expected)

    def test_folder_hints_fill_in_for_chd_and_unreadable_files(self):
        chd = self.tmp / "Sony PlayStation 2" / "Games" / "Game.chd"
        chd.parent.mkdir(parents=True)
        chd.write_bytes(b"MComprHD")
        self.assertEqual(detect_system(chd, self.tmp), "PlayStation 2")
        self.assertEqual(detect_system(self.tmp / "Game.chd", self.tmp), "")  # nothing to go on: not guessed
        self.assertEqual(folder_hint(chd, self.tmp), "PlayStation 2")
        self.assertEqual(leading_hint(chd, self.tmp), "PlayStation 2")

    def test_content_beats_folder_name(self):
        (self.tmp / "PS2").mkdir()
        iso = build_iso(self.tmp / "PS2" / "x.iso", {"SYSTEM.CNF": b"BOOT = cdrom:\\SLUS_000.00;1\n"})
        self.assertEqual(detect_system(iso, self.tmp), "PlayStation")  # a PS1 disc filed under PS2

    def test_region_of(self):
        self.assertEqual(region_of("Game (USA) (Rev 1)"), "USA")
        self.assertEqual(region_of("Game (USA, Europe)"), "USA, Europe")
        self.assertEqual(region_of("Game (Disc 1)"), "")


if __name__ == "__main__":
    unittest.main()
