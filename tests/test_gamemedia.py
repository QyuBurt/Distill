import hashlib
import json
import tempfile
import stat
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from distill import gamemedia as mm
from distill import recomps as rc

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 40
UUID = "0123abcd-4567-89ab-cdef-0123456789ab"
LB_IMAGES = f"""
<h3>Screenshot - Gameplay</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '1111')}.jpg" alt="Super Mario World - Screenshot - Gameplay">
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '2222')}.png" alt="Super Mario World - Screenshot - Title Screen">
<h3>Fanart - Background</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '3333')}.jpg" alt="Super Mario World - Fanart - Background">
<h3>Box - Front</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '4444')}.jpg" alt="Super Mario World - Box - Front">
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '1111')}.jpg" alt="again">
"""


class MediaCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(mm, "MEDIA_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        mm._PAGES.clear()  # (pages are kept for a few minutes: one test's must not be another's)


class LaunchBoxImageTests(MediaCase):
    def test_cache_readers_do_not_create_missing_or_unwritable_directories(self):
        missing = Path(mm.MEDIA_DIR) / "missing/media"
        with mock.patch.object(mm, "MEDIA_DIR", missing), \
                mock.patch.object(mm.paths, "ensure_parent", side_effect=PermissionError("fixture read-only")):
            self.assertIsNone(mm.cached_backdrop("fixture"))
            self.assertIsNone(mm.cached_recomp_banner("Fixture"))
            self.assertEqual(mm.cached_screenshots("fixture"), [])
        self.assertFalse(missing.exists())
        blocker = Path(mm.MEDIA_DIR) / "blocker"
        blocker.write_text("fixture")
        with mock.patch.object(mm, "MEDIA_DIR", blocker / "media"):
            self.assertIsNone(mm.cached_recomp_banner("Fixture"))
            self.assertEqual(mm.cached_screenshots("fixture"), [])

    def test_media_writers_create_missing_cache_parents(self):
        base = Path(mm.MEDIA_DIR)
        target = base / "images/new/back_fixture.jpg"
        with mock.patch.object(rc, "_get", return_value=JPEG):
            self.assertTrue(mm._download("https://fixture.invalid/image", target))
        self.assertEqual(target.read_bytes(), JPEG)
        if mm.paths.layout() == "xdg":
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        with mock.patch.object(mm, "MEDIA_DIR", base / "miss/new"), \
                mock.patch.object(mm, "lb_game_id", return_value=""), \
                mock.patch.object(mm, "launchbox_images", return_value={"fanart": [], "screenshots": []}):
            self.assertIsNone(mm.fetch_backdrop("fixture", "Fixture", "SNES"))
            self.assertTrue((mm.MEDIA_DIR / "back_fixture.miss").is_file())

    def test_thumbnail_tidy_failure_is_an_unavailable_cache_not_a_ui_exception(self):
        first, second = (Path(mm.MEDIA_DIR) / f"shot_fixture_{i:02d}.jpg" for i in range(2))
        for path in (first, second):
            path.write_bytes(JPEG)
        with mock.patch.object(mm, "_image_size", return_value=(1000, 600)), \
                mock.patch.object(mm, "_is_preview_of", return_value=True), \
                mock.patch.object(Path, "replace", side_effect=PermissionError("fixture read-only")):
            self.assertEqual(mm.cached_screenshots("fixture"), [])
        self.assertEqual(first.read_bytes(), JPEG)
        self.assertEqual(second.read_bytes(), JPEG)

    def test_pictures_are_sorted_by_the_words_around_them(self):
        got = mm.parse_launchbox_images(LB_IMAGES)
        self.assertEqual(len(got["screenshots"]), 2)
        self.assertEqual(len(got["fanart"]), 1)
        self.assertTrue(got["fanart"][0].startswith("https://images.launchbox-app.com/3333"))
        self.assertEqual(mm.parse_launchbox_images("<p>nothing</p>"), {"screenshots": [], "fanart": []})

    def test_a_backdrop_comes_from_launchbox_fan_art_and_is_kept(self):
        calls = []

        def fake_get(url, token="", accept="", timeout=20):
            calls.append(url)
            if "results" in url:
                return b"<a href='/games/details/777-super-mario-world'>Super Mario World</a> Super Nintendo Entertainment System"
            if "/images/" in url:
                return LB_IMAGES.encode()
            return JPEG

        with mock.patch.object(rc, "_get", fake_get), mock.patch.object(rc, "_lb_game_ids", lambda page, title, system: ["777"]):
            path = mm.fetch_backdrop("g1", "Super Mario World", "Super Nintendo Entertainment System")
            self.assertEqual(path, mm.cached_backdrop("g1"))
            n = len(calls)
            self.assertEqual(mm.fetch_backdrop("g1", "Super Mario World", "x"), path)
            self.assertEqual(len(calls), n)  # kept: nothing asked again

    def test_nothing_online_is_remembered_so_it_is_not_asked_again(self):
        asked = []

        def fake_get(url, token="", accept="", timeout=20):
            asked.append(url)
            return b"<html></html>"

        with mock.patch.object(rc, "_get", fake_get), mock.patch.object(rc, "_lb_game_ids", lambda *a: []):
            self.assertIsNone(mm.fetch_backdrop("g2", "Obscure", "x"))
            n = len(asked)
            self.assertIsNone(mm.fetch_backdrop("g2", "Obscure", "x"))
            self.assertEqual(len(asked), n)
            self.assertIsNone(mm.fetch_backdrop("g2", "Obscure", "x", retry=True) if False else None)

    def test_a_recomp_banner_belongs_to_the_game_and_falls_back_to_the_base_title(self):
        r = rc.Recomp("a", "Ship", "Gothic 2: Night of the Raven", "PC", "o/a")
        r2 = rc.Recomp("b", "Other port", "Gothic 2: Night of the Raven", "PC", "o/b")
        self.assertEqual(mm.recomp_banner_key(r.game), mm.recomp_banner_key(r2.game))  # projects for one game share the picture
        asked = []

        def fake_backdrop(key, title, platform, sgdb="", retry=False):
            asked.append(title)
            if title != "Gothic 2":
                return None
            dest = Path(mm.MEDIA_DIR) / f"back_{key}.jpg"
            dest.write_bytes(JPEG)
            return dest

        with mock.patch.object(mm, "fetch_backdrop", fake_backdrop):
            got = mm.fetch_recomp_banner(r)
        self.assertEqual(asked, ["Gothic 2: Night of the Raven", "Gothic 2"])
        self.assertEqual(got, mm.cached_recomp_banner(r2.game))

    def test_a_recomp_with_no_banner_online_is_not_searched_again_for_a_week(self):
        r = rc.Recomp("a", "Ship", "Obscure Game", "PC", "o/a")
        asked = []

        def fake_get(url, token="", accept="", timeout=20):
            asked.append(url)
            return b"<html></html>"

        with mock.patch.object(rc, "_get", fake_get), mock.patch.object(rc, "_lb_game_ids", lambda *a: []):
            self.assertIsNone(mm.fetch_recomp_banner(r))
            n = len(asked)
            self.assertGreater(n, 0)
            self.assertIsNone(mm.fetch_recomp_banner(r))
            self.assertEqual(len(asked), n)

    def test_screenshots_are_downloaded_up_to_the_limit(self):
        def fake_get(url, token="", accept="", timeout=20):
            return LB_IMAGES.encode() if "/images/" in url else JPEG

        with mock.patch.object(rc, "_get", fake_get), mock.patch.object(rc, "_lb_game_ids", lambda *a: ["777"]):
            shots = mm.fetch_screenshots("g3", "Super Mario World", "x", limit=1)
            self.assertEqual(len(shots), 1)
            self.assertEqual(mm.cached_screenshots("g3"), shots)

    def test_a_page_with_previews_keeps_one_big_picture_each_and_fetches_previews_first(self):
        big, small = [UUID.replace("0123", f"a{i}00") for i in range(2)], [UUID.replace("0123", f"b{i}00") for i in range(2)]
        page = "<h3>Screenshot</h3>" + "".join(
            f'<a href="https://images.launchbox-app.com/{b}.jpg"><img src="https://images.launchbox-app.com/{t}.jpg" alt="Screenshot"></a>' for b, t in zip(big, small))
        order, updates = [], []

        def fake_get(url, token="", accept="", timeout=20):
            order.append(url)
            return page.encode() if "/images/" in url else JPEG

        with mock.patch.object(rc, "_get", fake_get), mock.patch.object(rc, "_lb_game_ids", lambda *a: ["777"]):
            shots = mm.fetch_screenshots("g4", "Game", "x", on_update=lambda paths: updates.append([p.is_file() for p in paths]))
        self.assertEqual(len(shots), 2)
        self.assertTrue(all(p.is_file() and mm.thumb_path(p).is_file() for p in shots))
        downloads = [u for u in order if "/games/" not in u]
        names = [u.rsplit("/", 1)[1][:2] for u in downloads]
        self.assertEqual((names[:2], sorted(names[2:])), (["b0", "b1"], ["a0", "a1"]))  # the previews first, then the big ones (which come a few at a time)
        self.assertEqual(updates[0], [False, False])  # the page could already show the previews, the big pictures still to come
        self.assertEqual(mm.shot_image(shots[0]), shots[0])
        shots[0].unlink()
        self.assertEqual(mm.shot_image(shots[0]), mm.thumb_path(shots[0]))  # until it is there, the preview stands in

    def test_a_small_copy_saved_beside_each_big_picture_is_tidied_into_a_preview(self):
        import struct
        import zlib

        def png(w, h):
            raw = b"".join(b"\x00" + b"\x00" * (w * 3) for _ in range(h))
            def chunk(t, d):
                return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
            return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

        d = Path(mm.MEDIA_DIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / "shot_g5_00.jpg").write_bytes(png(1200, 675))
        (d / "shot_g5_01.jpg").write_bytes(png(400, 225))
        (d / "shot_g5_02.jpg").write_bytes(png(460, 215))  # a different shape: a screenshot of its own
        got = mm.cached_screenshots("g5")
        self.assertEqual([p.name for p in got], ["shot_g5_00.jpg", "shot_g5_02.jpg"])
        self.assertTrue((d / "shot_g5_00_t.jpg").is_file())
        self.assertFalse((d / "shot_g5_01.jpg").exists())

    def test_editing_a_game_clears_what_was_saved_for_it(self):
        d = Path(mm.MEDIA_DIR)
        d.mkdir(parents=True, exist_ok=True)
        for name in ("back_g1.jpg", "back_g1.miss", "shot_g1_00.jpg", "shot_g1_01.jpg", "shot_g1.miss", "back_g2.jpg"):
            (d / name).write_bytes(b"x")
        mm.clear_media("g1")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["back_g2.jpg"])  # only that game's, and its remembered misses

    def test_something_that_is_not_a_picture_is_not_kept(self):
        with mock.patch.object(rc, "_get", lambda *a, **k: b"<html>error</html>"):
            self.assertFalse(mm._download("https://x/y.jpg", Path(mm.MEDIA_DIR) / "z.jpg"))


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40
LB_MEDIA = f"""
<h3>Cart - Back</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '6666')}.png" alt="Super Mario World - Cart - Back">
<h3>Cart - Front</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '5555')}.png" alt="Super Mario World - Cart - Front">
<h3>Disc</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '7777')}.png" alt="Gran Turismo - Disc">
<h3>Box - Front</h3>
<img src="https://images.launchbox-app.com/{UUID.replace('0123', '4444')}.jpg" alt="Super Mario World - Box - Front">
"""


class DiscAndCartTests(MediaCase):
    def test_the_page_s_disc_and_cartridge_are_found_fronts_first(self):
        got = mm.parse_launchbox_images(LB_MEDIA)
        self.assertEqual([u.split("/")[-1][:4] for u in got["cart"]], ["5555", "6666"])  # the front, then the back
        self.assertEqual([u.split("/")[-1][:4] for u in got["disc"]], ["7777"])
        self.assertNotIn("4444", "".join(got["cart"] + got["disc"] + got["screenshots"] + got["fanart"]))  # box art is not one of them

    def test_a_page_without_them_has_only_the_two_usual_keys(self):
        self.assertEqual(mm.parse_launchbox_images(LB_IMAGES).keys(), {"screenshots", "fanart"})

    def test_which_systems_get_which(self):
        self.assertEqual([mm.media_kind(s) for s in ("PlayStation", "GameCube", "Sega Saturn", "Dreamcast")], ["disc"] * 4)
        self.assertEqual([mm.media_kind(s) for s in ("NES", "SNES", "Nintendo 64", "Game Boy", "Mega Drive", "Switch")], ["cart"] * 6)
        self.assertEqual([mm.media_kind(s) for s in ("PSP", "Famicom Disk System", "PC", "", "Unknown")], [""] * 5)

    def _fake_get(self, calls):
        def fake_get(url, token="", accept="", timeout=20):
            calls.append(url)
            if "results" in url:
                return b"<a href='/games/details/777-super-mario-world'>Super Mario World</a> Super Nintendo Entertainment System"
            if "/images/" in url:
                return LB_MEDIA.encode()
            return PNG
        return fake_get

    def test_the_cartridge_is_fetched_kept_and_not_asked_for_again(self):
        calls: list[str] = []
        with mock.patch.object(rc, "_get", self._fake_get(calls)), mock.patch.object(rc, "_lb_game_ids", lambda page, title, system: ["777"]):
            path = mm.fetch_media_art("g1", "cart", "Super Mario World", "Super Nintendo Entertainment System")
            self.assertEqual(path, mm.cached_media_art("g1", "cart"))
            self.assertTrue(any(u.endswith("5555" + UUID[4:] + ".png") or "/5555" in u for u in calls))  # the front was the one downloaded
            n = len(calls)
            self.assertEqual(mm.fetch_media_art("g1", "cart", "Super Mario World", "x"), path)
            self.assertEqual(len(calls), n)
            self.assertIsNone(mm.cached_media_art("g1", "disc"))

    def test_no_picture_online_is_remembered_and_other_kinds_are_refused(self):
        with mock.patch.object(rc, "_get", lambda *a, **k: b"<p>nothing</p>"), mock.patch.object(rc, "_lb_game_ids", lambda page, title, system: ["777"]):
            self.assertIsNone(mm.fetch_media_art("g2", "disc", "Gran Turismo", "Sony Playstation"))
            self.assertTrue((Path(mm.MEDIA_DIR) / "disc_g2.miss").is_file())
            self.assertIsNone(mm.fetch_media_art("g2", "box", "Gran Turismo", "Sony Playstation"))

    def test_editing_a_game_clears_its_disc_and_cartridge_too(self):
        d = Path(mm.MEDIA_DIR)
        d.mkdir(parents=True, exist_ok=True)
        for name in ("disc_g1.png", "cart_g1.png", "cart_g1.miss", "cart_g2.png"):
            (d / name).write_bytes(b"x")
        mm.clear_media("g1")
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["cart_g2.png"])


class RetroAchievementsTests(MediaCase):
    GAMES = [{"ID": 1, "Title": "Super Mario World", "NumAchievements": 70}, {"ID": 2, "Title": "~Hack~ Super Mario World: Extra", "NumAchievements": 30},
             {"ID": 3, "Title": "Legend of Zelda, The: A Link to the Past", "NumAchievements": 60}, {"ID": 4, "Title": "Empty Game", "NumAchievements": 0}]
    PROGRESS = {"ID": 3, "Title": "The Legend of Zelda: A Link to the Past", "ConsoleName": "SNES", "Achievements": {
        "10": {"ID": 10, "Title": "Locked", "Description": "d", "Points": 10, "BadgeName": "111"},
        "11": {"ID": 11, "Title": "Won", "Description": "d2", "Points": 5, "BadgeName": "222", "DateEarned": "2024-01-02 03:04:05"},
        "12": {"ID": 12, "Title": "Hard", "Description": "d3", "Points": 25, "BadgeName": "333", "DateEarned": "2024-01-01", "DateEarnedHardcore": "2024-01-01"}}}

    def test_titles_match_across_punctuation_articles_and_tags(self):
        self.assertEqual(mm.ra_norm("Legend of Zelda, The - A Link to the Past (USA) [!]"), "legend of zelda a link to the past")
        self.assertEqual(mm.ra_norm("The Legend of Zelda: A Link to the Past"), "legend of zelda a link to the past")
        self.assertEqual(mm.ra_best("Super Mario World", self.GAMES)["ID"], 1)
        self.assertEqual(mm.ra_best("The Legend of Zelda: A Link to the Past", self.GAMES)["ID"], 3)
        self.assertIsNone(mm.ra_best("Empty Game", self.GAMES))  # no achievements
        self.assertIsNone(mm.ra_best("Something Else Entirely", self.GAMES))
        self.assertIsNone(mm.ra_best("", self.GAMES))

    def test_progress_is_read_with_the_unlocked_first(self):
        game = mm.parse_ra_progress(self.PROGRESS)
        self.assertEqual([a.title for a in game.achievements], ["Won", "Hard", "Locked"])
        self.assertEqual((game.earned, game.points), (2, (30, 40)))
        self.assertTrue(game.achievements[1].hardcore and not game.achievements[0].hardcore)
        self.assertEqual(game.url, "https://retroachievements.org/game/3")
        self.assertIsNone(mm.parse_ra_progress({}))
        self.assertIsNone(mm.parse_ra_progress([]))

    def test_a_game_is_found_and_its_progress_cached(self):
        calls = []

        def fake_get(url, token="", accept="", timeout=20):
            calls.append(url)
            if "GetGameList" in url:
                return json.dumps(self.GAMES).encode()
            return json.dumps(self.PROGRESS).encode()

        with mock.patch.object(rc, "_get", fake_get):
            game = mm.ra_progress("SNES", "Legend of Zelda, The - A Link to the Past (USA)", "me", "key")
            self.assertEqual(game.id, 3)
            self.assertIn("i=3", calls[0])  # SNES is console 3
            n = len(calls)
            self.assertEqual(mm.ra_progress("SNES", "Legend of Zelda, The - A Link to the Past (USA)", "me", "key").id, 3)
            self.assertEqual(len(calls), n)
            self.assertIsNone(mm.ra_progress("Switch", "Anything", "me", "key"))  # no console on RetroAchievements

    def test_what_can_go_wrong_is_said_in_words(self):
        with self.assertRaisesRegex(mm.MediaError, "API keys"):
            mm.ra_progress("SNES", "X", "", "")

        def refused(*a, **k):
            raise urllib.error.HTTPError("u", 401, "no", {}, None)

        def offline(*a, **k):
            raise urllib.error.URLError("down")

        with mock.patch.object(rc, "_get", refused):
            self.assertIn("didn't accept", mm.ra_test("me", "bad"))
        with mock.patch.object(rc, "_get", offline):
            self.assertIn("Couldn't reach", mm.ra_test("me", "k"))
        with mock.patch.object(rc, "_get", lambda *a, **k: b'{"User": "me"}'):
            self.assertEqual(mm.ra_test("me", "k"), "")

    def test_badges_are_downloaded_once(self):
        png = b"\x89PNG" + b"x" * 30
        calls = []

        def fake_get(url, token="", accept="", timeout=20):
            calls.append(url)
            return png

        with mock.patch.object(rc, "_get", fake_get):
            a = mm.ra_badge("12345")
            b = mm.ra_badge("12345", locked=True)
            self.assertNotEqual(a, b)
            self.assertTrue(calls[1].endswith("12345_lock.png"))
            mm.ra_badge("12345")
            self.assertEqual(len(calls), 2)
            self.assertIsNone(mm.ra_badge("../.."))


class RaHashTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_a_cartridge_is_hashed_by_its_content_whatever_it_is_called(self):
        body = b"rom" * 100
        self.assertEqual(mm.ra_rom_hash(self.write("a.gba", body), "Game Boy Advance"), hashlib.md5(body).hexdigest())
        self.assertEqual(mm.ra_rom_hash(self.write("totally different.bin", body), "Game Boy Advance"), hashlib.md5(body).hexdigest())

    def test_headers_are_left_out(self):
        body = b"prg" * 100
        self.assertEqual(mm.ra_rom_hash(self.write("a.nes", b"NES\x1a" + b"\0" * 12 + body), "NES"), hashlib.md5(body).hexdigest())
        self.assertEqual(mm.ra_rom_hash(self.write("b.nes", body), "NES"), hashlib.md5(body).hexdigest())  # no header: the whole file
        snes = b"x" * 1024
        self.assertEqual(mm.ra_rom_hash(self.write("a.smc", b"\0" * 512 + snes), "SNES"), hashlib.md5(snes).hexdigest())
        self.assertEqual(mm.ra_rom_hash(self.write("a.sfc", snes), "SNES"), hashlib.md5(snes).hexdigest())

    def test_nintendo_64_is_hashed_in_big_endian_order(self):
        z64 = b"\x80\x37\x12\x40" + b"\x01\x02\x03\x04" * 4
        v64 = bytearray(z64)
        v64[0::2], v64[1::2] = z64[1::2], z64[0::2]
        n64 = bytearray(len(z64))
        for i in range(4):
            n64[i::4] = z64[3 - i::4]
        want = hashlib.md5(z64).hexdigest()
        self.assertEqual(mm.ra_rom_hash(self.write("a.z64", z64), "Nintendo 64"), want)
        self.assertEqual(mm.ra_rom_hash(self.write("a.v64", bytes(v64)), "Nintendo 64"), want)
        self.assertEqual(mm.ra_rom_hash(self.write("a.n64", bytes(n64)), "Nintendo 64"), want)

    def test_a_zip_with_one_file_is_hashed_by_what_is_inside(self):
        import zipfile
        body = b"inside" * 50
        z = self.dir / "a.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("game.gb", body)
        self.assertEqual(mm.ra_rom_hash(z, "Game Boy"), hashlib.md5(body).hexdigest())
        two = self.dir / "b.zip"
        with zipfile.ZipFile(two, "w") as f:
            f.writestr("1.gb", b"1")
            f.writestr("2.gb", b"2")
        self.assertEqual(mm.ra_rom_hash(two, "Game Boy"), "")

    def test_discs_missing_files_and_other_platforms_have_no_hash(self):
        self.assertEqual(mm.ra_rom_hash(self.write("a.chd", b"disc"), "PlayStation"), "")
        self.assertEqual(mm.ra_rom_hash(self.dir / "nope.gb", "Game Boy"), "")
        self.assertEqual(mm.ra_rom_hash(self.write("empty.gb", b""), "Game Boy"), "")

    def test_the_hash_picks_the_game_even_when_the_name_is_wrong(self):
        games = [{"ID": 1, "Title": "Other", "NumAchievements": 5, "Hashes": ["AAAA"]}, {"ID": 2, "Title": "Real Game", "NumAchievements": 9, "Hashes": ["ABCD", "ef01"]},
                 {"ID": 3, "Title": "No Set", "NumAchievements": 0, "Hashes": ["dead"]}]
        self.assertEqual(mm.ra_hash_hit("abcd", games)["ID"], 2)
        self.assertEqual(mm.ra_hash_hit("EF01".lower(), games)["ID"], 2)
        self.assertIsNone(mm.ra_hash_hit("dead", games))  # a set with no achievements is passed over
        self.assertIsNone(mm.ra_hash_hit("", games))
        self.assertIsNone(mm.ra_hash_hit("ffff", games))

    def test_a_renamed_file_finds_its_game_by_hash_and_an_unknown_one_falls_back_to_the_name(self):
        body = b"zelda rom" * 20
        md5 = hashlib.md5(body).hexdigest()
        games = [{"ID": 7, "Title": "The Real Name", "NumAchievements": 3, "Hashes": [md5]}, {"ID": 8, "Title": "Other Thing", "NumAchievements": 3, "Hashes": []}]
        calls = []

        def fake_get(url, token="", accept="", timeout=20):
            calls.append(url)
            if "GetGameList" in url:
                return json.dumps(games).encode()
            return json.dumps({"ID": int(url.split("g=")[1].split("&")[0]), "Title": "x", "Achievements": {}}).encode()

        path = self.write("zzz.gba", body)
        with tempfile.TemporaryDirectory() as cache, mock.patch.object(rc, "_get", fake_get), mock.patch.object(mm, "MEDIA_DIR", Path(cache)):
            self.assertEqual(mm.ra_progress("Game Boy Advance", "Totally Wrong Title", "me", "key", str(path)).id, 7)
            self.assertIn("h=1", calls[0])  # the list is asked for with its hashes
            self.assertEqual(mm.ra_progress("Game Boy Advance", "Other Thing", "me", "key", str(self.write("o.gba", b"unlisted"))).id, 8)
            self.assertEqual(mm.ra_progress("Game Boy Advance", "Other Thing", "me", "key").id, 8)  # no file given: by name alone, as before


def _u(n: str, ext: str = "png", host: str = "https://images.launchbox-app.com//") -> str:
    return f"{host}{n * 8}-1111-4222-8333-{n * 12}.{ext}"


def _entry_page(name: str, platform: str, plat_id: int = 29, media: str = "") -> str:
    """A LaunchBox game page the way the site writes it now: a heading, a platform link, then the Media section."""
    return (f'<h1>{name}</h1><div>Platform</div><a href="https://gamesdb.launchbox-app.com/platforms/games/{plat_id}-{platform.replace(" ", "-")}">{platform}</a>'
            f'<h2>Media</h2>{media}')


class CurrentLaunchBoxPageTests(MediaCase):
    """LaunchBox's pages as they are now: pictures on two hosts, written with a double slash, fan-made ones among the official."""
    MEDIA = (
        f'<h3>Box - Front</h3><img alt="Mother 3 - Box - Front (Japan) - 3185x2030" src="{_u("1", "jpg")}">'
        f'<h3>Cart - Front</h3><img alt="Mother 3 - Cart - Front (Japan) - 640x375" src="{_u("2")}">'
        f'<h3>Clear Logo</h3><img alt="Mother 3 - Clear Logo (Japan) - 2995x710" src="{_u("3")}">'
        f'<h3>Fanart - Background</h3><img alt="Mother 3 - Fanart - Background (Japan) - 3579x2013" src="{_u("4", "png", "https://gamesdb-images.launchbox.gg/r2_")}">'
        f'<h3>Fanart - Box - Front</h3><img alt="Mother 3 - Fanart - Box - Front (North America) - 1370x1370" src="{_u("5", "jpg")}">'
        f'<h3>Fanart - Cart - Front</h3><img alt="Mother 3 - Fanart - Cart - Front (North America) - 473x283" src="{_u("6")}">'
        f'<h3>Screenshot - Gameplay</h3><img alt="Mother 3 - Screenshot - Gameplay (Japan) - 3840x2560" src="{_u("7", "png", "https://gamesdb-images.launchbox.gg/r2_")}">'
    )

    def test_both_hosts_and_the_double_slash_are_read(self):
        got = mm.parse_launchbox_images(self.MEDIA)
        self.assertEqual(len(got["cart"]), 2)
        self.assertEqual(len(got["fanart"]), 1)
        self.assertEqual(len(got["screenshots"]), 1)
        self.assertIn("gamesdb-images.launchbox.gg/r2_", got["fanart"][0] + got["screenshots"][0])

    def test_the_official_cartridge_comes_before_a_fan_made_one(self):
        got = mm.parse_launchbox_images(self.MEDIA)
        self.assertEqual([u.split("/")[-1][:1] for u in got["cart"]], ["2", "6"])

    def test_fan_made_box_art_is_not_a_background_and_a_logo_is_nothing(self):
        got = mm.parse_launchbox_images(self.MEDIA)
        flat = "".join(got["fanart"] + got["cart"] + got["screenshots"])
        for n in "135":
            self.assertNotIn("/" + n * 8, flat)

    def test_box_art_for_the_cover_search_is_found_on_both_hosts_too(self):
        page = (f'<h3>Box - Front</h3><img alt="Mother 3 - Box - Front (Japan) - 3185x2030" src="{_u("1", "jpg")}">'
                f'<h3>Box - Front</h3><img alt="Mother 3 - Box - Front (World) - 999x1000" src="{_u("8", "png", "https://gamesdb-images.launchbox.gg/r2_")}">')
        self.assertEqual(len(rc._lb_posters(page)), 2)

    def test_a_front_beats_a_3d_render_beats_a_back(self):
        page = (f'<h3>Cart - Back</h3><img alt="G - Cart - Back (Japan)" src="{_u("a")}">'
                f'<h3>Cart - 3D</h3><img alt="G - Cart - 3D (Japan)" src="{_u("b")}">'
                f'<h3>Cart - Front</h3><img alt="G - Cart - Front (Japan)" src="{_u("c")}">')
        self.assertEqual([u.split("/")[-1][:1] for u in mm.parse_launchbox_images(page)["cart"]], ["c", "b", "a"])


class FindingTheRightEntryTests(MediaCase):
    """Mother 3 is on LaunchBox as 'Mother 3' (Game Boy Advance) and as 'Mother 3: Claus's Journey', which has box art and nothing else. A
    search result whose link names no game ('/games/details/9029') used to rank below the one that does."""
    SEARCH = ("<a href='/games/details/169285-mother-3-clauss-journey'>Mother 3: Claus's Journey</a> Nintendo Game Boy Advance "
              "<a href='/games/details/9029'>Mother 3</a> Nintendo Game Boy Advance")

    def _get(self, calls, pages):
        def fake_get(url, token="", accept="", timeout=20):
            calls.append(url)
            if "/results/" in url:
                return self.SEARCH.encode()
            gid = url.rstrip("/").rsplit("/", 1)[-1]
            if gid in pages:
                return pages[gid].encode()
            return PNG
        return fake_get

    PAGES = {"169285": _entry_page("Mother 3: Claus's Journey", "Nintendo Game Boy Advance", media='<h3>Box - Front</h3><img alt="x - Box - Front" src="' + _u("9", "jpg") + '">'),
             "9029": _entry_page("Mother 3", "Nintendo Game Boy Advance", media=CurrentLaunchBoxPageTests.MEDIA)}

    def test_the_entry_named_exactly_wins_whatever_the_link_says(self):
        with mock.patch.object(rc, "_get", self._get([], self.PAGES)):
            self.assertEqual(mm.lb_game_id("Mother 3", "Nintendo Game Boy Advance"), "9029")

    def test_the_cartridge_is_found_for_it(self):
        calls: list[str] = []
        with mock.patch.object(rc, "_get", self._get(calls, self.PAGES)):
            path = mm.fetch_media_art("m3", "cart", "Mother 3", "Nintendo Game Boy Advance")
        self.assertIsNotNone(path)
        self.assertTrue(any("/222222222222.png" in u or "22222222-1111" in u for u in calls))  # the official cartridge, not a fan-made one

    def test_the_right_platform_beats_a_better_name(self):
        pages = {"1": _entry_page("Tetris", "Nintendo Entertainment System", 40), "2": _entry_page("Tetris", "Nintendo Game Boy", 28),
                 "3": _entry_page("Tetris Attack", "Nintendo Game Boy", 28)}
        search = "<a href='/games/details/1-tetris'>t</a><a href='/games/details/2-tetris'>t</a><a href='/games/details/3-tetris-attack'>t</a>"

        def fake_get(url, token="", accept="", timeout=20):
            return search.encode() if "/results/" in url else pages.get(url.rsplit("/", 1)[-1], "").encode()

        with mock.patch.object(rc, "_get", fake_get):
            self.assertEqual(mm.lb_game_id("Tetris", "Nintendo Game Boy"), "2")

    def test_a_game_known_by_only_its_base_title_is_found_without_the_subtitle(self):
        pages = {"7": _entry_page("Gothic 2", "Microsoft Windows", 5)}
        seen: list[str] = []

        def fake_get(url, token="", accept="", timeout=20):
            seen.append(url)
            if "/results/" in url:
                return b"<a href='/games/details/7-gothic-2'>Gothic 2</a>" if "Night" not in url else b"nothing here"
            return pages.get(url.rsplit("/", 1)[-1], "").encode()

        with mock.patch.object(rc, "_get", fake_get):
            self.assertEqual(mm.lb_game_id("Gothic 2: Night of the Raven", "Microsoft Windows"), "7")

    def test_an_answer_remembered_by_the_old_search_is_not_believed(self):
        d = Path(mm.MEDIA_DIR)
        d.mkdir(parents=True, exist_ok=True)
        old_id = d / f"lbid_{__import__('hashlib').md5(b'Mother 3|Nintendo Game Boy Advance').hexdigest()[:16]}.txt"
        old_id.write_text("169285")  # the wrong entry, remembered by the old search
        (d / "cart_m3.miss").write_text("miss")  # and "no cartridge", remembered by it
        with mock.patch.object(rc, "_get", self._get([], self.PAGES)):
            self.assertEqual(mm.lb_game_id("Mother 3", "Nintendo Game Boy Advance"), "9029")
            self.assertIsNotNone(mm.fetch_media_art("m3", "cart", "Mother 3", "Nintendo Game Boy Advance"))  # looked for again, not skipped

    def test_a_nothing_found_from_the_new_search_is_believed_for_a_week(self):
        with mock.patch.object(rc, "_get", lambda *a, **k: b"<p>nothing</p>"):
            self.assertIsNone(mm.fetch_media_art("g9", "cart", "Nothing Here", "Nintendo Game Boy"))
        calls: list[str] = []
        with mock.patch.object(rc, "_get", lambda url, **k: calls.append(url) or b""):
            self.assertIsNone(mm.fetch_media_art("g9", "cart", "Nothing Here", "Nintendo Game Boy"))
        self.assertEqual(calls, [])

    def test_one_game_s_pictures_read_each_page_once(self):
        calls: list[str] = []
        with mock.patch.object(rc, "_get", self._get(calls, self.PAGES)):
            mm.fetch_backdrop("m3", "Mother 3", "Nintendo Game Boy Advance")
            mm.fetch_media_art("m3", "cart", "Mother 3", "Nintendo Game Boy Advance")
            mm.fetch_screenshots("m3", "Mother 3", "Nintendo Game Boy Advance")
        self.assertEqual(sum(u.endswith("/details/9029") for u in calls), 1)  # (not once for the background, again for the cartridge, again for the screenshots)
        self.assertEqual(sum("/results/" in u for u in calls), len({u for u in calls if "/results/" in u}))  # nor any search twice



if __name__ == "__main__":
    unittest.main()
