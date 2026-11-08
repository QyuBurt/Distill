"""PC recomps: a catalog of native PC ports built from a game you already own, with one-click install from each project's
own GitHub releases, update checks, "do I own the game?" matching against the library, and cover art.

Nothing here imports Qt. Nothing here ships, fetches or copies a game, ROM or asset: a project's own release is
downloaded, and the player's own dump is only ever read (hashed) to see whether it is the right one.

    catalog (assets/recomps.json, plus an optional distill_recomps_catalog.json beside the app)
      -> Recomp                      one project: game, console, repo, which dump it needs
    GitHub releases API              latest_release() / pick_asset() choose the download for this OS
    Installer                        download, verify, unpack, find the executable; records it in the Store
    match_library()                  library report entries -> RomMatch per recomp (verified / likely / mismatch)
    fetch_cover()                    SteamGridDB poster (with a key) -> LaunchBox poster -> the catalog's own cover (else a placeholder)

Run `python -m distill.recomps hash <file>` to print the checksums a catalog entry's "rom" block wants.
"""
from __future__ import annotations

import html as html_lib

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import apikeys
from .constants import APP_DIR, APP_NAME, ASSETS_DIR
from .github_auth import github_token, reveal
from .models import fmt_size
from .systems import system_of_name


USER_AGENT = f"{APP_NAME}-recomps"
UNKNOWN_SYSTEM = "Unknown"
SOURCE_TTL = 6 * 3600  # how long a downloaded catalog source is reused
DISCOVER_TTL = 24 * 3600
RELEASE_TTL = 3600  # seconds a release listing is trusted before GitHub is asked again
ART_RETRY = 7 * 86400  # a game with no cover found is not searched for again for a week
MAX_ART_BYTES = 6 << 20
HASH_LIMIT = 512 << 20  # larger dumps (disc images) are matched by name: hashing them would take minutes each
ARCHIVE_SUFFIXES = (".zip", ".7z", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")
SINGLE_FILE_SUFFIXES = (".appimage", ".exe")
SKIP_SUFFIXES = (".sha256", ".sha1", ".md5", ".sig", ".asc", ".txt", ".md", ".pdb", ".zsync", ".json", ".yml", ".yaml",
                 ".deb", ".rpm", ".msi", ".pkg", ".apk", ".ipa", ".flatpak", ".snap", ".blockmap", ".dsym")


# ---- where things live ---------------------------------------------------------------------------------------
ROOT: Path | None = None  # tests point this at a temp folder


def root() -> Path:
    """./recomps beside the app, or under %LOCALAPPDATA% / ~/.local/share when the app folder isn't writable."""
    global ROOT
    if ROOT is not None:
        return ROOT
    bases = [APP_DIR]
    if local := os.environ.get("LOCALAPPDATA"):
        bases.append(Path(local) / APP_NAME)
    bases.append(Path.home() / ".local" / "share" / APP_NAME)
    for base in bases:
        target = base / "recomps"
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write_test"
            probe.write_text("x")
            probe.unlink()
            ROOT = target
            return target
        except OSError:
            continue
    ROOT = Path(tempfile.gettempdir()) / f"{APP_NAME.lower()}_recomps"
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


def bundled_catalog() -> Path:
    return ASSETS_DIR / "recomps.json"


def user_catalog() -> Path:
    return APP_DIR / "distill_recomps_catalog.json"  # the player's own additions and overrides, same format


def platform_key() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def machine_key() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "x64"


# ---- the catalog ----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RomSpec:
    """Which dump a project needs. `sha1` / `crc32` make a match verified; `names` (words that must all appear in a
    file name) make it likely. `exts` are the raw dump formats the project can read ('' = any)."""
    sha1: tuple[str, ...] = ()
    crc32: tuple[str, ...] = ()
    names: tuple[tuple[str, ...], ...] = ()
    exts: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class Recomp:
    id: str
    name: str  # the project's name: "Ship of Harkinian"
    game: str  # the game it plays: "The Legend of Zelda: Ocarina of Time"
    system: str  # a name systems.py uses: "Nintendo 64"
    repo: str  # "owner/name" on GitHub
    description: str = ""
    tags: tuple[str, ...] = ()
    website: str = ""
    discord: str = ""
    cover: str = ""  # a direct image address, when the project publishes one
    cover_path: str = ""  # an image inside the repo ("docs/cover.png")
    launchbox_id: str = ""  # a LaunchBox Games Database id, to skip the search
    asset: dict = field(default_factory=dict)  # per platform, a regex for the right release file when the guess is wrong
    exe: str = ""  # regex for the executable inside the download when the guess is wrong
    rom: RomSpec = RomSpec()
    platforms: tuple[str, ...] = ("windows", "linux", "macos")
    setup: str = ""  # one line of first-run advice shown on the page
    source: str = ""  # where the entry came from when it is not the bundled catalog ("" = bundled)
    stars: int = 0  # GitHub stars, when the source says (curation uses it)
    updated: str = ""  # date of the last commit (YYYY-MM-DD), when the source says
    score: float = 0.0  # the source's own quality score, used to pick one project per game
    others: tuple[tuple[str, str], ...] = ()  # (name, repo) of other projects for the same game that curation folded in


def _tuple(v) -> tuple:
    return tuple(v) if isinstance(v, (list, tuple)) else ((v,) if v else ())


def _recomp(d: dict) -> Recomp:
    r = d.get("rom") or {}
    rom = RomSpec(tuple(s.lower() for s in _tuple(r.get("sha1"))), tuple(s.lower().zfill(8) for s in _tuple(r.get("crc32"))),
                  tuple(tuple(w.lower() for w in (n.split() if isinstance(n, str) else n)) for n in _tuple(r.get("names"))),
                  tuple(e.lower() for e in _tuple(r.get("exts"))), r.get("note", ""))
    return Recomp(
        id=d["id"], name=d["name"], game=d.get("game") or resolve_game(d["name"], d.get("description", ""), always=True), system=d["system"], repo=d["repo"],
        description=d.get("description", ""), tags=_tuple(d.get("tags")), website=d.get("website", ""),
        discord=d.get("discord", ""), cover=d.get("cover", ""), cover_path=d.get("cover_path", ""),
        launchbox_id=str(d.get("launchbox_id", "")), asset=dict(d.get("asset") or {}), exe=d.get("exe", ""), rom=rom,
        platforms=_tuple(d.get("platforms")) or ("windows", "linux", "macos"), setup=d.get("setup", ""))


def load_catalog(paths: Iterable[Path] | None = None) -> list[Recomp]:
    """The bundled catalog, then the player's own file on top (same id replaces; a new id adds). A bad file is skipped."""
    out: dict[str, Recomp] = {}
    for path in paths if paths is not None else (bundled_catalog(), user_catalog()):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for d in data.get("recomps", data) if isinstance(data, dict) else data:
            try:
                rc = _recomp(d)
            except (KeyError, TypeError, AttributeError):
                continue
            out[rc.id] = rc
    return sorted(out.values(), key=lambda r: (r.game.casefold(), r.name.casefold()))


# ---- the player's own projects ----------------------------------------------------------------------------------
CUSTOM_SOURCE = "Your own list"  # what the page says in "Listed by" for an entry from the player's own file
CUSTOM_KINDS = {"Static recompilation": "Recomp", "Decompilation": "Decomp", "Source port": "Source port", "Engine remake": "Engine remake"}
PLATFORM_NAMES = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}


def parse_repo(text: str) -> str:
    """'owner/name' from a GitHub address or the short form. Anything else is an error in plain words."""
    t = (text or "").strip()
    t = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", t, flags=re.IGNORECASE)
    parts = [p for p in t.split("?")[0].split("#")[0].split("/") if p]
    if len(parts) < 2:
        raise RecompError("Enter a GitHub address like github.com/owner/name, or owner/name.")
    owner, name = parts[0], re.sub(r"\.git$", "", parts[1])
    if not re.fullmatch(r"[\w.-]+", owner) or not re.fullmatch(r"[\w.-]+", name):
        raise RecompError("That doesn't look like a GitHub repository.")
    return f"{owner}/{name}"


def is_custom(r: Recomp) -> bool:
    return r.source == CUSTOM_SOURCE


def custom_id(repo: str) -> str:
    return "custom-" + _slug(repo.replace("/", "-"))


def read_user_entries() -> list[dict]:
    """The raw entries of the player's own file (hand-written overrides included), or [] when there is none or it is unreadable."""
    try:
        data = json.loads(user_catalog().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("recomps", data) if isinstance(data, dict) else data
    return [d for d in items if isinstance(d, dict)] if isinstance(items, list) else []


def _write_user_entries(entries: list[dict]) -> None:
    path = user_catalog()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"recomps": entries}, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise RecompError(f"Couldn't save your list: {exc}") from exc


def save_user_entry(entry: dict) -> None:
    """Add an entry to the player's own file, or replace the one with the same id. Everything else in the file is kept as it was."""
    entries = read_user_entries()
    for i, d in enumerate(entries):
        if d.get("id") == entry["id"]:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    _write_user_entries(entries)


def remove_user_entry(rid: str) -> bool:
    entries = read_user_entries()
    kept = [d for d in entries if d.get("id") != rid]
    if len(kept) == len(entries):
        return False
    _write_user_entries(kept)
    return True


def _phrases(text: str) -> list[str]:
    return [t for t in re.split(r"[;,\n]+", text or "") if t.strip()]


def custom_entry(repo: str, name: str, game: str, system: str, *, kind: str = "Static recompilation", description: str = "", website: str = "",
                 discord: str = "", platforms: Iterable[str] = (), setup: str = "", cover: str = "", asset: dict | None = None, exe: str = "",
                 rom_names: str = "", rom_exts: str = "", rom_sha1: str = "", rom_note: str = "", rid: str = "") -> dict:
    """One entry for the player's own file, checked. Raises RecompError in plain words for anything that would make a broken entry.
    `rom_names` is phrases separated by ';' or newlines (each phrase's words must all appear in a dump's name); `rom_exts` and
    `rom_sha1` are lists separated by spaces or commas."""
    repo = parse_repo(repo)
    name, game, system = name.strip(), game.strip(), system.strip()
    if not name:
        raise RecompError("Give the project a name.")
    if not game:
        raise RecompError("Say which game it plays.")
    if not system:
        raise RecompError("Say which console the game is for.")
    for label, url in (("website", website), ("Discord", discord), ("cover image", cover)):
        if url.strip() and not re.match(r"https?://\S+$", url.strip()):
            raise RecompError(f"The {label} address must start with http:// or https://.")
    plats = tuple(p for p in platforms if p in PLATFORM_NAMES) or tuple(PLATFORM_NAMES)
    patterns = {p: v.strip() for p, v in (asset or {}).items() if p in PLATFORM_NAMES and v.strip()}
    for label, pattern in [*((f"{PLATFORM_NAMES[p]} file", v) for p, v in patterns.items()), ("program", exe.strip())]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RecompError(f"The {label} pattern isn't valid: {exc}.") from exc
    sha1 = [s.lower() for s in re.split(r"[\s,;]+", rom_sha1.strip()) if s]
    if bad := [s for s in sha1 if not re.fullmatch(r"[0-9a-f]{40}", s)]:
        raise RecompError(f"'{bad[0]}' isn't a SHA-1 (40 letters and digits, 0-9 and a-f).")
    exts = [("." + e.lstrip(".")).lower() for e in re.split(r"[\s,;]+", rom_exts.strip()) if e.strip(".")]
    rom = {k: v for k, v in (("names", [n.split() for n in _phrases(rom_names) if n.split()]), ("exts", exts), ("sha1", sha1),
                             ("note", rom_note.strip())) if v}
    entry = {"id": rid or custom_id(repo), "name": name, "game": game, "system": system, "repo": repo,
             "description": description.strip(), "tags": [CUSTOM_KINDS[kind]] if kind in CUSTOM_KINDS else [],
             "website": website.strip(), "discord": discord.strip(), "cover": cover.strip(),
             "platforms": list(plats) if set(plats) != set(PLATFORM_NAMES) else [], "setup": setup.strip(),
             "asset": patterns, "exe": exe.strip(), "rom": rom}
    return {k: v for k, v in entry.items() if v or k in ("id", "name", "game", "system", "repo")}


_KIND_WORDS = (("Static recompilation", ("static-recompilation", "static recompilation", "recompilation", "recomp", "recompiled")),
               ("Decompilation", ("decompilation", "decomp", "decompiled")),
               ("Source port", ("source-port", "source port")),
               ("Engine remake", ("engine-remake", "engine remake", "reimplementation", "re-implementation")))
_SYSTEM_WORDS = {"n64": "Nintendo 64", "nintendo-64": "Nintendo 64", "gamecube": "GameCube", "gc": "GameCube", "wii": "Wii", "snes": "SNES",
                 "nes": "NES", "gba": "Game Boy Advance", "nds": "Nintendo DS", "3ds": "Nintendo 3DS", "psx": "PlayStation", "ps1": "PlayStation",
                 "ps2": "PlayStation 2", "psp": "PSP", "dreamcast": "Dreamcast", "saturn": "Sega Saturn", "genesis": "Mega Drive",
                 "megadrive": "Mega Drive", "xbox": "Xbox", "xbox360": "Xbox 360", "xbox-360": "Xbox 360"}


def guess_kind(topics: Iterable[str], description: str) -> str:
    """The kind of project a repository's topics (then its description) say it is, or ''."""
    for text in ([t.lower() for t in topics], [description.lower()]):
        for label, words in _KIND_WORDS:
            if any(w in t for t in text for w in words):
                return label
    return ""


def guess_system(topics: Iterable[str], description: str) -> str:
    for t in topics:
        t = t.lower()
        if t in _SYSTEM_WORDS:
            return _SYSTEM_WORDS[t]
        if s := system_of_name(t.replace("-", " ")):
            return s
    for w in re.findall(r"[a-z0-9]+", description.lower()):
        if w in _SYSTEM_WORDS:
            return _SYSTEM_WORDS[w]
    return ""


@dataclass(slots=True)
class RepoReport:
    """What Distill could see about a repository before it is added to the player's own list."""
    repo: str
    error: str = ""
    name: str = ""
    game: str = ""
    system: str = ""
    kind: str = ""
    description: str = ""
    website: str = ""
    stars: int = 0
    archived: bool = False
    pushed: str = ""
    release: str = ""
    released: str = ""
    downloads: dict = field(default_factory=dict)  # platform -> (file name, size, release tag)
    prerelease_only: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def _pretty_name(repo_name: str) -> str:
    words = re.sub(r"[-_.]+", " ", repo_name).strip()
    return words if any(c.isupper() for c in words) else words.title()


def inspect_repo(text: str, store: Store | None = None) -> RepoReport:
    """Look at a repository for the Add recomp window: what it says it is, how active it is, and which of this app's platforms it
    publishes a download for. Never raises: a problem comes back in `error`. Nothing is saved or downloaded."""
    try:
        repo = parse_repo(text)
    except RecompError as exc:
        return RepoReport(repo=str(text), error=str(exc))
    rep = RepoReport(repo=repo)
    try:
        facts = json.loads(_get(f"https://api.github.com/repos/{repo}", store.token() if store else "", "application/vnd.github+json"))
    except urllib.error.HTTPError as exc:
        rep.error = ("That repository doesn't exist, or it is private." if exc.code == 404 else
                     "GitHub is rate-limiting this PC. Try again later, or add a GitHub key under Settings > General > API keys."
                     if exc.code in (403, 429) else f"GitHub answered {exc.code}.")
        return rep
    except (urllib.error.URLError, OSError, ValueError):
        rep.error = "Couldn't reach GitHub."
        return rep
    if isinstance(facts, dict):
        rep.repo = str(facts.get("full_name") or repo)  # GitHub's own capitalisation
        rep.description = str(facts.get("description") or "").strip()
        rep.website = str(facts.get("homepage") or "").strip() if re.match(r"https?://", str(facts.get("homepage") or "")) else ""
        rep.stars, rep.archived, rep.pushed = int(facts.get("stargazers_count") or 0), bool(facts.get("archived")), str(facts.get("pushed_at") or "")[:10]
        topics = [str(t) for t in facts.get("topics") or []]
        rep.name = _pretty_name(str(facts.get("name") or repo.split("/")[1]))
        rep.kind, rep.system = guess_kind(topics, rep.description), guess_system(topics, rep.description)
        rep.game = tidy_game(resolve_game(rep.name, rep.description, always=True))
    try:
        rels = releases(rep.repo, store, force=True)
    except RecompError as exc:
        rep.error = str(exc) if "rate-limiting" in str(exc) or "reach" in str(exc) else ""
        return rep  # no releases yet is not an error: a new project can be added before its first build
    probe = Recomp("probe", rep.name or repo, rep.game, rep.system, rep.repo)
    for plat in PLATFORM_NAMES:
        found = choose_release(rels, probe, plat, "arm64" if plat == "macos" else "x64")
        if found:
            rel, asset = found
            rep.downloads[plat] = (asset["name"], int(asset.get("size") or 0), rel.tag)
    if rels:
        rep.release, rep.released = rels[0].tag, rels[0].published
        rep.prerelease_only = all(r.prerelease for r in rels)
    return rep


# ---- extra catalogs: community lists and GitHub topic search ---------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _name_words(text: str) -> tuple[str, ...]:
    return tuple(w for w in _TOKEN_SPLIT.split(text.lower()) if w)


_PLATFORM_SUFFIX = re.compile(r"\s+(?:(?:for|on)\s+(?:the\s+)?|-\s*)?(?:windows(?:\s*(?:pc|10|11))?|pc|linux|mac\s*os|macos|mac|steam\s*deck|desktop)"
                              r"(?:\s+port)?\s*$", re.IGNORECASE)


# Abbreviations projects use for an edition or expansion, spelled out so the poster search can find the game.
_ABBREVS = {"notr": "Night of the Raven"}
_ABBREV_RX = re.compile(r"\b(" + "|".join(_ABBREVS) + r")\b", re.IGNORECASE)


def _expand_abbrevs(title: str) -> str:
    """'Gothic 2 NOTR' is 'Gothic 2 Night of the Raven'."""
    return _ABBREV_RX.sub(lambda m: _ABBREVS[m[1].lower()], title)


def tidy_game(game: str) -> str:
    """A game title without the platform a port names on the end: 'Bloodborne for Windows' is 'Bloodborne'. Only PC and desktop
    platforms are removed (the console is kept in its own column), and never down to nothing."""
    game = _expand_abbrevs(game)
    cleaned = _PLATFORM_SUFFIX.sub("", game.strip()).rstrip(" -:")
    return cleaned if cleaned and len(cleaned) < len(game.strip()) and _sig_words(cleaned) else game


# What each project is, as the Awesome Decomp Games site sorts them. The first word of each pair is the label shown; the
# set is the tags (as lists and the catalog write them) that put a project under it.
KINDS = (("Static recompilation", {"recomp", "recompilation", "static recomp", "static recompilation", "static-recomp"}),
         ("Decompilation", {"decomp", "decompilation"}),
         ("Source port", {"source port", "source-port"}),
         ("Engine remake", {"engine remake", "engine-remake"}))
OTHER_KIND = "Other"


def kinds_of(r: Recomp) -> tuple[str, ...]:
    """Every kind a project belongs to (a few are both a recomp and a port), or ('Other',) when its tags don't say."""
    tags = {t.casefold() for t in r.tags}
    return tuple(label for label, keys in KINDS if tags & keys) or (OTHER_KIND,)


def kinds_in(catalog: Iterable[Recomp]) -> list[str]:
    """The kinds actually present, in the order above (Other last), for the filter's list."""
    present = {k for r in catalog for k in kinds_of(r)}
    return [label for label, _keys in KINDS if label in present] + ([OTHER_KIND] if OTHER_KIND in present else [])


# ---- the game a project plays, when its name doesn't say ------------------------------------------------------
_TITLE_TOK = r"[A-Z0-9][\w'\u2019!&+-]*"
_TITLE_RUN = re.compile(rf"{_TITLE_TOK}(?::?\s+(?:(?:of|the|and|a|an|de|vs|&|\+)\s+)*{_TITLE_TOK})*")
_LEAD_IN = re.compile(r"\b(?:re-?implementation|re-?make|re-?creation|re-?write|source port|port|decompilation|recompilation|clone|playing|"
                      r"engine|reverse[- ]engineering)\s*(?:of|for|from)?\s+", re.IGNORECASE)
_TITLE_THEN = re.compile(r"\s+(?:clone|remake|re-?implementation|re-?creation|port|source port|engine|decompilation|"
                         r"(?:re-?)?ported|re-?compiled|de-?compiled|re-?implemented|re-?built|re-?made|re-?mastered|re-?created|"
                         r"reverse[- ]engineered)\b", re.IGNORECASE)  # "Halo: Combat Evolved ported to Windows": the title comes first
_FILLER = {"the", "a", "an", "classic", "original", "game", "video", "popular", "famous", "legendary", "beloved", "old", "retro", "open",
           "source", "open-source", "opensource", "free", "unofficial", "fan", "fan-made", "modern", "cross-platform", "community",
           "native", "pc", "computer", "complete", "faithful", "fully", "playable", "new", "of", "for", "from"}
_NOT_GAMES = {"javascript", "typescript", "python", "rust", "java", "kotlin", "go", "golang", "c", "cpp", "csharp", "net", "dotnet", "linux",
              "windows", "macos", "mac", "osx", "pc", "dos", "sdl", "sdl2", "sdl3", "opengl", "vulkan", "directx", "webgl", "html", "html5", "web",
              "browser", "engine", "game", "games", "nintendo", "sony", "sega", "microsoft", "xbox", "playstation", "android", "ios", "steam",
              "github", "gnu", "unix", "tools", "tool", "library", "framework", "app", "apps", "software", "utility", "utilities", "code", "project", "wasm", "webassembly", "node", "js", "ts", "qt", "electron", "unity", "godot", "lua", "this", "that", "it"}


_ASIDE = re.compile(r"\s*[(\[][^)\]]{0,60}[)\]]")


def _plausible_title(text: str) -> bool:
    words = _sig_words(text)
    return (bool(words) and 3 <= len(text) <= 60 and len(text.split()) <= 8 and not words <= _NOT_GAMES
            and not text.rstrip().lower().endswith(("'s", "\u2019s")) and not system_of_name(text))


def _clean_title(text: str) -> str:
    words = text.rstrip(" :-").split()
    if len(words) > 1 and words[0] == "The" and words[1].lower() not in _FILLER:
        return " ".join(words)  # 'The Legend of Zelda': that "The" belongs to the title
    while words and words[0].lower() in _FILLER:
        words.pop(0)
    return _expand_abbrevs(" ".join(words))


def game_from_description(description: str) -> str:
    """The game a project's one-line description says it plays, or ''. A quick text check, no network: 'remake of SimCity 2000
    in JavaScript' and 'an open source Sim City 2000 clone' both give the title. It looks for a title right after words like
    port / remake / reimplementation / clone / engine, or right before one, and ignores things that are not games (a
    language, an OS, a console)."""
    text = " ".join(description.split())
    for lead in _LEAD_IN.finditer(text):
        pos = lead.end()
        while (w := re.compile(r"[A-Za-z-]+\s+").match(text, pos)) and w.group().strip().lower() in _FILLER and w.group().strip() != "The":
            pos = w.end()
        if (run := _TITLE_RUN.match(text, pos)) and _plausible_title(title := _clean_title(run.group())):
            return title
    for run in _TITLE_RUN.finditer(text):
        aside = _ASIDE.match(text, run.end())  # "Halo: Combat Evolved (for original Xbox) ported to ...": skip the bracketed note
        if _TITLE_THEN.match(text, aside.end() if aside else run.end()) and _plausible_title(title := _clean_title(run.group())):
            return title
    return ""


_PROJECT_WORDS = {"recomp", "recompiled", "recompilation", "decomp", "decompiled", "decompilation", "port", "ported", "remake", "remaster",
                  "remastered", "engine", "reimplementation", "clone", "unofficial", "native", "pc", "fan", "mod"}


def _looks_like_project_name(game: str) -> bool:
    """A title that names the project rather than a game: a single word ('OpenSC2K'), or one that says what the project is
    ('TotK PC Recomp', 'Doom Port')."""
    return len(game.split()) <= 1 or bool(set(_TOKEN_SPLIT.split(game.lower())) & _PROJECT_WORDS)


def resolve_game(game: str, description: str, always: bool = False) -> str:
    """The game a project plays: `game` as given, unless it is really just the project's own name (one word like 'OpenSC2K',
    one that says it is a port or recomp like 'TotK PC Recomp', or `always` when the list gave no separate game) and the description names a different title ('... SimCity 2000 clone').
    A title that already shares a word with the description's is left alone, so a real game title is never swapped."""
    game = tidy_game(game)
    if not always and not _looks_like_project_name(game):
        return game
    found = game_from_description(description)
    if not found or _sig_words(found) & _sig_words(game):
        return game
    return found


def parse_quiver_list(data: dict, source: str = "") -> list[Recomp]:
    """Quiver Launcher's list format: {"apps": [{name, project, repository, folderName, tags, releaseAssetFilter, appIconUrl}]}.
    Entries with no GitHub repository (manually managed, or on GitLab) can't be installed here and are left out. The
    console is read from the tags ('n64', 'gamecube' ...); without one the game is matched by name alone."""
    out = []
    for a in data.get("apps") or []:
        if not isinstance(a, dict) or not a.get("repository") or str(a.get("repositorySource", "github")).lower() != "github":
            continue
        repo = str(a["repository"]).strip().strip("/")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            continue
        tags = tuple(str(t) for t in a.get("tags") or [])
        system = next((system_of_name(t) for t in tags if system_of_name(t)), "") or UNKNOWN_SYSTEM
        title = str(a.get("name") or repo.split("/")[1])
        game = resolve_game(title, str(a.get("description") or ""), always=True)
        flt = a.get("releaseAssetFilter")
        shown = tuple(t.title() if len(t) > 3 else t.upper() for t in tags if not system_of_name(t) and t.lower() not in ("recomp", "decomp"))
        kinds = tuple(t.title() for t in tags if t.lower() in ("recomp", "decomp"))
        out.append(Recomp(
            id=_slug(a.get("folderName") or repo), name=str(a.get("project") or title), game=game, system=system, repo=repo,
            description=str(a.get("description") or ""), tags=kinds + shown, cover=str(a.get("appIconUrl") or ""),
            asset={p: re.escape(str(flt)) for p in ("windows", "linux", "macos")} if flt else {},
            rom=RomSpec(names=(_name_words(game),) if _name_words(game) else ()), source=source))
    return out


DECOMP_GAMES_URL = "https://submitgame.github.io/awesome-decomp-games/data/games.json"
DECOMP_GAMES_LABEL = "Awesome Decomp Games"
_DG_TYPES = {"static-recomp": "Recomp", "source-port": "Source port", "engine-remake": "Engine remake", "decompilation": "Decomp"}  # what can be installed
_PLATFORM_ALIASES = {"og xbox": "Xbox", "xbox og": "Xbox", "wii u": "Wii U", "ps vita": "PS Vita", "dos": "DOS", "pc": "PC"}
_DG_TITLE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def parse_decomp_games(data: dict, source: str = "") -> list[Recomp]:
    """The awesome-decomp-games list ({"games": [{id, title, platform, type, repo, release, releases, note, ...}]}).
    Kept: static recomps, source ports, engine remakes and decompilations that live on GitHub, are not archived and have
    published a release (there's nothing to install otherwise). Left out: browser ports, disassemblies, tools.
    A title like 'Majora's Mask (Zelda64Recomp)' is the game plus the project's own name."""
    out = []
    for g in data.get("games") or []:
        if not isinstance(g, dict) or g.get("type") not in _DG_TYPES or g.get("archived"):
            continue
        repo = str(g.get("repo") or "").strip().strip("/")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not (g.get("release") or (g.get("releases") or 0) > 0):
            continue
        title = str(g.get("title") or repo.split("/")[1])
        m = _DG_TITLE.match(title)
        game, name = (m[1], m[2]) if m and m[1] else (title, repo.split("/")[1].replace("-", " ").replace("_", " "))
        game = tidy_game(game)  # 'Bloodborne for Windows' plays Bloodborne
        note = str(g.get("note") or g.get("description") or "")
        if (found := resolve_game(game, note)) != game:  # a title that is only the project's name ('OpenSC2K'): the game is in the note
            game, name = found, (name if m and m[1] else title)
        parts = [x.strip() for x in str(g.get("platform") or "").split("/") if x.strip()]
        system = next((_PLATFORM_ALIASES.get(x.lower()) or system_of_name(x) for x in parts if _PLATFORM_ALIASES.get(x.lower()) or system_of_name(x)), "")
        system = system or (parts[0] if parts else UNKNOWN_SYSTEM)
        words = tuple(sorted(_sig_words(game)))
        out.append(Recomp(
            id=_slug(str(g.get("id") or repo)), name=name, game=game, system=system, repo=repo,
            description=str(g.get("note") or g.get("description") or ""), tags=(_DG_TYPES[g["type"]],),
            website=str(g.get("homepage") or "") if str(g.get("homepage") or "").startswith("https://") and "discord" not in str(g.get("homepage")) else "",
            discord=str(g.get("homepage") or "") if "discord.gg" in str(g.get("homepage") or "") else "",
            rom=RomSpec(names=(words,) if words else ()), source=source,
            stars=int(g.get("stars") or 0), updated=str(g.get("last_commit") or "")[:10], score=float(g.get("score") or 0)))
    return out


def parse_catalog(data, source: str = "") -> list[Recomp]:
    """Our own format ({"recomps": [...]} or a bare list), Quiver's ({"apps": [...]}) or awesome-decomp-games' ({"games": [...]}),
    told apart by their shape."""
    if isinstance(data, dict) and "apps" in data:
        return parse_quiver_list(data, source)
    if isinstance(data, dict) and isinstance(data.get("games"), list):
        return parse_decomp_games(data, source)
    out = []
    for d in data.get("recomps", []) if isinstance(data, dict) else data if isinstance(data, list) else []:
        try:
            out.append(_replace_source(_recomp(d), source))
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def _replace_source(r: Recomp, source: str) -> Recomp:
    return replace(r, source=source or r.source)


def _source_cache(spec: str) -> Path:
    d = root() / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(spec.encode()).hexdigest()[:16] + ".json")


def _read_source(spec: str, force: bool) -> object | None:
    """The JSON behind a source: a local file read fresh, or a URL downloaded at most every few hours (a failed
    download falls back to the last copy)."""
    if not spec.lower().startswith(("http://", "https://")):
        try:
            return json.loads(Path(spec).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    cache = _source_cache(spec)
    if not force and cache.exists() and time.time() - cache.stat().st_mtime < SOURCE_TTL:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        raw = _get(spec, timeout=20)
        data = json.loads(raw)
        cache.write_bytes(raw)
        return data
    except (urllib.error.URLError, OSError, ValueError):
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def fetch_source(spec: str, force: bool = False, _depth: int = 0) -> list[Recomp]:
    """Every project in one catalog source. An index that only points at other lists ({"lists": [{"remoteLocation": url}]})
    is followed one level, to at most 30 lists. A source that can't be read gives an empty list, never an error."""
    data = _read_source(spec, force)
    if data is None:
        return []
    label = (data.get("name") if isinstance(data, dict) else "") or (DECOMP_GAMES_LABEL if isinstance(data, dict) and "games" in data else "") or Path(urllib.parse.urlparse(spec).path).stem or spec
    if isinstance(data, dict) and isinstance(data.get("lists"), list) and _depth < 1:
        out: list[Recomp] = []
        for item in data["lists"][:30]:
            url = item.get("remoteLocation") if isinstance(item, dict) else None
            if isinstance(url, str) and url.lower().startswith("https://"):
                out += fetch_source(url, force, _depth + 1)
        return out
    return parse_catalog(data, str(label))


DISCOVER_TOPICS = ("recomp", "static-recompilation", "recompilation")


def discover(force: bool = False, min_stars: int = 15) -> list[Recomp]:
    """Projects found by searching GitHub for the recomp topics. Only repos that are not forks or archived and have a few
    stars; the console is read from the repo's topics. Unverified: some hits are tools, not games."""
    cache = root() / "discover.json"
    items = None
    if not force and cache.exists() and time.time() - cache.stat().st_mtime < DISCOVER_TTL:
        try:
            items = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            items = None
    if items is None:
        items = []
        try:
            for topic in DISCOVER_TOPICS:
                url = f"https://api.github.com/search/repositories?q=topic:{topic}+stars:>={min_stars}&sort=stars&per_page=60"
                items += json.loads(_get(url, github_token(), "application/vnd.github+json")).get("items", [])
            cache.write_text(json.dumps(items), encoding="utf-8")
        except (urllib.error.URLError, OSError, ValueError):
            try:
                items = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
    seen, out = set(), []
    for it in items:
        repo = it.get("full_name", "")
        if not repo or repo in seen or it.get("fork") or it.get("archived"):
            continue
        seen.add(repo)
        topics = tuple(it.get("topics") or [])
        system = next((system_of_name(t) for t in topics if system_of_name(t)), "") or UNKNOWN_SYSTEM
        name = it.get("name", repo).replace("-", " ").replace("_", " ")
        game = resolve_game(name, it.get("description") or "", always=True)
        out.append(Recomp(id=_slug(repo), name=name, game=game, system=system, repo=repo, description=it.get("description") or "",
                          tags=("Unverified",), website=it.get("homepage") or "", source="GitHub search",
                          rom=RomSpec(names=(_name_words(game),) if _name_words(game) else ())))
    return out


CURATION_MODES = {"curated": "Active or popular, one project per game", "all": "Everything in the lists"}
ACTIVE_DAYS = 365  # a commit within this many days = in progress
ACTIVE_MIN_STARS = 25  # ...and enough stars that it isn't somebody's empty experiment
POPULAR_STARS = 1000  # this many stars = worth listing even when development has stopped


def _days_since(day: str, now: float | None = None) -> int | None:
    try:
        then = datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(((now if now is not None else time.time()) - then.timestamp()) // 86400)


def curate(entries: Iterable[Recomp], now: float | None = None) -> list[Recomp]:
    """Keep what is in progress (a commit in the last year and a few stars) or popular (1,000+ stars). A popular project
    that has gone quiet is kept but tagged 'Inactive'. Entries with no activity data (hand-written or Quiver lists) are
    left alone: there is nothing to judge them by."""
    out = []
    for r in entries:
        if not r.updated:
            out.append(r)
            continue
        age = _days_since(r.updated, now)
        active = age is not None and age <= ACTIVE_DAYS and r.stars >= ACTIVE_MIN_STARS
        if active:
            out.append(r)
        elif r.stars >= POPULAR_STARS:
            out.append(replace(r, tags=r.tags + ("Inactive",)))
    return out


def _game_key(r: Recomp) -> tuple[str, str]:
    return "".join(sorted(_sig_words(r.game))), r.system.casefold()


def _rank(r: Recomp) -> tuple:
    return (r.score, r.stars, r.updated)


PRIOR_STARS = 100  # how many stars a project needs before its own score counts as much as the catalog-wide average


def weighted_scores(catalog: Iterable[Recomp], prior: int = PRIOR_STARS) -> dict[str, float]:
    """id -> score pulled toward the catalog's average score in proportion to how few stars stand behind it (a Bayesian
    average, as film sites rank with). A 4.8 with 2,000 stars stays near 4.8; a 4.8 with 5 stars lands near the average.
    Stars stand in for the number of ratings, which the project lists do not give. Unscored projects get 0."""
    items = list(catalog)
    scored = [r.score for r in items if r.score > 0]
    mean = sum(scored) / len(scored) if scored else 0.0
    out: dict[str, float] = {}
    for r in items:
        v = max(r.stars, 0)
        out[r.id] = (v * r.score + prior * mean) / (v + prior) if r.score > 0 else 0.0
    return out


def build_catalog(sources: Iterable[str] = (), find_new: bool = False, force: bool = False, curation: str = "curated",
                  builtin: bool = True) -> list[Recomp]:
    """Everything the page lists: the bundled catalog, the built-in community list (awesome-decomp-games), the person's own
    sources, GitHub search, and finally the player's own catalog file.

    A project already listed (same repo) is never replaced by a later source, so the checked entries win; only the
    player's own file overrides by id. In 'curated' mode the lists' entries are filtered to in-progress or popular
    projects, and only the best project per game and console is kept (the others are named on its page); the bundled
    entries are always kept. 'all' lists everything."""
    base = {r.id: r for r in load_catalog([bundled_catalog()])}
    repos = {r.repo.lower() for r in base.values()}
    extra = [r for spec in ([DECOMP_GAMES_URL] if builtin else []) + list(sources) for r in fetch_source(spec, force)]
    if find_new:
        extra += discover(force)
    if curation != "all":
        extra = sorted(curate(extra), key=_rank, reverse=True)  # best first, so the best of each game is the one kept
    winners: dict[tuple[str, str], str] = {}
    folded: dict[str, list[tuple[str, str]]] = {}
    if curation != "all":
        for r in base.values():
            winners.setdefault(_game_key(r), r.id)
    for r in extra:
        if r.repo.lower() in repos:
            continue
        rid = r.id if r.id not in base else _slug(r.repo)
        if rid in base:
            continue
        if curation != "all":
            key = _game_key(r)
            if key in winners:
                folded.setdefault(winners[key], []).append((r.name, r.repo))
                repos.add(r.repo.lower())
                continue
            winners[key] = rid
        repos.add(r.repo.lower())
        base[rid] = r if rid == r.id else replace(r, id=rid)
    for rid, more in folded.items():
        if rid in base and more:
            base[rid] = replace(base[rid], others=base[rid].others + tuple(more))
    for r in load_catalog([user_catalog()]):
        base[r.id] = replace(r, source=r.source or CUSTOM_SOURCE)  # the player's own entries say so on their page, and can be edited there
    return sorted(base.values(), key=lambda r: (r.game.casefold(), r.name.casefold()))


# ---- saved state ----------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Installed:
    id: str
    version: str  # release tag
    folder: str
    exe: str  # relative to folder
    when: float = 0.0
    linked: str = ""  # "" = installed by Distill; "folder" = the person's own install in a folder of its own; "file" = their own, sharing a folder
    stamp: str = ""  # an opaque build id for projects whose tag never changes (a release digest or date)

    def exe_path(self) -> Path:
        return Path(self.folder) / self.exe


@dataclass(slots=True)
class RomMatch:
    status: str  # 'verified' | 'likely' | 'mismatch'
    path: str
    note: str = ""


try:  # an optional file beside this one (distill/local_keys.py, never shipped in an update) that holds the person's own keys
    from .local_keys import SGDB_API_KEY as _LOCAL_SGDB_KEY
except ImportError:
    _LOCAL_SGDB_KEY = ""
BUILTIN_SGDB_KEY = reveal(_LOCAL_SGDB_KEY)  # a key saved from the Sources dialog wins; then this; then $SGDB_API_KEY


class Store:
    """state.json: what is installed, which games the library check found, and a short-lived release cache."""

    def __init__(self, path: Path | None = None):
        self.path = path or root() / "state.json"
        self._lock = threading.RLock()
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.data = {}

    def save(self) -> None:
        with self._lock:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def installed(self, rid: str) -> Installed | None:
        with self._lock:
            d = (self.data.get("installed") or {}).get(rid)
        try:
            return Installed(rid, d["version"], d["folder"], d["exe"], d.get("when", 0.0), d.get("linked", ""), d.get("stamp", "")) if d else None
        except (KeyError, TypeError):
            return None

    def set_installed(self, inst: Installed | None, rid: str = "") -> None:
        with self._lock:
            table = self.data.setdefault("installed", {})
            if inst is None:
                table.pop(rid, None)
            else:
                table[inst.id] = {"version": inst.version, "folder": inst.folder, "exe": inst.exe, "when": inst.when,
                                  "linked": inst.linked, "stamp": inst.stamp}
            self.save()

    def owned(self) -> dict[str, RomMatch]:
        with self._lock:
            raw = dict(self.data.get("owned") or {})
        out = {}
        for rid, d in raw.items():
            try:
                out[rid] = RomMatch(d["status"], d.get("path", ""), d.get("note", ""))
            except (KeyError, TypeError, AttributeError):
                pass
        return out

    def set_owned(self, matches: dict[str, RomMatch]) -> None:
        with self._lock:
            self.data["owned"] = {k: {"status": m.status, "path": m.path, "note": m.note} for k, m in matches.items()}
            self.data["owned_at"] = time.time()
            self.save()

    def sources(self) -> list[str]:
        """The person's own extra project lists (the community list is built in, not a setting)."""
        with self._lock:
            return [x for x in self.data.get("sources") or [] if isinstance(x, str)]

    def curation(self) -> str:
        with self._lock:
            return self.data.get("curation") if self.data.get("curation") in CURATION_MODES else "curated"

    def set_curation(self, mode: str) -> None:
        with self._lock:
            self.data["curation"] = mode if mode in CURATION_MODES else "curated"
            self.save()

    def set_sources(self, sources: list[str], find_new: bool) -> None:
        with self._lock:
            self.data["sources"] = list(dict.fromkeys(s.strip() for s in sources if s.strip()))
            self.data["find_new"] = bool(find_new)
            self.save()

    def find_new(self) -> bool:
        with self._lock:
            return bool(self.data.get("find_new"))

    def sgdb_key(self) -> str:
        """The key saved in Settings > General > API keys; then one saved by an older version here (moved over on first
        use); then the built-in one; then $SGDB_API_KEY."""
        with self._lock:
            if old := str(self.data.pop("sgdb_key", "") or "").strip():
                if not apikeys.get("sgdb"):
                    apikeys.set("sgdb", old)
                self.save()
        return apikeys.get("sgdb") or BUILTIN_SGDB_KEY or os.environ.get("SGDB_API_KEY", "")

    def set_sgdb_key(self, key: str) -> None:
        apikeys.set("sgdb", key)

    def get(self, key: str, default: object = None) -> object:
        with self._lock:
            return self.data.get(key, default)

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def token(self) -> str:
        return github_token() or str(self.data.get("github_token") or "")  # (the field is an older hand-edit; Settings > General holds the key now)

    def cache_get(self, key: str, ttl: float = RELEASE_TTL) -> object | None:
        with self._lock:
            hit = (self.data.get("cache") or {}).get(key)
        return hit["v"] if hit and time.time() - hit["t"] < ttl else None

    def cache_stale(self, key: str) -> object | None:
        with self._lock:
            hit = (self.data.get("cache") or {}).get(key)
        return hit["v"] if hit else None

    def cache_put(self, key: str, value: object) -> None:
        with self._lock:
            self.data.setdefault("cache", {})[key] = {"t": time.time(), "v": value}
            self.save()


# ---- GitHub ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    name: str
    body: str
    published: str
    prerelease: bool
    url: str
    assets: tuple[dict, ...]


class RecompError(RuntimeError):
    """Something the person should be told in plain words."""


def _get(url: str, token: str = "", accept: str = "", timeout: int = 20) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if token and urllib.parse.urlparse(url).hostname in ("api.github.com", "www.steamgriddb.com"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
        return resp.read()


def _release_of(d: dict) -> Release:
    return Release(d.get("tag_name", ""), d.get("name") or d.get("tag_name", ""), d.get("body") or "", (d.get("published_at") or "")[:10],
                   bool(d.get("prerelease")), d.get("html_url", ""),
                   tuple({"name": a.get("name", ""), "size": a.get("size", 0), "url": a.get("browser_download_url", ""),
                          "digest": a.get("digest") or ""} for a in d.get("assets") or []))


def releases(repo: str, store: Store | None = None, force: bool = False) -> list[Release]:
    """Recent releases, newest first, drafts left out. Cached for an hour; when GitHub refuses (rate limit) or is
    unreachable the last cached listing is used instead of an error."""
    key = f"rel:{repo}"
    if store and not force and (hit := store.cache_get(key)) is not None:
        return [_release_of(d) for d in hit]
    url = f"https://api.github.com/repos/{repo}/releases?per_page=15"
    try:
        raw = json.loads(_get(url, store.token() if store else "", "application/vnd.github+json"))
    except urllib.error.HTTPError as exc:
        if store and (stale := store.cache_stale(key)) is not None:
            return [_release_of(d) for d in stale]
        if exc.code in (403, 429):
            raise RecompError("GitHub is rate-limiting this PC. Try again later, or add a GitHub key under Settings > General > API keys.") from exc
        if exc.code == 404:
            raise RecompError(f"{repo} has no public releases.") from exc
        raise RecompError(f"GitHub answered {exc.code}.") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if store and (stale := store.cache_stale(key)) is not None:
            return [_release_of(d) for d in stale]
        raise RecompError("Couldn't reach GitHub.") from exc
    keep = [d for d in raw if isinstance(d, dict) and not d.get("draft")]
    if store:
        store.cache_put(key, [{k: d.get(k) for k in ("tag_name", "name", "body", "published_at", "prerelease", "html_url", "assets")}
                              | {"assets": [{k: a.get(k) for k in ("name", "size", "browser_download_url", "digest")}
                                            for a in d.get("assets") or []]} for d in keep])
    return [_release_of(d) for d in keep]


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_OS_WORDS = {"windows": {"windows", "win", "win32", "win64", "w64", "x86_64-pc-windows"}, "macos": {"macos", "mac", "osx", "darwin", "apple"},
             "linux": {"linux", "appimage", "gnu"}}
_ARM = {"arm64", "aarch64", "arm"}
_X64 = {"x64", "x86_64", "amd64", "win64", "64bit", "64"}
_BAD = {"source", "src", "debug", "symbols", "dbg", "headless", "server", "dedicated", "wasm", "android", "ios", "switch", "wiiu"}


def pick_asset(assets: Iterable[dict], rc: Recomp | None = None, plat: str = "", machine: str = "") -> dict | None:
    """The release file for this OS and CPU: a per-project regex (or list of them, best first; keyed "windows" or "windows-arm64") when the catalog gives one, else a scored guess.
    Archives beat installers; a file naming another OS is out; an ARM build is only taken on ARM."""
    plat, machine = plat or platform_key(), machine or machine_key()
    assets = [a for a in assets if a.get("url") and a.get("name")]
    if rc and (pattern := rc.asset.get(f"{plat}-{machine}") or rc.asset.get(plat)):
        # "windows-arm64" beats "windows"; a list is tried in order, so the build wanted most wins and the next is the fallback
        for one in ([pattern] if isinstance(pattern, str) else pattern):
            rx = re.compile(one, re.IGNORECASE)
            if hit := next((a for a in assets if rx.search(a["name"])), None):
                return hit
        return None
    best, best_score = None, 0
    for a in assets:
        name = a["name"].lower()
        if name.endswith(SKIP_SUFFIXES):
            continue
        is_archive = name.endswith(ARCHIVE_SUFFIXES)
        if not (is_archive or name.endswith(SINGLE_FILE_SUFFIXES) or name.endswith(".dmg")):
            continue
        words = set(_TOKEN_SPLIT.split(name))
        if words & _BAD:
            continue
        mine = bool(words & _OS_WORDS[plat]) or (plat == "linux" and name.endswith(".appimage")) \
            or (plat == "windows" and name.endswith(".exe")) or (plat == "macos" and name.endswith(".dmg"))
        other = any(words & ws for p, ws in _OS_WORDS.items() if p != plat)
        if not mine or (other and not (words & _OS_WORDS[plat])):
            continue
        score = 10 + (4 if is_archive else 0) - (2 if name.endswith(".dmg") else 0) - (3 if name.endswith(".exe") and not is_archive else 0)
        arm = bool(words & _ARM)
        if arm != (machine == "arm64") and not (plat == "macos" and "universal" in words):
            score -= 6 if arm else 1
        if words & _X64 and machine == "x64":
            score += 1
        if "universal" in words:
            score += 1
        if score > best_score:
            best, best_score = a, score
    return best


def choose_release(rels: list[Release], rc: Recomp, plat: str = "", machine: str = "") -> tuple[Release, dict] | None:
    """The newest stable release that has a file for this system; failing that, the newest pre-release that does."""
    for want_pre in (False, True):
        for rel in rels:
            if rel.prerelease != want_pre:
                continue
            if asset := pick_asset(rel.assets, rc, plat, machine):
                return rel, asset
    return None


def sha256_of(asset: dict) -> str:
    d = asset.get("digest") or ""
    return d.split(":", 1)[1].lower() if d.startswith("sha256:") else ""


def is_newer(installed: str, latest: str) -> bool:
    """Different tags mean an update unless the installed one is the newer (compared as dotted numbers when both are)."""
    if installed == latest or re.sub(r"^\D+", "", installed) == re.sub(r"^\D+", "", latest) != "":
        return False  # "1.3.197" against "Canary-1.3.197": the same build, only the tag has a prefix
    a, b = (tuple(int(x) for x in re.findall(r"\d+", t)) for t in (installed, latest))
    return b > a if a and b and a != b else True


# ---- the developer's own description ---------------------------------------------------------------------------
ABOUT_TTL = 24 * 3600


def project_about(repo: str, store: Store | None = None, force: bool = False, fallback: str = "") -> str:
    """The one-line description the developers gave the repository on GitHub, else `fallback`. Cached for a day; when
    GitHub can't be reached the last copy is used."""
    key = f"about:{repo}"
    if store and not force and (hit := store.cache_get(key, ABOUT_TTL)) is not None:
        return str(hit)
    try:
        info = json.loads(_get(f"https://api.github.com/repos/{repo}", store.token() if store else "", "application/vnd.github+json", 15))
        text = " ".join(str(info.get("description") or "").split())
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        text = ""
    if text:
        if store:
            store.cache_put(key, text)
        return text
    return (str(store.cache_stale(key) or "") if store else "") or fallback


# ---- installing -----------------------------------------------------------------------------------------------
class Cancelled(Exception):
    pass


def _safe_join(base: Path, member: str) -> Path:
    target = (base / member).resolve()
    if base.resolve() not in (target, *target.parents):
        raise RecompError("The download contains a file outside its folder; it was not unpacked.")
    return target


NEEDS_7ZIP = ("{name} uses a compression method (such as BCJ2) that Distill's built-in unpacker can't read. "
              "Install 7-Zip (7-zip.org) or NanaZip, then press Install again.")


def _reject_escaping_links(dest: Path) -> None:
    """A native 7-Zip keeps symbolic links as they are in the archive; none may point outside the folder it was unpacked into."""
    base = dest.resolve()
    for folder, dirs, files in os.walk(dest):
        for entry in (*dirs, *files):
            p = Path(folder) / entry
            if p.is_symlink() and base not in (target := p.resolve(), *target.parents):
                raise RecompError("The download contains a link that points outside its folder; it was not unpacked.")


def _extract_7z(archive: Path, dest: Path) -> None:
    """A real 7-Zip (or NanaZip) when one is installed: it reads every .7z filter, which py7zr doesn't (RetroArch's archives use BCJ2),
    and it is far faster. py7zr is the fallback; when that can't read the archive either, say what to install."""
    from .constants import SUBPROCESS_FLAGS
    from .system import _py7zr, find_7zip
    if sevenzip := find_7zip():
        run = subprocess.run([sevenzip, "x", "-y", "-bso0", "-bsp0", f"-o{dest}", str(archive)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", creationflags=SUBPROCESS_FLAGS)
        if run.returncode not in (0, 1):  # 1 is a warning (a file that couldn't be read back, say), not a failure
            tail = (run.stderr or "").strip().splitlines()[-1:] or [""]
            raise RecompError(f"7-Zip couldn't unpack {archive.name} (exit code {run.returncode}). {tail[0]}".strip())
        _reject_escaping_links(dest)
        return
    try:
        py7zr = _py7zr()
    except RuntimeError as exc:
        raise RecompError(NEEDS_7ZIP.format(name=archive.name)) from exc
    try:
        with py7zr.SevenZipFile(archive) as z:
            for member in z.getnames():
                _safe_join(dest, member)
            z.extractall(path=dest)
    except RecompError:
        raise
    except Exception as exc:  # py7zr raises its own family (UnsupportedCompressionMethodError, Bad7zFile...) plus lzma / OS errors
        if "Unsupported" in type(exc).__name__ or "filter" in str(exc).lower():
            raise RecompError(NEEDS_7ZIP.format(name=archive.name)) from exc
        raise RecompError(f"Couldn't unpack {archive.name}: {type(exc).__name__}: {exc}") from exc


def extract(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                _safe_join(dest, info.filename)
            z.extractall(dest)
            for info in z.infolist():  # zip keeps the executable bit in the high bits of external_attr
                mode = info.external_attr >> 16
                if mode & stat.S_IXUSR and (p := dest / info.filename).is_file():
                    p.chmod(p.stat().st_mode | 0o111)
    elif name.endswith(".7z"):
        _extract_7z(archive, dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                _safe_join(dest, m.name)
                if m.issym() or m.islnk():  # a link could point anywhere once unpacked
                    _safe_join(dest, str(Path(m.name).parent / m.linkname))
            if hasattr(tarfile, "data_filter"):  # the safe-extraction filter: 3.12+, and backported to 3.10.12 / 3.11.4
                t.extractall(dest, filter="data")
            else:
                t.extractall(dest)  # members were checked above
    else:
        raise RecompError(f"Don't know how to unpack {archive.name}.")


def _extract_dmg(dmg: Path, dest: Path) -> None:
    if sys.platform != "darwin":
        raise RecompError("A .dmg can only be opened on a Mac.")
    mount = Path(tempfile.mkdtemp(prefix="dmg_"))
    try:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg)],
                       check=True, capture_output=True, timeout=120)
        apps = list(mount.glob("*.app"))
        if not apps:
            raise RecompError("The disk image has no app in it.")
        for app in apps:
            shutil.copytree(app, dest / app.name, symlinks=True, dirs_exist_ok=True)
    except subprocess.SubprocessError as exc:
        raise RecompError("Couldn't open the disk image.") from exc
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
        shutil.rmtree(mount, ignore_errors=True)


_NOT_THE_GAME = re.compile(r"unins|uninst|crash|updater|vc_?redist|dxsetup|setup|installer|helper|launcher_?old|elevate|7z", re.IGNORECASE)


def find_executable(folder: Path, rc: Recomp, plat: str = "") -> Path | None:
    """The file to run inside an unpacked download: the catalog's regex if given, else the best name match."""
    plat = plat or platform_key()
    if rc.exe:
        rx = re.compile(rc.exe, re.IGNORECASE)
        for p in sorted(folder.rglob("*")):
            if rx.search(p.relative_to(folder).as_posix()) and (p.is_file() or p.suffix == ".app"):
                return p
        return None
    cands: list[Path] = []
    if plat == "macos":
        cands = [p for p in folder.rglob("*.app") if p.is_dir()]
    else:
        for p in folder.rglob("*"):
            if not p.is_file() or _NOT_THE_GAME.search(p.name):
                continue
            low = p.name.lower()
            if plat == "windows" and low.endswith(".exe"):
                cands.append(p)
            elif plat == "linux" and (low.endswith(".appimage") or "." not in low and _looks_executable(p)):
                cands.append(p)
    if not cands:
        return None
    words = {w for w in _TOKEN_SPLIT.split(f"{rc.name} {rc.repo} {rc.id}".lower()) if len(w) > 2}

    def score(p: Path) -> tuple:
        stem = set(_TOKEN_SPLIT.split(p.stem.lower()))
        return (len(stem & words), -len(p.relative_to(folder).parts), p.stat().st_size)
    return max(cands, key=score)


def _looks_executable(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return magic == b"\x7fELF" or magic[:2] == b"#!"


def writable(folder: Path) -> bool:
    """Can this program add files to the folder? (Windows keeps folders like Program Files closed to normal programs, and
    asking the permission bits is not reliable there, so this simply tries.)"""
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".distill-write-test-"):
            pass
        return True
    except OSError:
        return False


def not_writable_message(name: str, folder: Path | str) -> str:
    return (f"{name} is in {folder}, a protected folder that Distill isn't allowed to change (Windows keeps folders such as "
            f"Program Files closed to normal programs), so it can't be updated there.\n\nMove {name} to a folder you can write to "
            f"(for example inside your Emulators folder), link it again with \"I already have it...\", then update it.")


def _replace_file(item: Path, target: Path) -> None:
    """Move `item` to `target`, replacing what is there. The new file is put beside the old one first and swapped in with one
    rename, so a failure part-way can never leave the player without the file, and a refusal from the system (a protected
    folder, a file in use) is reported in plain words instead of as a raw error."""
    side = target.with_name(target.name + ".distill-new")
    try:
        shutil.move(str(item), str(side))
        os.replace(side, target)
    except OSError as exc:
        try:
            side.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, PermissionError):
            hint = " It may be in use (close it and try again), or in a protected folder such as Program Files (move the program to a folder you can write to)."
        else:
            hint = ""
        raise RecompError(f"Couldn't replace {target}: {exc.strerror or exc}.{hint}") from exc


def _merge_tree(src: Path, dest: Path) -> None:
    """Copy src over dest: new files replace old ones of the same name; anything else already there (saves, settings,
    the player's own mods) is left alone. Updating therefore never costs the player their data."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecompError(f"Couldn't create {dest}: {exc.strerror or exc}.") from exc
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir() and not item.is_symlink():
            _merge_tree(item, target)
        else:
            _replace_file(item, target)


class Installer:
    """One install or update. `progress(text, done, total)` is called from the worker thread; `cancel` stops it."""

    def __init__(self, store: Store, progress: Callable[[str, int, int], None] = lambda *_: None,
                 cancel: threading.Event | None = None):
        self.store, self.progress, self.cancel = store, progress, cancel or threading.Event()

    def folder_for(self, rc: Recomp) -> Path:
        return root() / "games" / rc.id

    def live_for(self, rc: Recomp) -> Path:
        """Where the unpacked files end up. Subclasses may point this at a folder the person chose."""
        return self.folder_for(rc) / "app"

    def record_for(self, rc: Recomp, rel: Release, asset: dict, live: Path, exe: Path) -> Installed:
        return Installed(rc.id, rel.tag, str(live), exe.relative_to(live).as_posix(), time.time())

    def find_exe(self, live: Path, rc: Recomp) -> Path | None:
        return find_executable(live, rc)

    def check_asset(self, rc: Recomp, asset: dict) -> None:
        """Called before anything is downloaded; raise RecompError to refuse the file."""

    def download(self, asset: dict, dest: Path) -> None:
        req = urllib.request.Request(asset["url"], headers={"User-Agent": USER_AGENT})
        sha, done = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, dest.open("wb") as out:
                total = int(resp.headers.get("Content-Length") or asset.get("size") or 0)
                while chunk := resp.read(1 << 18):
                    if self.cancel.is_set():
                        raise Cancelled
                    out.write(chunk)
                    sha.update(chunk)
                    done += len(chunk)
                    self.progress(f"Downloading {asset['name']}  {fmt_size(done)}" + (f" / {fmt_size(total)}" if total else ""), done, total)
        except (urllib.error.URLError, OSError) as exc:
            raise RecompError(f"Download failed: {exc}") from exc
        if (size := asset.get("size")) and done != size:
            raise RecompError("The download was incomplete.")
        if (want := sha256_of(asset)) and sha.hexdigest() != want:
            raise RecompError("The download's checksum doesn't match what GitHub published; it was discarded.")

    def install(self, rc: Recomp, rel: Release, asset: dict) -> Installed:
        self.check_asset(rc, asset)
        game_dir = self.folder_for(rc)
        game_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=game_dir))
        try:
            archive = tmp / asset["name"]
            self.download(asset, archive)
            if self.cancel.is_set():
                raise Cancelled
            self.progress("Unpacking...", 0, 0)
            stage = tmp / "stage"
            low = archive.name.lower()
            if low.endswith(ARCHIVE_SUFFIXES):
                extract(archive, stage)
            elif low.endswith(".dmg"):
                stage.mkdir()
                _extract_dmg(archive, stage)
            else:  # a single AppImage / exe
                stage.mkdir()
                shutil.move(str(archive), str(stage / archive.name))
                (stage / archive.name).chmod(0o755)
            while True:  # a lone top-level folder ("Game-1.2/") is peeled so updates always land in the same place
                kids = list(stage.iterdir())
                if len(kids) == 1 and kids[0].is_dir() and kids[0].suffix != ".app":
                    inner = kids[0]
                    inner.rename(stage.with_name("peeled"))
                    shutil.rmtree(stage)
                    stage = stage.with_name("peeled")
                else:
                    break
            live = self.live_for(rc)
            _merge_tree(stage, live)
            exe = self.find_exe(live, rc)
            if exe is None:
                raise RecompError("Unpacked the download but found nothing to run in it. Open the release page to install by hand.")
            if sys.platform != "win32" and exe.is_file():
                exe.chmod(exe.stat().st_mode | 0o111)
            inst = self.record_for(rc, rel, asset, live, exe)
            self.store.set_installed(inst)
            self.progress("Installed.", 1, 1)
            return inst
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def uninstall(store: Store, rc: Recomp) -> None:
    shutil.rmtree(root() / "games" / rc.id, ignore_errors=True)
    store.set_installed(None, rc.id)


def _is_console_exe(exe: Path) -> bool:
    """True for a Windows program built for a console (PE subsystem 3). Such a program must be given a console of its own when started
    from a window-only app like this one: with none, some (Ryujinx) stop at once. A normal double-click gives them one."""
    try:
        with exe.open("rb") as f:
            head = f.read(64)
            at = int.from_bytes(head[0x3C:0x40], "little")
            f.seek(at)
            pe = f.read(24 + 70)
    except OSError:
        return False
    return len(pe) >= 24 + 70 and pe[:4] == b"PE\0\0" and int.from_bytes(pe[24 + 68:24 + 70], "little") == 3


def launch(inst: Installed) -> None:
    exe = inst.exe_path()
    if not exe.exists():
        raise RecompError("The game's files are missing. Install it again.")
    flags = 0
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        if _is_console_exe(exe):
            flags, quiet = subprocess.CREATE_NEW_CONSOLE, {}  # as when double-clicked: its own console, its own streams
        else:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        cmd = [str(exe)]
    elif sys.platform == "darwin" and exe.suffix == ".app":
        cmd = ["open", str(exe)]
    else:
        cmd = [str(exe)]
    try:
        subprocess.Popen(cmd, cwd=str(exe.parent), creationflags=flags, close_fds=True,
                         start_new_session=(sys.platform != "win32"), **quiet)
    except OSError as exc:
        raise RecompError(f"Couldn't start it: {exc}") from exc


# ---- do I own the game? ---------------------------------------------------------------------------------------
_N64_MAGIC = {b"\x80\x37\x12\x40": None, b"\x37\x80\x40\x12": "v64", b"\x40\x12\x37\x80": "n64"}


def rom_hashes(path: Path, stop: threading.Event | None = None) -> tuple[str, str] | None:
    """(sha1, crc32) of a dump as the preservation databases list it. N64 dumps in the other byte orders (.v64, .n64)
    are put back to big-endian first, so the same game matches whatever the file was saved as."""
    sha, crc = hashlib.sha1(), 0
    try:
        with path.open("rb") as f:
            head = f.read(4)
            order = _N64_MAGIC.get(head)
            f.seek(0)
            while chunk := f.read(1 << 20):
                if stop is not None and stop.is_set():
                    return None
                if order == "v64":
                    chunk = _swap(chunk, 2)
                elif order == "n64":
                    chunk = _swap(chunk, 4)
                sha.update(chunk)
                crc = zlib.crc32(chunk, crc)
    except OSError:
        return None
    return sha.hexdigest(), f"{crc & 0xFFFFFFFF:08x}"


def _swap(data: bytes, width: int) -> bytes:
    data = data[:len(data) - len(data) % width]
    out = bytearray(data)
    for i in range(width):
        out[i::width] = data[width - 1 - i::width]
    return bytes(out)


def _words(name: str) -> set[str]:
    return {w for w in _TOKEN_SPLIT.split(name.lower()) if w}


def _same_system(a: str, b: str) -> bool:
    """Same console. A community entry whose console isn't known ('Unknown') is compared by name alone."""
    return b == UNKNOWN_SYSTEM or a.casefold() == b.casefold()


def match_library(recomps: Iterable[Recomp], entries: Iterable, stop: threading.Event | None = None,
                  note: Callable[[str], None] = lambda _t: None) -> dict[str, RomMatch]:
    """Which projects the library can run. `entries` are library report rows (name, system, kind, size, path).

    A file counts when it is for the project's console and its name holds the game's words ('likely'). When the
    catalog lists checksums the file is hashed too: a hit is 'verified', a miss 'mismatch' (the wrong region or a
    bad dump). Only files that look right are ever read."""
    entries = [e for e in entries if getattr(e, "path", "")]
    out: dict[str, RomMatch] = {}
    cache: dict[str, tuple[str, str] | None] = {}
    for rc in recomps:
        if stop is not None and stop.is_set():
            break
        best: RomMatch | None = None
        rank = {"verified": 3, "likely": 2, "mismatch": 1}
        for e in entries:
            if not _same_system(e.system, rc.system):
                continue
            ext = "." + e.kind.lower().lstrip(".")
            words = _sig_words(Path(e.name).stem)
            named = any(bool(need := {w for w in n if w not in _STOP_WORDS}) and need <= words for n in rc.rom.names)
            if not named and not (rc.rom.sha1 or rc.rom.crc32):
                continue
            raw = not rc.rom.exts or ext in rc.rom.exts
            hashable = bool(rc.rom.sha1 or rc.rom.crc32) and raw and e.size <= HASH_LIMIT
            got = None
            if hashable:
                if e.path not in cache:
                    note(f"Checking {e.name}...")
                    cache[e.path] = rom_hashes(Path(e.path), stop)
                got = cache[e.path]
            result: RomMatch | None = None
            if got and (got[0] in rc.rom.sha1 or got[1] in rc.rom.crc32):
                result = RomMatch("verified", e.path)
            elif got and named:
                result = RomMatch("mismatch", e.path, "The file's checksum isn't one this project accepts (wrong region or revision?).")
            elif not got and named:
                result = RomMatch("likely", e.path, "" if raw else f"It's a {ext[1:].upper()} file; the project needs the original dump.")
            if result and (best is None or rank[result.status] > rank[best.status]):
                best = result
        if best:
            out[rc.id] = best
    return out


# ---- cover art ------------------------------------------------------------------------------------------------
_IMG_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")  # PNG, JPEG, GIF, WebP


def art_dir() -> Path:
    d = root() / "posters-v3"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_cover(rc: Recomp) -> Path | None:
    return next((p for p in art_dir().glob(f"{rc.id}.*") if p.suffix.lower() in (".jpg", ".png", ".gif", ".webp")), None)


def _image(url: str) -> bytes | None:
    try:
        data = _get(url, timeout=15)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if 1024 < len(data) <= MAX_ART_BYTES and data.startswith(_IMG_MAGIC) else None


def _ext_of(data: bytes) -> str:
    return ".png" if data.startswith(b"\x89PNG") else ".gif" if data.startswith(b"GIF8") else ".webp" if data.startswith(b"RIFF") else ".jpg"


_LB_SEARCH = "https://gamesdb.launchbox-app.com/games/results/"
_LB_DETAIL = "https://gamesdb.launchbox-app.com/games/details/"
_LB_IMAGES = "https://gamesdb.launchbox-app.com/games/images/"
_LB_IMG = re.compile(r"https?://images\.launchbox-app\.com/[0-9a-f-]{36}\.(?:jpg|jpeg|png)", re.IGNORECASE)
_LB_LINK = re.compile(r"/games/(?:details|images)/(\d+)(?:-([a-z0-9-]+))?", re.IGNORECASE)
_OG_IMG = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
LB_PLATFORMS = {"GameCube": "Nintendo GameCube", "Wii": "Nintendo Wii", "Xbox": "Microsoft Xbox", "Xbox 360": "Microsoft Xbox 360",
                "PlayStation": "Sony Playstation", "PlayStation 2": "Sony Playstation 2", "PlayStation 3": "Sony Playstation 3",
                "PlayStation 4": "Sony Playstation 4", "SNES": "Super Nintendo Entertainment System", "NES": "Nintendo Entertainment System",
                "Dreamcast": "Sega Dreamcast", "Saturn": "Sega Saturn"}
# image types worth showing as a poster, best first. Posters are portrait, which is what the grid wants.
_LB_TYPES = (("poster", re.compile(r"poster", re.IGNORECASE)), ("box front", re.compile(r"box\s*-\s*front", re.IGNORECASE)),
             ("flyer", re.compile(r"flyer\s*-\s*front", re.IGNORECASE)))


_STOP_WORDS = {"the", "of", "a", "an", "and", "for", "to"}


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}  # 'Gothic II' = 'Gothic 2'


def _sig_words(text: str) -> set[str]:
    """The words that identify a title: lower-cased, apostrophes dropped ("Majora's" = "majoras"), roman numerals made
    arabic, small words ignored."""
    return {_ROMAN.get(w, w) for w in _TOKEN_SPLIT.split(re.sub(r"['\u2019]", "", text.lower())) if w and w not in _STOP_WORDS}


def _squash(text: str) -> str:
    return _TOKEN_SPLIT.sub("", re.sub(r"['\u2019]", "", text.lower()))


def _names_game(game: str, found: str) -> bool:
    """`found` (a search result's title) is this game: it has every word of the title, or, for a long enough title, the same
    letters run together ('Sim City 2000' is 'SimCity 2000')."""
    if _sig_words(game) <= _sig_words(found):
        return True
    a = _squash(game)
    return len(a) >= 8 and a in _squash(found)


def _lb_game_ids(page: str, game: str, platform: str) -> list[str]:
    """Search-result games worth trying, best first. A result must be this game (its address names every word of the
    title); among those the right console comes first, then the fewest extra words ('... Multiplayer' loses to the
    plain title). Results are never taken on position alone, which is how a different game's box got shown."""
    want = _sig_words(game)
    seen: dict[str, dict] = {}
    for m in _LB_LINK.finditer(page):
        info = seen.setdefault(m[1], {"slug": "", "plat": False, "pos": len(seen)})
        info["slug"] = info["slug"] or (m[2] or "")
        if platform.lower() in page[max(0, m.start() - 300):m.end() + 600].lower():
            info["plat"] = True
    ranked = []
    for gid, i in seen.items():
        words = _sig_words(i["slug"].replace("-", " ")) if i["slug"] else None
        if words is not None and not _names_game(game, i["slug"].replace("-", " ")):
            continue
        ranked.append((not i["plat"], len(words - want) if words is not None else 99, i["pos"], gid))
    return [g for *_k, g in sorted(ranked)]


def _lb_posters(page: str) -> list[str]:
    """Poster-like pictures on a LaunchBox page, best first (Poster, then Box - Front, then Flyer - Front). Each image
    is labelled by the nearest type name in the text around its address."""
    found: dict[str, int] = {}
    for m in _LB_IMG.finditer(page):
        lo = max(0, m.start() - 400)
        around = page[lo:m.end() + 400]
        best = None
        for rank, (_name, rx) in enumerate(_LB_TYPES):
            for hit in rx.finditer(around):
                dist = abs(lo + hit.start() - m.start())
                if best is None or dist < best[0]:
                    best = (dist, rank)
        if best is not None and m[0] not in found:
            found[m[0]] = best[1]
    return [u for u, _r in sorted(found.items(), key=lambda kv: kv[1])]


_ARABIC_ROMAN = {"2": "II", "3": "III", "4": "IV", "5": "V", "6": "VI", "7": "VII", "8": "VIII", "9": "IX"}


def _title_fallbacks(game: str) -> list[str]:
    """Titles to search for, most specific first: the full title, then without the subtitle ('Gothic 2: Night of the Raven' and
    'Gothic 2 Night of the Raven' both give 'Gothic 2'). A poster for the base game beats no poster."""
    out = [game]
    head = re.split(r"\s*(?::|\s-\s|\u2013|\u2014)\s*", game, maxsplit=1)[0]
    out.append(head)
    words = head.split()
    for i, w in enumerate(words[:-1]):  # a number or roman numeral with words after it ends the base title
        if i and (w.isdigit() or w.upper() in _ARABIC_ROMAN.values()):
            out.append(" ".join(words[:i + 1]))
            break
    return [t for t in dict.fromkeys(out) if len(t) >= 3 and _sig_words(t)]


def _search_queries(title: str) -> list[str]:
    """What to type into a poster search for the title: as written, letters only, and with its numbers as roman numerals."""
    roman = " ".join(_ARABIC_ROMAN.get(w, w) for w in title.split())
    return list(dict.fromkeys([title, " ".join(_name_words(title.replace("'", ""))), roman]))


def _lb_posters_for(ids: list[str]) -> list[str]:
    for gid in ids[:3]:
        out: list[str] = []
        for url in (_LB_IMAGES + gid, _LB_DETAIL + gid):
            try:
                page = _get(url, timeout=15).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, ValueError):
                continue
            out += [u for u in _lb_posters(page) if u not in out]
            if url.startswith(_LB_DETAIL) and (m := _OG_IMG.search(page)):
                out.append(urllib.parse.urljoin(_LB_DETAIL, m[1]))
        if out:
            return out
    return []


def launchbox_covers(rc: Recomp) -> list[str]:
    """Poster candidates for the game from the LaunchBox Games Database, best first, by scraping its public pages:
    search (title and console checked), then each likely game's images page, then its details page. Best effort: when
    the site's layout changes this finds nothing and the grid shows a plain placeholder instead."""
    if rc.launchbox_id:
        return _lb_posters_for([rc.launchbox_id])
    platform = LB_PLATFORMS.get(rc.system, rc.system if rc.system != UNKNOWN_SYSTEM else "")
    for title in _title_fallbacks(rc.game):
        for q in _search_queries(title):
            try:
                page = _get(_LB_SEARCH + urllib.parse.quote(q), timeout=15).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if ids := _lb_game_ids(page, title, platform):
                if out := _lb_posters_for(ids):
                    return out
                break
    return []


_META_DESC = re.compile(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']*)["\']'
                        r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\'](?:og:)?description["\']', re.IGNORECASE)
_LB_OVERVIEW_END = ("Developers", "Publishers", "Platform", "Genre", "Alternate Names", "Wikipedia", "Video", "Advertisement", "Media")
OVERVIEW_TTL = 7 * 24 * 3600


def _lb_overview(page: str) -> str:
    """The Overview text on a LaunchBox game page, or ''. The page's description tag only holds the start of a long one, so the text
    under the Overview heading is read first (up to the next labelled block); the description tag is the fallback."""
    full = ""
    if m := re.search(r"<h[1-6][^>]*>\s*Overview\b.*?</h[1-6]>(.*)", page, re.IGNORECASE | re.DOTALL):
        section = m[1]
        if cut := re.search(r"(?i)>\s*(?:Developers?|Publishers?|Genres?|Alternate Names|Also Known As|Wikipedia)\s*:?\s*<", section):
            section = section[:max(0, section.rfind("<", 0, cut.start() + 1))]  # the facts table after the Overview (who made it, genre...) is not part of it
        body = re.sub(r"(?i)<li\b[^>]*>", "\n\u2022 ", section)
        body = re.sub(r"(?i)</?(?:p|br|li|ul|ol|div|section|h[1-6]|tr|td)\b[^>]*>", "\n", body)  # block tags end a line; inline ones don't
        body = html_lib.unescape(re.sub(r"<[^>]+>", "", body))
        kept: list[str] = []
        for ln in (x.strip() for x in body.splitlines()):
            if not ln or ln == "Edit":
                continue
            if ln.rstrip(":").strip() in _LB_OVERVIEW_END or re.match(r"(?i)(?:developers?|publishers?|genres?|alternate names|also known as)\b\s*:?\s*$", ln) \
                    or (len(ln) < 120 and re.match(r"(?:Developers|Publishers)\s+\S", ln)):
                break
            kept.append(re.sub(r"^[*\u2022-]\s+", "\u2022 ", ln))
        full = "\n\n".join(kept)
    short = ""
    if m := _META_DESC.search(page):
        short = html_lib.unescape(m[1] or m[2] or "").strip()
        short = re.sub(r"[ \t]*\*[ \t]+", "\n\n\u2022 ", short)  # the description tag runs bullets together as ' * item'
    text = max((full.strip(), short), key=len)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) >= 20 else ""


def launchbox_overview(rc: Recomp, store: Store | None = None, force: bool = False) -> str:
    """The LaunchBox Games Database Overview of the game a project plays, or '' when it has none (an engine remake of a
    game nobody listed, say). Found the way covers are: the catalog's id, else a search checked for title and console.
    Cached for a week, a miss too."""
    key = f"lbov2:{rc.id}"
    if store and not force and (hit := store.cache_get(key, OVERVIEW_TTL)) is not None:
        return str(hit)

    def read(ids: list[str]) -> str:
        for gid in ids[:3]:
            try:
                page = _get(_LB_DETAIL + gid, timeout=15).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if text := _lb_overview(page):
                return text
        return ""
    text, reached = "", False
    if rc.launchbox_id:
        text = read([rc.launchbox_id])
    else:
        platform = LB_PLATFORMS.get(rc.system, rc.system if rc.system != UNKNOWN_SYSTEM else "")
        for title in _title_fallbacks(rc.game):
            for q in _search_queries(title):
                try:
                    page = _get(_LB_SEARCH + urllib.parse.quote(q), timeout=15).decode("utf-8", "replace")
                    reached = True
                except (urllib.error.URLError, OSError, ValueError):
                    continue
                if ids := _lb_game_ids(page, title, platform):
                    if text := read(ids):
                        break
                    break
            if text:
                break
    if store and (text or reached or rc.launchbox_id):  # a miss is only remembered when the site answered
        store.cache_put(key, text)
    return text or (str(store.cache_stale(key) or "") if store else "")


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG, JPEG or GIF from its header, or None (WebP and anything odd)."""
    try:
        if data.startswith(b"\x89PNG"):
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data.startswith(b"GIF8"):
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except (IndexError, ValueError):
        pass
    return None


def is_portrait(data: bytes) -> bool:
    """Tall enough to be a poster. A picture whose shape can't be read is given the benefit of the doubt."""
    size = image_size(data)
    return size is None or size[1] >= size[0] * 1.15


SGDB_API = "https://www.steamgriddb.com/api/v2"
SGDB_DIMS = "600x900,342x482,660x930"  # SteamGridDB's portrait grid sizes: real posters, not banners or squares


class SgdbBusy(Exception):
    """SteamGridDB kept answering 'too many requests'. Not a miss: the game must not be remembered as having no poster."""


def _sgdb(path: str, key: str) -> list[dict]:
    for attempt in range(3):  # a 429 is waited out (as long as the server says, up to 5 s), not taken for "no poster"
        try:
            data = json.loads(_get(SGDB_API + path, key, "application/json", 15))
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            if attempt == 2:
                raise SgdbBusy from exc
            try:
                wait = float(exc.headers.get("Retry-After", "")) if exc.headers else 0.0
            except ValueError:
                wait = 0.0
            time.sleep(min(5.0, wait or 1.5 * (attempt + 1)))
    out = data.get("data") if isinstance(data, dict) else None
    return [d for d in out if isinstance(d, dict)] if isinstance(out, list) else []


def sgdb_test(key: str) -> str:
    """'' when the key works, else a sentence saying what is wrong."""
    try:
        _sgdb("/search/autocomplete/zelda", key)
        return ""
    except SgdbBusy:
        return "SteamGridDB is busy right now. Try again in a minute."
    except urllib.error.HTTPError as exc:
        return "SteamGridDB rejected the key." if exc.code in (401, 403) else f"SteamGridDB answered {exc.code}."
    except (urllib.error.URLError, OSError, ValueError):
        return "Couldn't reach SteamGridDB."


def sgdb_posters(rc: Recomp, key: str) -> list[str]:
    """600x900-style portrait posters for the game from SteamGridDB, best-scored first. The search result must be this game
    (every word of the title in its name); the plainest title wins. When the full title finds nothing, the title without its
    subtitle is tried ('Gothic 2'). Needs the person's own free API key."""
    for title in _title_fallbacks(rc.game):
        want = _sig_words(title)
        found: list[dict] = []
        for q in _search_queries(title):
            try:
                found = _sgdb("/search/autocomplete/" + urllib.parse.quote(q, safe=""), key)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            found = [g for g in found if _names_game(title, str(g.get("name", "")))]
            if found:
                break
        found.sort(key=lambda g: (len(_sig_words(str(g.get("name", ""))) - want), not g.get("verified")))
        for g in found[:3]:
            try:
                grids = _sgdb(f"/grids/game/{int(g['id'])}?dimensions={SGDB_DIMS}&types=static&nsfw=false&humor=false", key)
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                continue
            urls = [x["url"] for x in sorted(grids, key=lambda x: -(x.get("score") or 0)) if isinstance(x.get("url"), str)]
            if urls:
                return urls[:5]
    return []


def _src_file(rc: Recomp) -> Path:
    return art_dir() / f"{rc.id}.src"


def cover_source(rc: Recomp) -> str:
    """Where the cached poster came from ('steamgriddb', 'other', or 'steamgriddb-miss' = SteamGridDB had none)."""
    try:
        return _src_file(rc).read_text().strip()
    except OSError:
        return ""


def cover_settled(rc: Recomp, sgdb_key: str = "") -> bool:
    """True when the cached poster is as good as it will get: any poster with no SteamGridDB key, or one that came from
    SteamGridDB (or that SteamGridDB has been asked about and has nothing for). A cover the person picked is final, and so
    is the choice to have none: neither is ever looked up again."""
    source = cover_source(rc)
    if source == REMOVED_SRC:
        return True
    return cached_cover(rc) is not None and (not sgdb_key or source in ("steamgriddb", "steamgriddb-miss", CUSTOM_SRC))


def fetch_cover(rc: Recomp, token: str = "", retry: bool = False, sgdb_key: str = "") -> Path | None:
    """The cached poster, or find and cache one. Tries, in order: SteamGridDB (with the person's key), LaunchBox's poster
    (else front box art), the catalog's own address, a file in the repo. A portrait picture always beats a landscape one.
    With a key and an older non-SteamGridDB poster cached, only SteamGridDB is tried, to upgrade it. Returns None when
    nothing worked (remembered for a week, so a missing poster doesn't cost a search every launch)."""
    old = cached_cover(rc)
    if old and cover_settled(rc, sgdb_key):
        return old
    if not old and not retry and cover_source(rc) == REMOVED_SRC:
        return None  # the person took the cover off on purpose: not searched for again
    miss = art_dir() / f"{rc.id}.none"
    if not old and not retry and miss.exists() and time.time() - miss.stat().st_mtime < ART_RETRY:
        try:
            if miss.read_text(encoding="utf-8").strip() == rc.game:  # looked for this title lately; a different title is worth a new search
                return None
        except OSError:
            return None
    sources: list[tuple[str, Callable[[], list[str]]]] = []
    if sgdb_key:
        sources.append(("steamgriddb", lambda: sgdb_posters(rc, sgdb_key)))
    if not old:
        sources.append(("other", lambda: launchbox_covers(rc)))
        if rc.cover:
            sources.append(("other", lambda: [rc.cover]))
        if rc.cover_path:
            sources.append(("other", lambda: [f"https://raw.githubusercontent.com/{rc.repo}/HEAD/{rc.cover_path.lstrip('/')}"]))
    # No README picture or owner avatar as a last resort: those are banners and logos, and a plain placeholder looks tidier.
    spare: tuple[str, bytes] | None = None  # first picture that worked but is not portrait: used only if nothing portrait turns up
    chosen: tuple[str, bytes] | None = None
    for name, source in sources:
        try:
            urls = source()
        except SgdbBusy:
            raise  # rate-limited: say nothing was decided, so the game is asked about again later
        except Exception:  # a scraper must never take the page down
            continue
        for url in filter(None, urls):
            if not (data := _image(url)):
                continue
            if is_portrait(data):
                chosen = (name, data)
                break
            spare = spare or (name, data)
        if chosen:
            break
    if old and not chosen:  # the upgrade found nothing: keep what we have and don't ask SteamGridDB again
        _src_file(rc).write_text("steamgriddb-miss")
        return old
    chosen = chosen or spare
    if chosen:
        for f in art_dir().glob(f"{rc.id}.*"):
            f.unlink(missing_ok=True)
        dest = art_dir() / f"{rc.id}{_ext_of(chosen[1])}"
        dest.write_bytes(chosen[1])
        _src_file(rc).write_text(chosen[0])
        return dest
    miss.write_text(rc.game, encoding="utf-8")
    if sgdb_key:
        _src_file(rc).write_text("steamgriddb-miss")
    return None


# ---- a cover the person chooses ---------------------------------------------------------------------------------
CUSTOM_SRC = "custom"  # the cached poster was picked in the Add / Edit window (SteamGridDB search or a file of their own)
REMOVED_SRC = "removed"  # the person took the cover off: there is none, and none is looked for
POSTER_SIZE = (600, 900)  # SteamGridDB's standard poster, 2:3. The Add / Edit window's empty poster has this shape


def sgdb_games(term: str, key: str) -> list[dict]:
    """Games on SteamGridDB that match a search, as {"id", "name", "verified"}, best first. [] for a blank search.
    Raises SgdbBusy when SteamGridDB keeps saying 'too many requests', and urllib errors when it can't be reached."""
    term = (term or "").strip()
    if not term:
        return []
    found = _sgdb("/search/autocomplete/" + urllib.parse.quote(term, safe=""), key)
    return [{"id": int(g["id"]), "name": str(g.get("name") or ""), "verified": bool(g.get("verified"))}
            for g in found if isinstance(g.get("id"), int)][:12]


def sgdb_grids(game_id: int, key: str) -> list[dict]:
    """The portrait posters SteamGridDB has for one game, highest scored first: {"url", "thumb", "score", "author"}."""
    grids = _sgdb(f"/grids/game/{int(game_id)}?dimensions={SGDB_DIMS}&types=static&nsfw=false&humor=false", key)
    out = []
    for g in sorted(grids, key=lambda x: -(x.get("score") or 0)):
        url = g.get("url")
        if isinstance(url, str) and url.startswith("http"):
            thumb = g.get("thumb") if isinstance(g.get("thumb"), str) and str(g.get("thumb")).startswith("http") else url
            author = g.get("author") if isinstance(g.get("author"), dict) else {}
            out.append({"url": url, "thumb": thumb, "score": g.get("score") or 0, "author": str(author.get("name") or "")})
    return out


def fetch_image(url: str) -> bytes | None:
    """A picture from the web, if it is one this app can show and is not too big. None otherwise."""
    return _image(url)


def check_cover_bytes(data: bytes) -> bytes:
    """`data` when it is a picture the posters can use (PNG, JPEG, GIF or WebP, under the size limit); else a RecompError in
    plain words."""
    if not data.startswith(_IMG_MAGIC) or (data.startswith(b"RIFF") and data[8:12] != b"WEBP"):
        raise RecompError("That isn't a picture Distill can use. Pick a PNG, JPEG, WebP or GIF.")
    if len(data) > MAX_ART_BYTES:
        raise RecompError(f"That picture is too big ({len(data) / (1 << 20):.1f} MB). Pick one under {MAX_ART_BYTES >> 20} MB.")
    return data


def set_cover(rid: str, data: bytes, source: str = CUSTOM_SRC) -> Path:
    """Make `data` the poster of the project `rid`, replacing whatever was cached. The choice is final: neither the page nor a
    SteamGridDB key will swap it for another poster later."""
    check_cover_bytes(data)
    folder = art_dir()
    for f in folder.glob(f"{rid}.*"):
        f.unlink(missing_ok=True)
    dest = folder / f"{rid}{_ext_of(data)}"
    try:
        dest.write_bytes(data)
        (folder / f"{rid}.src").write_text(source, encoding="utf-8")
    except OSError as exc:
        raise RecompError(f"Couldn't save the cover: {exc}") from exc
    return dest


def clear_cover(rid: str) -> None:
    """Take the poster of the project `rid` away, and leave a note that it is to stay away (no new search, no placeholder
    fetch), until a cover is chosen again."""
    folder = art_dir()
    for f in folder.glob(f"{rid}.*"):
        f.unlink(missing_ok=True)
    try:
        (folder / f"{rid}.src").write_text(REMOVED_SRC, encoding="utf-8")
    except OSError as exc:
        raise RecompError(f"Couldn't remove the cover: {exc}") from exc


if __name__ == "__main__":  # python -m distill.recomps hash <file> ...
    if len(sys.argv) > 2 and sys.argv[1] == "hash":
        for arg in sys.argv[2:]:
            got = rom_hashes(Path(arg))
            print(f'{arg}\n  "sha1": ["{got[0]}"], "crc32": ["{got[1]}"]' if got else f"{arg}: unreadable")
    else:
        print(__doc__)
