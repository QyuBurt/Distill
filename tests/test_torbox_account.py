"""What is on a TorBox account: the three lists are read with the saved key, paged, and merged newest first."""
import unittest
from unittest import mock

from distill import torbox
from distill.downloads import LinkError


def raw(ident, **extra):
    return {"id": ident, "name": f"Game {ident}", "size": 1000, "created_at": f"2025-01-{ident:02d}T10:00:00Z", **extra}


class AccountTests(unittest.TestCase):
    @staticmethod
    def fake(lists):
        """A stand-in for _torbox_call: path -> data (a list, a LinkError to raise, or a function of the offset)."""
        calls = []

        def call(path, params=None, form=None, timeout=30):
            calls.append((path, dict(params or {})))
            data = lists[path.split("/")[0]]
            if isinstance(data, Exception):
                raise data
            return {"success": True, "data": data(params["offset"]) if callable(data) else data}
        return call, calls

    def test_all_three_lists_are_merged_newest_first(self):
        call, calls = self.fake({"torrents": [raw(3, files=[{"id": 4, "name": "Game 3/a.bin", "short_name": "a.bin", "size": 5}])],
                                 "usenet": [raw(9)], "webdl": [raw(1)]})
        with mock.patch.object(torbox, "_torbox_call", call):
            items, problems = torbox.torbox_account()
        self.assertEqual([(i.kind, i.id) for i in items], [("Usenet", 9), ("Torrent", 3), ("Web", 1)])
        self.assertEqual(problems, [])
        self.assertEqual(items[1].files, (torbox.AccountFile(4, "a.bin", "Game 3/a.bin", 5),))
        self.assertTrue(all(p["bypass_cache"] == "true" for _path, p in calls))  # not the 10-minute-old copy

    def test_a_long_list_is_read_page_by_page(self):
        pages = {0: [raw(i % 28 + 1) | {"id": i} for i in range(1000)], 1000: [raw(5000)]}
        call, calls = self.fake({"torrents": lambda offset: pages[offset], "usenet": [], "webdl": None})
        with mock.patch.object(torbox, "_torbox_call", call):
            items, _problems = torbox.torbox_account()
        self.assertEqual(len(items), 1001)
        self.assertEqual([p["offset"] for path, p in calls if path.startswith("torrents")], [0, 1000])

    def test_one_list_failing_keeps_the_others(self):
        call, _ = self.fake({"torrents": [raw(2)], "usenet": LinkError("TorBox: HTTP 500"), "webdl": []})
        with mock.patch.object(torbox, "_torbox_call", call):
            items, problems = torbox.torbox_account()
        self.assertEqual(len(items), 1)
        self.assertEqual(problems, ["Usenet downloads: TorBox: HTTP 500"])

    def test_every_list_failing_raises(self):
        err = LinkError("TorBox unreachable: timed out")
        call, _ = self.fake({"torrents": err, "usenet": err, "webdl": err})
        with mock.patch.object(torbox, "_torbox_call", call), self.assertRaises(LinkError):
            torbox.torbox_account()

    def test_a_rejected_key_stops_at_once(self):
        call, calls = self.fake({"torrents": LinkError("TorBox rejected the API key - check it under Logins."),
                                 "usenet": [], "webdl": []})
        with mock.patch.object(torbox, "_torbox_call", call), self.assertRaises(LinkError):
            torbox.torbox_account()
        self.assertEqual(len(calls), 1)

    def test_status_wording(self):
        ready = torbox._account_item("Torrent", raw(1, download_finished=True, progress=0.2))
        self.assertEqual((ready.status, ready.progress), ("Ready", 1.0))
        busy = torbox._account_item("Torrent", raw(2, download_state="downloading", progress=0.5))
        self.assertEqual(busy.status, "Downloading 50%")
        queued = torbox._account_item("Web", raw(3))
        self.assertEqual(queued.status, "Queued")
        self.assertIsNone(torbox._account_item("Web", {"name": "no id"}))

    @staticmethod
    def item(kind, ident, names, name="Pack"):
        files = [{"id": n, "name": p, "size": 10 * n} for n, p in enumerate(names, 1)]
        return torbox._account_item(kind, raw(ident, name=name, files=files, download_finished=True))

    def test_picked_files_become_list_entries_that_download_from_torbox(self):
        single = self.item("Torrent", 7, ["Game.iso"], name="Game.iso")
        pack = self.item("Torrent", 8, ["Disc 1.bin", "Disc 2.bin"])
        news = self.item("Usenet", 9, ["Pack/x.rar"])
        web = self.item("Web", 3, ["f.zip"], name="f.zip")
        got = torbox.account_remote_files([(single, single.files[0]), (pack, pack.files[1]), (news, news.files[0]),
                                           (web, web.files[0])])
        self.assertEqual([(f.name, f.url, f.group, f.source) for f in got], [
            ("Game.iso", "torbox:7:1", "Game.iso", "TorBox"),
            ("Pack/Disc 2.bin", "torbox:8:2", "Pack", "TorBox"),  # a multi-file download keeps its folder
            ("Pack/x.rar", "torbox:usenet:9:1", "Pack", "TorBox"),
            ("f.zip", "torbox:web:3:1", "f.zip", "TorBox")])
        self.assertEqual(got[1].size, 20)

    def test_unsafe_paths_are_left_out(self):
        bad = self.item("Torrent", 1, ["../escape.bin", "ok.bin"])
        self.assertEqual([f.name for f in torbox.account_remote_files([(bad, f) for f in bad.files])], ["Pack/ok.bin"])

    def test_each_kind_asks_its_own_link_route(self):
        from distill.downloads import direct_url
        seen = []

        def call(path, params=None, form=None, timeout=30):
            seen.append((path, {k: v for k, v in params.items() if k != "token"}))
            return {"success": True, "data": "https://dl.example/x"}
        with mock.patch.object(torbox, "_torbox_call", call), mock.patch.object(torbox, "torbox_key", lambda: "k"):
            for address in ("torbox:5:7", "torbox:usenet:5:7", "torbox:web:5:7"):
                self.assertEqual(direct_url(address), "https://dl.example/x")
            with self.assertRaises(LinkError):
                direct_url("torbox:nonsense:5:7")
        self.assertEqual([p for p, _ in seen], ["torrents/requestdl", "usenet/requestdl", "webdl/requestdl"])
        self.assertEqual([next(k for k in q if k.endswith("_id") and k != "file_id") for _p, q in seen],
                         ["torrent_id", "usenet_id", "web_id"])


if __name__ == "__main__":
    unittest.main()
