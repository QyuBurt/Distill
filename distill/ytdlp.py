"""yt-dlp for video and audio sites (YouTube, Vimeo, SoundCloud, Bandcamp, Twitch, and hundreds more).

Distill doesn't reimplement any site: it lists a link with `yt-dlp --flat-playlist -J`, queues each video as a row,
and downloads each one by running yt-dlp and reading its progress. yt-dlp is found on PATH or in the runtime tools/ytdlp directory, and
can be fetched from its official GitHub release from Settings. Merging separate video and audio streams needs
ffmpeg beside yt-dlp (without it yt-dlp falls back to the best single file). No Qt in here."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .constants import APP_DIR, APP_NAME, SUBPROCESS_FLAGS, TOOLS_DIR_NAME
from . import paths

PREFIX = "ytdlp:"  # RemoteFile.url of a row this module handles
SETTINGS_FILE = paths.config_dir() / "distill_ytdlp.json"
PRESETS = {
    "Best quality": ["-f", "bv*+ba/b"],
    "Up to 1080p": ["-f", "bv*[height<=1080]+ba/b[height<=1080]"],
    "Up to 720p": ["-f", "bv*[height<=720]+ba/b[height<=720]"],
    "Audio only": ["-f", "bestaudio/b"],
}
VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "soundcloud.com", "bandcamp.com", "dailymotion.com",
    "tiktok.com", "twitter.com", "x.com", "reddit.com", "facebook.com", "instagram.com", "bilibili.com",
    "nicovideo.jp", "mixcloud.com", "streamable.com", "rumble.com", "odysee.com", "kick.com", "ted.com",
)
FORCE_PREFIXES = ("yt:", "ytdlp:", "yt-dlp:")  # paste "yt:https://..." to hand any link to yt-dlp
PERMANENT = ("private video", "video unavailable", "sign in", "members-only", "unsupported url", "requires login",
             "this video is", "has been removed", "not available in your country", "age-restricted",
             "ffmpeg not found", "ffmpeg is not installed")  # the last two: retrying cannot install ffmpeg

# What a row can be asked to become (the expandable picker on the Download page). A choice is stored on the row as
# 'kind:quality' text, e.g. 'mp4:1080' or 'mp3:320' ('mp4:best' = no height limit); blank = use the Settings quality.
KINDS = {"mp4": "MP4 video", "mp3": "MP3 audio"}
MP4_STEPS = (2160, 1440, 1080, 720, 480, 360, 240, 144)  # heights offered, when the video has them
MP3_RATES = (320, 256, 192, 160, 128, 96)  # kbit/s offered


class YtdlpError(Exception):
    pass


@dataclass(slots=True)
class Settings:
    preset: str = "Best quality"
    exe: str = ""  # a yt-dlp the person picked; blank = look in ./tools/ytdlp, then PATH


SETTINGS = Settings()


def load_settings() -> None:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        SETTINGS.preset = data.get("preset") if data.get("preset") in PRESETS else "Best quality"
        SETTINGS.exe = str(data.get("exe", ""))
    except (OSError, ValueError, AttributeError):
        pass


def save_settings() -> None:
    try:
        paths.ensure_parent(SETTINGS_FILE)
        SETTINGS_FILE.write_text(json.dumps({"preset": SETTINGS.preset, "exe": SETTINGS.exe}), encoding="utf-8")
    except OSError:
        pass


def parse_choice(spec: str) -> tuple[str, int] | None:
    """('mp4' | 'mp3', height or kbit/s; 0 = best) from a row's stored choice, or None for blank / unreadable."""
    kind, _, quality = (spec or "").strip().lower().partition(":")
    if kind not in KINDS:
        return None
    if quality in ("", "best"):
        return kind, 0
    try:
        number = int(quality)
    except ValueError:
        return None
    return (kind, number) if number > 0 else None


def make_choice(kind: str, quality: int) -> str:
    return f"{kind}:{quality if quality > 0 else 'best'}"


def choice_label(spec: str) -> str:
    """'MP4 1080p', 'MP3 320 kbps', 'MP4 best' ... for the list; blank when there is no explicit choice."""
    if (got := parse_choice(spec)) is None:
        return ""
    kind, quality = got
    if kind == "mp3":
        return f"MP3 {quality} kbps" if quality else "MP3 best"
    return f"MP4 {quality}p" if quality else "MP4 best"


def choice_args(spec: str) -> list[str]:
    """yt-dlp arguments for a stored choice (empty when there is none: the Settings quality applies instead)."""
    if (got := parse_choice(spec)) is None:
        return []
    kind, quality = got
    if kind == "mp3":  # the best audio there is, converted (needs ffmpeg); 0 = best VBR
        return ["-f", "bestaudio/b", "-x", "--audio-format", "mp3", "--audio-quality", f"{quality}K" if quality else "0"]
    limit = f"[height<={quality}]" if quality else ""
    # res first, then prefer streams that fit an .mp4 without re-encoding; the merge is a remux (ffmpeg)
    return ["-f", f"bv*{limit}+ba/b{limit}", "-S", "res,ext:mp4:m4a", "--merge-output-format", "mp4"]


def preset_choice(preset: str) -> str:
    """The choice the picker shows for a row that has none: what the Settings quality means, in picker terms."""
    if preset == "Audio only":
        return make_choice("mp3", 320)
    height = {"Up to 1080p": 1080, "Up to 720p": 720}.get(preset, 0)
    return make_choice("mp4", height)


def mp4_options(heights: list[int] | None) -> list[int]:
    """Heights to offer for a video: the steps it can reach (all of them when unknown), plus its own top height."""
    if not heights:
        return list(MP4_STEPS)
    top = max(heights)
    steps = [h for h in MP4_STEPS if h <= top]
    if top not in steps and top > MP4_STEPS[-1]:
        steps = sorted({*steps, top}, reverse=True)
    return steps or [top]


def ffmpeg_found() -> bool:
    """Whether ffmpeg can be found (on PATH, or beside yt-dlp): MP3 conversion and stream merging need it."""
    if shutil.which("ffmpeg"):
        return True
    exe = find_exe()
    names = ("ffmpeg.exe",) if sys.platform == "win32" else ("ffmpeg",)
    folders = [tools_folder(), *((exe.parent,) if exe else ())]
    return any((folder / n).is_file() for folder in folders for n in names)


def tools_folder() -> Path:
    if sys.platform.startswith("linux"):
        return paths.data_dir() / TOOLS_DIR_NAME / "ytdlp"
    for base in (APP_DIR, Path(os.environ.get("LOCALAPPDATA", "")) / APP_NAME if os.environ.get("LOCALAPPDATA") else None):
        if base is None:
            continue
        folder = base / TOOLS_DIR_NAME / "ytdlp"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except OSError:
            continue
    return Path(os.environ.get("TEMP", "/tmp")) / "distill_ytdlp"


def _exe_names() -> tuple[str, ...]:
    return ("yt-dlp.exe",) if sys.platform == "win32" else ("yt-dlp", "yt-dlp_macos", "yt-dlp_linux")


def find_exe() -> Path | None:
    if SETTINGS.exe and Path(SETTINGS.exe).is_file():
        return Path(SETTINGS.exe)
    for name in _exe_names():
        local = tools_folder() / name
        if local.is_file():
            return local
    found = shutil.which("yt-dlp")
    return Path(found) if found else None


def is_video_url(url: str) -> bool:
    if url.lower().startswith(FORCE_PREFIXES):
        return True
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS)


def strip_prefix(token: str) -> str:
    for p in FORCE_PREFIXES:
        if token.lower().startswith(p):
            return token[len(p):]
    return token


# ---- listing ---------------------------------------------------------------------------------------------------------
def parse_listing(data: dict, source_url: str):
    """RemoteFile rows from yt-dlp's JSON for a link: one for a video, one per entry for a playlist."""
    from .downloads import RemoteFile  # noqa: PLC0415  (downloads imports this module's settings lazily)

    def row(item: dict, group: str):
        vid = str(item.get("id") or "")
        target = item.get("webpage_url") or item.get("url") or ""
        if not str(target).startswith("http"):
            if str(item.get("ie_key") or "").lower() == "youtube" and vid:
                target = f"https://www.youtube.com/watch?v={vid}"
            else:
                return None
        title = str(item.get("title") or vid or "video").strip() or "video"
        name = f"{title} [{vid}]" if vid and vid not in title else title
        size = item.get("filesize") or item.get("filesize_approx")
        return RemoteFile(name, PREFIX + str(target), int(size) if isinstance(size, (int, float)) and size > 0 else None,
                          group, None, False, True, "yt-dlp")

    if data.get("_type") == "playlist" or "entries" in data:
        group = str(data.get("title") or data.get("id") or "playlist").strip()
        rows = [r for e in (data.get("entries") or []) if isinstance(e, dict) and (r := row(e, group))]
        if not rows:
            raise YtdlpError("That playlist has no videos yt-dlp can read")
        return rows
    single = row({**data, "webpage_url": data.get("webpage_url") or source_url}, "")
    if single is None:
        raise YtdlpError("yt-dlp found nothing to download at that link")
    return [single]


def list_entries(url: str, exe: Path | None = None, run: Callable = subprocess.run):
    exe = exe or find_exe()
    if not exe:
        raise YtdlpError("Video sites need yt-dlp. It is downloaded in Settings > Tools > Download Tools.")
    try:
        done = run([str(exe), "--flat-playlist", "-J", "--no-warnings", "--", strip_prefix(url)], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", timeout=180, stdin=subprocess.DEVNULL,
                   creationflags=SUBPROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise YtdlpError(f"yt-dlp: {exc}") from None
    if done.returncode != 0:
        lines = [ln for ln in (done.stderr or "").splitlines() if ln.strip()]
        raise YtdlpError("yt-dlp: " + (next((ln for ln in lines if ln.startswith("ERROR")), lines[-1] if lines else "failed")
                                       .removeprefix("ERROR: ")))
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise YtdlpError("yt-dlp returned something unreadable") from None
    return parse_listing(data, strip_prefix(url))


def parse_heights(data: dict) -> list[int]:
    """The video heights a video offers, from yt-dlp's JSON for it (tallest first; empty for audio-only links)."""
    found = {int(f["height"]) for f in data.get("formats") or [] if isinstance(f, dict)
             and isinstance(f.get("height"), (int, float)) and f["height"] > 0 and f.get("vcodec") != "none"}
    return sorted(found, reverse=True)


def probe_heights(url: str, exe: Path | None = None, run: Callable = subprocess.run) -> list[int]:
    """Ask yt-dlp which heights one video comes in (a few seconds: the page is read, nothing is downloaded)."""
    exe = exe or find_exe()
    if not exe:
        raise YtdlpError("yt-dlp isn't installed")
    try:
        done = run([str(exe), "-J", "--no-playlist", "--no-warnings", "--", strip_prefix(url)], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", timeout=90, stdin=subprocess.DEVNULL,
                   creationflags=SUBPROCESS_FLAGS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise YtdlpError(f"yt-dlp: {exc}") from None
    if done.returncode != 0:
        raise YtdlpError("yt-dlp couldn't read that video")
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise YtdlpError("yt-dlp returned something unreadable") from None
    return parse_heights(data) if isinstance(data, dict) else []


# ---- downloading -----------------------------------------------------------------------------------------------------
def command(exe: Path, url: str, dest: Path, preset: str, limit_kbps: int = 0, proxy: str = "",
            choice: str = "") -> list[str]:
    """yt-dlp's command line for one video, saved as '<dest>.<ext>' (the extension is whatever it picks). A row's own
    `choice` (see parse_choice) wins over the Settings quality `preset`."""
    template = str(dest.parent / (dest.name.replace("%", "%%") + ".%(ext)s"))
    cmd = [str(exe), "--newline", "--no-warnings", "--no-playlist", "--progress", "--progress-template",
           "download:dlp|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s",
           "--print", "after_move:dlpath|%(filepath)s", "-o", template,
           *(choice_args(choice) or PRESETS.get(preset, PRESETS["Best quality"]))]
    if limit_kbps > 0:
        cmd += ["--limit-rate", f"{limit_kbps}K"]
    if proxy:
        cmd += ["--proxy", "" if proxy == "direct" else proxy]
    return [*cmd, "--", url]


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_progress(line: str) -> tuple[int, int | None, float | None] | None:
    """(bytes so far, total or estimate, bytes per second) from one of our progress lines."""
    if not line.startswith("dlp|"):
        return None
    f = line.rstrip("\r\n").split("|")[1:]
    if len(f) < 4:
        return None
    done, total, est, speed = (_num(x) for x in f[:4])
    got = total if total else est
    return int(done or 0), int(got) if got else None, speed


def final_path(line: str) -> Path | None:
    return Path(line.rstrip("\r\n")[len("dlpath|"):]) if line.startswith("dlpath|") else None


def is_permanent_failure(message: str) -> bool:
    low = message.lower()
    return any(p in low for p in PERMANENT)
