import json
import unittest

from distill import ldn
from distill import recomps as rc

GAMES = {
    "a": {"id": "a", "player_count": 3, "max_player_count": 4, "game_name": "Puyo Puyo Tetris", "title_id": "010053d0001be000",
          "mode": "P2P", "status": "Joinable", "players": ["x", "y", "z"]},
    "b": {"id": "b", "player_count": 2, "max_player_count": 8, "game_name": "Super Smash Bros. Ultimate", "title_id": "01006A800016E000",
          "mode": "P2P", "status": "Joinable", "players": ["p", "q"]},
    "c": {"id": "c", "player_count": 1, "max_player_count": 4, "game_name": "Puyo Puyo Tetris", "title_id": "010053d0001be000",
          "mode": "Proxy", "status": "Joinable", "players": ["r"]},
}
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
    def test_reads_lobbies_and_stats(self):
        snap = ldn.fetch(fake({ldn.GAMES_URL: GAMES, ldn.STATS_URL: STATS}))
        self.assertEqual([x.id for x in snap.lobbies], ["a", "b", "c"])
        self.assertEqual(snap.lobbies[0].title_id, "010053D0001BE000")
        self.assertEqual((snap.stats.total_players, snap.stats.private_players, snap.stats.public_games), (10, 3, 3))

    def test_groups_by_game_busiest_first(self):
        snap = ldn.fetch(fake({ldn.GAMES_URL: GAMES, ldn.STATS_URL: STATS}))
        self.assertEqual(snap.by_game(), [("Puyo Puyo Tetris", 2, 4), ("Super Smash Bros. Ultimate", 1, 2)])

    def test_works_out_counts_when_stats_are_missing(self):
        snap = ldn.fetch(fake({ldn.GAMES_URL: list(GAMES.values()), ldn.STATS_URL: OSError("down")}))
        self.assertEqual((snap.stats.total_players, snap.stats.public_games), (6, 3))

    def test_empty_and_odd_data(self):
        self.assertEqual(ldn.fetch(fake({ldn.GAMES_URL: {}, ldn.STATS_URL: {}})).lobbies, ())
        lob = ldn.parse_lobbies([{"title_id": "0100abc", "players": ["a", "b"]}, "junk", {"game_name": "X", "player_count": "n/a"}])
        self.assertEqual([(x.name, x.players) for x in lob], [("0100ABC", 2), ("X", 0)])

    def test_server_down_is_a_readable_error(self):
        with self.assertRaises(rc.RecompError):
            ldn.fetch(fake({ldn.GAMES_URL: OSError("refused")}))

    def test_player_names_are_not_kept(self):
        self.assertFalse(any("x" == v for lob in ldn.parse_lobbies(GAMES) for v in vars(lob).values()))


if __name__ == "__main__":
    unittest.main()
