"""Qt-free runtime locations. Resolving paths never creates application state."""
from __future__ import annotations

import io
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from .constants import APP_DIR


def layout() -> str:
    return "xdg" if sys.platform.startswith("linux") and os.environ.get("DISTILL_PORTABLE") != "1" else "legacy"


def _directory(category: str, fallback: str) -> Path:
    if layout() == "legacy":
        return APP_DIR
    value = os.environ.get(f"XDG_{category}_HOME", "")
    if value and "\0" not in value and os.path.isabs(value):
        return Path(value) / "distill"
    try:
        home = Path.home()
    except (RuntimeError, OSError) as exc:
        raise RuntimeError("Cannot determine the home directory for Distill runtime files") from exc
    if not home.is_absolute():
        raise RuntimeError("Cannot determine an absolute home directory for Distill runtime files")
    return home / fallback / "distill"


def config_dir() -> Path:
    return _directory("CONFIG", ".config")


def data_dir() -> Path:
    return _directory("DATA", ".local/share")


def state_dir() -> Path:
    return _directory("STATE", ".local/state")


def cache_dir() -> Path:
    return _directory("CACHE", ".cache")


def ensure_dir(path: Path) -> None:
    """Create missing XDG components owner-only; leave existing permissions alone."""
    path = Path(path)
    if layout() == "legacy":
        path.mkdir(parents=True, exist_ok=True)
        return
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(f"Not a directory: {current}")
    for component in reversed(missing):
        try:
            component.mkdir(mode=0o700)
        except FileExistsError:
            if not component.is_dir():
                raise


def ensure_parent(file: Path) -> None:
    ensure_dir(Path(file).parent)


_CONFIG_MIGRATION = (
    "distill_settings.json", "distill_emulators_catalog.json", "distill_recomps_catalog.json",
    "distill_forks.json", "distill_ytdlp.json", "distill_torrent_settings.json", "distill_soundtracks.json",
)
# Fixed keys from orgpages.ORG_TOOLS; arbitrary distill_undo_* files are never scanned.
_UNDO_TOOLS = ("cleanup", "dupes", "onegr", "discfix", "patch", "tidy", "multi", "playlists",
               "bysystem", "datcheck", "bioscheck", "report")
_STATE_MIGRATION = ("distill_soundtrack_saves.json", *(f"distill_undo_{tool}.json" for tool in _UNDO_TOOLS))
_MIGRATION_LIMIT = 8 * 1024 * 1024


def migrate_legacy() -> list[dict[str, str]]:
    """Copy only safe, allowlisted legacy files; existing destinations always win.

    Login data, rules, queues, logs, caches, tools and installed games are deferred.
    Failures are returned per file and never abort startup or remove a source.
    """
    if layout() != "xdg":
        return []
    results = []
    for names, directory in ((_CONFIG_MIGRATION, config_dir), (_STATE_MIGRATION, state_dir)):
        for name in names:
            source = APP_DIR / name
            temporary = None
            result = {"source": str(source), "status": "skipped"}
            try:
                try:
                    info = source.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MIGRATION_LIMIT:
                    result["reason"] = "not a regular file or exceeds size limit"
                    results.append(result)
                    continue
                destination = directory() / name
                result["destination"] = str(destination)
                if os.path.lexists(destination):
                    result["reason"] = "destination exists"
                    results.append(result)
                    continue
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("Legacy source changed while opening")
                    raw = stream.read(_MIGRATION_LIMIT + 1)
                if len(raw) > _MIGRATION_LIMIT:
                    result["reason"] = "exceeds size limit"
                    results.append(result)
                    continue
                ensure_parent(destination)
                fd, name_tmp = tempfile.mkstemp(prefix=".distill-migrate-", dir=destination.parent)
                temporary = Path(name_tmp)
                with os.fdopen(fd, "wb") as stream:
                    shutil.copyfileobj(io.BytesIO(raw), stream)
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    result["reason"] = "destination created concurrently"
                else:
                    result["status"] = "copied"
            except OSError as exc:
                result.update(status="failed", reason=str(exc))
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except OSError as exc:
                        result.update(status="failed", reason=str(exc))
            results.append(result)
    return results
