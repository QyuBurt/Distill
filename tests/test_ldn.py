import json
import unittest

from distill import ldn
from distill import recomps as rc

# shaped like the live server's answer
GAMES = [
    {"id": "a", "is_public": True, "player_count": 3, "max_player_count": 4, "game_name": "Puyo Puyo Tetris", "title_id": "010053d0001be000",
     "title_version": "1.0.1", "mode": "P2P", "status": "Joinable", "scene_id": 1, "players": ["Ann", "Bo", "Cy"], "created_at": 1794011370853},
    {"id": "b", "is_public": False, "player_count": 2, "max_player_count": 8, "game_name": "Super Smash Bros.\u2122 Ultimate", "title_id": "01006A800016E000",
     "title_version": "13.0.4", "mode": "Master Server Proxy", "status": "Joinable", "scene_id": 1, "players": ["OliOli", "Kaicuno"], "created_at": 1794011370853},
    {"id": "c", "is_public": True, "player_count": 4, "max_player_count": 4, "game_name": "Puyo Puyo Tetris", "title_id": "010053d0001be000",
     "title_version": "1.0.0", "mode": "P2P", "status": "Joinable", "players": ["D", "E", "F", "G"], "created_at": 0},
]
STATS = {"total_game_count": 4, "private_game_count": 1, "public_game_count": 3, "in_progress_count": 0, "master_proxy_count": 0,
         "total_player_count": 10, "private_player_count": 3, "public_player_count": 7}


def fake(routes):
    def get(url, **_kw):
        v = routes[url]
        if isinstance(v, Exception):
            raise v
        return json.dumps(v).encode()
    return get


class LdnTests(unittest.TestCase):
    def snap(self, stats=STATS):
        return ldn.fetch(fake({ldn.GAMES_URL: GAMES, ldn.STATS_URL: stats}))

    def test_reads_every_field_of_a_lobby(self):
        by = {x.id: x for x in self.snap().lobbies}
        a = by["a"]
        self.assertEqual((a.name, a.title_id, a.version, a.players, a.max_players, a.mode, a.status),
                         ("Puyo Puyo Tetris", "010053D0001BE000", "1.0.1", 3, 4, "P2P", "Joinable"))
        self.assertEqual(a.names, ("Ann", "Bo", "Cy"))
        self.assertEqual(a.created, 1794011370.853)  # milliseconds in, seconds out
        self.assertEqual(self.snap().stats.total_players, 10)

    def test_busiest_first(self):
        self.assertEqual([x.id for x in self.snap().lobbies], ["c", "a", "b"])

    def test_a_private_lobby_keeps_its_count_but_not_its_names(self):
        b = {x.id: x for x in self.snap().lobbies}["b"]
        self.assertFalse(b.public)
        self.assertEqual((b.players, b.names), (2, ()))
        self.assertNotIn("OliOli", repr(self.snap()))

    def test_full_lobbies_are_not_joinable(self):
        by = {x.id: x for x in self.snap().lobbies}
        self.assertTrue(by["a"].joinable)
        self.assertFalse(by["c"].joinable)  # 4/4

    def test_works_out_counts_when_stats_are_missing(self):
        snap = ldn.fetch(fake({ldn.GAMES_URL: GAMES, ldn.STATS_URL: OSError("down")}))
        self.assertEqual((snap.stats.total_players, snap.stats.public_players, snap.stats.private_players), (9, 7, 2))
        self.assertEqual((snap.stats.public_games, snap.stats.private_games), (2, 1))

    def test_empty_and_odd_data(self):
        self.assertEqual(ldn.fetch(fake({ldn.GAMES_URL: {}, ldn.STATS_URL: {}})).lobbies, ())
        lob = ldn.parse_lobbies([{"title_id": "0100abc", "players": ["a", "b"]}, "junk", {"game_name": "X", "player_count": "n/a"}])
        self.assertEqual([(x.name, x.players, x.public) for x in lob], [("0100ABC", 2, True), ("X", 0, True)])

    def test_server_down_is_a_readable_error(self):
        with self.assertRaises(rc.RecompError):
            ldn.fetch(fake({ldn.GAMES_URL: OSError("refused")}))

    def test_age_text(self):
        now = 10_000.0
        self.assertEqual(ldn.age_text(0, now), "")
        self.assertEqual(ldn.age_text(now - 20, now), "just now")
        self.assertEqual(ldn.age_text(now - 12 * 60, now), "12m")
        self.assertEqual(ldn.age_text(now - 3 * 3600 - 5 * 60, now), "3h 05m")
        self.assertEqual(ldn.age_text(now - 2 * 86400, now), "2d")


if __name__ == "__main__":
    unittest.main()
