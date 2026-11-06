# Distill

Batch disc-image compressor, extractor and organiser (CHD, RVZ, CSO, PS4/PS5 PKG, PS3, Xbox, ZAR), with an
archive.org / torrent / TorBox downloader, soundtrack grabber and library clean-up tools. PySide6 with a dark, custom
look (Fusion style + one style sheet, `ui/theme.py`), bundled fonts (Inter, Outfit, JetBrains Mono) and line icons drawn from SVG paths (`ui/icons.py`).

## Run

    python Distill.pyw          # double-click on Windows; installs PySide6 and py7zr on first start
    python -m distill           # same, from a terminal

## Layout

    distill/
      constants.py  models.py  system.py  formats.py   shared definitions, disk/process helpers, disc and PKG readers
      batch.py                                          the conversion engine (BatchRunner)
      organize.py  systems.py  library.py                multi-disc, leftovers, duplicates, playlist repair, tidy names,
                                                        sort by system, library report
      downloads.py  links.py  torrents.py  torbox.py  torrent_runners.py
      recomps.py                                        PC Recomps: catalog, GitHub releases, install / update / launch, library match, covers
      soundtracks.py                                    game name -> soundtrack album -> save plan (Standard / RomM) -> file list; undo records
      soundtrack_tags.py                                filling in missing audio tags and cover art (needs mutagen)
      dlfiles.py                                        locating / revealing / deleting what a download left on disk
      fingerprint.py                                    finding identical files by size, then checksum (CHD: from its header)
      switchconv.py                                     Switch: NSZ / XCZ / merge (nscb_rust) and NUP packing (nup, hactoolnet)
      tools.py                                          auto-install / update of chdman, DolphinTool, extractors ...
      orgpages.py                                       the Organize pages: which tools exist, how a page scans its steps
      crashlog.py                                       unhandled errors -> distill_crash.txt (no console in a .pyw)
      selftest.py                                       what `Distill.exe --self-test` checks without Qt (CI runs it on the built exe)
      bootstrap.py  app.py  __main__.py
      ui/                                               everything that imports Qt
    tests/                                              unittest suite (pytest runs it too)

Rules the tests enforce: nothing outside `ui/` and `app.py` may import Qt, and modules never import each other in a circle.

## PC Recomps

The **PC Recomps** page (under GAMES) lists native PC ports of console games (Ship of Harkinian, Zelda 64: Recompiled, Starship,
Dusklight and more) as a poster grid. Click one for its page: description, links, setup note, the one-line description the developers gave the repository (cached for a day) and the latest release's notes, collapsed until you open them.

- **Install / Update / Play:** one click downloads the project's own GitHub release for your OS, checks it against the checksum
  GitHub publishes, unpacks it under `recomps/games/<project>/app` and finds the program to run. An update is unpacked *over* the
  old copy, so saves and settings the game keeps in its folder survive. Uninstall deletes the folder (it asks first).
- **Updates:** the page asks GitHub about every installed project when it opens (answers are kept for an hour; a rate-limited or
  offline GitHub falls back to the last answer). Set `GITHUB_TOKEN` for a higher limit.
- **Only games I have:** run the **Library Report** (Organise Library), then **Check my library**. Every project says which dump it
  needs. A file for the right console whose name matches shows *ROM?*; when the catalog lists checksums the file is hashed and a hit
  shows *ROM ✓* (N64 `.v64`/`.n64` dumps are byte-swapped first, so any order matches). Files are only read. Nothing here ships, copies
  or converts a game, and no game or asset is ever downloaded.
- **Covers:** portrait posters. With a free SteamGridDB API key (Sources... > key; get one at steamgriddb.com > Preferences > API) they
  come from SteamGridDB at its portrait sizes (600x900, 342x482, 660x930), best-scored first. Without a key, or when SteamGridDB has none,
  from LaunchBox (Poster, else Box - Front), then the catalog entry's own `cover`, then a plain placeholder. In every source the result
  must be that game (its name contains every word of the title), the plainest title wins, and a portrait picture beats a landscape one.
  Adding a key upgrades posters already cached from LaunchBox. Kept in `recomps/posters-v3`; no poster found is not searched again for
  a week. Set `launchbox_id` on an entry to pin the right LaunchBox game.
- **Built-in community list:** the [awesome-decomp-games](https://submitgame.github.io/awesome-decomp-games/data/games.json) list is
  part of the app (not a source you manage) and is re-read at most every 6 hours, so the catalog follows it. Its static recomps, source
  ports, engine remakes and decompilations are used when they're on GitHub, not archived, and have published a release; browser ports,
  disassemblies and tools are left out. "Majora's Mask (Zelda64Recomp)" becomes the game plus the project's name, and the list's curator
  note is the description.
- **Curation** (Sources... > What to show from the lists; default on): keep what is *in progress* (a commit in the last 365 days and 25+
  stars) or *popular* (1,000+ stars; tagged *Inactive* if development has stopped), then keep only the best project per game and console
  (the list's own score, then stars, then recency). The others are named under "Other projects for this game" on the winner's page. The
  built-in 8 are always kept. Lists with no activity data (Quiver lists, your own file) are not filtered. "Everything in the lists" turns
  it all off. The thresholds are `ACTIVE_DAYS`, `ACTIVE_MIN_STARS` and `POPULAR_STARS` in `distill/recomps.py`.
  The wheel glides (70 px per notch, eased, notches add up mid-glide; touchpads and Reduce Motion keep the normal behaviour).
  Posters load only for what is on screen, three at a time, so a long list doesn't hammer SteamGridDB or LaunchBox.
- **More projects:** the **Sources...** button adds extra lists, as a web address or a file. Quiver Launcher's list format (`apps` with
  `repository`, `tags`, `releaseAssetFilter`), awesome-decomp-games' (`games`) and this app's own format all work, and an index that points at other lists is followed
  one level. Lists are cached for 6 hours and fall back to the last copy offline. The console is read from the tags (`n64`, `gamecube` ...);
  an entry with none is matched to your library by game name alone. Entries without a GitHub repository are skipped. Projects already in
  the built-in list are never replaced by a source. A checkbox also searches GitHub's recomp topics for new projects; those are marked
  *Unverified* because some are tools rather than games.
- **Emulators:** the Games > Emulators page lists every emulator in `distill/assets/emulators.json` by console maker. Point it at your
  Emulators folder once: emulators already there (or in the usual places) are found and linked, the rest install into that folder, and
  updates are merged over the existing files so saves and settings stay. Linked installs are never deleted. Add your own entries in
  `distill_emulators_catalog.json` beside the app (same format). Names and platforms are cross-referenced with
  [detain/emurelation](https://github.com/detain/emurelation) when it can be reached.
  The list shows compact **Tiles** by default (status, system, and Launch / Update buttons); the **Tiles | Cards** switch beside the
  filter changes it to larger cards, and the choice is remembered.
  Each emulator's page has a **Forks** card: press *Add fork...* and paste a GitHub address (the repository doesn't have to be a GitHub fork).
  Distill looks at it (a download for this PC, stars, age, last push) before anything is saved, installs it into a folder of its own and
  downloads only from the owner's account. An author Distill doesn't know needs a tick that you've checked them, and such a fork is never
  touched by *Update all*. Forks are kept in `distill_forks.json` beside the app.
- **Adding projects:** the catalog is `distill/assets/recomps.json`. Put your own in `distill_recomps_catalog.json` beside the app
  (same format; the same `id` replaces an entry). `python -m distill.recomps hash <rom>` prints the `sha1` / `crc32` lines to paste in.
  `asset` and `exe` regexes fix a wrong automatic pick of the release file or the program.

The engine is `distill/recomps.py` (no Qt); the page is `distill/ui/recomp_page.py`.

## Switch NSZ / NUP

The **Switch NSZ / NUP** page (under CONVERT & COMPRESS) has a **Switch 1 | Switch 2** switch, like Tiles | Cards on Emulators.

- **Switch 1** uses [nscb_rust](https://github.com/cxfcxf/nscb_rust) (a Rust port of NSC_BUILDER's core; downloaded and updated
  automatically): *Compress* `.nsp`/`.xci` to `.nsz`/`.xcz`, *Decompress* them back, or *Merge* a game, its updates and its DLC into one
  `.nsp`/`.xci` (optionally compressed in the same run). Files are grouped by the `[Title ID]` in their names; tick *Name files from their
  contents first* to add it. Results are built in a scratch folder and moved into place, so a stopped run leaves no half files.
- **Switch 2** uses [nup](https://github.com/a55uka/nup) (the NX Package format; fetched as the source of one exact commit and run with
  Python, with any module it needs installed into its own folder). *Pack to NUP* takes decrypted dump folders (`exefs/` + `romfs/`, or
  `base/ updates/ dlc/`) and makes one `.nup` each. Switch 1 `.nsp`/`.xci`/`.nsz`/`.xcz` files are decrypted first with
  [hactoolnet](https://github.com/Thealexbarney/LibHac) (needs the .NET runtime), and the game's own name, publisher, version and icon are
  read from its Control data. *Artwork*: the game's own, a picture named like the game in an artwork folder (`Game.png/.jpg`), or none.
  *Add artwork to existing NUPs* runs `nup edit`. Switch 2 games can't be decrypted on a PC: dump them on the console and point at that
  folder (no keys are needed to pack a dump).
- **Keys:** Settings > Tools > Switch keys holds `prod.keys` (required for Switch 1 and for decrypting) and `title.keys` (optional; the
  tickets inside files are used automatically). They are found in `~/.switch` or beside the app. Distill only remembers the paths.

## Download list: right-click menu

Right-click files in the Files & Torrents list (or press Delete) for: Open folder, Start, Stop, Tick / Untick, Copy name(s) /
link(s), Delete..., and "Remove N finished from the list".

- **Start / Stop** act on just the selected files while the rest of the run carries on. A stopped file keeps its partial
  download, so Start resumes it. Web files start straight away; files for a torrent run (or a run that just ended) are
  lined up and start when it finishes.
- **Delete...** asks whether to only remove the files from the list, or to delete them from disk as well (partial downloads
  included, permanently). Files still downloading are stopped first.

## Soundtracks

The **Soundtrack Grabber** page (under DOWNLOADS) takes game names, one per line, and searches for each game's soundtrack. Type them,
drop a text file or a folder of games on the box, or press **Games directory...** to read the names from your library (game
folders *and* loose game files, one platform folder deep). `Name = other name` searches under a different name while the
folder keeps `Name`; `Name = link` uses an archive.org item or file listing you already have; `Name = other name = link` does both.

**Finding the right album.** Each game gets two archive.org searches (the name as an exact phrase, then all its words in
any order; 50 results each), merged and scored. Matching ignores capitalisation and uses whole words, and a title that holds
the name as one unbroken phrase beats one that merely contains the words (*Super Mario 64* ranks above *Mario Kart 64*, which
is left unticked). Albums split into separate tracks are favoured over single-file ones, since those can be expanded and
previewed. Results are remembered for three days; **Shift-click Search** to look again. The *Match* column shows the score,
games that need a decision are dimmed, and covers appear as thumbnails.

**Saving.** Right-click a game (or a track) for Save, Save to another folder, Open save folder, Match to game... and Remove;
Delete removes the selected rows and Enter saves them. The Start button on that page (**Save Selected Soundtracks**) saves every ticked game; open a game to tick or untick its individual tracks. The *RomM support* switch
picks the layout:

- **Off (Standard):** `<Save to>/Soundtracks/<Game>/01 - Title.flac`. A blank Save to means your Downloads folder.
- **RomM:** `<Games directory>/<platform>/<Game>/soundtrack/01 - Title.flac`, beside the game, as RomM expects. A game that is
  a loose file is first moved, with its `.bin`/`.cue` companions, into a folder named like it. A preview asks you to confirm;
  no match, several matches or a folder already in the way means that game is skipped, never guessed (right-click it and
  use *Match to game...* to choose by hand).

Files are never overwritten. When the run ends you get a summary (saved / skipped / failed) and **Retry failed**. Missing
tags (title, album, artist, track and disc numbers, cover) are filled in and a `cover.jpg` is saved beside the tracks; existing
tags are never overwritten, and `mutagen` is needed for the tags (not the cover file). **More > Undo last save** deletes only
the files that save created, removes folders it left empty and puts moved games back; the move log is one click away.

Multi-disc albums keep a `CD1/`, `CD2/` subfolder; *Best (FLAC first)* takes FLAC over MP3 when both exist. Click a track to
preview it (it streams from the source; click it again to stop). A small player appears at the bottom of the sidebar with
previous / play-pause / next, a seek bar and stop, and keeps playing as you move between tools. Click its cover for a larger
view, or its title to jump back to the Soundtrack Grabber page. Previews can't use your saved logins, so private items won't stream.
Sources are pluggable (`soundtracks.Provider`); archive.org and pasted links ship today.

## Interface

- **Look:** a dark theme and a light one, switched by clicking the Distill logo (`ui/theme.py` holds every colour). Fonts and icons live in `distill/assets/`. Type: Inter for
  everything you read and press, Outfit for page titles, dialog heads and big numbers, JetBrains Mono for logs and link boxes;
  nothing is set smaller than 9pt. All three are used under the SIL Open Font License (`assets/fonts/OFL*.txt`). The Start button's gradient drifts and glows
  while a batch runs, holds still when paused (Settings > General > Reduce Motion turns every animation off). The window keeps its normal
  title bar, tinted dark on Windows 10/11. Sidebar names, page titles and blurbs are in `ui/nav.py`; each page's own
  description stays one hover away on its blurb.

- **Remembered between runs:** window size and position, the last page, folded cards, every page's folders and options, and the
  soundtrack list's column widths (`distill_settings.json` next to the app).
- **Go anywhere:** Ctrl+K opens a "go to" box; Ctrl+1 to Ctrl+9 open the first nine pages. Ctrl+, opens Settings (which has a
  search box).
- **Notices** that only need a glance appear in a strip under the page title instead of a pop-up. The title bar shows progress
  while a run is going, and a run that ends while the window is in the background flashes the taskbar and shows a notification
  (switch it off in Settings > General).
- **Log:** search it, copy it or save it; errors are red, warnings amber, verified lines green.
- **Sliding switches** replace drop-downs with a few named levels (compression, I/O mode, thread budget, parallel downloads).
- Greyed-out controls say why in their tooltip. Drop a folder onto any folder box to fill it in.

## Organise pages

The Organise group has four pages, in the order you'd use them:

1. **Fix Names & Discs** - *Tidy filenames*, *Multi-disc games* and *Playlist repair* as three steps. Press Scan and the first
   step with something to do is shown; apply it and the next scan moves on to the following step, so steps never act on the
   same files at once. Untick a step to leave it out.
2. **Remove extras** - *Clean up leftovers* and *Duplicates* as two steps, sharing one *delete permanently* option.
3. **Sort by system** - after the steps above.
4. **Library report** - changes nothing.

*Undo last run* on a page reverses whichever of its steps ran last. Each step still keeps its own undo history and log entries.

## Organise: finding matches that aren't spelled identically

- **Multi-disc games** treats free text right after the disc marker as that disc's own label, so
  `Resident Evil 2 (Disc 1)(Leon)` and `(Disc 2)(Claire)` are one game. Region / revision tags (`(USA)`, `(Rev 1)`) still
  keep releases apart. *Loose matching* also pairs discs with merely similar names (same folder, no disc number in common,
  same region tags, matching numerals - so Final Fantasy VII is never paired with VIII); those sets are left unticked.
- **Duplicates** can match by *Same name* (as before), *Same title, ignoring region / version tags*, or *Same contents*
  (size, then a start/end fingerprint, then a full SHA-1; CHDs are compared from their header, so the same disc compressed
  differently still matches). *Also look at ROMs and other game files* adds .gba / .nes / .exe / .zip and similar; those
  name matches are left unticked. Contents matches are ticked: they are proven identical.

## Converting without a Destination

Leave Destination blank and each converted title is filed in a folder named for its platform inside the source folder
(`PlayStation 2`, `Sega Saturn`, `Dreamcast`, `GameCube` ...), told from the file itself (disc headers, PKG title ids, ROM
extensions). Archives are judged by the discs inside them. When the platform can't be told, or the title already sits under a
folder named for it, the result is saved next to its source as before. A Destination, or unticking *file results in a folder
for each platform* on a page, restores the old behaviour. Re-running finds earlier results in those platform folders.

## Organise: sorting, reports and history

- **Sort by system** puts each game in a folder for its console (`PlayStation 2`, `Dreamcast`, `Game Boy Advance` ...). The
  system is read from the file itself (disc headers, `.rvz` / `.wia` headers, ROM extensions); a folder named `PS2`,
  `Dreamcast` etc. fills in for CHDs and archives, which can't be read cheaply. *Sort into* blank means a folder per system
  inside the scanned folder. Disc sets (`.cue` + tracks) and playlists move together and a game's own folder is kept. Files
  it can't place are left alone (or go to `Unknown system`, your choice).
- **Library report** lists every game (system, format, region, size, folder) and saves it as a searchable web page or a
  spreadsheet. Untick a system to leave it out.
- **Undo last run** now steps back through the last 20 runs of each tool, and every run is also written to
  `distill_organise_log.txt` next to the app. **Save preview...** on each page saves what a scan found, and what applying it
  would do to each file, as text or a spreadsheet before anything changes.
- **Tidy filenames** can rename to a pattern: `{title} ({region}) {disc}` with `{tags}`, `{system}` and `{name}` available.
- **Multi-disc games** can list only the games that are missing a disc.

## Test

    python -m unittest discover -s tests -t .

Engine tests run against stand-in command-line tools, so no chdman or DolphinTool is needed. The window tests
(`test_ui_smoke`, `test_motion`) build the real UI and are skipped when PySide6 isn't installed, or when
`DISTILL_SKIP_UI_TESTS=1` is set (`build_exe.bat` and CI set it for the engine run; CI runs the window tests separately).

## Build a Windows .exe

    build_exe.bat              # dist\Distill\Distill.exe + dist\Distill-portable.zip
    build_exe.bat onefile      # dist\Distill.exe

The build runs the tests first, then starts the finished program with `--self-test` to prove it launches. `tools\` and the
saved settings live next to the `.exe`, so the folder stays portable.

## GitHub Actions and releases

`.github/workflows/build.yml` runs on every push to `main` and on pull requests:

1. **test**: the engine tests on Ubuntu and Windows, Python 3.10 / 3.12 / 3.13. The build waits for these.
2. **window tests**: the Qt window tests, reported separately so a display quirk can't block a build.
3. **build**: PyInstaller on Windows, in two variants (`Distill-portable` zip, `Distill-onefile` exe), each launched with
   `--self-test` before it is uploaded. PyInstaller's `warn-Distill.txt` is uploaded too (`build-diagnostics-*`): look there first if a
   build misbehaves.
4. **release**: pushing a tag such as `v1.0.1` publishes both programs and a `SHA256SUMS.txt` as a GitHub release. The tag must
   match `APP_VERSION` in `distill/constants.py` (and `version` in `pyproject.toml`, which a test checks).

        git tag v1.0 && git push origin v1.0

Release builds come from Actions, which has none of your keys. If you create `distill/local_keys.py` (git-ignored; template in
`distill/local_keys.example.py`) and build locally, those keys are inside that build: don't share it.
