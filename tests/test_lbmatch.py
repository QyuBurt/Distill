"""Matching library games to LaunchBox entries: the title logic, the decision, the lookup with a fake network, the saved link, and
the art and details that follow it. No Qt and no network."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from distill import gamemedia as mm
from distill import games as gm
from distill import lbmatch as lb
from distill.lbmatch import AUTO, CONFIRMED, NONE, REVIEW


def match(title, system="SNES", year="", lb_id="1", developer="", publisher="", genre=""):
    return gm.Match(title=title, system=system, year=year, lb_id=lb_id, developer=developer, publisher=publisher, genre=genre)


def entry(path, system="SNES"):
    return SimpleNamespace(name=Path(path).name, system=system, kind="SFC", region="USA", size=1024, modified=1.0, folder="", path=str(path))


class Titles(unittest.TestCase):
    def test_tags_punctuation_and_articles_do_not_make_a_different_title(self):
        for a, b in (("Super Mario Bros. 3 (USA)", "super mario bros 3"), ("Legend of Zelda, The", "The Legend of Zelda"),
                     ("Sonic & Knuckles", "Sonic and Knuckles"), ("Don't Starve", "dont starve"), ("Metroid [!] (Rev 1)", "Metroid")):
            with self.subTest(a=a):
                self.assertEqual(lb.norm_title(a), lb.norm_title(b))

    def test_roman_numerals_after_the_first_word_are_numbers_and_a_disc_is_not_part_of_the_title(self):
        self.assertEqual(lb.norm_title("Final Fantasy VII (Disc 1)"), lb.norm_title("Final Fantasy 7"))
        self.assertEqual(lb.norm_title("Street Fighter II"), "street fighter 2")
        self.assertEqual(lb.norm_title("X-Men"), "x men")  # a leading letter is a letter, not a numeral

    def test_nothing_normalises_to_nothing(self):
        self.assertEqual(lb.norm_title(""), "")
        self.assertEqual(lb.similarity("", "Mario"), 0.0)

    def test_the_same_title_is_one_and_a_subtitle_or_another_game_is_not(self):
        self.assertEqual(lb.similarity("Super Mario Bros. 3 (USA)", "Super Mario Bros 3"), 1.0)
        self.assertLess(lb.similarity("Zelda Ocarina of Time", "Zelda Ocarina of Time Master Quest"), lb.SAME)
        self.assertGreaterEqual(lb.similarity("Zelda Ocarina of Time", "Zelda Ocarina of Time Master Quest"), lb.CLOSE)
        self.assertLess(lb.similarity("Super Metroid", "Metroid Prime"), lb.CLOSE)


class Deciding(unittest.TestCase):
    def test_one_entry_that_is_the_same_title_on_the_same_platform_is_a_match(self):
        d = lb.decide("Super Metroid (USA)", "SNES", "", [match("Super Metroid", lb_id="10")])
        self.assertEqual((d.state, d.match.lb_id), (AUTO, "10"))

    def test_the_same_title_on_other_platforms_is_not_in_the_way(self):
        got = lb.decide("Sonic", "Mega Drive", "", [match("Sonic", "Master System", lb_id="1"), match("Sonic", "Mega Drive", lb_id="2")])
        self.assertEqual((got.state, got.match.lb_id), (AUTO, "2"))

    def test_the_title_only_on_another_platform_is_left_for_the_player(self):
        d = lb.decide("Sonic", "Mega Drive", "", [match("Sonic", "Master System")])
        self.assertEqual(d.state, REVIEW)

    def test_a_platform_the_scan_could_not_place_matches_only_an_unambiguous_title(self):
        self.assertEqual(lb.decide("Metroid", gm.UNKNOWN, "", [match("Metroid", "NES")]).state, AUTO)
        two = [match("Metroid", "NES", lb_id="1"), match("Metroid", "Game Boy Advance", lb_id="2")]
        self.assertEqual(lb.decide("Metroid", gm.UNKNOWN, "", two).state, REVIEW)
        self.assertEqual(lb.decide("Metroid", "", "", two).state, REVIEW)

    def test_an_entry_whose_platform_could_not_be_read_counts_as_neutral(self):
        self.assertEqual(lb.decide("Metroid", "NES", "", [match("Metroid", "")]).state, AUTO)

    def test_the_same_title_twice_on_one_platform_needs_the_player_unless_the_year_tells_them_apart(self):
        twice = [match("Tetris", "NES", "1988", "1"), match("Tetris", "NES", "1989", "2")]
        self.assertEqual(lb.decide("Tetris", "NES", "", twice).state, REVIEW)
        got = lb.decide("Tetris", "NES", "1989", twice)
        self.assertEqual((got.state, got.match.lb_id), (AUTO, "2"))

    def test_a_close_title_is_offered_not_taken(self):
        d = lb.decide("Zelda Ocarina of Time", "N64", "", [match("Zelda Ocarina of Time Master Quest", "N64")])
        self.assertEqual(d.state, REVIEW)
        self.assertEqual(len(d.others), 1)

    def test_nothing_alike_is_none(self):
        self.assertEqual(lb.decide("Super Metroid", "SNES", "", [match("Barbie Horse Adventures")]).state, NONE)
        self.assertEqual(lb.decide("Super Metroid", "SNES", "", []).state, NONE)

    def test_the_candidates_a_review_would_list_come_best_first(self):
        d = lb.decide("Super Metroid", "SNES", "", [match("Super Metroid Redux", lb_id="2"), match("Super Metroid Rotation", lb_id="3")])
        self.assertEqual(d.state, REVIEW)
        self.assertEqual(len(d.others), 2)


class Lookup(unittest.TestCase):
    """ensure_match, against a real games.db and a fake LaunchBox."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = gm.GameStore(self.tmp / "games.db")
        self.store.sync([entry("/r/Super Metroid (USA).sfc")], "/r")
        self.game = self.store.games()[0]
        self.searches, self.reads = [], []
        patcher = mock.patch.object(mm, "MEDIA_DIR", self.tmp / "media")  # the art's id memo goes here, not into the real folder
        patcher.start()
        self.addCleanup(patcher.stop)

    def search_for(self, ids, slugs):
        def search(title, system):
            self.searches.append((title, system))
            return ids, slugs
        return search

    def read_from(self, entries):
        def read(lb_id, slug):
            self.reads.append(lb_id)
            return entries.get(lb_id)
        return read

    def run_match(self, ids, slugs, entries, **kw):
        return lb.ensure_match(self.game, self.store, search=self.search_for(ids, slugs), read=self.read_from(entries), **kw)

    def test_a_clear_match_is_linked_saved_and_fills_only_what_is_empty(self):
        self.store.update_details(self.game.id, developer="Mine")  # the player typed this
        self.game = self.store.get(self.game.id)
        changed = self.run_match(["10"], {"10": "super-metroid"},
                                 {"10": match("Super Metroid", "SNES", "1994", "10", developer="Nintendo R&D2", publisher="Nintendo", genre="Action")})
        self.assertTrue(changed)
        saved = self.store.get(self.game.id)
        self.assertEqual((saved.lb_id, saved.lb_match), ("10", AUTO))
        self.assertEqual((saved.developer, saved.publisher, saved.year, saved.genre), ("Mine", "Nintendo", "1994", "Action"))
        self.assertEqual((self.game.lb_id, self.game.publisher), ("10", "Nintendo"))  # the object the page holds is updated too

    def test_most_games_cost_one_page_and_the_entry_that_reads_like_the_title_is_read_first(self):
        self.run_match(["11", "10"], {"11": "super-metroid-hyper", "10": "super-metroid"},
                       {"10": match("Super Metroid", "SNES", lb_id="10"), "11": match("Super Metroid Hyper", "SNES", lb_id="11")})
        self.assertEqual(self.reads, ["10"])

    def test_an_entry_that_is_plainly_another_game_is_not_read(self):
        self.run_match(["20", "10"], {"20": "metroid-prime", "10": "super-metroid"},
                       {"10": match("Super Metroid", "SNES", lb_id="10"), "20": match("Metroid Prime", "GameCube", lb_id="20")})
        self.assertNotIn("20", self.reads)

    def test_a_title_nothing_like_it_is_none_even_if_the_search_returned_something(self):
        self.run_match(["20"], {"20": "metroid-prime"}, {"20": match("Metroid Prime", "GameCube", lb_id="20")})
        self.assertEqual(self.store.get(self.game.id).lb_match, NONE)  # another game on another platform: not even offered

    def test_a_game_known_only_under_another_name_is_still_offered(self):
        self.store.sync([entry("/r/Rockman X (Japan).sfc")], "/r")
        rock = next(g for g in self.store.games() if "Rockman" in g.name)
        lb.ensure_match(rock, self.store, search=self.search_for(["30"], {"30": "mega-man-x"}),
                        read=self.read_from({"30": match("Mega Man X", "SNES", lb_id="30")}))
        self.assertEqual(self.store.get(rock.id).lb_match, REVIEW)  # the search's own best is read, and it is only a possible

    def test_two_equally_good_entries_wait_for_the_player(self):
        self.run_match(["10", "12"], {"10": "super-metroid", "12": "super-metroid-2"},
                       {"10": match("Super Metroid", "SNES", "1994", "10"), "12": match("Super Metroid", "SNES", "1994", "12")})
        saved = self.store.get(self.game.id)
        self.assertEqual((saved.lb_id, saved.lb_match), ("", REVIEW))

    def test_a_right_title_on_the_wrong_platform_does_not_end_the_search(self):
        self.run_match(["9", "10"], {"9": "super-metroid", "10": "super-metroid"},
                       {"9": match("Super Metroid", "GameCube", lb_id="9"), "10": match("Super Metroid", "SNES", lb_id="10")})
        self.assertEqual(self.store.get(self.game.id).lb_id, "10")

    def test_nothing_found_is_remembered_for_a_week_and_then_tried_again(self):
        self.assertTrue(self.run_match([], {}, {}, now=lambda: 1000.0))
        self.assertEqual(self.store.get(self.game.id).lb_match, NONE)
        self.assertFalse(self.run_match([], {}, {}, now=lambda: 1000.0 + 3600))
        self.assertEqual(len(self.searches), 1)  # an hour later it does not ask again
        self.game = self.store.get(self.game.id)
        self.assertTrue(self.run_match([], {}, {}, now=lambda: 1000.0 + lb.RETRY_NONE + 1))
        self.assertEqual(len(self.searches), 2)

    def test_when_launchbox_cannot_be_reached_nothing_is_recorded(self):
        def offline(title, system):
            raise gm.GameError("no connection")
        self.assertFalse(lb.ensure_match(self.game, self.store, search=offline, read=self.read_from({})))
        self.assertEqual(self.store.get(self.game.id).lb_match, "")

    def test_entries_that_could_not_be_read_are_not_an_answer(self):
        self.assertFalse(self.run_match(["10"], {"10": "super-metroid"}, {}))  # read() gives None
        self.assertEqual(self.store.get(self.game.id).lb_match, "")

    def test_a_saved_answer_is_not_asked_for_again_unless_forced(self):
        entries = {"10": match("Super Metroid", "SNES", lb_id="10")}
        self.run_match(["10"], {"10": "super-metroid"}, entries)
        self.game = self.store.get(self.game.id)
        self.assertFalse(self.run_match(["10"], {"10": "super-metroid"}, entries))
        self.assertEqual(len(self.searches), 1)
        self.assertTrue(self.run_match(["10"], {"10": "super-metroid"}, entries, force=True))
        self.assertEqual(len(self.searches), 2)

    def test_a_review_waits_for_the_player_and_a_confirmed_choice_is_kept(self):
        self.assertFalse(lb.needs_match(SimpleNamespace(name="X", lb_match=REVIEW, lb_tried=0.0)))
        self.assertFalse(lb.needs_match(SimpleNamespace(name="X", lb_match=CONFIRMED, lb_tried=0.0)))
        self.assertTrue(lb.needs_match(SimpleNamespace(name="X", lb_match="", lb_tried=0.0)))
        self.assertFalse(lb.needs_match(SimpleNamespace(name="  ", lb_match="", lb_tried=0.0)))  # no name, nothing to search

    def test_a_match_points_the_art_at_the_entry_without_another_search(self):
        self.run_match(["10"], {"10": "super-metroid"}, {"10": match("Super Metroid", "SNES", lb_id="10")})
        platform = gm.LB_PLATFORM_NAMES.get("SNES", "SNES")
        with mock.patch.object(mm, "lb_candidates", side_effect=AssertionError("the art searched again")):
            self.assertEqual(mm.lb_game_id(self.game.name, platform), "10")

    def test_a_game_the_scan_could_not_place_takes_the_entrys_platform_and_keeps_the_link(self):
        self.store.sync([entry("/r/Chrono Trigger.sfc", system=gm.UNKNOWN)], "/r")
        game = next(g for g in self.store.games() if g.name.startswith("Chrono"))
        lb.ensure_match(game, self.store, search=self.search_for(["50"], {"50": "chrono-trigger"}),
                        read=self.read_from({"50": match("Chrono Trigger", "SNES", "1995", "50")}))
        saved = self.store.get(game.id)
        self.assertEqual((saved.system, saved.lb_id, saved.lb_match), ("SNES", "50", AUTO))  # placing it did not wipe its link

    def test_a_game_already_placed_keeps_its_platform(self):
        self.run_match(["10"], {"10": "super-metroid"}, {"10": match("Super Metroid", "SNES", lb_id="10")})
        self.assertEqual(self.store.get(self.game.id).system, "SNES")

    def test_a_choice_the_player_made_is_never_looked_up_again_not_even_when_forced(self):
        lb.confirm(self.game, self.store, match("Super Metroid", "SNES", "1994", "77"))
        self.game = self.store.get(self.game.id)
        self.assertFalse(self.run_match(["10"], {"10": "super-metroid"}, {"10": match("Super Metroid", "SNES", lb_id="10")}, force=True))
        self.assertEqual((self.store.get(self.game.id).lb_id, self.searches), ("77", []))

    def test_a_caller_that_wants_to_stop_when_launchbox_is_out_of_reach_can_ask_to_hear_of_it(self):
        def offline(title, system):
            raise gm.GameError("no connection")
        with self.assertRaises(gm.GameError):
            lb.ensure_match(self.game, self.store, strict=True, search=offline, read=self.read_from({}))

    def test_the_same_title_twice_is_found_by_reading_the_second_page(self):
        # two entries whose addresses read almost alike: the first would have been taken had the second not been read
        self.run_match(["10", "12"], {"10": "super-metroid", "12": "super-metroid-2"},
                       {"10": match("Super Metroid", "SNES", "1994", "10"), "12": match("Super Metroid", "SNES", "1994", "12")})
        self.assertEqual(self.store.get(self.game.id).lb_match, REVIEW)
        self.assertEqual(sorted(self.reads), ["10", "12"])

    def test_the_player_picking_an_entry_confirms_it(self):
        lb.confirm(self.game, self.store, match("Super Metroid", "SNES", "1994", "77", developer="Nintendo"))
        saved = self.store.get(self.game.id)
        self.assertEqual((saved.lb_id, saved.lb_match, saved.developer), ("77", CONFIRMED, "Nintendo"))


class SavedLink(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "games.db"
        self.store = gm.GameStore(self.path)
        self.store.sync([entry("/r/Metroid.sfc")], "/r")
        self.gid = self.store.games()[0].id

    def test_a_link_survives_a_rescan(self):
        self.store.set_match(self.gid, "5", AUTO, 100.0)
        self.store.sync([entry("/r/Metroid.sfc"), entry("/r/Zelda.sfc")], "/r")
        kept = self.store.get(self.gid)
        self.assertEqual((kept.lb_id, kept.lb_match, kept.lb_tried), ("5", AUTO, 100.0))

    def test_a_new_name_clears_a_guessed_link_but_not_one_the_player_chose(self):
        self.store.set_match(self.gid, "5", AUTO, 1.0)
        self.store.update_details(self.gid, title="Metroid Redux")
        self.assertEqual((self.store.get(self.gid).lb_id, self.store.get(self.gid).lb_match), ("", ""))
        self.store.set_match(self.gid, "6", CONFIRMED, 1.0)
        self.store.update_details(self.gid, title="Metroid Remix")
        self.assertEqual((self.store.get(self.gid).lb_id, self.store.get(self.gid).lb_match), ("6", CONFIRMED))

    def test_saving_the_edit_window_without_renaming_does_not_forget_the_link(self):
        self.store.update_details(self.gid, title="Metroid Redux")
        self.store.set_match(self.gid, "5", AUTO, 1.0)
        self.store.update_details(self.gid, title="Metroid Redux", developer="Nintendo")  # the same name again, as the window sends it
        self.assertEqual(self.store.get(self.gid).lb_id, "5")

    def test_a_new_platform_clears_a_guessed_link(self):
        self.store.set_match(self.gid, "5", AUTO, 1.0)
        self.store.update_details(self.gid, system="NES")
        self.assertEqual(self.store.get(self.gid).lb_id, "")

    def test_a_database_from_before_this_release_gains_the_columns_and_keeps_its_games(self):
        self.store.set_match(self.gid, "5", AUTO, 1.0)
        raw = sqlite3.connect(self.path)
        try:
            for col in ("lb_id", "lb_match", "lb_tried"):
                raw.execute(f"ALTER TABLE user DROP COLUMN {col}")
            raw.commit()
        except sqlite3.OperationalError:
            raw.close()
            self.skipTest("this SQLite cannot drop a column")
        raw.close()
        reopened = gm.GameStore(self.path)  # opening migrates it
        old = reopened.get(self.gid)
        self.assertEqual((old.name, old.lb_id, old.lb_match), ("Metroid", "", ""))
        reopened.set_match(self.gid, "8", AUTO, 2.0)
        self.assertEqual(reopened.get(self.gid).lb_id, "8")


class OpeningAMatchedGame(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = gm.GameStore(Path(tmp.name) / "games.db")
        self.store.sync([entry("/r/Metroid.sfc")], "/r")
        self.game = self.store.games()[0]

    def test_a_matched_game_reads_its_entry_directly_with_no_search(self):
        self.store.set_match(self.game.id, "42", AUTO, 1.0)
        game = self.store.get(self.game.id)
        asked = []
        with mock.patch.object(rc_get(), "_get", side_effect=lambda url, **k: asked.append(url) or b"<page/>"), \
                mock.patch.object(gm, "parse_launchbox_details", return_value=gm.Details(overview="A hunter wakes.")), \
                mock.patch.object(gm, "lb_candidates", side_effect=AssertionError("searched")):
            got = gm.launchbox_details(game)
        self.assertEqual(got.overview, "A hunter wakes.")
        self.assertTrue(asked[0].endswith("42"))

    def test_an_unmatched_game_still_searches_as_before(self):
        asked = []
        with mock.patch.object(rc_get(), "_get", side_effect=lambda url, **k: asked.append(url) or b""):
            gm.launchbox_details(self.game)
        self.assertTrue(any(url.startswith(rc_get()._LB_SEARCH) for url in asked))  # it searched for the title, as it always did


def rc_get():
    from distill import recomps
    return recomps


if __name__ == "__main__":
    unittest.main()
