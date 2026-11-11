import threading
import unittest

from distill.artqueue import ArtQueue


class Busy(Exception):
    pass


def settle(q: ArtQueue, timeout: float = 5.0) -> None:
    done = threading.Event()

    def poll():
        while q.pending or q.running:
            threading.Event().wait(0.005)
        done.set()

    threading.Thread(target=poll, daemon=True).start()
    assert done.wait(timeout), "the queue never drained"


class ArtQueueTests(unittest.TestCase):
    def make(self, fetch, **kw):
        self.got, self.lock = [], threading.Lock()

        def deliver(item, found):
            with self.lock:
                self.got.append((item, found))
        return ArtQueue(fetch, deliver, sleep=lambda s: None, **kw)

    def test_every_item_is_fetched_with_its_own_arguments(self):
        q = self.make(lambda item, key, token: f"{item}:{key}:{token}", workers=2)
        for i in range(5):
            q.add(i, "k", "t")
        settle(q)
        self.assertEqual(sorted(self.got), [(i, f"{i}:k:t") for i in range(5)])
        self.assertEqual((q.pending, q.running), (0, 0))

    def test_the_newest_item_is_fetched_first(self):
        gate, order = threading.Event(), []

        def fetch(item):
            if item == "first":
                gate.wait(5)  # hold the only worker so the rest pile up behind it
            order.append(item)
        q = self.make(fetch, workers=1)
        q.add("first")
        while q.pending:  # the worker has taken it
            threading.Event().wait(0.005)
        for x in ("a", "b", "c"):
            q.add(x)
        gate.set()
        settle(q)
        self.assertEqual(order, ["first", "c", "b", "a"])

    def test_no_more_threads_than_workers_run_at_once(self):
        gate, peak, now, lock = threading.Event(), [0], [0], threading.Lock()

        def fetch(item):
            with lock:
                now[0] += 1
                peak[0] = max(peak[0], now[0])
            gate.wait(0.05)
            with lock:
                now[0] -= 1
        q = self.make(fetch, workers=3)
        for i in range(12):
            q.add(i)
        settle(q)
        self.assertLessEqual(peak[0], 3)
        self.assertEqual(len(self.got), 12)

    def test_a_busy_answer_is_reported_and_the_item_delivered_as_missing(self):
        asked = []

        def fetch(item):
            raise Busy()
        q = self.make(fetch, busy=Busy, on_busy=asked.append)
        q.add("x")
        settle(q)
        self.assertEqual(asked, ["x"])
        self.assertEqual(self.got, [("x", None)])

    def test_one_bad_item_does_not_stop_the_rest(self):
        def fetch(item):
            if item == 1:
                raise RuntimeError("boom")
            return item
        q = self.make(fetch, workers=1)
        for i in range(3):
            q.add(i)
        settle(q)
        self.assertEqual(dict(self.got), {0: 0, 1: None, 2: 2})

    def test_a_failing_hand_over_does_not_leave_the_pool_a_thread_short(self):
        calls = []

        def deliver(item, found):
            calls.append(item)
            raise RuntimeError("queue gone")
        q = ArtQueue(lambda item: item, deliver, workers=1, sleep=lambda s: None)
        for i in range(3):
            q.add(i)
        settle(q)
        self.assertEqual(sorted(calls), [0, 1, 2])
        self.assertEqual(q.running, 0)


if __name__ == "__main__":
    unittest.main()
