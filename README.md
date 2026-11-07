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
installed tools and recomps in `$XDG_DATA_HOME/distill` (default `~/.local/share/distill`),
state in `$XDG_STATE_HOME/distill` (default `~/.local/state/distill`), and caches in
`$XDG_CACHE_HOME/distill` (default `~/.cache/distill`). Empty or non-absolute overrides use the defaults.
Bundled assets stay with the application. This path layout does not imply complete Linux support.

Set `DISTILL_PORTABLE=1` to keep runtime files beside the source launcher or frozen executable instead.
An unwritable portable directory reports the existing write error; it never selects another location.
At normal startup, missing XDG files are copied from legacy settings, emulator/recomp catalogs, forks,
yt-dlp/torrent/soundtrack preferences, soundtrack saves, and known Organize undo histories. Existing XDG
content wins and originals are preserved. Login-data files (API keys and logins), rules (which may contain
passwords), queue sessions and torrent directories, logs, caches, tools/recomps trees and user libraries
are **not automatically migrated**; deferred files stay beside the app and remain usable in portable mode.

**Updates:** Distill checks GitHub for a newer release when it starts and asks whether to install it. You can update in one click, skip a version, or ignore it. Your settings and tools are kept.

## Features

### Games

- **PC Recomps** is a poster grid of native PC ports of console games (Ship of Harkinian, Zelda 64: Recompiled, Starship and more). Install, update and play with one click from each project's own GitHub release, checked against the published checksum. Updates keep your saves. *Check my library* shows which games you already have the dump for. Nothing is ever downloaded or converted for you.
- **Emulators** is a catalog of about 36 emulators with install, update and launch.

### Downloads

- **Files & Torrents** is a download queue for direct links, archive.org, torrents, debrid services (including TorBox) and video or audio sites through yt-dlp. It has speed limits, schedules and per-site limits, resumes partial files, and has a right-click menu for starting, stopping and deleting.
  - **Scan a page for links:** paste `scan:https://example.com/page` (or `scan[zip,7z]:https://...` to pick file types) and every download link on that page is listed: files, magnets and links to Drive, MediaFire, Dropbox, OneDrive, Pixeldrain, 1fichier and archive.org. Pixeldrain lists are expanded into their files.
  - **Watch folder:** switch it on in Settings > Downloads, then drop a `.txt`, `.torrent`, `.magnet` or `.url` file into the `Watch` folder beside Distill and its links are listed. Taken files move into `Watch/queued`.
  - **Unpacking:** multi-part sets (`.part1.rar`, `.7z.001`, `.rar` + `.r00`, `.zip` + `.z01`) are unpacked from the first part and, with "then delete them", every part goes afterwards. Password-protected archives are tried with each line of `distill_archive_passwords.txt` (next to Distill), then with ones worked out from the site the file came from (`www.site.com`, `site.com`, `site`), then with their own name.
- **Soundtrack Grabber** takes a list of game names (or reads them from your library) and finds each game's soundtrack on archive.org. It scores the matches, previews tracks, and can fill in missing tags and cover art.

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
2. **Remove Extras / Junk.** Remove leftovers, duplicates (by name, title, or identical contents) and extra releases (one game, one ROM). Nothing is deleted until you tick it and confirm.
3. **Patch ROMs.** Apply patches to ROMs.
4. **Sort by System.** Put each game in a folder for its console, read from the file itself.
5. **Library Report.** Verify against DAT files, check BIOS files, and export a searchable web page or spreadsheet. It changes nothing.

### Interface

- Dark and light themes (click the Distill logo)
- `Ctrl+K` to jump to any page, `Ctrl+1` to `Ctrl+9` for the first nine, `Ctrl+,` for Settings
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

Distill works without any keys. These only raise rate limits or add artwork:

| Key | Used for |
| --- | --- |
| `GITHUB_TOKEN` | Read-only token. Raises GitHub's limit from 60 to 5,000 requests an hour (PC Recomps, tool updates) |
| `SGDB_API_KEY` | steamgriddb.com key for portrait posters in PC Recomps |

The easiest way is **Settings > General > API keys**: paste a key there and it is saved (encrypted for your Windows account) and used straight away, in a downloaded build too. Otherwise set them as environment variables, or copy `distill/local_keys.example.py` to `distill/local_keys.py` (git-ignored). Never put a key you care about in a build you share, since it can be read back out of the `.exe`.

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
  recomps.py, emulators.py            PC Recomps and Emulators catalogs
  soundtracks.py                      Soundtrack Grabber
  switchconv.py                       Switch NSZ / NUP
  tools.py                            auto-install and update of command-line tools
  updater.py                          app self-update from GitHub releases
  ui/                                 everything that imports Qt
tests/                                unittest suite
```

Rules the tests enforce: nothing outside `ui/` and `app.py` may import Qt, and modules never import each other in a circle.

## Credits

Distill drives these tools and does not reimplement them: chdman (MAME), DolphinTool, maxcso, nscb_rust, nup, hactoolnet, xdvdfs, ZArchive, yt-dlp and ffmpeg. Fonts are Inter, Outfit and JetBrains Mono, used under the SIL Open Font License.
