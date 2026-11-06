import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from distill import cookies, debrid


class FakeNet:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        for frag, reply in self.routes.items():
            if frag in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"unexpected call {url}")


class DebridTests(unittest.TestCase):
    def test_real_debrid_unlocks_only_supported_hosts(self):
        net = FakeNet({"hosts/domains": ["1fichier.com", "mega.nz"],
                       "unrestrict/link": {"download": "https://dl/x", "filename": "g.zip", "filesize": 99}})
        rd = debrid.RealDebrid("tok", net)
        self.assertTrue(rd.supports("1fichier.com") and rd.supports("www.mega.nz"))
        self.assertFalse(rd.supports("myserver.lan"))
        got = rd.unlock("https://mega.nz/file/x")
        self.assertEqual((got.url, got.name, got.size), ("https://dl/x", "g.zip", 99))
        self.assertEqual(net.calls[-1][2]["Authorization"], "Bearer tok")
        self.assertEqual(sum("hosts/domains" in c[1] for c in net.calls), 1)  # the host list is cached

    def test_never_sends_own_login_sites(self):
        rd = debrid.RealDebrid("t", FakeNet({"hosts/domains": ["archive.org"]}))
        self.assertFalse(rd.supports("archive.org"))

    def test_other_services_parse_their_replies(self):
        ad = debrid.AllDebrid("k", FakeNet({"hosts/domains": {"data": {"hosts": ["a.com"], "redirectors": ["b.com"]}},
                                            "link/unlock": {"status": "success", "data": {"link": "https://x", "filename": "f", "filesize": 5}}}))
        self.assertTrue(ad.supports("b.com"))
        self.assertEqual(ad.unlock("https://a.com/1").url, "https://x")
        pm = debrid.Premiumize("k", FakeNet({"services/list": {"directdl": ["c.com"]},
                                             "directdl": {"status": "success", "content": [{"path": "dir/f.bin", "size": 7, "link": "https://y"}]}}))
        u = pm.unlock("https://c.com/1")
        self.assertEqual((u.url, u.name, u.size), ("https://y", "f.bin", 7))
        dl = debrid.DebridLink("k", FakeNet({"hostnames": {"value": ["d.com"]},
                                             "downloader/add": {"success": True, "value": {"downloadUrl": "https://z", "name": "n", "size": 3}}}))
        self.assertEqual(dl.unlock("https://d.com/1").url, "https://z")

    def test_errors_are_named(self):
        rd = debrid.RealDebrid("t", FakeNet({"hosts/domains": ["a.com"], "unrestrict": debrid.DebridError("File removed")}))
        keys = {"realdebrid": {"apikey": "t"}}
        debrid._LIVE[("realdebrid", "t")] = rd
        with self.assertRaises(debrid.DebridError) as cm:
            debrid.unlock("https://a.com/1", "a.com", keys)
        self.assertIn("Real-Debrid: File removed", str(cm.exception))
        debrid._LIVE.clear()

    def test_no_keys_or_unsupported_means_not_handled(self):
        self.assertIsNone(debrid.unlock("https://a.com/1", "a.com", {}))
        debrid._LIVE[("realdebrid", "t")] = debrid.RealDebrid("t", FakeNet({"hosts/domains": ["other.com"]}))
        self.assertIsNone(debrid.unlock("https://a.com/1", "a.com", {"realdebrid": {"apikey": "t"}}))
        debrid._LIVE.clear()

    def test_torbox_is_torrent_only(self):
        self.assertTrue(debrid.TorBoxService.torrents)
        self.assertFalse(debrid.TorBoxService("t").supports("mega.nz"))


class CookieTests(unittest.TestCase):
    TXT = ("# Netscape HTTP Cookie File\n"
           ".example.com\tTRUE\t/\tTRUE\t4102444800\tsid\tabc\n"
           "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t4102444800\ttok\tdef\n"
           "dl.other.org\tFALSE\t/\tFALSE\t0\tk\tv\n"
           ".old.com\tTRUE\t/\tFALSE\t1000\tx\ty\n"
           "junk line\n")

    def test_parse_group_and_logins(self):
        cs = cookies.parse_netscape(self.TXT)
        self.assertEqual(len(cs), 4)
        groups = cookies.group_by_domain(cs)
        self.assertEqual(list(groups), ["dl.other.org", "example.com"])  # the expired one is dropped
        logins = cookies.to_logins(groups, {"example.com"})
        self.assertEqual(logins, [{"host": "example.com", "kind": "Cookie", "user": "", "secret": "sid=abc; tok=def"}])

    def test_merge_replaces_and_orders_specific_first(self):
        old = [{"host": "example.com", "kind": "Cookie", "user": "", "secret": "old"},
               {"host": "keep.org", "kind": "Basic (user + password)", "user": "u", "secret": "p"}]
        new = [{"host": "dl.example.com", "kind": "Cookie", "user": "", "secret": "n"},
               {"host": "example.com", "kind": "Cookie", "user": "", "secret": "new"}]
        merged = cookies.merge_logins(old, new)
        self.assertEqual([e["host"] for e in merged], ["dl.example.com", "example.com", "keep.org"])
        self.assertEqual(merged[1]["secret"], "new")

    def test_firefox_store(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "cookies.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, expiry INTEGER, isSecure INTEGER)")
            con.execute("INSERT INTO moz_cookies VALUES ('.site.com','a','1',?,1)", (int(time.time()) + 999,))
            con.commit()
            con.close()
            got = cookies.read_firefox(db)
            self.assertEqual((got[0].domain, got[0].name, got[0].secure), ("site.com", "a", True))


if __name__ == "__main__":
    unittest.main()


class TorrentDebridTests(unittest.TestCase):
    IH = "a" * 40

    def test_premiumize_cache_list_and_fresh_links(self):
        net = FakeNet({"cache/check": {"status": "success", "response": [True, False]},
                       "transfer/directdl": {"status": "success", "content": [
                           {"path": "Movie/a.mkv", "size": 10, "link": "https://dl/a"},
                           {"path": "Movie/b.srt", "size": 2, "link": "https://dl/b"}]}})
        pm = debrid.Premiumize("k", net)
        self.assertEqual(pm.torrent_cached([self.IH, "b" * 40]), {self.IH})
        handle = pm.torrent_open(self.IH, f"magnet:?xt=urn:btih:{self.IH}")
        self.assertEqual([(f.id, f.name, f.size) for f in handle.files], [("0", "Movie/a.mkv", 10), ("1", "Movie/b.srt", 2)])
        self.assertEqual(pm.torrent_link(handle.id, "1"), "https://dl/b")
        with self.assertRaises(debrid.DebridError):
            pm.torrent_link(handle.id, "9")

    def test_real_debrid_flow_selects_files_and_waits_until_ready(self):
        states = iter([{"status": "waiting_files_selection"},
                       {"status": "downloading"},
                       {"status": "downloaded", "files": [{"path": "/x/a.bin", "bytes": 5, "selected": 1},
                                                          {"path": "/x/skip", "bytes": 1, "selected": 0}],
                        "links": ["https://rd/1"]}])
        calls = []

        def fetch(method, url, headers, body):
            calls.append((method, url))
            if url.endswith("addMagnet"):
                return {"id": "T1"}
            if "/torrents/info/" in url:
                return next(states)
            if "selectFiles" in url:
                return None
            if "unrestrict/link" in url:
                return {"download": "https://real/a.bin", "filename": "a.bin", "filesize": 5}
            raise AssertionError(url)
        rd = debrid.RealDebrid("t", fetch)
        rd._sleep = lambda _s: None
        handle = rd.torrent_open(self.IH, "magnet:?xt=urn:btih:" + self.IH)
        self.assertEqual([(f.id, f.name, f.size) for f in handle.files], [("0", "x/a.bin", 5)])
        self.assertTrue(any("selectFiles" in u for _m, u in calls))
        states2 = iter([{"status": "downloaded", "links": ["https://rd/1"], "files": []}])
        rd._fetch = lambda m, u, h, b: next(states2) if "/info/" in u else {"download": "https://real/a.bin"}
        self.assertEqual(rd.torrent_link("T1", "0"), "https://real/a.bin")

    def test_real_debrid_removes_a_torrent_that_isnt_cached(self):
        calls = []

        def fetch(method, url, headers, body):
            calls.append(method)
            if url.endswith("addMagnet"):
                return {"id": "T2"}
            if "/info/" in url:
                return {"status": "downloading"}
            return None
        rd = debrid.RealDebrid("t", fetch)
        rd._sleep = lambda _s: None
        with self.assertRaises(debrid.DebridError) as cm:
            rd.torrent_open(self.IH, "magnet:?xt=urn:btih:" + self.IH)
        self.assertIn("not cached", str(cm.exception))
        self.assertIn("DELETE", calls)

    def test_service_choice(self):
        self.assertIsNone(debrid.torrent_service({}))
        debrid._LIVE.clear()
        self.assertEqual(debrid.torrent_service({"premiumize": {"apikey": "k"}, "debridlink": {"apikey": "d"}}).key, "premiumize")
        self.assertIsNone(debrid.torrent_service({"alldebrid": {"apikey": "a"}}))  # links only
        debrid._LIVE.clear()
