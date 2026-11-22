"""How big a game's own media (disc, cartridge, UMD) is next to its case, per platform, so the game page can draw them in proportion.

Pure Python (no Qt) so it can be tested. The numbers are real-world millimetres where the size is well known, else estimates; every
entry is a share of the *cover art*, not of the whole case (the art is a few millimetres smaller than the case). Tune them here.

  disc / umd : the media's diameter (UMD: the shell's side) as a share of the cover's HEIGHT
  cart       : the cartridge's width as a share of the cover's WIDTH
"""
from __future__ import annotations

from typing import Callable, NamedTuple


class MediaSpec(NamedTuple):
    shape: str  # "disc" | "cart" | "umd"
    scale: float


DEFAULT_DISC, DEFAULT_CART = 0.78, 0.66  # for a platform not listed below (the old fixed look)
MIN_CART_PX, MIN_UMD_PX = 56, 84  # the smallest drawn, so a tiny real object (a Switch card) is still seen

# 120 mm discs. Cover heights: jewel case booklet 120 mm; DVD case (Amaray) art ~184 mm; Blu-ray case art ~166 mm.
_JEWEL = MediaSpec("disc", 1.00)  # 120 / 120: a CD fills its jewel case's front
_DVD = MediaSpec("disc", 0.65)  # 120 / 184: the same disc looks smaller in the taller case
_BLURAY = MediaSpec("disc", 0.72)  # 120 / 166
_SPECS: dict[str, MediaSpec] = {
    "PlayStation": _JEWEL, "Sega Saturn": _JEWEL, "Dreamcast": _JEWEL, "PC Engine CD": _JEWEL, "3DO": MediaSpec("disc", 0.96),
    "Sega CD": _DVD,  # (the US case was Genesis-sized)
    "PlayStation 2": _DVD, "Xbox": _DVD, "Xbox 360": _DVD, "Wii": _DVD, "Wii U": MediaSpec("disc", 0.66),
    "GameCube": MediaSpec("disc", 0.43),  # an 80 mm mini disc in a DVD-size case: 80 / 184
    "PlayStation 3": _BLURAY, "PlayStation 4": _BLURAY, "PlayStation 5": _BLURAY, "Xbox One": _BLURAY,
    "PSP": MediaSpec("umd", 0.385),  # a 64 mm UMD shell in a ~104 x 171 mm case: 64 / 166
    # cartridges (width of the cart / width of the box or case front)
    "NES": MediaSpec("cart", 0.82), "SNES": MediaSpec("cart", 0.72), "Nintendo 64": MediaSpec("cart", 0.78),
    "Mega Drive": MediaSpec("cart", 0.78), "Master System": MediaSpec("cart", 0.66), "Atari 2600": MediaSpec("cart", 0.62),
    "Atari 7800": MediaSpec("cart", 0.60), "Atari Lynx": MediaSpec("cart", 0.50), "Game Boy": MediaSpec("cart", 0.55),
    "Game Boy Color": MediaSpec("cart", 0.55), "Game Boy Advance": MediaSpec("cart", 0.62), "Game Gear": MediaSpec("cart", 0.55),
    "Neo Geo Pocket": MediaSpec("cart", 0.45), "Virtual Boy": MediaSpec("cart", 0.60), "WonderSwan": MediaSpec("cart", 0.40),
    "PC Engine": MediaSpec("cart", 0.48),  # a HuCard is a thin card
    "Nintendo DS": MediaSpec("cart", 0.32), "Nintendo 3DS": MediaSpec("cart", 0.32),  # ~33 mm card in a ~135 mm case
    "Switch": MediaSpec("cart", 0.21),  # 21 mm card in a ~104 mm case
}


def known_spec(system: str) -> MediaSpec | None:
    """The entry for this system, or None when it is not in the table."""
    return _SPECS.get(system)


def spec_for(system: str, kind: str) -> MediaSpec:
    """The media's shape and size for a system. `kind` ('disc'/'cart', from gamemedia.media_kind) is the fallback for a system not listed."""
    s = _SPECS.get(system)
    if s is not None:
        return s
    return MediaSpec("cart", DEFAULT_CART) if kind == "cart" else MediaSpec("disc", DEFAULT_DISC)


def find_disc(width: int, height: int, dark: Callable[[int, int], bool], step: int = 3) -> tuple[float, float, float] | None:
    """Find the disc inside a picture of a whole UMD (shell + disc). `dark(x, y)` says whether a pixel belongs to the disc rather than to the
    pale shell or the background. Returns (centre x, centre y, radius) in pixels, or None when there is no round disc that is clearly smaller
    than the picture (a picture of just a disc, or something that is not a disc).

    A column or row counts when enough of it is dark (so a printed logo in a corner of the shell does not widen the disc); the disc's edge
    is then pushed out by the little a chord that long falls short of the full width."""
    if width < 40 or height < 40:
        return None
    cols = [sum(1 for y in range(0, height, step) if dark(x, y)) * step for x in range(0, width, step)]
    rows = [sum(1 for x in range(0, width, step) if dark(x, y)) * step for y in range(0, height, step)]
    need_c, need_r = height * 0.18, width * 0.18
    xs = [i * step for i, c in enumerate(cols) if c >= need_c]
    ys = [i * step for i, c in enumerate(rows) if c >= need_r]
    if not xs or not ys:
        return None
    dx, dy = xs[-1] - xs[0] + step, ys[-1] - ys[0] + step
    if min(dx, dy) < 0.45 * min(width, height) or abs(dx - dy) > 0.08 * max(dx, dy):
        return None  # not round enough to be a disc
    r = (dx + dy) / 4.0
    r += need_c * need_c / (8.0 * r) / 2.0  # (the edge columns that were left out)
    cx, cy = (xs[0] + xs[-1] + step) / 2.0, (ys[0] + ys[-1] + step) / 2.0
    if r > 0.97 * min(width, height) / 2.0 or r * 2 > 0.97 * min(width, height):
        return None  # fills the picture: there is no shell around it
    return cx, cy, r
