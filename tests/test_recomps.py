"""The PC Recomps engine: catalog, release file choice, install from a local server, update check, ROM matching, covers."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import recomps as rc


def asset(name: str, size: int = 10, digest: str = "") -> dict:
    return {"name": name, "url": f"https://x/{name}", "size": size, "digest": digest}


def recomp(**kw) -> rc.Recomp:
    base = dict(id="g", name="Game Port", game="The Game", system="Nintendo 64", repo="o/game")
    base.update(kw)
    return rc.Recomp(**base)


class Isolated(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="distill_rc_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        p = mock.patch.object(rc, "ROOT", self.tmp / "recomps")
        p.start()
        self.addCleanup(p.stop)
        (self.tmp / "recomps").mkdir()
        from distill import apikeys  # saved API keys stay out of the real app folder
        p = mock.patch.object(apikeys, "FILE", self.tmp / "apikeys.json")
        p.start()
        self.addCleanup(p.stop)
        apikeys._cache = None


class CatalogTests(Isolated):
    def test_bundled_catalog_loads_and_is_sane(self):
        cat = rc.load_catalog([rc.bundled_catalog()])
        self.assertGreaterEqual(len(cat), 5)
        self.assertEqual(len({r.id for r in cat}), len(cat))
        for r in cat:
            self.assertRegex(r.repo, r"^[\w.-]+/[\w.-]+$")
            self.assertTrue(r.system and r.game)

    def test_user_file_overrides_and_adds_and_bad_files_are_skipped(self):
        a, b = self.tmp / "a.json", self.tmp / "b.json"
        a.write_text(json.dumps({"recomps": [{"id": "x", "name": "X", "system": "SNES", "repo": "o/x"}]}))
        b.write_text(json.dumps([{"id": "x", "name": "X2", "system": "SNES", "repo": "o/x"}, {"id": "y", "name": "Y", "system": "NES", "repo": "o/y"},
                                 {"name": "no id"}]))
        cat = {r.id: r for r in rc.load_catalog([a, b, self.tmp / "missing.json"])}
        self.assertEqual(set(cat), {"x", "y"})
        self.assertEqual(cat["x"].name, "X2")
        (self.tmp / "bad.json").write_text("{not json")
        self.assertEqual(rc.load_catalog([self.tmp / "bad.json"]), [])


class AssetPickTests(unittest.TestCase):
    NAMES = ["Game-Windows.zip", "Game-Linux.zip", "Game-Mac.zip", "Game.AppImage", "Game-source.zip", "Game-Windows-arm64.zip",
             "Game-Windows.zip.sha256", "Game-installer.msi", "Game-macos-universal.dmg"]

    def pick(self, plat: str, machine: str = "x64", rcp: rc.Recomp | None = None):
        got = rc.pick_asset([asset(n) for n in self.NAMES], rcp, plat, machine)
        return got["name"] if got else None

    def test_each_platform(self):
        self.assertEqual(self.pick("windows"), "Game-Windows.zip")
        self.assertEqual(self.pick("linux"), "Game-Linux.zip")
        self.assertEqual(self.pick("macos"), "Game-Mac.zip")

    def test_arm_only_on_arm(self):
        self.assertEqual(self.pick("windows", "arm64"), "Game-Windows-arm64.zip")
        self.assertEqual(self.pick("windows", "x64"), "Game-Windows.zip")

    def test_nothing_for_this_system(self):
        self.assertIsNone(rc.pick_asset([asset("Game-Windows.zip")], None, "linux", "x64"))
        self.assertIsNone(rc.pick_asset([asset("Game-source.zip"), asset("a.sha256")], None, "windows", "x64"))

    def test_catalog_regex_wins(self):
        r = recomp(asset={"windows": r"special"})
        self.assertEqual(rc.pick_asset([asset("Game-Windows.zip"), asset("special-build.zip")], r, "windows", "x64")["name"], "special-build.zip")
        self.assertIsNone(rc.pick_asset([asset("Game-Windows.zip")], r, "windows", "x64"))

    def test_stable_release_preferred_over_newer_prerelease(self):
        mk = lambda tag, pre, names: rc.Release(tag, tag, "", "", pre, "", tuple(asset(n) for n in names))  # noqa: E731
        rels = [mk("nightly", True, ["Game-Windows.zip"]), mk("v1", False, ["Game-Windows.zip"]), mk("v0", False, [])]
        rel, a = rc.choose_release(rels, recomp(), "windows", "x64")
        self.assertEqual(rel.tag, "v1")
        rel, _a = rc.choose_release([mk("nightly", True, ["Game-Windows.zip"])], recomp(), "windows", "x64")
        self.assertEqual(rel.tag, "nightly")
        self.assertIsNone(rc.choose_release([mk("v2", False, ["Game-Linux.zip"])], recomp(), "windows", "x64"))

    def test_versions(self):
        self.assertTrue(rc.is_newer("v1.2.0", "v1.10.0"))
        self.assertFalse(rc.is_newer("v2.0.3", "v2.0.3"))
        self.assertFalse(rc.is_newer("v2.0.3", "v2.0.2"))
        self.assertTrue(rc.is_newer("nightly-a", "nightly-b"))


def make_zip(files: dict[str, bytes], top: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(f"{top}{name}", data)
    return buf.getvalue()


class InstallTests(Isolated):
    def run_install(self, payload: bytes, rcp: rc.Recomp, digest: str = "", tag: str = "v1", plat: str = "windows"):
        store = rc.Store(self.tmp / "state.json")
        a = asset("Game-Windows.zip", len(payload), digest)
        rel = rc.Release(tag, tag, "", "", False, "", (a,))

        class Resp(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        seen = []
        with mock.patch.object(rc.urllib.request, "urlopen", lambda *a, **k: Resp(payload)), \
                mock.patch.object(rc, "platform_key", lambda: plat):
            inst = rc.Installer(store, lambda *a: seen.append(a)).install(rcp, rel, a)
        return store, inst, seen

    def test_install_peels_top_folder_finds_exe_and_records_it(self):
        payload = make_zip({"Game.exe": b"MZ" + b"0" * 50, "unins000.exe": b"MZ", "data/x.bin": b"1"}, top="Game-1.0/")
        store, inst, seen = self.run_install(payload, recomp())
        self.assertEqual(inst.exe, "Game.exe")
        self.assertTrue(inst.exe_path().is_file())
        self.assertEqual(store.installed("g").version, "v1")
        self.assertTrue(seen and seen[-1][0] == "Installed.")
        self.assertEqual(rc.Store(self.tmp / "state.json").installed("g").exe, "Game.exe")  # survives a restart

    def test_update_keeps_files_the_game_made(self):
        _s, inst, _ = self.run_install(make_zip({"Game.exe": b"MZ-v1"}), recomp())
        (Path(inst.folder) / "saves").mkdir()
        (Path(inst.folder) / "saves" / "slot1.sav").write_bytes(b"progress")
        _s, inst2, _ = self.run_install(make_zip({"Game.exe": b"MZ-v2"}), recomp(), tag="v2")
        self.assertEqual(inst2.folder, inst.folder)
        self.assertEqual(inst2.exe_path().read_bytes(), b"MZ-v2")
        self.assertEqual((Path(inst.folder) / "saves" / "slot1.sav").read_bytes(), b"progress")

    def test_checksum_mismatch_is_refused_and_leaves_nothing(self):
        payload = make_zip({"Game.exe": b"MZ"})
        with self.assertRaises(rc.RecompError):
            self.run_install(payload, recomp(), digest="sha256:" + "0" * 64)
        self.assertIsNone(rc.Store(self.tmp / "state.json").installed("g"))
        good = "sha256:" + hashlib.sha256(payload).hexdigest()
        self.run_install(payload, recomp(), digest=good)

    def test_zip_with_path_traversal_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../evil.exe", b"x")
        with self.assertRaises(rc.RecompError):
            self.run_install(buf.getvalue(), recomp())
        self.assertFalse((self.tmp / "recomps" / "games" / "evil.exe").exists())

    def test_nothing_to_run_is_a_clear_error(self):
        with self.assertRaises(rc.RecompError):
            self.run_install(make_zip({"readme.txt": b"hi"}), recomp())

    def test_exe_regex_and_name_preference(self):
        d = self.tmp / "pkg"
        (d / "sub").mkdir(parents=True)
        for n in ("sub/Other.exe", "GamePort.exe", "vc_redist.x64.exe"):
            (d / n).write_bytes(b"MZ")
        self.assertEqual(rc.find_executable(d, recomp(), "windows").name, "GamePort.exe")
        self.assertEqual(rc.find_executable(d, recomp(exe=r"sub/Other\.exe"), "windows").name, "Other.exe")

    def test_uninstall_removes_folder_and_record(self):
        store, inst, _ = self.run_install(make_zip({"Game.exe": b"MZ"}), recomp())
        rc.uninstall(store, recomp())
        self.assertFalse(Path(inst.folder).exists())
        self.assertIsNone(store.installed("g"))


class ReleaseFetchTests(Isolated):
    def api(self, body):
        return mock.patch.object(rc, "_get", lambda *a, **k: json.dumps(body).encode())

    def test_drafts_dropped_and_listing_cached(self):
        store = rc.Store(self.tmp / "s.json")
        body = [{"tag_name": "v2", "draft": True}, {"tag_name": "v1", "prerelease": False, "published_at": "2026-09-30T10:00:00Z",
                                                    "assets": [{"name": "a.zip", "size": 3, "browser_download_url": "https://x/a.zip",
                                                                "digest": "sha256:ab"}]}]
        with self.api(body):
            got = rc.releases("o/g", store)
        self.assertEqual([r.tag for r in got], ["v1"])
        self.assertEqual(got[0].published, "2026-09-30")
        with mock.patch.object(rc, "_get", side_effect=AssertionError("cache should answer")):
            self.assertEqual(rc.releases("o/g", store)[0].assets[0]["digest"], "sha256:ab")

    def test_rate_limit_falls_back_to_stale_cache_else_explains(self):
        store = rc.Store(self.tmp / "s.json")
        err = rc.urllib.error.HTTPError("u", 403, "limit", {}, None)
        with mock.patch.object(rc, "_get", side_effect=err):
            with self.assertRaises(rc.RecompError) as cm:
                rc.releases("o/g", store)
            self.assertIn("rate", str(cm.exception).lower())
        store.cache_put("rel:o/g", [{"tag_name": "old", "assets": []}])
        store.data["cache"]["rel:o/g"]["t"] = 0  # long expired
        with mock.patch.object(rc, "_get", side_effect=err):
            self.assertEqual(rc.releases("o/g", store)[0].tag, "old")


def lib(name: str, system: str, path: Path, kind: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, system=system, kind=kind or path.suffix.lstrip(".").upper(), size=path.stat().st_size if path.exists() else 0,
                           path=str(path))


class MatchTests(Isolated):
    def rom(self, name: str, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_name_match_is_likely_and_other_systems_ignored(self):
        r = recomp(rom=rc.RomSpec(names=(("ocarina", "time"),), exts=(".z64",)))
        p = self.rom("Legend of Zelda, The - Ocarina of Time (USA).z64", b"x" * 100)
        q = self.rom("Ocarina of Time.gba", b"x")
        out = rc.match_library([r], [lib(p.name, "Nintendo 64", p), lib(q.name, "Game Boy Advance", q)])
        self.assertEqual(out["g"].status, "likely")
        self.assertEqual(out["g"].path, str(p))
        self.assertEqual(rc.match_library([r], [lib(q.name, "Game Boy Advance", q)]), {})

    def test_compressed_dump_is_likely_with_a_note(self):
        r = recomp(system="GameCube", rom=rc.RomSpec(names=(("twilight", "princess"),), exts=(".iso",)))
        p = self.rom("Twilight Princess (USA).rvz", b"x")
        m = rc.match_library([r], [lib(p.name, "GameCube", p)])["g"]
        self.assertEqual(m.status, "likely")
        self.assertIn("RVZ", m.note)

    def test_checksum_verifies_even_when_renamed(self):
        data = bytes(range(256)) * 40
        sha = hashlib.sha1(data).hexdigest()
        r = recomp(rom=rc.RomSpec(sha1=(sha,), names=(("the", "game"),), exts=(".z64",)))
        p = self.rom("totally renamed.z64", data)
        self.assertEqual(rc.match_library([r], [lib(p.name, "Nintendo 64", p)])["g"].status, "verified")

    def test_wrong_checksum_with_right_name_is_mismatch(self):
        r = recomp(rom=rc.RomSpec(sha1=("0" * 40,), names=(("the", "game"),), exts=(".z64",)))
        p = self.rom("The Game (Europe).z64", b"abc" * 100)
        m = rc.match_library([r], [lib(p.name, "Nintendo 64", p)])["g"]
        self.assertEqual(m.status, "mismatch")

    def test_best_candidate_wins(self):
        data = b"good" * 100
        r = recomp(rom=rc.RomSpec(sha1=(hashlib.sha1(data).hexdigest(),), names=(("the", "game"),), exts=(".z64",)))
        bad, good = self.rom("The Game (E).z64", b"bad" * 100), self.rom("The Game (U).z64", data)
        out = rc.match_library([r], [lib(bad.name, "Nintendo 64", bad), lib(good.name, "Nintendo 64", good)])
        self.assertEqual((out["g"].status, out["g"].path), ("verified", str(good)))

    def test_n64_byte_orders_hash_the_same(self):
        z64 = bytes([0x80, 0x37, 0x12, 0x40]) + bytes(range(252)) * 8
        v64 = bytearray(z64)
        v64[0::2], v64[1::2] = z64[1::2][:len(v64[0::2])], z64[0::2][:len(v64[1::2])]
        n64 = bytearray(z64)
        for i in range(4):
            n64[i::4] = z64[3 - i::4]
        paths = [self.rom(f"r.{e}", bytes(d)) for e, d in (("z64", z64), ("v64", v64), ("n64", n64))]
        hashes = {rc.rom_hashes(p) for p in paths}
        self.assertEqual(len(hashes), 1)
        self.assertEqual(hashes.pop(), (hashlib.sha1(z64).hexdigest(), f"{zlib.crc32(z64) & 0xFFFFFFFF:08x}"))

    def test_stop_event_ends_the_check_early(self):
        import threading
        stop = threading.Event()
        stop.set()
        r = recomp(rom=rc.RomSpec(names=(("a",),)))
        p = self.rom("a.z64", b"x")
        self.assertEqual(rc.match_library([r], [lib("a.z64", "Nintendo 64", p)], stop), {})

    def test_store_round_trips_owned(self):
        s = rc.Store(self.tmp / "s.json")
        s.set_owned({"g": rc.RomMatch("verified", "/a/b.z64", "n")})
        self.assertEqual(rc.Store(self.tmp / "s.json").owned()["g"], rc.RomMatch("verified", "/a/b.z64", "n"))


QUIVER = {"name": "N64 Recomps", "apps": [
    {"name": "Majora's Mask", "project": "2 Ship 2 Harkinian", "repository": "HarbourMasters/2ship2harkinian", "folderName": "MajorasMask-2Ship2Harkinian",
     "tags": ["n64", "decomp", "harbour-masters"], "appIconUrl": None},
    {"name": "Exit 1", "repository": "someone/exits", "folderName": "Exit1", "releaseAssetFilter": "EXIT1", "tags": ["gamecube"]},
    {"name": "Manual", "project": "Itch drop", "folderName": "ManualExample", "tags": ["manual"]},
    {"name": "On GitLab", "repository": "ns/proj", "repositorySource": "gitlab", "folderName": "G"},
    {"name": "No Tags", "repository": "a/b", "folderName": "NoTags"},
    "garbage", {"name": "Bad repo", "repository": "not a repo", "folderName": "X"}]}


class SourceTests(Isolated):
    def test_quiver_list_parsed(self):
        got = {r.id: r for r in rc.parse_catalog(QUIVER, "N64 Recomps")}
        self.assertEqual(set(got), {"majorasmask-2ship2harkinian", "exit1", "notags"})  # manual, gitlab, junk skipped
        m = got["majorasmask-2ship2harkinian"]
        self.assertEqual((m.name, m.game, m.system, m.repo, m.source), ("2 Ship 2 Harkinian", "Majora's Mask", "Nintendo 64", "HarbourMasters/2ship2harkinian", "N64 Recomps"))
        self.assertIn("Decomp", m.tags)
        self.assertEqual(got["exit1"].system, "GameCube")
        self.assertEqual(got["exit1"].asset["windows"], "EXIT1")
        self.assertEqual(got["notags"].system, rc.UNKNOWN_SYSTEM)
        self.assertEqual(rc.pick_asset([asset("EXIT2-Windows.zip"), asset("EXIT1-Windows.zip")], got["exit1"], "windows", "x64")["name"], "EXIT1-Windows.zip")

    def test_native_format_and_bare_list_also_accepted(self):
        d = {"id": "x", "name": "X", "system": "SNES", "repo": "o/x"}
        self.assertEqual(rc.parse_catalog({"recomps": [d]}, "mine")[0].source, "mine")
        self.assertEqual(rc.parse_catalog([d, {"nope": 1}])[0].id, "x")
        self.assertEqual(rc.parse_catalog("junk"), [])

    def test_unknown_console_matches_by_name_alone(self):
        r = rc.parse_catalog({"apps": [{"name": "Banjo Kazooie", "repository": "a/b", "folderName": "BK"}]})[0]
        p = self.tmp / "Banjo-Kazooie (USA).z64"
        p.write_bytes(b"x")
        m = rc.match_library([r], [lib(p.name, "Nintendo 64", p)])
        self.assertEqual(m[r.id].status, "likely")

    def test_local_file_source_and_index_followed_one_level(self):
        a = self.tmp / "list.json"
        a.write_text(json.dumps(QUIVER))
        self.assertEqual(len(rc.fetch_source(str(a))), 3)
        self.assertEqual(rc.fetch_source(str(self.tmp / "missing.json")), [])
        index = {"version": 2, "lists": [{"id": "1", "remoteLocation": "https://x/n64.json"}, {"remoteLocation": "http://insecure/x.json"}]}
        pages = {"https://x/index.json": index, "https://x/n64.json": QUIVER}
        with mock.patch.object(rc, "_get", lambda url, *a, **k: json.dumps(pages[url]).encode()):
            got = rc.fetch_source("https://x/index.json")
        self.assertEqual(len(got), 3)  # the http:// list was ignored

    def test_remote_source_cached_and_falls_back_when_offline(self):
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            return json.dumps(QUIVER).encode()
        with mock.patch.object(rc, "_get", fake):
            rc.fetch_source("https://x/l.json")
            rc.fetch_source("https://x/l.json")
        self.assertEqual(len(calls), 1)  # second answered from disk
        with mock.patch.object(rc, "_get", side_effect=rc.urllib.error.URLError("down")):
            self.assertEqual(len(rc.fetch_source("https://x/l.json", force=True)), 3)

    def test_build_catalog_keeps_checked_entries_and_user_file_wins(self):
        a = self.tmp / "list.json"
        a.write_text(json.dumps(QUIVER))
        user = self.tmp / "user.json"
        user.write_text(json.dumps([{"id": "notags", "name": "Mine", "system": "SNES", "repo": "a/b"}]))
        with mock.patch.object(rc, "user_catalog", lambda: user):
            cat = {r.id: r for r in rc.build_catalog([str(a)], builtin=False, curation="all")}
        bundled = cat["2ship2harkinian"]
        self.assertEqual(bundled.source, "")  # the curated entry stayed; the source's duplicate repo was dropped
        self.assertNotIn("majorasmask-2ship2harkinian", cat)
        self.assertIn("exit1", cat)
        self.assertEqual(cat["notags"].name, "Mine")

    def test_discover_filters_and_marks_unverified(self):
        items = [{"full_name": "o/Cool-Recomp", "name": "Cool-Recomp", "description": "d", "topics": ["n64", "recomp"], "homepage": "https://h"},
                 {"full_name": "o/forked", "fork": True}, {"full_name": "o/old", "archived": True},
                 {"full_name": "o/Cool-Recomp", "name": "Cool-Recomp", "topics": []}]
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps({"items": items}).encode()):
            got = rc.discover()
        self.assertEqual([r.repo for r in got], ["o/Cool-Recomp"])
        self.assertEqual((got[0].system, got[0].tags, got[0].source), ("Nintendo 64", ("Unverified",), "GitHub search"))
        with mock.patch.object(rc, "_get", side_effect=rc.urllib.error.URLError("down")):
            self.assertEqual(len(rc.discover()), 1)  # cached
            self.assertEqual(len(rc.discover(force=True)), 1)  # offline: last results

    def test_store_keeps_sources(self):
        s = rc.Store(self.tmp / "s.json")
        s.set_sources([" https://a ", "https://a", "/b.json", ""], True)
        s2 = rc.Store(self.tmp / "s.json")
        self.assertEqual((s2.sources(), s2.find_new()), (["https://a", "/b.json"], True))


class AboutTests(Isolated):
    def test_repo_description_cached_and_falls_back(self):
        store = rc.Store(self.tmp / "s.json")
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps({"description": "  A native\n port.  "}).encode()):
            self.assertEqual(rc.project_about("o/p", store), "A native port.")
        with mock.patch.object(rc, "_get", side_effect=AssertionError("cached")):
            self.assertEqual(rc.project_about("o/p", store), "A native port.")
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps({"description": None}).encode()):
            self.assertEqual(rc.project_about("o/q", store, fallback="Catalog text."), "Catalog text.")
        with mock.patch.object(rc, "_get", side_effect=rc.urllib.error.URLError("down")):
            self.assertEqual(rc.project_about("o/p", store, force=True), "A native port.")  # offline: last copy


DECOMP = {"generated_at": "2026-10-31T09:57:12Z", "total": 9, "games": [
    {"id": "zelda64recomp-zelda64recomp", "title": "The Legend of Zelda: Majora's Mask (Zelda64Recomp)", "platform": "N64", "type": "static-recomp",
     "repo": "Zelda64Recomp/Zelda64Recomp", "description": "Static recompilation", "release": "v1.2.2", "releases": 7, "homepage": None,
     "note": "N64: Recompiled port with ultrawide. Needs your US ROM."},
    {"id": "harbourmasters-shipwright", "title": "The Legend of Zelda: Ocarina of Time (Ship of Harkinian)", "platform": "N64 / GameCube",
     "type": "decompilation", "repo": "HarbourMasters/Shipwright", "release": "9.3.1", "releases": 53, "homepage": "https://shipofharkinian.com"},
    {"id": "tgp482-black", "title": "Black", "platform": "og xbox", "type": "static-recomp", "repo": "TGP482/BLACK-XBERecomp", "release": "V3",
     "releases": 3, "homepage": "https://discord.gg/8dAn4ZSsap"},
    {"id": "o-prerelease", "title": "Only Prereleases", "platform": "Switch", "type": "static-recomp", "repo": "o/pre", "release": None, "releases": 61},
    {"id": "o-norelease", "title": "Nothing Yet", "platform": "Switch", "type": "static-recomp", "repo": "o/none", "release": None, "releases": 0},
    {"id": "o-browser", "title": "In a Tab", "platform": "PC", "type": "browser-port", "repo": None, "release": None, "releases": 0},
    {"id": "o-disasm", "title": "Pokemon Red", "platform": "Game Boy", "type": "disassembly", "repo": "o/pokered", "release": "v1", "releases": 1},
    {"id": "o-old", "title": "Archived One", "platform": "PC", "type": "engine-remake", "repo": "o/old", "release": "v1", "releases": 1, "archived": True},
    {"id": "o-pc", "title": "RollerCoaster Tycoon 2 (OpenRCT2)", "platform": "PC", "type": "engine-remake", "repo": "OpenRCT2/OpenRCT2", "release": "v0.5.6",
     "releases": 67}]}


class DecompGamesTests(Isolated):
    def test_parsed_and_filtered(self):
        got = {r.id: r for r in rc.parse_catalog(DECOMP, "src")}
        self.assertEqual(set(got), {"zelda64recomp-zelda64recomp", "harbourmasters-shipwright", "tgp482-black", "o-prerelease", "o-pc"})
        z = got["zelda64recomp-zelda64recomp"]
        self.assertEqual((z.name, z.game, z.system, z.repo, z.source), ("Zelda64Recomp", "The Legend of Zelda: Majora's Mask", "Nintendo 64", "Zelda64Recomp/Zelda64Recomp", "src"))
        self.assertEqual(z.tags, ("Recomp",))
        self.assertIn("ultrawide", z.description)  # the curator's note beats the repo blurb
        self.assertEqual(got["harbourmasters-shipwright"].system, "Nintendo 64")  # first of "N64 / GameCube"
        self.assertEqual(got["harbourmasters-shipwright"].website, "https://shipofharkinian.com")
        b = got["tgp482-black"]
        self.assertEqual((b.name, b.game, b.system, b.discord, b.website), ("BLACK XBERecomp", "Black", "Xbox", "https://discord.gg/8dAn4ZSsap", ""))
        self.assertEqual(got["o-pc"].system, "PC")

    def test_community_list_is_not_a_managed_source_and_curation_defaults_on(self):
        s = rc.Store(self.tmp / "s.json")
        self.assertEqual(s.sources(), [])  # the community list is built in, not a source the person manages
        self.assertEqual(s.curation(), "curated")
        s.set_curation("all")
        self.assertEqual(rc.Store(self.tmp / "s.json").curation(), "all")
        s.set_curation("nonsense")
        self.assertEqual(s.curation(), "curated")

    def test_loaded_through_the_source_and_curated_entries_win(self):
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps(DECOMP).encode()):
            cat = {r.id: r for r in rc.build_catalog([], curation="all")}  # the list is built in: no source needed
        self.assertEqual(cat["2ship2harkinian"].source, "")
        self.assertIn("ship-of-harkinian", cat)  # bundled, same repo as the list's entry
        self.assertNotIn("harbourmasters-shipwright", cat)
        self.assertEqual(cat["tgp482-black"].source, rc.DECOMP_GAMES_LABEL)

    def test_apostrophes_and_small_words_do_not_stop_a_library_match(self):
        r = rc.parse_catalog(DECOMP)[0]
        p = self.tmp / "Legend of Zelda, The - Majoras Mask (USA).z64"
        p.write_bytes(b"x")
        self.assertEqual(rc.match_library([r], [lib(p.name, "Nintendo 64", p)])[r.id].status, "likely")
        q = self.tmp / "Zelda - Ocarina of Time.z64"
        q.write_bytes(b"x")
        self.assertEqual(rc.match_library([r], [lib(q.name, "Nintendo 64", q)]), {})


NOW = 1793600000.0  # a fixed "today" (2026-11-01) so the tests don't age


def entry(i, game, system="Xbox 360", stars=100, updated="2026-10-01", score=4.0, **kw):
    return {"id": i, "title": game + (" (" + kw.pop("proj") + ")" if "proj" in kw else ""), "platform": system, "type": "static-recomp",
            "repo": f"o/{i}", "release": "v1", "releases": 1, "stars": stars, "last_commit": updated + "T00:00:00Z", "score": score}


class CurationTests(Isolated):
    def parse(self, *games):
        return rc.parse_catalog({"games": list(games)}, "list")

    def test_active_or_popular_kept_the_rest_dropped_and_stale_popular_tagged(self):
        rs = self.parse(entry("fresh", "A", stars=40, updated="2026-09-01"),
                        entry("tiny", "B", stars=5, updated="2026-10-30"),  # active but nobody's looked at it
                        entry("stale", "C", stars=200, updated="2025-01-01"),  # quiet and not popular
                        entry("legend", "D", stars=5000, updated="2023-01-01"))  # quiet but popular
        got = {r.id: r for r in rc.curate(rs, NOW)}
        self.assertEqual(set(got), {"fresh", "legend"})
        self.assertNotIn("Inactive", got["fresh"].tags)
        self.assertIn("Inactive", got["legend"].tags)

    def test_entries_without_activity_data_are_left_alone(self):
        q = rc.parse_catalog(QUIVER)
        self.assertEqual(len(rc.curate(q, NOW)), len(q))

    def test_one_project_per_game_best_first_others_named(self):
        data = {"games": [entry("a", "Army of Two", stars=136, score=4.15), entry("b", "Army of Two", stars=99, score=4.15),
                          entry("c", "Army of Two", stars=500, score=4.30), entry("x", "Army of Two", system="PS3", stars=300, score=4.0),
                          entry("m", "Burnout Revenge", stars=300)]}
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps(data).encode()), mock.patch.object(rc.time, "time", lambda: NOW):
            cat = {r.id: r for r in rc.build_catalog([])}
        self.assertEqual({"c", "x", "m"} - set(cat), set())
        self.assertNotIn("a", cat)
        self.assertNotIn("b", cat)  # same game and console as c: folded in
        self.assertEqual(sorted(n for n, _repo in cat["c"].others), ["a", "b"])
        self.assertIn("x", cat)  # the same game on another console is a different project
        self.assertEqual(cat["c"].others[0][1].split("/")[0], "o")

    def test_bundled_entries_always_kept_and_absorb_duplicates_of_their_game(self):
        same = entry("harbourmasters-shipwright", "The Legend of Zelda: Ocarina of Time", system="N64")
        same["repo"] = "HarbourMasters/Shipwright"  # the bundled project itself, as the list knows it
        data = {"games": [entry("other-oot", "The Legend of Zelda: Ocarina of Time", system="N64", stars=3000, score=4.9), same]}
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps(data).encode()), mock.patch.object(rc.time, "time", lambda: NOW):
            cat = {r.id: r for r in rc.build_catalog([])}
        self.assertIn("ship-of-harkinian", cat)
        self.assertNotIn("other-oot", cat)
        self.assertEqual([n for n, _r in cat["ship-of-harkinian"].others], ["other oot"])  # its own listing is not "another project"
        self.assertIn("2ship2harkinian", cat)
        self.assertIn("zelda64recomp", cat)  # two bundled projects for one game both stay

    def test_all_mode_lists_everything(self):
        data = {"games": [entry("a", "Army of Two", stars=2), entry("b", "Army of Two", stars=1, updated="2019-01-01")]}
        with mock.patch.object(rc, "_get", lambda *a, **k: json.dumps(data).encode()):
            cat = {r.id for r in rc.build_catalog([], curation="all")}
        self.assertTrue({"a", "b"} <= cat)


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (300).to_bytes(4, "big") + (420).to_bytes(4, "big") + b"0" * 2000  # a portrait 300x420


class LaunchBoxParseTests(unittest.TestCase):
    GUID = lambda self, c: "-".join(c * n for n in (8, 4, 4, 4, 12))  # noqa: E731

    def test_search_checks_the_title_then_console_then_fewest_extra_words(self):
        gap = "." * 900
        page = (f'<a href="/games/details/1-the-legend-of-zelda-twilight-princess">Z</a> Nintendo Wii {gap}'
                f'<a href="/games/details/2-the-legend-of-zelda-twilight-princess-multiplayer">Z</a> Nintendo GameCube {gap}'
                f'<a href="/games/details/3-the-legend-of-zelda-twilight-princess">Z</a> Nintendo GameCube {gap}'
                f'<a href="/games/details/4-shadows-of-eldoria">S</a> Nintendo GameCube')
        self.assertEqual(rc._lb_game_ids(page, "The Legend of Zelda: Twilight Princess", "Nintendo GameCube"), ["3", "2", "1"])  # 4: a different game
        self.assertEqual(rc._lb_game_ids(page, "The Legend of Zelda: Twilight Princess", "Atari 2600"), ["1", "3", "2"])

    def test_apostrophes_and_hyphens_do_not_break_title_match(self):
        page = '<a href="/games/details/9-the-legend-of-zelda-majoras-mask">x</a> Nintendo 64'
        self.assertEqual(rc._lb_game_ids(page, "The Legend of Zelda: Majora's Mask", "Nintendo 64"), ["9"])
        page = '<a href="/games/details/7-banjo-kazooie">x</a>'
        self.assertEqual(rc._lb_game_ids(page, "Banjo-Kazooie", "Nintendo 64"), ["7"])
        self.assertEqual(rc._lb_game_ids("nothing", "Banjo-Kazooie", "Nintendo 64"), [])

    def test_posters_ranked_poster_then_box_front_and_others_ignored(self):
        a, b, c = self.GUID("a"), self.GUID("b"), self.GUID("c")
        gap = "." * 900
        page = (f'<img src="https://images.launchbox-app.com/{a}.jpg"> Screenshot - Gameplay {gap}'
                f'<img src="https://images.launchbox-app.com/{b}.png"> Box - Front {gap}'
                f'<img src="https://images.launchbox-app.com/{c}.jpg"> Poster')
        self.assertEqual(rc._lb_posters(page), [f"https://images.launchbox-app.com/{c}.jpg", f"https://images.launchbox-app.com/{b}.png"])
        self.assertEqual(rc._lb_posters("<img> Clear Logo"), [])

    def test_image_size_and_portrait(self):
        import struct
        png = lambda w, h: b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"0" * 2000  # noqa: E731
        self.assertEqual(rc.image_size(png(300, 420)), (300, 420))
        self.assertTrue(rc.is_portrait(png(300, 420)))
        self.assertFalse(rc.is_portrait(png(420, 300)))
        self.assertFalse(rc.is_portrait(png(300, 300)))
        jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"0" * 14 + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 500, 350) + b"0" * 50
        self.assertEqual(rc.image_size(jpg), (350, 500))
        self.assertTrue(rc.is_portrait(b"RIFF" + b"0" * 100))  # unreadable shape: benefit of the doubt

    def test_console_names_map_to_launchbox_names(self):
        self.assertEqual(rc.LB_PLATFORMS["GameCube"], "Nintendo GameCube")


class CoverTests(Isolated):
    def test_portrait_poster_beats_landscape_box_and_landscape_is_the_fallback(self):
        import struct
        png = lambda w, h: b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"0" * 2000  # noqa: E731
        guid = lambda c: "-".join(c * n for n in (8, 4, 4, 4, 12))  # noqa: E731
        box, poster = f"https://images.launchbox-app.com/{guid('b')}.jpg", f"https://images.launchbox-app.com/{guid('c')}.jpg"
        pages = {rc._LB_SEARCH + "The%20Game": b'<a href="/games/details/5-the-game">x</a> Nintendo 64',
                 rc._LB_IMAGES + "5": f'<img src="{box}"> Box - Front {"." * 900}<img src="{poster}"> Poster'.encode(),
                 rc._LB_DETAIL + "5": b"<html></html>"}
        shapes = {box: png(420, 300), poster: png(300, 420)}

        def fake(url, *a, **k):
            if url in pages:
                return pages[url]
            if url in shapes:
                return shapes[url]
            raise rc.urllib.error.URLError("nope")
        with mock.patch.object(rc, "_get", fake):
            got = rc.fetch_cover(recomp(id="p1"))
        self.assertEqual(rc.image_size(got.read_bytes()), (300, 420))
        shapes[poster] = b"junk"  # no poster any more: the landscape box is better than nothing
        with mock.patch.object(rc, "_get", fake):
            got = rc.fetch_cover(recomp(id="p2"))
        self.assertEqual(rc.image_size(got.read_bytes()), (420, 300))

    def test_first_working_source_wins_and_is_cached(self):
        r = recomp(cover="https://x/cover.png")
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            return PNG
        with mock.patch.object(rc, "_get", fake):
            p = rc.fetch_cover(r)
        self.assertEqual(p.suffix, ".png")
        self.assertEqual(calls[-1], "https://x/cover.png")  # LaunchBox was asked first, found nothing usable
        with mock.patch.object(rc, "_get", side_effect=AssertionError):
            self.assertEqual(rc.fetch_cover(r), p)  # served from disk

    def test_launchbox_search_images_then_details_picture(self):
        r = recomp()
        pages = {
            rc._LB_SEARCH + "The%20Game": b'<a href="/games/details/99-other-thing">Other</a> Nintendo 64 ' + b"." * 900
                                          + b'<a href="/games/details/123-the-game">x</a> Nintendo 64',
            rc._LB_IMAGES + "123": b"<html>no pictures</html>",
            rc._LB_DETAIL + "123": b'<meta property="og:image" content="https://images.launchbox-app.com/abc.jpg">',
            "https://images.launchbox-app.com/abc.jpg": PNG,
        }

        def fake(url, *a, **k):
            if url in pages:
                return pages[url]
            raise rc.urllib.error.URLError("nope")
        with mock.patch.object(rc, "_get", fake):
            self.assertIsNotNone(rc.fetch_cover(r))

    def test_no_readme_or_avatar_fallback_a_missing_poster_means_placeholder(self):
        r = recomp(id="h")
        asked = []

        def fake(url, *a, **k):
            asked.append(url)
            raise rc.urllib.error.URLError("nope")
        with mock.patch.object(rc, "_get", fake):
            self.assertIsNone(rc.fetch_cover(r))
        self.assertFalse([u for u in asked if "readme" in u or u.endswith(".png?size=400")])

    def test_nothing_found_is_remembered_for_a_week(self):
        r = recomp(id="z")
        with mock.patch.object(rc, "_get", side_effect=rc.urllib.error.URLError("down")):
            self.assertIsNone(rc.fetch_cover(r))
        with mock.patch.object(rc, "_get", side_effect=AssertionError("should not search again")):
            self.assertIsNone(rc.fetch_cover(r))

    def test_non_images_are_rejected(self):
        r = recomp(id="n", cover="https://x/c")
        with mock.patch.object(rc, "_get", lambda *a, **k: b"<html>" + b"x" * 3000):
            self.assertIsNone(rc.fetch_cover(r))


class SteamGridDBTests(Isolated):
    def png(self, w, h):
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"0" * 2000

    def api(self, grids_by_id=None, calls=None):
        grids_by_id = grids_by_id if grids_by_id is not None else {7: [{"url": "https://cdn.sgdb/low.png", "score": 1}, {"url": "https://cdn.sgdb/top.png", "score": 9}]}

        def fake(url, token="", accept="", timeout=20):
            if calls is not None:
                calls.append((url, token))
            if "/search/autocomplete/" in url:
                return json.dumps({"success": True, "data": [
                    {"id": 8, "name": "The Game Deluxe Edition Remastered", "verified": True},
                    {"id": 7, "name": "The Game", "verified": True},
                    {"id": 6, "name": "Other Thing", "verified": True}]}).encode()
            if "/grids/game/" in url:
                gid = int(url.split("/grids/game/")[1].split("?")[0])
                return json.dumps({"success": True, "data": grids_by_id.get(gid, [])}).encode()
            if url == "https://cdn.sgdb/top.png":
                return self.png(600, 900)
            if url == "https://cdn.sgdb/low.png":
                return self.png(600, 900)
            if url.startswith(rc._LB_SEARCH):
                return b"<html>nothing</html>"
            raise rc.urllib.error.URLError("nope")
        return fake

    def test_title_checked_plainest_game_best_score_and_key_sent(self):
        calls = []
        with mock.patch.object(rc, "_get", self.api(calls=calls)):
            urls = rc.sgdb_posters(recomp(), "KEY")
        self.assertEqual(urls, ["https://cdn.sgdb/top.png", "https://cdn.sgdb/low.png"])
        grid_call = next(u for u, _t in calls if "/grids/game/" in u)
        self.assertIn("/grids/game/7?", grid_call)  # "The Game", not "...Deluxe Edition", not "Other Thing"
        self.assertIn("dimensions=600x900", grid_call)
        self.assertTrue(all(t == "KEY" for u, t in calls if "steamgriddb" in u))

    def test_fetch_cover_prefers_steamgriddb_and_skips_launchbox(self):
        calls = []
        with mock.patch.object(rc, "_get", self.api(calls=calls)):
            p = rc.fetch_cover(recomp(), sgdb_key="KEY")
        self.assertEqual(rc.image_size(p.read_bytes()), (600, 900))
        self.assertEqual(rc.cover_source(recomp()), "steamgriddb")
        self.assertFalse([u for u, _t in calls if "launchbox" in u])

    def test_no_key_never_calls_steamgriddb(self):
        calls = []
        with mock.patch.object(rc, "_get", self.api(calls=calls)):
            rc.fetch_cover(recomp(id="nokey"))
        self.assertFalse([u for u, _t in calls if "steamgriddb.com" in u])

    def test_adding_a_key_upgrades_an_older_poster_and_a_miss_keeps_it(self):
        r = recomp(id="up")
        with mock.patch.object(rc, "_get", self.api(grids_by_id={})):
            old = rc.art_dir() / "up.png"
            old.write_bytes(self.png(300, 420))
            rc._src_file(r).write_text("other")
            self.assertFalse(rc.cover_settled(r, "KEY"))
            self.assertTrue(rc.cover_settled(r, ""))
            self.assertEqual(rc.fetch_cover(r, sgdb_key="KEY"), old)  # SteamGridDB has nothing: keep the old one
            self.assertEqual(rc.cover_source(r), "steamgriddb-miss")
            self.assertTrue(rc.cover_settled(r, "KEY"))  # and it isn't asked again
        r2 = recomp(id="up2")
        (rc.art_dir() / "up2.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 2000)
        with mock.patch.object(rc, "_get", self.api()):
            new = rc.fetch_cover(r2, sgdb_key="KEY")
        self.assertEqual(rc.image_size(new.read_bytes()), (600, 900))
        self.assertEqual([p.suffix for p in rc.art_dir().glob("up2.*") if p.suffix != ".src"], [".png"])  # old file replaced

    def test_key_test_messages(self):
        with mock.patch.object(rc, "_get", lambda *a, **k: b'{"success": true, "data": []}'):
            self.assertEqual(rc.sgdb_test("k"), "")
        with mock.patch.object(rc, "_get", side_effect=rc.urllib.error.HTTPError("u", 401, "x", {}, None)):
            self.assertIn("rejected", rc.sgdb_test("bad"))
        with mock.patch.object(rc, "_get", side_effect=rc.urllib.error.URLError("down")):
            self.assertIn("reach", rc.sgdb_test("k"))

    def test_store_keeps_key(self):
        s = rc.Store(self.tmp / "s.json")
        s.set_sgdb_key("  abc ")
        self.assertEqual(rc.Store(self.tmp / "s.json").sgdb_key(), "abc")


if __name__ == "__main__":
    unittest.main()


class WeightedScoreTests(unittest.TestCase):
    def mk(self, i, score, stars):
        return rc.Recomp(id=i, name=i, game=i, system="PC", repo=f"o/{i}", score=score, stars=stars)

    def test_same_score_more_stars_ranks_higher(self):
        cat = [self.mk("few", 4.8, 5), self.mk("many", 4.8, 2000), self.mk("avg", 4.0, 500)]
        w = rc.weighted_scores(cat)
        self.assertGreater(w["many"], w["few"])

    def test_many_stars_stays_near_own_score_and_few_pulled_to_average(self):
        cat = [self.mk("few", 4.8, 5), self.mk("many", 4.8, 5000)] + [self.mk(f"m{i}", 3.0, 300) for i in range(8)]
        w = rc.weighted_scores(cat)
        mean = sum(r.score for r in cat) / len(cat)
        self.assertAlmostEqual(w["many"], 4.8, delta=0.05)
        self.assertLess(abs(w["few"] - mean), abs(w["few"] - 4.8))  # nearer the average than its own score

    def test_unscored_is_zero_and_empty_is_safe(self):
        self.assertEqual(rc.weighted_scores([self.mk("x", 0.0, 900)])["x"], 0.0)
        self.assertEqual(rc.weighted_scores([]), {})


class GameFromDescriptionTests(unittest.TestCase):
    def test_titles_found_after_or_before_the_usual_words(self):
        for text, want in [("Open source re-implementation of SimCity 2000 in JavaScript", "SimCity 2000"),
                           ("An open-source Sim City 2000 clone", "Sim City 2000"),
                           ("Unofficial port of Transport Tycoon Deluxe to Linux", "Transport Tycoon Deluxe"),
                           ("Remake of the classic game Prince of Persia (1989)", "Prince of Persia"),
                           ("A Rust engine for Elder Scrolls: Daggerfall", "Elder Scrolls: Daggerfall"),
                           ("reimplementation of Rollercoaster Tycoon 2 written in C++", "Rollercoaster Tycoon 2")]:
            self.assertEqual(rc.game_from_description(text), want, text)

    def test_no_title_or_not_a_game_gives_nothing(self):
        for text in ("", "Written in C++ with SDL2", "port of Linux", "Based on Nintendo's decomp of the engine", "A port for the Switch"):
            self.assertEqual(rc.game_from_description(text), "", text)

    def test_resolve_only_replaces_a_name_that_is_not_a_game(self):
        self.assertEqual(rc.resolve_game("OpenSC2K", "Open source remake of SimCity 2000"), "SimCity 2000")
        self.assertEqual(rc.resolve_game("Super Mario 64", "port of Quake"), "Super Mario 64")  # a real, multi-word title stays
        self.assertEqual(rc.resolve_game("Doom", "source port of Doom"), "Doom")  # the description says the same game
        self.assertEqual(rc.resolve_game("Some Tool", "clone of Minesweeper", always=True), "Minesweeper")

    def test_catalog_entry_gets_the_game_and_keeps_the_project_name(self):
        data = {"games": [dict(entry("a", "OpenSC2K", system="PC"), type="source-port", note="Open source SimCity 2000 clone in JavaScript")]}
        (r,) = rc.parse_decomp_games(data)
        self.assertEqual((r.game, r.name), ("SimCity 2000", "OpenSC2K"))
        self.assertEqual(r.rom.names, (tuple(sorted(rc._sig_words("SimCity 2000"))),))  # the library check now looks for the right game too

    def test_search_result_with_the_letters_run_together_still_matches(self):
        page = '<a href="/games/details/55-simcity-2000">SimCity 2000</a><a href="/games/details/9-simcity-3000">x</a>'
        self.assertEqual(rc._lb_game_ids(page, "Sim City 2000", "windows"), ["55"])
        self.assertTrue(rc._names_game("Sim City 2000", "SimCity 2000"))
        self.assertFalse(rc._names_game("Doom", "Doomsday Machine"))


class TidyAndKindTests(unittest.TestCase):
    def test_platform_on_the_end_of_a_title_is_dropped(self):
        for text, want in [("Bloodborne for Windows", "Bloodborne"), ("Bloodborne on PC", "Bloodborne"), ("Resident Evil for the PC", "Resident Evil"),
                           ("Doom - Linux", "Doom"), ("Hunt for Red October", "Hunt for Red October"), ("Windows", "Windows"),
                           ("Mario Kart for Wii", "Mario Kart for Wii")]:
            self.assertEqual(rc.tidy_game(text), want, text)

    def test_catalog_title_is_tidied_but_project_name_kept(self):
        (r,) = rc.parse_decomp_games({"games": [dict(entry("b", "Bloodborne for Windows", system="PS4", proj="shadPS4"), type="static-recomp")]})
        self.assertEqual((r.game, r.name), ("Bloodborne", "shadPS4"))

    def test_kinds_come_from_the_tags(self):
        mk = lambda *tags: rc.Recomp(id="x", name="x", game="x", system="PC", repo="o/x", tags=tags)  # noqa: E731
        self.assertEqual(rc.kinds_of(mk("Recomp")), ("Static recompilation",))
        self.assertEqual(rc.kinds_of(mk("Decomp", "Source port")), ("Decompilation", "Source port"))
        self.assertEqual(rc.kinds_of(mk("Engine remake")), ("Engine remake",))
        self.assertEqual(rc.kinds_of(mk("Unverified")), ("Other",))
        self.assertEqual(rc.kinds_in([mk("Decomp"), mk("Unverified"), mk("Recomp")]), ["Static recompilation", "Decompilation", "Other"])

    def test_every_kind_the_list_gives_is_recognised(self):
        data = {"games": [dict(entry(k, "G" + k, system="PC"), type=k) for k in ("static-recomp", "source-port", "engine-remake", "decompilation")]}
        got = rc.parse_decomp_games(data)
        self.assertEqual({k for r in got for k in rc.kinds_of(r)}, {"Static recompilation", "Source port", "Engine remake", "Decompilation"})


class ProjectNamedTitleTests(unittest.TestCase):
    TOTK = "Static recompilation of The Legend of Zelda: Tears of the Kingdom to native Windows x86-64, with a Vulkan renderer. Game files not included."

    def test_the_game_keeps_its_leading_the(self):
        self.assertEqual(rc.game_from_description(self.TOTK), "The Legend of Zelda: Tears of the Kingdom")

    def test_a_title_that_says_recomp_is_a_project_name_not_a_game(self):
        self.assertEqual(rc.resolve_game("TotK PC Recomp", self.TOTK), "The Legend of Zelda: Tears of the Kingdom")
        self.assertEqual(rc.resolve_game("Super Mario 64", self.TOTK), "Super Mario 64")  # a real title with no project words stays

    def test_catalog_entry(self):
        data = {"games": [dict(entry("t", "TotK Recomp", system="Switch"), note=self.TOTK)]}
        (r,) = rc.parse_decomp_games(data)
        self.assertEqual((r.game, r.name), ("The Legend of Zelda: Tears of the Kingdom", "TotK Recomp"))


class BuiltinKeyTests(Isolated):
    def test_key_order_is_saved_then_built_in_then_environment(self):
        store = rc.Store(self.tmp / "k.json")
        with mock.patch.dict("os.environ", {"SGDB_API_KEY": "env"}), mock.patch.object(rc, "BUILTIN_SGDB_KEY", ""):
            self.assertEqual(store.sgdb_key(), "env")
            with mock.patch.object(rc, "BUILTIN_SGDB_KEY", "built-in"):
                self.assertEqual(store.sgdb_key(), "built-in")
                store.set_sgdb_key("  saved ")
                self.assertEqual(store.sgdb_key(), "saved")
                store.set_sgdb_key("")
                self.assertEqual(store.sgdb_key(), "built-in")


class OlderSavedKeyTests(Isolated):
    def test_a_key_an_older_version_saved_in_the_store_moves_to_the_api_keys_file(self):
        from distill import apikeys
        path = self.tmp / "old.json"
        path.write_text(json.dumps({"sgdb_key": " old "}))
        with mock.patch.object(rc, "BUILTIN_SGDB_KEY", ""), mock.patch.dict("os.environ", {}, clear=True):
            store = rc.Store(path)
            self.assertEqual(store.sgdb_key(), "old")
            self.assertEqual(apikeys.get("sgdb"), "old")
            self.assertNotIn("sgdb_key", json.loads(path.read_text()))
            store.set_sgdb_key("")  # clearing it really clears it: the old copy must not come back
            self.assertEqual(rc.Store(path).sgdb_key(), "")


class SgdbRateLimitTests(Isolated):
    def http(self, code, retry_after=None):
        hdrs = {"Retry-After": retry_after} if retry_after else {}
        return rc.urllib.error.HTTPError("u", code, "x", hdrs, None)

    def test_a_429_is_waited_out_then_succeeds(self):
        answers = [self.http(429, "1"), b'{"data": [{"id": 1}]}']

        def fake(*a, **k):
            r = answers.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(rc, "_get", fake), mock.patch.object(rc.time, "sleep") as sl:
            self.assertEqual(rc._sgdb("/x", "K"), [{"id": 1}])
        sl.assert_called_once_with(1.0)

    def test_a_429_that_never_clears_is_busy_not_a_miss(self):
        with mock.patch.object(rc, "_get", side_effect=self.http(429)), mock.patch.object(rc.time, "sleep"):
            with self.assertRaises(rc.SgdbBusy):
                rc._sgdb("/x", "K")
            r = recomp(id="busy")
            with self.assertRaises(rc.SgdbBusy):
                rc.fetch_cover(r, sgdb_key="K")
        self.assertFalse((rc.art_dir() / "busy.none").exists())  # nothing remembered: it is asked about again
        self.assertNotEqual(rc.cover_source(r), "steamgriddb-miss")

    def test_other_errors_still_pass_through(self):
        with mock.patch.object(rc, "_get", side_effect=self.http(401)):
            with self.assertRaises(rc.urllib.error.HTTPError):
                rc._sgdb("/x", "K")
            self.assertIn("rejected", rc.sgdb_test("K"))


class PortedDescriptionTests(unittest.TestCase):
    def test_a_title_followed_by_ported_or_recompiled_is_the_game(self):
        for text, game in (("Halo: Combat Evolved ported to Windows", "Halo: Combat Evolved"),
                           ("Super Mario 64 recompiled for PC", "Super Mario 64"),
                           ("Ocarina of Time decompiled and ported to PC", "Ocarina of Time"),
                           ("Doom reimplemented in Rust", "Doom"),
                           ("A faithful PC port of Halo: Combat Evolved", "Halo: Combat Evolved"),
                           ("Halo: Combat Evolved (for original Xbox) ported to Windows, Linux and Android", "Halo: Combat Evolved"),
                           ("Wipeout [PS1] decompiled", "Wipeout")):
            self.assertEqual(rc.game_from_description(text), game, text)

    def test_the_project_name_is_replaced_by_that_game(self):
        self.assertEqual(rc.resolve_game("OpenCE", "Halo: Combat Evolved ported to Windows", always=True), "Halo: Combat Evolved")

    def test_things_that_are_not_games_are_not_taken_for_one(self):
        for text in ("Tools ported to Windows", "Fast native renderer written in Rust"):
            self.assertEqual(rc.game_from_description(text), "", text)
