"""The Distill look: page names as designed, and the bundled fonts and icons are all there (no Qt needed)."""
from __future__ import annotations

import unittest
from pathlib import Path

from distill.ui.nav import NAV_GROUPS, PAGE_CHIP, PAGE_DETAIL, PAGE_INFO

ASSETS = Path(__file__).resolve().parent.parent / "distill" / "assets"


class NavTests(unittest.TestCase):
    def test_sidebar_groups_and_names_follow_the_design(self):
        names = {group: [text for _key, text in items] for group, items in NAV_GROUPS}
        self.assertEqual(list(names), ["GAMES", "DOWNLOADS", "CONVERT & COMPRESS", "ORGANISE LIBRARY"])
        self.assertEqual(names["CONVERT & COMPRESS"], [
            "CHD Compression", "CSO (PSP Images)", "PS3 ISO / Folder", "PS4 PKG Manager", "PS5 PKG Manager",
            "RVZ (Dolphin)", "Switch NSZ / NUP", "Xbox ISO / XEX", "ZAR Archive Tool"])
        self.assertEqual(names["ORGANISE LIBRARY"], ["Fix Names & Discs", "Remove Extras / Junk", "Patch ROMs",
                                                     "Sort by System", "Library Report"])
        self.assertEqual(names["DOWNLOADS"], ["Files & Torrents", "Soundtrack Grabber"])
        self.assertEqual(names["GAMES"], ["Games", "PC Recomps", "Emulators"])

    def test_every_page_has_a_title_blurb_detail_and_chip(self):
        keys = [key for _g, items in NAV_GROUPS for key, _t in items]
        self.assertEqual(len(keys), 19)
        for key in keys:
            self.assertTrue(PAGE_INFO[key][0] and PAGE_INFO[key][1], key)
            self.assertTrue(PAGE_DETAIL[key], key)
            self.assertTrue(PAGE_CHIP[key], key)

    def test_page_titles_are_consistent(self):
        titles = {key: info[0] for key, info in PAGE_INFO.items()}
        self.assertEqual(titles, {
            "chd": "CHD Compression", "cso": "CSO Compression (PSP)", "ps3": "PS3 ISO & Folder Converter",
            "pkg": "PS4 PKG Manager", "pkg5": "PS5 PKG Manager", "rvz": "RVZ Compression (Dolphin)",
            "switch": "Switch NSZ / NUP Converter", "xbox": "Xbox ISO / XEX Repacker", "zar": "ZAR Archive Compressor", "fix": "Fix Names & Discs",
            "extras": "Remove Extras & Junk", "patch": "Patch ROMs", "bysystem": "Sort by System", "report": "Library Report",
            "dl": "Files & Torrents", "sound": "Soundtrack Grabber", "games": "Games", "recomps": "PC Recomps", "emus": "Emulators"})

    def test_ps_pages_run_in_numerical_order(self):
        keys = [key for _g, items in NAV_GROUPS for key, _t in items]
        self.assertLess(keys.index("ps3"), keys.index("pkg"))
        self.assertLess(keys.index("pkg"), keys.index("pkg5"))


class AssetTests(unittest.TestCase):
    def test_fonts_are_bundled_with_their_licence(self):
        for name in ("Inter_18pt-Regular", "Inter_18pt-Medium", "Inter_18pt-SemiBold", "Inter_18pt-Bold",
                     "Outfit-Bold", "JetBrainsMono-Regular", "JetBrainsMono-Bold"):
            self.assertTrue((ASSETS / "fonts" / f"{name}.ttf").is_file(), name)
        for licence in ("OFL.txt", "OFL-Outfit.txt", "OFL-JetBrainsMono.txt"):  # Inter's, then one for each other family
            self.assertTrue((ASSETS / "fonts" / licence).is_file(), licence)

    def test_type_is_never_set_smaller_than_nine_points_or_in_tracked_capitals(self):
        import re
        ui = Path(__file__).resolve().parent.parent / "distill"
        for path in ui.rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            for size in re.findall(r"font-size:\s*([0-9.]+)pt", src):
                self.assertGreaterEqual(float(size), 9.0, f"{path.name}: {size}pt is too small to read comfortably")
            self.assertNotIn("AllUppercase", src, path.name)
            self.assertNotIn("letter-spacing: 1px", src, path.name)

    def test_icons_exist_in_both_colourways_and_every_size(self):
        for variant in ("dark", "bright"):
            for size in (16, 24, 32, 48, 64, 128, 256, 512):
                self.assertTrue((ASSETS / "icons" / f"distill-{variant}-{size}.png").is_file(), (variant, size))
        for glyph in ("check-dark", "chevron-down", "chevron-up", "chevron-right"):
            self.assertTrue((ASSETS / "icons" / f"{glyph}.png").is_file(), glyph)

    def test_group_marks_are_bundled_pictures(self):
        import re
        ui = Path(__file__).resolve().parent.parent / "distill" / "ui"
        icons = (ui / "icons.py").read_text(encoding="utf-8")
        cards = (ui / "emu_cards.py").read_text(encoding="utf-8")
        pictures = dict(re.findall(r'"([a-z0-9]+)":\s*"([a-z0-9]+\.png)"', re.search(r"PICTURES = \{(.*?)\}", icons, re.S).group(1)))
        marks = dict(re.findall(r'"([A-Za-z]+)":\s*"([a-z0-9]+)"', re.search(r"GROUP_LOGOS = \{(.*?)\}", cards, re.S).group(1)))
        import json
        manifest = json.loads((ASSETS / "emulators.json").read_text(encoding="utf-8"))["emulators"]
        self.assertEqual(set(marks), {e.get("group") or "Multi" for e in manifest})  # every group has its own mark
        for group, name in marks.items():
            self.assertIn(name, pictures, group)
            self.assertTrue((ASSETS / "icons" / pictures[name]).is_file(), (group, name))

    def test_every_icon_path_is_balanced_svg(self):
        import re
        src = (Path(__file__).resolve().parent.parent / "distill" / "ui" / "icons.py").read_text(encoding="utf-8")
        body = re.search(r"PATHS: dict\[str, str\] = \{(.*?)\n\}\n", src, re.S).group(1)
        self.assertGreaterEqual(len(re.findall(r'^\s+"[a-z-]+":', body, re.M)), 18)
        self.assertEqual(body.count("<path") + body.count("<circle") + body.count("<rect"),
                         body.count("/>"))  # every element is self-closed



class ThemeTests(unittest.TestCase):
    def test_light_palette_is_readable_and_switches_back(self):
        try:
            from distill.ui.theme import T
        except ImportError:  # no Qt here
            self.skipTest("Qt not installed")

        def lum(c):
            return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()

        try:
            T.set_dark(False)
            self.assertGreater(lum(T.bg), 200)
            self.assertLess(lum(T.fg), 90)  # text is dark on the light page
            self.assertEqual(T.check_icon, "check.png")
            T.set_dark(True)
            self.assertLess(lum(T.bg), 60)
            self.assertGreater(lum(T.fg), 150)
            self.assertEqual(T.check_icon, "check-dark.png")
        finally:
            T.set_dark(True)


if __name__ == "__main__":
    unittest.main()
