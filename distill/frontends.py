"""Folder-layout presets for emulator front-ends: the folder name each one expects for a system.

Nothing here imports Qt. Names follow each front-end's documented defaults; a system a front-end has no folder for
falls back to the plain system name. Check against your own install before a big move (Undo Last Run reverses it)."""
from __future__ import annotations

from .constants import ILLEGAL_NAME_CHARS

PRESET_NONE = "Plain system names"
PRESETS = (PRESET_NONE, "RomM", "ES-DE", "Batocera", "EmuDeck", "LaunchBox")

# system -> (RomM, ES-DE, Batocera, EmuDeck, LaunchBox)
_TABLE: dict[str, tuple[str, str, str, str, str]] = {
    "PlayStation": ("psx", "psx", "psx", "psx", "Sony PlayStation"),
    "PlayStation 2": ("ps2", "ps2", "ps2", "ps2", "Sony PlayStation 2"),
    "PlayStation 3": ("ps3", "ps3", "ps3", "ps3", "Sony PlayStation 3"),
    "PlayStation 4": ("ps4", "ps4", "ps4", "ps4", "Sony PlayStation 4"),
    "PlayStation 5": ("ps5", "ps5", "ps5", "ps5", "Sony PlayStation 5"),
    "PSP": ("psp", "psp", "psp", "psp", "Sony PSP"),
    "GameCube": ("ngc", "gc", "gamecube", "gc", "Nintendo GameCube"),
    "Wii": ("wii", "wii", "wii", "wii", "Nintendo Wii"),
    "Wii U": ("wiiu", "wiiu", "wiiu", "wiiu", "Nintendo Wii U"),
    "Switch": ("switch", "switch", "switch", "switch", "Nintendo Switch"),
    "Nintendo 3DS": ("3ds", "n3ds", "3ds", "n3ds", "Nintendo 3DS"),
    "Nintendo DS": ("nds", "nds", "nds", "nds", "Nintendo DS"),
    "Nintendo 64": ("n64", "n64", "n64", "n64", "Nintendo 64"),
    "SNES": ("snes", "snes", "snes", "snes", "Super Nintendo Entertainment System"),
    "NES": ("nes", "nes", "nes", "nes", "Nintendo Entertainment System"),
    "Famicom Disk System": ("fds", "fds", "fds", "fds", "Nintendo Famicom Disk System"),
    "Game Boy": ("gb", "gb", "gb", "gb", "Nintendo Game Boy"),
    "Game Boy Color": ("gbc", "gbc", "gbc", "gbc", "Nintendo Game Boy Color"),
    "Game Boy Advance": ("gba", "gba", "gba", "gba", "Nintendo Game Boy Advance"),
    "Virtual Boy": ("virtualboy", "virtualboy", "virtualboy", "virtualboy", "Nintendo Virtual Boy"),
    "Dreamcast": ("dc", "dreamcast", "dreamcast", "dreamcast", "Sega Dreamcast"),
    "Sega Saturn": ("saturn", "saturn", "saturn", "saturn", "Sega Saturn"),
    "Sega CD": ("segacd", "segacd", "segacd", "segacd", "Sega CD"),
    "Mega Drive": ("genesis", "megadrive", "megadrive", "genesis", "Sega Genesis"),
    "Master System": ("sms", "mastersystem", "mastersystem", "mastersystem", "Sega Master System"),
    "Game Gear": ("gamegear", "gamegear", "gamegear", "gamegear", "Sega Game Gear"),
    "PC Engine": ("tg16", "pcengine", "pcengine", "pcengine", "NEC TurboGrafx-16"),
    "PC Engine CD": ("tgcd", "pcenginecd", "pcenginecd", "pcenginecd", "NEC TurboGrafx-CD"),
    "Atari 2600": ("atari2600", "atari2600", "atari2600", "atari2600", "Atari 2600"),
    "Atari 7800": ("atari7800", "atari7800", "atari7800", "atari7800", "Atari 7800"),
    "Atari Lynx": ("lynx", "atarilynx", "lynx", "atarilynx", "Atari Lynx"),
    "WonderSwan": ("wonderswan", "wonderswan", "wswan", "wonderswan", "WonderSwan"),
    "Neo Geo Pocket": ("ngp", "ngp", "ngp", "ngp", "SNK Neo Geo Pocket"),
    "Xbox": ("xbox", "xbox", "xbox", "xbox", "Microsoft Xbox"),
    "Xbox 360": ("xbox360", "xbox360", "xbox360", "xbox360", "Microsoft Xbox 360"),
    "3DO": ("3do", "3do", "3do", "3do", "3DO Interactive Multiplayer"),
}
_COLUMN = {name: i for i, name in enumerate(PRESETS[1:])}


def folder_for(preset: str, system: str) -> str:
    """The folder name `preset` uses for `system`; the plain system name for no preset or an unlisted system."""
    row = _TABLE.get(system)
    if row and preset in _COLUMN:
        return row[_COLUMN[preset]]
    return ILLEGAL_NAME_CHARS.sub("", system).strip(" .")


def system_for_folder(name: str) -> str:
    """Reverse lookup of a preset folder name ('n3ds' -> 'Nintendo 3DS'), case-insensitive; '' if unknown.
    LaunchBox's long names are included; names shared by two systems are skipped."""
    seen: dict[str, str] = {}
    clash: set[str] = set()
    for system, row in _TABLE.items():
        for n in row:
            k = n.casefold()
            if k in seen and seen[k] != system:
                clash.add(k)
            seen[k] = system
    k = name.casefold()
    return "" if k in clash else seen.get(k, "")
