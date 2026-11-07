"""The Emulators engine: manifest, release sources, update logic, finding existing installs, install / update / unlink, emurelation."""
from __future__ import annotations

import io
import json
import urllib.error
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import emulators as em
from distill import recomps as rc


def asset(name: str, size: int = 10, digest: str = "") -> dict:
    return {"name": name, "url": f"https://github.com/o/r/releases/download/t/{name}", "size": size, "digest": digest}


def rel(tag: str, *assets: dict, pre: bool = False, published: str = "2026-01-01") -> rc.Release:
    return rc.Release(tag, tag, "", published, pre, "", tuple(assets))


def emu(**kw) -> em.Emulator:
    base = dict(id="pcsx2", name="PCSX2", system="PlayStation 2", group="Sony", repo="PCSX2/pcsx2", systems=("PlayStation 2",))
    base.update(kw)
    return em.Emulator(**base)


class Isolated(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="distill_em_")
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        (self.tmp / "state").mkdir()
        p = mock.patch.object(rc, "ROOT", self.tmp / "state")
        p.start()
        self.addCleanup(p.stop)
        self.store = rc.Store(self.tmp / "emulators_state.json")

    def touch(self, rel_path: str, data: bytes = b"MZ") -> Path:
        p = self.tmp / "emus" / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class ManifestTests(Isolated):
    def test_bundled_manifest_loads_and_is_sane(self):
        cat = em.load_catalog([em.bundled_manifest()], "windows")
        self.assertGreaterEqual(len(cat), 20)
        self.assertEqual(len({e.id for e in cat}), len(cat))
        for e in cat:
            self.assertTrue(e.name and e.system, e.id)
            self.assertTrue(e.repo or e.adapter, e.id)
            if e.repo and e.source == "github":
                self.assertRegex(e.repo, r"^[\w.-]+/[\w.-]+$", e.id)
            self.assertIn(e.version_from, ("tag", "published_at", "asset"))

    def test_groups_are_listed_biggest_first(self):
        cat = em.load_catalog([em.bundled_manifest()], "windows")
        groups = list(dict.fromkeys(e.group for e in cat))
        size = {g: sum(e.group == g for e in cat) for g in groups}
        self.assertEqual(groups, sorted(groups, key=lambda g: (-size[g], g.casefold())))  # a tie falls back to A to Z
        self.assertEqual(groups[0], max(size, key=size.get))
        for group in groups:  # and the emulators inside a group A to Z as well
            names = [e.name.casefold() for e in cat if e.group == group]
            self.assertEqual(names, sorted(names), group)

    def test_repo_is_resolved_per_os(self):
        win = {e.id: e for e in em.load_catalog([em.bundled_manifest()], "windows")}
        lin = {e.id: e for e in em.load_catalog([em.bundled_manifest()], "linux")}
        self.assertEqual(win["rpcs3"].repo, "RPCS3/rpcs3-binaries-win")
        self.assertEqual(lin["rpcs3"].repo, "RPCS3/rpcs3-binaries-linux")

    def test_user_file_adds_and_overrides_but_cannot_drop_a_trusted_owner(self):
        user = self.tmp / "mine.json"
        user.write_text(json.dumps({"emulators": [
            {"id": "mine", "name": "Mine", "system": "X", "repo": "me/mine"},
            {"id": "kytyps5", "name": "KytyPS5", "system": "PlayStation 5", "repo": "evil/KytyPS5"},
            {"id": "pcsx2", "name": "PCSX2", "system": "PlayStation 2", "repo": "PCSX2/pcsx2-fork"},
            "junk", {"name": "no id"}]}))
        cat = {e.id: e for e in em.load_catalog([em.bundled_manifest(), user], "windows")}
        self.assertIn("mine", cat)
        self.assertEqual(cat["pcsx2"].repo, "PCSX2/pcsx2-fork")
        self.assertEqual(cat["kytyps5"].trusted_owner, "KytyPS5")  # inherited from the bundled entry
        self.assertFalse(cat["kytyps5"].available)  # and the repo no longer matches it
        self.assertIn("malware", cat["kytyps5"].notes)

    def test_an_entry_marked_inactive_is_listed_but_not_downloadable(self):
        cat = {e.id: e for e in em.load_catalog([em.bundled_manifest()], "windows")}
        self.assertFalse(em.downloadable(cat["anyps5"]))
        self.assertTrue(em.downloadable(cat["pcsx2"]))
        self.assertFalse(em.downloadable(cat["mame"], "linux"))  # Windows-only build


class ReleaseChoiceTests(Isolated):
    def test_stable_skips_prereleases_nightly_takes_the_newest(self):
        e = emu()
        rels = [rel("v2.5.1", asset("pcsx2-v2.5.1-windows-x64-Qt.7z"), pre=True), rel("v2.4.0", asset("pcsx2-v2.4.0-windows-x64-Qt.7z"))]
        self.assertEqual(em.choose(e, rels, "stable", "windows", "x64")[0].tag, "v2.4.0")
        self.assertEqual(em.choose(e, rels, "nightly", "windows", "x64")[0].tag, "v2.5.1")

    def test_a_release_with_no_file_for_this_os_is_skipped(self):
        e = emu()
        rels = [rel("v3", asset("pcsx2-v3-linux.AppImage")), rel("v2", asset("pcsx2-v2-windows-x64.7z"))]
        self.assertEqual(em.choose(e, rels, "stable", "windows", "x64")[0].tag, "v2")

    def test_latest_for_explains_why_there_is_nothing(self):
        self.assertIn("no download", em.latest_for(emu(available=False), self.store).lower())
        self.assertIn("no build", em.latest_for(emu(platforms=("linux",)), self.store).lower() if rc.platform_key() != "linux" else "no build")
        with mock.patch.object(em, "fetch_releases", side_effect=rc.RecompError("GitHub is rate-limiting this PC.")):
            self.assertIn("rate-limiting", em.latest_for(emu(), self.store))


class UpdateStateTests(Isolated):
    def inst(self, version="", stamp="", linked=""):
        return rc.Installed("pcsx2", version, str(self.tmp), "x.exe", 0.0, linked, stamp)

    def test_tag_mode_compares_versions(self):
        e, latest = emu(), (rel("v2.5.0"), asset("a.7z"))
        self.assertEqual(em.update_state(e, None, latest), "missing")
        self.assertEqual(em.update_state(e, self.inst("v2.4.0"), latest), "update")
        self.assertEqual(em.update_state(e, self.inst("v2.5.0"), latest), "current")
        self.assertEqual(em.update_state(e, self.inst("v2.6.0"), latest), "current")  # newer than the release: never offered as an update
        self.assertEqual(em.update_state(e, self.inst(""), latest), "unknown")  # a linked install whose version couldn't be told
        self.assertEqual(em.update_state(e, self.inst("v2.4.0"), "No download"), "unknown")

    def test_rolling_tag_is_told_apart_by_the_build(self):
        e = emu(id="duckstation", version_from="published_at")
        a1, a2 = asset("d.zip", digest="sha256:" + "a" * 64), asset("d.zip", digest="sha256:" + "b" * 64)
        installed = self.inst("latest (2026-01-01)", em.stamp_of(e, rel("latest"), a1))
        self.assertEqual(em.update_state(e, installed, (rel("latest"), a1)), "current")
        self.assertEqual(em.update_state(e, installed, (rel("latest"), a2)), "update")  # same tag, new build
        self.assertEqual(em.update_state(e, self.inst("v0.1"), (rel("latest"), a2)), "unknown")

    def test_asset_mode_uses_the_file_name(self):
        e = emu(id="rpcs3", version_from="asset")
        old, new = asset("rpcs3-v0.0.38-100_win64.7z"), asset("rpcs3-v0.0.38-101_win64.7z")
        installed = self.inst("x", em.stamp_of(e, rel("build-abc"), old))
        self.assertEqual(em.update_state(e, installed, (rel("build-def"), new)), "update")
        self.assertEqual(em.label_of(e, rel("build-def"), new), "rpcs3-v0.0.38-101_win64")

    def test_a_linked_install_in_a_shared_folder_is_never_updated(self):
        self.assertTrue(em.can_update(self.inst("v1", linked="folder")))
        self.assertFalse(em.can_update(self.inst("v1", linked="file")))
        self.assertFalse(em.can_update(None))


class ReleaseSourceTests(Isolated):
    def test_gitlab_releases_become_release_objects(self):
        e = emu(id="ryujinx", source="gitlab", host="git.example.org", repo="ryubing/ryujinx")
        body = json.dumps([{"tag_name": "1.3.3", "name": "Ryujinx 1.3.3", "description": "notes", "released_at": "2026-02-02T00:00:00Z",
                            "_links": {"self": "https://git.example.org/r"},
                            "assets": {"links": [{"name": "ryujinx-1.3.3-win_x64.zip", "direct_asset_url": "https://git.example.org/dl/win.zip"}]}}]).encode()
        with mock.patch.object(rc, "_get", return_value=body) as get:
            rels = em.fetch_releases(e, self.store)
        self.assertIn("ryubing%2Fryujinx", get.call_args[0][0])
        self.assertEqual((rels[0].tag, rels[0].body, rels[0].published), ("1.3.3", "notes", "2026-02-02"))
        self.assertEqual(rels[0].assets[0]["url"], "https://git.example.org/dl/win.zip")

    def test_forgejo_releases_use_the_github_shape(self):
        e = emu(id="eden", source="forgejo", host="git.example.org", repo="eden/eden")
        body = json.dumps([{"tag_name": "v0.2", "name": "v0.2", "body": "b", "published_at": "2026-03-03T00:00:00Z", "prerelease": False,
                            "draft": False, "html_url": "u", "assets": [{"name": "eden-windows.zip", "size": 5, "browser_download_url": "https://x/e.zip"}]},
                           {"tag_name": "d", "draft": True}]).encode()
        with mock.patch.object(rc, "_get", return_value=body):
            rels = em.fetch_releases(e, self.store)
        self.assertEqual([r.tag for r in rels], ["v0.2"])

    BS_YAML = """PackageIdentifier: BlueStack.BlueStacks
PackageVersion: {ver}
InstallerType: exe
ReleaseDate: 2025-11-25
Installers:
- Architecture: x64
  InstallerUrl: https://ak-build.bluestacks.com/public/app-player/windows/nxt/{ver}/abc/FullInstaller/x64/BlueStacksFullInstaller_{ver}_amd64_native.exe
  InstallerSha256: 5AD80B2B1D63095D593D81F364247CBD14B2B1ACD266218B62303388375E5DA7
ManifestType: installer
"""

    def bluestacks(self):
        return next(e for e in em.load_catalog([em.bundled_manifest()], "windows") if e.id == "bluestacks")

    def test_bluestacks_reads_its_newest_installer_from_the_winget_manifests(self):
        e = self.bluestacks()
        listing = json.dumps([{"name": n, "type": "dir"} for n in ("4.280.1.1002", "5.22.9.1001", "5.22.166.1003", "5.9.0.1062")]
                             + [{"name": "README.md", "type": "file"}, {"name": "notaversion", "type": "dir"}]).encode()
        asked = []

        def get(url, token="", accept="", timeout=20):
            asked.append(url)
            return listing if "api.github.com" in url else self.BS_YAML.format(ver=url.split("/")[-2]).encode()
        with mock.patch.object(rc, "_get", get):
            rels = em.fetch_releases(e, self.store, force=True)
        self.assertEqual([r.tag for r in rels], ["5.22.166.1003"])  # numeric order: 5.22 beats 5.9 and 4.x
        a = rels[0].assets[0]
        self.assertEqual(a["name"], "BlueStacksInstaller_5.22.166.1003_windows-x64.exe")
        self.assertEqual(a["digest"], "sha256:5ad80b2b1d63095d593d81f364247cbd14b2b1acd266218b62303388375e5da7")
        self.assertEqual(rels[0].published, "2025-11-25")
        self.assertEqual(len(asked), 2)  # the listing, then the one installer file
        picked = em.choose(e, rels, "stable", "windows", "x64")
        self.assertEqual(picked[1]["url"], a["url"])  # the usual asset picking takes it
        self.assertIsNone(em.choose(e, rels, "stable", "linux", "x64"))  # and only on Windows

    def test_bluestacks_skips_a_version_folder_with_no_installer_yet(self):
        e = self.bluestacks()
        listing = json.dumps([{"name": "5.22.200.1001", "type": "dir"}, {"name": "5.22.166.1003", "type": "dir"}]).encode()

        def get(url, token="", accept="", timeout=20):
            if "api.github.com" in url:
                return listing
            if "5.22.200.1001" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return self.BS_YAML.format(ver="5.22.166.1003").encode()
        with mock.patch.object(rc, "_get", get):
            self.assertEqual(em.fetch_releases(e, self.store, force=True)[0].tag, "5.22.166.1003")

    def test_bluestacks_downloads_only_from_its_own_servers(self):
        e = self.bluestacks()
        self.assertEqual(em.download_hosts(e), frozenset({"ak-build.bluestacks.com", "cdn3.bluestacks.com"}))
        inst = em.EmulatorInstaller(self.store)
        inst.check_asset(e, {"url": "https://ak-build.bluestacks.com/public/x.exe"})
        with self.assertRaises(rc.RecompError):
            inst.check_asset(e, {"url": "https://evil.example/BlueStacksInstaller.exe"})

    def test_a_site_that_is_down_falls_back_to_the_last_listing(self):
        e = emu(id="eden", source="forgejo", host="git.example.org", repo="eden/eden")
        good = json.dumps([{"tag_name": "v1", "assets": []}]).encode()
        with mock.patch.object(rc, "_get", return_value=good):
            em.fetch_releases(e, self.store, force=True)
        with mock.patch.object(rc, "_get", side_effect=OSError("down")):
            self.assertEqual(em.fetch_releases(e, self.store, force=True)[0].tag, "v1")
        with mock.patch.object(rc, "_get", side_effect=OSError("down")):
            with self.assertRaises(rc.RecompError):
                em.fetch_releases(emu(id="other", source="forgejo", host="h", repo="a/b"), self.store, force=True)

    def test_dolphin_update_answer_with_and_without_file_list(self):
        e = emu(id="dolphin", source="custom", adapter="dolphin_update", endpoint="https://dolphin-emu.org/update/latest/beta", repo="")
        bare = json.dumps({"shortrev": "2603", "changelog": [{"msg": "Fix a thing"}]}).encode()
        with mock.patch.object(rc, "_get", return_value=bare):
            rels = em.fetch_releases(e, self.store, force=True)
        pick = rc.pick_asset(rels[0].assets, e, "windows", "x64")
        self.assertEqual(rels[0].tag, "2603")
        self.assertTrue(pick["url"].endswith("/2603/dolphin-2603-x64.7z"))
        self.assertIn("Fix a thing", rels[0].body)
        listed = json.dumps({"shortrev": "2604", "artifacts": [{"name": "dolphin-2604-windows-x64.7z", "url": "https://dl.example/d.7z"}]}).encode()
        with mock.patch.object(rc, "_get", return_value=listed):
            rels = em.fetch_releases(e, self.store, force=True)
        self.assertEqual(rels[0].assets[0]["url"], "https://dl.example/d.7z")
        with mock.patch.object(rc, "_get", return_value=b"{}"), self.assertRaises(rc.RecompError):
            em.fetch_releases(e, self.store, force=True)

    def test_retroarch_listing_picks_the_highest_version_for_this_os(self):
        e = emu(id="retroarch", source="custom", adapter="libretro_buildbot", repo="")
        page = b'<a href="1.9.0/">1.9.0/</a> <a href="1.20.0/">1.20.0/</a> <a href="1.19.1/">x</a> <a href="nightly/">nightly</a>'
        with mock.patch.object(rc, "_get", return_value=page):
            rels = em.fetch_releases(e, self.store, force=True)
        self.assertEqual(rels[0].tag, "1.20.0")
        self.assertTrue(rc.pick_asset(rels[0].assets, e, "linux", "x64")["url"].endswith("/1.20.0/linux/x86_64/RetroArch.7z"))
        with mock.patch.object(rc, "_get", return_value=b"nothing"), self.assertRaises(rc.RecompError):
            em.fetch_releases(e, self.store, force=True)

    def test_scummvm_reads_its_downloads_page(self):
        e = emu(id="scummvm", name="ScummVM", source="custom", adapter="scummvm_site", repo="")
        base = "https://downloads.scummvm.org/frs/scummvm/2026.3.0/"
        page = (f'<a href="{base}scummvm-2026.3.0-win32-x86_64.zip">zip64</a> <a href="{base}scummvm-2026.3.0-win32-x86_64.zip.sha256">sha</a> '
                f'<a href="{base}scummvm-2026.3.0-win32-x86.zip">zip32</a> <a href="{base}scummvm-2026.3.0-win32.exe">exe</a> '
                f'<a href="{base}scummvm-2026.3.0-macosx.dmg">dmg</a> <a href="{base}scummvm-2026.3.0-macosx-intel.dmg">old</a>').encode()
        with mock.patch.object(rc, "_get", return_value=page):
            rels = em.fetch_releases(e, self.store, force=True)
        self.assertEqual(rels[0].tag, "2026.3.0")
        self.assertEqual(len(rels[0].assets), 2)
        win = rc.pick_asset(rels[0].assets, e, "windows", "x64")
        self.assertTrue(win["url"].endswith("/2026.3.0/scummvm-2026.3.0-win32-x86_64.zip"))
        self.assertTrue(rc.pick_asset(rels[0].assets, e, "macos", "arm64")["url"].endswith(".dmg"))
        self.assertEqual(em.download_hosts(e), frozenset({"downloads.scummvm.org"}))
        with mock.patch.object(rc, "_get", return_value=b"nothing"), self.assertRaises(rc.RecompError):
            em.fetch_releases(e, self.store, force=True)

    def test_scummvm_is_downloadable_from_the_catalog(self):
        by = {x.id: x for x in em.load_catalog([em.bundled_manifest()], "windows")}
        self.assertEqual(by["scummvm"].adapter, "scummvm_site")
        self.assertTrue(em.downloadable(by["scummvm"], "windows"))


class FindingInstallsTests(Isolated):
    def catalog(self):
        return em.load_catalog([em.bundled_manifest()], "windows")

    def test_scan_finds_installs_by_folder_name_and_reads_a_version_from_it(self):
        self.touch("PCSX2/pcsx2-qt.exe")
        self.touch("duckstation-v0.1.9/duckstation-qt-x64-ReleaseLTCG.exe")
        self.touch("Dolphin Emulator/Dolphin.exe")
        self.touch("Dolphin Emulator/DolphinTool.exe")
        self.touch("random/foo.exe")
        found = em.scan_folder(self.tmp / "emus", self.catalog(), "windows")
        self.assertEqual(set(found), {"pcsx2", "duckstation", "dolphin"})
        self.assertEqual(found["pcsx2"].exe, "pcsx2-qt.exe")
        self.assertEqual(found["dolphin"].exe, "Dolphin.exe")  # not DolphinTool
        self.assertEqual(found["duckstation"].version, "0.1.9")
        self.assertEqual(found["pcsx2"].linked, "folder")

    def test_scan_does_not_dive_into_game_libraries_or_match_look_alikes(self):
        self.touch("RPCS3/rpcs3.exe")
        self.touch("RPCS3/dev_hdd0/game/NPUB1/rpcs3.exe")
        self.touch("aresgalaxy/galaxy.exe")  # a folder that only starts with the same letters is not the ares emulator
        self.touch("Sony/PCSX2/pcsx2-qt.exe")  # one level down inside a grouping folder is still found
        found = em.scan_folder(self.tmp / "emus", self.catalog(), "windows")
        self.assertEqual(found["rpcs3"].exe, "rpcs3.exe")
        self.assertNotIn("ares", found)
        self.assertIn("pcsx2", found)

    def test_a_loose_exe_is_linked_as_file_and_never_updated(self):
        self.touch("xemu.exe")
        found = em.scan_folder(self.tmp / "emus", self.catalog(), "windows")
        self.assertEqual(found["xemu"].linked, "file")

    def test_sidecar_wins_over_guessing(self):
        self.touch("PCSX2-old/pcsx2-qt.exe")
        em.write_sidecar(self.tmp / "emus" / "PCSX2-old", "pcsx2", "v2.3.0", "v2.3.0")
        self.assertEqual(em.scan_folder(self.tmp / "emus", self.catalog(), "windows")["pcsx2"].version, "v2.3.0")

    def test_emurelation_alternate_names_let_a_scan_recognise_more_folders(self):
        self.touch("Mupen GUI/app.exe")
        catalog = self.catalog()
        self.assertNotIn("rmg", em.scan_folder(self.tmp / "emus", catalog, "windows"))
        relation = em.parse_relation({"RMG": {"id": "RMG", "name": "RMG", "AltNames": ["Mupen GUI", "Rosalie's Mupen GUI"]}}, {})
        found = em.scan_folder(self.tmp / "emus", em.enrich(catalog, relation), "windows")
        self.assertIn("rmg", found)

    def test_apply_scan_keeps_what_is_there_and_replaces_what_is_gone(self):
        self.touch("PCSX2/pcsx2-qt.exe")
        found = em.scan_folder(self.tmp / "emus", self.catalog(), "windows")
        self.assertEqual(em.apply_scan(self.store, found), ["pcsx2"])
        self.assertEqual(em.apply_scan(self.store, found), [])  # nothing new
        (self.tmp / "emus" / "PCSX2" / "pcsx2-qt.exe").unlink()
        self.touch("PCSX2/pcsx2-qt.exe", b"MZ2")
        inst = self.store.installed("pcsx2")
        self.assertTrue(inst.exe_path().exists())

    def test_link_by_hand_and_unlink_leaves_files_alone(self):
        exe = self.touch("anywhere/PCSX2 portable/pcsx2-qt.exe")
        e = {x.id: x for x in self.catalog()}["pcsx2"]
        f = em.link(self.store, e, exe, "windows")
        self.assertEqual(f.linked, "folder")
        self.assertEqual(self.store.installed("pcsx2").linked, "folder")
        em.unlink(self.store, e)
        self.assertIsNone(self.store.installed("pcsx2"))
        self.assertTrue(exe.exists())
        with self.assertRaises(rc.RecompError):
            em.link(self.store, e, self.tmp / "nope", "windows")
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(rc.RecompError):
            em.link(self.store, e, empty, "windows")

    def test_an_exe_picked_from_the_emulators_folder_itself_counts_as_shared(self):
        root = self.tmp / "emus"
        exe = self.touch("pcsx2-qt.exe")
        em.set_emulators_root(self.store, root)
        e = {x.id: x for x in self.catalog()}["pcsx2"]
        self.assertEqual(em.link(self.store, e, exe, "windows").linked, "file")

    def test_uninstall_deletes_only_what_distill_installed(self):
        root = self.tmp / "emus"
        em.set_emulators_root(self.store, root)
        e = {x.id: x for x in self.catalog()}["pcsx2"]
        mine = self.touch("PCSX2/pcsx2-qt.exe").parent
        self.store.set_installed(rc.Installed("pcsx2", "v1", str(mine), "pcsx2-qt.exe"))
        self.assertEqual(em.uninstall(self.store, e), "Uninstalled.")
        self.assertFalse(mine.exists())
        theirs = self.touch("PCSX2/pcsx2-qt.exe").parent
        self.store.set_installed(rc.Installed("pcsx2", "v1", str(theirs), "pcsx2-qt.exe", 0.0, "folder"))
        self.assertIn("left where they are", em.uninstall(self.store, e))
        self.assertTrue(theirs.exists())
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        self.store.set_installed(rc.Installed("pcsx2", "v1", str(outside), "x.exe"))
        em.uninstall(self.store, e)  # not inside any folder Distill manages: forgotten, not deleted
        self.assertTrue(outside.exists())


class InstallTests(Isolated):
    def zip_bytes(self, files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        return buf.getvalue()

    def install(self, e, payload: bytes, tag="v2", name="pcsx2-windows-x64.zip", digest=""):
        a = asset(name, len(payload), digest)
        r = rel(tag, a)

        class Resp(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False
        with mock.patch.object(rc.urllib.request, "urlopen", lambda *a, **k: Resp(payload)), \
                mock.patch.object(rc, "platform_key", lambda: "windows"), mock.patch.object(em.rc, "platform_key", lambda: "windows"):
            return em.EmulatorInstaller(self.store).install(e, r, a)

    def test_fresh_install_goes_into_the_emulators_folder(self):
        root = self.tmp / "emus"
        em.set_emulators_root(self.store, root)
        e = emu(exe=r"(^|/)pcsx2-qt\.exe$")
        inst = self.install(e, self.zip_bytes({"PCSX2-v2/pcsx2-qt.exe": b"MZ", "PCSX2-v2/resources/a.txt": b"a"}))
        self.assertEqual(Path(inst.folder), root / "PCSX2")
        self.assertTrue((root / "PCSX2" / "pcsx2-qt.exe").exists())
        self.assertEqual((inst.version, inst.linked), ("v2", ""))
        self.assertEqual(em.read_sidecar(root / "PCSX2", "pcsx2"), ("v2", "v2"))
        self.assertEqual(self.store.installed("pcsx2").version, "v2")

    def test_without_an_emulators_folder_it_installs_beside_distill(self):
        e = emu(exe=r"(^|/)pcsx2-qt\.exe$")
        inst = self.install(e, self.zip_bytes({"pcsx2-qt.exe": b"MZ"}))
        self.assertIn(str(self.tmp / "state" / "emulators" / "pcsx2"), inst.folder)

    def test_updating_a_linked_install_merges_in_place_and_keeps_the_players_files(self):
        exe = self.touch("PCSX2/pcsx2-qt.exe", b"old")
        self.touch("PCSX2/portable.ini", b"keep me")
        self.touch("PCSX2/memcards/Mcd001.ps2", b"save")
        e = emu(exe=r"(^|/)pcsx2-qt\.exe$")
        self.store.set_installed(rc.Installed("pcsx2", "v1", str(exe.parent), "pcsx2-qt.exe", 0.0, "folder"))
        inst = self.install(e, self.zip_bytes({"pcsx2-qt.exe": b"new"}), tag="v2")
        self.assertEqual(Path(inst.folder), exe.parent)
        self.assertEqual(exe.read_bytes(), b"new")
        self.assertEqual((exe.parent / "portable.ini").read_bytes(), b"keep me")
        self.assertEqual((exe.parent / "memcards" / "Mcd001.ps2").read_bytes(), b"save")
        self.assertEqual((inst.version, inst.linked), ("v2", "folder"))  # still linked: Distill still won't delete it

    def test_a_protected_folder_is_explained_before_downloading(self):
        exe = self.touch("ScummVM/scummvm.exe", b"old")
        self.store.set_installed(rc.Installed("pcsx2", "v1", str(exe.parent), "scummvm.exe", 0.0, "folder"))
        with mock.patch.object(rc, "writable", return_value=False), \
             mock.patch.object(rc.urllib.request, "urlopen", side_effect=AssertionError("must not download")):
            with self.assertRaises(rc.RecompError) as cm:
                em.EmulatorInstaller(self.store).install(emu(), rel("v2", asset("a.zip")), asset("a.zip"))
        self.assertIn("Move ", str(cm.exception))  # the way out: move it somewhere writable and link it again
        self.assertNotIn("administrator", str(cm.exception))
        self.assertIn(str(exe.parent), str(cm.exception))
        self.assertEqual(exe.read_bytes(), b"old")

    def test_update_blocked_says_why_only_for_a_protected_folder(self):
        exe = self.touch("ScummVM/scummvm.exe", b"old")
        inst = rc.Installed("scummvm", "v1", str(exe.parent), "scummvm.exe", 0.0, "folder")
        e = emu(id="scummvm", name="ScummVM")
        self.assertEqual(em.update_blocked(e, inst), "")  # an ordinary folder is fine
        self.assertEqual(em.update_blocked(e, None), "")  # nothing installed: nothing to block
        with mock.patch.object(rc, "writable", return_value=False):
            why = em.update_blocked(e, inst)
        self.assertIn("ScummVM", why)
        self.assertIn(str(exe.parent), why)
        self.assertIn("Move ScummVM to a folder you can write to", why)
        gone = rc.Installed("scummvm", "v1", str(self.tmp / "missing"), "scummvm.exe", 0.0, "folder")
        with mock.patch.object(rc, "writable", return_value=False):
            self.assertEqual(em.update_blocked(e, gone), "")  # a folder that isn't there is a different problem ("Files missing")

    def test_a_refused_file_is_reported_plainly_and_the_old_one_survives(self):
        src, dest = self.tmp / "stage", self.tmp / "live"
        (src).mkdir()
        (dest).mkdir()
        (src / "AUTHORS.txt").write_bytes(b"new")
        (dest / "AUTHORS.txt").write_bytes(b"old")
        with mock.patch.object(rc.os, "replace", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(rc.RecompError) as cm:
                rc._merge_tree(src, dest)
        self.assertIn("AUTHORS.txt", str(cm.exception))
        self.assertIn("Program Files", str(cm.exception))
        self.assertEqual((dest / "AUTHORS.txt").read_bytes(), b"old")  # never deleted first
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["AUTHORS.txt"])  # and no half-copied leftovers

    def test_merging_replaces_files_and_leaves_no_temporary_copies(self):
        src, dest = self.tmp / "stage", self.tmp / "live"
        (src / "data").mkdir(parents=True)
        (dest / "data").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"new")
        (src / "data" / "b.txt").write_bytes(b"new")
        (dest / "a.txt").write_bytes(b"old")
        (dest / "save.dat").write_bytes(b"mine")
        rc._merge_tree(src, dest)
        self.assertEqual((dest / "a.txt").read_bytes(), b"new")
        self.assertEqual((dest / "data" / "b.txt").read_bytes(), b"new")
        self.assertEqual((dest / "save.dat").read_bytes(), b"mine")
        self.assertEqual([p.name for p in dest.rglob("*") if p.name.endswith(".distill-new")], [])

    def test_writable_is_true_for_a_normal_folder_and_false_for_a_missing_one(self):
        self.assertTrue(rc.writable(self.tmp))
        self.assertFalse(rc.writable(self.tmp / "nope"))

    def test_a_shared_folder_is_refused_before_downloading(self):
        exe = self.touch("xemu.exe")
        self.store.set_installed(rc.Installed("pcsx2", "", str(exe.parent), "xemu.exe", 0.0, "file"))
        with mock.patch.object(rc.urllib.request, "urlopen", side_effect=AssertionError("must not download")):
            with self.assertRaises(rc.RecompError) as cm:
                em.EmulatorInstaller(self.store).install(emu(), rel("v2", asset("a.zip")), asset("a.zip"))
        self.assertIn("shared", str(cm.exception))

    def test_a_download_from_the_wrong_owner_is_refused(self):
        e = emu(id="kytyps5", name="KytyPS5", repo="KytyPS5/KytyPS5", trusted_owner="KytyPS5")
        bad = {"name": "k.zip", "url": "https://github.com/evil/KytyPS5/releases/download/v1/k.zip", "size": 1, "digest": ""}
        with mock.patch.object(rc.urllib.request, "urlopen", side_effect=AssertionError("must not download")):
            with self.assertRaises(rc.RecompError) as cm:
                em.EmulatorInstaller(self.store).install(e, rel("v1", bad), bad)
        self.assertIn("malware", str(cm.exception))
        good = {**bad, "url": "https://github.com/KytyPS5/KytyPS5/releases/download/v1/k.zip"}
        em.EmulatorInstaller(self.store).check_asset(e, good)  # no error

    def test_a_checksum_mismatch_installs_nothing(self):
        e = emu(exe=r"(^|/)pcsx2-qt\.exe$")
        with self.assertRaises(rc.RecompError):
            self.install(e, self.zip_bytes({"pcsx2-qt.exe": b"MZ"}), digest="sha256:" + "0" * 64)
        self.assertIsNone(self.store.installed("pcsx2"))

    def test_an_appimage_update_removes_the_old_versioned_file(self):
        folder = self.tmp / "emus" / "Cemu"
        old = folder / "Cemu-2.0.AppImage"
        folder.mkdir(parents=True)
        old.write_bytes(b"\x7fELF-old")
        os.utime(old, (1_000_000, 1_000_000))
        e = emu(id="cemu", name="Cemu", exe=r"\.AppImage$")
        self.store.set_installed(rc.Installed("cemu", "v2.0", str(folder), old.name, 0.0, "folder"))
        with mock.patch.object(rc, "platform_key", lambda: "linux"), mock.patch.object(em.rc, "platform_key", lambda: "linux"):
            a = asset("Cemu-2.1-x86_64.AppImage", 8)
            payload = b"\x7fELF-new"

            class Resp(io.BytesIO):
                headers = {"Content-Length": "8"}

                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    return False
            with mock.patch.object(rc.urllib.request, "urlopen", lambda *a, **k: Resp(payload)):
                inst = em.EmulatorInstaller(self.store).install(e, rel("v2.1", a), a)
        self.assertEqual(inst.exe, "Cemu-2.1-x86_64.AppImage")
        self.assertFalse(old.exists())


class RelationTests(Isolated):
    PLATFORMS = {  # the shape of emurelation's platforms/<source>.json
        "Sony PlayStation 2": {"id": "Sony PlayStation 2", "name": "Sony PlayStation 2", "emulators": ["PCSX2", "Play!"]},
        "Nintendo Wii U (Wii U)": {"id": "Nintendo Wii U (Wii U)", "name": "Nintendo Wii U (Wii U)", "emulators": ["Cemu", "Decaf"]},
        "GameCube": {"id": "GameCube", "name": "GameCube", "emulators": [{"id": "dolphin", "name": "Dolphin"}]},
    }
    EMULATORS = {"dolphin": {"id": "dolphin", "name": "Dolphin", "AltNames": ["Dolphin Emulator", "dolphin-emu"], "platforms": ["Wii"]}}

    def test_parse_links_alternate_names_and_platforms(self):
        rel_ = em.parse_relation(self.EMULATORS, self.PLATFORMS)
        dolphin = emu(id="dolphin", name="Dolphin", systems=())
        self.assertIn("Dolphin Emulator", rel_.aliases_for(dolphin))
        self.assertEqual(set(rel_.platforms_for(dolphin)), {"Wii", "GameCube"})
        self.assertEqual(set(rel_.systems_for(dolphin)), {"Wii", "GameCube"})

    def test_enrich_adds_but_never_removes(self):
        rel_ = em.parse_relation({}, self.PLATFORMS)
        out = em.enrich([emu(systems=("PlayStation",), aliases=("pcsx2-qt",))], rel_)[0]
        self.assertEqual(out.systems, ("PlayStation", "PlayStation 2"))
        self.assertIn("pcsx2-qt", out.aliases)
        self.assertEqual(em.enrich([emu()], None)[0], emu())

    def test_parse_survives_odd_shapes(self):
        for odd in (None, [], "text", {"emulators": None}, {"x": 1}, [1, "a", {"name": ""}]):
            em.parse_relation(odd, odd)

    def test_load_fetches_caches_and_falls_back_to_the_cached_copy(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return json.dumps(self.PLATFORMS if "platforms" in url else self.EMULATORS).encode()
        first = em.load_relation(fetch=fetch)
        self.assertIsNotNone(first)
        self.assertEqual(len(calls), 2)
        em.load_relation(fetch=fetch)
        self.assertEqual(len(calls), 2)  # fresh on disk: not asked again

        def down(url):
            raise OSError("offline")
        self.assertIsNotNone(em.load_relation(force=True, fetch=down))  # the cached copy is still used

    def test_unreachable_and_never_cached_is_simply_none(self):
        def down(url):
            raise OSError("offline")
        self.assertIsNone(em.load_relation(fetch=down))

    def test_a_download_that_does_not_parse_does_not_replace_the_cache(self):
        em.load_relation(fetch=lambda url: json.dumps({"a": {"name": "A", "emulators": ["PCSX2"]}}).encode())
        em.load_relation(force=True, fetch=lambda url: b"<html>rate limited</html>")
        text = (em._relation_dir() / "platforms_local.json").read_text()
        self.assertIn("PCSX2", text)


class LibraryTests(Isolated):
    def test_counts_and_missing_emulators(self):
        from types import SimpleNamespace as NS
        entries = [NS(system="PlayStation 2")] * 5 + [NS(system="GameCube")] * 2 + [NS(system="Unknown system")] * 9
        counts = em.library_counts(entries)
        self.assertEqual(counts, {"PlayStation 2": 5, "GameCube": 2})
        cat = em.load_catalog([em.bundled_manifest()], "windows")
        with mock.patch.object(rc, "platform_key", lambda: "windows"):
            missing = {e.id: n for e, n in em.missing_for_library(cat, self.store, counts)}
        self.assertEqual(missing["pcsx2"], 5)
        self.assertEqual(missing["dolphin"], 2)
        self.assertNotIn("duckstation", missing)
        self.store.set_installed(rc.Installed("pcsx2", "v1", str(self.tmp), "x.exe"))
        with mock.patch.object(rc, "platform_key", lambda: "windows"):
            self.assertNotIn("pcsx2", {e.id for e, _n in em.missing_for_library(cat, self.store, counts)})

    def test_one_emulator_is_offered_per_system(self):
        cat = [emu(id="a", name="A", systems=("Switch",)), emu(id="b", name="B", systems=("Switch",))]
        got = em.missing_for_library(cat, self.store, {"Switch": 3})
        self.assertEqual([e.id for e, _ in got], ["a"])

    def test_status_text(self):
        e = emu()
        self.assertEqual(em.status_text(e, None, None)[0], "missing")
        self.assertEqual(em.status_text(emu(available=False), None, None)[0], "unavailable")
        gone = rc.Installed("pcsx2", "v1", str(self.tmp / "nowhere"), "x.exe")
        self.assertEqual(em.status_text(e, gone, None)[0], "gone")
        here = self.touch("PCSX2/pcsx2-qt.exe")
        inst = rc.Installed("pcsx2", "v1", str(here.parent), "pcsx2-qt.exe")
        self.assertEqual(em.status_text(e, inst, (rel("v2"), asset("a.7z")))[0], "update")
        self.assertEqual(em.status_text(e, inst, (rel("v1"), asset("a.7z")))[0], "current")
        self.assertIn("linked", em.status_text(e, rc.Installed("pcsx2", "v1", str(here.parent), "pcsx2-qt.exe", 0.0, "folder"), (rel("v1"), asset("a")))[1])


class SettingsTests(Isolated):
    def test_root_and_channel_persist(self):
        e = emu(channels=("stable", "nightly"))
        self.assertIsNone(em.emulators_root(self.store))
        em.set_emulators_root(self.store, self.tmp / "x")
        em.set_channel(self.store, e, "nightly")
        again = rc.Store(self.tmp / "emulators_state.json")
        self.assertEqual(em.emulators_root(again), self.tmp / "x")
        self.assertEqual(em.channel_of(again, e), "nightly")
        self.assertEqual(em.channel_of(again, emu(channels=("stable",))), "stable")  # a channel it doesn't have is ignored
        em.set_emulators_root(again, None)
        self.assertIsNone(em.emulators_root(again))



class ForkTests(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.parent = emu(id="kytyps5", name="KytyPS5", system="PlayStation 5", repo="KytyPS5/KytyPS5", trusted_owner="KytyPS5",
                          systems=("PlayStation 5",))
        self.other = emu()
        self.path = self.tmp / "forks.json"

    def test_parse_repo_takes_urls_and_short_form(self):
        for text in ("Jetsku/KytyPS5", "https://github.com/Jetsku/KytyPS5/releases", "github.com/Jetsku/KytyPS5.git", " www.github.com/Jetsku/KytyPS5?tab=x "):
            self.assertEqual(em.parse_repo(text), "Jetsku/KytyPS5")
        for bad in ("", "Jetsku", "https://example.com/a/b/c d"):
            with self.assertRaises(rc.RecompError):
                em.parse_repo(bad)

    def test_a_fork_inherits_the_parent_and_pins_its_own_owner(self):
        f = em.make_fork(self.parent, "Jetsku/KytyPS5", description="Astro Bot work", accepted=True)
        self.assertEqual((f.parent, f.system, f.group, f.systems, f.trusted_owner), ("kytyps5", "PlayStation 5", "Sony", ("PlayStation 5",), "Jetsku"))
        self.assertEqual(f.name, "KytyPS5 (Jetsku)")
        self.assertEqual(em.folder_name(f), "KytyPS5 (Jetsku)")
        self.assertNotEqual(em.folder_name(f), em.folder_name(self.parent))
        self.assertTrue(f.available and f.risk_accepted and not f.author_known)
        self.assertEqual(f.aliases, ())

    def test_unknown_author_needs_acceptance_and_known_author_does_not(self):
        with self.assertRaises(rc.RecompError):
            em.add_fork(self.parent, "Jetsku/KytyPS5", catalog=[self.parent], path=self.path)
        self.assertFalse(self.path.exists())
        em.add_fork(self.parent, "Jetsku/KytyPS5", "", "x", accepted=True, catalog=[self.parent], path=self.path)
        em.add_fork(self.parent, "KytyPS5/other-build", catalog=[self.parent], path=self.path)  # the parent's own owner
        forks = em.load_forks([self.parent], self.path)
        by = {f.repo: f for f in forks}
        self.assertTrue(by["Jetsku/KytyPS5"].risk_accepted and by["Jetsku/KytyPS5"].available)
        self.assertTrue(by["KytyPS5/other-build"].author_known)
        self.assertFalse(em.auto_update_ok(by["Jetsku/KytyPS5"]))
        self.assertTrue(em.auto_update_ok(by["KytyPS5/other-build"]))
        self.assertTrue(em.auto_update_ok(self.parent))

    def test_a_hand_edited_unconfirmed_fork_cannot_download(self):
        self.path.write_text(json.dumps({"forks": [{"parent": "kytyps5", "repo": "Evil/KytyPS5"}]}), encoding="utf-8")
        (fork,) = em.load_forks([self.parent], self.path)
        self.assertFalse(fork.available)
        self.assertEqual(em.status_text(fork, None, None)[0], "unavailable")
        inst = em.EmulatorInstaller(self.store)
        with self.assertRaises(rc.RecompError):
            inst.check_asset(fork, {"url": "https://github.com/Evil/KytyPS5/releases/download/t/a.zip"})

    def test_installer_still_pins_the_forks_own_owner(self):
        f = em.make_fork(self.parent, "Jetsku/KytyPS5", accepted=True)
        inst = em.EmulatorInstaller(self.store)
        inst.check_asset(f, {"url": "https://github.com/Jetsku/KytyPS5/releases/download/t/a.zip"})
        with self.assertRaises(rc.RecompError):
            inst.check_asset(f, {"url": "https://github.com/KytyPS5/KytyPS5/releases/download/t/a.zip"})

    def test_load_skips_orphans_duplicates_and_junk_and_remove_works(self):
        self.path.write_text(json.dumps({"forks": [
            {"parent": "nope", "repo": "a/b", "risk_accepted": True}, "junk", {"parent": "kytyps5"},
            {"parent": "kytyps5", "repo": "a/b", "risk_accepted": True}, {"parent": "kytyps5", "repo": "A/B", "risk_accepted": True}]}), encoding="utf-8")
        forks = em.load_forks([self.parent], self.path)
        self.assertEqual(len(forks), 1)
        em.remove_fork(forks[0], self.path)
        self.assertEqual(em.load_forks([self.parent], self.path), [])
        self.assertEqual(em.load_forks([self.parent], self.tmp / "missing.json"), [])

    def test_a_fork_folder_is_not_taken_for_the_parent_and_the_other_way_round(self):
        f = em.make_fork(self.parent, "Jetsku/KytyPS5", accepted=True)
        exe = f.exe
        for name in ("KytyPS5 (Jetsku)", "KytyPS5"):
            self.touch(f"{name}/launcher.exe")
        found = em.scan_folder(self.tmp / "emus", [self.parent, f], "windows")
        self.assertEqual(found["kytyps5"].folder, str(self.tmp / "emus" / "KytyPS5"))
        self.assertEqual(found[f.id].folder, str(self.tmp / "emus" / "KytyPS5 (Jetsku)"))
        self.assertEqual(exe, "")

    def test_forks_are_never_offered_as_the_missing_emulator(self):
        f = em.make_fork(self.parent, "KytyPS5/other-build")
        out = em.missing_for_library([f, self.parent], self.store, {"PlayStation 5": 3})
        self.assertEqual([e.id for e, _n in out], ["kytyps5"])

    def test_enrich_leaves_forks_alone(self):
        f = em.make_fork(self.parent, "KytyPS5/other-build")
        rel = em.parse_relation({"emulators": [{"name": "KytyPS5", "AltNames": ["Kyty"], "platforms": ["PlayStation 5"]}]}, None)
        out = {e.id: e for e in em.enrich([self.parent, f], rel)}
        self.assertIn("Kyty", out["kytyps5"].aliases)
        self.assertEqual(out[f.id], f)

    def test_inspect_reports_release_and_flags_a_missing_one(self):
        facts = json.dumps({"stargazers_count": 26, "archived": False, "created_at": "2026-08-01T00:00:00Z",
                            "pushed_at": "2026-10-03T00:00:00Z", "fork": False}).encode()
        rels = [{"tag_name": "u59-int4b", "name": "x", "body": "", "published_at": "2026-10-03T00:59:00Z", "prerelease": False, "html_url": "",
                 "assets": [{"name": "KytyPS5-windows-x64.zip", "size": 5, "browser_download_url": "https://github.com/Jetsku/KytyPS5/releases/download/t/a.zip"}]}]

        def fake_get(url, token="", accept="", timeout=20):
            return facts if url.endswith("/repos/Jetsku/KytyPS5") else json.dumps(rels).encode()
        with mock.patch.object(rc, "_get", fake_get), mock.patch.object(rc, "platform_key", lambda: "windows"), \
                mock.patch.object(rc, "machine_key", lambda: "x64"):
            rep = em.inspect_fork(self.parent, "https://github.com/Jetsku/KytyPS5/releases", self.store, [self.parent])
            self.assertTrue(rep.ok and rep.standalone and not rep.known)
            self.assertEqual((rep.stars, rep.release, rep.asset), (26, "u59-int4b", "KytyPS5-windows-x64.zip"))
            rels[0]["assets"] = []
            self.assertFalse(em.inspect_fork(self.parent, "Jetsku/KytyPS5", self.store, [self.parent]).ok)
        self.assertIn("Enter a GitHub address", em.inspect_fork(self.parent, "nonsense", self.store).error)



class ForkFolderTests(Isolated):
    """A fork's folder often carries its parent's name too, so a scan hands it to the parent; linking it to the fork must take it back."""

    def setUp(self) -> None:
        super().setUp()
        self.parent = emu(id="kytyps5", name="KytyPS5", system="PlayStation 5", repo="KytyPS5/KytyPS5", systems=("PlayStation 5",), exe="kyty.*\\.exe$")
        self.fork = em.make_fork(self.parent, "Jetsku/KytyPS5", accepted=True)
        self.exe = self.touch("KytyPS5 Jetsku/kyty.exe")

    def scan(self, catalog):
        em.apply_scan(self.store, em.scan_folder(self.tmp / "emus", catalog, "windows"))

    def test_a_scan_without_the_fork_gives_the_folder_to_the_parent(self):
        self.scan([self.parent])
        self.assertIsNotNone(self.store.installed("kytyps5"))  # the situation the person is in

    def test_linking_the_fork_takes_the_folder_from_the_parent(self):
        self.scan([self.parent])
        found = em.link(self.store, self.fork, self.exe.parent, "windows")
        self.assertEqual(found.released, ("kytyps5",))
        self.assertIsNone(self.store.installed("kytyps5"))
        self.assertEqual(self.store.installed(self.fork.id).folder, str(self.exe.parent))
        self.assertTrue(self.exe.exists())  # nothing on disk was touched

    def test_a_later_scan_does_not_give_it_back_to_the_parent(self):
        self.scan([self.parent])
        em.link(self.store, self.fork, self.exe.parent, "windows")
        self.scan([self.parent, self.fork])
        self.scan([self.parent])
        self.assertIsNone(self.store.installed("kytyps5"))
        self.assertIsNotNone(self.store.installed(self.fork.id))

    def test_a_folder_distill_installed_for_another_emulator_is_not_taken(self):
        self.store.set_installed(rc.Installed("kytyps5", "1", str(self.exe.parent), "kyty.exe", 0.0, ""))  # a managed install: linked is empty
        with self.assertRaises(rc.RecompError):
            em.link(self.store, self.fork, self.exe.parent, "windows")
        self.assertIsNotNone(self.store.installed("kytyps5"))
        self.assertIsNone(self.store.installed(self.fork.id))

    def test_two_forks_in_their_own_folders_stay_apart(self):
        dlss = em.make_fork(self.parent, "someone/KytyPS5-DLSS", accepted=True)
        other = self.touch("KytyPS5 DLSS/kyty.exe")
        em.link(self.store, self.fork, self.exe.parent, "windows")
        found = em.link(self.store, dlss, other.parent, "windows")
        self.assertEqual(found.released, ())
        self.assertEqual({self.store.installed(self.fork.id).folder, self.store.installed(dlss.id).folder}, {str(self.exe.parent), str(other.parent)})


class PortableTests(Isolated):
    def test_marker_file_or_folder_is_made_beside_the_exe(self):
        d = Path(tempfile.mkdtemp())
        exe = d / "emu.exe"
        exe.write_bytes(b"x")
        self.assertTrue(em.make_portable(emu(id="a", portable="portable.txt"), exe))
        self.assertTrue((d / "portable.txt").is_file())
        d2 = d / "two"
        d2.mkdir()
        exe2 = d2 / "emu.exe"
        exe2.write_bytes(b"x")
        self.assertTrue(em.make_portable(emu(id="b", portable="user/"), exe2))
        self.assertTrue((d2 / "user").is_dir())

    def test_nothing_existing_is_touched_and_some_cases_are_skipped(self):
        d = Path(tempfile.mkdtemp())
        exe = d / "emu.exe"
        exe.write_bytes(b"x")
        (d / "portable.txt").write_text("mine")
        self.assertFalse(em.make_portable(emu(id="a", portable="portable.txt"), exe))
        self.assertEqual((d / "portable.txt").read_text(), "mine")
        self.assertFalse(em.make_portable(emu(id="b"), exe))  # no option
        self.assertFalse(em.make_portable(emu(id="c", portable="portable", portable_auto=False), exe))
        self.assertFalse(em.make_portable(emu(id="d", portable="portable"), d / "Emu.AppImage"))
        self.assertFalse(em.make_portable(emu(id="e", portable="portable"), d / "Emu.app" / "Contents" / "emu"))


class LogoTests(Isolated):
    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40

    def test_url_comes_from_the_catalog_or_the_owners_github_picture(self):
        self.assertEqual(em.logo_url(emu(repo="PCSX2/pcsx2")), "https://github.com/PCSX2/pcsx2".replace("/pcsx2", "") + ".png?size=128")
        self.assertEqual(em.logo_url(emu(logo="https://example.org/a.png")), "https://example.org/a.png")
        self.assertEqual(em.logo_url(emu(logo="http://example.org/a.png", source="gitlab", repo="a/b")), "")
        self.assertEqual(em.logo_url(em.make_fork(emu(repo="PCSX2/pcsx2"), "x/y", accepted=True)), "")

    def test_fetch_checks_the_image_and_caches_it(self):
        e = emu(repo="PCSX2/pcsx2")
        calls = []

        def get(url):
            calls.append(url)
            return self.PNG
        path = em.fetch_logo(e, get)
        self.assertTrue(path and path.read_bytes() == self.PNG)
        self.assertEqual(em.fetch_logo(e, get), path)
        self.assertEqual(len(calls), 1)  # the second call came from the cache

    def test_fetch_refuses_junk_and_survives_failures(self):
        e = emu(id="other", repo="a/b")
        self.assertIsNone(em.fetch_logo(e, lambda u: b"<html>not an image</html>"))
        self.assertIsNone(em.fetch_logo(e, lambda u: self.PNG + b"0" * em.LOGO_MAX))

        def boom(url):
            raise OSError("offline")
        self.assertIsNone(em.fetch_logo(e, boom))
        self.assertFalse(em.logo_file(e).exists())

    def test_steamgriddb_icon_is_preferred_over_the_owner_picture(self):
        e = emu(id="sgdbemu", name="Some Emu", repo="a/b")
        seen = {}

        def finder(names, key, cancel, deadline, min_score=0.6):
            seen.update(names=names, key=key, min_score=min_score)
            return self.PNG, object()
        path = em.fetch_logo_best(e, "KEY", lambda u: self.fail("the owner picture must not be fetched"), finder)
        self.assertEqual(path.read_bytes(), self.PNG)
        self.assertEqual(seen["names"], ["Some Emu"])
        self.assertGreaterEqual(seen["min_score"], 0.8)
        self.assertEqual(em.logo_source(e), "steamgriddb")
        self.assertTrue(em.logo_settled(e, "KEY"))

    def test_steamgriddb_miss_falls_back_to_the_owner_picture_and_is_remembered(self):
        e = emu(id="missemu", repo="a/b")
        path = em.fetch_logo_best(e, "KEY", lambda u: self.PNG, lambda *a, **k: None)
        self.assertTrue(path and path.read_bytes() == self.PNG)  # none is missing
        self.assertEqual(em.logo_source(e), "steamgriddb-miss")  # and SteamGridDB is not asked again
        self.assertTrue(em.logo_settled(e, "KEY"))

    def test_miss_with_the_owner_picture_unreachable_is_tried_again(self):
        e = emu(id="missoff", repo="a/b")

        def boom(url):
            raise OSError("offline")
        self.assertIsNone(em.fetch_logo_best(e, "KEY", boom, lambda *a, **k: None))
        self.assertFalse(em.logo_settled(e, "KEY"))

    def test_shipped_icon_wins_over_everything_and_matches_on_id(self):
        folder = Path(tempfile.mkdtemp())
        e = emu(id="myemu", repo="a/b")
        (folder / "MyEmu.JPG").write_bytes(b"x")
        (folder / "other.ICO").write_bytes(b"x")
        with mock.patch.object(em, "images_dir", lambda: folder):
            self.assertEqual(em.bundled_image(e).name, "MyEmu.JPG")
            self.assertEqual(em.icon_path(e).name, "MyEmu.JPG")
            self.assertTrue(em.logo_settled(e, "KEY"))
            self.assertEqual(em.fetch_logo_best(e, "KEY", lambda u: self.fail("no download"), lambda *a, **k: self.fail("no lookup")).name,
                             "MyEmu.JPG")
            fork = em.make_fork(e, "x/y", accepted=True)
            self.assertEqual(em.bundled_image(emu(id="Other", repo="a/b")).name, "other.ICO")  # .ico is accepted too
            self.assertEqual(em.bundled_image(fork).name, "MyEmu.JPG")  # a fork wears its emulator's picture
        with mock.patch.object(em, "images_dir", lambda: folder / "nothing"):
            self.assertIsNone(em.bundled_image(e))

    def test_steamgriddb_failure_is_not_remembered_as_a_miss(self):
        e = emu(id="offemu", repo="a/b")

        def finder(*a, **k):
            raise OSError("offline")
        self.assertIsNone(em.fetch_logo_best(e, "KEY", None, finder))
        self.assertEqual(em.logo_source(e), "")
        self.assertFalse(em.logo_settled(e, "KEY"))

    def test_catalog_address_wins_over_steamgriddb(self):
        e = emu(id="exp", logo="https://example.org/a.png")
        path = em.fetch_logo_best(e, "KEY", lambda u: self.PNG, lambda *a, **k: self.fail("not asked"))
        self.assertTrue(path and em.logo_source(e) == "explicit")



class SourceTests(Isolated):
    def test_dolphins_artifacts_are_named_by_system_so_the_right_one_is_picked(self):
        e = emu(id="dolphin", source="custom", adapter="dolphin_update", repo="", endpoint="https://dolphin-emu.org/update/latest/beta")
        answer = json.dumps({"shortrev": "2603a", "artifacts": [
            {"system": "Windows x64", "url": "https://dl.dolphin-emu.org/releases/2603a/dolphin-2603a-x64.7z"},
            {"system": "Windows ARM64", "url": "https://dl.dolphin-emu.org/releases/2603a/dolphin-2603a-ARM64.7z"},
            {"system": "macOS Universal", "url": "https://dl.dolphin-emu.org/releases/2603a/dolphin-2603a-universal.dmg"}]}).encode()
        with mock.patch.object(rc, "_get", lambda *a, **k: answer):
            (rel,) = em.fetch_releases(e, self.store, force=True)
        self.assertEqual([a["name"] for a in rel.assets], ["dolphin-2603a-windows-x64.7z", "dolphin-2603a-windows-arm64.7z", "dolphin-2603a-macos-universal.dmg"])
        win = rc.pick_asset(rel.assets, e, "windows", "x64")
        self.assertTrue(win["url"].endswith("dolphin-2603a-x64.7z"))
        self.assertTrue(rc.pick_asset(rel.assets, e, "windows", "arm64")["url"].endswith("ARM64.7z"))

    def test_retroarch_takes_the_newest_version_in_the_stable_listing(self):
        e = emu(id="retroarch", source="custom", adapter="libretro_buildbot", repo="")
        page = b'<a href="1.9.14/">1.9.14/</a><a href="1.20.0/">1.20.0/</a><a href="1.10.1/">1.10.1/</a><a href="latest/">latest/</a>'
        with mock.patch.object(rc, "_get", lambda *a, **k: page):
            (rel,) = em.fetch_releases(e, self.store, force=True)
        self.assertEqual(rel.tag, "1.20.0")
        self.assertEqual(rc.pick_asset(rel.assets, e, "windows", "x64")["url"], "https://buildbot.libretro.com/stable/1.20.0/windows/x86_64/RetroArch.7z")

    def test_downloads_are_pinned_to_the_sites_each_source_names(self):
        inst = em.EmulatorInstaller(self.store)
        forgejo = emu(id="ryujinx", source="forgejo", host="git.ryujinx.app", repo="projects/Ryubing")
        inst.check_asset(forgejo, {"url": "https://git.ryujinx.app/projects/Ryubing/releases/download/1.3.0/ryujinx-1.3.0-win_x64.zip"})
        for bad in ("https://evil.example/ryujinx.zip", "http://git.ryujinx.app/x.zip", "https://git.ryujinx.app.evil.example/x.zip"):
            with self.assertRaises(rc.RecompError):
                inst.check_asset(forgejo, {"url": bad})
        dolphin = emu(id="dolphin", source="custom", adapter="dolphin_update", repo="")
        inst.check_asset(dolphin, {"url": "https://dl.dolphin-emu.org/releases/2603a/dolphin-2603a-x64.7z"})
        with self.assertRaises(rc.RecompError):
            inst.check_asset(dolphin, {"url": "https://dolphin-emu.com/dolphin.7z"})
        retro = emu(id="retroarch", source="custom", adapter="libretro_buildbot", repo="")
        inst.check_asset(retro, {"url": "https://buildbot.libretro.com/stable/1.20.0/windows/x86_64/RetroArch.7z"})
        with self.assertRaises(rc.RecompError):
            inst.check_asset(retro, {"url": "https://retroarch.example/RetroArch.7z"})

    def test_the_bundled_entries_for_these_three_are_wired_to_the_right_places(self):
        by = {e.id: e for e in em.load_catalog()}
        r = by["ryujinx"]
        self.assertEqual((r.source, r.host, r.repo), ("forgejo", "git.ryujinx.app", "projects/Ryubing"))
        self.assertEqual(em.download_hosts(r), frozenset({"git.ryujinx.app"}))
        self.assertEqual(by["dolphin"].endpoint, "https://dolphin-emu.org/update/latest/beta")
        self.assertEqual(em.download_hosts(by["dolphin"]), frozenset({"dl.dolphin-emu.org"}))
        self.assertEqual(em.download_hosts(by["retroarch"]), frozenset({"buildbot.libretro.com"}))
        with mock.patch.object(rc, "platform_key", lambda: "windows"):  # Dolphin has no Linux download, by design
            for rid in ("ryujinx", "dolphin", "retroarch"):
                self.assertTrue(em.downloadable(by[rid]), rid)



class SourceRobustnessTests(Isolated):
    def test_dolphin_files_are_named_whatever_keys_label_them(self):
        e = emu(id="dolphin", source="custom", adapter="dolphin_update", repo="")
        base = "https://dl.dolphin-emu.org/releases/2603a/"
        for label in ({"system": "Windows x64"}, {"name": "Windows x64"}, {}):
            answer = json.dumps({"shortrev": "2603a", "artifacts": [
                {**label, "url": base + "dolphin-2603a-x64.7z"}, {"url": base + "dolphin-2603a-ARM64.7z"},
                {"url": base + "dolphin-2603a-universal.dmg"}, {"url": base + "dolphin-2603a.apk"}]}).encode()
            with mock.patch.object(rc, "_get", lambda *a, **k: answer):
                (rel,) = em.fetch_releases(e, self.store, force=True)
            self.assertEqual([a["name"] for a in rel.assets], ["dolphin-2603a-windows-x64.7z", "dolphin-2603a-windows-arm64.7z", "dolphin-2603a-macos-universal.dmg"])
            self.assertTrue(rc.pick_asset(rel.assets, e, "windows", "x64")["url"].endswith("dolphin-2603a-x64.7z"))

    def test_retroarch_falls_back_to_github_tags_and_reads_relative_links(self):
        e = emu(id="retroarch", source="custom", adapter="libretro_buildbot", repo="")

        def get(url, *a, **k):
            if "buildbot" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return json.dumps([{"name": "v1.9.14"}, {"name": "v1.20.0"}, {"name": "nightly"}]).encode()
        with mock.patch.object(rc, "_get", get):
            (rel,) = em.fetch_releases(e, self.store, force=True)
        self.assertEqual(rel.tag, "1.20.0")
        with mock.patch.object(rc, "_get", lambda *a, **k: b'<a href="./1.19.1/">1.19.1/</a>'):
            self.assertEqual(em.fetch_releases(e, self.store, force=True)[0].tag, "1.19.1")

    def test_a_failure_names_the_site_and_the_reason(self):
        e = emu(id="retroarch", source="custom", adapter="dolphin_update", repo="", endpoint="https://dolphin-emu.org/update/latest/beta")

        def boom(url, *a, **k):
            raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
        with mock.patch.object(rc, "_get", boom):
            with self.assertRaises(rc.RecompError) as cm:
                em.fetch_releases(e, self.store, force=True)
        self.assertIn("dolphin-emu.org (HTTP 503)", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
