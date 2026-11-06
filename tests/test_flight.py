"""The icon's flight between the emulator list and a page: the arithmetic everywhere, the widget lifecycle where PySide6 is installed
(animations are stepped by hand so nothing waits on a clock)."""
from __future__ import annotations

import os
import sys
import unittest

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill.ui import flightmath as fm

try:
    import PySide6  # noqa: F401
    HAVE_QT = not os.environ.get("DISTILL_SKIP_UI_TESTS")
except ImportError:  # pragma: no cover
    HAVE_QT = False

if sys.platform != "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class Maths(unittest.TestCase):
    def test_the_ends_are_exact(self):
        a, b = (10, 20, 34, 34), (300, 40, 76, 76)
        self.assertEqual(fm.lerp_box(a, b, 0), a)
        self.assertEqual(fm.lerp_box(a, b, 1), b)
        self.assertEqual(fm.lerp_box(a, b, -3), a)
        self.assertEqual(fm.lerp_box(a, b, 7), b)

    def test_the_middle_is_halfway_in_whole_pixels(self):
        self.assertEqual(fm.lerp_box((0, 0, 40, 40), (100, 20, 76, 76), 0.5), (50, 10, 58, 58))

    def test_easing_starts_and_ends_gently_and_never_overshoots(self):
        self.assertEqual((fm.ease_in_out_cubic(0), fm.ease_in_out_cubic(0.5), fm.ease_in_out_cubic(1)), (0.0, 0.5, 1.0))
        values = [fm.ease_in_out_cubic(i / 50) for i in range(51)]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[2], 0.01)  # slow to leave
        self.assertGreater(values[-3], 0.99)  # and slow to arrive


@unittest.skipUnless(HAVE_QT, "PySide6 not installed (or DISTILL_SKIP_UI_TESTS is set)")
class Lifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QWidget

        from distill.ui import flight, motion

        self.flight, self.motion, self.QRect = flight, motion, QRect
        motion.Motion.reduce = False
        self.addCleanup(setattr, motion.Motion, "reduce", False)
        self.win = QWidget()
        self.win.resize(600, 400)
        self.win.show()
        self.addCleanup(self.win.close)
        self.app.processEvents()
        self.px = QPixmap(76, 76)
        self.px.fill()
        self.ended = 0

    def make(self, end=None):
        def done():
            self.ended += 1
        return self.flight.Flight(self.win, self.px, self.QRect(10, 10, 34, 34), end or (lambda: self.QRect(300, 40, 76, 76)), done=done)

    def test_it_lands_on_the_end_box_and_reports_once(self):
        f = self.make()
        f.go()
        f._anim.setCurrentTime(f._anim.duration())
        self.app.processEvents()
        self.assertEqual(f._last, (300, 40, 76, 76))
        self.assertEqual(self.ended, 1)
        f.finish()  # asking again does nothing more
        self.assertEqual(self.ended, 1)

    def test_cancelling_midway_reports_once_and_removes_the_picture(self):
        f = self.make()
        f.go()
        f._anim.setCurrentTime(f._anim.duration() // 2)
        flyer = f._flyer
        f.finish()
        self.assertEqual(self.ended, 1)
        self.assertFalse(flyer.isVisible())

    def test_no_motion_means_no_flight(self):
        self.motion.Motion.reduce = True
        f = self.make()
        f.go()
        self.assertEqual(self.ended, 1)

    def test_a_missing_destination_keeps_the_picture_where_it_was(self):
        f = self.make(end=lambda: None)
        f.go()
        f._anim.setCurrentTime(f._anim.duration() // 2)
        self.assertEqual(f._last, (10, 10, 34, 34))
        f.finish()

    def test_a_moving_destination_is_followed(self):
        where = {"box": self.QRect(100, 100, 50, 50)}
        f = self.make(end=lambda: where["box"])
        f.go()
        f._anim.setCurrentTime(f._anim.duration() // 2)
        where["box"] = self.QRect(400, 200, 76, 76)  # the page finished laying out somewhere else
        f._anim.setCurrentTime(f._anim.duration())
        self.assertEqual(f._last, (400, 200, 76, 76))

    def test_hold_hides_and_release_restores(self):
        from PySide6.QtWidgets import QLabel

        label = QLabel("x", self.win)
        self.flight.hold(label)
        self.assertAlmostEqual(label.graphicsEffect().opacity(), 0.0)
        self.flight.release(label)
        self.assertIsNone(label.graphicsEffect())


if __name__ == "__main__":
    unittest.main()
