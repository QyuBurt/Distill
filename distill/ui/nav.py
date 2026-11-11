"""Sidebar groups and per-page titles."""
from __future__ import annotations

from ..orgpages import ORG_PAGES


NAV_GROUPS = (
    ("GAMES", (("games", "Games"), ("recomps", "PC Recomps"), ("emus", "Emulators"))),
    ("DOWNLOADS", (("dl", "Files & Torrents"), ("sound", "Soundtrack Grabber"))),
    ("CONVERT & COMPRESS", (("chd", "CHD Compression"), ("cso", "CSO (PSP Images)"), ("ps3", "PS3 ISO / Folder"),
                            ("pkg", "PS4 PKG Manager"), ("pkg5", "PS5 PKG Manager"), ("rvz", "RVZ (Dolphin)"),
                            ("switch", "Switch NSZ / NUP"), ("xbox", "Xbox ISO / XEX"), ("zar", "ZAR Archive Tool"))),
    ("ORGANISE LIBRARY", tuple((p.key, p.nav) for p in ORG_PAGES)),  # in the order you would use them
)


# (title, blurb) under each page's name, as designed
PAGE_INFO = {
    "chd": ("CHD Compression", "Convert CD and DVD images (PS1, PS2, Saturn, Dreamcast, CD-i and more) to compressed "
                              "CHD with chdman."),
    "cso": ("CSO Compression (PSP)", "Compress PlayStation Portable ISO dumps into smaller .cso files with "
                                           "maxcso."),
    "pkg": ("PS4 PKG Manager", "Unpack PlayStation 4 .pkg files into game folders, with each game's "
                                          "updates merged in."),
    "pkg5": ("PS5 PKG Manager", "Unpack PlayStation 5 .pkg files, then relink them for AnyPS5 or pack them for "
                                 "KytyPS5."),
    "ps3": ("PS3 ISO & Folder Converter", "Decrypt PlayStation 3 ISOs and keep them as ISOs, unpack them to game "
                                          "folders, or both."),
    "rvz": ("RVZ Compression (Dolphin)", "Convert GameCube and Wii discs to compressed RVZ with DolphinTool."),
    "switch": ("Switch NSZ / NUP Converter", "Compress, decompress and merge Switch 1 games as NSZ / XCZ, or pack Switch 2 dumps "
                                           "into NUP with artwork."),
    "xbox": ("Xbox ISO / XEX Repacker", "Repack Original Xbox and Xbox 360 disc images into smaller, verified .xiso "
                                    "files with xdvdfs."),
    "zar": ("ZAR Archive Compressor", "Compress PS4, PS5 and Xbox 360 games and their updates into single .zar "
                                   "archives."),
    "dl": ("Files & Torrents", "Download archive.org items, file lists, links, magnets and .torrent files, "
                                        "with resume and verification."),
    "sound": ("Soundtrack Grabber", "Search, stream and save game soundtracks (FLAC or MP3) from KHInsider and archive.org, tagged and filed in "
                                    "one folder or beside each game for RomM."),
    "games": ("Games", "Your own game library: every platform in one place, with covers, details and a Play button. "
                      "Games start in RetroArch."),
    "recomps": ("PC Recomps", "Native PC ports of console games: install, update and play them with one click. "
                               "You bring your own copy of the game."),
    "emus": ("Emulators", "Find the emulators you already have, install the ones you don't, and keep them all up to date. "
                          "Point Distill at your Emulators folder once."),
    "fix": ("Fix Names & Discs", "Tidy filenames, group each multi-disc game with a playlist, fix broken playlists and "
                                  "repair disc images."),
    "extras": ("Remove Extras & Junk", "Find leftovers, duplicate games and extra releases, then remove what you "
                                             "tick."),
    "patch": ("Patch ROMs", "Apply IPS, UPS, BPS and xdelta patches to ROMs for translations and hacks, with "
                            "checksum verification. The original is kept."),
    "bysystem": ("Sort by System", "Move each game into a folder for its console, identified from the file itself."),
    "report": ("Library Report", "Save a list of every game with its system, format, region, size and folder as a "
                                 "web page or spreadsheet. Changes nothing."),
}

# what each page actually does, in the app's own words: shown when hovering the page's description
PAGE_DETAIL = {
    "chd": PAGE_INFO["chd"][1],
    "cso": "Convert PSP discs to compressed CSO with maxcso.",
    "pkg": "Extract PS4 .pkg files into game folders with ShadPS4Plus PKG_Extractor. Can merge each game's updates in "
           "and pack the result into a .zar.",
    "pkg5": "Extract PS5 debug .pkg files with pkg-to-anyps5. Relink them for AnyPS5, or pack the game files into a "
            ".zar with eboot.bin at the root for KytyPS5.",
    "ps3": "Decrypt Redump PS3 ISOs with PS3Dec and .dkey files. Keep the ISO, unpack it to a folder, or both. Output "
           "is named Game Name - Title ID from PARAM.SFO.",
    "rvz": "Convert GameCube and Wii discs to compressed RVZ with DolphinTool.",
    "switch": "Switch 1: nscb_rust compresses NSP / XCI to NSZ / XCZ, decompresses them, or merges a game with its updates and "
              "DLC. Switch 2: nup packs decrypted dumps (or Switch 1 files, decrypted with hactoolnet) into .nup with an icon.",
    "xbox": "Repack Xbox and Xbox 360 ISOs as trimmed .xiso files with xdvdfs, verified against the source by "
            "checksum. Original Xbox images run in xemu.",
    "zar": "Compress PS4, PS5 and Xbox 360 games and their updates into single .zar files with ZArchive. Read directly "
           "by shadPS4 (0.17+), KytyPS5 and Xenia Canary.",
    "dl": "Download archive.org items, file listings, 1fichier links, magnets and .torrent files, with resume and "
          "verification.",
    "sound": "Search for a game, stream its albums like a music player and save what you like: all in one Soundtracks folder, or beside "
             "each game for RomM.",
    "games": "Scans the folder you choose for games (the same scan as the Library Report), works out each one's platform and title, and shows them "
             "as posters. Covers come from SteamGridDB (with your key) or the LaunchBox Games Database; details come from LaunchBox. Play starts the "
             "game in RetroArch with the core you pick for that platform. Nothing in your folder is changed.",
    "recomps": "Projects that rebuild a console game to run natively on PC. Each is downloaded from its own GitHub "
                "releases; none includes the game, so the page checks your library for a copy you already own.",
    "emus": "Cards for emulators, grouped by console maker. Ones already on this PC are found in your Emulators folder and the usual places and "
            "linked (never deleted); the rest install from each project's own releases. Updates are merged over the folder, so saves and "
            "settings stay. The page also shows which emulators your Library Report says you need, and each emulator can hold forks you add yourself.",
    **{p.key: p.blurb for p in ORG_PAGES},
}

# the small label above each page's title
PAGE_CHIP = {
    "chd": "CHD", "cso": "PSP", "pkg": "PS4", "pkg5": "PS5", "ps3": "PS3", "rvz": "GAMECUBE / WII",
    "xbox": "XBOX", "switch": "SWITCH", "zar": "UTILITY", "fix": "ORGANISE", "extras": "ORGANISE", "patch": "ORGANISE", "bysystem": "ORGANISE",
    "report": "ANALYTICS", "dl": "DOWNLOAD", "sound": "AUDIO", "games": "GAMES", "recomps": "GAMES", "emus": "GAMES",
}
