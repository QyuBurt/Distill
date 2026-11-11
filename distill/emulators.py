"""Emulators: a catalog of emulators, found if you already have them, installed if you don't, and kept up to date.

Nothing here imports Qt. It sits on the PC Recomps engine (`distill.recomps`): the same Store, release picking, download +
checksum, unpack and launch code, with four differences.

    catalog (assets/emulators.json, plus an optional distill_emulators_catalog.json beside the app)
      -> Emulator                     one emulator: systems it plays, where its builds come from, how to find its exe
    fetch_releases()                  GitHub, GitLab, Forgejo, or a small adapter for sites that publish no release feed
    scan_folder() / link()            find installs the person already has (their Emulators folder, the usual places) and
                                      remember them as "linked": Distill never deletes those, and updates merge over them
    EmulatorInstaller                 install or update into the Emulators folder (or in place for a linked one)
    Relation                          the detain/emurelation mapping (names, alternate names, platforms per emulator),
                                      fetched and cached when reachable, used to recognise more installs and systems

An update never costs the person their data: new files replace old ones of the same name and everything else in the folder
(saves, configs, a portable.txt, shader caches) is left where it is.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from . import recomps as rc
from .constants import APP_DIR, ASSETS_DIR, ILLEGAL_NAME_CHARS
from .systems import system_of_name

SIDECAR = ".distill.json"  # written beside an install so its version survives a lost state file
RELATION_BASE = "https://raw.githubusercontent.com/detain/emurelation/master/"
RELATION_FILES = ("emulators/local.json", "platforms/local.json")
RELATION_TTL = 7 * 86400
SCAN_SKIP = {"dev_hdd0", "dev_hdd1", "games", "game", "roms", "rom", "saves", "savestates", "shaders", "cache", "bios", "firmware",
             "screenshots", "textures", "mods", "node_modules", ".git", "$recycle.bin", "system volume information"}
SCAN_DEPTH = 2
EXE_DEPTH = 3  # how far into an emulator's folder to look for its executable: portable folders can hold a whole game library


# ---- the catalog ----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Emulator:
    id: str
    name: str
    system: str  # shown on the page: "PlayStation 2", "GameCube / Wii"
    group: str = "Multi"  # the console maker it is listed under
    repo: str = ""  # "owner/name" (GitHub) or the path on `host`; already resolved for this OS
    source: str = "github"  # github | gitlab | forgejo | custom
    host: str = ""  # gitlab / forgejo host
    adapter: str = ""  # custom: dolphin_update | libretro_buildbot | blastem_site | bigpemu_site | bluestacks_winget | scummvm_site
    endpoint: str = ""
    version_from: str = "tag"  # tag | published_at | asset: what tells one build from the next
    asset: dict = field(default_factory=dict)  # per OS ("windows") or OS and CPU ("windows-arm64"), a regex, or a list of them best first, for the right release file when the guess is wrong
    exe: str = ""  # regex for the executable when the guess is wrong
    platforms: tuple[str, ...] = ("windows", "linux", "macos")
    website: str = ""
    notes: str = ""
    portable: str = ""  # what makes it keep its settings beside the program: a file name, or a folder name ending in "/"
    portable_auto: bool = True  # False: Distill does not add it for a fresh install (the program is portable already)
    trusted_owner: str = ""
    channels: tuple[str, ...] = ("stable",)
    channel_repos: dict = field(default_factory=dict)  # a channel that lives in a repository of its own: {"nightly": "Ryubing/Canary"}
    available: bool = True  # False: listed, but there is nothing to download yet
    verify: bool = False
    systems: tuple[str, ...] = ()  # library system names it plays, as the Library Report writes them
    aliases: tuple[str, ...] = ()  # other folder names an existing install may have
    also: tuple[str, ...] = ()  # platform names the emurelation mapping lists for it
    parent: str = ""  # a fork: the id of the emulator it is an alternative to
    description: str = ""  # a fork: what it is for, in the person's own words
    author_known: bool = False  # a fork whose owner also owns an emulator in the catalog
    risk_accepted: bool = False  # a fork by anyone else: the person said they checked the author
    folder: str = ""  # a fork: its own folder name inside the Emulators folder
    logo: str = ""  # an image address to use instead of the owner's GitHub picture


def _tuple(v) -> tuple:
    return tuple(v) if isinstance(v, (list, tuple)) else ((v,) if v else ())


def _emulator(d: dict, plat: str) -> Emulator:
    by_os = d.get("repo_by_platform") or {}
    repo = d.get("repo") or by_os.get(plat) or next(iter(by_os.values()), "")
    owner = str(d.get("trusted_owner") or "")
    available, notes = d.get("active", True) is not False, d.get("notes", "")
    if owner and repo and d.get("source", "github") == "github" and repo.split("/")[0].lower() != owner.lower():
        available, notes = False, f"Refused: this entry points at {repo.split('/')[0]}, not {owner}. Copies of this project carry malware."
    if not (repo or d.get("adapter")):
        available = False
    return Emulator(
        id=d["id"], name=d["name"], system=d.get("system", ""), group=d.get("group") or "Multi", repo=repo, source=d.get("source", "github"),
        host=d.get("host", ""), adapter=d.get("adapter", ""), endpoint=d.get("api_endpoint", "") or d.get("endpoint", ""),
        version_from=d.get("version_from", "tag"), asset=dict(d.get("asset") or {}), exe=d.get("exe", ""),
        platforms=_tuple(d.get("platforms")) or ("windows", "linux", "macos"), website=d.get("website", ""), notes=notes,
        portable=d.get("portable", ""), portable_auto=d.get("portable_auto", True) is not False, trusted_owner=owner, channels=_tuple(d.get("channels")) or ("stable",),
        channel_repos={str(k): str(v) for k, v in (d.get("channel_repos") or {}).items()}, available=available,
        verify=bool(d.get("verify")), systems=_tuple(d.get("systems")), aliases=tuple(a for a in _tuple(d.get("aliases")) if a),
        logo=str(d.get("logo") or ""))


def bundled_manifest() -> Path:
    return ASSETS_DIR / "emulators.json"


IMAGE_EXTS = (".png", ".ico", ".jpg", ".jpeg", ".webp")  # in the order tried, when a name has more than one
ICON_SIZE = 256  # px: every bundled icon is brought to a square of this size (see ui/emu_images.py)


def images_dir() -> Path:
    """The folder of icons that ship with the app: <assets>/emulator_images/icons, one file per emulator, named for its id
    (duckstation.png, or .ico / .jpg / .webp). An icon put there is used before anything is looked up online."""
    return ASSETS_DIR / "emulator_images" / "icons"


def bundled_image(em: Emulator) -> Path | None:
    """The shipped icon for this emulator, matched on its id (a fork on its parent's id: it is the same program's picture)
    without regard to case, or None."""
    wanted = (em.parent or em.id).lower()
    try:
        names = {p.name.lower(): p for p in images_dir().iterdir() if p.is_file()}
    except OSError:
        return None
    return next((names[wanted + ext] for ext in IMAGE_EXTS if wanted + ext in names), None)


def user_manifest() -> Path:
    return APP_DIR / "distill_emulators_catalog.json"  # the person's own additions and overrides, same format


def load_catalog(paths: Iterable[Path] | None = None, plat: str = "") -> list[Emulator]:
    """The bundled manifest, then the person's own file on top (same id replaces; a new id adds). A bad file or entry is
    skipped. A replacement can't drop the `trusted_owner` the bundled entry set."""
    plat = plat or rc.platform_key()
    out: dict[str, Emulator] = {}
    for path in paths if paths is not None else (bundled_manifest(), user_manifest()):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for d in data.get("emulators", data) if isinstance(data, dict) else data:
            try:
                if not isinstance(d, dict):
                    continue
                em = _emulator(d, plat)
            except (KeyError, TypeError, AttributeError):
                continue
            if (old := out.get(em.id)) and old.trusted_owner and em.trusted_owner != old.trusted_owner:
                em = _emulator({**d, "trusted_owner": old.trusted_owner}, plat)
            out[em.id] = em

    size: dict[str, int] = {}
    for e in out.values():
        size[e.group] = size.get(e.group, 0) + 1

    def order(e: Emulator) -> tuple:  # the biggest group first (a tie A to Z), and the emulators A to Z inside each
        return (-size[e.group], e.group.casefold(), e.name.casefold())
    return sorted(out.values(), key=order)


def for_this_os(em: Emulator, plat: str = "") -> bool:
    return (plat or rc.platform_key()) in em.platforms


def downloadable(em: Emulator, plat: str = "") -> bool:
    return em.available and for_this_os(em, plat)


# ---- the emurelation mapping ----------------------------------------------------------------------------------
def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _spaced(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _label(x) -> str:
    if isinstance(x, dict):
        return str(x.get("name") or x.get("id") or "")
    return str(x or "")


def _items(data) -> list[dict]:
    """The entries of an emurelation file: an {id: entry} object, a list, or either under an "emulators" / "platforms" key."""
    if isinstance(data, dict):
        for key in ("emulators", "platforms"):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break
    if isinstance(data, dict):
        return [v if isinstance(v, dict) else {"name": str(k)} for k, v in data.items()]
    if isinstance(data, list):
        return [x if isinstance(x, dict) else {"name": str(x)} for x in data]
    return []


def _names_of(entry: dict) -> list[str]:
    names = [_label(entry)]
    for key in ("AltNames", "altNames", "alt_names", "altnames", "aliases", "Aliases"):
        names += [_label(a) for a in _tuple(entry.get(key))]
    return [n for n in dict.fromkeys(names) if n]


_COMPANIES = ("Nintendo", "Sony", "Microsoft", "Sega", "NEC", "SNK", "Bandai", "Panasonic", "Philips", "Mattel", "Commodore", "Sinclair", "Amstrad")


def display_platforms(names: Iterable[str]) -> list[str]:
    """Platform names as the emulator's page lists them: one entry per system, named the way the rest of Distill names it and without
    the maker in front ('Xbox', not 'Microsoft Xbox'). emurelation spells one system several ways ('Gamecube', 'Nintendo Gamecube'),
    and they all come out as one."""
    out: dict[str, str] = {}
    for raw in names:
        name = " ".join(str(raw).split())
        if not name:
            continue
        m = re.match(r"^(.*?)\s*\(([^()]*)\)$", name)  # 'Nintendo Wii U (Wii U)': the bracket only repeats the name
        if m and m[2].casefold() in m[1].casefold():
            name = m[1]
        label = system_of_name(name)
        if not label:
            for company in _COMPANIES:
                if name.casefold().startswith(company.casefold() + " "):
                    rest = name[len(company) + 1:].strip()
                    if len(rest) > 2 and not rest[0].isdigit():
                        label = system_of_name(rest) or rest
                    break
        label = label or name
        out.setdefault(re.sub(r"[^a-z0-9]+", "", label.casefold()), label)
    return sorted(out.values(), key=str.casefold)


@dataclass(slots=True)
class Relation:
    """Which names mean the same emulator, and which platforms each one plays, across the sources emurelation maps."""
    link: dict[str, str] = field(default_factory=dict)  # any normalised name -> its canonical normalised name
    names: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # canonical -> every spelling seen
    platforms: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # canonical -> platform names

    def _key(self, em: Emulator) -> str:
        for cand in (em.id, em.name, *em.aliases):
            if (hit := self.link.get(norm(cand))):
                return hit
        return ""

    def aliases_for(self, em: Emulator) -> tuple[str, ...]:
        return tuple(sorted(n for n in self.names.get(self._key(em), ()) if len(norm(n)) >= 3))

    def platforms_for(self, em: Emulator) -> tuple[str, ...]:
        return tuple(sorted(self.platforms.get(self._key(em), ()), key=str.casefold))

    def systems_for(self, em: Emulator) -> tuple[str, ...]:
        """Library system names for the platforms emurelation lists (only the ones Distill itself knows how to name)."""
        return tuple(dict.fromkeys(s for p in self.platforms_for(em) if (s := system_of_name(p))))


def parse_relation(emulators_data, platforms_data) -> Relation:
    rel = Relation()
    for e in _items(emulators_data):
        names = _names_of(e)
        if not names:
            continue
        canon = norm(names[0])
        for n in names:
            rel.link.setdefault(norm(n), canon)
            rel.names[canon].add(n)
        for p in _tuple(e.get("platforms")):
            if label := _label(p):
                rel.platforms[canon].add(label)
    for p in _items(platforms_data):
        pname = _label(p)
        for e in _tuple(p.get("emulators")):
            en = _label(e)
            if not en or not pname:
                continue
            canon = rel.link.setdefault(norm(en), norm(en))
            rel.names[canon].add(en)
            rel.platforms[canon].add(pname)
    return rel


def _relation_dir() -> Path:
    d = rc.root() / "emurelation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_relation(force: bool = False, fetch: Callable[[str], bytes] | None = None) -> Relation | None:
    """The emurelation mapping, cached for a week. None when it was never reachable or can't be read: it only adds to what the
    manifest already says, so nothing depends on it."""
    fetch = fetch or (lambda url: rc._get(url, timeout=60))
    docs = []
    for name in RELATION_FILES:
        path = _relation_dir() / name.replace("/", "_")
        fresh = path.exists() and time.time() - path.stat().st_mtime < RELATION_TTL
        if force or not fresh:
            try:
                data = fetch(RELATION_BASE + name)
                json.loads(data)  # only a file that parses replaces the cached one
                path.write_bytes(data)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            docs.append(None)
    return parse_relation(*docs) if any(d is not None for d in docs) else None


def enrich(catalog: Iterable[Emulator], relation: Relation | None) -> list[Emulator]:
    """The same entries with the extra names and systems emurelation knows (never fewer than the manifest gave)."""
    if relation is None:
        return list(catalog)
    out = []
    for em in catalog:
        if em.parent:
            out.append(em)
            continue
        alts = tuple(dict.fromkeys((*em.aliases, *relation.aliases_for(em))))
        systems = tuple(dict.fromkeys((*em.systems, *relation.systems_for(em))))
        out.append(replace(em, aliases=alts, systems=systems, also=relation.platforms_for(em)))
    return out


# ---- release sources -------------------------------------------------------------------------------------------
def _release_dict(tag: str, name: str, body: str, published: str, prerelease: bool, url: str, assets: list[dict]) -> dict:
    return {"tag_name": tag, "name": name, "body": body, "published_at": published, "prerelease": prerelease, "html_url": url,
            "assets": [{"name": a["name"], "size": a.get("size", 0), "browser_download_url": a["url"], "digest": a.get("digest", "")} for a in assets]}


def _gitlab(em: Emulator) -> list[dict]:
    url = f"https://{em.host}/api/v4/projects/{urllib.parse.quote(em.repo, safe='')}/releases?per_page=15"
    out = []
    for d in json.loads(rc._get(url)):
        links = (d.get("assets") or {}).get("links") or []
        assets = [{"name": a.get("name", ""), "url": a.get("direct_asset_url") or a.get("url", "")} for a in links]
        out.append(_release_dict(d.get("tag_name", ""), d.get("name") or d.get("tag_name", ""), d.get("description") or "",
                                 d.get("released_at") or "", False, (d.get("_links") or {}).get("self", ""), assets))
    return out


def _forgejo(em: Emulator) -> list[dict]:
    url = f"https://{em.host}/api/v1/repos/{em.repo}/releases?limit=15"
    return [d for d in json.loads(rc._get(url)) if isinstance(d, dict) and not d.get("draft")]


def _dolphin_name(ver: str, a: dict, url: str) -> str:
    """A file name that says which OS and CPU it is for, so the usual asset picking works. Dolphin's own file names often say neither
    ("dolphin-2603a-x64.7z"), and which key carries the label ("system", "name"...) isn't something to rely on: all of them are read, and
    the file type settles what is left (.7z is the Windows build, .dmg the Mac one). Android and anything unrecognised returns ''."""
    file = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    low = file.lower()
    text = " ".join(str(a.get(k) or "") for k in ("system", "name", "platform", "os")).lower() + " " + low
    ext = "".join(Path(low).suffixes[-2:]) if low.endswith((".tar.gz", ".tar.xz")) else Path(low).suffix
    if "android" in text or low.endswith(".apk"):
        return ""
    if "mac" in text or low.endswith(".dmg"):
        return f"dolphin-{ver}-macos-universal{ext}"
    if "linux" in text:
        return f"dolphin-{ver}-linux-{'arm64' if re.search('arm|aarch', text) else 'x64'}{ext}"
    if "win" in text or low.endswith((".7z", ".zip", ".exe")):
        return f"dolphin-{ver}-windows-{'arm64' if re.search('arm|aarch', text) else 'x64'}{ext}"
    return file


def _dolphin(em: Emulator) -> list[dict]:
    """Dolphin publishes no release feed; its updater endpoint names the newest build. Read it loosely: the file names are taken
    from the answer when it lists them, else built from the version the way dolphin-emu.org names its downloads."""
    d = json.loads(rc._get(em.endpoint or "https://dolphin-emu.org/update/latest/beta"))
    ver = str(d.get("shortrev") or d.get("version") or str(d.get("hash") or "")[:10] or "").strip()
    if not ver:
        raise rc.RecompError("Dolphin's update service didn't name a version.")
    assets = []
    for a in d.get("artifacts") or d.get("assets") or []:
        if isinstance(a, dict) and (url := a.get("url") or a.get("browser_download_url")):
            if name := _dolphin_name(ver, a, str(url)):
                assets.append({"name": name, "url": url, "size": a.get("size", 0)})
    if not assets:
        base = f"https://dl.dolphin-emu.org/releases/{ver}/"
        assets = [{"name": f"dolphin-{ver}-windows-x64.7z", "url": f"{base}dolphin-{ver}-x64.7z"},
                  {"name": f"dolphin-{ver}-macos-universal.dmg", "url": f"{base}dolphin-{ver}-universal.dmg"}]
    log = d.get("changelog") or d.get("changes") or []
    body = "\n".join(f"- {_label(c) or c.get('msg', '') if isinstance(c, dict) else c}" for c in log[:30])
    return [_release_dict(ver, f"Dolphin {ver}", body, str(d.get("date") or "")[:10], False,
                          str(d.get("html_url") or d.get("download-page") or "https://dolphin-emu.org/download/"), assets)]


def _libretro_versions(base: str) -> list[str]:
    """RetroArch's stable versions, oldest first: from the buildbot's own listing, else from the project's tags on GitHub."""
    found: list[str] = []
    try:
        page = rc._get(base, timeout=30).decode("utf-8", "replace")
        found = re.findall(r'href="(?:\./)?(\d+\.\d+\.\d+)/?"', page)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    if not found:
        try:
            tags = json.loads(rc._get("https://api.github.com/repos/libretro/RetroArch/tags?per_page=60", accept="application/vnd.github+json"))
            found = [m.group(1) for t in tags if isinstance(t, dict) and (m := re.fullmatch(r"v?(\d+\.\d+\.\d+)", str(t.get("name", ""))))]
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return sorted(set(found), key=lambda v: tuple(int(x) for x in v.split(".")))


def _libretro(em: Emulator) -> list[dict]:
    base = em.endpoint or "https://buildbot.libretro.com/stable/"
    versions = _libretro_versions(base)
    if not versions:
        raise rc.RecompError("Couldn't read RetroArch's version list from buildbot.libretro.com or GitHub.")
    v = versions[-1]
    assets = [{"name": "RetroArch-windows-x86_64.7z", "url": f"{base}{v}/windows/x86_64/RetroArch.7z"},
              {"name": "RetroArch-linux-x86_64.7z", "url": f"{base}{v}/linux/x86_64/RetroArch.7z"},
              {"name": "RetroArch-macos-universal.dmg", "url": f"{base}{v}/apple/osx/universal/RetroArch_Metal.dmg"}]
    return [_release_dict(v, f"RetroArch {v}", "", "", False, "https://www.retroarch.com/?page=platforms", assets)]


def _site_release(em: Emulator, page: str, pattern: str, name_of, label: str, url: str) -> list[dict]:
    """A release read off a project's own download page, for projects that publish no feed: every link on the page matching `pattern`
    becomes a file, named by `name_of` so the usual asset picking can tell the OS and CPU."""
    html = rc._get(page, timeout=30).decode("utf-8", "replace")
    assets, seen = [], set()
    for m in re.finditer(pattern, html):
        link = urllib.parse.urljoin(page, m.group(1))
        if link in seen or not (name := name_of(m)):
            continue
        seen.add(link)
        assets.append({"name": name, "url": link})
    if not assets:
        raise rc.RecompError(f"Couldn't find any downloads on {urllib.parse.urlparse(page).hostname}.")
    ver = next((v for a in assets if (v := _version_in(a["name"]))), "")
    if not ver:
        raise rc.RecompError(f"Couldn't read a version from {urllib.parse.urlparse(page).hostname}.")
    return [_release_dict(ver, f"{label} {ver}", "", "", False, url, assets)]


def _version_in(name: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)+)", name)
    return m.group(1) if m else ""


def _blastem(em: Emulator) -> list[dict]:
    """BlastEm: Mercurial source and no release feed; the stable files are listed on its downloads page."""
    page = em.endpoint or "https://www.retrodev.com/blastem/downloads.html"
    labels = {"blastem32": "linux-x86", "blastem64": "linux-x64", "blastem-osx": "macos-x64", "blastem-win32": "windows-x86", "blastem-win64": "windows-x64"}

    def name_of(m):
        kind = labels.get(m.group(2))
        return f"blastem-{kind}-{m.group(3)}{m.group(4)}" if kind else ""

    return _site_release(em, page, r'href="([^"]*?/?(blastem(?:32|64|-osx|-win32|-win64))-(\d+(?:\.\d+)+)(\.zip|\.tar\.gz))"', name_of,
                         "BlastEm", "https://www.retrodev.com/blastem/downloads.html")


def _bigpemu(em: Emulator) -> list[dict]:
    """BigPEmu: closed source, published as zips on the author's own page."""
    page = em.endpoint or "https://www.richwhitehouse.com/jaguar/index.php?content=download"
    kinds = {"": "windows-x64", "WinARM64_": "windows-arm64", "Linux64_": "linux-x64", "LinuxARM64_": "linux-arm64"}

    def name_of(m):
        kind = kinds.get(m.group(2))
        v = m.group(3)
        return f"BigPEmu-{kind}-{v[0]}.{v[1:]}{m.group(4)}" if kind and len(v) > 1 else ""

    # file names carry the version without its dot: BigPEmu_v1221.zip is 1.221
    return _site_release(em, page, r'(https://www\.richwhitehouse\.com/jaguar/builds/BigPEmu_(|WinARM64_|Linux64_|LinuxARM64_)v(\d+)(\.zip|\.tar\.gz))',
                          lambda m: name_of(m), "BigPEmu", "https://www.richwhitehouse.com/jaguar/")


def _scummvm(em: Emulator) -> list[dict]:
    """ScummVM: its GitHub releases carry source only. The binaries are on its downloads page, hosted at downloads.scummvm.org
    (Windows zip, macOS disk image; Linux is Flatpak/Snap/distro packages only, so there is nothing to unpack there)."""
    page = em.endpoint or "https://www.scummvm.org/downloads/"
    kinds = {"win32-x86_64.zip": "windows-x64.zip", "macosx.dmg": "macos-universal.dmg"}

    def name_of(m):
        kind = kinds.get(m.group(3))
        return f"scummvm-{m.group(2)}-{kind}" if kind else ""

    return _site_release(em, page, r'href="((?:https://downloads\.scummvm\.org)?/frs/scummvm/([^/"]+)/scummvm-[\d.]+[a-z]?-(win32-x86_64\.zip|macosx\.dmg))"',
                         lambda m: name_of(m), "ScummVM", "https://www.scummvm.org/downloads/")


WINGET_BLUESTACKS = "manifests/b/BlueStack/BlueStacks"


def _bluestacks(em: Emulator) -> list[dict]:
    """BlueStacks: Windows only, only as an installer, and no release feed. Microsoft's winget community manifests keep each
    version's installer address and SHA-256 (one folder per version), so the newest folder that names an installer gives the file,
    and the hash is checked when it is downloaded. The address must still be on BlueStacks' own servers (see `download_hosts`)."""
    listing = json.loads(rc._get(em.endpoint or f"https://api.github.com/repos/microsoft/winget-pkgs/contents/{WINGET_BLUESTACKS}",
                                 accept="application/vnd.github+json"))
    versions = []
    for entry in listing if isinstance(listing, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if entry.get("type") == "dir" and re.fullmatch(r"\d+(?:\.\d+)+", name):
            versions.append((tuple(int(x) for x in name.split(".")), name))
    for _key, ver in sorted(versions, reverse=True)[:5]:  # a folder with no installer file yet is passed over for the one before it
        url = f"https://raw.githubusercontent.com/microsoft/winget-pkgs/HEAD/{WINGET_BLUESTACKS}/{ver}/BlueStack.BlueStacks.installer.yaml"
        try:
            text = rc._get(url, timeout=30).decode("utf-8", "replace")
        except urllib.error.HTTPError:
            continue
        assets = []
        for block in re.split(r"^\s*-\s+Architecture:\s*", text, flags=re.M)[1:]:
            arch = (block.split() or [""])[0].strip("'\"").lower()
            link = re.search(r"InstallerUrl:\s*['\"]?([^\s'\"]+)", block)
            sha = re.search(r"InstallerSha256:\s*['\"]?([0-9A-Fa-f]{64})", block)
            if arch in ("x64", "x86", "arm64") and link:
                assets.append({"name": f"BlueStacksInstaller_{ver}_windows-{arch}.exe", "url": link.group(1),
                               "digest": f"sha256:{sha.group(1).lower()}" if sha else ""})
        if assets:
            date = re.search(r"^ReleaseDate:\s*['\"]?(\d{4}-\d{2}-\d{2})", text, re.M)
            return [_release_dict(ver, f"BlueStacks {ver}", "", date.group(1) if date else "", False, "https://www.bluestacks.com/download.html", assets)]
    raise rc.RecompError("Couldn't find a BlueStacks installer in the winget manifests.")


ADAPTERS = {"dolphin_update": _dolphin, "libretro_buildbot": _libretro, "blastem_site": _blastem, "bigpemu_site": _bigpemu,
            "bluestacks_winget": _bluestacks, "scummvm_site": _scummvm}


def _site(em: Emulator) -> str:
    return em.host or urllib.parse.urlparse(em.endpoint).hostname or next(iter(sorted(download_hosts(em))), "") or em.name


def _why(exc: BaseException) -> str:
    """' (HTTP 403)' / ' (timed out)' / ' (<reason>)': what went wrong, for the message the person reads."""
    if isinstance(exc, urllib.error.HTTPError):
        return f" (HTTP {exc.code})"
    reason = getattr(exc, "reason", exc)
    text = "timed out" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else str(reason)
    return f" ({text[:80]})" if text else ""


def fetch_releases(em: Emulator, store: rc.Store | None = None, force: bool = False, channel: str = "stable") -> list[rc.Release]:
    """Newest first. Cached for an hour per emulator; when the site can't be reached the last cached listing is used.
    A channel with a repository of its own (`channel_repos`) is read from that repository, and cached apart from the stable one."""
    other = em.channel_repos.get(channel, "") if channel != "stable" else ""
    if other:
        em = replace(em, repo=other)
    if em.source == "github":
        return rc.releases(em.repo, store, force)
    key = f"emu:{em.id}:{channel}" if other else f"emu:{em.id}"
    if store and not force and (hit := store.cache_get(key)) is not None:
        return [rc._release_of(d) for d in hit]
    try:
        if em.source == "gitlab":
            raw = _gitlab(em)
        elif em.source == "forgejo":
            raw = _forgejo(em)
        elif em.source == "custom" and em.adapter in ADAPTERS:
            raw = ADAPTERS[em.adapter](em)
        else:
            raise rc.RecompError(f"{em.name} has no known place to look for releases.")
    except rc.RecompError:
        raise
    except (urllib.error.URLError, OSError, ValueError, AttributeError, TypeError) as exc:
        if store and (stale := store.cache_stale(key)) is not None:
            return [rc._release_of(d) for d in stale]
        raise rc.RecompError(f"Couldn't reach {_site(em)}{_why(exc)}.") from exc
    if store:
        store.cache_put(key, raw)
    return [rc._release_of(d) for d in raw]


def choose(em: Emulator, rels: list[rc.Release], channel: str = "stable", plat: str = "", machine: str = "") -> tuple[rc.Release, dict] | None:
    """Stable: the newest non-pre-release that has a file for this OS (else the newest pre-release that does).
    Nightly: simply the newest release that has one (an emulator whose nightly has its own repository is given that repository's releases)."""
    if channel == "nightly":
        for rel in rels:
            if asset := rc.pick_asset(rel.assets, em, plat, machine):
                return rel, asset
        return None
    return rc.choose_release(rels, em, plat, machine)


def latest_for(em: Emulator, store: rc.Store, force: bool = False) -> tuple[rc.Release, dict] | str:
    """(release, file) to install, or a sentence saying why there isn't one."""
    if not em.available:
        return em.notes or f"{em.name} has no download yet."
    if not for_this_os(em):
        return f"{em.name} has no build for {rc.platform_key()}."
    try:
        chan = channel_of(store, em)
        found = choose(em, fetch_releases(em, store, force, chan), chan)
    except rc.RecompError as exc:
        return str(exc)
    return found if found else f"No download for {rc.platform_key()} in the recent releases. Use the website link."


def stamp_of(em: Emulator, rel: rc.Release, asset: dict) -> str:
    """What tells this build from the one before it, for emulators whose tag never changes."""
    if em.version_from == "asset":
        return asset["name"]
    if em.version_from == "published_at":
        return rc.sha256_of(asset) or f"{rel.published}:{asset.get('size', 0)}"
    return rel.tag


def label_of(em: Emulator, rel: rc.Release, asset: dict) -> str:
    """The version as the person reads it."""
    if em.version_from == "asset":
        return re.sub(r"(\.tar\.\w+|\.\w+)$", "", asset["name"])
    if em.version_from == "published_at":
        return f"{rel.tag} ({rel.published})" if rel.published else rel.tag
    return rel.tag


def update_state(em: Emulator, inst: rc.Installed | None, latest) -> str:
    """'missing' | 'current' | 'update' | 'unknown' (installed, but its version isn't known or the latest couldn't be read)."""
    if inst is None:
        return "missing"
    if not isinstance(latest, tuple):
        return "unknown"
    rel, asset = latest
    if em.version_from == "tag":
        if not inst.version:
            return "unknown"
        return "update" if rc.is_newer(inst.version, rel.tag) else "current"
    if not inst.stamp:
        return "unknown"
    return "update" if inst.stamp != stamp_of(em, rel, asset) else "current"


def can_update(inst: rc.Installed | None) -> bool:
    """A linked install that shares its folder with other programs is left alone: merging a download into it could scatter files."""
    return inst is not None and inst.linked != "file"


def update_blocked(em: Emulator, inst: rc.Installed | None) -> str:
    """Why this install can't be updated where it is ('' when it can): its folder is one the system keeps closed to normal
    programs, such as Program Files. Distill doesn't update those; the person moves the program somewhere writable and links it."""
    if inst is not None and Path(inst.folder).is_dir() and not rc.writable(Path(inst.folder)):
        return rc.not_writable_message(em.name, inst.folder)
    return ""


# ---- settings kept in the Store ---------------------------------------------------------------------------------
def store_path() -> Path:
    return rc.root() / "emulators_state.json"


def open_store() -> rc.Store:
    return rc.Store(store_path())


def emulators_root(store: rc.Store) -> Path | None:
    v = store.get("emu_root")
    return Path(str(v)) if v else None


def set_emulators_root(store: rc.Store, path: Path | str | None) -> None:
    store.put("emu_root", str(path) if path else "")


def channel_of(store: rc.Store, em: Emulator) -> str:
    chosen = (store.get("emu_channels") or {}).get(em.id)
    return chosen if chosen in em.channels else "stable"


def set_channel(store: rc.Store, em: Emulator, channel: str) -> None:
    table = dict(store.get("emu_channels") or {})
    table[em.id] = channel
    store.put("emu_channels", table)


def download_hosts(em: Emulator) -> frozenset[str]:
    """The only sites a download for this emulator may come from, when it isn't on GitHub (those are pinned by owner instead)."""
    if em.source in ("gitlab", "forgejo") and em.host:
        return frozenset({em.host.lower()})
    return frozenset({"dolphin_update": ("dl.dolphin-emu.org",), "libretro_buildbot": ("buildbot.libretro.com",), "blastem_site": ("www.retrodev.com",),
                       "bigpemu_site": ("www.richwhitehouse.com",), "scummvm_site": ("downloads.scummvm.org",),
                       "bluestacks_winget": ("ak-build.bluestacks.com", "cdn3.bluestacks.com")}.get(em.adapter, ()))


def folder_name(em: Emulator) -> str:
    return ILLEGAL_NAME_CHARS.sub("", em.folder or em.name).strip(" .") or em.id


# ---- finding installs that already exist --------------------------------------------------------------------------
@dataclass(slots=True)
class Found:
    id: str
    folder: str
    exe: str  # relative to folder
    linked: str  # "folder" | "file"
    version: str = ""
    stamp: str = ""
    released: tuple[str, ...] = ()  # link(): emulators that had been pointed at this same program and no longer are


def default_roots(plat: str = "") -> list[Path]:
    """The usual places people keep emulators, only the ones that exist."""
    plat, home = plat or rc.platform_key(), Path.home()
    cands: list[Path] = []
    if plat == "windows":
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            if v := os.environ.get(var):
                cands.append(Path(v))
        if v := os.environ.get("LOCALAPPDATA"):
            cands.append(Path(v) / "Programs")
        cands += [home / "Emulators", home / "Games" / "Emulators", Path("C:/Emulators")]
    elif plat == "macos":
        cands += [Path("/Applications"), home / "Applications", home / "Emulators"]
    else:
        cands += [home / "Applications", home / "Emulators", home / "Games" / "Emulators", home / ".local" / "bin", Path("/opt")]
    return [p for p in dict.fromkeys(cands) if p.is_dir()]


class _Matcher:
    """Does a folder or file name look like one of this emulator's names?"""

    def __init__(self, em: Emulator):
        names = {em.id, em.name, *em.aliases} if em.parent else {em.id, em.name, re.sub(r"\s*\(.*?\)", "", em.name), *em.aliases}
        if em.folder:
            names.add(em.folder)
        self.forms = [(_spaced(n), norm(n)) for n in names if len(norm(n)) >= 3]

    def __call__(self, name: str) -> int:
        """0 = no, else how many characters of the name matched (a longer match beats a shorter one)."""
        spaced, compact, best = _spaced(name), norm(name), 0
        for sp, co in self.forms:
            if spaced == sp or spaced.startswith(sp + " ") or compact == co or (compact.startswith(co) and compact[len(co)].isdigit()):
                best = max(best, len(co))
        return best


def _version_in_name(name: str) -> str:
    m = re.search(r"(?<![\w.])v?(\d+(?:\.\d+){1,3})(?![\w])", name)
    return m.group(1) if m else ""


def _trim(version: str) -> str:
    parts = version.split(".")
    while len(parts) > 3 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def _win_version(path: Path) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        dll = ctypes.windll.version
        size = dll.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not dll.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or not length.value:
            return ""

        class FixedInfo(ctypes.Structure):
            _fields_ = [(n, wintypes.DWORD) for n in ("sig", "struc", "ms", "ls", "pms", "pls", "mask", "flags", "os", "type", "sub", "dms", "dls")]
        info = ctypes.cast(ptr, ctypes.POINTER(FixedInfo)).contents
        v = f"{info.ms >> 16}.{info.ms & 0xFFFF}.{info.ls >> 16}.{info.ls & 0xFFFF}"
        return "" if v.startswith("0.0.0") else _trim(v)
    except (OSError, AttributeError, ValueError):
        return ""


def read_sidecar(folder: Path, rid: str) -> tuple[str, str] | None:
    try:
        d = json.loads((folder / SIDECAR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (str(d.get("version", "")), str(d.get("stamp", ""))) if isinstance(d, dict) and d.get("id") == rid else None


def write_sidecar(folder: Path, rid: str, version: str, stamp: str) -> None:
    try:
        (folder / SIDECAR).write_text(json.dumps({"id": rid, "version": version, "stamp": stamp}), encoding="utf-8")
    except OSError:
        pass


def detect_version(em: Emulator, folder: Path, exe: Path) -> tuple[str, str]:
    """(version, stamp) of an install Distill didn't make: its own note beside it, else the file's version resource, else a
    version in the folder's name, else nothing. A guess from a name is only ever used to say 'installed vX', never to hide an update."""
    if known := read_sidecar(folder, em.id):
        return known
    if v := _win_version(exe):
        return v, ""
    return _version_in_name(folder.name) or _version_in_name(exe.name), ""


def locate_exe(folder: Path, em: Emulator, plat: str = "", depth: int = EXE_DEPTH) -> Path | None:
    """The file to run inside an emulator's folder. Looks a few levels down only, and not into folders that hold games or saves:
    a portable RPCS3 or Dolphin folder can contain a whole library."""
    plat = plat or rc.platform_key()
    rx = re.compile(em.exe, re.IGNORECASE) if em.exe else None
    cands: list[Path] = []
    base = len(folder.parts)
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        level = len(here.parts) - base
        if plat == "macos":
            apps = [d for d in dirnames if d.lower().endswith(".app")]
            cands += [here / d for d in apps]
            dirnames[:] = [d for d in dirnames if not d.lower().endswith(".app")]
        dirnames[:] = [d for d in dirnames if d.lower() not in SCAN_SKIP] if level < depth else []
        for fn in filenames:
            p = here / fn
            rel = p.relative_to(folder).as_posix()
            if rx:
                if rx.search(rel):
                    cands.append(p)
                continue
            low = fn.lower()
            if rc._NOT_THE_GAME.search(fn):
                continue
            if plat == "windows" and low.endswith(".exe") or plat == "linux" and (low.endswith(".appimage") or "." not in low and rc._looks_executable(p)):
                cands.append(p)
    if rx and plat == "macos":
        cands = [p for p in cands if rx.search(p.relative_to(folder).as_posix())]
    if not cands:
        return None
    words = {w for w in rc._TOKEN_SPLIT.split(f"{em.name} {em.id}".lower()) if len(w) > 2}

    def score(p: Path) -> tuple:
        stem = set(rc._TOKEN_SPLIT.split(p.stem.lower()))
        try:
            st = p.stat()
            when, size = st.st_mtime, st.st_size if p.is_file() else 0
        except OSError:
            when, size = 0.0, 0
        return (len(stem & words), -len(p.relative_to(folder).parts), when, size)  # newest wins, so an update beats the old version beside it
    return max(cands, key=score)


def _is_exe_file(p: Path, plat: str) -> bool:
    low = p.name.lower()
    if plat == "windows":
        return low.endswith(".exe")
    if plat == "macos":
        return low.endswith(".app")
    return low.endswith(".appimage")


def scan_folder(root: Path, catalog: Iterable[Emulator], plat: str = "", depth: int = SCAN_DEPTH,
                stop: Callable[[], bool] = lambda: False) -> dict[str, Found]:
    """Emulators already in `root`: a folder named like one (PCSX2, duckstation-v0.1, Dolphin Emulator) with its executable
    inside, or a loose AppImage / exe named like one. The first match for each emulator wins."""
    plat = plat or rc.platform_key()
    matchers = {em.id: (em, _Matcher(em)) for em in catalog if for_this_os(em, plat)}
    found: dict[str, Found] = {}

    def best(name: str) -> Emulator | None:
        scored = [(m(name), em) for em, m in matchers.values()]
        score, em = max(scored, key=lambda t: t[0], default=(0, None))
        return em if score else None

    def walk(folder: Path, left: int) -> None:
        try:
            kids = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            return
        for kid in kids:
            if stop():
                return
            if kid.name.startswith(".") or kid.name.lower() in SCAN_SKIP:
                continue
            try:
                is_dir = kid.is_dir()
            except OSError:
                continue
            if is_dir and kid.suffix.lower() != ".app":
                em = best(kid.name)
                if em and em.id not in found and (exe := locate_exe(kid, em, plat)):
                    ver, stamp = detect_version(em, kid, exe)
                    found[em.id] = Found(em.id, str(kid), exe.relative_to(kid).as_posix(), "folder", ver, stamp)
                elif not em and left > 1:
                    walk(kid, left - 1)
            elif _is_exe_file(kid, plat):
                em = best(kid.stem)
                if em and em.id not in found:
                    ver, stamp = detect_version(em, folder, kid)
                    found[em.id] = Found(em.id, str(folder), kid.name, "file", ver, stamp)
    walk(root, depth)
    return found


def scan_all(store: rc.Store, catalog: Iterable[Emulator], plat: str = "", stop: Callable[[], bool] = lambda: False) -> dict[str, Found]:
    """The Emulators folder first, then the usual places; the first find for an emulator wins."""
    catalog = list(catalog)
    roots = [r for r in (emulators_root(store),) if r and r.is_dir()]
    out: dict[str, Found] = {}
    for root in roots:
        out.update(scan_folder(root, catalog, plat, SCAN_DEPTH, stop))
    for root in default_roots(plat):
        if root in roots:
            continue
        for rid, f in scan_folder(root, catalog, plat, 1, stop).items():
            out.setdefault(rid, f)
    return out


def _program_key(folder: str, exe: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.join(folder, exe)))


def others_at(store: rc.Store, rid: str, folder: str, exe: str) -> list[str]:
    """The ids of other emulators the store records at this very program. One program is one emulator: a fork lives in a folder of its
    own, so when its folder also carries its parent's name (\"KytyPS5 Jetsku\") a scan can hand it to the parent, and the parent would
    then update the fork's files with its own downloads."""
    key = _program_key(folder, exe)
    out = []
    for other in list((store.data.get("installed") or {})):
        if other != rid and (inst := store.installed(other)) is not None and _program_key(inst.folder, inst.exe) == key:
            out.append(other)
    return out


def apply_scan(store: rc.Store, found: dict[str, Found]) -> list[str]:
    """Remember what a scan found. A managed install, or a link whose files are still there, is kept; a missing or new one is set.
    Returns the ids that are newly linked."""
    new = []
    for rid, f in found.items():
        cur = store.installed(rid)
        if cur and cur.exe_path().exists():
            continue
        if others_at(store, rid, f.folder, f.exe):
            continue  # another emulator already has this program (a fork linked by hand, say): a name match doesn't take it back
        store.set_installed(rc.Installed(rid, f.version, f.folder, f.exe, time.time(), f.linked, f.stamp))
        new.append(rid)
    return new


def link(store: rc.Store, em: Emulator, path: Path, plat: str = "") -> Found:
    """Point Distill at an install the person chose by hand: its executable, or the folder holding it."""
    plat = plat or rc.platform_key()
    path = Path(path)
    if path.is_dir() and path.suffix.lower() != ".app":
        exe = locate_exe(path, em, plat)
        if exe is None:
            raise rc.RecompError(f"Found nothing to run for {em.name} in that folder.")
        folder = path
    elif path.exists():
        exe, folder = path, path.parent
    else:
        raise rc.RecompError("That file or folder doesn't exist.")
    shared = {Path.home(), *default_roots(plat)}
    if root := emulators_root(store):
        shared.add(root)
    kind = "file" if folder in shared else "folder"  # an exe sitting loose in a shared folder: never merge updates into it
    rel = exe.relative_to(folder).as_posix()
    rivals = others_at(store, em.id, str(folder), rel)
    for other in rivals:
        held = store.installed(other)
        if held is not None and not held.linked:
            raise rc.RecompError(f"That is the copy Distill installed for {other}, so it can't also be {em.name}.")
    ver, stamp = detect_version(em, folder, exe)
    f = Found(em.id, str(folder), rel, kind, ver, stamp)
    store.set_installed(rc.Installed(em.id, ver, str(folder), rel, time.time(), kind, stamp))
    for other in rivals:  # the person has said what this program is: whoever else had been linked to it (the parent, after a scan) lets go
        store.set_installed(None, other)
    f.released = tuple(rivals)
    return f


def unlink(store: rc.Store, em: Emulator) -> None:
    """Forget a linked install. Nothing on disk is touched."""
    store.set_installed(None, em.id)


def uninstall(store: rc.Store, em: Emulator) -> str:
    """Remove an install Distill made (its folder, including anything the emulator kept there). A linked install is only
    forgotten, never deleted. Returns a sentence saying what happened."""
    inst = store.installed(em.id)
    if inst is None:
        return "Not installed."
    folder = Path(inst.folder)
    managed_roots = [r.resolve() for r in (emulators_root(store), rc.root()) if r]
    try:
        inside = any(m in folder.resolve().parents or m == folder.resolve() for m in managed_roots)
    except OSError:
        inside = False
    if inst.linked or not inside or folder.resolve() in managed_roots:
        store.set_installed(None, em.id)
        return "Removed from Distill. Your files were left where they are."
    import shutil
    shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(rc.root() / "emulators" / em.id, ignore_errors=True)
    store.set_installed(None, em.id)
    return "Uninstalled."


# ---- installing -------------------------------------------------------------------------------------------------
def make_portable(em: Emulator, exe: Path) -> bool:
    """Make a fresh install portable (settings, saves and caches stay beside the program) when the emulator has the option: its
    marker file or folder is created next to the executable. Nothing already there is touched. Not done inside a macOS app bundle
    or for an AppImage, where beside the program is not a place the emulator looks. True when something was created."""
    marker = em.portable.strip()
    if not marker or not em.portable_auto or em.parent:
        return False
    if exe.name.lower().endswith(".appimage") or ".app" in (part.lower()[-4:] for part in exe.parts):
        return False
    target = exe.parent / marker.rstrip("/\\")
    if not target.name or target.exists():
        return False
    try:
        if marker.endswith(("/", "\\")):
            target.mkdir()
        else:
            target.touch()
    except OSError:
        return False  # not being portable is only a lesser comfort: the install itself has worked
    return True


class EmulatorInstaller(rc.Installer):
    """An install or update. Fresh installs go to <Emulators folder>/<Name> (or Distill's own folder when none is set); an update
    goes where the emulator already is, linked or not. Files are merged over what's there, never replaced wholesale."""

    def folder_for(self, em) -> Path:  # staging: downloads and unpacking happen here, never in the person's folder
        return rc.root() / "emulators" / em.id

    def _guard(self, em) -> None:
        cur = self.store.installed(em.id)
        if cur and cur.linked == "file":
            raise rc.RecompError(f"{em.name} sits in a folder shared with other programs, so Distill won't update it. Move it into a folder of its own, then link it again.")

    def live_for(self, em) -> Path:
        self._guard(em)
        cur = self.store.installed(em.id)
        if cur:
            return Path(cur.folder)
        root = emulators_root(self.store)
        return root / folder_name(em) if root else self.folder_for(em) / "app"

    def check_asset(self, em, asset: dict) -> None:
        self._guard(em)  # before anything is downloaded
        if why := update_blocked(em, self.store.installed(em.id)):
            raise rc.RecompError(why)  # e.g. ScummVM in Program Files: say so before a download (the page checks first, this is the backstop)
        if em.parent and not (em.author_known or em.risk_accepted):
            raise rc.RecompError(f"{em.name} is by an author Distill doesn't know and you haven't confirmed. Nothing was downloaded.")
        u = urllib.parse.urlparse(asset.get("url", ""))
        if (hosts := download_hosts(em)) and (u.scheme != "https" or (u.hostname or "").lower() not in hosts):
            raise rc.RecompError(f"The download for {em.name} doesn't come from {' or '.join(sorted(hosts))}, so it was refused.")
        if not em.trusted_owner:
            return
        owner = u.path.strip("/").split("/")[0].lower() if u.path.strip("/") else ""
        if u.hostname not in ("github.com", "www.github.com") or owner != em.trusted_owner.lower():
            raise rc.RecompError(f"The download for {em.name} doesn't come from {em.trusted_owner}, so it was refused. "
                                 "Fake copies of this project are circulating with malware in them.")

    def find_exe(self, live: Path, em) -> Path | None:
        return locate_exe(live, em)

    def record_for(self, em, rel: rc.Release, asset: dict, live: Path, exe: Path) -> rc.Installed:
        prev = self.store.installed(em.id)
        version, stamp = label_of(em, rel, asset), stamp_of(em, rel, asset)
        if prev and prev.exe_path() != exe and prev.exe_path().is_file() and prev.exe_path().name.lower().endswith(".appimage") \
                and live in prev.exe_path().parents:
            try:
                prev.exe_path().unlink()  # an AppImage is named for its version: the old one would otherwise pile up beside the new
            except OSError:
                pass
        if prev is None:  # an update never changes it: switching a working install would hide the person's settings and saves
            make_portable(em, exe)
        write_sidecar(live, em.id, version, stamp)
        return rc.Installed(em.id, version, str(live), exe.relative_to(live).as_posix(), time.time(), prev.linked if prev else "", stamp)


# ---- the library: which emulators do my games need ----------------------------------------------------------------
def library_counts(entries: Iterable) -> dict[str, int]:
    """Games per system in a Library Report (unknown systems left out)."""
    out: dict[str, int] = defaultdict(int)
    for e in entries:
        system = getattr(e, "system", "")
        if system and not system.lower().startswith("unknown"):
            out[system] += 1
    return dict(out)


def games_for(em: Emulator, counts: dict[str, int]) -> int:
    return sum(counts.get(s, 0) for s in em.systems)


def missing_for_library(catalog: Iterable[Emulator], store: rc.Store, counts: dict[str, int]) -> list[tuple[Emulator, int]]:
    """Emulators for systems the library has games for, none of which is installed yet, most games first. One per system:
    when several emulators play the same system, the ones listed first in the catalog are offered."""
    have = {s for em in catalog if store.installed(em.id) for s in em.systems}
    out, covered = [], set()
    for em in catalog:
        if em.parent or not downloadable(em) or store.installed(em.id):
            continue
        need = [s for s in em.systems if counts.get(s) and s not in have and s not in covered]
        if need:
            covered.update(need)
            out.append((em, sum(counts[s] for s in need)))
    return sorted(out, key=lambda t: -t[1])


def status_text(em: Emulator, inst: rc.Installed | None, latest) -> tuple[str, str]:
    """(code, short text) for the list: code is one of unavailable | missing | gone | current | update | unknown."""
    if not downloadable(em):
        return "unavailable", "No download yet" if em.available is False else f"Not on {rc.platform_key()}"
    if inst is None:
        return "missing", "Not installed"
    if not inst.exe_path().exists():
        return "gone", "Files missing"
    state = update_state(em, inst, latest)
    tag = " (linked)" if inst.linked else ""
    if state == "update":
        return "update", "Update available"
    if state == "unknown":
        return "unknown", "Installed" + tag
    return "current", "Up to date" + tag


# ---- forks: community builds the person adds under an emulator ---------------------------------------------------
def forks_manifest() -> Path:
    return APP_DIR / "distill_forks.json"  # written by the app (Add fork / Remove fork); kept apart from the hand-edited catalog


def parse_repo(text: str) -> str:
    """'owner/name' from a GitHub address or the short form. Anything else is an error in plain words."""
    return rc.parse_repo(text)


def parse_repo_or_empty(text: str) -> str:
    try:
        return parse_repo(text)
    except rc.RecompError:
        return ""


def fork_id(parent_id: str, repo: str) -> str:
    return "fork-" + re.sub(r"[^a-z0-9._-]+", "-", f"{parent_id}-{repo}".lower())


def known_authors(catalog: Iterable[Emulator]) -> set[str]:
    """Owners Distill already trusts: whoever makes (or is pinned as the only source of) an emulator in the catalog."""
    out: set[str] = set()
    for e in catalog:
        if e.parent:
            continue
        if e.trusted_owner:
            out.add(e.trusted_owner.lower())
        if e.source == "github" and "/" in e.repo:
            out.add(e.repo.split("/")[0].lower())
    return out


def make_fork(parent: Emulator, repo: str, name: str = "", description: str = "", known: bool = False, accepted: bool = False,
              exe: str = "", asset: dict | None = None) -> Emulator:
    """An emulator entry for a fork of `parent`. What the fork plays and how its program is found come from the parent; where
    it downloads from is pinned to the fork's own owner. Its own folder name keeps it from landing on top of the parent."""
    repo = parse_repo(repo)
    owner, rname = repo.split("/")
    default = f"{parent.name} ({owner})" if norm(rname) == norm(parent.name) else f"{parent.name} - {owner}-{rname}"
    shown = (name or "").strip() or f"{parent.name} ({owner})"
    permitted = known or accepted
    return replace(
        parent, id=fork_id(parent.id, repo), name=shown, repo=repo, source="github", host="", adapter="", endpoint="", version_from="tag",
        asset=dict(asset or {}), exe=exe or parent.exe, website="", notes="" if permitted else "Confirm this author before installing.",
        trusted_owner=owner, channels=("stable", "nightly"), available=permitted, verify=False, aliases=(), also=(), parent=parent.id,
        description=(description or "").strip(), author_known=known, risk_accepted=accepted and not known,
        folder=ILLEGAL_NAME_CHARS.sub("", default).strip(" ."))


def load_forks(catalog: Iterable[Emulator], path: Path | None = None) -> list[Emulator]:
    """The forks in distill_forks.json, as entries. One whose parent is gone, that is a duplicate, or that is unreadable is skipped."""
    catalog = [e for e in catalog if not e.parent]
    by_id, known = {e.id: e for e in catalog}, known_authors(catalog)
    try:
        data = json.loads((path or forks_manifest()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: dict[str, Emulator] = {}
    for d in (data.get("forks") if isinstance(data, dict) else data) or []:
        try:
            parent = by_id.get(str(d.get("parent", "")))
            if parent is None:
                continue
            owner = parse_repo(d["repo"]).split("/")[0].lower()
            fork = make_fork(parent, d["repo"], d.get("name", ""), d.get("description", ""), owner in known, bool(d.get("risk_accepted")),
                             str(d.get("exe", "")), d.get("asset") if isinstance(d.get("asset"), dict) else None)
        except (AttributeError, KeyError, TypeError, rc.RecompError):
            continue
        out.setdefault(fork.id, fork)
    return sorted(out.values(), key=lambda f: (f.parent, f.name.casefold()))


def forks_of(forks: Iterable[Emulator], parent_id: str) -> list[Emulator]:
    return [f for f in forks if f.parent == parent_id]


def _read_fork_entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("forks") if isinstance(data, dict) else data
    return [d for d in items or [] if isinstance(d, dict)]


def _write_fork_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"forks": entries}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def add_fork(parent: Emulator, repo: str, name: str = "", description: str = "", accepted: bool = False,
             catalog: Iterable[Emulator] = (), path: Path | None = None) -> Emulator:
    """Remember a fork. An author Distill doesn't know needs `accepted` (the person ticked the risk box). Returns the entry."""
    path = path or forks_manifest()
    repo = parse_repo(repo)
    known = repo.split("/")[0].lower() in known_authors([*catalog, parent])
    if not known and not accepted:
        raise rc.RecompError("That author isn't one Distill knows. Confirm that you've checked them to add it.")
    fork = make_fork(parent, repo, name, description, known, accepted)
    entries = [d for d in _read_fork_entries(path) if fork_id(str(d.get("parent", "")), str(d.get("repo", ""))) != fork.id]
    entries.append({"parent": parent.id, "repo": repo, "name": (name or "").strip(), "description": fork.description,
                    "risk_accepted": bool(accepted and not known)})
    _write_fork_entries(path, entries)
    return fork


def remove_fork(fork: Emulator, path: Path | None = None) -> None:
    path = path or forks_manifest()
    _write_fork_entries(path, [d for d in _read_fork_entries(path) if fork_id(str(d.get("parent", "")), str(d.get("repo", ""))) != fork.id])


def auto_update_ok(em: Emulator) -> bool:
    """Whether 'Update all' may touch it. A fork by an author the person accepted by hand is only ever updated by a click."""
    return not em.parent or em.author_known


@dataclass(slots=True)
class ForkReport:
    """What Distill could see about a repository before it is added as a fork."""
    repo: str
    known: bool = False
    error: str = ""
    stars: int = 0
    archived: bool = False
    created: str = ""
    pushed: str = ""
    standalone: bool = False  # not marked as a fork on GitHub (a detached copy)
    release: str = ""
    released: str = ""
    asset: str = ""
    size: int = 0
    prerelease_only: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.asset) and not self.archived


def inspect_fork(parent: Emulator, repo: str, store: rc.Store, catalog: Iterable[Emulator] = ()) -> ForkReport:
    """Look at a repository for the Add fork window: who owns it, how old and active it is, and whether it publishes a file
    this PC can install. Never raises: a problem comes back in `error`."""
    try:
        repo = parse_repo(repo)
    except rc.RecompError as exc:
        return ForkReport(repo=str(repo), error=str(exc))
    rep = ForkReport(repo=repo, known=repo.split("/")[0].lower() in known_authors([*catalog, parent]))
    try:
        facts = json.loads(rc._get(f"https://api.github.com/repos/{repo}", store.token(), "application/vnd.github+json"))
    except urllib.error.HTTPError as exc:
        rep.error = ("That repository doesn't exist, or it is private." if exc.code == 404 else
                     "GitHub is rate-limiting this PC. Try again later, or add a GitHub key under Settings > API keys > Updates." if exc.code in (403, 429) else f"GitHub answered {exc.code}.")
        return rep
    except (urllib.error.URLError, OSError, ValueError):
        rep.error = "Couldn't reach GitHub."
        return rep
    if isinstance(facts, dict):
        rep.stars = int(facts.get("stargazers_count") or 0)
        rep.archived = bool(facts.get("archived"))
        rep.created, rep.pushed = str(facts.get("created_at") or "")[:10], str(facts.get("pushed_at") or "")[:10]
        rep.standalone = facts.get("fork") is False
    cand = make_fork(parent, repo, known=True)  # a trial entry only: nothing is saved or downloaded
    try:
        rels = fetch_releases(cand, store, force=True)
    except rc.RecompError as exc:
        rep.error = str(exc)
        return rep
    found = choose(cand, rels, "nightly")
    if found is None:
        rep.error = f"No download for {rc.platform_key()} in its releases."
        return rep
    rel, asset = found
    rep.release, rep.released, rep.asset, rep.size = rel.tag, rel.published, asset["name"], int(asset.get("size") or 0)
    rep.prerelease_only = rel.prerelease and not any(not r.prerelease and rc.pick_asset(r.assets, cand) for r in rels)
    return rep


# ---- logos ------------------------------------------------------------------------------------------------------
LOGO_MAX = 2_000_000


def logo_url(em: Emulator) -> str:
    """Where a picture for this emulator can be had: the address the catalog gives, else the picture of the GitHub account that makes it
    (usually the project's own logo). Nothing for a fork (it uses its emulator's) or a project hosted elsewhere."""
    if em.parent:
        return ""
    if em.logo.startswith("https://"):
        return em.logo
    if em.source == "github" and "/" in em.repo:
        return f"https://github.com/{em.repo.split('/')[0]}.png?size=128"
    return ""


def logo_file(em: Emulator) -> Path:
    return rc.root() / "logos" / (re.sub(r"[^a-z0-9._-]+", "-", em.id.lower()) + ".img")


def logo_source_file(em: Emulator) -> Path:
    return logo_file(em).with_suffix(".src")


def logo_source(em: Emulator) -> str:
    """Where the cached picture came from: 'explicit' (the catalog's own address), 'steamgriddb', 'steamgriddb-miss' (it has none
    for this emulator, so none is shown), or 'avatar' (the GitHub owner's picture, only when there is no SteamGridDB key)."""
    try:
        return logo_source_file(em).read_text().strip()
    except OSError:
        return ""


def _set_logo_source(em: Emulator, source: str) -> None:
    try:
        logo_source_file(em).parent.mkdir(parents=True, exist_ok=True)
        logo_source_file(em).write_text(source)
    except OSError:
        pass


SGDB_LOGO_MIN_SCORE = 0.85  # an emulator's name must be (nearly) the whole name of the SteamGridDB entry: 'Dolphin' is not a game


def logo_settled(em: Emulator, sgdb_key: str = "") -> bool:
    """True when there is nothing more to try for this emulator's picture this time."""
    if em.parent or bundled_image(em) is not None:
        return True
    if em.logo.startswith("https://"):
        return logo_file(em).is_file()
    if sgdb_key:
        source = logo_source(em)
        return source == "steamgriddb" or (source == "steamgriddb-miss" and (logo_file(em).is_file() or not logo_url(em)))
    return logo_file(em).is_file() or not logo_url(em)


def icon_path(em: Emulator) -> Path | None:
    """The picture to show for this emulator right now: the one shipped with the app, else the cached one (a fork uses its
    emulator's: pass that), else None."""
    if (bundled := bundled_image(em)) is not None:
        return bundled
    cached = logo_file(em)
    return cached if cached.is_file() else None


def fetch_logo_best(em: Emulator, sgdb_key: str = "", fetch: Callable[[str], bytes] | None = None,
                    finder: Callable[..., tuple[bytes, object] | None] | None = None) -> Path | None:
    """The emulator's picture, first found of: the one shipped in the app's emulator_images/icons folder, the catalog's own address, the
    community's icon for the emulator by name on SteamGridDB (with a key), the GitHub owner's picture, so that none is missing. A
    SteamGridDB with nothing is remembered, so it is not asked again; one that cannot be reached is not remembered as a miss."""
    if em.parent:
        return None
    if (bundled := bundled_image(em)) is not None:
        return bundled
    if em.logo.startswith("https://"):
        path = fetch_logo(em, fetch)
        if path is not None:
            _set_logo_source(em, "explicit")
        return path
    if not sgdb_key:
        path = fetch_logo(em, fetch)
        if path is not None and not logo_source(em):
            _set_logo_source(em, "avatar")
        return path
    if logo_source(em) == "steamgriddb" and logo_file(em).is_file():
        return logo_file(em)
    if finder is None:
        from . import iconfinder
        finder = iconfinder._find_sgdb
    try:
        found = finder([em.name], sgdb_key, None, 12.0, min_score=SGDB_LOGO_MIN_SCORE)
    except Exception:  # offline, busy, key refused: try again another time
        return None
    if found is None:
        _set_logo_source(em, "steamgriddb-miss")
        return fetch_logo(em, fetch)  # the owner's picture: not always the emulator's own, but better than none
    data = found[0]
    if not data or len(data) > LOGO_MAX:
        return None
    try:
        logo_file(em).parent.mkdir(parents=True, exist_ok=True)
        logo_file(em).write_bytes(data)
    except OSError:
        return None
    _set_logo_source(em, "steamgriddb")
    return logo_file(em)


def fetch_logo(em: Emulator, fetch: Callable[[str], bytes] | None = None) -> Path | None:
    """The cached picture, downloading it first if there isn't one. None when there is no address, it can't be reached, or what came
    back isn't a PNG, JPEG, GIF or WebP image of a sane size."""
    path = logo_file(em)
    if path.is_file() and path.stat().st_size:
        return path
    url = logo_url(em)
    if not url:
        return None
    try:
        data = (fetch or (lambda u: rc._get(u, timeout=15)))(url)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    magic = data[:12]
    if not data or len(data) > LOGO_MAX or not (magic.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")) or (magic[:4] == b"RIFF" and magic[8:12] == b"WEBP")):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        return None
    return path
