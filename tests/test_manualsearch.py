"""Finding and fetching a game's manual online (distill/manualsearch.py): the search and ranking with the network replaced, the download against a
small web server on this computer."""
from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from tests import helpers  # noqa: F401
from distill import manualsearch as ms

SNES = ms.platform_words("SNES", "Super Nintendo Entertainment System")


class NameTests(unittest.TestCase):
    def test_a_file_name_becomes_what_a_person_would_search_for(self):
        self.assertEqual(ms.clean_name("Super Metroid (USA).sfc"), "Super Metroid")
        self.assertEqual(ms.clean_name("Final Fantasy VII (Disc 1) [!]"), "Final Fantasy VII")
        self.assertEqual(ms.clean_name("Sonic & Knuckles"), "Sonic & Knuckles")
        self.assertEqual(ms.clean_name(""), "")

    def test_a_platform_is_known_by_several_names(self):
        self.assertIn("snes", SNES)
        self.assertIn("super nintendo", SNES)
        self.assertIn("super nintendo entertainment system", SNES)  # (LaunchBox's name for it, too)
        self.assertEqual(ms.platform_words("Odd System", ""), ["odd system"])
        self.assertEqual(ms.platform_words("Unknown system", ""), [])


class ScoreTests(unittest.TestCase):
    def s(self, title, subjects="", game="Super Metroid", terms=SNES):
        return ms.score(game, terms, title, subjects)

    def test_the_right_manual_scores_high(self):
        self.assertGreater(self.s("Super Metroid (Super Nintendo) Instruction Booklet"), 0.9)
        self.assertGreater(self.s("Super Metroid manual - SNES"), 0.9)

    def test_a_region_or_subtitle_does_not_hurt(self):
        self.assertGreater(self.s("Super Metroid (Europe) - Manual"), 0.7)
        self.assertGreater(ms.score("The Legend of Zelda - A Link to the Past", SNES, "Legend of Zelda, The - A Link to the Past - Manual (SNES)"), 0.9)

    def test_another_game_that_shares_a_word_is_not_offered(self):
        self.assertLess(self.s("Super Mario World manual"), ms.MIN_SCORE)
        self.assertLess(self.s("Metroid Prime Manual"), ms.MIN_SCORE)
        self.assertLess(self.s("Metroid"), ms.MIN_SCORE)

    def test_a_guide_is_not_a_manual(self):
        self.assertLess(self.s("Super Metroid Strategy Guide"), ms.MIN_SCORE)
        self.assertLess(self.s("Super Metroid", subjects="walkthrough; hint book"), self.s("Super Metroid", subjects="instruction booklet"))

    def test_the_platform_and_the_word_manual_count_for_it(self):
        bare, full = self.s("Super Metroid"), self.s("Super Metroid manual SNES")
        self.assertGreater(full, bare)

    def test_nothing_to_compare_is_nothing(self):
        self.assertEqual(ms.score("", SNES, "Anything"), 0.0)
        self.assertEqual(ms.score("Super Metroid", SNES, "Manual"), 0.0)  # (only noise words: nothing of the game in it)

    def test_the_score_stays_between_zero_and_one(self):
        for title in ("Super Metroid manual SNES super nintendo instruction booklet", "x", "Super Metroid " * 20):
            self.assertTrue(0.0 <= self.s(title) <= 1.0)


class ParseTests(unittest.TestCase):
    def test_items_are_read_from_a_search_reply(self):
        reply = json.dumps({"response": {"numFound": 2, "docs": [{"identifier": "a", "title": "A", "subject": ["x", "y"]}, {"identifier": "b"}]}})
        self.assertEqual(ms.parse_search(reply), [{"identifier": "a", "title": "A", "subject": "x y"}, {"identifier": "b", "title": "b", "subject": ""}])

    def test_anything_unreadable_is_no_items(self):
        for text in ("", "<html>", "[]", '{"response": 5}', '{"response": {"docs": "x"}}', '{"response": {"docs": [1, {"title": "no id"}]}}'):
            self.assertEqual(ms.parse_search(text), [], text)

    def test_the_best_pdf_in_an_item(self):
        files = [{"name": "cover.jpg", "size": "10"}, {"name": "game_text.pdf", "size": "5", "format": "Text PDF"},
                 {"name": "scan.pdf", "size": "9000000", "format": "Image Container PDF", "source": "derivative"},
                 {"name": "manual.pdf", "size": "2000000", "format": "Text PDF", "source": "original"}]
        self.assertEqual(ms.pick_pdf(files), {"name": "manual.pdf", "size": 2000000})

    def test_a_text_pdf_beats_a_scan_and_a_smaller_one_beats_a_bigger(self):
        files = [{"name": "big.pdf", "size": "9000000", "format": "Text PDF"}, {"name": "small.pdf", "size": "1000", "format": "Text PDF"},
                 {"name": "scan.pdf", "size": "10", "format": "Image Container PDF"}]
        self.assertEqual(ms.pick_pdf(files)["name"], "small.pdf")

    def test_a_pdf_that_is_too_big_or_missing_is_not_offered(self):
        self.assertIsNone(ms.pick_pdf([{"name": "huge.pdf", "size": str(ms.MAX_BYTES + 1)}]))
        self.assertIsNone(ms.pick_pdf([{"name": "x.txt", "size": "1"}]))
        self.assertIsNone(ms.pick_pdf([]))
        self.assertIsNone(ms.pick_pdf(None))  # type: ignore[arg-type]
        self.assertEqual(ms.pick_pdf([{"name": "odd.pdf", "size": "not a number"}]), {"name": "odd.pdf", "size": 0})

    def test_the_query_asks_for_manuals_about_the_game_on_the_platform(self):
        q = ms.build_query("Super Metroid", ["snes", "super nintendo"])
        self.assertIn('"Super Metroid"', q)
        self.assertIn("mediatype:texts", q)
        self.assertIn('"snes"', q)
        self.assertNotIn("snes", ms.build_query("Super Metroid"))


class SearchTests(unittest.TestCase):
    def reply(self, *items):
        return json.dumps({"response": {"docs": [{"identifier": i, "title": t, "subject": s} for i, t, s in items]}}).encode()

    def web(self, searches, files, packs=None):
        """A `get` that answers searches (a list of replies, in turn), the search for manual packs (`packs`, else none) and item listings (by
        identifier), and remembers what was asked."""
        asked: list[str] = []
        turns = iter(searches)

        def get(url: str) -> bytes:
            asked.append(url)
            if url.startswith(ms.SEARCH_URL) and "manual_packs" in urllib.parse.unquote_plus(url):
                return packs if packs is not None else json.dumps({"response": {"docs": []}}).encode()
            if url.startswith(ms.SEARCH_URL):
                return next(turns)
            ident = urllib.parse.unquote(url[len(ms.META_URL):])
            if ident not in files:
                raise urllib.error.URLError("no such item")
            return json.dumps({"files": files[ident]}).encode()

        get.asked = asked  # type: ignore[attr-defined]
        return get

    PDF = [{"name": "Super Metroid manual.pdf", "size": "3000000", "format": "Text PDF", "source": "original"}]

    def test_results_are_ranked_and_carry_where_to_get_them(self):
        get = self.web([self.reply(("sm-manual", "Super Metroid (Super Nintendo) Manual", "manual"), ("smw", "Super Mario World manual", ""),
                                   ("sm-guide", "Super Metroid Strategy Guide", "guide"))] * 2,
                       {"sm-manual": self.PDF, "smw": self.PDF, "sm-guide": self.PDF})
        found = ms.search("Super Metroid (USA).sfc", "SNES", "Super Nintendo Entertainment System", get)
        self.assertEqual([m.identifier for m in found], ["sm-manual"])  # (another game and a guide are left out)
        m = found[0]
        self.assertEqual((m.source, m.size, m.size_text()), ("Internet Archive", 3000000, "2.9 MB"))
        self.assertEqual(m.url, f"{ms.DOWNLOAD_URL}sm-manual/Super%20Metroid%20manual.pdf")
        self.assertEqual(m.page, ms.DETAILS_URL + "sm-manual")
        self.assertGreater(m.score, 0.9)

    def test_the_wide_search_is_only_made_when_the_narrow_one_finds_little(self):
        many = self.reply(*[(f"i{n}", f"Super Metroid manual {n}", "") for n in range(8)])
        get = self.web([many], {f"i{n}": self.PDF for n in range(8)})
        ms.search("Super Metroid", "SNES", "", get)
        self.assertEqual(len([u for u in get.asked if u.startswith(ms.SEARCH_URL) and "manual_packs" not in urllib.parse.unquote_plus(u)]), 1)
        few = self.web([self.reply(("a", "Super Metroid manual", "")), self.reply(("b", "Super Metroid instruction booklet", ""))], {"a": self.PDF, "b": self.PDF})
        found = ms.search("Super Metroid", "SNES", "", few)
        self.assertEqual(len([u for u in few.asked if u.startswith(ms.SEARCH_URL) and "manual_packs" not in urllib.parse.unquote_plus(u)]), 2)
        self.assertEqual({m.identifier for m in found}, {"a", "b"})  # (both searches' items are ranked together)

    def test_the_platform_is_in_the_narrow_search_but_not_the_wide_one(self):
        get = self.web([self.reply(), self.reply()], {})
        ms.search("Super Metroid", "SNES", "", get)
        first, second = [urllib.parse.unquote_plus(u) for u in get.asked if u.startswith(ms.SEARCH_URL) and "manual_packs" not in urllib.parse.unquote_plus(u)]
        self.assertIn("snes", first)
        self.assertNotIn("snes", second)

    def test_an_item_with_no_pdf_or_that_cannot_be_looked_into_is_left_out(self):
        get = self.web([self.reply(("a", "Super Metroid manual", ""), ("b", "Super Metroid manual two", ""), ("c", "Super Metroid manual three", ""))] * 2,
                       {"a": [{"name": "scan.jpg", "size": "5"}], "b": self.PDF})  # (c has no listing at all)
        self.assertEqual([m.identifier for m in ms.search("Super Metroid", "SNES", "", get)], ["b"])

    def test_at_most_a_handful_are_offered(self):
        get = self.web([self.reply(*[(f"i{n}", f"Super Metroid manual {n}", "") for n in range(20)])], {f"i{n}": self.PDF for n in range(20)})
        self.assertLessEqual(len(ms.search("Super Metroid", "SNES", "", get)), ms.MAX_RESULTS)

    def test_no_connection_is_said_in_words(self):
        def down(url: str) -> bytes:
            raise urllib.error.URLError("offline")

        with self.assertRaises(ms.ManualError) as ctx:
            ms.search("Super Metroid", "SNES", "", down)
        self.assertIn("Internet Archive", str(ctx.exception))

    def test_nothing_to_search_for_is_nothing(self):
        self.assertEqual(ms.search("", "SNES", "", lambda u: b""), [])

    def test_nothing_found_is_an_empty_list(self):
        self.assertEqual(ms.search("Obscure Game", "SNES", "", self.web([self.reply(), self.reply()], {})), [])


# A few of the real file names in the Archive's SNES manual pack (SNESManuals), with sizes as the Archive lists them.
SNES_PACK_FILES = [{"name": n, "size": z} for n, z in [
    ("Super Metroid (Japan, USA).pdf", "2400000"), ("Super Mario World (USA).pdf", "1700000"),
    ("Super Mario World 2 - Yoshi's Island (USA) (Rev 1).pdf", "2200000"), ("Super Mario All-Stars + Super Mario World (USA).pdf", "2200000"),
    ("Legend of Zelda, The - A Link to the Past (USA).pdf", "2600000"), ("Donkey Kong Country 2 - Diddy's Kong Quest (USA) (Rev 1).pdf", "2000000"),
    ("Donkey Kong Country (USA) (Rev 2).pdf", "3700000"), ("Street Fighter II (USA).pdf", "2500000"), ("Street Fighter II Turbo (USA).pdf", "3100000"),
    ("Super Street Fighter II (USA).pdf", "1500000"), ("Terranigma (Europe).pdf", "2300000"), ("Super Metroid (Japan, USA)_jp2.zip", "15100000"),
    ("Kirby's Dream Land 3 (USA).pdf", "6600000"), ("Huge Thing (USA).pdf", str(ms.MAX_BYTES + 1))]]


class PackTests(unittest.TestCase):
    def match(self, game, files=SNES_PACK_FILES, region=""):
        return ms.match_pack(game, SNES, files, "Nintendo Super NES/ SNES Manuals", "SNESManuals", region)

    def test_the_name_as_a_rom_set_writes_it_is_read_as_a_person_would(self):
        self.assertEqual(ms.nointro_title("Legend of Zelda, The - A Link to the Past (USA)"), "The Legend of Zelda - A Link to the Past")
        self.assertEqual(ms.nointro_title("Donkey Kong Country 2 - Diddy's Kong Quest (USA) (Rev 1)"), "Donkey Kong Country 2 - Diddy's Kong Quest")
        self.assertEqual(ms.nointro_title("Chessmaster, The (USA)"), "The Chessmaster")
        self.assertEqual(ms.nointro_title("Super Metroid"), "Super Metroid")

    def test_the_region_tag_is_read(self):
        self.assertEqual(ms.region_of("Super Metroid (Japan, USA)"), "japan, usa")
        self.assertEqual(ms.region_of("Terranigma (Europe)"), "europe")
        self.assertEqual(ms.region_of("Donkey Kong Country (USA) (Rev 2)"), "usa")
        self.assertEqual(ms.region_of("Mario Paint"), "")
        self.assertEqual(ms.region_of("Thing (Proto)"), "")  # (a prototype tag is not a region)

    def test_a_game_is_found_by_its_own_file_name(self):
        (m, *_rest) = self.match("Super Metroid (USA).sfc")
        self.assertEqual(m.title, "Super Metroid (Japan, USA)")
        self.assertEqual(m.url, f"{ms.DOWNLOAD_URL}SNESManuals/Super%20Metroid%20%28Japan%2C%20USA%29.pdf")
        self.assertEqual((m.size, m.source, m.page), (2400000, "Nintendo Super NES/ SNES Manuals", ms.DETAILS_URL + "SNESManuals"))
        self.assertGreater(m.score, 0.9)

    def test_a_name_with_the_article_moved_is_still_found(self):
        got = self.match("The Legend of Zelda - A Link to the Past (USA).sfc")
        self.assertEqual(got[0].title, "Legend of Zelda, The - A Link to the Past (USA)")
        self.assertEqual(self.match("Legend of Zelda, The - A Link to the Past (USA)")[0].title, got[0].title)

    def test_an_exact_name_beats_the_sequels_that_contain_it(self):
        got = self.match("Super Mario World")
        self.assertEqual([m.title for m in got], ["Super Mario World (USA)"])  # (not Yoshi's Island, not All-Stars)
        got = self.match("Street Fighter II")
        self.assertEqual(got[0].title, "Street Fighter II (USA)")
        self.assertNotIn("Super Street Fighter II (USA)", [m.title for m in got])

    def test_a_revision_is_found_when_it_is_the_only_one(self):
        self.assertEqual(self.match("Donkey Kong Country 2 - Diddy's Kong Quest")[0].title, "Donkey Kong Country 2 - Diddy's Kong Quest (USA) (Rev 1)")

    def test_a_game_the_pack_does_not_have_finds_nothing(self):
        self.assertEqual(self.match("Chrono Trigger"), [])
        self.assertEqual(self.match("Super Mario"), [])  # (not a different game that shares words with it)

    def test_two_titles_are_the_same_game_when_their_main_titles_are(self):
        self.assertTrue(ms.same_game("Donkey Kong Country 2", "Donkey Kong Country 2 - Diddy's Kong Quest"))
        self.assertTrue(ms.same_game("Donkey Kong Country 2: Diddy's Kong Quest", "Donkey Kong Country 2 - Diddy's Kong Quest"))
        self.assertTrue(ms.same_game("Super Metroid", "super metroid"))
        self.assertFalse(ms.same_game("Super Mario", "Super Mario World"))
        self.assertFalse(ms.same_game("Street Fighter II", "Street Fighter II Turbo"))
        self.assertFalse(ms.same_game("Zelda - A Link to the Past", "Zelda - Link's Awakening"))

    def test_the_games_own_region_decides_between_versions(self):
        files = [{"name": "Super Foo (USA).pdf", "size": "1"}, {"name": "Super Foo (Europe).pdf", "size": "1"}]
        self.assertEqual(self.match("Super Foo", files, "europe")[0].title, "Super Foo (Europe)")
        self.assertEqual(self.match("Super Foo", files, "usa")[0].title, "Super Foo (USA)")

    def test_only_pdfs_of_a_sensible_size_in_the_top_folder_are_offered(self):
        self.assertEqual(self.match("Huge Thing"), [])
        self.assertEqual(self.match("Super Metroid", [{"name": "Super Metroid (Japan, USA)_jp2.zip", "size": "5"}, {"name": "sub/Super Metroid.pdf", "size": "5"}]), [])
        self.assertEqual(self.match("Super Metroid", None), [])  # type: ignore[arg-type]

    def test_a_search_asks_the_packs_first_and_lists_what_it_finds_before_other_items(self):
        pack_search = json.dumps({"response": {"docs": [{"identifier": "SNESManuals", "title": "Nintendo Super NES/ SNES Manuals", "subject": ["Manuals"]}]}}).encode()
        item_search = json.dumps({"response": {"docs": [{"identifier": "sm-scan", "title": "Super Metroid manual", "subject": "manual"}]}}).encode()
        asked = []

        def get(url: str) -> bytes:
            asked.append(urllib.parse.unquote_plus(url))
            if url.startswith(ms.SEARCH_URL):
                return pack_search if "manual_packs" in urllib.parse.unquote_plus(url) else item_search
            ident = urllib.parse.unquote(url[len(ms.META_URL):])
            files = SNES_PACK_FILES if ident == "SNESManuals" else [{"name": "scan.pdf", "size": "9", "format": "Text PDF", "source": "original"}]
            return json.dumps({"files": files}).encode()

        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock
            with mock.patch.object(ms, "temp_dir", return_value=Path(tmp)):
                found = ms.search("Super Metroid (USA).sfc", "SNES", "Super Nintendo Entertainment System", get)
        self.assertIn("manual_packs", asked[0])
        self.assertIn('"snes"', asked[0])
        self.assertEqual([m.identifier for m in found], ["SNESManuals", "sm-scan"])
        self.assertEqual(found[0].title, "Super Metroid (Japan, USA)")

    def test_a_packs_file_list_is_kept_for_a_week(self):
        calls = []

        def get(url: str) -> bytes:
            calls.append(url)
            return json.dumps({"files": [{"name": "A (USA).pdf", "size": "5", "format": "x"}, {"name": "cover.jpg", "size": "1"}]}).encode()

        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock
            with mock.patch.object(ms, "temp_dir", return_value=Path(tmp)):
                first = ms._pack_listing("SNESManuals", get)
                again = ms._pack_listing("SNESManuals", get)
        self.assertEqual(first, [{"name": "A (USA).pdf", "size": "5"}])  # (only the PDFs are kept)
        self.assertEqual(again, first)
        self.assertEqual(len(calls), 1)

    def test_a_pack_that_cannot_be_read_is_skipped_and_the_rest_carry_on(self):
        packs = json.dumps({"response": {"docs": [{"identifier": "broken", "title": "B"}, {"identifier": "good", "title": "G"}]}}).encode()

        def get(url: str) -> bytes:
            if url.startswith(ms.SEARCH_URL):
                return packs
            if "broken" in url:
                raise urllib.error.URLError("gone")
            return json.dumps({"files": [{"name": "Super Metroid (USA).pdf", "size": "5"}]}).encode()

        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock
            with mock.patch.object(ms, "temp_dir", return_value=Path(tmp)):
                found = ms.search_packs("Super Metroid", SNES, get)
        self.assertEqual([m.identifier for m in found], ["good"])

    def test_without_platform_names_the_packs_are_not_searched(self):
        self.assertEqual(ms.search_packs("Super Metroid", [], lambda u: b""), [])


class Handler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self):  # noqa: N802
        body = self.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 100_000):
                self.wfile.write(body[i:i + 100_000])
        except ConnectionError:  # (the test stopped the download part way: that is the point)
            pass

    def log_message(self, *args):  # silence
        pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        Handler.files = {"/ok.pdf": b"%PDF-1.4\n" + bytes(700_000), "/html.pdf": b"<html>not a pdf</html>", "/empty.pdf": b""}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "out" / "manual.pdf"

    def test_a_pdf_is_downloaded_with_progress(self):
        seen: list[tuple[int, int]] = []
        got = ms.download(f"{self.base}/ok.pdf", self.dest, lambda d, t: seen.append((d, t)))
        self.assertEqual(got, self.dest)
        self.assertEqual(self.dest.stat().st_size, 700_009)
        self.assertEqual(seen[-1], (700_009, 700_009))
        self.assertTrue(all(b[0] >= a[0] for a, b in zip(seen, seen[1:])))  # (progress only goes forward)
        self.assertFalse(self.dest.with_name("manual.pdf.part").exists())

    def test_something_that_is_not_a_pdf_is_not_kept(self):
        with self.assertRaises(ms.ManualError) as ctx:
            ms.download(f"{self.base}/html.pdf", self.dest)
        self.assertIn("wasn't a PDF", str(ctx.exception))
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dest.parent.glob("*")), [])

    def test_an_empty_download_is_refused(self):
        with self.assertRaises(ms.ManualError):
            ms.download(f"{self.base}/empty.pdf", self.dest)
        self.assertFalse(self.dest.exists())

    def test_one_that_is_too_big_is_refused_before_it_is_fetched(self):
        calls = []
        with self.assertRaises(ms.ManualError) as ctx:
            ms.download(f"{self.base}/ok.pdf", self.dest, lambda d, t: calls.append(d), limit=1000)
        self.assertIn("too big", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_cancelling_stops_it_and_leaves_nothing(self):
        n = [0]

        def cancelled():
            n[0] += 1
            return n[0] > 2

        with self.assertRaises(ms.ManualError) as ctx:
            ms.download(f"{self.base}/ok.pdf", self.dest, cancelled=cancelled)
        self.assertEqual(str(ctx.exception), "Cancelled.")
        self.assertEqual(list(self.dest.parent.glob("*")), [])

    def test_a_missing_file_or_server_is_said_in_words(self):
        for url in (f"{self.base}/nowhere.pdf", "http://127.0.0.1:9/x.pdf"):
            with self.assertRaises(ms.ManualError) as ctx:
                ms.download(url, self.dest)
            self.assertIn("Couldn't download", str(ctx.exception))

    def test_old_downloads_are_swept_away_and_new_ones_stay(self):
        import os
        import time

        old, new = ms.temp_dir() / "old-test.pdf", ms.temp_dir() / "new-test.pdf"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        os.utime(old, (time.time() - 3 * 86400,) * 2)
        self.addCleanup(lambda: [f.unlink(missing_ok=True) for f in (old, new)])
        ms.sweep_temp()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
