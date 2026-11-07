"""Ryujinx (Ryubing) local-wireless lobbies: who is playing what right now, read from the LDN server's public listing (ldn.ryujinx.app).
No Qt in here. Each lobby carries the game, its version, the mode, how full it is, when it was made and who is in it. The server also lists
lobbies their owners marked private; those are shown as a count and a row, but their members' names are never kept."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import recomps as rc

BASE = "https://ldn.ryujinx.app"
GAMES_URL = BASE + "/api/public_games"  # one entry per lobby
STATS_URL = BASE + "/api/ldn"  # whole-server counts; optional, the games list alone is enough to show something
EMULATOR_ID = "ryujinx"
REFRESH_SECONDS = 30
JOINABLE = "Joinable"


@dataclass(frozen=True)
class Lobby:
    id: str
    name: str
    title_id: str
    version: str
    players: int
    max_players: int
    mode: str
    status: str
    public: bool
    names: tuple[str, ...]  # empty for a private lobby
    created: float  # seconds since the epoch; 0 when the server didn't say

    @property
    def joinable(self) -> bool:
        return self.status.lower() == JOINABLE.lower() and (not self.max_players or self.players < self.max_players)


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


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _created(v) -> float:
    n = _int(v)
    return n / 1000 if n > 10**11 else float(n)  # the server counts milliseconds


def age_text(created: float, now: float | None = None) -> str:
    """'just now', '12m', '3h 05m', '2d' for a lobby made `created` seconds-since-epoch ago; '' when unknown."""
    if not created:
        return ""
    secs = int((now if now is not None else time.time()) - created)
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h {mins % 60:02d}m" if hours < 24 else f"{hours // 24}d"


def parse_lobbies(data) -> tuple[Lobby, ...]:
    items = list(data.values()) if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for i, d in enumerate(items):
        if not isinstance(d, dict):
            continue
        title = str(d.get("title_id") or "").strip().upper()
        name = str(d.get("game_name") or "").strip() or title or "Unknown game"
        who = tuple(str(p).strip() for p in d["players"] if str(p).strip()) if isinstance(d.get("players"), list) else ()
        count = d.get("player_count")
        public = d.get("is_public") is not False  # a server that doesn't say is treated as public
        out.append(Lobby(str(d.get("id") or i), name, title, str(d.get("title_version") or "").strip(),
                         _int(count if count is not None else len(who)), _int(d.get("max_player_count")),
                         str(d.get("mode") or ""), str(d.get("status") or ""), public, who if public else (), _created(d.get("created_at"))))
    out.sort(key=lambda x: (-x.players, x.name.lower(), x.id))
    return tuple(out)


def parse_stats(data, lobbies: tuple[Lobby, ...] = ()) -> Stats:
    """The server's counts when it gave some, else worked out from the lobbies."""
    if isinstance(data, dict) and data:
        return Stats(_int(data.get("total_player_count")), _int(data.get("public_player_count")), _int(data.get("private_player_count")),
                     _int(data.get("total_game_count")), _int(data.get("public_game_count")), _int(data.get("private_game_count")),
                     _int(data.get("in_progress_count")), _int(data.get("master_proxy_count")))
    pub = [lob for lob in lobbies if lob.public]
    prv = [lob for lob in lobbies if not lob.public]
    pp, vp = sum(lob.players for lob in pub), sum(lob.players for lob in prv)
    return Stats(pp + vp, pp, vp, len(lobbies), len(pub), len(prv))


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
