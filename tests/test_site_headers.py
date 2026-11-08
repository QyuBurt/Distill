import http.server
import json
import threading
import unittest

from distill import downloads as dl
from distill import netrules
from distill.netrules import HostRule, Rules, parse_headers


class _Echo(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/same":
            self.send_response(302)
            self.send_header("Location", "/echo")
            self.end_headers()
        elif self.path.startswith("/bounce"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.other}/echo")
            self.end_headers()
        else:
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    srv.daemon_threads = True
    srv.other = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ParseTests(unittest.TestCase):
    def test_lines_are_checked(self):
        got = parse_headers("X-Token: abc\n  Accept-Language : en \nHost: evil\nRange: bytes=0-\nbad line\nX-Empty:\n"
                            "Bad Name: x\nContent-Length: 5\nUser-Agent: nope\nCookie: a=b")
        self.assertEqual(got, {"X-Token": "abc", "Accept-Language": "en", "Cookie": "a=b"})
        self.assertEqual(parse_headers(""), {})

    def test_rule_headers(self):
        r = Rules()
        r.hosts = [HostRule("example.com", referer="auto", user_agent=" Mozilla/5.0  (X11) ", headers="X-Token: abc")]
        self.assertEqual(r.headers_for("https://dl.example.com/a/b.zip"),
                         {"X-Token": "abc", "User-Agent": "Mozilla/5.0 (X11)", "Referer": "https://dl.example.com/"})
        self.assertEqual(r.headers_for("https://elsewhere.org/a.zip"), {})
        r.hosts[0].referer = "https://example.com/page"
        self.assertEqual(r.headers_for("https://example.com/x")["Referer"], "https://example.com/page")
        r.hosts[0].referer = "javascript:alert(1)"
        self.assertNotIn("Referer", r.headers_for("https://example.com/x"))

    def test_rules_round_trip_and_older_files_still_load(self):
        r = Rules()
        r.hosts = [HostRule("example.com", 2, 0, 0, 0, "", "auto", "UA/1", "X-Token: abc\nHost: evil")]
        back = Rules.from_json(r.to_json())
        self.assertEqual((back.hosts[0].referer, back.hosts[0].user_agent, back.hosts[0].headers), ("auto", "UA/1", "X-Token: abc"))
        old = Rules.from_json({"hosts": [{"host": "old.example", "max_files": 3, "kbps": 0, "attempts": 0, "segments": 0, "proxy": ""}]})
        self.assertEqual((old.hosts[0].max_files, old.hosts[0].referer, old.hosts[0].headers), (3, "", ""))


class RequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a, cls.b = _serve(), _serve()
        cls.a.other = cls.b.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for s in (cls.a, cls.b):
            s.shutdown()
            s.server_close()

    def setUp(self):
        self.saved = Rules(netrules.RULES.global_kbps, list(netrules.RULES.schedule), list(netrules.RULES.hosts),
                           netrules.RULES.segments, netrules.RULES.proxy)
        rules = Rules()
        rules.hosts = [HostRule("127.0.0.1", referer="auto", user_agent="TestAgent/1", headers="X-Token: secret\nCookie: sid=1")]
        netrules.set_rules(rules)
        self.base = f"http://127.0.0.1:{self.a.server_address[1]}"

    def tearDown(self):
        netrules.set_rules(self.saved)

    def get(self, url):
        with dl.http_open(url) as resp:
            return json.loads(resp.read())

    def test_the_site_rule_is_applied_to_a_request(self):
        seen = self.get(self.base + "/echo")
        self.assertEqual(seen["referer"], self.base + "/")
        self.assertEqual(seen["user-agent"], "TestAgent/1")
        self.assertEqual(seen["x-token"], "secret")

    def test_a_request_for_another_site_is_untouched(self):
        seen = self.get(f"http://localhost:{self.b.server_address[1]}/echo")
        self.assertNotIn("x-token", seen)
        self.assertNotEqual(seen["user-agent"], "TestAgent/1")

    def test_a_redirect_on_the_same_site_keeps_the_headers(self):
        seen = self.get(self.base + "/same")
        self.assertEqual((seen["x-token"], seen["cookie"]), ("secret", "sid=1"))

    def test_a_redirect_to_another_site_does_not_carry_the_private_ones(self):
        seen = self.get(self.base + "/bounce")  # lands on localhost, a different site
        self.assertNotIn("x-token", seen)
        self.assertNotIn("cookie", seen)

    def test_a_range_request_still_works_with_rules_on(self):
        with dl.http_open(self.base + "/echo", {"Range": "bytes=5-"}) as resp:
            self.assertEqual(json.loads(resp.read())["range"], "bytes=5-")


if __name__ == "__main__":
    unittest.main()
