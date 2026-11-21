import threading
import unittest
import urllib.error
from unittest import mock

from distill import lbthrottle as lt
from distill import recomps as rc


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    return lt.Throttle(clock=clock, sleep=lambda s: None, min_gap=0, **kw), clock


class Hosts(unittest.TestCase):
    def test_only_launchbox_hosts_are_limited(self):
        for host in ("gamesdb.launchbox-app.com", "images.launchbox-app.com", "gamesdb-images.launchbox.gg"):
            self.assertIsNotNone(lt.for_host(host), host)
        for host in ("api.github.com", "www.steamgriddb.com", "evil-launchbox-app.com", "", None):
            self.assertIsNone(lt.for_host(host), host)

    def test_pages_and_pictures_have_their_own_limits(self):
        self.assertIsNot(lt.for_host("gamesdb.launchbox-app.com"), lt.for_host("images.launchbox-app.com"))
        self.assertIs(lt.for_host("images.launchbox-app.com"), lt.for_host("gamesdb-images.launchbox.gg"))


class Adapting(unittest.TestCase):
    def test_it_starts_modestly_and_climbs_on_good_answers_up_to_the_ceiling(self):
        t, _ = make(start=2, ceiling=4, good_run=3)
        self.assertEqual(t.limit, 2)
        for _ in range(3):
            t.good()
        self.assertEqual(t.limit, 3)
        for _ in range(30):
            t.good()
        self.assertEqual(t.limit, 4)

    def test_slow_down_halves_it_and_a_run_starts_again_from_nothing(self):
        t, _ = make(start=4, ceiling=8, good_run=3)
        t.good(); t.good()
        t.busy(2)
        self.assertEqual(t.limit, 2)
        t.good()  # the run before the warning does not count
        self.assertEqual(t.limit, 2)
        for _ in range(3):
            t.good()
        self.assertEqual(t.limit, 3)

    def test_it_never_goes_below_the_floor(self):
        t, _ = make(start=2, floor=1)
        for _ in range(5):
            t.busy()
        self.assertEqual(t.limit, 1)

    def test_the_wait_asked_for_is_used_and_capped(self):
        t, clock = make()
        self.assertEqual(t.busy(7), 7)
        self.assertEqual(t.cooling(), 7)
        clock.t += 7
        self.assertEqual(t.cooling(), 0)
        self.assertEqual(t.busy(), lt.COOL_DOWN)
        self.assertEqual(t.busy(9999), lt.MAX_COOL_DOWN)

    def test_retry_after_header(self):
        self.assertEqual(lt.retry_after({"Retry-After": "12"}), 12.0)
        self.assertEqual(lt.retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 0.0)
        self.assertEqual(lt.retry_after(None), 0.0)


class Slots(unittest.TestCase):
    def test_no_more_requests_in_flight_than_the_limit(self):
        t = lt.Throttle(start=2, ceiling=2, min_gap=0)
        peak, now, lock, gate = [0], [0], threading.Lock(), threading.Event()

        def request():
            with t.slot():
                with lock:
                    now[0] += 1
                    peak[0] = max(peak[0], now[0])
                gate.wait(0.05)
                with lock:
                    now[0] -= 1

        threads = [threading.Thread(target=request) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
        self.assertEqual(peak[0], 2)
        self.assertEqual(t.in_flight, 0)

    def test_a_slot_is_given_back_when_the_request_fails(self):
        t = lt.Throttle(start=1, ceiling=1, min_gap=0)
        with self.assertRaises(RuntimeError), t.slot():
            raise RuntimeError
        self.assertEqual(t.in_flight, 0)


class Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Quick(lt.Throttle):
    """A throttle whose cool-downs are a hundredth of a second, so a test does not sit through the real ones."""

    def busy(self, wait: float = 0.0) -> float:
        return super().busy(0.01)


class InGet(unittest.TestCase):
    def setUp(self):
        self.fast = Quick(min_gap=0)
        patcher = mock.patch.object(lt, "for_host", lambda h: self.fast if lt.is_launchbox(h) else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def err(self, code, retry=None):
        return urllib.error.HTTPError("u", code, "x", {"Retry-After": retry} if retry else {}, None)

    def test_a_slow_down_is_waited_out_and_asked_again(self):
        answers = [self.err(429, "1"), Resp(b"page")]

        def fake(*a, **k):
            r = answers.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(rc.urllib.request, "urlopen", fake):
            self.assertEqual(rc._get("https://gamesdb.launchbox-app.com/games/results/x"), b"page")
        self.assertEqual(self.fast.slowed, 1)

    def test_it_gives_up_after_three_slow_downs_with_the_error(self):
        with mock.patch.object(rc.urllib.request, "urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(self.err(503))):
            with self.assertRaises(urllib.error.HTTPError):
                rc._get("https://gamesdb.launchbox-app.com/games/results/x")
        self.assertEqual(self.fast.slowed, 3)

    def test_other_errors_pass_straight_through(self):
        with mock.patch.object(rc.urllib.request, "urlopen", side_effect=self.err(404)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                rc._get("https://gamesdb.launchbox-app.com/games/details/1")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.fast.slowed, 0)

    def test_other_sites_are_not_slowed(self):
        with mock.patch.object(rc.urllib.request, "urlopen", return_value=Resp(b"ok")):
            self.assertEqual(rc._get("https://api.github.com/x"), b"ok")
        self.assertEqual(self.fast.slowed, 0)


if __name__ == "__main__":
    unittest.main()
