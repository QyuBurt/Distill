"""Switch conversions: the pure readers, the grouping, and the runner against stand-in nscb_rust / nup / hactoolnet."""
from __future__ import annotations

import struct
import threading
import unittest
from pathlib import Path

from tests.helpers import make_tool, POSIX_ONLY, TempDirTest
from distill.switchconv import (
    check_prod_keys, find_image, identify_files, link_value, review_files, group_key, is_nx_dump, parse_cnmt, parse_nacp, parse_nca_info, plain_name, plan_groups,
    classify_switch_drop, dump_title_id, find_nup_titles, npdm_program_id, read_title_keys, SwitchConfig, SwitchRunner, ticket_title_key, title_id_of, title_kind, TitleEdit,
)

BASE, UPD, DLC = "0100633007D48000", "0100633007D48800", "0100633007D49001"

FAKE_NSCB = '''
a = sys.argv[1:]
out = pathlib.Path(a[a.index("-o") + 1]) if "-o" in a else None
if "--help" in a:
    print("nscb usage"); sys.exit(0)
if "--compress" in a:
    src = pathlib.Path(a[a.index("--compress") + 1])
    if "BROKEN" in src.name:
        print("Error: simulated failure"); sys.exit(1)
    print("50%")
    (out / (src.stem + (".nsz" if src.suffix == ".nsp" else ".xcz"))).write_bytes(b"z" * 10)
elif "--decompress" in a:
    src = pathlib.Path(a[a.index("--decompress") + 1])
    (out / (src.stem + (".nsp" if src.suffix == ".nsz" else ".xci"))).write_bytes(b"d" * 100)
elif "-d" in a:
    typ = a[a.index("--type") + 1] if "--type" in a else "nsp"
    (out / ("merged." + typ)).write_bytes(b"m" * 50)
elif "--verify" in a:
    print("ok")
else:
    sys.exit(2)
'''

FAKE_NUP = '''
a = sys.argv[1:]
if a[0] == "pack":
    pathlib.Path(a[2]).write_text(" ".join(a))
elif a[0] in ("verify", "edit"):
    if a[0] == "edit":
        pathlib.Path(a[1]).write_text(" ".join(a))
else:
    sys.exit(2)
'''


def run(cfg: SwitchConfig, only=None):
    events: list[tuple] = []
    pause = threading.Event()
    pause.set()
    SwitchRunner(cfg, events.append, pause, only).run()
    done = [e for e in events if e and e[0] == "done"]
    return events, done[-1][1]


class PureTests(unittest.TestCase):
    def test_title_ids_and_kinds(self):
        self.assertEqual(title_id_of(f"Game [{BASE.lower()}][v0].nsp"), BASE)
        self.assertEqual([title_kind(t) for t in (BASE, UPD, DLC)], ["base", "update", "dlc"])
        self.assertEqual({group_key(t) for t in (BASE, UPD, DLC)}, {group_key(BASE)})

    def test_groups_need_a_game_and_a_second_piece(self):
        files = [Path(f"Game [{BASE}].nsp"), Path(f"Game [{UPD}][v65536].nsp"), Path(f"Game [{DLC}].nsp"),
                 Path("Lonely [0100000000001000].nsp"), Path("NoId.nsp"), Path("Patch only [0100AAAAAAAAA800].nsp")]
        groups, notes = plan_groups(files)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0].name, f"Game [{BASE}].nsp")  # the game first
        self.assertEqual(len(notes), 3)

    def test_plain_name(self):
        self.assertEqual(plain_name(Path(f"Super: Game [{BASE}][v0] (Update).nsp")), "Super Game")

    def test_ticket_title_key(self):
        tik = struct.pack("<I", 0x10004) + b"\0" * 0x100 + b"\0" * 0x3C
        body = bytearray(0x2C0)
        body[0x40:0x50] = bytes(range(16))
        body[0x160:0x170] = bytes([0xAA] * 16)
        rid, key = ticket_title_key(tik + bytes(body))
        self.assertEqual((rid, key), ("AA" * 16, bytes(range(16)).hex().upper()))
        self.assertIsNone(ticket_title_key(b"junk"))

    def test_nacp_cnmt_and_nca_info(self):
        nacp = bytearray(0x4000)
        nacp[0x300:0x300 + 4] = b"Titl"  # language 1 (British English) is the first filled in
        nacp[0x300 + 0x200:0x300 + 0x200 + 3] = b"Pub"
        nacp[0x3060:0x3065] = b"1.2.3"
        self.assertEqual(parse_nacp(bytes(nacp)), {"name": "Titl", "publisher": "Pub", "version": "1.2.3"})
        cnmt = struct.pack("<QI", int(UPD, 16), 65536) + b"\0" + b"\x81" + b"\0" * 30
        cnmt = bytearray(cnmt)
        cnmt[0xC] = 0x81
        self.assertEqual(parse_cnmt(bytes(cnmt)), (UPD, 65536, 0x81))
        self.assertEqual(parse_nca_info(f"Title ID:  {BASE.lower()}\nContent Type:     Program\n"), ("Program", BASE))

    def test_title_keys_and_prod_keys(self):
        d = TempDirTest()
        d.setUp()
        self.addCleanup(d.tearDown)
        tk = d.write("title.keys", f"{'ab' * 16},{'cd' * 16}\n{'11' * 16} = {'22' * 16}\n# c\n")
        self.assertEqual(len(read_title_keys(tk)), 2)
        self.assertIn("No prod.keys", check_prod_keys(""))
        self.assertIn("doesn't look", check_prod_keys(str(tk)))
        self.assertEqual(check_prod_keys(str(d.write("prod.keys", "master_key_00 = 00\n"))), "")

    def test_dump_detection_and_artwork_lookup(self):
        d = TempDirTest()
        d.setUp()
        self.addCleanup(d.tearDown)
        (d.tmp / "a" / "exefs").mkdir(parents=True)
        (d.tmp / "b" / "updates" / "65536" / "romfs").mkdir(parents=True)
        (d.tmp / "c").mkdir()
        self.assertEqual([is_nx_dump(d.tmp / n) for n in "abc"], [True, True, False])
        img = d.write("Game.PNG")
        self.assertEqual(find_image(["Nope", "Game"], d.tmp), img)


def make_nsp(path: Path, title_id: str) -> Path:
    """A PFS0 holding one ticket whose rights id starts with the title id (what an eShop .nsp looks like)."""
    tik = struct.pack("<I", 0x10004) + b"\0" * 0x100 + b"\0" * 0x3C + bytes(0x2C0)
    tik = bytearray(tik)
    body = 0x140
    tik[body + 0x160:body + 0x168] = bytes.fromhex(title_id)
    name = b"x.tik\0"
    head = b"PFS0" + struct.pack("<II", 1, len(name)) + b"\0" * 4
    entry = struct.pack("<QQI", 0, len(tik), 0) + b"\0" * 4
    path.write_bytes(head + entry + name + bytes(tik))
    return path


class IdentifyTests(TempDirTest):
    def test_the_ticket_inside_wins_over_the_name_and_links_win_over_both(self):
        a = make_nsp(self.tmp / "no id in this name.nsp", UPD)
        b = make_nsp(self.tmp / f"Liar [{BASE}].nsp", DLC)
        c = self.write("mystery.xci", "x")
        ids, notes = identify_files([a, b, c])
        self.assertEqual((ids[a], ids[b], c in ids), (UPD, DLC, False))
        self.assertEqual(len(notes), 1)
        ids, games, unknown = review_files([make_nsp(self.tmp / "Game.nsp", BASE), a, c], {str(c): link_value(BASE)})
        self.assertEqual((games, unknown), ({BASE: "Game"}, []))
        self.assertEqual(title_kind(ids[c]), "dlc")
        self.assertEqual(group_key(ids[c]), group_key(BASE))

    def test_merge_groups_files_with_no_ids_in_their_names(self):
        for n, t in (("a.nsp", BASE), ("b.nsp", UPD), ("c.nsp", DLC)):
            make_nsp(self.tmp / n, t)
        groups, notes = plan_groups(sorted(self.tmp.iterdir()), identify_files(sorted(self.tmp.iterdir()))[0])
        self.assertEqual([[p.name for p in g] for g in groups], [["a.nsp", "c.nsp", "b.nsp"]])


class DropTests(TempDirTest):
    def test_single_files_route_by_extension_and_keep_the_file(self):
        nsp, nsz, nup = (self.tmp / n for n in ("A.nsp", "B.nsz", "C.nup"))
        for p in (nsp, nsz, nup):
            p.write_bytes(b"x")
        got = classify_switch_drop([nsp])
        self.assertEqual((got.side, got.action, got.source, got.only), ("s1", "compress", self.tmp, (nsp,)))
        self.assertEqual(classify_switch_drop([nsz]).action, "decompress")
        self.assertEqual((classify_switch_drop([nup]).side, classify_switch_drop([nup]).action), ("s2", "edit"))

    def test_mixed_or_scattered_drops_are_refused(self):
        a, b = self.tmp / "A.nsp", self.tmp / "B.nup"
        (self.tmp / "sub").mkdir()
        c = self.tmp / "sub" / "C.nsp"
        for p in (a, b, c):
            p.write_bytes(b"x")
        self.assertIsNone(classify_switch_drop([a, b]))  # different kinds
        self.assertIsNone(classify_switch_drop([a, c]))  # different folders
        self.assertIsNone(classify_switch_drop([self.tmp / "notes.txt"]))
        d = self.tmp / "D.nsp"
        d.write_bytes(b"x")
        self.assertEqual(len(classify_switch_drop([a, d]).only), 2)  # several files from one folder are fine

    def test_dump_folder_and_plain_folder(self):
        dump = self.tmp / "Game"
        (dump / "exefs").mkdir(parents=True)
        got = classify_switch_drop([dump])
        self.assertEqual((got.side, got.action, got.source, got.only), ("s2", "pack", dump, ()))
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "x.xci").write_bytes(b"x")
        self.assertEqual(classify_switch_drop([lib]).action, "compress")
        self.assertIsNone(classify_switch_drop([self.tmp / "empty"]))

    def test_zip_is_recognised_by_its_contents_without_extracting(self):
        import zipfile
        s2, s1, other = (self.tmp / n for n in ("dump.zip", "games.zip", "docs.zip"))
        with zipfile.ZipFile(s2, "w") as z:
            z.writestr("My Game/exefs/main", "x")
            z.writestr("My Game/romfs/data", "x")
        with zipfile.ZipFile(s1, "w") as z:
            z.writestr("Game [0100633007D48000].nsp", "x")
        with zipfile.ZipFile(other, "w") as z:
            z.writestr("readme.txt", "x")
        got = classify_switch_drop([s2])
        self.assertEqual((got.side, got.action, got.archive), ("s2", "pack", True))
        self.assertEqual((classify_switch_drop([s1]).side, classify_switch_drop([s1]).archive), ("s1", True))
        self.assertIsNone(classify_switch_drop([other]))


@POSIX_ONLY
class RunnerTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.nscb = str(make_tool(self.tmp / "nscb", FAKE_NSCB))
        self.nup = str(make_tool(self.tmp / "nup.py", FAKE_NUP))
        self.keys = str(self.write("prod.keys", "master_key_00 = 00\n"))
        self.src = self.tmp / "src"
        self.src.mkdir()

    def cfg(self, **kw) -> SwitchConfig:
        base = dict(side="s1", action="compress", source=self.src, dest=None, nscb=self.nscb, nup=self.nup, prod_keys=self.keys)
        return SwitchConfig(**{**base, **kw})

    def test_compress_decompress_skip_and_failure(self):
        (self.src / "Good.nsp").write_bytes(b"x" * 100)
        (self.src / "BROKEN.xci").write_bytes(b"x" * 100)
        _ev, summary = run(self.cfg())
        self.assertTrue((self.src / "Good.nsz").is_file())
        self.assertFalse((self.src / "BROKEN.xcz").exists())
        self.assertEqual((summary.total, summary.converted, len(summary.failures)), (2, 1, 1))
        self.assertEqual(summary.failures[0].stage, "convert")
        self.assertEqual([p.name for p in self.src.iterdir() if p.name.startswith(".distill")], [])  # scratch is gone
        _ev, again = run(self.cfg())  # the existing result is not redone
        self.assertEqual(again.converted, 1)
        (self.src / "Good.nsp").unlink()
        _ev, back = run(self.cfg(action="decompress", delete=True))
        self.assertTrue((self.src / "Good.nsp").is_file())
        self.assertFalse((self.src / "Good.nsz").exists())  # deleted after success
        self.assertTrue(back.deleted)

    def test_missing_keys_or_tool_stop_the_run_before_it_starts(self):
        ev, summary = run(self.cfg(prod_keys=""))
        self.assertEqual(summary.total, 0)
        self.assertTrue(any("prod.keys" in str(e[1]) for e in ev if e[0] == "log"))

    def test_merge_groups_by_title_id(self):
        for n in (f"Game [{BASE}].nsp", f"Game [{UPD}].nsp", f"Game [{DLC}].nsp", "Other [0100000000001000].nsp"):
            (self.src / n).write_bytes(b"x" * 10)
        _ev, summary = run(self.cfg(action="merge", merge_type="xci", verify=True))
        self.assertEqual((summary.total, summary.converted), (1, 1))
        self.assertTrue((self.src / f"Game [{BASE}] (merged).xci").is_file())

    def test_merge_then_compress(self):
        for n in (f"Game [{BASE}].nsp", f"Game [{UPD}].nsp"):
            (self.src / n).write_bytes(b"x" * 10)
        # the stand-in's merge writes merged.nsp; compressing it yields merged.nsz, which is what gets renamed
        _ev, summary = run(self.cfg(action="merge", merge_compress=True))
        self.assertTrue((self.src / f"Game [{BASE}] (merged).nsz").is_file(), summary)

    def test_nup_pack_from_dump_with_artwork_beside_it(self):
        dump = self.src / "My Game"
        (dump / "exefs").mkdir(parents=True)
        (dump / "romfs").mkdir()
        (self.src / "My Game.png").write_bytes(b"png")
        out = self.tmp / "out"
        _ev, summary = run(self.cfg(side="s2", action="pack", dest=out, level=7, publisher="Acme", display_version="1.0"))
        self.assertEqual((summary.total, summary.converted), (1, 1), summary)
        args = (out / "My Game.nup").read_text()
        self.assertIn("--level 7", args)
        self.assertIn("--publisher Acme", args)
        self.assertIn("--icon " + str(self.src / "My Game.png"), args)
        self.assertTrue(dump.is_dir())  # a dump folder is never deleted
        _ev, none = run(self.cfg(side="s2", action="pack", dest=self.tmp / "o2", art_mode="none"))
        self.assertNotIn("--icon", (self.tmp / "o2" / "My Game.nup").read_text())

    def test_nup_edit_adds_artwork_by_name(self):
        (self.src / "Game.nup").write_text("x")
        art = self.tmp / "art"
        art.mkdir()
        (art / "Game.jpg").write_bytes(b"j")
        (self.src / "Other.nup").write_text("x")
        _ev, summary = run(self.cfg(side="s2", action="edit", art_mode="folder", art_dir=str(art)))
        self.assertIn("--icon " + str(art / "Game.jpg"), (self.src / "Game.nup").read_text())
        self.assertEqual((self.src / "Other.nup").read_text(), "x")  # nothing to set: left alone
        self.assertEqual(summary.converted, 2)

    def test_manage_metadata_lists_titles_and_overrides_one_dump(self):
        for name in ("Alpha", "Beta"):
            (self.src / name / "exefs").mkdir(parents=True)
            (self.src / name / "romfs").mkdir()
        (self.src / "Beta.png").write_bytes(b"png")
        custom = self.tmp / "custom.png"
        custom.write_bytes(b"c")
        titles = find_nup_titles(self.src, "pack")
        self.assertEqual([t.name for t in titles], ["Alpha", "Beta"])
        self.assertTrue(all(t.kind == "Dump" for t in titles))
        beta = next(t for t in titles if t.name == "Beta")
        edit = TitleEdit(name="Beta Deluxe", publisher="Acme", display_version="2.0", icon=str(custom), filename="Beta Final")
        out = self.tmp / "out"
        _ev, summary = run(self.cfg(side="s2", action="pack", dest=out, title_edits=((beta.key, edit),)))
        self.assertEqual(summary.converted, 2, summary)
        changed = (out / "Beta Final.nup").read_text()
        self.assertIn("--name Beta Deluxe", changed)
        self.assertIn("--publisher Acme", changed)
        self.assertIn("--display-version 2.0", changed)
        self.assertIn("--icon " + str(custom), changed)
        plain = (out / "Alpha.nup").read_text()  # the other title is untouched
        self.assertNotIn("--publisher", plain)
        self.assertNotIn("--name", plain)

    def test_manage_metadata_edits_an_existing_nup(self):
        (self.src / "Game.nup").write_text("x")
        (self.src / "Other.nup").write_text("x")
        titles = find_nup_titles(self.src, "edit")
        self.assertEqual([t.kind for t in titles], ["NUP", "NUP"])
        game = next(t for t in titles if t.name == "Game")
        _ev, _summary = run(self.cfg(side="s2", action="edit", title_edits=((game.key, TitleEdit(publisher="Acme", display_version="3")),)))
        text = (self.src / "Game.nup").read_text()
        self.assertIn("--publisher Acme", text)
        self.assertIn("--display-version 3", text)
        self.assertEqual((self.src / "Other.nup").read_text(), "x")

    def test_artwork_folder_is_searched_in_game_mode_too(self):
        (self.src / "My Game" / "exefs").mkdir(parents=True)
        art = self.tmp / "art"
        art.mkdir()
        (art / "My Game.png").write_bytes(b"p")
        out = self.tmp / "out"
        run(self.cfg(side="s2", action="pack", dest=out, art_dir=str(art)))
        self.assertIn("--icon " + str(art / "My Game.png"), (out / "My Game.nup").read_text())

    def test_a_dropped_file_runs_alone_even_though_its_folder_has_more(self):
        (self.src / "One.nsp").write_bytes(b"x" * 10)
        (self.src / "Two.nsp").write_bytes(b"x" * 10)
        drop = classify_switch_drop([self.src / "One.nsp"])
        _ev, summary = run(self.cfg(source=drop.source), only=frozenset(drop.only))
        self.assertEqual(summary.total, 1)
        self.assertTrue((self.src / "One.nsz").is_file())
        self.assertFalse((self.src / "Two.nsz").exists())

    def test_title_id_comes_from_the_update_when_the_base_has_no_npdm(self):
        def npdm(tid: int, acid_size: int = 0x300) -> bytes:  # META header, ACI0 block right after it (as nup reads it)
            head = bytearray(0x80)
            head[:4] = b"META"
            head[0x70:0x74] = struct.pack("<I", 0x80)
            aci0 = bytearray(0x40)
            aci0[:4] = b"ACI0"
            aci0[0x10:0x18] = struct.pack("<Q", tid)
            return bytes(head) + bytes(aci0) + bytes(acid_size)
        dump = self.src / "Game"
        (dump / "base" / "exefs").mkdir(parents=True)  # a base with no main.npdm
        for ver, tid in (("65536", 0x0100AAAA00000000), ("131072", 0x0100633007D48000)):
            (dump / "updates" / ver / "exefs").mkdir(parents=True)
            (dump / "updates" / ver / "exefs" / "main.npdm").write_bytes(npdm(tid))
        self.assertEqual(dump_title_id(dump), ("0100633007D48000", "update 131072"))  # the newest update, by number
        self.assertEqual(npdm_program_id(dump / "updates" / "65536" / "exefs" / "main.npdm"), "0100AAAA00000000")
        out = self.tmp / "out"
        run(self.cfg(side="s2", action="pack", dest=out))
        self.assertIn("--title-id 0100633007D48000", (out / "Game.nup").read_text())
        # once the base has its own NPDM nup reads it itself and Distill stays out of the way
        (dump / "base" / "exefs" / "main.npdm").write_bytes(npdm(0x0100633007D48000))
        self.assertEqual(dump_title_id(dump), ("0100633007D48000", "base"))
        out2 = self.tmp / "out2"
        run(self.cfg(side="s2", action="pack", dest=out2))
        self.assertNotIn("--title-id", (out2 / "Game.nup").read_text())

    def test_renaming_a_dump_renames_its_file_and_a_title_id_can_be_forced(self):
        (self.src / "Old Name" / "exefs").mkdir(parents=True)
        dump = self.src / "Old Name"
        out = self.tmp / "out"
        run(self.cfg(side="s2", action="pack", dest=out,
                     title_edits=((str(dump), TitleEdit(name="New: Name", title_id="0100633007D48000")),)))
        text = (out / "New Name.nup").read_text() if (out / "New Name.nup").exists() else next(out.glob("*.nup")).read_text()
        self.assertIn("--name New: Name", text)
        self.assertIn("--title-id 0100633007D48000", text)
        self.assertFalse((out / "Old Name.nup").exists())

    def test_stop_ends_a_run_cleanly(self):
        (self.src / "A.nsp").write_bytes(b"x")
        events: list[tuple] = []
        pause = threading.Event()
        pause.set()
        runner = SwitchRunner(self.cfg(), events.append, pause)
        runner.shutdown()
        runner.run()
        self.assertEqual(events[-1][0], "done")


FAKE_HAC = '''
import struct
a = sys.argv[1:]
f = pathlib.Path(a[-1])
def opt(name):
    return pathlib.Path(a[a.index(name) + 1]) if name in a else None
if "--version" in a:
    print("hactoolnet 0.19"); sys.exit(0)
if "pfs0" in a:
    d = opt("--outdir"); d.mkdir(parents=True, exist_ok=True)
    for n in ("prog", "meta", "ctl"):
        (d / (n + ".nca")).write_bytes(b"n" * (300 if n == "prog" else 10))
    sys.exit(0)
if f.suffix == ".nca":
    kind = {"prog": "Program", "meta": "Meta", "ctl": "Control"}[f.stem]
    if "--section0dir" in a:
        d = opt("--section0dir"); d.mkdir(parents=True, exist_ok=True)
        c = bytearray(0x40); c[0:12] = struct.pack("<QI", 0x0100633007D48000, 0); c[0xC] = 0x80
        (d / "Application.cnmt").write_bytes(bytes(c)); sys.exit(0)
    if "--romfsdir" in a and kind == "Control":
        d = opt("--romfsdir"); d.mkdir(parents=True, exist_ok=True)
        n = bytearray(0x4000); n[0:9] = b"Fake Game"; n[0x200:0x203] = b"Pub"; n[0x3060:0x3063] = b"1.0"
        (d / "control.nacp").write_bytes(bytes(n)); (d / "icon_AmericanEnglish.dat").write_bytes(b"jpeg"); sys.exit(0)
    if "--exefsdir" in a:
        for o in ("--exefsdir", "--romfsdir"):
            d = opt(o); d.mkdir(parents=True, exist_ok=True); (d / "f.bin").write_bytes(b"1")
        sys.exit(0)
    print("Title ID:  0100633007D48000\\nContent Type:  " + kind); sys.exit(0)
sys.exit(2)
'''


@POSIX_ONLY
class DecryptTests(RunnerTests):
    def test_nsp_is_decrypted_and_packed_with_the_games_own_name_and_icon(self):
        hac = str(make_tool(self.tmp / "hactoolnet", FAKE_HAC))
        (self.src / f"Whatever [{BASE}].nsp").write_bytes(b"nsp" * 10)
        out = self.tmp / "out"
        _ev, summary = run(self.cfg(side="s2", action="pack", dest=out, hactoolnet=hac, delete=True))
        self.assertEqual((summary.total, summary.converted, summary.failures), (1, 1, ()), summary)
        nup = out / f"Fake Game [{BASE}].nup"
        args = nup.read_text()
        self.assertIn("--name Fake Game", args)
        self.assertIn("--publisher Pub", args)
        self.assertIn("--display-version 1.0", args)
        self.assertIn("--icon", args)
        self.assertFalse((self.src / f"Whatever [{BASE}].nsp").exists())  # used up after success
        self.assertEqual([p.name for p in out.iterdir()], [nup.name])  # no scratch left behind

    def test_without_hactoolnet_the_title_fails_with_a_reason(self):
        (self.src / f"Whatever [{BASE}].nsp").write_bytes(b"x")
        _ev, summary = run(self.cfg(side="s2", action="pack", hactoolnet=""))
        self.assertEqual(len(summary.failures), 1)
        self.assertIn("hactoolnet", summary.failures[0].reason)


if __name__ == "__main__":
    unittest.main()
