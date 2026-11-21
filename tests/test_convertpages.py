"""The Convert hub lists exactly the converter pages the sidebar has. No Qt."""
from __future__ import annotations

import unittest

from distill.convertpages import DEFAULT_UNPINNED, GROUPS, IN_OUT, TOOL_OF, all_tool_keys, organise_keys, page_keys
from distill.ui.nav import NAV_GROUPS, PAGE_CHIP, PAGE_DETAIL, PAGE_INFO


class Hub(unittest.TestCase):
    def converter_keys(self):
        items = dict(NAV_GROUPS)["CONVERT & COMPRESS"]
        return [key for key, _text in items if key != "convert"]

    def test_the_hub_opens_every_converter_page_once(self):
        self.assertEqual(sorted(page_keys()), sorted(self.converter_keys()))
        self.assertEqual(len(page_keys()), len(set(page_keys())))

    def test_every_card_says_what_goes_in_and_what_comes_out(self):
        for key in page_keys():
            self.assertTrue(IN_OUT[key], key)
        self.assertEqual(set(IN_OUT), set(page_keys()))

    def test_every_page_names_the_tool_it_needs(self):
        self.assertEqual(set(TOOL_OF), set(page_keys()))
        self.assertEqual(len(set(TOOL_OF.values())), len(TOOL_OF), "one tool each")

    def test_every_group_has_a_title_and_pages(self):
        for title, items in GROUPS:
            self.assertTrue(title and items)

    def test_the_hub_has_a_sidebar_group_of_its_own_and_is_described(self):
        self.assertEqual(dict(NAV_GROUPS)["TOOLS"], (("convert", "All Tools"),))
        self.assertTrue(PAGE_INFO["convert"][0] and PAGE_INFO["convert"][1])
        self.assertTrue(PAGE_DETAIL["convert"] and PAGE_CHIP["convert"])


class Pins(unittest.TestCase):
    def test_the_organisers_are_the_organise_group_in_the_sidebar_order(self):
        self.assertEqual(organise_keys(), [key for key, _text in dict(NAV_GROUPS)["ORGANISE LIBRARY"]])

    def test_the_hub_opens_every_converter_and_every_organiser(self):
        self.assertEqual(all_tool_keys(), page_keys() + organise_keys())
        self.assertEqual(len(all_tool_keys()), 14)

    def test_a_fresh_install_pins_the_hub_and_the_other_pages_but_none_of_the_tools(self):
        self.assertEqual(DEFAULT_UNPINNED, frozenset(all_tool_keys()))
        for key in ("convert", "games", "recomps", "emus", "dl", "sound"):
            self.assertNotIn(key, DEFAULT_UNPINNED, key)

    def test_every_unpinned_page_is_still_a_real_page_the_hub_can_open(self):
        keys = {key for _group, items in NAV_GROUPS for key, _text in items}
        self.assertLessEqual(DEFAULT_UNPINNED, keys)
        for key in DEFAULT_UNPINNED:
            self.assertTrue(PAGE_INFO[key][0] and PAGE_INFO[key][1], key)  # its card has a title and a line to show


if __name__ == "__main__":
    unittest.main()
