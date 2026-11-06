import io
import json
import unittest
import urllib.error
from unittest import mock

from distill import iconfinder as ic
from distill.iconfinder import IconHit, normalize, page_title, parse_results, score

SEARCH_PAGE = """
<a href="detail.php?id=14113"><img src="images/100/14113.png" title="Fire Emblem&trade;: Fortune&#8217;s Weave"></a>
<a href="https://www.switchiconshowdown.com/detail.php?id=14112"><img src="images/100/14112.png" alt="Splatoon&trade; Raiders"></a>
<a href="detail.php?id=14113">again</a>
<a href="detail.php?id=500">Plain Link Title</a>
"""


class IconFinderTests(unittest.TestCase):
    def test_parse_results_reads_titles_once_each(self):
        got = parse_results(SEARCH_PAGE)
        self.assertEqual([i for i, _t in got], [14113, 14112, 500])
        self.assertEqual(got[2][1], "Plain Link Title")
        self.assertIn("Fortune", got[0][1])

    def test_normalize_drops_marks_and_punctuation(self):
        self.assertEqual(normalize("Fire Emblem™: Fortune’s Weave"), normalize("fire emblem fortune s weave"))
        self.assertEqual(normalize("Tom &amp; Jerry"), "tom and jerry")

    def test_scores_prefer_the_right_game(self):
        self.assertEqual(score("Splatoon Raiders", "Splatoon™ Raiders"), 1.0)
        self.assertGreater(score("Hades", "Hades"), score("Hades", "Hades II"))
        self.assertLess(score("Hades", "Hades II"), 0.7)
        self.assertEqual(score("Mario Kart World", "Fire Emblem"), 0.0)

    def test_page_title_and_url(self):
        self.assertEqual(page_title("<title>Switch Icon Showdown! Star Fox</title>"), "Star Fox")
        self.assertEqual(IconHit(14106, "Star Fox").url, "https://www.switchiconshowdown.com/images/14106.png")

    def test_title_on_the_anchor_itself_is_read(self):
        page = '<a href="detail.php?id=7&q=x" title="Star Fox"><img src="a.png"></a>'
        self.assertEqual(parse_results(page), [(7, "Star Fox")])

    def test_name_variants_strip_tags_and_punctuation_typed_name_first(self):
        got = ic.name_variants(["My Title", "Zelda: Link [0100ABCD][v0] (Update)"])
        self.assertEqual(got[0], "My Title")
        self.assertIn("Zelda: Link", got)
        self.assertIn("Zelda Link", got)
        self.assertEqual(len(got), len({g.casefold() for g in got}))

    def test_sgdb_icon_query_never_asks_for_jpeg(self):
        self.assertNotIn("jpeg", ic.SGDB_ICON_QUERY)
        self.assertIn("image/png", ic.SGDB_ICON_QUERY)

    def test_sgdb_400_message_carries_the_servers_reason(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(json.dumps({"errors": ["mimes invalid"]}).encode()))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ic.IconLookupError) as cm:
                ic._sgdb("/x", "K", 5)
        self.assertIn("400", str(cm.exception))
        self.assertIn("mimes invalid", str(cm.exception))

    def test_untitled_results_are_named_by_their_detail_page(self):
        def fake_get(url, timeout, who=""):
            if "search.php" in url:
                return b'<a href="detail.php?id=9"><img src="i.png"></a>'
            return b"<title>Switch Icon Showdown! Star Fox</title>"
        with mock.patch.object(ic, "_get", fake_get):
            hits = ic.search("Star Fox")
        self.assertEqual([(h.icon_id, h.title) for h in hits], [(9, "Star Fox")])

    def test_sgdb_is_used_when_showdown_has_nothing_and_picks_a_square_png(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        seen = []

        def fake_sgdb(path, key, timeout):
            seen.append(path)
            if path.startswith("/search/"):
                return [{"id": 5, "name": "Star Fox", "verified": True}]
            return [{"id": 1, "url": "https://cdn/wide.png", "width": 512, "height": 256, "score": 9},
                    {"id": 2, "url": "https://cdn/sq.png", "width": 256, "height": 256, "score": 1}]

        def fake_get(url, timeout, who=""):
            if "switchiconshowdown" in url:
                return b"<html></html>"
            return png

        with mock.patch.object(ic, "_get", fake_get), mock.patch.object(ic, "_sgdb", fake_sgdb), \
                mock.patch.object(ic, "sgdb_key", lambda: "K"):
            data, hit = ic.find_icon(["Star Fox"])
        self.assertEqual((hit.source, hit.icon_id), (ic.SOURCE_SGDB, 2))
        self.assertTrue(any("mimes=image/png" in p and "jpeg" not in p for p in seen))

    def test_a_sgdb_400_is_reported_together_with_showdowns_miss(self):
        def fake_sgdb(path, key, timeout):
            raise ic.IconLookupError("SteamGridDB answered 400.")
        with mock.patch.object(ic, "_get", lambda *a, **k: b"<html></html>"), mock.patch.object(ic, "_sgdb", fake_sgdb), \
                mock.patch.object(ic, "sgdb_key", lambda: "K"):
            with self.assertRaises(ic.IconLookupError) as cm:
                ic.find_icon(["Star Fox"])
        self.assertIn("Switch Icon Showdown had nothing alike", str(cm.exception))
        self.assertIn("400", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
