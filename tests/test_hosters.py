import base64
import unittest
from unittest import mock

from distill import hosters
from distill.downloads import LinkError


class AddressTests(unittest.TestCase):
    def test_google_drive(self):
        want = "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
        self.assertEqual(hosters.gdrive_direct("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing"), want)
        self.assertEqual(hosters.gdrive_direct("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp"), want)
        with self.assertRaises(LinkError):
            hosters.gdrive_direct("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp")

    def test_dropbox(self):
        self.assertEqual(hosters.dropbox_direct("https://www.dropbox.com/s/abc/file.zip?dl=0"),
                         "https://www.dropbox.com/s/abc/file.zip?dl=1")
        self.assertIn("rlkey=k", hosters.dropbox_direct("https://www.dropbox.com/scl/fi/x/f.zip?rlkey=k&dl=0"))
        with self.assertRaises(LinkError):
            hosters.dropbox_direct("https://www.dropbox.com/scl/fo/abc/def")

    def test_onedrive(self):
        url = "https://1drv.ms/u/s!AbCd"
        got = hosters.onedrive_direct(url)
        token = got.split("u!")[1].split("/")[0]
        self.assertEqual(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode(), url)

    def test_routing(self):
        self.assertEqual(hosters.hoster_for("https://www.mediafire.com/file/x/y/file").name, "MediaFire")
        self.assertEqual(hosters.hoster_for("https://drive.google.com/file/d/abcdefghijk").name, "Google Drive")
        self.assertIsNone(hosters.hoster_for("https://example.com/x"))
        self.assertIsNone(hosters.hoster_for("https://notmediafire.com/x"))


class MediaFireTests(unittest.TestCase):
    def test_plain_and_scrambled_links(self):
        plain = '<a class="input popsok" aria-label="Download file" id="downloadButton" href="https://download1.mediafire.com/a/b.zip">Download</a>'
        self.assertEqual(hosters.mediafire_find_link(plain), "https://download1.mediafire.com/a/b.zip")
        scrambled = base64.b64encode(b"https://download2.mediafire.com/c/d.zip").decode().rstrip("=")
        page = f'<a id="downloadButton" href="#" data-scrambled-url="{scrambled}">Download</a>'
        self.assertEqual(hosters.mediafire_find_link(page), "https://download2.mediafire.com/c/d.zip")
        self.assertIsNone(hosters.mediafire_find_link("<html>file removed</html>"))

    def test_direct_uses_the_page(self):
        page = '<a id="downloadButton" href="https://download9.mediafire.com/z.bin">x</a>'
        self.assertEqual(hosters.mediafire_direct("https://www.mediafire.com/file/i/z.bin/file", lambda _u: page),
                         "https://download9.mediafire.com/z.bin")
        with self.assertRaises(LinkError):
            hosters.mediafire_direct("https://www.mediafire.com/file/i/z.bin/file", lambda _u: "<p>gone</p>")
        with self.assertRaises(LinkError):
            hosters.mediafire_direct("https://www.mediafire.com/folder/abc/x", lambda _u: "")


class RowTests(unittest.TestCase):
    def test_disposition(self):
        self.assertEqual(hosters.parse_disposition("attachment; filename=\"a b.zip\"; filename*=UTF-8''a%20b.zip"), "a b.zip")
        self.assertEqual(hosters.parse_disposition("attachment; filename=plain.bin"), "plain.bin")
        self.assertEqual(hosters.parse_disposition(""), "")

    def test_entry_reads_name_and_size_from_headers(self):
        class Resp:
            headers = {"Content-Disposition": 'attachment; filename="Game.zip"', "Content-Range": "bytes 0-0/12345",
                       "Content-Type": "application/zip"}

            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch.object(hosters, "http_open", return_value=Resp()):
            row = hosters.entry(hosters.hoster_for("https://drive.google.com/file/d/abcdefghijk"),
                                "https://drive.google.com/file/d/abcdefghijk")
        self.assertEqual((row.name, row.size, row.group, row.url), ("Game.zip", 12345, "Google Drive",
                                                                    "https://drive.google.com/file/d/abcdefghijk"))

    def test_a_web_page_instead_of_the_file_is_an_error(self):
        class Page:
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch.object(hosters, "http_open", return_value=Page()), self.assertRaises(LinkError) as cm:
            hosters.probe("https://x/y", "Google Drive")
        self.assertIn("web page", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
