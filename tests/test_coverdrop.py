import tempfile
import unittest
from pathlib import Path
from unittest import mock

from distill import coverdrop as cd
from distill import games as gm

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def game(gid, title, system="PlayStation 2", path=""):
    return gm.Game(id=gid, title=title, system=system, path=path or f"/roms/{title}.iso")


class CoverDropTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def pics(self, folder, *names):
        d = self.tmp / folder
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(PNG)
        return d

    def test_a_folder_named_for_a_platform_is_one_set(self):
        d = self.pics("PS2", "A.png", "B.jpg", "notes.txt")
        sets = cd.plan([d], ["PlayStation 2"])
        self.assertEqual([(s.platform, [p.name for p in s.files]) for s in sets], [("PlayStation 2", ["A.png", "B.jpg"])])

    def test_a_folder_of_platform_folders_is_a_set_each(self):
        self.pics("all/PS2", "A.png")
        self.pics("all/Dreamcast", "B.png")
        sets = cd.plan([self.tmp / "all"])
        self.assertEqual({s.platform for s in sets}, {"PlayStation 2", "Dreamcast"})

    def test_an_unnamed_folder_has_no_platform_and_no_pictures_no_set(self):
        d = self.pics("my art", "A.png")
        self.assertEqual(cd.plan([d])[0].platform, "")
        self.assertEqual(cd.plan([self.tmp / "empty"]), [])

    def test_titles_match_past_tags_punctuation_and_launchbox_suffixes(self):
        d = self.pics("PS2", "Final Fantasy X-01.png", "Pokémon Colosseum.png", "Gran Turismo 4 - Front.jpg", "Zelda.png")
        games = [game("1", "Final Fantasy X (USA)"), game("2", "Pokemon Colosseum"), game("3", "Gran Turismo 4"), game("4", "Other")]
        m = cd.match(cd._pictures(d), games)
        self.assertEqual(sorted(g.id for g, _ in m.pairs), ["1", "2", "3"])
        self.assertEqual([p.name for p in m.unmatched], ["Zelda.png"])

    def test_every_version_of_a_game_gets_the_picture_and_the_plain_name_wins(self):
        d = self.pics("PS2", "Tekken.png", "Tekken-02.png")
        m = cd.match(cd._pictures(d), [game("1", "Tekken (USA)"), game("2", "Tekken (Europe)")])
        self.assertEqual([(g.id, p.name) for g, p in m.pairs], [("1", "Tekken.png"), ("2", "Tekken.png")])
        self.assertEqual(m.unmatched, [])

    def test_a_picture_matches_the_games_file_name(self):
        d = self.pics("PS2", "SLUS-123 Racer.png")
        m = cd.match(cd._pictures(d), [game("1", "Racer", path="/roms/SLUS-123 Racer.iso")])
        self.assertEqual(len(m.pairs), 1)

    def test_apply_sets_covers_and_reports_failures(self):
        d = self.pics("PS2", "A.png")
        big = d / "B.png"
        big.write_bytes(PNG + b"\0" * ((6 << 20) + 1))
        with mock.patch.object(cd.rc, "set_cover") as setter:
            done, failed = cd.apply([(game("1", "A"), d / "A.png"), (game("2", "B"), big)])
        self.assertEqual(done, ["1"])
        self.assertEqual(len(failed), 1)
        setter.assert_called_once()


if __name__ == "__main__":
    unittest.main()
