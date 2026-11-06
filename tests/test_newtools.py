"""1G1R, front-end presets, DAT verification, patch applier, BIN/CUE repair and the size estimate."""
import hashlib
import unittest
import zlib
from pathlib import Path

from distill import cuebin, organize, orgpages, patcher
from distill.datverify import load_dat, verify
from distill.estimate import estimate
from distill.frontends import folder_for, system_for_folder
from distill.onegr import choose, parse_priority
from tests.test_org_features import Base, NEVER


def _vnum(n: int) -> bytes:  # UPS / BPS number encoding
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n:
            out.append(x)
            n -= 1
        else:
            out.append(0x80 | x)
            return bytes(out)


def _crc(b: bytes) -> bytes:
    return (zlib.crc32(b) & 0xFFFFFFFF).to_bytes(4, "little")


class OneGameTests(Base):
    P = parse_priority("USA, Europe, Japan")

    def test_choice(self):
        self.assertEqual(choose(["G (Japan)", "G (Europe)", "G (USA)", "G (USA) (Beta)"], self.P), "G (USA)")
        self.assertEqual(choose(["G (USA)", "G (USA) (Rev 1)"], self.P), "G (USA) (Rev 1)")
        self.assertEqual(choose(["G (Japan) (Beta)", "G (Japan)"], self.P), "G (Japan)")
        self.assertEqual(choose(["G (World)", "G (Japan)"], self.P), "G (World)")
        self.assertEqual(choose(["G (USA, Europe)", "G (Europe)"], self.P), "G (USA, Europe)")

    def test_finding_moves_the_rest_aside_and_undoes(self):
        for n in ("Zelda (USA).gba", "Zelda (Europe).gba", "Zelda (Japan).gba", "Mario (Japan).gba"):
            self.make(n)
        found, _ = organize.find_onegr(self.root, False, {"other_files": True}, NEVER)
        self.assertEqual(len(found), 1)
        self.apply("onegr", found)
        self.assertTrue((self.root / "Zelda (USA).gba").exists())
        self.assertTrue((self.root / "Mario (Japan).gba").exists())
        self.assertFalse((self.root / "Zelda (Japan).gba").exists())
        organize.undo_last("onegr")
        self.assertTrue((self.root / "Zelda (Japan).gba").exists())

    def test_discs_are_not_mixed(self):
        for n in ("Game (USA) (Disc 1).chd", "Game (USA) (Disc 2).chd", "Game (Europe) (Disc 1).chd"):
            self.make(n)
        found, _ = organize.find_onegr(self.root, False, {}, NEVER)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].title, "Game (USA) (Disc 1)")


class PresetTests(Base):
    def test_names(self):
        self.assertEqual(folder_for("ES-DE", "Nintendo 3DS"), "n3ds")
        self.assertEqual(folder_for("Batocera", "GameCube"), "gamecube")
        self.assertEqual(folder_for("LaunchBox", "Dreamcast"), "Sega Dreamcast")
        self.assertEqual(folder_for("Plain system names", "Dreamcast"), "Dreamcast")
        self.assertEqual(system_for_folder("n3ds"), "Nintendo 3DS")

    def test_sort_uses_preset(self):
        self.make("Pokemon.gba")
        found, _ = organize.find_by_system(self.root, False, {"preset": "ES-DE"}, NEVER)
        self.apply("bysystem", found)
        self.assertTrue((self.root / "gba" / "Pokemon.gba").exists())
        again, _ = organize.find_by_system(self.root, True, {"preset": "ES-DE"}, NEVER)
        self.assertEqual(again, [])  # already in place


class DatTests(Base):
    def dat(self, data: bytes, name="Real Name (USA).gba") -> Path:
        xml = (f'<?xml version="1.0"?><datafile><header><name>Test</name></header><game name="Real Name (USA)">'
               f'<rom name="{name}" size="{len(data)}" crc="{zlib.crc32(data) & 0xFFFFFFFF:08x}" '
               f'md5="{hashlib.md5(data).hexdigest()}" sha1="{hashlib.sha1(data).hexdigest()}"/></game>'
               f'<game name="Other (USA)"><rom name="Other (USA).gba" size="3" crc="00000001"/></game></datafile>')
        p = self.root / "t.dat"
        p.write_text(xml)
        return p

    def test_verify_and_rename(self):
        data = b"abc" * 1000
        dat = load_dat(self.dat(data))
        good, bad, odd = self.make("wrong.gba", data), self.make("Other (USA).gba", b"zzz"), self.make("x.gba", b"q")
        rep = verify([good, bad, odd], dat, NEVER)
        self.assertEqual([v.canonical for v in rep.ok], ["Real Name (USA).gba"])
        self.assertEqual([p.name for p, _g in rep.bad], ["Other (USA).gba"])
        self.assertEqual([p.name for p in rep.unknown], ["x.gba"])
        self.assertEqual([g.name for g in rep.missing], ["Other (USA)"])

    def test_tool_renames(self):
        data = b"abc" * 1000
        self.make("wrong.gba", data)
        found, msg = organize.find_dat_verify(self.root, False, {"dat": str(self.dat(data))}, NEVER)
        self.assertIn("1 of 1", msg)
        self.apply("datcheck", [f for f in found if f.ops])
        self.assertTrue((self.root / "Real Name (USA).gba").exists())

    def test_no_dat_means_skip(self):
        self.assertEqual(organize.find_dat_verify(self.root, False, {}, NEVER), ([], ""))

    def test_clrmamepro_text(self):
        d = b"hello world"
        p = self.root / "c.dat"
        p.write_text(f'clrmamepro (\n name "Set"\n)\ngame (\n name "Hi (USA)"\n'
                     f' rom ( name "Hi (USA).nes" size {len(d)} crc {zlib.crc32(d) & 0xFFFFFFFF:08x} )\n)\n')
        dat = load_dat(p)
        self.assertEqual((dat.name, dat.games[0].name, dat.games[0].roms[0].size), ("Set", "Hi (USA)", 11))


class PatchTests(Base):
    SRC = bytes(range(256)) * 8

    def test_ips(self):
        patch = b"PATCH" + (10).to_bytes(3, "big") + (2).to_bytes(2, "big") + b"\xaa\xbb" \
            + (20).to_bytes(3, "big") + b"\0\0" + (5).to_bytes(2, "big") + b"\xff" + b"EOF"
        out, _ = patcher.apply_ips(self.SRC, patch)
        self.assertEqual(out[10:12], b"\xaa\xbb")
        self.assertEqual(out[20:25], b"\xff" * 5)

    def test_ups_roundtrip_and_bad_source(self):
        tgt = bytearray(self.SRC)
        tgt[5] ^= 0x55
        body = b"UPS1" + _vnum(len(self.SRC)) + _vnum(len(tgt)) + _vnum(5) + bytes([0x55, 0])
        body += _crc(self.SRC) + _crc(bytes(tgt))
        patch = body + _crc(body)
        out, _ = patcher.apply_ups(self.SRC, patch)
        self.assertEqual(out, bytes(tgt))
        with self.assertRaises(patcher.PatchError):
            patcher.apply_ups(b"nope" * 100, patch)

    def test_bps(self):
        tgt = self.SRC[:100] + b"NEW!" + self.SRC[104:]
        acts = _vnum(((100 - 1) << 2) | 0) + _vnum(((4 - 1) << 2) | 1) + b"NEW!" + _vnum(((len(self.SRC) - 104 - 1) << 2) | 0)
        body = b"BPS1" + _vnum(len(self.SRC)) + _vnum(len(tgt)) + _vnum(0) + acts + _crc(self.SRC) + _crc(tgt)
        patch = body + _crc(body)
        out, notes = patcher.apply_bps(self.SRC, patch)
        self.assertEqual(out, tgt)
        with self.assertRaises(patcher.PatchError):
            patcher.apply_bps(self.SRC[:-1] + b"\0", patch)

    def test_apply_patch_never_overwrites(self):
        src = self.make("g.gba", self.SRC)
        pat = self.make("fix.ips", b"PATCH" + (0).to_bytes(3, "big") + (1).to_bytes(2, "big") + b"\x01EOF")
        out, _ = patcher.apply_patch(src, pat)
        self.assertEqual(out.name, "g (fix).gba")
        self.assertEqual(src.read_bytes(), self.SRC)


class CueTests(Base):
    def make_disc(self):
        a, b = b"A" * 2352 * 10, b"B" * 2352 * 5
        self.make("d (Track 1).bin", a)
        self.make("d (Track 2).bin", b)
        cue = self.make("d.cue", (b'FILE "d (Track 1).bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'
                                  b'FILE "d (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n'
                                  b'    INDEX 01 00:00:02\n'))
        return cue, a, b

    def test_merge_then_split(self):
        cue, a, b = self.make_disc()
        merged = cuebin.merge_cue(cue, self.root / "m" / "disc.cue") if (self.root / "m").mkdir() is None else None
        tracks = cuebin.parse_cue(merged)
        self.assertEqual([t.indexes for t in tracks], [{1: 0}, {0: 10, 1: 12}])
        self.assertEqual(merged.with_suffix(".bin").read_bytes(), a + b)
        split = cuebin.split_cue(merged, self.root / "s")
        self.assertEqual((self.root / "s" / "disc (Track 1).bin").read_bytes(), a)
        self.assertEqual((self.root / "s" / "disc (Track 2).bin").read_bytes(), b)
        self.assertEqual(cuebin.parse_cue(split)[1].indexes, {0: 0, 1: 2})

    def test_refuses_to_overwrite(self):
        cue, *_ = self.make_disc()
        (self.root / "o").mkdir()
        (self.root / "o" / "x.cue").write_text("x")
        with self.assertRaises(cuebin.CueError):
            cuebin.merge_cue(cue, self.root / "o" / "x.cue")

    def test_ccd(self):
        self.make("g.img", b"I" * 2352 * 4)
        ccd = self.make("g.ccd", b"[CloneCD]\nVersion=3\n[TRACK 1]\nMODE=1\nINDEX 1=0\n[TRACK 2]\nMODE=0\nINDEX 0=2\nINDEX 1=3\n")
        cue = cuebin.ccd_to_cue(ccd)
        t = cuebin.parse_cue(cue)
        self.assertEqual((t[0].mode, t[1].mode, t[1].indexes), ("MODE1/2352", "AUDIO", {0: 2, 1: 3}))
        self.assertTrue((self.root / "g.bin").exists())


class EstimateTests(Base):
    def test_estimate(self):
        z = self.make("z.iso", b"\0" * (4 << 20))
        r = self.make("r.iso", __import__("os").urandom(2 << 20))
        e = estimate([z, r], "CHD")
        self.assertEqual(e.before, 6 << 20)
        self.assertLess(e.after, 2.3 * (1 << 20))
        self.assertGreater(e.saved, 3.5 * (1 << 20))


if __name__ == "__main__":
    unittest.main()


class BiosRouterTests(Base):
    def test_bios(self):
        import json
        from distill import bios
        self.make("good.bin", b"abc")
        self.make("sub/moved.bin", b"zzz")
        db = {"S": [{"file": "good.bin", "md5": hashlib.md5(b"abc").hexdigest()}, {"file": "moved.bin", "md5": "0" * 32},
                    {"file": "gone.bin", "md5": "1" * 32, "required": False}]}
        p = self.make("db.json", json.dumps(db).encode())
        res = bios.check(self.root, bios.load_db(p), NEVER)
        self.assertEqual([(r.file, r.status) for r in res], [("good.bin", "ok"), ("moved.bin", "bad"), ("gone.bin", "missing")])

    def test_router(self):
        from distill.router import route
        for name, tool in (("a.ips", "patch"), ("g.nsp", "nsz"), ("d.ccd", "cuebin"), ("x.iso", "convert"), ("t.dat", "datcheck")):
            self.assertEqual(route(self.make(name))[0], tool)
        self.assertEqual(route(self.root)[0], "report")


class UiToolTests(Base):
    def test_discfix_merge_replaces_and_undoes(self):
        a, b = b"A" * 2352 * 4, b"B" * 2352 * 3
        self.make("g (Track 1).bin", a)
        self.make("g (Track 2).bin", b)
        self.make("g.cue", b'FILE "g (Track 1).bin" BINARY\n TRACK 01 MODE1/2352\n  INDEX 01 00:00:00\n'
                           b'FILE "g (Track 2).bin" BINARY\n TRACK 02 AUDIO\n  INDEX 01 00:00:00\n')
        found, _ = organize.find_discfix(self.root, False, {"action": organize.DISC_ACTIONS[0]}, NEVER)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].problem, "")
        ok, fails = self.apply("discfix", found)
        self.assertEqual((ok, fails), (1, []))
        self.assertEqual((self.root / "g.bin").read_bytes(), a + b)
        self.assertFalse((self.root / "g (Track 1).bin").exists())
        self.assertFalse((self.root / ".distill_tmp_g").exists())
        organize.undo_last("discfix")
        self.assertTrue((self.root / "g (Track 1).bin").exists())
        self.assertFalse((self.root / "g.bin").exists())
        self.assertEqual(len(cuebin.parse_cue(self.root / "g.cue")), 2)

    def test_discfix_ccd_keep_originals(self):
        self.make("c.img", b"I" * 2352 * 3)
        self.make("c.ccd", b"[TRACK 1]\nMODE=1\nINDEX 1=0\n")
        found, _ = organize.find_discfix(self.root, False, {"action": organize.DISC_ACTIONS[2], "replace": False}, NEVER)
        self.apply("discfix", found)
        self.assertTrue((self.root / "c (converted)" / "c.cue").exists())
        self.assertTrue((self.root / "c.img").exists())

    def test_bios_step_reports_and_skips(self):
        import json
        self.make("a.bin", b"abc")
        db = self.make("b.json", json.dumps({"S": [{"file": "a.bin", "md5": hashlib.md5(b"abc").hexdigest()},
                                                   {"file": "m.bin", "md5": "0" * 32}]}).encode())
        self.assertEqual(organize.find_bios(self.root, False, {}, NEVER), ([], ""))
        found, msg = organize.find_bios(self.root, False, {"biosdb": str(db)}, NEVER)
        self.assertEqual((found[0].title, found[0].count, found[0].ops), ("S", "1/2", []))
        self.assertIn("1 problem", msg)


class OptionKindTests(unittest.TestCase):
    def test_every_option_kind_is_one_the_page_can_draw(self):
        for tool in orgpages.ORG_TOOLS.values():
            for opt in tool.options:
                self.assertIn(opt[1], ("check", "choice", "folder", "file", "text", "switch"), (tool.key, opt[0]))
                if opt[1] == "file":
                    self.assertEqual(len(opt), 6, opt[0])  # placeholder and dialog filter given
                if opt[1] == "switch":
                    self.assertEqual(len(opt), 7, opt[0])  # off text, on text and tooltip given


def _bps(src: bytes, tgt: bytes) -> bytes:
    first = next(i for i in range(len(src)) if src[i] != tgt[i])
    acts = (_vnum(((first - 1) << 2) | 0) if first else b"") + _vnum(((len(tgt) - first - 1) << 2) | 1) + tgt[first:]
    body = b"BPS1" + _vnum(len(src)) + _vnum(len(tgt)) + _vnum(0) + acts + _crc(src) + _crc(tgt)
    return body + _crc(body)


class PatchPageTests(Base):
    SRC = bytes(range(256)) * 4

    def setUp(self):
        super().setUp()
        self.rom = self.make("Mother 3 (Japan).gba", self.SRC)
        self.t1 = self.SRC[:10] + b"HI" + self.SRC[12:]
        self.t2 = self.SRC[:20] + b"YO" + self.SRC[22:]

    def scan(self, **o):
        return organize.find_patch_games(self.root, True, o, NEVER)

    def test_lists_games_with_checksum_matched_ticked_patches(self):
        self.make("other.gba", b"x" * 500)
        self.make("fan/English.bps", _bps(self.SRC, self.t1))
        found, _ = self.scan()
        self.assertEqual([f.title for f in found], ["Mother 3 (Japan).gba"])  # the game without a patch is hidden
        c = found[0].data[0]
        self.assertTrue(c.ticked and c.verified)
        self.assertEqual(len(self.scan(show_all=True)[0]), 2)

    def test_wrong_rom_is_reported_not_ticked(self):
        self.make("Mother 3 English.bps", _bps(bytes(range(255, -1, -1)) * 4, self.SRC))
        found, _ = self.scan()
        c = found[0].data[0]
        self.assertFalse(c.ticked)
        self.assertIn("Wrong ROM", c.problem)

    def test_ips_by_name_is_unticked_and_unmatched_grouped(self):
        self.make("Mother 3 fix.ips", b"PATCH" + (0).to_bytes(3, "big") + (1).to_bytes(2, "big") + b"\x01EOF")
        self.make("elsewhere/nobody.ips", b"PATCH" + b"EOF")
        found, _ = self.scan()
        self.assertFalse(found[0].data[0].ticked)
        self.assertEqual(found[-1].title, organize.UNMATCHED_TITLE)
        self.assertEqual(found[-1].data[0].patch.name, "nobody.ips")

    def test_manual_attach_and_detach(self):
        self.make("lonely.ips", b"PATCH" + b"EOF")
        self.make("g.gba", b"x" * 300)
        found, _ = self.scan(show_all=True)
        patch = self.root / "lonely.ips"
        organize.attach_patch(found, patch, self.root / "g.gba", NEVER)
        game = next(f for f in found if f.title == "g.gba")
        self.assertTrue(game.data[0].ticked)
        self.assertFalse(any(f.title == organize.UNMATCHED_TITLE for f in found))
        organize.detach_patch(found, patch)
        self.assertTrue(any(f.title == organize.UNMATCHED_TITLE for f in found))
        self.assertEqual(game.data, [])

    def test_manual_pairs_survive_a_rescan(self):
        self.make("lonely.ips", b"PATCH" + b"EOF")
        found, _ = self.scan(_manual=[(str(self.root / "lonely.ips"), str(self.rom))])
        self.assertEqual(found[0].data[0].how, "manual")

    def test_manual_none_keeps_a_patch_unmatched_on_rescan(self):
        self.make("Mother 3 fix.ips", b"PATCH" + b"EOF")
        found, _ = self.scan(_manual=[(str(self.root / "Mother 3 fix.ips"), "")])
        self.assertEqual([f.title for f in found], [organize.UNMATCHED_TITLE])

    def test_separate_copies_and_undo(self):
        self.make("a.bps", _bps(self.SRC, self.t1))
        self.make("b.bps", _bps(self.SRC, self.t2))
        found, _ = self.scan()
        ticks = {(0, 0), (0, 1)}
        picks = organize.patch_findings(found, ticks, stack=False)
        self.assertEqual([f.title for f in picks], ["Mother 3 (Japan) (a).gba", "Mother 3 (Japan) (b).gba"])
        self.apply("patch", picks)
        self.assertEqual((self.root / "Mother 3 (Japan) (a).gba").read_bytes(), self.t1)
        self.assertEqual((self.root / "Mother 3 (Japan) (b).gba").read_bytes(), self.t2)
        organize.undo_last("patch")
        self.assertFalse((self.root / "Mother 3 (Japan) (a).gba").exists())
        self.assertEqual(self.rom.read_bytes(), self.SRC)

    def test_stacked_applies_in_order_into_one_file(self):
        self.make("a.ips", b"PATCH" + (10).to_bytes(3, "big") + (2).to_bytes(2, "big") + b"HIEOF")
        self.make("b.ips", b"PATCH" + (20).to_bytes(3, "big") + (2).to_bytes(2, "big") + b"YOEOF")
        found, _ = self.scan()
        picks = organize.patch_findings(found, {(0, 0), (0, 1)}, stack=True)
        self.assertEqual(len(picks), 1)
        self.apply("patch", picks)
        out = (self.root / "Mother 3 (Japan) (a + b).gba").read_bytes()
        self.assertEqual((out[10:12], out[20:22]), (b"HI", b"YO"))
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".distill")], [])

    def test_patch_inside_zip_and_no_patch_message(self):
        import zipfile
        found, msg = self.scan()
        self.assertEqual(found, [])
        self.assertIn("1 ROM", msg)
        with zipfile.ZipFile(self.root / "tr.zip", "w") as z:
            z.writestr("m3.ups", b"x")
        found, _ = self.scan()
        self.assertIn("inside this archive", found[0].problem)

    def test_custom_name_separate_and_stacked(self):
        self.make("a.ips", b"PATCH" + (10).to_bytes(3, "big") + (2).to_bytes(2, "big") + b"HIEOF")
        self.make("b.ips", b"PATCH" + (20).to_bytes(3, "big") + (2).to_bytes(2, "big") + b"YOEOF")
        found, _ = self.scan()
        a, b = (self.root / "a.ips"), (self.root / "b.ips")
        names = {organize.name_key(self.rom, [a]): "Mother 3 English", organize.name_key(self.rom, [a, b]): "M3 full: v2"}
        sep = organize.patch_findings(found, {(0, 0), (0, 1)}, False, names)
        self.assertEqual([f.title for f in sep], ["Mother 3 English.gba", "Mother 3 (Japan) (b).gba"])
        stk = organize.patch_findings(found, {(0, 0), (0, 1)}, True, names)
        self.assertEqual(stk[0].title, "M3 full v2.gba")  # illegal ':' dropped, extension added
        self.apply("patch", stk)
        self.assertTrue((self.root / "M3 full v2.gba").exists())

    def test_custom_name_never_overwrites(self):
        self.make("Mother 3 a.ips", b"PATCH" + b"EOF")
        (self.root / "sub").mkdir()
        (self.root / "Taken.gba").write_bytes(b"z")
        found, _ = self.scan()
        names = {organize.name_key(self.rom, [self.root / "Mother 3 a.ips"]): "Taken.gba"}
        plan = organize.patch_findings(found, {(0, 0)}, False, names)
        self.assertEqual(plan[0].title, "Taken (2).gba")
