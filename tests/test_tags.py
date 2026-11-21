"""A game's tags (co-op, couch, kids): tidying, saving, merging, bulk changes, searching and filtering."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import helpers  # noqa: F401
from distill import games as gm


def rom(root: Path, name: str, system: str = "SNES", size: int = 10):
    return SimpleNamespace(name=name, system=system, kind="SFC", region="", size=size, modified=1.0, folder="", path=str(root / name))


class CleanTagsTests(unittest.TestCase):
    def test_commas_semicolons_and_new_lines_separate(self):
        self.assertEqual(gm.clean_tags("co-op, couch; kids\nretro"), "co-op|couch|kids|retro")

    def test_spaces_are_tidied_and_empty_ones_dropped(self):
        self.assertEqual(gm.clean_tags("  co-op ,, split   screen ,"), "co-op|split screen")

    def test_a_tag_written_twice_is_kept_once_with_its_first_spelling(self):
        self.assertEqual(gm.clean_tags("Co-op, co-op, CO-OP, kids"), "Co-op|kids")

    def test_the_pipe_that_tags_are_stored_with_cannot_be_part_of_one(self):
        self.assertEqual(gm.clean_tags("a|b"), "a|b")
        self.assertEqual(gm.split_tags(gm.clean_tags("a|b")), ["a", "b"])

    def test_a_list_can_be_given_instead(self):
        self.assertEqual(gm.clean_tags(["co-op, kids", "Kids", "couch"]), "co-op|kids|couch")

    def test_limits(self):
        long = "x" * 100
        self.assertEqual(len(gm.split_tags(gm.clean_tags(long))[0]), gm.MAX_TAG_LEN)
        many = ",".join(f"t{i}" for i in range(100))
        self.assertEqual(len(gm.split_tags(gm.clean_tags(many))), gm.MAX_TAGS)

    def test_nothing_is_nothing(self):
        self.assertEqual(gm.clean_tags(""), "")
        self.assertEqual(gm.split_tags(""), [])
        self.assertEqual(gm.split_tags(None), [])  # type: ignore[arg-type]


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = gm.GameStore(self.root / "games.db")
        self.store.sync([rom(self.root, "Metroid.sfc"), rom(self.root, "Zelda.sfc"), rom(self.root, "Kirby.sfc")], str(self.root))
        self.ids = {g.title: g.id for g in self.store.games()}

    def tags(self, title: str) -> list[str]:
        return gm.split_tags(self.store.get(self.ids[title]).tags)

    def test_a_new_game_has_no_tags(self):
        self.assertEqual(self.tags("Metroid"), [])

    def test_tags_are_saved_with_the_details_and_tidied(self):
        self.store.update_details(self.ids["Metroid"], tags=" co-op , Couch,, co-op ")
        self.assertEqual(self.tags("Metroid"), ["co-op", "Couch"])
        self.store.update_details(self.ids["Metroid"], rating=3)  # (another field: the tags are left alone)
        self.assertEqual(self.tags("Metroid"), ["co-op", "Couch"])
        self.store.update_details(self.ids["Metroid"], tags="")
        self.assertEqual(self.tags("Metroid"), [])

    def test_add_and_remove_for_many_games(self):
        everyone = list(self.ids.values())
        self.assertEqual(self.store.tag_many(everyone, add="kids"), 3)
        self.assertEqual(self.store.tag_many(everyone, add="Kids"), 0)  # (already there: nothing changed)
        self.store.tag_many([self.ids["Zelda"]], add="co-op, couch")
        self.assertEqual(self.tags("Zelda"), ["kids", "co-op", "couch"])
        self.assertEqual(self.store.tag_many(everyone, remove="KIDS"), 3)
        self.assertEqual(self.tags("Zelda"), ["co-op", "couch"])
        self.assertEqual(self.tags("Metroid"), [])

    def test_removing_what_is_not_there_changes_nothing(self):
        self.assertEqual(self.store.tag_many(self.ids.values(), remove="nothing"), 0)

    def test_search_finds_tags(self):
        self.store.update_details(self.ids["Kirby"], tags="split screen")
        self.assertEqual([g.title for g in self.store.games(query="split")], ["Kirby"])

    def test_the_tag_filter_matches_exactly_without_regard_to_case(self):
        self.store.update_details(self.ids["Zelda"], tags="co-op, couch")
        self.store.update_details(self.ids["Kirby"], tags="co-operative")
        self.assertEqual([g.title for g in self.store.games(filters={"tag": "CO-OP"})], ["Zelda"])
        self.assertEqual(self.store.games(filters={"tag": "none"}), [])

    def test_the_filter_menu_offers_each_tag_with_its_count(self):
        self.store.tag_many(self.ids.values(), add="kids")
        self.store.tag_many([self.ids["Zelda"]], add="co-op")
        self.assertEqual(self.store.facets()["tag"], [("kids", 3), ("co-op", 1)])

    def test_tags_survive_a_rescan_and_a_restart(self):
        self.store.update_details(self.ids["Metroid"], tags="co-op")
        self.store.sync([rom(self.root, "Metroid.sfc"), rom(self.root, "Zelda.sfc"), rom(self.root, "Kirby.sfc")], str(self.root))
        again = gm.GameStore(self.root / "games.db")
        self.assertEqual(gm.split_tags(again.get(self.ids["Metroid"]).tags), ["co-op"])

    def test_a_library_from_before_tags_gets_the_column(self):
        old = self.root / "old.db"
        db = sqlite3.connect(old)
        before = gm._SCHEMA.replace(", tags TEXT DEFAULT ''", "")  # the library as the last release made it
        self.assertNotIn("tags", before)
        db.executescript(before)
        db.commit()
        db.close()
        store = gm.GameStore(old)  # opening it adds the column
        store.sync([rom(self.root, "Old.sfc")], str(self.root))
        gid = store.games()[0].id
        store.update_details(gid, tags="retro")
        self.assertEqual(gm.split_tags(store.get(gid).tags), ["retro"])


class FilterTests(unittest.TestCase):
    def test_matches_filters_on_a_tag(self):
        g = gm.Game(id="1", title="x", system="SNES", path="/x", tags="co-op|couch")
        self.assertTrue(gm.matches_filters(g, {"tag": "couch"}))
        self.assertTrue(gm.matches_filters(g, {"tag": "COUCH"}))
        self.assertFalse(gm.matches_filters(g, {"tag": "kids"}))
        self.assertTrue(gm.matches_filters(g, {"tag": ""}))  # (an empty filter doesn't count)


if __name__ == "__main__":
    unittest.main()
