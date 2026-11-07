"""Ryujinx (Ryubing) local-wireless lobbies: who is playing what right now, read from the LDN server's public stats (ldn.ryujinx.app).
No Qt in here. The server only publishes public lobbies; private ones show up as counts, never as names.
Shapes follow Ryubing/LdnServer's README: a stats object (`total_player_count`, `public_game_count`, ...) and a games object keyed by lobby id
(`game_name`, `title_id`, `player_count`, `max_player_count`, `mode`, `status`). Lobby members' names are deliberately not kept."""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import recomps as rc

BASE = "https://ldn.ryujinx.app"
GAMES_URL = BASE + "/api/public_games"  # one entry per public lobby
STATS_URL = BASE + "/api/ldn"  # whole-server counts; optional, the games list alone is enough to show something
EMULATOR_ID = "ryujinx"
REFRESH_SECONDS = 30


@dataclass(frozen=True)
class Lobby:
    id: str
    name: str
    title_id: str
    players: int
    max_players: int
    mode: str
    status: str


@dataclass(frozen=True)
class Stats:
    total_players: int = 0
    public_players: int = 0
    private_players: int = 0
    total_games: int = 0
    public_games: int = 0
    private_games: int = 0
    in_progress: int = 0
    on_proxy: int = 0


@dataclass(frozen=True)
class Snapshot:
    stats: Stats
    lobbies: tuple[Lobby, ...]

    def by_game(self) -> list[tuple[str, int, int]]:
        """(game, lobbies, players), busiest first: the answer to \"what can I join\"."""
        acc: dict[str, list[int]] = {}
        for lob in self.lobbies:
            row = acc.setdefault(lob.name, [0, 0])
            row[0] += 1
            row[1] += lob.players
        return sorted(((n, a, b) for n, (a, b) in acc.items()), key=lambda r: (-r[2], -r[1], r[0].lower()))


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def parse_lobbies(data) -> tuple[Lobby, ...]:
    items = list(data.values()) if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for i, d in enumerate(items):
        if not isinstance(d, dict):
            continue
        title = str(d.get("title_id") or "").strip().upper()
        name = str(d.get("game_name") or "").strip() or title or "Unknown game"
        players = d.get("player_count")
        if players is None and isinstance(d.get("players"), list):
            players = len(d["players"])  # names are counted, not stored
        out.append(Lobby(str(d.get("id") or i), name, title, _int(players), _int(d.get("max_player_count")),
                         str(d.get("mode") or ""), str(d.get("status") or "")))
    out.sort(key=lambda x: (-x.players, x.name.lower()))
    return tuple(out)


def parse_stats(data, lobbies: tuple[Lobby, ...] = ()) -> Stats:
    """The server's counts when it gave some, else worked out from the public lobbies."""
    if isinstance(data, dict) and data:
        return Stats(_int(data.get("total_player_count")), _int(data.get("public_player_count")), _int(data.get("private_player_count")),
                     _int(data.get("total_game_count")), _int(data.get("public_game_count")), _int(data.get("private_game_count")),
                     _int(data.get("in_progress_count")), _int(data.get("master_proxy_count")))
    n = sum(lob.players for lob in lobbies)
    return Stats(n, n, 0, len(lobbies), len(lobbies), 0)


def _json(get, url: str):
    return json.loads(get(url, accept="application/json", timeout=12).decode("utf-8", "replace"))


def fetch(get=None) -> Snapshot:
    """Read the lobbies (required) and the server counts (best effort). Raises rc.RecompError with a sentence the person can read."""
    get = get or rc._get
    try:
        lobbies = parse_lobbies(_json(get, GAMES_URL))
    except Exception as exc:  # noqa: BLE001 - network, HTTP and JSON errors all mean the same thing here
        raise rc.RecompError(f"Couldn't reach the Ryujinx LDN server ({type(exc).__name__}).") from exc
    try:
        stats = parse_stats(_json(get, STATS_URL), lobbies)
    except Exception:  # noqa: BLE001
        stats = parse_stats(None, lobbies)
    return Snapshot(stats, lobbies)
