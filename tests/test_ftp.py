import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from distill import downloads as dl
from distill import ftpclient, links
from distill.downloads import LinkError, RemoteFile

FILES = {"/pub/a.zip": b"0123456789" * 50, "/pub/sub/b.7z": b"B" * 300, "/pub/sub/deeper/c.rar": b"C" * 7, "/top.bin": b"T" * 40}
DIRS = {"/", "/pub", "/pub/sub", "/pub/sub/deeper"}


class _Handler(socketserver.StreamRequestHandler):
    """Just enough of an FTP server for the client: login, passive mode, SIZE, CWD, MLSD or LIST, REST and RETR."""

    def send(self, text):
        self.wfile.write((text + "\r\n").encode("utf-8"))
        self.wfile.flush()

    def handle(self):
        srv = self.server
        self.send("220 test server")
        user, rest, listener = "", 0, None
        while True:
            line = self.rfile.readline().decode("utf-8", "replace").strip()
            if not line:
                if listener:
                    listener.close()
                return
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            srv.log.append(line if cmd != "PASS" else "PASS ***")
            if cmd == "USER":
                user = arg
                self.send("331 password please")
            elif cmd == "PASS":
                ok = (user == "anonymous") if not srv.login else (user, arg) == srv.login
                self.send("230 in" if ok else "530 Login incorrect")
            elif cmd in ("TYPE", "OPTS"):
                self.send("200 ok")
            elif cmd == "PASV":
                if listener:
                    listener.close()
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                p = listener.getsockname()[1]
                self.send(f"227 Entering Passive Mode (127,0,0,1,{p >> 8},{p & 255})")
            elif cmd == "SIZE":
                self.send(f"213 {len(FILES[arg])}" if arg in FILES else "550 no such file")
            elif cmd == "CWD":
                self.send("250 ok" if arg.rstrip("/") in DIRS or arg == "/" else "550 not a directory")
            elif cmd == "REST":
                rest = int(arg)
                self.send("350 ok")
            elif cmd in ("MLSD", "LIST") and listener:
                if cmd == "MLSD" and not srv.mlsd:
                    self.send("500 unknown command")
                    continue
                folder = (arg or "/").rstrip("/") or "/"
                prefix = "" if folder == "/" else folder
                names = {}
                for path, data in FILES.items():
                    if path.startswith(prefix + "/") and path.count("/") == prefix.count("/") + 1:
                        names[path.rsplit("/", 1)[1]] = ("file", len(data))
                for d in DIRS:
                    if d != "/" and d.startswith(prefix + "/") and d.count("/") == prefix.count("/") + 1:
                        names[d.rsplit("/", 1)[1]] = ("dir", 0)
                self.send("150 here it comes")
                conn, _ = listener.accept()
                if cmd == "MLSD":
                    body = "".join(f"type={k};size={n}; {name}\r\n" for name, (k, n) in names.items())
                else:
                    body = "".join(f"{'d' if k == 'dir' else '-'}rw-r--r-- 1 u g {n} Jan  1 12:00 {name}\r\n" for name, (k, n) in names.items())
                conn.sendall(body.encode("utf-8"))
                conn.close()
                listener.close()
                self.send("226 done")
            elif cmd == "RETR" and listener:
                if arg not in FILES:
                    self.send("550 no such file")
                    continue
                self.send("150 sending")
                conn, _ = listener.accept()
                conn.sendall(FILES[arg][rest:])
                conn.close()
                listener.close()
                rest = 0
                self.send("226 done")
            elif cmd == "QUIT":
                self.send("221 bye")
                if listener:
                    listener.close()
                return
            else:
                self.send("502 not implemented")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _running() -> threading.Event:
    event = threading.Event()
    event.set()  # the runner waits while this is clear: clear means "paused"
    return event


class FtpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        cls.server.log, cls.server.login, cls.server.mlsd = [], None, True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.login, self.server.mlsd = None, True
        self.base = f"ftp://127.0.0.1:{self.port}"
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_folder_tree_is_listed_with_mlsd(self):
        group, found = ftpclient.list_files(self.base + "/pub/")
        self.assertEqual(group, "pub")
        self.assertEqual(sorted((n, s) for n, _u, s in found),
                         [("a.zip", 500), ("sub/b.7z", 300), ("sub/deeper/c.rar", 7)])
        self.assertIn(f"{self.base}/pub/sub/b.7z", [u for _n, u, _s in found])

    def test_a_folder_tree_is_listed_with_list_when_there_is_no_mlsd(self):
        self.server.mlsd = False
        _group, found = ftpclient.list_files(self.base + "/pub")  # no trailing slash: found out to be a folder
        self.assertEqual(sorted(n for n, _u, _s in found), ["a.zip", "sub/b.7z", "sub/deeper/c.rar"])
        self.assertEqual({n: s for n, _u, s in found}["a.zip"], 500)

    def test_one_file_is_one_row_with_its_size(self):
        group, found = ftpclient.list_files(self.base + "/top.bin")
        self.assertEqual(found, [("top.bin", self.base + "/top.bin", 40)])

    def test_depth_and_count_limits(self):
        _g, found = ftpclient.list_files(self.base + "/pub/", max_depth=0)
        self.assertEqual([n for n, _u, _s in found], ["a.zip"])
        _g, found = ftpclient.list_files(self.base + "/pub/", max_files=2)
        self.assertEqual(len(found), 2)

    def test_logins(self):
        self.server.login = ("bob", "pw")
        with self.assertRaises(ftpclient.FtpError) as ctx:
            ftpclient.list_files(self.base + "/pub/")  # anonymous is refused
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("Login incorrect", str(ctx.exception))
        _g, found = ftpclient.list_files(f"ftp://bob:pw@127.0.0.1:{self.port}/pub/")
        self.assertTrue(found and all("bob:pw@" in u for _n, u, _s in found))  # the files keep the address's own login
        _g, found = ftpclient.list_files(self.base + "/pub/", creds=("bob", "pw"))  # or a saved one
        self.assertTrue(found)

    def test_streaming_and_resuming(self):
        data = FILES["/pub/a.zip"]
        with ftpclient.FtpStream(self.base + "/pub/a.zip") as s:
            self.assertEqual((s.status, s.headers.get("Content-Length")), (200, "500"))
            got = b""
            while chunk := s.read(64):
                got += chunk
        self.assertEqual(got, data)
        with ftpclient.FtpStream(self.base + "/pub/a.zip", rest=120) as s:
            self.assertEqual((s.status, s.headers.get("Content-Length")), (206, "380"))
            got = b""
            while chunk := s.read(64):
                got += chunk
        self.assertEqual(got, data[120:])

    def test_a_missing_file_is_a_permanent_error(self):
        with self.assertRaises(ftpclient.FtpError) as ctx:
            ftpclient.FtpStream(self.base + "/nope.bin")
        self.assertTrue(ctx.exception.permanent)
        self.assertFalse(dl.DownloadRunner._retryable(ctx.exception))
        self.assertTrue(dl.DownloadRunner._retryable(ftpclient.FtpError("timed out", permanent=False)))

    def test_nothing_listening_is_a_plain_error(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(ftpclient.FtpError) as ctx:
            ftpclient.list_files(f"ftp://127.0.0.1:{port}/x/")
        self.assertFalse(ctx.exception.permanent)

    def test_list_line_parsing(self):
        P = ftpclient.parse_list_line
        self.assertEqual(P("-rw-r--r--   1 ftp ftp      1234 Mar 03 10:22 my file.zip"), ("my file.zip", False, 1234))
        self.assertEqual(P("drwxr-xr-x   2 ftp ftp      4096 Mar 03  2020 Games"), ("Games", True, None))
        self.assertEqual(P("lrwxrwxrwx   1 ftp ftp         4 Mar 03 10:22 link -> /etc"), None)
        self.assertEqual(P("03-03-24  10:22AM       <DIR>          Roms"), ("Roms", True, None))
        self.assertEqual(P("03-03-24  10:22PM              5120 disc.iso"), ("disc.iso", False, 5120))
        self.assertIsNone(P("total 12"))

    def test_parse(self):
        t = ftpclient.parse("ftps://me:p%40ss@host.example:2121/a%20b/c.bin")
        self.assertEqual((t.secure, t.host, t.port, t.user, t.password, t.path), (True, "host.example", 2121, "me", "p@ss", "/a b/c.bin"))
        self.assertEqual(ftpclient.parse("ftp://host.example/x").user, "anonymous")
        self.assertEqual(ftpclient.parse("ftp://host.example/x", ("u", "p")).user, "u")
        with self.assertRaises(ftpclient.FtpError):
            ftpclient.parse("http://host.example/x")

    def test_pasting_an_ftp_link_lists_it(self):
        files, errors = links.resolve_links(self.base + "/pub/ " + self.base + "/top.bin")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(f.name for f in files), ["a.zip", "sub/b.7z", "sub/deeper/c.rar", "top.bin"])
        self.assertEqual(next(f for f in files if f.name == "top.bin").size, 40)
        _f, errors = links.resolve_links(f"ftp://127.0.0.1:{self.port}/missing/")
        self.assertTrue(errors and "no files found" in errors[0])  # (a real server answers 550, which is reported as an FTP error)

    def test_the_runner_downloads_and_resumes_over_ftp(self):
        rf = RemoteFile("a.zip", self.base + "/pub/a.zip", 500)
        events = []
        runner = dl.DownloadRunner([(0, rf)], self.tmp / "out", 1, False, False, events.append, _running())
        runner.run()
        self.assertEqual((self.tmp / "out" / "a.zip").read_bytes(), FILES["/pub/a.zip"])
        self.assertEqual((runner.done, runner.failures), (1, []))
        # a half-finished .part carries on from where it stopped
        (self.tmp / "out2").mkdir()
        (self.tmp / "out2" / "a.zip.part").write_bytes(FILES["/pub/a.zip"][:200])
        runner = dl.DownloadRunner([(0, rf)], self.tmp / "out2", 1, False, False, events.append, _running())
        runner.run()
        self.assertEqual((self.tmp / "out2" / "a.zip").read_bytes(), FILES["/pub/a.zip"])
        self.assertTrue(any("REST 200" in line for line in self.server.log))

    def test_a_wrong_login_fails_the_file_without_retrying(self):
        self.server.login = ("bob", "pw")
        before = len(self.server.log)
        runner = dl.DownloadRunner([(0, RemoteFile("a.zip", self.base + "/pub/a.zip", 500))], self.tmp / "out", 1, False, False,
                                   [].append, _running())
        runner.run()
        self.assertEqual(len(runner.failures), 1)
        self.assertIn("Login incorrect", runner.failures[0][2])
        self.assertEqual(sum(1 for line in self.server.log[before:] if line.startswith("USER")), 1)


if __name__ == "__main__":
    unittest.main()
