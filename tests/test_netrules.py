import unittest
from datetime import datetime

from distill.netrules import HostGate, HostRule, PAUSED, Rules, ScheduleRule, Throttle, describe_schedule, host_of


def at(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 2 + day, h, m)  # 2 Nov 2026 is a Monday


class RuleTests(unittest.TestCase):
    def test_schedule_picks_first_matching_window_else_global(self):
        r = Rules(global_kbps=2048, schedule=[ScheduleRule(start=120, end=480, kbps=0),
                                              ScheduleRule(days=(5, 6), start=0, end=0, kbps=PAUSED)])
        self.assertEqual(r.active_kbps(at(0, 3)), 0)        # 03:00 Monday: full speed
        self.assertEqual(r.active_kbps(at(0, 12)), 2048)    # noon Monday: the global cap
        self.assertEqual(r.active_kbps(at(5, 12)), PAUSED)  # Saturday: paused

    def test_window_across_midnight(self):
        rule = ScheduleRule(days=(4,), start=22 * 60, end=6 * 60, kbps=0)  # Friday 22:00 to Saturday 06:00
        self.assertTrue(rule.covers(at(4, 23)))
        self.assertTrue(rule.covers(at(5, 5)))
        self.assertFalse(rule.covers(at(5, 7)))
        self.assertFalse(rule.covers(at(4, 5)))

    def test_most_specific_host_rule(self):
        r = Rules(hosts=[HostRule("example.com", 2), HostRule("cdn.example.com", 5)])
        self.assertEqual(r.host_rule("cdn.example.com").max_files, 5)
        self.assertEqual(r.host_rule("www.example.com").max_files, 2)
        self.assertIsNone(r.host_rule("notexample.com"))

    def test_json_round_trip_and_junk(self):
        r = Rules(1000, [ScheduleRule((0, 1), 60, 120, 500)], [HostRule("a.com", 3, 100, 6)])
        self.assertEqual(Rules.from_json(r.to_json()).to_json(), r.to_json())
        self.assertEqual(Rules.from_json("junk").to_json(), Rules().to_json())
        self.assertEqual(Rules.from_json({"hosts": [{"host": ""}, 5], "schedule": ["x"]}).hosts, [])

    def test_host_of(self):
        self.assertEqual(host_of("https://Cdn.Example.com/a"), "cdn.example.com")
        self.assertEqual(host_of("torbox:1:2"), "torbox.app")

    def test_describe(self):
        self.assertIn("paused", describe_schedule(ScheduleRule((5,), 0, 0, PAUSED)))


class GateTests(unittest.TestCase):
    def test_per_site_cap(self):
        g = HostGate(Rules(hosts=[HostRule("a.com", 2)]))
        self.assertTrue(g.try_acquire("a.com") and g.try_acquire("a.com"))
        self.assertFalse(g.try_acquire("a.com"))
        self.assertTrue(g.try_acquire("b.com"))  # other sites are unaffected
        g.release("a.com")
        self.assertTrue(g.try_acquire("a.com"))


class ThrottleTests(unittest.TestCase):
    def make(self, rules):
        self.t, self.slept = [0.0], []

        def sleep(s):
            self.slept.append(s)
            self.t[0] += s
        return Throttle(rules, clock=lambda: self.t[0], sleep=sleep, now=lambda: at(0, 12))

    def test_global_limit_averages_out(self):
        th = self.make(Rules(global_kbps=100))  # 100 KB/s
        for _ in range(10):
            self.assertTrue(th.consume(100 * 1024, "x.com", lambda: False))
        self.assertAlmostEqual(self.t[0], 9.0, delta=0.5)  # ten 100 KB chunks take ~10 s (the first is free)

    def test_site_limit_is_the_tighter_one(self):
        th = self.make(Rules(global_kbps=1000, hosts=[HostRule("x.com", kbps=100)]))
        for _ in range(5):
            th.consume(100 * 1024, "x.com", lambda: False)
        self.assertGreater(self.t[0], 3.5)

    def test_unlimited_never_sleeps(self):
        th = self.make(Rules())
        th.consume(10**9, "x.com", lambda: False)
        self.assertEqual(self.slept, [])

    def test_paused_waits_and_abort_gets_out(self):
        th = self.make(Rules(schedule=[ScheduleRule(kbps=PAUSED)]))
        calls = [0]

        def abort():
            calls[0] += 1
            return calls[0] > 3
        self.assertFalse(th.consume(1, "x.com", abort))


if __name__ == "__main__":
    unittest.main()


class ProxyTests(unittest.TestCase):
    def test_normalize(self):
        from distill.netrules import normalize_proxy
        self.assertEqual(normalize_proxy("10.0.0.1:8080"), ("http://10.0.0.1:8080", ""))
        self.assertEqual(normalize_proxy(""), ("", ""))
        self.assertEqual(normalize_proxy("direct", allow_direct=True), ("direct", ""))
        self.assertTrue(normalize_proxy("direct")[1])  # not allowed globally
        self.assertIn("SOCKS", normalize_proxy("socks5://h:1")[1])

    def test_site_rule_beats_overall_and_direct_opts_out(self):
        r = Rules(proxy="http://global:1", hosts=[HostRule("a.com", proxy="http://a:2"), HostRule("b.com", proxy="direct")])
        self.assertEqual(r.proxy_for("x.com"), "http://global:1")
        self.assertEqual(r.proxy_for("www.a.com"), "http://a:2")
        self.assertEqual(r.proxy_for("b.com"), "direct")
        self.assertEqual(Rules.from_json(r.to_json()).to_json(), r.to_json())
        self.assertEqual(Rules.from_json({"proxy": "socks5://x:1"}).proxy, "")


class OpenerTests(unittest.TestCase):
    def test_opener_follows_the_rule(self):
        import urllib.request
        from distill import downloads as dl, netrules
        saved = (netrules.RULES.proxy, netrules.RULES.hosts)
        self.addCleanup(lambda: (setattr(netrules.RULES, "proxy", saved[0]), setattr(netrules.RULES, "hosts", saved[1])))
        netrules.RULES.proxy, netrules.RULES.hosts = "", [HostRule("p.com", proxy="http://prox:9")]
        self.assertIs(dl.opener_for("https://x.com/a"), dl._OPENER)
        op = dl.opener_for("https://p.com/a")
        proxies = next(h for h in op.handlers if isinstance(h, urllib.request.ProxyHandler)).proxies
        self.assertEqual(proxies["https"], "http://prox:9")
        netrules.RULES.hosts = [HostRule("p.com", proxy="direct")]
        direct = dl.opener_for("https://p.com/a")
        self.assertIsNot(direct, dl._OPENER)
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in direct.handlers))  # no proxy at all
