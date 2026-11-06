"""The shipped icons are brought to one size. Skipped when PySide6 isn't installed (CI installs it)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401

try:
    import PySide6  # noqa: F401
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class NormalizeIconTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtGui import QGuiApplication
        self.app = QGuiApplication.instance() or QGuiApplication([])
        self.dir = Path(tempfile.mkdtemp())

    def make(self, name, w, h, fmt="PNG"):
        from PySide6.QtGui import QColor, QImage
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor("red"))
        self.assertTrue(img.save(str(self.dir / name), fmt))

    def test_every_picture_ends_square_png_of_one_size_in_place(self):
        from PySide6.QtGui import QImage
        from distill.ui.emu_images import normalize_icons
        self.make("Wide.png", 600, 200)
        self.make("tiny.jpg", 40, 40, "JPG")
        done = normalize_icons(self.dir, 128)
        self.assertEqual(len(done), 2)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["tiny.png", "wide.png"])
        for p in self.dir.iterdir():
            img = QImage(str(p))
            self.assertEqual((img.width(), img.height()), (128, 128))
        self.assertEqual(normalize_icons(self.dir, 128), [])  # nothing left to do the second time

    def test_an_ico_is_read_at_its_biggest_size(self):
        import struct
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage
        from distill.ui.emu_images import normalize_icons

        def png(side, colour):
            img = QImage(side, side, QImage.Format.Format_ARGB32)
            img.fill(QColor(colour))
            data, buf = QByteArray(), QBuffer()
            buf.setBuffer(data)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            return bytes(data)
        small, big = png(16, "blue"), png(64, "red")  # the small one first, as many .ico files are
        head = struct.pack("<HHH", 0, 1, 2)
        offset = 6 + 16 * 2
        entries = (struct.pack("<BBBBHHII", 16, 16, 0, 0, 1, 32, len(small), offset)
                   + struct.pack("<BBBBHHII", 64, 64, 0, 0, 1, 32, len(big), offset + len(small)))
        (self.dir / "Icon.ico").write_bytes(head + entries + small + big)
        normalize_icons(self.dir, 32)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["icon.png"])
        out = QImage(str(self.dir / "icon.png"))
        self.assertEqual((out.width(), out.height()), (32, 32))
        self.assertEqual(out.pixelColor(16, 16).red(), 255)  # the 64 px red one, not the 16 px blue one

    def test_a_picture_with_its_own_background_fills_the_tile(self):
        from PySide6.QtGui import QColor, QImage, QPainter
        from distill.ui.emu_cards import _full_bleed
        solid = QImage(256, 256, QImage.Format.Format_ARGB32)
        solid.fill(QColor("red"))
        self.assertTrue(_full_bleed(solid))
        logo = QImage(256, 256, QImage.Format.Format_ARGB32)
        logo.fill(QColor(0, 0, 0, 0))
        painter = QPainter(logo)
        painter.fillRect(80, 80, 96, 96, QColor("red"))  # a mark in the middle of nothing
        painter.end()
        self.assertFalse(_full_bleed(logo))
        padded = QImage(256, 256, QImage.Format.Format_ARGB32)
        padded.fill(QColor(0, 0, 0, 0))
        painter = QPainter(padded)
        painter.fillRect(0, 64, 256, 128, QColor("red"))  # a wide picture: padded top and bottom
        painter.end()
        self.assertFalse(_full_bleed(padded))

    def test_the_tile_is_drawn_at_the_screens_own_pixels(self):
        from PySide6.QtGui import QColor
        from distill.ui.emu_cards import logo_pixmap
        self.make("x.png", 256, 256)
        px = logo_pixmap(str(self.dir / "x.png"), QColor("blue"), 34, dpr=1.5)
        self.assertEqual((px.width(), px.height()), (51, 51))
        self.assertAlmostEqual(px.devicePixelRatio(), 1.5)

    def test_a_picture_on_transparency_gets_no_group_colour_behind_it(self):
        from PySide6.QtGui import QColor, QImage, QPainter
        from distill.ui.emu_cards import _full_bleed, logo_pixmap
        src = QImage(256, 256, QImage.Format.Format_ARGB32)
        src.fill(QColor(0, 0, 0, 0))
        painter = QPainter(src)
        painter.fillRect(96, 96, 64, 64, QColor("red"))  # a small mark on nothing
        painter.end()
        self.assertTrue(src.save(str(self.dir / "mark.png"), "PNG"))
        self.assertFalse(_full_bleed(src))
        image = logo_pixmap(str(self.dir / "mark.png"), QColor("blue"), 40, dpr=1.0).toImage()
        self.assertEqual(image.pixelColor(6, 6).alpha(), 0)  # beside the mark: nothing drawn, no wash, no backing tile
        self.assertGreater(image.pixelColor(20, 20).alpha(), 200)  # the mark itself

    def test_two_files_for_one_name_leave_the_newest(self):
        import os
        from distill.ui.emu_images import normalize_icons
        self.make("a.png", 50, 50)
        self.make("a.jpg", 50, 50, "JPG")
        os.utime(self.dir / "a.png", (1, 1))
        normalize_icons(self.dir, 64)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["a.png"])

    def test_a_file_that_is_not_a_picture_is_left_alone(self):
        from distill.ui.emu_images import normalize_icons
        (self.dir / "junk.png").write_bytes(b"not an image")
        self.assertEqual(normalize_icons(self.dir, 64), [])
        self.assertEqual((self.dir / "junk.png").read_bytes(), b"not an image")


if __name__ == "__main__":
    unittest.main()
