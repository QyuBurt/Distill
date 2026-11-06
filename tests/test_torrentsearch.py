"""Torrent search core: the parsers, the merge, magnets and .torrent hashing. No network, no Qt."""
from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import torrentsearch as ts

H1, H2 = "a" * 40, "b" * 40


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"


class ParserTests(unittest.TestCase):
    def test_knaben_hits_need_a_real_hash_and_get_clean_names(self):
        data = {"hits": [
            {"title": "Game\x00 A\n  (v1)", "hash": H1.upper(), "seeders": 30, "peers": 4, "bytes": 99,
             "date": "2024-05-06T07:08:09+00:00", "magnetUrl": "magnet:?xt=urn:btih:" + H2},
            {"title": "From magnet", "magnetUrl": f"magnet:?xt=urn:btih:{H2}&tr=udp://x"},
            {"title": "Proxy link only", "link": "https://knaben.eu/live/dl/rutracker/?id=1"},
            {"title": "Zeros", "hash": "0" * 40},
            "junk",
        ]}
        got = ts.parse_knaben(data)
        self.assertEqual([(r.name, r.infohash) for r in got], [("Game A (v1)", H1), ("From magnet", H2)])
        self.assertEqual((got[0].seeders, got[0].leechers, got[0].size, got[0].sources), (30, 4, 99, ["Knaben"]))
        self.assertGreater(got[0].added, 0)
        self.assertEqual(got[0].magnet, "")  # never a link a source sent: the magnet is built here from the hash
        self.assertEqual(ts.parse_knaben([1, 2]), [])
        self.assertEqual(ts.parse_knaben({"hits": "no"}), [])

    def test_text_is_cleaned_and_cut(self):
        self.assertEqual(ts._text("a\x00b\tc\n\nd  e"), "a b c d e")
        self.assertEqual(len(ts._text("x" * 1000)), ts.MAX_NAME)
        self.assertEqual(ts._text(None), "")

    def test_torrents_csv_accepts_a_list_or_a_wrapper(self):
        row = {"infohash": H2, "name": "Game B", "size_bytes": 5, "seeders": 7, "leechers": 1, "created_unix": 9}
        self.assertEqual(ts.parse_torrents_csv([row])[0].infohash, H2)
        self.assertEqual(ts.parse_torrents_csv({"torrents": [row]})[0].seeders, 7)
        self.assertEqual(ts.parse_torrents_csv({"torrents": [{"name": "no hash"}]}), [])

    def test_prowlarr_magnet_hash_and_torrent_only_rows(self):
        data = [
            {"title": "With magnet", "protocol": "torrent", "indexer": "IndexerX", "seeders": 5, "leechers": 2,
             "size": 10, "magnetUrl": f"magnet:?xt=urn:btih:{H1}&dn=x", "publishDate": "2024-01-02T03:04:05Z"},
            {"title": "Hash only", "protocol": "torrent", "indexer": "IndexerY", "infoHash": H2, "seeders": 1},
            {"title": "File only", "protocol": "torrent", "indexer": "IndexerZ",
             "downloadUrl": "http://prowlarr/9/download?link=abc", "seeders": 3},
            {"title": "Usenet", "protocol": "usenet", "downloadUrl": "http://x/nzb"},
            {"title": "Nothing to add"},
        ]
        got = ts.parse_prowlarr(data)
        self.assertEqual([r.name for r in got], ["With magnet", "Hash only", "File only", "Nothing to add"][:3])
        self.assertEqual(got[0].infohash, H1)
        self.assertGreater(got[0].added, 0)
        self.assertEqual(got[1].infohash, H2)
        self.assertEqual((got[2].infohash, got[2].torrent_url), ("", "http://prowlarr/9/download?link=abc"))
        self.assertEqual(got[0].sources, ["IndexerX"])


class MergeTests(unittest.TestCase):
    def test_same_hash_joins_sources_and_takes_the_best_counts(self):
        a = ts.SearchResult("Game", H1, 100, 5, 1, sources=["Nyaa"])
        b = ts.SearchResult("Game", H1, 0, 9, 0, sources=["Torrents-CSV"], added=42)
        c = ts.SearchResult("Other", H2, 1, 50, 2, sources=["Prowlarr"])
        out = ts.merge([a, b, c])
        self.assertEqual([r.infohash for r in out], [H2, H1])  # most seeders first
        self.assertEqual((out[1].seeders, out[1].size, out[1].added), (9, 100, 42))
        self.assertEqual(out[1].sources, ["Nyaa", "Torrents-CSV"])


class MagnetTests(unittest.TestCase):
    def test_build_magnet_has_hash_name_and_trackers(self):
        with mock.patch.object(ts, "trackers", lambda: ["udp://t.example:80/announce"]):
            m = ts.build_magnet(H1, "A B&C")
        self.assertIn(f"xt=urn:btih:{H1}", m)
        self.assertIn("dn=A%20B%26C", m)
        self.assertIn("tr=udp%3A%2F%2Ft.example%3A80%2Fannounce", m)
        self.assertNotIn("tr=", ts.build_magnet(H1, "x", extra_trackers=False))

    def test_tracker_list_parsing_and_fallback(self):
        text = "udp://a:1/announce\n\nhttp://b/announce\nnot a tracker\nudp://a:1/announce\n"
        self.assertEqual(ts.parse_trackers(text), ["udp://a:1/announce", "http://b/announce"])
        with mock.patch.object(ts, "TRACKERS_FILE", ts.APP_DIR / "definitely-not-here.txt"):
            self.assertEqual(ts.trackers(), list(ts.BUNDLED_TRACKERS[:ts.MAGNET_TRACKERS]))

    def test_torrent_infohash_is_sha1_of_the_info_dictionary(self):
        info = {b"name": b"x", b"length": 5, b"piece length": 16384, b"pieces": b"p" * 20}
        data = bencode({b"announce": b"http://t/a", b"info": info, b"zzz": [1, b"two", {b"k": 3}]})
        self.assertEqual(ts.torrent_infohash(data), hashlib.sha1(bencode(info)).hexdigest())
        with self.assertRaises(ts.SearchError):
            ts.torrent_infohash(b"<html>not a torrent</html>")

    def test_to_magnet_for_each_kind_of_result(self):
        own = ts.SearchResult("n", magnet=f"magnet:?xt=urn:btih:{H1}&tr=udp://own")
        self.assertEqual(ts.to_magnet(own), own.magnet)
        self.assertIn(H2, ts.to_magnet(ts.SearchResult("n", H2)))
        info = {b"name": b"x", b"length": 1, b"piece length": 16384, b"pieces": b"p" * 20}
        blob = bencode({b"info": info})

        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return blob

        with mock.patch.object(ts, "http_open", lambda *a, **k: Reply()):
            m = ts.to_magnet(ts.SearchResult("file only", torrent_url="http://x/dl"))
        self.assertIn(hashlib.sha1(bencode(info)).hexdigest(), m)
        with self.assertRaises(ts.SearchError):
            ts.to_magnet(ts.SearchResult("nothing"))


class RequestTests(unittest.TestCase):
    def test_knaben_is_asked_with_a_json_post_and_falls_back_to_the_next_address(self):
        import json
        seen = []

        class Reply:
            def __init__(self, raw): self.raw = raw
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return self.raw

        class Opener:
            def open(self, req, timeout=0):
                seen.append((req.full_url, req.get_method(), json.loads(req.data), req.get_header("Content-type")))
                if len(seen) == 1:
                    raise OSError("down")
                return Reply(json.dumps({"hits": [{"title": "G", "hash": H1, "seeders": 3}]}).encode())

        with mock.patch.object(ts, "opener_for", lambda url: Opener()):
            got = ts.search_knaben("some game")
        self.assertEqual([r.infohash for r in got], [H1])
        self.assertEqual([u for u, *_ in seen], list(ts.KNABEN_URLS))
        self.assertEqual(seen[0][1:], ("POST", seen[0][2], "application/json"))
        self.assertEqual((seen[0][2]["query"], seen[0][2]["order_by"]), ("some game", "seeders"))

    def test_an_oversized_or_unreadable_reply_is_an_error_not_a_crash(self):
        class Reply:
            def __init__(self, raw): self.raw = raw
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return self.raw[:n] if n >= 0 else self.raw

        class Opener:
            def __init__(self, raw): self.raw = raw
            def open(self, req, timeout=0): return Reply(self.raw)

        with mock.patch.object(ts, "opener_for", lambda url: Opener(b"<html>")):
            with self.assertRaises(ts.SearchError):
                ts._json("https://x/")
        with mock.patch.object(ts, "opener_for", lambda url: Opener(b" " * (ts.MAX_REPLY + 5))):
            with self.assertRaises(ts.SearchError):
                ts._json("https://x/")


class SearchTests(unittest.TestCase):
    def test_a_failing_source_becomes_a_note_and_the_others_still_answer(self):
        good = [ts.SearchResult("Game", H1, seeders=4, sources=["Prowlarr"])]

        def boom(_q):
            raise ts.SearchError("Network error: down")

        with mock.patch.object(ts, "refresh_trackers", lambda: None), \
                mock.patch.object(ts, "search_torrents_csv", boom), \
                mock.patch.object(ts, "search_knaben", lambda q: []), \
                mock.patch.object(ts, "search_prowlarr", lambda q, u, k: good), \
                mock.patch.object(ts, "prowlarr_config", lambda: ("http://p:9696", "key")):
            results, notes = ts.search("  game  ")
        self.assertEqual([r.infohash for r in results], [H1])
        self.assertEqual(notes, ["Torrents-CSV: Network error: down"])

    def test_prowlarr_is_only_asked_when_configured(self):
        calls = []
        with mock.patch.object(ts, "refresh_trackers", lambda: None), \
                mock.patch.object(ts, "search_torrents_csv", lambda q: []), \
                mock.patch.object(ts, "search_knaben", lambda q: []), \
                mock.patch.object(ts, "search_prowlarr", lambda q, u, k: calls.append((u, k)) or []), \
                mock.patch.object(ts, "prowlarr_config", lambda: ("http://p:9696", "key")):
            ts.search("game")
        self.assertEqual(calls, [("http://p:9696", "key")])

    def test_every_built_in_source_is_asked(self):
        with mock.patch.object(ts, "refresh_trackers", lambda: None), \
                mock.patch.object(ts, "search_knaben", lambda q: [ts.SearchResult("Game", H1, seeders=2, sources=["Knaben"])]), \
                mock.patch.object(ts, "search_torrents_csv", lambda q: [ts.SearchResult("Game", H1, seeders=8, sources=["Torrents-CSV"])]), \
                mock.patch.object(ts, "prowlarr_config", lambda: None):
            results, notes = ts.search("game")
        self.assertEqual(notes, [])
        self.assertEqual((len(results), results[0].seeders, results[0].sources), (1, 8, ["Knaben", "Torrents-CSV"]))

    def test_prowlarr_config_needs_both_fields_and_adds_a_scheme(self):
        with mock.patch.dict(ts.LOGINS, {"prowlarr": {"url": "localhost:9696/", "apikey": "k"}}, clear=False):
            self.assertEqual(ts.prowlarr_config(), ("http://localhost:9696", "k"))
        with mock.patch.dict(ts.LOGINS, {"prowlarr": {"url": "localhost:9696", "apikey": ""}}, clear=False):
            self.assertIsNone(ts.prowlarr_config())


if __name__ == "__main__":
    unittest.main()
