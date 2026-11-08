#!/bin/bash
# Build Distill's AppImage inside a container, so it can be built from Windows (Docker Desktop) or macOS.
#
#   bash packaging/build-in-docker.sh
#
# Leaves Distill-x86_64.AppImage in the repository root. Nothing is installed on the host.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${DISTILL_BUILD_IMAGE:-ubuntu:24.04}"

docker run --platform linux/amd64 --rm -v "${here}:/src" -w /src "${image}" bash -lc '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
glib=libglib2.0-0t64
apt-cache show "$glib" >/dev/null 2>&1 || glib=libglib2.0-0
apt-get install -y -qq python3 python3-venv python3-pip file curl ca-certificates \
    libegl1 libopengl0 libxkbcommon0 libdbus-1-3 "$glib" libfontconfig1 libfreetype6 libpulse0 libgl1 \
    libx11-6 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xkb1 libxcb-xinerama0 \
    libwayland-client0 libwayland-cursor0 libwayland-egl1 >/dev/null
python3 -m venv /venv
/venv/bin/pip install -q --upgrade pip
/venv/bin/pip install -q -r requirements.txt "pyinstaller>=6.6"
echo "== tests =="
QT_QPA_PLATFORM=offscreen /venv/bin/python tests/ci_unittest.py --timeout 180 -- /venv/bin/python -m unittest discover -s tests -t .
echo "== PyInstaller =="
/venv/bin/pyinstaller --noconfirm distill-linux.spec >/tmp/pyi.log 2>&1 || { tail -30 /tmp/pyi.log; exit 1; }
if [[ ! -f dist/Distill/_internal/PySide6/Qt/plugins/platforms/libqoffscreen.so ]]; then
    echo "ERROR: Qt platform plugins are missing; check build dependencies and /tmp/pyi.log for failed to obtain Qt library info." >&2
    exit 1
fi
echo "== AppImage =="
curl -sL -o /tmp/appimagetool https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x /tmp/appimagetool
rm -rf /tmp/AppDir && mkdir -p /tmp/AppDir/usr/share/icons/hicolor/256x256/apps /tmp/AppDir/usr/share/applications
cp -r dist/Distill /tmp/AppDir/Distill
cp packaging/AppRun /tmp/AppDir/AppRun && chmod +x /tmp/AppDir/AppRun
cp packaging/distill.desktop /tmp/AppDir/distill.desktop
cp packaging/distill.desktop /tmp/AppDir/usr/share/applications/distill.desktop
cp distill.png /tmp/AppDir/distill.png
cp distill.png /tmp/AppDir/usr/share/icons/hicolor/256x256/apps/distill.png
cd /tmp && APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 ./appimagetool --no-appstream AppDir /src/Distill-x86_64.AppImage
cd /src && sha256sum Distill-x86_64.AppImage > Distill-x86_64.AppImage.sha256
echo "== smoke test: the packaged app starts and builds its window =="
export XDG_CONFIG_HOME=/tmp/distill-config XDG_DATA_HOME=/tmp/distill-data
export XDG_STATE_HOME=/tmp/distill-state XDG_CACHE_HOME=/tmp/distill-cache
QT_QPA_PLATFORM=offscreen APPIMAGE_EXTRACT_AND_RUN=1 ./Distill-x86_64.AppImage --self-test /tmp/report.txt || { cat /tmp/report.txt; exit 1; }
tail -3 /tmp/report.txt
'
