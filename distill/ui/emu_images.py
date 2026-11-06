"""Keeping the pictures that ship with the app small and alike: every icon in assets/emulator_images/icons is brought to a
ICON_SIZE-pixel square PNG named for its emulator id, in place. Best effort: a folder that cannot be written to (a frozen build
under Program Files, say) is left as it is, and the picture is shown at whatever size it has."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter

from .. import emulators as em


def read_image(src: Path) -> QImage:
    """The picture in a file. An .ico holds several sizes, and Qt's plain load gives the first, which is often the 16 px one: the
    biggest is taken instead."""
    if src.suffix.lower() != ".ico":
        return QImage(str(src))
    reader = QImageReader(str(src))
    best = QImage()
    for index in range(max(1, reader.imageCount())):
        if reader.jumpToImage(index) and not (candidate := reader.read()).isNull():
            if best.isNull() or candidate.width() * candidate.height() > best.width() * best.height():
                best = candidate
    return best


def square_png(src: Path, size: int = em.ICON_SIZE) -> QImage | None:
    """The picture fitted inside a transparent size x size square (never cropped, centred, smoothly scaled either way)."""
    image = read_image(src)
    if image.isNull():
        return None
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    fitted = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = QImage(size, size, QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.drawImage(QRect((size - fitted.width()) // 2, (size - fitted.height()) // 2, fitted.width(), fitted.height()), fitted)
    painter.end()
    return out


def _already_right(path: Path, size: int) -> bool:
    if path.suffix != ".png" or path.name != path.name.lower():
        return False
    probe = QImage(str(path))
    return not probe.isNull() and probe.width() == size and probe.height() == size


def normalize_icons(folder: Path | None = None, size: int = em.ICON_SIZE) -> list[str]:
    """Resize and convert every icon in the folder, replacing the original. Where one name has several files (duck.jpg and
    duck.png) the newest is used and the others are removed. Returns what was done, one line each (empty when all was right)."""
    folder = folder or em.images_dir()
    done: list[str] = []
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in em.IMAGE_EXTS]
    except OSError:
        return done
    groups: dict[str, list[Path]] = {}
    for p in files:
        groups.setdefault(p.stem.lower(), []).append(p)
    for stem, group in sorted(groups.items()):
        group.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest = group[0]
        target = folder / f"{stem}.png"
        try:
            if len(group) == 1 and _already_right(newest, size):
                continue
            if (image := square_png(newest, size)) is None:
                continue  # not a picture: left alone
            part = target.with_name(f"{stem}.png.part")
            if not image.save(str(part), "PNG"):
                part.unlink(missing_ok=True)
                continue
            for p in group:
                if p != target:
                    p.unlink()
            os.replace(part, target)  # (a same-named .png is replaced by the new one, in one step)
            done.append(f"{newest.name} -> {target.name} ({size}x{size}, {target.stat().st_size // 1024} KB)")
        except OSError:
            continue  # read-only or in use: try again next time
    return done
