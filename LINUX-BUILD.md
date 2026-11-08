# Linux support and AppImage builds

Run from source with Python 3.10+ and the packages listed in `requirements.txt`:

```bash
python Distill.pyw
```

System-wide PySide6 works; a virtual environment is optional. No package installation runs at app startup.

## Runtime files and existing data

Linux uses `distill.paths`, whether the application folder is writable or mounted read-only:

| Files | Directory | Default |
| --- | --- | --- |
| Settings and catalog overrides | `$XDG_CONFIG_HOME/distill` | `~/.config/distill` |
| Downloaded tools, managed installs and data | `$XDG_DATA_HOME/distill` | `~/.local/share/distill` |
| Logs, undo records and runtime state | `$XDG_STATE_HOME/distill` | `~/.local/state/distill` |
| Downloadable caches | `$XDG_CACHE_HOME/distill` | `~/.cache/distill` |

Only absolute XDG overrides are accepted. `DISTILL_PORTABLE=1` keeps the existing app-folder layout instead.
Tools stay in the existing data directory's `tools/` folder. Explicit and app-relative tool paths retain their
meaning. Startup copies only the existing allowlisted legacy settings and state files; it never removes the
source or overwrites an existing destination. Credentials, tool folders, installed games and queues are not migrated.
There is no separate `~/.local/share/Distill` fallback for read-only bundles.

## Linux features

- Native downloads: ffmpeg/ffprobe, pkg-to-anyps5, xdvdfs, PKG_Extractor, AnyPS5 relinker and Redrrx/ps3dec.
  PKG_Extractor's AppImage runs with extract-and-run, so it does not require FUSE.
- PS3Dec dialect detection runs only on Linux. Windows retains `d key <hex> <input> <output>` without a help probe.
- Dolphin, MAME and ScummVM install through user Flatpak. Project64 uses a portable Windows build through Wine.
  Flatpak and Wine must already be installed; missing tools produce an explanation.
- Eden uses its Forgejo downloads on Linux. Xenia's unusually named tarball is recognized by content.
- Ares has a **Build from source** button that opens a terminal. Its generated script installs distribution build
  dependencies after the user's password prompt, then builds into the emulator folder. Debian, Fedora and Arch
  families are supported. Atomic Fedora systems receive instructions before any package installation.

`chdman`, DolphinTool, maxcso and ZArchive need distribution packages or source builds when no native release is
available. On Arch, install chdman from `mame-tools`. Windows catalog sources, asset selection and trust settings
remain separate from Linux catalog overrides. `Distill.spec` and `build_exe.bat` are unchanged.

## Validation without touching user data

```bash
QT_QPA_PLATFORM=offscreen python tests/ci_unittest.py --timeout 180 -- python -m unittest discover -s tests -t .
scratch=$(mktemp -d)
XDG_CONFIG_HOME="$scratch/config" XDG_DATA_HOME="$scratch/data" \
XDG_STATE_HOME="$scratch/state" XDG_CACHE_HOME="$scratch/cache" \
QT_QPA_PLATFORM=offscreen python Distill.pyw --self-test "$scratch/report.txt"
cat "$scratch/report.txt"
```

The tests isolate application state themselves and deny external Python network connections. Qt tests run offscreen.
The self-test constructs the real window without starting its background download threads.

## Building an AppImage

`distill-linux.spec` builds a PyInstaller one-folder bundle; `packaging/AppRun` starts it inside the AppImage.
The Linux GitHub Actions workflow runs all tests offscreen, builds the bundle, packages the image and runs its
self-test. Its artifact contains `Distill-x86_64.AppImage` and `Distill-x86_64.AppImage.sha256`.
It runs on pushes, pull requests and manual dispatches.

For Docker Desktop (WSL2 backend on Windows) or Docker on Linux:

```bash
bash packaging/build-in-docker.sh
```

The container installs its own dependencies and leaves the AppImage and checksum sidecar in the repository root.
It always targets `linux/amd64`. ARM hosts, including Apple Silicon Macs, require Docker's amd64 emulation.
Tests must pass before it builds. Set `DISTILL_BUILD_IMAGE` to select a different Ubuntu-compatible base image.

To build manually, install the requirements and PyInstaller in your build environment, then run:

```bash
pyinstaller --noconfirm distill-linux.spec
curl -fL -o appimagetool https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x appimagetool
mkdir -p AppDir/usr/share/icons/hicolor/256x256/apps AppDir/usr/share/applications
cp -r dist/Distill AppDir/Distill
cp packaging/AppRun AppDir/AppRun
chmod +x AppDir/AppRun
cp packaging/distill.desktop AppDir/
cp packaging/distill.desktop AppDir/usr/share/applications/
cp distill.png AppDir/
cp distill.png AppDir/usr/share/icons/hicolor/256x256/apps/
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 ./appimagetool --no-appstream AppDir Distill-x86_64.AppImage
sha256sum Distill-x86_64.AppImage > Distill-x86_64.AppImage.sha256
```

Run the image's `--self-test` using the scratch XDG environment above and `APPIMAGE_EXTRACT_AND_RUN=1` on systems
without FUSE. Native Windows validation and full AppImage distribution testing remain separate build checks.

## Credits

Native tool downloads, emulator install kinds (Flatpak, Wine, source build) and the AppImage packaging recipe come from a Linux port by Niko. The XDG runtime layout, test-infrastructure baseline and AppImage startup/child-environment fixes were added on top of it.
