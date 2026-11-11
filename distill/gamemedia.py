"""Extra art and achievements for the Games page: a background picture, screenshots, and RetroAchievements. No Qt in here.

Background art comes from SteamGridDB (its "heroes", with the player's key) and else from the LaunchBox Games Database's fan art;
screenshots come from LaunchBox. Everything is downloaded once into a cache folder and a miss is remembered for a while, so a
game with nothing online isn't asked about every time its page opens. Both sources' pages are read on a best-effort basis: when
their layout changes the page simply shows less. RetroAchievements needs the player's own user name and Web API key (Settings >
API keys > Games & artwork). A game is matched by the hash of its file where RetroAchievements hashes that platform the simple way (cartridge
ROMs), so a rename doesn't matter; otherwise, and when the hash is unknown to RetroAchievements, by name."""
from __future__ import annotations

import difflib
import hashlib
import html as html_lib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import recomps as rc
from .constants import APP_DIR

MEDIA_DIR = APP_DIR / "game_media"
MISS_TTL = 7 * 86400  # how long "nothing online" is believed
LIST_TTL = 7 * 86400  # how long a console's game list from RetroAchievements is kept
PROGRESS_TTL = 6 * 3600  # how long a game's achievements are kept before being asked for again
IMAGE_HOST = re.compile(r"https?://images\.launchbox-app\.com/[0-9a-fA-F-]{20,}\.(?:jpe?g|png|webp)", re.I)
MAX_IMAGE_BYTES = 12_000_000


class MediaError(Exception):
    """Something the player can be told: no key, a key that was refused, no connection."""


def _dir() -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR


def _fresh(path: Path, ttl: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime < ttl
    except OSError:
        return False


def _download(url: str, dest: Path) -> bool:
    """Save a picture (not an HTML error page, not something huge). True when it is in place."""
    try:
        data = rc._get(url, timeout=25)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if not data or len(data) > MAX_IMAGE_BYTES or data[:2] not in (b"\xff\xd8", b"\x89P", b"RI", b"GI"):  # jpeg, png, webp (RIFF), gif
        return False
    part = dest.with_name(dest.name + ".part")  # written aside and renamed, so a picture that is half there is never shown
    try:
        part.write_bytes(data)
        part.replace(dest)
    except OSError:
        part.unlink(missing_ok=True)
        return False
    return True


# ---- LaunchBox pictures ------------------------------------------------------------------------------------------
def parse_launchbox_images(page: str) -> dict[str, list[str]]:
    """The pictures on a LaunchBox 'images' page, sorted into 'screenshots' and 'fanart' by the words around each one (its alt text, its
    caption and the heading before it). Box art, logos, banners and the like are left out."""
    out: dict[str, list[str]] = {"screenshots": [], "fanart": []}
    seen: set[str] = set()
    for m in IMAGE_HOST.finditer(page):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        tag_start = page.rfind("<", 0, m.start())
        tag = page[tag_start:page.find(">", m.end()) + 1].casefold() if tag_start >= 0 else ""
        chunks = [c.strip() for c in re.findall(r">([^<>]+)<", page[max(0, m.start() - 600):tag_start if tag_start >= 0 else m.start()]) if c.strip()]
        before = html_lib.unescape(chunks[-1]).casefold() if chunks else ""  # the nearest words before the picture: its heading or caption
        for text in (tag, before):  # the picture's own alt text and title first; then the heading or caption just before it
            if re.search(r"fan ?art|background|wallpaper", text):
                out["fanart"].append(url)
                break
            if "screenshot" in text:
                out["screenshots"].append(url)
                break
    pairs = {full: thumb for full, thumb in _thumb_pairs(page).items() if full in out["screenshots"]}
    if pairs:  # the small previews are not screenshots of their own: they are kept beside the big ones
        out["screenshots"] = [u for u in out["screenshots"] if u not in pairs.values()]
        out["thumbs"] = pairs  # type: ignore[assignment]
    return out


def _thumb_pairs(page: str) -> dict[str, str]:
    """{full-size picture: its small preview} for pictures the page shows as a small image linking to the big one."""
    pairs: dict[str, str] = {}
    for m in re.finditer(r"<a\b[^>]*?href=[\"']?(" + IMAGE_HOST.pattern + r")[^>]*>\s*<img\b[^>]*?src=[\"']?(" + IMAGE_HOST.pattern + ")", page, re.I):
        if m.group(1) != m.group(2):
            pairs.setdefault(m.group(1), m.group(2))
    return pairs


def lb_game_id(title: str, system: str) -> str:
    """The LaunchBox Games Database id of a game ('' when not found), remembered on disk. `system` is LaunchBox's platform name."""
    memo = _dir() / f"lbid_{hashlib.md5(f'{title}|{system}'.encode()).hexdigest()[:16]}.txt"
    if memo.is_file() and (_fresh(memo, 30 * 86400) or memo.read_text(encoding="utf-8").strip()):
        return memo.read_text(encoding="utf-8").strip()
    found = ""
    for q in rc._search_queries(title):
        try:
            page = rc._get(rc._LB_SEARCH + urllib.parse.quote(q), timeout=15).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        ids = rc._lb_game_ids(page, title, system)
        if ids:
            found = ids[0]
            break
    if found:
        memo.write_text(found, encoding="utf-8")
    return found


def launchbox_images(lb_id: str) -> dict[str, list[str]]:
    if not lb_id:
        return {"screenshots": [], "fanart": []}
    try:
        page = rc._get(rc._LB_DETAIL.replace("/details/", "/images/") + lb_id, timeout=20).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return {"screenshots": [], "fanart": []}
    return parse_launchbox_images(page)


# ---- background art and screenshots --------------------------------------------------------------------------------
def cached_backdrop(game_id: str) -> Path | None:
    p = _dir() / f"back_{game_id}.jpg"
    return p if p.is_file() else None


def fetch_backdrop(game_id: str, title: str, lb_platform: str, sgdb_key: str = "", retry: bool = False) -> Path | None:
    """A wide picture for the top of a game's page: SteamGridDB's best hero for it, else LaunchBox fan art. None when there is none."""
    dest, miss = _dir() / f"back_{game_id}.jpg", _dir() / f"back_{game_id}.miss"
    if dest.is_file():
        return dest
    if not retry and _fresh(miss, MISS_TTL):
        return None
    if sgdb_key:
        try:
            for hit in rc.sgdb_games(title, sgdb_key)[:2]:
                heroes = rc._sgdb(f"/heroes/game/{hit['id']}?types=static&nsfw=false&humor=false", sgdb_key)
                for hero in heroes[:3]:
                    if hero.get("url") and _download(str(hero["url"]), dest):
                        return dest
        except (urllib.error.URLError, OSError, ValueError, rc.SgdbBusy):
            pass
    images = launchbox_images(lb_game_id(title, lb_platform))
    for url in images["fanart"][:3]:
        if _download(url, dest):
            return dest
    miss.write_text("miss", encoding="utf-8")
    return None


def recomp_banner_key(game: str) -> str:
    """The cache name of a recomp's banner. It belongs to the game the project plays, not to the project, so two projects for one game
    share a picture and a rename of the project doesn't lose it."""
    return "rc_" + (rc._slug(game) or "game")


def cached_recomp_banner(game: str) -> Path | None:
    return cached_backdrop(recomp_banner_key(game))


def fetch_recomp_banner(r: rc.Recomp, sgdb_key: str = "", retry: bool = False) -> Path | None:
    """A wide picture for the top of a recomp's page, from the game it plays ('The Legend of Zelda: Ocarina of Time'), the same sources as
    a game's background: SteamGridDB's best hero, else LaunchBox fan art. The full title is tried first, then the title without its
    subtitle ('Gothic 2'), so a base game's art still beats none. None when there is none (remembered for a week, like a game's)."""
    key = recomp_banner_key(r.game)
    if hit := cached_backdrop(key):
        return hit
    if not retry and _fresh(_dir() / f"back_{key}.miss", MISS_TTL):
        return None
    platform = rc.LB_PLATFORMS.get(r.system, r.system if r.system != rc.UNKNOWN_SYSTEM else "")
    for title in rc._title_fallbacks(r.game) or [r.game]:
        if hit := fetch_backdrop(key, title, platform, sgdb_key, retry=True):  # (the miss was checked above, once, for all the titles)
            return hit
    return None


def clear_media(game_id: str) -> None:
    """Forget a game's background and screenshots (and that nothing was found) so they are looked for again, after its name or details were edited."""
    for pattern in (f"back_{game_id}.*", f"shot_{game_id}_*", f"shot_{game_id}.miss"):
        for f in _dir().glob(pattern):
            f.unlink(missing_ok=True)


THUMB_MAX_W = 420  # LaunchBox's small previews are 400 wide; a picture this narrow beside a bigger one of the same shape is its preview


def _image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a jpeg or png from its header, without decoding it."""
    try:
        with open(path, "rb") as f:
            head = f.read(262144)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            size = int.from_bytes(head[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(head[i + 7:i + 9], "big"), int.from_bytes(head[i + 5:i + 7], "big")
            i += 2 + max(size, 2)
    return None


def thumb_path(full: Path) -> Path:
    """Where a screenshot's small preview is kept: shot_<game>_00.jpg -> shot_<game>_00_t.jpg."""
    return full.with_name(full.stem + "_t.jpg")


def shot_image(full: Path) -> Path | None:
    """The picture to show for a screenshot: the big one once it is there, the small preview until then."""
    if full.is_file():
        return full
    t = thumb_path(full)
    return t if t.is_file() else None


def _is_preview_of(small: Path, big: Path) -> bool:
    a, b = _image_size(small), _image_size(big)
    return bool(a and b and a[0] <= THUMB_MAX_W and b[0] > a[0] and abs(a[0] / a[1] - b[0] / b[1]) < 0.03 * (b[0] / b[1]))


def cached_screenshots(game_id: str) -> list[Path]:
    """The game's saved screenshots (their big-picture paths; for one still downloading only the small preview exists). Screenshots saved
    before previews were kept apart, where a small copy sits beside each big one, are tidied up here without downloading anything."""
    d = _dir()
    fulls = sorted(d.glob(f"shot_{game_id}_[0-9][0-9].jpg"))
    prev = None
    for p in fulls:
        if prev is not None and not thumb_path(prev).exists() and _is_preview_of(p, prev):
            p.replace(thumb_path(prev))
            continue
        if _image_size(p) is None or _image_size(p)[0] > THUMB_MAX_W:
            prev = p
    fulls = sorted(d.glob(f"shot_{game_id}_[0-9][0-9].jpg"))
    seen = {p.name for p in fulls}
    for t in d.glob(f"shot_{game_id}_[0-9][0-9]_t.jpg"):  # a preview whose big picture is not here (yet)
        if t.name.replace("_t.jpg", ".jpg") not in seen:
            fulls.append(t.with_name(t.name.replace("_t.jpg", ".jpg")))
    return sorted(fulls)


def fetch_screenshots(game_id: str, title: str, lb_platform: str, limit: int = 8, retry: bool = False, on_update=None) -> list[Path]:
    """The game's LaunchBox screenshots, downloaded (up to `limit`). Only the big picture of each is kept, plus its small preview when the
    page offers one: the previews are fetched first (they are quick) and `on_update(paths)` is called as pictures arrive, so the page can
    show the small ones at once and swap in the big ones. A screenshot saved with only its preview (the app was closed part way) is finished."""
    have = cached_screenshots(game_id)
    miss = _dir() / f"shot_{game_id}.miss"
    if have and all(p.is_file() for p in have):
        return have[:limit]
    if not have and not retry and _fresh(miss, MISS_TTL):
        return []
    found = launchbox_images(lb_game_id(title, lb_platform))
    thumbs: dict[str, str] = found.get("thumbs", {})  # type: ignore[assignment]
    urls = [u for u in found["screenshots"] if u not in thumbs.values()]
    if thumbs:
        urls = urls[:limit]
        for i, url in enumerate(urls):  # the small previews first
            dest = thumb_path(_dir() / f"shot_{game_id}_{i:02d}.jpg")
            if url in thumbs and not dest.is_file() and not (dest.with_name(dest.name.replace("_t.jpg", ".jpg"))).is_file():
                _download(thumbs[url], dest)
        if on_update:
            on_update(cached_screenshots(game_id))
    kept, prev = 0, None
    for i, url in enumerate(urls if thumbs else found["screenshots"]):
        if kept >= limit:
            break
        dest = _dir() / f"shot_{game_id}_{i:02d}.jpg"
        if not dest.is_file() and not _download(url, dest):
            continue
        if not thumbs and prev is not None and not thumb_path(prev).exists() and _is_preview_of(dest, prev):
            dest.replace(thumb_path(prev))  # a page that doesn't say which is which: a small copy of the one before is its preview
            continue
        prev, kept = dest, kept + 1
        if on_update:
            on_update(cached_screenshots(game_id))
    out = cached_screenshots(game_id)[:limit]
    if not out:
        miss.write_text("miss", encoding="utf-8")
    return out


# ---- RetroAchievements ---------------------------------------------------------------------------------------------------
RA_API = "https://retroachievements.org/API/"
RA_BADGES = "https://media.retroachievements.org/Badge/"
# Distill's platform name -> RetroAchievements' console id. A wrong id only means the game isn't found.
RA_CONSOLES = {
    "Mega Drive": 1, "Nintendo 64": 2, "SNES": 3, "Game Boy": 4, "Game Boy Advance": 5, "Game Boy Color": 6, "NES": 7, "PC Engine": 8, "PC Engine CD": 76, "Sega CD": 9,
    "32X": 10, "Master System": 11, "PlayStation": 12, "Atari Lynx": 13, "Neo Geo Pocket": 14, "Game Gear": 15, "GameCube": 16, "Atari Jaguar": 17,
    "Nintendo DS": 18, "Wii": 19, "PlayStation 2": 21, "Magnavox Odyssey 2": 23, "Pokemon Mini": 24, "Atari 2600": 25, "DOS": 26, "Arcade": 27,
    "Virtual Boy": 28, "MSX": 29, "SG-1000": 33, "Amstrad CPC": 37, "Apple II": 38, "Sega Saturn": 39, "Dreamcast": 40, "PSP": 41, "3DO": 43,
    "ColecoVision": 44, "Intellivision": 45, "Vectrex": 46, "PC-8000": 47, "PC-FX": 49, "Atari 7800": 51, "WonderSwan": 53, "Neo Geo CD": 56,
    "Fairchild Channel F": 57, "Famicom Disk System": 81,
}


@dataclass(slots=True)
class Achievement:
    id: int
    title: str
    description: str
    points: int
    badge: str
    earned: bool = False
    hardcore: bool = False
    date: str = ""


@dataclass(slots=True)
class RaGame:
    id: int
    title: str
    console: str = ""
    achievements: list[Achievement] = field(default_factory=list)
    url: str = ""

    @property
    def earned(self) -> int:
        return sum(a.earned for a in self.achievements)

    @property
    def points(self) -> tuple[int, int]:
        """(points earned, points in all)."""
        return sum(a.points for a in self.achievements if a.earned), sum(a.points for a in self.achievements)


def ra_console_id(system: str) -> int:
    return RA_CONSOLES.get(system, 0)


def _ra(endpoint: str, user: str, key: str, **params) -> object:
    if not user or not key:
        raise MediaError("Add your RetroAchievements user name and Web API key in Settings > API keys > Games & artwork.")
    query = urllib.parse.urlencode({"z": user, "y": key, **params})
    try:
        return json.loads(rc._get(f"{RA_API}{endpoint}?{query}", timeout=25))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise MediaError("RetroAchievements didn't accept that user name and key.") from exc
        raise MediaError(f"RetroAchievements answered {exc.code}.") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MediaError("Couldn't reach RetroAchievements.") from exc


def ra_test(user: str, key: str) -> str:
    """'' when RetroAchievements accepts the user name and key, else a sentence saying what is wrong."""
    try:
        data = _ra("API_GetUserProfile.php", user, key, u=user)
    except MediaError as exc:
        return str(exc)
    return "" if isinstance(data, dict) and data.get("User") else "RetroAchievements didn't recognise that user."


_ARTICLE = re.compile(r"^(the|a|an)\s+")


def ra_norm(title: str) -> str:
    """A title as it is compared: no brackets or tags, 'Legend of Zelda, The' as 'the legend of zelda', no punctuation."""
    t = html_lib.unescape(title or "").casefold()
    t = re.sub(r"~[^~]*~|\([^)]*\)|\[[^\]]*\]", " ", t)
    t = t.replace("&", " and ")
    if m := re.match(r"^(.*?),\s*(the|a|an)\b(.*)$", t):
        t = f"{m.group(2)} {m.group(1)}{m.group(3)}"
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return _ARTICLE.sub("", t)


def ra_best(title: str, games: list[dict], cutoff: float = 0.88) -> dict | None:
    """The RetroAchievements game that is `title`: the same name, else a close one (a subtitle or punctuation apart). Sets with no
    achievements and '~Hack~' / '~Demo~' / '~Prototype~' entries are passed over."""
    want = ra_norm(title)
    if not want:
        return None
    best, best_score = None, 0.0
    for g in games:
        name = str(g.get("Title", ""))
        if re.search(r"~(hack|demo|prototype|unlicensed|homebrew)", name, re.I) or int(g.get("NumAchievements") or 0) <= 0:
            continue
        got = ra_norm(name)
        score = 1.0 if got == want else difflib.SequenceMatcher(None, want, got).ratio()
        if score > best_score:
            best, best_score = g, score
    return best if best is not None and best_score >= cutoff else None


def ra_game_list(console: int, user: str, key: str) -> list[dict]:
    """Every game on a console that has achievements (kept a week: the lists are long)."""
    cache = _dir() / f"ra_games_h_{console}.json"  # the _h list carries each game's file hashes (an older list without them is not read)
    if _fresh(cache, LIST_TTL):
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    data = _ra("API_GetGameList.php", user, key, i=console, f=1, h=1)
    games = [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []
    if games:
        try:
            cache.write_text(json.dumps([{k: g.get(k) for k in ("ID", "Title", "NumAchievements", "Hashes")} for g in games]), encoding="utf-8")
        except OSError:
            pass
    return games


def parse_ra_progress(data: object) -> RaGame | None:
    """A game and the player's progress in it, from API_GetGameInfoAndUserProgress."""
    if not isinstance(data, dict) or not data.get("ID"):
        return None
    game = RaGame(int(data["ID"]), str(data.get("Title", "")), str(data.get("ConsoleName", "")), url=f"https://retroachievements.org/game/{int(data['ID'])}")
    raw = data.get("Achievements")
    items = raw.values() if isinstance(raw, dict) else (raw or [])
    for a in items:
        if not isinstance(a, dict) or not a.get("BadgeName"):
            continue
        hard = str(a.get("DateEarnedHardcore") or "")
        soft = str(a.get("DateEarned") or "")
        game.achievements.append(Achievement(int(a.get("ID") or 0), str(a.get("Title", "")), str(a.get("Description", "")), int(a.get("Points") or 0),
                                             str(a["BadgeName"]), bool(hard or soft), bool(hard), hard or soft))
    game.achievements.sort(key=lambda x: (not x.earned, x.points, x.id))  # unlocked first
    return game


# What RetroAchievements hashes for a cartridge: the whole file, less a copier or emulator header, so a file is only ever compared by its content.
_RA_HEADERS = {"NES": 16, "Atari Lynx": 64, "Atari 7800": 128}  # a fixed header that is stripped when the file starts with the right bytes
_RA_HEADER_MAGIC = {"NES": b"NES\x1a", "Atari Lynx": b"LYNX", "Atari 7800": b"\x01ATARI7800"}
RA_HASHED_SYSTEMS = frozenset({"NES", "SNES", "Game Boy", "Game Boy Color", "Game Boy Advance", "Mega Drive", "Master System", "Game Gear", "32X", "PC Engine",
                               "Nintendo 64", "Atari Lynx", "Atari 7800", "Atari 2600", "SG-1000", "Neo Geo Pocket", "WonderSwan", "Virtual Boy", "Pokemon Mini",
                               "ColecoVision", "Intellivision", "Vectrex", "Magnavox Odyssey 2"})  # everything else is matched by name
_RA_READ_LIMIT = 128 << 20  # a cartridge bigger than this is not a cartridge


def _n64_to_z64(data: bytes) -> bytes:
    """RetroAchievements hashes a Nintendo 64 ROM in big-endian (.z64) byte order; .v64 is byte-swapped and .n64 word-swapped."""
    if data[:4] == b"\x37\x80\x40\x12":  # .v64: every pair of bytes swapped
        out = bytearray(data)
        out[0::2], out[1::2] = data[1::2], data[0::2]
        return bytes(out)
    if data[:4] == b"\x40\x12\x37\x80":  # .n64: every four bytes reversed
        out = bytearray(len(data))
        for i in range(4):
            out[i::4] = data[3 - i::4]
        return bytes(out)
    return data


def ra_rom_hash(path: str | Path, system: str, stop: threading.Event | None = None) -> str:
    """The hash RetroAchievements knows a cartridge by (an MD5 of its content, less a header), or '' for a platform whose files it hashes in a more
    involved way (discs), a file that can't be read, or one too big. A .zip holding one file is hashed by what is inside."""
    if system not in RA_HASHED_SYSTEMS:
        return ""
    try:
        p = Path(path)
        if p.suffix.lower() == ".zip" and zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                files = [i for i in z.infolist() if not i.is_dir()]
                if len(files) != 1 or files[0].file_size > _RA_READ_LIMIT:
                    return ""
                data = z.read(files[0])
        else:
            if p.stat().st_size > _RA_READ_LIMIT:
                return ""
            data = p.read_bytes()
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return ""
    if stop is not None and stop.is_set():
        return ""
    head = _RA_HEADERS.get(system, 0)
    if head and data.startswith(_RA_HEADER_MAGIC[system]):
        data = data[head:]
    elif system == "SNES" and len(data) % 1024 == 512:  # a copier's 512-byte header
        data = data[512:]
    elif system == "Nintendo 64":
        data = _n64_to_z64(data)
    return hashlib.md5(data).hexdigest() if data else ""


def ra_hash_hit(md5: str, games: list[dict]) -> dict | None:
    """The game in a console's list that has this file hash (None for no hash or no such game)."""
    if not md5:
        return None
    for g in games:
        if int(g.get("NumAchievements") or 0) > 0 and md5 in {str(h).lower() for h in (g.get("Hashes") or ())}:
            return g
    return None


def ra_progress(system: str, title: str, user: str, key: str, path: str = "") -> RaGame | None:
    """The achievements of a game and which of them the player has, or None when RetroAchievements has no such game. Cached for hours.
    With `path`, the file is looked up by its hash first (a renamed or oddly named file still matches), then by title."""
    console = ra_console_id(system)
    if not console:
        return None
    md5 = ra_rom_hash(path, system) if path else ""
    safe = hashlib.md5(f"{console}|{ra_norm(title)}|{md5}|{user.casefold()}".encode()).hexdigest()[:16]
    cache = _dir() / f"ra_{safe}.json"
    if _fresh(cache, PROGRESS_TTL):
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            return parse_ra_progress(blob) if blob else None
        except (OSError, ValueError):
            pass
    games = ra_game_list(console, user, key)
    hit = ra_hash_hit(md5, games) or ra_best(title, games)
    if hit is None:
        try:
            cache.write_text("{}", encoding="utf-8")
        except OSError:
            pass
        return None
    data = _ra("API_GetGameInfoAndUserProgress.php", user, key, u=user, g=int(hit["ID"]), a=1)
    if isinstance(data, dict):
        try:
            cache.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
    return parse_ra_progress(data)


def ra_badge(badge: str, locked: bool = False) -> Path | None:
    """An achievement's picture, downloaded once ('_lock' is the grey one shown for achievements not yet won)."""
    name = re.sub(r"[^0-9A-Za-z_]", "", badge) + ("_lock" if locked else "")
    if not name:
        return None
    dest = _dir() / f"badge_{name}.png"
    if dest.is_file() or _download(f"{RA_BADGES}{name}.png", dest):
        return dest
    return None
