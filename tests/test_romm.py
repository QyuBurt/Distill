import io
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from distill import games as gm
from distill import romm

TOKEN = "rmm_secrettoken"


def rom(i, name, fs, slug="snes", platform="Super Nintendo Entertainment System", **extra):
    return {"id": i, "name": name, "fs_name": fs, "platform_slug": slug, "platform_fs_slug": slug, "platform_display_name": platform,
            "fs_size_bytes": 11, "url_cover": f"/assets/romm/{i}.jpg", "files": [{"file_name": fs, "file_size_bytes": 11}], **extra}


ROMS = [rom(1, "Super Mario World", "Super Mario World (USA).sfc", regions=["USA"], metadatum={"first_release_date": 658800000000, "genres": ["Platform"], "companies": ["Nintendo"]}),
        rom(2, "Zelda", "Zelda.sfc"),
        rom(3, "Final Fantasy VII", "ff7", slug="psx", platform="Sony PlayStation", has_multiple_files=True,
            files=[{"file_name": "ff7.cue", "file_size_bytes": 1}, {"file_name": "ff7.bin", "file_size_bytes": 99}])]


def zipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ff7.cue", "FILE ff7.bin BINARY")
        z.writestr("ff7.bin", b"x" * 99)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        Handler.seen.append((self.path, self.headers.get("Authorization", "")))
        if self.path.startswith("/assets/"):
            return self.reply(200, b"\xff\xd8\xff" + b"c" * 2000, "image/jpeg")
        if self.path == "/api/heartbeat":
            return self.reply(200, b'{"SYSTEM": {"VERSION": "4.0"}}')
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, b"{}")
        if self.path.startswith("/api/platforms"):
            return self.reply(200, b"[]")
        if self.path.startswith("/api/roms/3/content/"):
            return self.reply(200, zipped(), "application/zip")
        if self.path.startswith("/api/roms/") and "/content/" in self.path:
            return self.reply(200, b"hello world", "application/octet-stream")
        if self.path.startswith("/api/roms"):
            q = dict(p.split("=") for p in self.path.split("?")[1].split("&"))
            lo, n = int(q["offset"]), int(q["limit"])
            return self.reply(200, json.dumps({"items": ROMS[lo:lo + 2], "total": len(ROMS), "offset": lo, "limit": n}).encode())
        return self.reply(404)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class AddressTests(unittest.TestCase):
    def test_addresses_are_tidied(self):
        self.assertEqual(romm.normalize_url("romm.local:8080/"), "http://romm.local:8080")
        self.assertEqual(romm.normalize_url("192.168.1.5:8080"), "http://192.168.1.5:8080")
        self.assertEqual(romm.normalize_url("localhost"), "http://localhost")
        self.assertEqual(romm.normalize_url("https://games.example.com/api/"), "https://games.example.com")
        self.assertEqual(romm.normalize_url("games.example.com/romm"), "https://games.example.com/romm")
        self.assertEqual(romm.normalize_url("  "), "")
        self.assertEqual(romm.server_key("HTTP://Host:80/api"), "host:80")

    def test_the_sign_in_is_a_token_a_password_or_a_cookie(self):
        self.assertEqual(romm.auth_headers("rmm_abc"), {"Authorization": "Bearer rmm_abc"})
        self.assertEqual(romm.auth_headers("plain-token"), {"Authorization": "Bearer plain-token"})
        self.assertEqual(romm.auth_headers("me:pw")["Authorization"], "Basic bWU6cHc=")
        self.assertEqual(romm.auth_headers("romm_session=abc123"), {"Cookie": "romm_session=abc123"})
        self.assertEqual(romm.auth_headers("rmm_a=="), {"Authorization": "Bearer rmm_a=="})
        self.assertEqual(romm.auth_headers(""), {})

    def test_a_missing_address_or_token_is_said_in_words(self):
        with self.assertRaisesRegex(romm.RommError, "address"):
            romm.Client("", "x")
        with self.assertRaisesRegex(romm.RommError, "token"):
            romm.Client("localhost", " ")


class ParseTests(unittest.TestCase):
    def test_a_game_is_read_with_what_the_server_knows(self):
        r = romm.parse_rom(ROMS[0])
        self.assertEqual((r.id, r.name, r.year, r.genres, r.companies, r.regions), (1, "Super Mario World", "1990", ["Platform"], ["Nintendo"], ["USA"]))
        self.assertFalse(r.multi)
        self.assertIsNone(romm.parse_rom({"id": 1}))
        self.assertIsNone(romm.parse_rom({"fs_name": "x"}))
        self.assertIsNone(romm.parse_rom("nope"))

    def test_platforms_get_distills_names(self):
        self.assertEqual(romm.system_of(romm.parse_rom(ROMS[0])), "SNES")
        self.assertEqual(romm.system_of(romm.parse_rom(ROMS[2])), "PlayStation")
        odd = romm.parse_rom(rom(9, "X", "x.bin", slug="genesis", platform="Zzz"))
        self.assertEqual(romm.system_of(odd), "Mega Drive")

    def test_files_go_in_a_folder_per_platform_and_never_outside_it(self):
        base = Path("/games")
        self.assertEqual(romm.destination(romm.parse_rom(ROMS[0]), base), base / "snes" / "Super Mario World (USA).sfc")
        self.assertEqual(romm.destination(romm.parse_rom(ROMS[2]), base), base / "psx" / "ff7" / "ff7.cue")  # the cue sheet starts a disc game
        evil = romm.parse_rom(rom(5, "E", "../../evil.sfc"))
        self.assertEqual(romm.destination(evil, base).parent, base / "snes")

    def test_covers_become_full_addresses_and_the_type_is_the_extension(self):
        e = romm.to_entry(romm.parse_rom(ROMS[0]), "http://h:1", Path("/g"))
        self.assertEqual((e.cover_url, e.kind, e.system, e.year, e.developer, e.remote_files), ("http://h:1/assets/romm/1.jpg", "SFC", "SNES", "1990", "Nintendo", 1))
        self.assertEqual(romm.to_entry(romm.parse_rom(ROMS[2]), "http://h:1", Path("/g")).remote_files, 2)


class ClientTests(ServerCase):
    def test_a_good_token_passes_and_a_bad_one_is_refused_in_words(self):
        self.assertEqual(romm.Client(self.url, TOKEN).test(), "")
        self.assertIn("didn't accept", romm.Client(self.url, "wrong").test())
        self.assertIn("Couldn't reach", romm.Client("127.0.0.1:9", TOKEN).test())

    def test_every_game_is_listed_a_page_at_a_time_and_the_token_is_sent(self):
        Handler.seen.clear()
        notes = []
        found = romm.Client(self.url, TOKEN).roms(notes.append)
        self.assertEqual([r.id for r in found], [1, 2, 3])
        self.assertTrue(notes[-1].endswith("3 of 3 games..."))
        self.assertTrue(all(auth == f"Bearer {TOKEN}" for path, auth in Handler.seen if path.startswith("/api/roms")))

    def test_fetch_library_gives_entries_with_paths(self):
        entries = romm.fetch_library(self.url, TOKEN, self.dir)
        self.assertEqual([e.remote_id for e in entries], ["1", "2", "3"])
        self.assertEqual(entries[0].path, str(self.dir / "snes" / "Super Mario World (USA).sfc"))

    def test_a_game_is_downloaded_whole_with_progress(self):
        r = romm.parse_rom(ROMS[1])
        dest = romm.destination(r, self.dir)
        seen = []
        out = romm.Client(self.url, TOKEN).download("2", r.fs_name, 1, dest, lambda d, t: seen.append((d, t)), threading.Event())
        self.assertEqual(out.read_bytes(), b"hello world")
        self.assertEqual(seen[-1], (11, 11))
        self.assertFalse(list(dest.parent.glob("*.part")))

    def test_a_game_of_several_files_is_unpacked_and_starts_from_its_cue_sheet(self):
        r = romm.parse_rom(ROMS[2])
        dest = romm.destination(r, self.dir)
        out = romm.Client(self.url, TOKEN).download("3", r.fs_name, 2, dest, lambda d, t: None, threading.Event())
        self.assertEqual(out, dest)
        self.assertTrue((dest.parent / "ff7.bin").is_file())
        self.assertFalse(list(dest.parent.glob("*.zip")))

    def test_a_cancelled_or_refused_download_leaves_nothing_behind(self):
        r = romm.parse_rom(ROMS[1])
        dest = romm.destination(r, self.dir)
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(romm.RommError, "Cancelled"):
            romm.Client(self.url, TOKEN).download("2", r.fs_name, 1, dest, lambda d, t: None, stop)
        with self.assertRaises(romm.RommError):
            romm.Client(self.url, "wrong").download("2", r.fs_name, 1, dest, lambda d, t: None, threading.Event())
        self.assertFalse(dest.exists() or list(dest.parent.glob("*.part")))

    def test_a_cover_is_fetched_and_the_token_stays_on_the_servers_own_host(self):
        c = romm.Client(self.url, TOKEN)
        self.assertTrue(c.cover(f"http://{self.url}/assets/romm/1.jpg").startswith(b"\xff\xd8"))
        sent = []
        with mock.patch("urllib.request.urlopen", lambda req, timeout=0: sent.append(dict(req.header_items())) or io.BytesIO(b"x" * 2000)):
            c.cover("http://elsewhere.example/c.jpg")
            c.cover(f"http://{self.url}/c.jpg")
        self.assertNotIn("Authorization", sent[0])
        self.assertIn("Authorization", sent[1])


class UnpackTests(unittest.TestCase):
    def test_a_zip_that_reaches_outside_its_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            z = Path(t) / "a.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("../escape.txt", "x")
            with self.assertRaisesRegex(romm.RommError, "outside"):
                romm.unpack(z, Path(t) / "out")
            self.assertFalse((Path(t) / "escape.txt").exists())

    def test_the_entry_file_is_the_playlist_the_cue_or_the_biggest(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "a.dat").write_bytes(b"x" * 50)
            (d / "b.txt").write_bytes(b"x" * 99)
            self.assertEqual(romm.pick_entry(d).name, "b.txt")
            (d / "a.cue").write_text("c")
            self.assertEqual(romm.pick_entry(d).name, "a.cue")
            (d / "a.m3u").write_text("m")
            self.assertEqual(romm.pick_entry(d).name, "a.m3u")


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = gm.GameStore(self.dir / "games.db")
        self.entries = [romm.to_entry(romm.parse_rom(d), "http://h", self.dir / "dl") for d in ROMS]

    def test_server_games_join_the_catalog_beside_local_ones(self):
        local = SimpleNamespace(name="Local (USA).sfc", system="SNES", kind="SFC", region="USA", size=1, modified=1.0, folder="", path=str(self.dir / "Local (USA).sfc"))
        self.store.sync([local], str(self.dir))
        self.assertEqual(self.store.sync_remote(self.entries, "romm:h"), (3, 0))
        games = self.store.games()
        self.assertEqual(len(games), 4)
        mario = next(g for g in games if g.name == "Super Mario World")
        self.assertEqual((mario.source, mario.remote_id, mario.remote_name, mario.cover_url, mario.year, mario.genre, mario.developer),
                         ("romm", "1", "Super Mario World (USA).sfc", "http://h/assets/romm/1.jpg", "1990", "Platform", "Nintendo"))
        self.assertTrue(mario.remote and not mario.installed)
        self.assertTrue(next(g for g in games if g.name == "Local").installed)
        self.assertEqual(next(g for g in games if g.name == "Final Fantasy VII").remote_files, 2)

    def test_syncing_again_adds_nothing_keeps_the_players_changes_and_drops_what_the_server_dropped(self):
        self.store.sync_remote(self.entries, "romm:h")
        mario = next(g for g in self.store.games() if g.name == "Super Mario World")
        self.store.update_details(mario.id, developer="My own", title="My Mario")
        self.store.set_favorite(mario.id, True)
        self.assertEqual(self.store.sync_remote(self.entries[:2], "romm:h"), (0, 1))
        again = self.store.get(mario.id)
        self.assertEqual((again.name, again.developer, again.favorite), ("My Mario", "My own", True))
        self.assertEqual(len(self.store.games()), 2)

    def test_an_empty_listing_removes_nothing_and_the_library_scan_never_touches_server_games(self):
        self.store.sync_remote(self.entries, "romm:h")
        self.assertEqual(self.store.sync_remote([], "romm:h"), (0, 0))
        self.store.sync([], str(self.dir))  # a scan of an empty folder
        self.assertEqual(len(self.store.games()), 3)

    def test_installed_means_the_file_is_there_and_a_server_game_is_never_missing(self):
        self.store.sync_remote(self.entries, "romm:h")
        g = self.store.games(query="Zelda")[0]
        self.assertEqual(gm.missing_ids([g]), set())
        Path(g.path).parent.mkdir(parents=True)
        Path(g.path).write_bytes(b"x")
        self.assertTrue(self.store.get(g.id).installed)

    def test_filters_tell_server_games_from_local_ones_and_installed_from_not(self):
        local = SimpleNamespace(name="Local.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(self.dir / "Local.sfc"))
        (self.dir / "Local.sfc").write_bytes(b"x")
        self.store.sync([local], str(self.dir))
        self.store.sync_remote(self.entries, "romm:h")
        names = lambda **f: sorted(g.name for g in self.store.games(filters=f))  # noqa: E731
        self.assertEqual(names(source="romm"), ["Final Fantasy VII", "Super Mario World", "Zelda"])
        self.assertEqual(names(source="local"), ["Local"])
        self.assertEqual(names(installed="yes"), ["Local"])
        self.assertEqual(len(names(installed="no")), 3)
        self.assertEqual(gm.clean_rules({"filters": {"source": "romm", "installed": "no", "junk": "x"}}), {"filters": {"installed": "no", "source": "romm"}})

    def test_the_installed_count_is_local_games_plus_downloaded_server_games(self):
        local = SimpleNamespace(name="Local.sfc", system="SNES", kind="SFC", region="", size=1, modified=1.0, folder="", path=str(self.dir / "Local.sfc"))
        self.store.sync([local], str(self.dir))
        self.store.sync_remote(self.entries, "romm:h")
        self.assertEqual(self.store.installed_count(), 1)
        zelda = self.store.games(query="Zelda")[0]
        Path(zelda.path).parent.mkdir(parents=True)
        Path(zelda.path).write_bytes(b"x")
        self.assertEqual(self.store.installed_count(), 2)
        self.assertEqual(sorted(g.name for g in self.store.games(filters={"installed": "yes"})), ["Local", "Zelda"])

    def test_the_list_has_a_source_column_that_sorts(self):
        self.store.sync_remote(self.entries, "romm:h")
        games = self.store.games()
        self.assertEqual(gm.column_text(games[0], "source"), "RomM")
        self.assertEqual(gm.sort_by_column(games, "source")[0].source, "romm")

    def test_a_server_cover_is_kept_as_the_games_poster_and_not_asked_for_twice(self):
        self.store.sync_remote(self.entries, "romm:h")
        g = self.store.games(query="Zelda")[0]
        img = b"\xff\xd8\xff" + b"c" * 2000
        calls = []

        class FakeClient:
            def __init__(self, *a):
                pass

            def cover(self, url):
                calls.append(url)
                return img

        with tempfile.TemporaryDirectory() as art, mock.patch("distill.romm.Client", FakeClient), mock.patch("distill.apikeys.get", lambda n: "x"), \
                mock.patch("distill.recomps.art_dir", lambda: Path(art)), mock.patch("distill.recomps.check_cover_bytes", lambda d: None):
            p = gm.fetch_cover(g)
            self.assertEqual(p.read_bytes(), img)
            self.assertEqual(gm.fetch_cover(g), p)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
