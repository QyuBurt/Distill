"""RetroArch core management: the buildbot listings, where the cores live, install / update / remove, and which systems a core covers."""
from __future__ import annotations

import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import libretro_cores as lc
from distill import recomps as rc

INDEX = """2026-10-30 04:11 aabbccdd fceumm_libretro.dll.zip
2026-10-29 04:10 11223344 gambatte_libretro.dll.zip
2026-10-28 04:09 deadbeef mgba_libretro.dll.zip
2026-10-28 04:09 cafebabe notacore.txt
"""
INFO = {"fceumm": 'display_name = "Nintendo - NES / Famicom (FCEUmm)"\nsystemname = "Nintendo Entertainment System"\n'
                  'supported_extensions = "fds|nes|unf|unif"\ncategories = "Emulator"\nlicense = "GPLv2"\nfirmware_count = 1\n'
                  'firmware0_desc = "disksys.rom"\nfirmware0_path = "disksys.rom"\nfirmware0_opt = "true"\n',
        "gambatte": 'display_name = "Nintendo - Game Boy / Color (Gambatte)"\nsystemname = "Game Boy / Game Boy Color"\n'
                    'supported_extensions = "gb|gbc|dmg"\ncategories = "Emulator"\n',
        "mgba": 'display_name = "Nintendo - Game Boy Advance (mGBA)"\nsystemname = "Game Boy Advance"\nfirmware_count = 1\n'
                'firmware0_desc = "gba_bios.bin"\nfirmware0_path = "gba_bios.bin"\nfirmware0_opt = "false"\ncategories = "Emulator"\n'}


def core_zip(file: str, payload: bytes = b"MZ-core") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(file, payload)
    return buf.getvalue()


def info_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in INFO.items():
            z.writestr(f"{name}_libretro.info", text)
    return buf.getvalue()


def catalog(tmp: Path | None = None) -> lc.Catalog:
    index = lc.parse_index(INDEX, ".dll")
    cores = [lc._from_info(f[: -len("_libretro.dll")], f, lc.parse_info(INFO.get(f[: -len("_libretro.dll")], "")), d, c) for f, (d, c) in index.items()]
    return lc.Catalog(tuple(cores), "https://buildbot.libretro.com/nightly/windows/x86_64/latest/", ".dll", "windows/x86_64")


class Listings(unittest.TestCase):
    def test_extended_index(self):
        found = lc.parse_index(INDEX, ".dll")
        self.assertEqual(found["fceumm_libretro.dll"], ("2026-10-30", "aabbccdd"))
        self.assertEqual(len(found), 3)  # the stray file is ignored

    def test_plain_index_and_html_listing(self):
        self.assertEqual(lc.parse_index("fceumm_libretro.so.zip\nmgba_libretro.so.zip\n", ".so"),
                         {"fceumm_libretro.so": ("", ""), "mgba_libretro.so": ("", "")})
        html = ('<a href="./snes9x_libretro.dylib.zip">x</a> <a href="../">up</a> <a href="RetroArch.dmg">d</a>'
                '<a href="/nightly/apple/osx/arm64/latest/mgba_libretro.dylib.zip">m</a>')  # relative and absolute links both
        self.assertEqual(list(lc.parse_index(html, ".dylib")), ["snes9x_libretro.dylib", "mgba_libretro.dylib"])

    def test_wrong_platform_suffix_is_ignored(self):
        self.assertEqual(lc.parse_index(INDEX, ".so"), {})

    def test_info_parsing(self):
        core = lc._from_info("fceumm", "fceumm_libretro.dll", lc.parse_info(INFO["fceumm"]), "2026-10-30", "aabbccdd")
        self.assertEqual(core.display, "Nintendo - NES / Famicom (FCEUmm)")
        self.assertEqual(core.extensions, ("fds", "nes", "unf", "unif"))
        self.assertEqual(core.firmware, (("disksys.rom", "disksys.rom", True),))
        self.assertFalse(core.needs_firmware)  # its only firmware is optional
        self.assertTrue(lc._from_info("mgba", "x", lc.parse_info(INFO["mgba"]), "", "").needs_firmware)

    def test_a_core_without_info_still_has_a_name_and_is_not_hidden(self):
        core = lc._from_info("some_new_core", "some_new_core_libretro.dll", {}, "", "")
        self.assertEqual(core.display, "Some New Core")
        self.assertTrue(core.is_emulator)

    def test_targets(self):
        self.assertEqual(lc.target("win32", "AMD64"), ("windows/x86_64", ".dll"))
        self.assertEqual(lc.target("linux", "x86_64"), ("linux/x86_64", ".so"))
        self.assertEqual(lc.target("darwin", "arm64"), ("apple/osx/arm64", ".dylib"))
        self.assertEqual(lc.target("darwin", "x86_64"), ("apple/osx/x86_64", ".dylib"))
        with self.assertRaises(rc.RecompError):
            lc.target("linux", "riscv64")

    def test_urls_only_from_the_buildbot(self):
        lc.check_url("https://buildbot.libretro.com/stable/1.19.1/windows/x86_64/fceumm_libretro.dll.zip")
        for bad in ("http://buildbot.libretro.com/x.zip", "https://buildbot.libretro.com.evil.example/x.zip", "https://example.com/x.zip"):
            with self.assertRaises(rc.RecompError):
                lc.check_url(bad)

    def test_catalog_reads_the_nightly_latest_folder_and_caches(self):
        calls = []

        def fake_get(url, *a, **k):
            calls.append(url)
            if url == "https://buildbot.libretro.com/nightly/windows/x86_64/latest/.index-extended":
                raise OSError("404")  # falls through to the plain index
            if url == "https://buildbot.libretro.com/nightly/windows/x86_64/latest/.index":
                return INDEX.encode()
            if url.endswith("info.zip"):
                return info_zip()
            raise OSError("unexpected " + url)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(rc, "_get", fake_get):
            store = rc.Store(Path(d) / "state.json")
            cat = lc.fetch_catalog(store, platform="win32", machine="AMD64")
            self.assertEqual(cat.base, "https://buildbot.libretro.com/nightly/windows/x86_64/latest/")
            self.assertEqual([c.name for c in cat.cores], ["gambatte", "mgba", "fceumm"])  # sorted by display name
            n = len(calls)
            again = lc.fetch_catalog(store, platform="win32", machine="AMD64")  # from the cache: no requests
            self.assertEqual(len(calls), n)
            self.assertEqual({c.name for c in again.cores}, {c.name for c in cat.cores})
            self.assertEqual(next(c for c in again.cores if c.name == "mgba").firmware[0][1], "gba_bios.bin")

    def test_a_missing_listing_names_the_address_it_asked_for(self):
        def fake_get(url, *a, **k):
            raise lc.urllib.error.HTTPError(url, 404, "Not Found", None, None)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(rc, "_get", fake_get):
            with self.assertRaises(rc.RecompError) as ctx:
                lc.fetch_catalog(rc.Store(Path(d) / "state.json"), platform="linux", machine="x86_64")
        self.assertIn("nightly/linux/x86_64/latest/", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    def test_an_unreachable_server_falls_back_to_the_last_list(self):
        with tempfile.TemporaryDirectory() as d:
            store = rc.Store(Path(d) / "state.json")
            with mock.patch.object(rc, "_get", lambda url, *a, **k: info_zip() if url.endswith("info.zip") else INDEX.encode()):
                lc.fetch_catalog(store, platform="win32", machine="AMD64")
            store.data["cache"]["lrcores:windows/x86_64"]["t"] = 0  # long expired
            with mock.patch.object(rc, "_get", side_effect=OSError("offline")):
                cat = lc.fetch_catalog(store, platform="win32", machine="AMD64")
        self.assertEqual(len(cat.cores), 3)


class Where(unittest.TestCase):
    def test_portable_windows_layout(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "retroarch.exe").write_bytes(b"")
            loc = lc.locate(d, "retroarch.exe", "", platform="win32")
            self.assertTrue(loc.ok)
            self.assertEqual(loc.cores, Path(d) / "cores")
            self.assertEqual(loc.info, Path(d) / "info")

    def test_cfg_beside_the_program_decides(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            (Path(d) / "retroarch.cfg").write_text(f'libretro_directory = "{other}"\nlibretro_info_path = ":\\myinfo"\n')
            loc = lc.locate(d, "retroarch.exe", "folder", platform="win32")
            self.assertEqual(loc.cores, Path(other))
            self.assertEqual(loc.info, Path(d) / "myinfo")

    def test_relative_colon_paths(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "retroarch.cfg").write_text('libretro_directory = ":\\cores"\n')
            self.assertEqual(lc.locate(d, "retroarch.exe", platform="win32").cores, Path(d) / "cores")

    def test_linux_appimage_uses_the_user_config(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as home:
            loc = lc.locate(d, "RetroArch.AppImage", "", platform="linux", home=home)
            self.assertEqual(loc.cores, Path(home) / ".config" / "retroarch" / "cores")

    def test_an_existing_user_cores_folder_is_preferred(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as home:
            flat = Path(home) / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch" / "cores"
            flat.mkdir(parents=True)
            self.assertEqual(lc.locate(d, "retroarch", "folder", platform="linux", home=home).cores, flat)

    def test_macos_app_bundle(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as home:
            loc = lc.locate(d, "RetroArch.app", "", platform="darwin", home=home)
            self.assertEqual(loc.cores, Path(home) / "Library" / "Application Support" / "RetroArch" / "cores")

    def test_a_folder_that_cannot_be_written_is_refused_with_a_reason(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lc.os, "access", return_value=False):
            loc = lc.locate(d, "retroarch.exe", "folder", platform="win32")
            self.assertFalse(loc.ok)
            self.assertIn("Core Downloader", loc.reason)


class Folder(unittest.TestCase):
    def test_scan_ignores_everything_that_is_not_a_core(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("fceumm_libretro.dll", "mgba_libretro.dll", "readme.txt", "x_libretro.so", ".distill-tmp_libretro.dll"):
                (Path(d) / n).write_bytes(b"x")
            self.assertEqual(sorted(lc.scan_installed(Path(d), ".dll")), ["fceumm", "mgba"])
            self.assertEqual(lc.scan_installed(Path(d) / "missing", ".dll"), {})

    def test_status(self):
        c = lc._from_info("fceumm", "fceumm_libretro.dll", {}, "2026-10-30", "aabbccdd")
        rec = {"crc": "aabbccdd", "date": "2026-10-30"}
        self.assertEqual(lc.status_of(c, False, None), "missing")
        self.assertEqual(lc.status_of(c, True, None), "unknown")
        self.assertEqual(lc.status_of(c, True, rec), "current")
        self.assertEqual(lc.status_of(c, True, {**rec, "crc": "00000000"}), "update")
        dated = lc._from_info("x", "x_libretro.dll", {}, "2026-11-01", "")
        self.assertEqual(lc.status_of(dated, True, {"date": "2026-10-01"}), "update")
        gone = lc.Core("mine", "mine_libretro.dll", "Mine", remote=False)
        self.assertEqual(lc.status_of(gone, True, None), "local")

    def test_local_only_cores_are_listed(self):
        cat = catalog()
        out = lc.with_local_only(cat, {"fceumm": Path("a"), "homebrew": Path("b")})
        self.assertIn("homebrew", [c.name for c in out])
        self.assertFalse(next(c for c in out if c.name == "homebrew").remote)

    def test_record_round_trip_and_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            lc.save_record(Path(d), {"a": {"crc": "1"}})
            self.assertEqual(lc.load_record(Path(d)), {"a": {"crc": "1"}})
            (Path(d) / lc.RECORD_NAME).write_text("not json")
            self.assertEqual(lc.load_record(Path(d)), {})


def fake_download(self, asset, dest):
    name = asset["url"].rsplit("/", 1)[1][: -len(".zip")]
    dest.write_bytes(core_zip(name))


class Changing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.loc = lc.Location(root / "cores", root / "info")
        self.cat = catalog()
        self.by = {c.name: c for c in self.cat.cores}
        self.store = rc.Store(root / "state.json")

    def run_batch(self, install, remove=(), **kw):
        with mock.patch.object(rc.Installer, "download", fake_download), mock.patch.object(rc, "_get", side_effect=lambda *a, **k: info_zip()):
            return lc.apply_changes(self.store, self.loc, self.cat, [self.by[n] for n in install], [self.by[n] for n in remove], **kw)

    def test_install_writes_the_core_its_info_and_the_record(self):
        res = self.run_batch(["fceumm", "mgba"])
        self.assertEqual(sorted(res.installed), ["fceumm", "mgba"])
        self.assertEqual((self.loc.cores / "fceumm_libretro.dll").read_bytes(), b"MZ-core")
        self.assertTrue((self.loc.info / "fceumm_libretro.info").is_file())
        rec = lc.load_record(self.loc.cores)
        self.assertEqual(rec["fceumm"]["crc"], "aabbccdd")
        self.assertEqual(lc.status_of(self.by["fceumm"], True, rec["fceumm"]), "current")
        self.assertEqual([p.name for p in self.loc.cores.iterdir() if p.name.startswith(".distill-")], [])  # no temp folder left behind

    def test_remove_deletes_only_that_core_and_its_record(self):
        self.run_batch(["fceumm", "mgba"])
        res = self.run_batch([], ["fceumm"])
        self.assertEqual(res.removed, ["fceumm"])
        self.assertFalse((self.loc.cores / "fceumm_libretro.dll").exists())
        self.assertTrue((self.loc.cores / "mgba_libretro.dll").exists())
        self.assertNotIn("fceumm", lc.load_record(self.loc.cores))

    def test_one_failure_does_not_stop_the_rest(self):
        def flaky(self_, asset, dest):
            if "mgba" in asset["url"]:
                raise rc.RecompError("Download failed: boom")
            fake_download(self_, asset, dest)
        with mock.patch.object(rc.Installer, "download", flaky), mock.patch.object(rc, "_get", side_effect=lambda *a, **k: info_zip()):
            res = lc.apply_changes(self.store, self.loc, self.cat, [self.by["mgba"], self.by["fceumm"]], [])
        self.assertEqual(res.installed, ["fceumm"])
        self.assertIn("Download failed", res.failed[self.by["mgba"].display])
        self.assertIn("1 failed", res.summary())

    def test_a_zip_without_the_core_is_refused(self):
        def wrong(self_, asset, dest):
            dest.write_bytes(core_zip("something_else_libretro.dll"))
        with mock.patch.object(rc.Installer, "download", wrong), mock.patch.object(rc, "_get", side_effect=lambda *a, **k: info_zip()):
            res = lc.apply_changes(self.store, self.loc, self.cat, [self.by["fceumm"]], [])
        self.assertEqual(res.installed, [])
        self.assertFalse((self.loc.cores / "fceumm_libretro.dll").exists())

    def test_a_zip_cannot_write_outside_the_folder(self):
        def sneaky(self_, asset, dest):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("../escape.txt", "x")
                z.writestr("fceumm_libretro.dll", "ok")
            dest.write_bytes(buf.getvalue())
        with mock.patch.object(rc.Installer, "download", sneaky), mock.patch.object(rc, "_get", side_effect=lambda *a, **k: info_zip()):
            lc.apply_changes(self.store, self.loc, self.cat, [self.by["fceumm"]], [])
        self.assertFalse((self.loc.cores.parent / "escape.txt").exists())
        self.assertEqual((self.loc.cores / "fceumm_libretro.dll").read_bytes(), b"ok")  # only the named file is ever written

    def test_a_core_in_use_says_so(self):
        with mock.patch.object(lc.os, "replace", side_effect=PermissionError):
            res = self.run_batch(["fceumm"])
        self.assertIn("Close RetroArch", res.failed[self.by["fceumm"].display])

    def test_cancel_stops_before_the_next_core(self):
        cancel = threading.Event()
        cancel.set()
        res = self.run_batch(["fceumm", "mgba"], cancel=cancel)
        self.assertTrue(res.cancelled)
        self.assertEqual(res.installed, [])

    def test_a_location_that_is_refused_installs_nothing(self):
        self.loc = lc.Location(None, None, None, "nope")
        res = lc.apply_changes(self.store, self.loc, self.cat, [self.by["fceumm"]], [])
        self.assertTrue(res.failed)


class Systems(unittest.TestCase):
    def test_library_names_against_libretro_names(self):
        yes = [("Nintendo Entertainment System", "NES"), ("Super Nintendo Entertainment System", "SNES"), ("Game Boy / Game Boy Color", "Game Boy Color"),
               ("Game Boy Advance", "Game Boy Advance"), ("Sega - Mega Drive - Genesis", "Mega Drive"), ("Sony - PlayStation", "PlayStation"),
               ("Sony - PlayStation Portable", "PSP"), ("NEC - PC Engine - TurboGrafx 16", "PC Engine"), ("Nintendo - Wii U", "Wii U"),
               ("Sony - PlayStation 2", "PlayStation 2")]
        no = [("Super Nintendo Entertainment System", "NES"), ("Game Boy Advance", "Game Boy"), ("Sony - PlayStation 2", "PlayStation"),
              ("Sony - PlayStation Portable", "PlayStation"), ("Nintendo - Wii U", "Wii"), ("", "NES"), ("Nintendo - Nintendo DS", "Nintendo 3DS")]
        for core_system, lib in yes:
            self.assertTrue(lc.system_matches(core_system, lib), (core_system, lib))
        for core_system, lib in no:
            self.assertFalse(lc.system_matches(core_system, lib), (core_system, lib))

    def test_an_unknown_system_name_matches_whole_words_only(self):
        self.assertTrue(lc.system_matches("Sega - SG-1000", "SG-1000"))
        self.assertFalse(lc.system_matches("Atari 2600", "Atari 26"))



def named(*names: str) -> list[lc.Core]:
    return [lc.Core(n, f"{n}_libretro.dll", n) for n in names]


class Picks(unittest.TestCase):
    def test_the_first_core_the_server_has_is_the_pick(self):
        recs = lc.recommendations(named("fceumm", "nestopia", "snes9x", "bsnes"))
        self.assertEqual([s for s, _w in recs["nestopia"]], ["NES / Famicom"])  # mesen isn't there, so the next best
        self.assertNotIn("fceumm", recs)
        self.assertEqual([s for s, _w in recs["snes9x"]], ["SNES / Super Famicom"])
        self.assertNotIn("bsnes", recs)

    def test_one_core_can_be_the_pick_for_several_systems(self):
        recs = lc.recommendations(named("genesis_plus_gx", "picodrive"))
        self.assertEqual({s for s, _w in recs["genesis_plus_gx"]}, {"Mega Drive / Genesis", "Master System / Game Gear", "Sega CD"})
        self.assertEqual([s for s, _w in recs["picodrive"]], ["Sega 32X"])

    def test_a_system_with_none_of_its_cores_gets_no_star(self):
        self.assertEqual(lc.recommendations(named("some_new_core")), {})

    def test_each_system_has_exactly_one_pick(self):
        everything = {n for _s, names, _w in lc.PICKS for n in names}
        recs = lc.recommendations(named(*everything))
        picked = [s for entries in recs.values() for s, _w in entries]
        self.assertEqual(len(picked), len(set(picked)))
        self.assertEqual(set(picked), {s for s, _n, _w in lc.PICKS})

    def test_arcade_has_one_fbneo_star(self):
        recs = lc.recommendations(named("fbneo", "mame", "fbalpha2012", "mame2003_plus"))
        self.assertEqual(list(recs), ["fbneo"])

    def test_the_table_is_well_formed(self):
        systems = [s for s, _n, _w in lc.PICKS]
        self.assertEqual(len(systems), len(set(systems)))
        for system, names, why in lc.PICKS:
            self.assertTrue(names and why, system)
            self.assertEqual(len(names), len(set(names)), system)
            for n in names:
                self.assertRegex(n, r"^[a-z0-9_]+$", system)  # a buildbot core name, not a display name

if __name__ == "__main__":
    unittest.main()
