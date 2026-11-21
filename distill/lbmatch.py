"""Matching a game in the library to its LaunchBox Games Database entry, so that its art and its details are the right ones.

It runs for the games on screen (each one's art worker calls ensure_match before it fetches the cover), so a library fills in as it is
looked at and nothing is fetched for a game nobody opens. What it decides, for one game:
  - a clear match (the same title on the same platform, and only one such entry): linked, and its still-empty developer, publisher, year
    and genre filled in. Nothing the player typed is ever replaced.
  - several equally good entries, or a close title that is not the same: marked for review and left alone, for the player to pick.
  - nothing that resembles it: marked as none, and tried again after a week.
  - LaunchBox could not be reached: nothing is recorded, so it is tried again the next time the game is shown.
The link is saved with the player's other per-game data (games.db), so it survives a rescan; the cover, background, disc, cartridge and
screenshots then come from that entry, and opening the game reads that entry's page directly. No Qt, and the two network steps can be
swapped for fakes, so it is all tested without a network.
"""
from __future__ import annotations

import difflib
import re
import threading
import time
import urllib.error
from dataclasses import dataclass
from typing import Callable

from . import gamemedia as mm
from . import games as gm
from . import recomps as rc

AUTO, CONFIRMED, REVIEW, NONE = "auto", "confirmed", "review", "none"
RETRY_NONE = 7 * 86400  # a game nothing was found for is looked for again after a week
SAME = 0.97  # how alike two titles must be to count as the same game
CLOSE = 0.75  # and how alike to be worth showing the player as a possible
SHORTLIST = 4  # entries whose pages are read for one game, at most
DUPLICATE = 0.85  # an unread entry whose address is this alike to the title is read too, to see whether it is the same title again
MATCHES_AT_ONCE = 4  # (was 2) the shared throttle in lbthrottle is what keeps LaunchBox happy now: it adds places while the site answers and takes them away when it does not
_gate = threading.Semaphore(MATCHES_AT_ONCE)  # however many art workers there are, at most this many games are looked up at once

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")  # (USA), [!], (Rev 1), (Disc 2)
_DISC = re.compile(r"\b(?:disc|disk|cd)\s*\d+\b")
_TRAILING_ARTICLE = re.compile(r"^(.*),\s*(the|a|an)\s*$")
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}  # (not i, v or x: they are words and letters too)


def norm_title(text: str) -> str:
    """A title as it is compared: lower case, without region and version tags, 'Disc 1', punctuation or a leading 'The'; '&' as 'and', and
    roman numerals after the first word as numbers ('Final Fantasy VII' = 'Final Fantasy 7')."""
    t = _BRACKETS.sub(" ", str(text).casefold())
    if m := _TRAILING_ARTICLE.match(t.strip()):  # 'Legend of Zelda, The'
        t = f"{m[2]} {m[1]}"
    t = t.replace("&", " and ").replace("\u2019", "'").replace("'", "")
    t = _DISC.sub(" ", t)
    words = re.sub(r"[^a-z0-9]+", " ", t).split()
    if words and words[0] in ("the", "a", "an") and len(words) > 1:
        words = words[1:]
    return " ".join(w if i == 0 else _ROMAN.get(w, w) for i, w in enumerate(words))


def similarity(a: str, b: str) -> float:
    """0..1: 1.0 for the same title once tags and punctuation are set aside. A title that is the other plus a subtitle ('Pokemon' and
    'Pokemon Red Version') never reaches SAME: it is close, not the same game."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    score = difflib.SequenceMatcher(None, na, nb).ratio()
    if nb.startswith(na + " ") or na.startswith(nb + " "):
        score = max(score, 0.6 + 0.3 * min(len(na), len(nb)) / max(len(na), len(nb)))
    return min(score, 0.96)


@dataclass(frozen=True, slots=True)
class Decision:
    state: str                      # AUTO | REVIEW | NONE
    match: gm.Match | None = None   # the entry chosen (AUTO) or the best candidate (REVIEW)
    others: tuple = ()              # the other candidates worth showing, best first (what a review window lists)
    why: str = ""


def decide(title: str, system: str, year: str, matches: list[gm.Match]) -> Decision:
    """Which of the LaunchBox entries read so far is this game, if any. `system` is Distill's platform name for the game ('' or
    gm.UNKNOWN when the scan could not place it); an entry whose platform could not be read is neutral on it."""
    known = bool(system) and system != gm.UNKNOWN
    scored = []
    for order, m in enumerate(matches):
        alike = similarity(title, m.title)
        platform = (not known) or not m.system or m.system.casefold() == system.casefold()
        on_it = known and bool(m.system) and m.system.casefold() == system.casefold()
        scored.append((alike, platform, on_it, bool(year) and m.year == year, order, m))
    scored.sort(key=lambda s: (-s[0], not s[2], not s[3], s[4]))
    ranked = tuple(s[5] for s in scored if s[0] >= CLOSE)
    same = [s for s in scored if s[0] >= SAME and s[1]]  # the same title, on the game's platform (or one nothing is known about)
    if len(same) > 1 and year:  # the same title twice: the year may tell them apart
        by_year = [s for s in same if s[3]]
        if len(by_year) == 1:
            same = by_year
    if len(same) == 1:
        return Decision(AUTO, same[0][5], ranked, "the same title" + (" on its platform" if same[0][2] else ""))
    if len(same) > 1:
        return Decision(REVIEW, same[0][5], ranked, "the same title more than once")
    if ranked:
        return Decision(REVIEW, ranked[0], ranked, "a close title, not the same one")
    return Decision(NONE, None, (), "nothing resembles it")


def needs_match(game: gm.Game, now: float | None = None) -> bool:
    """Whether ensure_match has anything to do for this game: it has no saved answer, or its 'none' is old enough to try again."""
    now = time.time() if now is None else now
    if not game.name.strip() or game.lb_match in (AUTO, CONFIRMED, REVIEW):
        return False
    return not (game.lb_match == NONE and now - game.lb_tried < RETRY_NONE)


def _read_entry(lb_id: str, slug: str) -> gm.Match | None:
    """One LaunchBox entry's page as a Match, or None when it could not be read."""
    try:
        page = rc._get(rc._LB_DETAIL + lb_id, timeout=15).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return gm.parse_launchbox_match(page, lb_id, slug)


def ensure_match(game: gm.Game, store: gm.GameStore, *, force: bool = False, strict: bool = False, search: Callable | None = None,
                 read: Callable | None = None, now: Callable[[], float] = time.time) -> bool:
    """Look the game up if that has not been done, and save the answer. Called off the UI thread. True when something about the game
    changed (so a caller can refresh); the Game object itself is updated too, since the page holds that one. `force` looks again even
    if it has an answer (not for one the player chose: that is theirs); `strict` lets a failure to reach LaunchBox raise gm.GameError
    for a caller that wants to stop, where otherwise it is just not an answer."""
    if game.lb_match == CONFIRMED or (not force and not needs_match(game, now())):
        return False
    search, read = search or gm.lb_candidates, read or _read_entry
    title = game.name.strip()
    system = "" if game.system == gm.UNKNOWN else game.system
    with _gate:
        try:
            ids, slugs = search(title, system)
        except gm.GameError:
            if strict:
                raise
            return False  # LaunchBox is out of reach: say nothing, and try again when the game is next shown
        # The entries whose address already reads like the title come first (the search puts the right platform first, and keeps that order),
        # and ones whose address is plainly another game are not read at all.
        alike = {i: similarity(title, slugs.get(i, "").replace("-", " ")) if slugs.get(i) else None for i in ids}
        listed = sorted(enumerate(ids), key=lambda p: (-(alike[p[1]] if alike[p[1]] is not None else 0.5), p[0]))
        keep = [p for p in listed if alike[p[1]] is None or alike[p[1]] >= 0.55]
        matches: list[gm.Match] = []
        reached = False
        decision = Decision(NONE)
        for n, (_pos, lb_id) in enumerate(keep[:SHORTLIST]):
            entry = read(lb_id, slugs.get(lb_id, ""))
            if entry is None:
                continue
            reached = True
            matches.append(entry)
            decision = decide(title, game.system, game.year, matches)
            # A clear answer ends it, so most games cost one page: unless another entry's address reads almost the same, which might be
            # the same title twice (a duplicate entry), and that is only seen by reading it.
            if decision.state == AUTO and not any((alike[i] or 0) >= DUPLICATE for _p, i in keep[n + 1:SHORTLIST]):
                break
        if decision.state == NONE and ids:  # nothing reads like the title: LaunchBox's own first answer may be the game under another
            first = ids[0]                  # name (Rockman / Mega Man), so it is offered, on the game's platform, and never taken unasked
            other = next((m for m in matches if m.lb_id == first), None) or read(first, slugs.get(first, ""))
            if other is not None:
                reached = True
                if not system or not other.system or other.system.casefold() == system.casefold():
                    decision = Decision(REVIEW, other, (other,), "LaunchBox's first answer, under another name")
        if ids and not reached:
            return False  # the entries could not be read: not an answer
    stamp = now()
    if decision.state == AUTO and decision.match is not None:
        _link(game, store, decision.match, AUTO, stamp)
    else:
        state = REVIEW if decision.state == REVIEW else NONE
        store.set_match(game.id, "", state, stamp)
        game.lb_id, game.lb_match, game.lb_tried = "", state, stamp
    return True


def _link(game: gm.Game, store: gm.GameStore, m: gm.Match, state: str, stamp: float) -> None:
    """Fill what the game does not have, save the link, and point the art at the entry. A game the scan could not place also takes the
    entry's platform, as the manual lookup always did. The details go first, because a new platform clears a guessed link: it is saved after."""
    fill = {f: getattr(m, f) for f in ("developer", "publisher", "year", "genre") if getattr(m, f) and not getattr(game, f)}
    if game.system == gm.UNKNOWN and m.system:
        fill["system"] = m.system
    if fill:
        store.update_details(game.id, **fill)
        for field, value in fill.items():
            setattr(game, field, value)
    store.set_match(game.id, m.lb_id, state, stamp)
    game.lb_id, game.lb_match, game.lb_tried = m.lb_id, state, stamp
    platform = gm.LB_PLATFORM_NAMES.get(game.system, "" if game.system == gm.UNKNOWN else game.system)
    mm.remember_lb_id(game.name, platform, m.lb_id)


def confirm(game: gm.Game, store: gm.GameStore, m: gm.Match) -> None:
    """The player picked this entry for the game: it is the one, whatever the scoring thought."""
    _link(game, store, m, CONFIRMED, time.time())
