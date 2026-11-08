"""Identity, file-type sets, regexes and message tables shared by the whole app."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


# No flashing console windows, and child processes (chdman / 7-Zip) run at BELOW_NORMAL priority.
# chdman can use every core without making the PC sluggish: priority only matters when another
# program actually wants the CPU, so there is no slowdown when the machine is otherwise idle.
SUBPROCESS_FLAGS = (
    (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if sys.platform == "win32" else 0
)


# Identity: shown in the title bar, the window header and the log banner.
APP_NAME = "Distill"  # the name people see, and the identity of settings folders, registry keys and the user agent
APP_VERSION = "v1.0"  # keep in step with `version` in pyproject.toml (tests/test_structure.py checks) and with release tags


# GameCube/Wii-only containers. A plain .iso is told apart from PS1/PS2/PSP images by its header.
DOLPHIN_ONLY_EXTENSIONS = frozenset({".gcm", ".wbfs", ".gcz", ".wia"})


CHD_EXTENSIONS = frozenset({".cue", ".gdi", ".iso", ".cdi", ".toc", ".nrg"})


RVZ_EXTENSIONS = frozenset({".iso"}) | DOLPHIN_ONLY_EXTENSIONS


CSO_EXTENSIONS = frozenset({".iso"})  # only PSP discs (PSP_GAME folder) are taken on the CSO tab


OUTPUT_EXTENSION = {"chd": ".chd", "rvz": ".rvz", "cso": ".cso", "pkg": "", "zar": ".zar", "ps3": ".iso", "xbox": ".xiso"}  # pkg: a folder


TOOL_NAMES = {"chd": "chdman", "rvz": "DolphinTool", "cso": "maxcso", "pkg": "PKG_Extractor", "zar": "ZArchive", "ps3": "PS3Dec (PS3QDD)", "xbox": "xdvdfs"}


XBOX_EXTENSIONS = frozenset({".iso"})  # only Xbox / Xbox 360 discs (an XDVDFS volume) are taken on the Xbox tab


PS3_EXTENSIONS = frozenset({".iso"})  # only PS3 discs (PS3_GAME folder) are taken on the PS3 tab


PS3_OUTPUTS = {"Decrypted ISO": "iso", "Loose folder (extracted)": "folder", "Decrypted ISO + loose folder": "both"}


NO_PROGRESS_RE = re.compile(rb"(?!)")  # matches nothing: a tool that prints no usable progress


# chdman codecs for each CHD compression choice in Settings. chdman encodes at its heaviest settings by default (LZMA
# is the slow part), so lighter codecs trade a little size for a lot of speed. None passes no -c: chdman's own default.
CHD_CODECS: dict[str, dict[str, str | None]] = {
    "default": {"cd": None, "dvd": None},
    "fast": {"cd": "cdzl,cdfl", "dvd": "zlib"},  # zlib (+ FLAC for audio tracks)
    "fastest": {"cd": "cdfl", "dvd": "huff"},  # CD: FLAC on audio only, data left raw; DVD: Huffman
    "none": {"cd": "none", "dvd": "none"},
}


# CSO v1 is the format every PSP emulator and CFW plugin reads. maxcso prints plain "NN%" progress.
MAXCSO_FORMAT = "cso1"


PKG_EXTENSIONS = frozenset({".pkg"})


PKG_LAYOUTS = {"One folder per PKG": "folder", "shadPS4 layout": "shadps4"}


PKG_TOOLS = {"shadplus": "ShadPS4Plus PKG_Extractor", "anyps5": "pkg-to-anyps5"}  # PS4, PS5



TOOL_MISSING_TEXT = {  # shown when a page's tool can't be found (the Settings window opens on Tools)
    "chd": "Could not locate the 'chdman' executable.\n\nPlease use the Browse button to select your actual chdman.exe file.",
    "rvz": "Could not locate DolphinTool.\n\nPlease use the Browse button to select DolphinTool.exe (or your Dolphin folder).",
    "cso": "Could not locate the 'maxcso' executable.\n\nPlease use the Browse button to select your maxcso.exe file.",
    "xbox": "Could not locate xdvdfs.\n\nPlease use the Browse button to select xdvdfs.exe (from github.com/antangelo/xdvdfs).",
    "ps3": "Could not locate PS3Dec.\n\nPlease use the Browse button to select PS3Dec.exe (it ships inside PS3QDD, "
           "github.com/ElektroStudios/PS3-Quick-Disc-Decryptor).",
    "relinker": "Could not locate the AnyPS5 relinker.\n\nUse the Browse button to select relinker.exe (from a "
                "boykopovar/AnyPS5 release or your own build), or choose KytyPS5 instead.",
    "zar": "Could not locate the 'zarchive' executable.\n\nPlease use the Browse button to select zarchive.exe "
           "(from github.com/Exzap/ZArchive).",
}


PKG_MISSING_HINT = {
    "anyps5": "Please use the Browse button to select pkg-to-anyps5.exe (or install it with  cargo install "
              "--git https://github.com/awake-devel/pkg-to-anyps5).",
    "shadplus": "Please use the Browse button to select pkg_extractor.exe (from github.com/AzaharPlus/shadPS4Plus, "
                "release 'PKG Extractor').",
}


if sys.platform.startswith("linux"):
    _EXE = ".exe" if sys.platform == "win32" else ""  # only Windows programs carry the extension

    def _missing_tool(main: str, linux_hint: str = "") -> str:
        """Help text for a program that isn't there: what to pick, then (on Linux) where to get it."""
        return main + (f"\n\n{linux_hint}" if linux_hint and sys.platform != "win32" else "")

    TOOL_MISSING_TEXT = {  # shown when a page's tool can't be found (the Settings window opens on Tools)
        "chd": _missing_tool(
            f"Could not locate the 'chdman' program.\n\nPlease use the Browse button to select your actual chdman{_EXE} file.",
            "On Linux chdman ships with MAME's tools: Fedora/Bazzite 'sudo rpm-ostree install mame-tools', "
            "Debian/Ubuntu 'sudo apt install mame-tools', Arch 'sudo pacman -S mame-tools'."),
        "rvz": _missing_tool(
            f"Could not locate DolphinTool.\n\nPlease use the Browse button to select DolphinTool{_EXE} "
            "(or your Dolphin folder).",
            "On Linux, DolphinTool is built together with Dolphin (github.com/dolphin-emu/dolphin, cmake); no project "
            "publishes a Linux binary of it."),
        "cso": _missing_tool(
            f"Could not locate the 'maxcso' program.\n\nPlease use the Browse button to select your maxcso{_EXE} file.",
            "On Linux, maxcso has to be built from source (github.com/unknownbrackets/maxcso, make), or installed from "
            "your distribution if it packages it."),
        "xbox": _missing_tool(
            f"Could not locate xdvdfs.\n\nPlease use the Browse button to select xdvdfs{_EXE} (from "
            "github.com/antangelo/xdvdfs)."),
        "ps3": _missing_tool(
            f"Could not locate PS3Dec.\n\nPlease use the Browse button to select PS3Dec{_EXE} (on Windows it ships "
            "inside PS3QDD, github.com/ElektroStudios/PS3-Quick-Disc-Decryptor; on Linux and macOS use ps3dec from "
            "github.com/Redrrx/ps3dec)."),
        "relinker": _missing_tool(
            f"Could not locate the AnyPS5 relinker.\n\nUse the Browse button to select relinker{_EXE} (from a "
            "boykopovar/AnyPS5 release or your own build), or choose KytyPS5 instead."),
        "zar": _missing_tool(
            f"Could not locate the 'zarchive' program.\n\nPlease use the Browse button to select zarchive{_EXE} "
            "(from github.com/Exzap/ZArchive).",
            "On Linux, ZArchive has to be built from source (github.com/Exzap/ZArchive, cmake), or installed from your "
            "distribution if it packages it."),
    }

    PKG_MISSING_HINT = {
        "anyps5": f"Please use the Browse button to select pkg-to-anyps5{_EXE} (or install it with  cargo install "
                  "--git https://github.com/awake-devel/pkg-to-anyps5).",
        "shadplus": f"Please use the Browse button to select pkg_extractor{_EXE} (from github.com/AzaharPlus/shadPS4Plus, "
                    "release 'PKG Extractor').",
    }


TOOLS_DIR_NAME = "tools"  # every conversion tool gets a folder of its own in here


# RVZ: zstd with Dolphin's suggested block size. Level and scrubbing are user settings.
RVZ_BLOCK_SIZE = "131072"


RVZ_DEFAULT_LEVEL = 5


RVZ_MAX_LEVEL = 22


ARCHIVE_EXTENSIONS = frozenset({".zip", ".7z", ".7zip"})


PROGRESS_RE = re.compile(rb"(\d+(?:\.\d+)?)%\s*complete")


CHD_DONE_RE = re.compile(rb"compression complete", re.I)  # chdman's last line once the whole CHD is written; it should exit within moments


PERCENT_RE = re.compile(rb"(\d+(?:\.\d+)?)\s*%")


LINE_SPLIT_RE = re.compile(rb"[\r\n]+")


CUE_FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)


GDI_FILE_RE = re.compile(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"([^"]+)"|(\S+))')


PHASE_WORK = "Extracting & Converting"


# .iso files are inspected: PSP (PSP_GAME folder) and PS2 (SYSTEM.CNF containing BOOT2) go through
# createdvd. Anything else falls back to size: bigger than any CD image => DVD. cue/gdi/toc/cdi/nrg
# inputs always use createcd. 2048 is the hunk size PPSSPP recommends, and community testing
# reports it gives slightly smaller LZMA output than chdman's 4096 default.
DVD_ISO_MIN_BYTES = 900 * 1024 * 1024


DVD_HUNK_SIZE = "2048"


if getattr(sys, "frozen", False):  # a packaged build: tools/ and settings live beside the .exe
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent  # the folder holding the distill package

# Bundled read-only files (fonts, icons, the emulator and recomp catalogs). A PyInstaller build unpacks its data files
# under sys._MEIPASS (Distill.spec puts them at distill/assets); a source checkout keeps them beside this file.
ASSETS_DIR = (Path(sys._MEIPASS) / "distill" if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parent) / "assets"


ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
