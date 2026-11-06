"""Tag saved soundtrack files (title, album, artist, track and disc numbers, cover) so players such as Plex, Jellyfin
and phone apps show them properly. Uses `mutagen` when it is installed; without it nothing is tagged and nothing
breaks. Existing tags are never overwritten: only what is missing is filled in."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .soundtracks import AUDIO_EXTS

TRACK_RE = re.compile(r"^\s*(\d{1,3})\s*[-_.)]*\s*(.*)$")
DISC_RE = re.compile(r"(?:cd|dis[ck])\s*0*(\d+)", re.IGNORECASE)
COVER_NAMES = ("cover.jpg", "cover.png")


@dataclass(frozen=True, slots=True)
class TagInfo:
    album: str
    artist: str = ""
    cover: bytes | None = None
    cover_mime: str = "image/jpeg"
    total: int = 0  # tracks in the soundtrack (0: unknown)


def parse_track(path: Path) -> tuple[int | None, str, int | None]:
    """(track number, title, disc number) from `<disc folder>/NN - Title.ext`."""
    m = TRACK_RE.match(path.stem)
    number, title = (int(m.group(1)), m.group(2).strip()) if m and m.group(2).strip() else (None, path.stem)
    disc = DISC_RE.search(path.parent.name)
    return number, title or path.stem, int(disc.group(1)) if disc else None


def _mutagen():
    try:
        import mutagen
        return mutagen
    except ImportError:
        return None


def available() -> bool:
    return _mutagen() is not None


def tag_file(path: Path, info: TagInfo) -> str:
    """Fill in the missing tags of one audio file. Returns 'tagged', or why it was left alone."""
    if path.suffix.lower() not in AUDIO_EXTS:
        return "not audio"
    mutagen = _mutagen()
    if mutagen is None:
        return "mutagen is not installed"
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return "unknown format"
        if audio.tags is None:
            audio.add_tags()
        number, title, disc = parse_track(path)
        wanted = {"title": title, "album": info.album, "artist": info.artist, "albumartist": info.artist,
                  "tracknumber": (f"{number}/{info.total}" if number and info.total else str(number) if number else ""),
                  "discnumber": str(disc) if disc else ""}
        changed = False
        for key, value in wanted.items():
            if not value:
                continue
            try:
                if not audio.get(key):
                    audio[key] = [value]
                    changed = True
            except (KeyError, ValueError, TypeError):
                continue  # this format has no such field
        if changed:
            audio.save()
        if info.cover:
            _embed_cover(path, info)
        return "tagged"
    except Exception as exc:  # a bad file must never stop the rest
        return f"could not tag: {exc}"


def _embed_cover(path: Path, info: TagInfo) -> None:
    """Add the cover as the file's picture when it has none. Best effort per format."""
    mutagen = _mutagen()
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(path)
            if not f.pictures:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, info.cover_mime, info.cover
                f.add_picture(pic)
                f.save()
        elif ext == ".mp3":
            from mutagen.id3 import APIC, ID3
            tags = ID3(path)
            if not tags.getall("APIC"):
                tags.add(APIC(encoding=3, mime=info.cover_mime, type=3, desc="Cover", data=info.cover))
                tags.save(path)
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(path)
            if "covr" not in (f.tags or {}):
                fmt = MP4Cover.FORMAT_PNG if info.cover_mime.endswith("png") else MP4Cover.FORMAT_JPEG
                f["covr"] = [MP4Cover(info.cover, imageformat=fmt)]
                f.save()
    except Exception:
        pass  # the cover.jpg beside the tracks still covers it
    _ = mutagen


def save_cover_file(folder: Path, data: bytes, mime: str) -> Path | None:
    """Write cover.jpg / cover.png beside the tracks unless a cover is already there."""
    if any((folder / n).exists() for n in COVER_NAMES):
        return None
    target = folder / ("cover.png" if mime.endswith("png") else "cover.jpg")
    try:
        target.write_bytes(data)
    except OSError:
        return None
    return target
