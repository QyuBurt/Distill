import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from distill import gameimport as gi
from distill import games as gm


def entry(path, system="SNES", region="USA"):
    return SimpleNamespace(name=Path(path).name, system=system, kind="SFC", region=region, size=1, modified=1.0, folder="", path=str(path))


PLATFORM = """<?xml version="1.0" standalone="yes"?>
<LaunchBox>
  <Game>
    <ID>id-zelda</ID><Title>The Legend of Zelda: A Link to the Past</Title><Platform>Super Nintendo Entertainment System</Platform>
    <ApplicationPath>..\\Roms\\snes\\Zelda ALttP (USA).sfc</ApplicationPath><Favorite>true</Favorite><PlayCount>7</PlayCount><PlayTime>5400</PlayTime>
    <LastPlayedDate>2024-03-05T10:00:00+00:00</LastPlayedDate><StarRating>4.5</StarRating><Completed>true</Completed>
    <Developer>Nintendo EAD</Developer><Publisher>Nintendo</Publisher><ReleaseDate>1991-11-21T00:00:00-08:00</ReleaseDate><Genre>Action;Adventure</Genre>
    <Notes>Got to the Dark World.</Notes>
  </Game>
  <Game>
    <ID>id-sonic</ID><Title>Sonic the Hedgehog</Title><Platform>Sega Genesis</Platform><ApplicationPath>C:\\nowhere\\sonic.md</ApplicationPath>
    <Favorite>false</Favorite><PlayCount>2</PlayCount><PlayTime>600</PlayTime>
  </Game>
  <Game><ID>id-missing</ID><Title>Not In My Library</Title><Platform>Nintendo 64</Platform></Game>
</LaunchBox>
"""
PLAYLIST = """<?xml version="1.0" standalone="yes"?>
<LaunchBox>
  <Playlist><PlaylistId>p1</PlaylistId><Name>Best of 16-bit</Name></Playlist>
  <PlaylistGame><GameId>id-zelda</GameId><GameTitle>x</GameTitle></PlaylistGame>
  <PlaylistGame><GameId>id-sonic</GameId></PlaylistGame>
  <PlaylistGame><GameId>id-missing</GameId></PlaylistGame>
</LaunchBox>
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lb = self.root / "LaunchBox"
        (self.lb / "Data" / "Platforms").mkdir(parents=True)
        (self.lb / "Data" / "Playlists").mkdir()
        (self.lb / "Data" / "Platforms" / "Super Nintendo Entertainment System.xml").write_text(PLATFORM, encoding="utf-8-sig")
        (self.lb / "Data" / "Playlists" / "Best.xml").write_text(PLAYLIST, encoding="utf-8")
        self.roms = self.root / "Roms" / "snes"
        self.roms.mkdir(parents=True)
        self.zelda = self.roms / "Zelda ALttP (USA).sfc"
        self.store = gm.GameStore(self.root / "games.db")
        self.sonic = self.root / "elsewhere" / "Sonic the Hedgehog (USA).md"
        self.store.sync([entry(self.zelda), entry(self.sonic, "Mega Drive")], str(self.root))


class LaunchBoxTests(Fixture):
    def test_the_games_are_read_with_their_history(self):
        games = {g.title: g for g in gi.read_launchbox(self.lb)}
        z = games["The Legend of Zelda: A Link to the Past"]
        self.assertEqual((z.system, z.favorite, z.play_count, z.play_seconds, z.rating, z.status), ("SNES", True, 7, 5400.0, 5, "completed"))
        self.assertEqual((z.developer, z.publisher, z.year, z.genre), ("Nintendo EAD", "Nintendo", "1991", "Action, Adventure"))
        self.assertGreater(z.last_played, 0)
        self.assertEqual(z.collections, ["Best of 16-bit"])
        self.assertEqual(games["Sonic the Hedgehog"].system, "Mega Drive")  # 'Sega Genesis' is Distill's Mega Drive
        self.assertEqual(os.path.normpath(z.paths[0]), os.path.normpath(str(self.zelda)))  # the path was relative to LaunchBox's own folder

    def test_any_of_launchboxs_folders_is_accepted(self):
        for pick in (self.lb, self.lb / "Data", self.lb / "Data" / "Platforms"):
            self.assertEqual(len(gi.read_launchbox(pick)), 3)
        with self.assertRaises(gi.ImportProblem):
            gi.read_launchbox(self.root)

    def test_importing_brings_the_history_across_and_lists_what_is_missing(self):
        report = gi.apply(self.store, gi.read_launchbox(self.lb), "LaunchBox")
        self.assertEqual((report.seen, report.matched, report.unmatched), (3, 2, ["Not In My Library"]))
        z = next(g for g in self.store.games() if g.system == "SNES")
        self.assertEqual((z.favorite, z.play_count, z.play_seconds, z.rating, z.status, z.developer, z.year, z.notes),
                         (True, 7, 5400.0, 5, "completed", "Nintendo EAD", "1991", "Got to the Dark World."))
        s = next(g for g in self.store.games() if g.system == "Mega Drive")  # found by name: its file is somewhere else
        self.assertEqual((s.play_count, s.play_seconds, s.favorite), (2, 600.0, False))
        (_cid, name, n) = self.store.collections()[0]
        self.assertEqual((name, n), ("Best of 16-bit", 2))
        self.assertEqual(report.collections, ["Best of 16-bit"])
        self.assertIn("Not In My Library", report.summary())

    def test_importing_twice_changes_nothing_and_never_overwrites_the_players_own(self):
        z = next(g for g in self.store.games() if g.system == "SNES")
        self.store.update_details(z.id, rating=2, status="playing", developer="Me")
        self.store.record_play(z.id, time.time() + 1000)
        self.store.add_playtime(z.id, 99999)  # more than LaunchBox knew about
        first = gi.apply(self.store, gi.read_launchbox(self.lb), "LaunchBox")
        z = self.store.get(z.id)
        self.assertEqual((z.rating, z.status, z.developer), (2, "playing", "Me"))  # what the player set in Distill stays
        self.assertEqual(z.play_seconds, 99999.0)  # time only goes up
        self.assertEqual(z.play_count, 7)  # (one Distill play, LaunchBox had seven)
        self.assertEqual(first.favourites, 1)
        again = gi.apply(self.store, gi.read_launchbox(self.lb), "LaunchBox")
        self.assertEqual((again.favourites, again.playtime, again.ratings, again.statuses, again.details), (0, 0, 0, 0, 0))
        self.assertEqual(len(self.store.collections()), 1)

    def test_a_broken_platform_file_does_not_stop_the_others(self):
        (self.lb / "Data" / "Platforms" / "Broken.xml").write_text("<LaunchBox><Game>", encoding="utf-8")
        self.assertEqual(len(gi.read_launchbox(self.lb)), 3)


class PlayniteTests(Fixture):
    def export(self, data) -> Path:
        path = self.root / "playnite-games.json"
        path.write_text(json.dumps(data), encoding="utf-8-sig")
        return path

    def test_a_script_export_is_read_whatever_shape_powershell_gave_it(self):
        path = self.export([
            {"Name": "Zelda ALttP", "Platforms": "Super Nintendo Entertainment System", "Favorite": True, "PlayCount": 3, "Playtime": 3600,
             "LastActivity": "2024-02-01T09:00:00", "UserScore": 90, "CompletionStatus": "Beaten", "Categories": "JRPG", "Roms": str(self.zelda), "Notes": ""},
            {"Name": "Sonic the Hedgehog", "Platforms": ["Sega Mega Drive"], "Playtime": 120, "CompletionStatus": {"Name": "Plan to Play"}, "Categories": ["Retro", "Fast"],
             "Roms": ["{PlayniteDir}\\x.md"]},
            {"Name": ""}, "junk"])
        games = gi.read_playnite(path)
        self.assertEqual([g.title for g in games], ["Zelda ALttP", "Sonic the Hedgehog"])
        z, s = games
        self.assertEqual((z.system, z.rating, z.status, z.collections, z.favorite), ("SNES", 5, "completed", ["JRPG"], True))
        self.assertEqual((s.system, s.status, s.collections, s.paths), ("Mega Drive", "plan", ["Retro", "Fast"], []))  # a Playnite variable can't be resolved
        report = gi.apply(self.store, games, "Playnite")
        self.assertEqual((report.matched, report.favourites, sorted(report.collections)), (2, 1, ["Fast", "JRPG", "Retro"]))

    def test_a_wrapped_list_and_a_bad_file(self):
        self.assertEqual(len(gi.read_playnite(self.export({"Games": [{"Name": "A"}, {"Name": "B"}]}))), 2)
        bad = self.root / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        with self.assertRaises(gi.ImportProblem):
            gi.read_playnite(bad)
        with self.assertRaises(gi.ImportProblem):
            gi.read_playnite(self.export("text"))


class ValueTests(unittest.TestCase):
    def test_dates_stars_and_progress(self):
        self.assertEqual(gi._epoch("0001-01-01T00:00:00"), 0.0)
        self.assertGreater(gi._epoch("2024-03-05T10:00:00-05:00"), gi._epoch("2024-03-05T10:00:00+00:00") - 1)
        self.assertEqual(gi._epoch("/Date(1700000000000)/"), 1700000000.0)
        self.assertEqual([gi._stars(v) for v in (0, 0.2, 3.4, 4.5, 9)], [0, 1, 3, 5, 5])
        self.assertEqual([gi._status(t) for t in ("Completed", "Beaten", "Playing", "Plan to Play", "Abandoned", "Not Played", "On Hold", "")],
                         ["completed", "completed", "playing", "plan", "dropped", "", "", ""])

    def test_a_game_on_two_platforms_with_the_same_name_is_matched_by_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = gm.GameStore(Path(tmp) / "g.db")
            store.sync([entry("/r/Sonic.sfc", "SNES"), entry("/r/Sonic.md", "Mega Drive")], "/r")
            index = gi.Index(store.games())
            self.assertEqual(index.find(gi.Imported("Sonic", "Mega Drive")).system, "Mega Drive")
            self.assertIsNone(index.find(gi.Imported("Sonic")))  # two games and no platform: not guessed
