<div align="center">

<img src="distill.png" alt="Distill" width="128">

# Distill

**Your retro and PC game library, in one app.**
Compress disc images, download games and soundtracks, install PC ports, and clean up your collection.

[Download the latest release](../../releases/latest) · [Report a problem](../../issues)

</div>

---

## What is Distill?

Distill is a Windows desktop app that puts the tools you'd normally run by hand in one place. It handles batch conversion and compression of game dumps, a download manager, native PC ports of console games, an emulator catalog, and a set of library organisers. It finds, downloads and updates the command-line tools it needs by itself.

## Download

Grab a build from the [Releases](../../releases/latest) page:

| File | What it is |
| --- | --- |
| `Distill-portable.zip` | Folder build. Starts fastest and trips antivirus the least. Unzip anywhere and run `Distill.exe`. |
| `Distill.exe` | Single file. Slower to start, since it unpacks itself to a temp folder each time. |

On Windows, settings and downloaded tools are kept next to the `.exe`, so both builds are portable. Each release includes a `SHA256SUMS.txt` to verify your download.

### Linux runtime files

Linux uses per-user XDG locations: config in `$XDG_CONFIG_HOME/distill` (default `~/.config/distill`),
installed tools, recomps and the Watch inbox in `$XDG_DATA_HOME/distill` (default `~/.local/share/distill`),
state in `$XDG_STATE_HOME/distill` (default `~/.local/state/distill`), and caches in
`$XDG_CACHE_HOME/distill` (default `~/.cache/distill`), including `game_media` artwork and achievements. Empty or non-absolute overrides use the defaults.
Bundled assets stay with the application. This path layout does not imply complete Linux support.

Set `DISTILL_PORTABLE=1` to keep runtime files beside the source launcher or frozen executable instead.
An unwritable portable directory reports the existing write error; it never selects another location.
At normal startup, missing XDG files are copied from legacy settings, emulator/recomp catalogs, forks,
yt-dlp/torrent/soundtrack preferences, soundtrack saves, and known Organize undo histories. Existing XDG
content wins and originals are preserved. Credentials are handled separately by the unified store described
below. Rules (which may contain passwords), queue sessions and torrent directories, logs, caches, tools/recomps trees and user libraries
are **not automatically migrated**; deferred files stay beside the app and remain usable in portable mode.

**Updates:** Distill checks GitHub for a newer release when it starts and asks whether to install it. You can update in one click, skip a version, or ignore it. Your settings and tools are kept.

## Features

### Games

- **PC Recomps** is a poster grid of native PC ports of console games (Ship of Harkinian, Zelda 64: Recompiled, Starship and more). Install, update and play with one click from each project's own GitHub release, checked against the published checksum. Updates keep your saves. *Check my library* shows which games you already have the dump for. Nothing is ever downloaded or converted for you. **Add recomp...** puts a project that isn't on any list yet on the page: give its GitHub address and Distill fills in what it can, then you say what game it plays and what kind of project it is. It installs and updates like the rest, and you can edit or remove it later.
- **Emulators** is a catalog of about 36 emulators with install, update and launch.
- **Edit game** has three tabs. *Details* is as before. *Media* has a row for the cover, background, disc or cartridge, screenshots and manual. Every row has the same shape: the picture in a box of one size (click it to see it larger; with screenshots, to step through them), then a text box of one length saying what the game has, with four symbol buttons: a magnifier (look one up online: LaunchBox, SteamGridDB), an open folder (add a file from your computer), a bin (take it away) and a wand (go back to automatic). Hover any symbol for its words. Pictures you chose are kept when the game is renamed, and *Find missing pictures* fetches what the game lacks with a bar showing which is being grabbed. *Emulator* chooses what plays the game (or Automatic), can make that the default for the platform, and holds the launch options of an emulator of its own. Nothing changes until Save is pressed, and the game's page no longer carries emulator controls.
- **Fetch pictures...** (in the Games page's menu, and in the tools of a selection) looks online for the backgrounds, discs or cartridges and screenshots many games lack, for the whole library, the selected games or one platform. A bar and two lines show which game and which picture is being fetched, and how many were found; Stop is prompt. Pictures you chose or turned off are never touched, and ones not found are not looked for again for a week unless you tick *Look again*. Covers are still fetched as you scroll.
- **Loading is visible.** Where a picture is on its way, a light band sweeps the empty space (poster cards in the grid, the screenshot strip, previews in the Edit window's Media tab) and the picture fades in when it arrives, instead of appearing from nowhere. A game's page shows a slim line saying what is being looked for.
- **Library health...** (Games menu) is a check-up: games whose file has gone, games stored twice, games with no platform, cover or details, and pictures lacking. Each problem has a button that selects the games it is about, so the tools bar can deal with them (or opens Fetch pictures).
- **Saves.** The archive button on a game's page opens its backups: *Back up now* copies the game's RetroArch save states and battery saves, *Restore* puts them back where RetroArch keeps them (what they replace is backed up first, so a restore can be undone). *Back up all saves...* in the Games menu does every RetroArch game at once, e.g. before updating RetroArch or its cores. The newest 20 backups of each game are kept. Emulators of their own keep saves in their own ways and aren't covered.
- Fetch pictures now includes **covers**, and a game's page has a **Stop** button on its picture-fetching line.
- **Manuals** open as a book. On a game's page, *Add manual...* keeps a PDF with the game and *Manual* reads it: the cover stands alone, pages sit in spreads with their thickness showing on each side, and a page is picked up by its outer edge or corner and turned (let go past a third of the way, or flick, and it finishes; click the edge, press the arrow keys or use the wheel to turn it for you). Right-click the button to choose another PDF or remove it. Where you got to is remembered for each game, and *Right to left* mirrors the book for manuals read from the back.
- **Manuals** open as a book. On a game's page, *Add manual...* keeps a PDF with the game and *Manual* reads it: the cover stands alone, pages sit in spreads with their thickness showing on each side, and a page is picked up by its outer edge or corner and turned (let go past a third of the way, or flick, and it finishes; click the edge, press the arrow keys or use the wheel to turn it for you). Right-click the button to choose another PDF or remove it. Where you got to is remembered for each game, and *Right to left* mirrors the book for manuals read from the back.
- **Games library > Libraries > Import / export > Export RetroArch playlists** writes a playlist for each platform, your favourites and each collection into RetroArch's playlist folder. A playlist RetroArch already has keeps everything in it and only gains the games it was missing.
- **Games library > Libraries > Import / export > Export to Sunshine (Moonlight)** adds your favourites (or the whole library) and your installed PC recomps to Sunshine's app list with their posters, so Moonlight shows them as a grid on a TV, phone or Steam Deck. Each app runs `Distill --launch game:<id>` (or `recomp:<id>`): no window, it starts the game with the emulator, core and launch options set in Distill, stays until it quits, and counts the play time. Add `--resume` to continue a RetroArch game from its newest save state. Entries already in `apps.json` are left alone, nothing is removed, and the file is backed up first. A failed launch is written to `launch.log` beside Distill's data.
- **Games library > Libraries > Import / export > Import history** brings your favourites, play time, stars, progress and collections across from a LaunchBox folder or a Playnite JSON export. Only games already in your library are updated, nothing you set in Distill is overwritten, and importing twice changes nothing.

### Downloads

- **Files & Torrents** is a download queue for direct links, archive.org, torrents, debrid services (including TorBox) and video or audio sites through yt-dlp. It has speed limits, schedules and per-site limits, resumes partial files, and has a right-click menu for starting, stopping and deleting.
  - **Save to** can be left empty: downloads then go to your Downloads folder, which the box shows as its placeholder.
  - **Scan a page for links:** paste `scan:https://example.com/page` (or `scan[zip,7z]:https://...` to pick file types) and every download link on that page is listed: files, magnets and links to Drive, MediaFire, Dropbox, OneDrive, Pixeldrain, 1fichier and archive.org. Pixeldrain lists are expanded into their files.
  - **Watch folder:** switch it on in Settings > Downloads, then drop a `.txt`, `.torrent`, `.magnet` or `.url` file into `Watch` under the Linux XDG data directory, or beside Distill on Windows and in portable mode. Its links are listed; taken files move into `Watch/queued`.
  - **Unpacking:** multi-part sets (`.part1.rar`, `.7z.001`, `.rar` + `.r00`, `.zip` + `.z01`) are unpacked from the first part and, with "then delete them", every part goes afterwards. Password-protected archives are tried with each line of `distill_archive_passwords.txt` (next to Distill), then with ones worked out from the site the file came from (`www.site.com`, `site.com`, `site`), then with their own name.
  - **FTP:** paste an `ftp://` or `ftps://` address (`ftp://user:password@host/path` if it needs a login, or save one under Settings > API keys > Downloads). A folder is listed with everything under it; downloads resume. FTP does not use the proxy rules.
  - **Site headers:** under Settings > Downloads > per-site rules, a site can have a Referer (`auto` = its own front page), a User-Agent and extra `Name: value` headers. They are sent only to that site, never on a redirect to another one.
  - **When finished:** run a command of your own (`"C:\Tools\sync.bat" {folder}`; also `{list}`, `{count}`, `{failed}`), then optionally sleep, hibernate or shut down after a 60-second countdown you can cancel. Nothing happens if you stopped the downloads yourself, and the power option resets to Nothing each time Distill starts.
- **Distill Resonance** is a music player for game soundtracks. Search a game and its albums are laid out with the cover on the left and the tracks on the right. Tracks stream from KHInsider (archive.org is searched too); the **Home** tab shows what is new and popular, **Browse** goes through KHInsider by platform, year or letter, keep albums in your **Library**, like tracks into **Playlists** (export one as an .m3u8), and line tracks up in the **Queue**. KHInsider sits behind a Cloudflare check that plain Python cannot pass, so install the optional `curl_cffi` package (`python -m pip install curl_cffi`); without it only archive.org is used. Albums from KHInsider carry an orange KHInsider chip as credit. Settings has the disk space each cache may take, a backup and restore for your library, and a check that KHInsider is answering. Keys: Ctrl+Space plays or pauses, Ctrl+Left and Ctrl+Right skip, Ctrl+F searches, and the media keys work. Saving uses KHInsider (FLAC when available) and falls back to archive.org. It can fill in missing tags and cover art.

### Convert & compress

Batch conversion with verification. Leave *Destination* blank and results are filed into a folder for each platform.

| Page | Does |
| --- | --- |
| **CHD** | CD and DVD images (PS1, PS2, Saturn, Dreamcast, CD-i and more) to CHD, with chdman |
| **CSO** | PSP ISOs to CSO, with maxcso |
| **RVZ** | GameCube and Wii discs to RVZ, with DolphinTool |
| **PS3** | Decrypt PS3 ISOs; keep as ISO, unpack to folders, or both |
| **PS4 PKG** | Unpack PS4 `.pkg` files with each game's updates merged in |
| **PS5 PKG** | Unpack PS5 `.pkg` files, then relink for AnyPS5 or pack for KytyPS5 |
| **Switch** | Compress, decompress and merge NSZ/XCZ; pack Switch 2 dumps into NUP |
| **Xbox** | Repack Xbox and Xbox 360 discs into verified `.xiso`, with xdvdfs |
| **ZAR** | Compress PS4, PS5 and Xbox 360 games and updates into `.zar` archives |

### Organise library

Work through the pages in order. Every change can be previewed first, and every run can be undone.

1. **Fix Names & Discs.** Tidy filenames, group multi-disc games, repair playlists and repair disc images.
2. **Remove Extras / Junk.** Remove leftovers, duplicates (by name, title, or identical contents) and extra releases (one game, one ROM). Nothing is deleted until you select it and confirm.
3. **Patch ROMs.** Apply patches to ROMs.
4. **Sort by System.** Put each game in a folder for its console, read from the file itself.
5. **Library Report.** Verify against DAT files, check BIOS files, and export a searchable web page or spreadsheet. It changes nothing.

### Interface

- Dark and light themes (click the Distill logo)
- `Ctrl+K` to jump to any page, `Ctrl+1` to `Ctrl+9` for the first nine pinned tools, `Ctrl+,` for Settings
- **Compact sidebar** (`Ctrl+B`, the button beside the logo, or Settings > Sidebar): the sidebar tucks away to the left and the page uses the whole window. Move the mouse to the left edge and it eases out over the page; move away and it eases back
- **Pin and unpin tools** under Settings > Sidebar to keep only the ones you use in the sidebar. Unpinned tools stay available from `Ctrl+K`
- **Runs don't lock the window.** Start a conversion or download, then go anywhere in the app. A bar along the bottom shows what is running and its percentage; click it to return to that page. Conversions and other tool jobs go one at a time, but downloads (games, soundtracks) run on their own, so one can go while a conversion does; each page's Start, Pause and Stop control that page's own run. Organise still waits for either to finish, since it moves files.
- Searchable, copyable log with coloured errors, warnings and verified lines
- Window layout, folders and options remembered between runs
- Taskbar progress and a notification when a long run finishes
- Reduce Motion option in Settings

## Running from source

Requires Python 3.10 or newer (3.12 or 3.13 if you want torrent support, since libtorrent has no wheel for the newest versions).

```
pip install -r requirements.txt
python -m distill
```

On Windows you can also double-click `Distill.pyw`.

### Optional keys

Distill works without any keys. They only raise rate limits, add artwork or sign you in to a file host. Everything lives in **Settings > API keys**, in folders so you only open what you use:

| Folder | Holds | Used for |
| --- | --- | --- |
| Games & artwork | SteamGridDB, RetroAchievements | Posters, covers and icons for Games, PC Recomps and Emulators; a game's achievements |
| Downloads & file hosts | archive.org, 1fichier, TorBox and other debrid services, Prowlarr, any other site | Sign-ins for the Files & Torrents page, cached torrents, more torrent search |
| Updates | GitHub | A read-only token raises GitHub's limit from 60 to 5,000 requests an hour (tool, emulator and recomp update checks) |

Paste a key there and it is saved and used straight away, in a downloaded build too. Keys and remembered logins share **one file, `distill_keys.json`**: in `$XDG_CONFIG_HOME/distill` (default `~/.config/distill`) on Linux, or next to the app on Windows and with `DISTILL_PORTABLE=1`. It is encrypted for your Windows account, or with the system keyring where available; otherwise it uses plain text with owner-only permissions. An encrypted file moved to another computer or account may be unreadable.

On its first read, the store folds in the older `distill_apikeys.json` and `distill_logins.json` from the same directory; existing unified entries win, and custom logins merge by host. On Linux it also imports app-folder credentials (including an older `distill_keys.json`), preserving those originals. Migration is finalized once inside the sealed store after durable publication and verification; only verified, imported same-directory files can then be removed. Unreadable legacy files are left untouched, reported, and never imported later. An unreadable unified store blocks fallback imports; the current unified file is never replaced while unreadable. Finalization survives forgetting every credential, so retained files cannot restore forgotten sign-ins. Failed publication retains every original and retries only on the next start. The general startup path migration does not copy credential files.

Credential updates and migration share an owner-only sibling lock file that serializes individual storage transactions. Multiple Distill instances or other writers editing the same credentials are not synchronized; the last save wins. Lock acquisition has a bounded wait; failure leaves stored credentials untouched. Only regular, non-symlink credential files up to 1 MiB are read; rejected files remain untouched.

Otherwise set `GITHUB_TOKEN` / `SGDB_API_KEY` as environment variables, or copy `distill/local_keys.example.py` to `distill/local_keys.py` (git-ignored). A key saved in Settings is used first. Never put a key you care about in a build you share, since it can be read back out of the `.exe`.

## Development

**Tests**

```
python -m unittest discover -s tests -t .
```

Engine tests run against stand-in command-line tools, so no chdman or DolphinTool is needed. Window tests need PySide6 and are skipped with `DISTILL_SKIP_UI_TESTS=1`.

**Build a Windows exe**

```
build_exe.bat              # dist\Distill\Distill.exe + dist\Distill-portable.zip
build_exe.bat onefile      # dist\Distill.exe
```

The build runs the tests, then starts the finished program with `--self-test` to prove it launches.

**Releases**

Push a version tag and GitHub Actions tests, builds both variants, and publishes them with checksums. The app version is set from the tag automatically.

```
git tag v1.0.1 && git push origin v1.0.1
```

**Layout**

```
distill/
  batch.py, formats.py, system.py     conversion engine and disc / PKG readers
  organize.py, library.py, systems.py organiser tools
  downloads.py, torrents.py, torbox.py, ytdlp.py   downloader
  crawl.py, hosters.py, folderwatch.py, postprocess.py   page scanner, file hosts, watch folder, unpacking
  ftpclient.py, afteractions.py       FTP / FTPS, and the command / sleep / shutdown after a run
  recomps.py, emulators.py            PC Recomps and Emulators catalogs
  launcher.py, streaming.py           `--launch` (no window) and the Sunshine / Moonlight export
  soundtracks.py                      Distill Resonance
  switchconv.py                       Switch NSZ / NUP
  tools.py                            auto-install and update of command-line tools
  updater.py                          app self-update from GitHub releases
  ui/                                 everything that imports Qt
tests/                                unittest suite
```

Rules the tests enforce: nothing outside `ui/` and `app.py` may import Qt, and modules never import each other in a circle.

## Linux

Run `python Distill.pyw` with Python 3.10+ and the dependencies in `requirements.txt` (including PySide6).
Linux uses the XDG config, data, state and cache directories under `distill/`; downloaded tools live in
`$XDG_DATA_HOME/distill/tools` (default `~/.local/share/distill/tools`). `DISTILL_PORTABLE=1` uses the app folder.
Native tool downloads, Flatpak emulators, Project64 through Wine and an ares source-build action are supported.
See [LINUX-BUILD.md](LINUX-BUILD.md) for packaging, runtime paths and tool requirements.

**AppImage (x86_64):** build it with `bash packaging/build-in-docker.sh` (or take the `Distill-x86_64.AppImage` artifact from the
*Linux AppImage* workflow), then `chmod +x Distill-x86_64.AppImage && ./Distill-x86_64.AppImage`. Verify it with the
`Distill-x86_64.AppImage.sha256` file next to it. On systems without FUSE run it with `APPIMAGE_EXTRACT_AND_RUN=1`.
Wayland is used when available. Flatpak and Wine are only needed for the emulators that use them.

## Credits

Distill drives these tools and does not reimplement them: chdman (MAME), DolphinTool, maxcso, nscb_rust, nup, hactoolnet, xdvdfs, ZArchive, yt-dlp and ffmpeg. Fonts are Inter, Outfit and JetBrains Mono, used under the SIL Open Font License. Linux support (native tool downloads, Flatpak/Wine/source-build emulator installs and the AppImage packaging) builds on a Linux port by Niko.
