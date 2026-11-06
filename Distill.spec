# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe.   Folder build (default):  pyinstaller --noconfirm Distill.spec
#                       Single .exe:             set DISTILL_ONEFILE=1 first
#
# A folder build starts fastest and trips antivirus scanners least; a single .exe unpacks itself to a temp folder
# on every start. Either way tools/ and the saved settings live next to the .exe (see distill.constants.APP_DIR).
import os

from PyInstaller.utils.hooks import collect_all

onefile = os.environ.get("DISTILL_ONEFILE") == "1"

datas, binaries, hiddenimports = [], [], []


def bundle(package, required):
    """Pull a package's data files, native libraries and submodules into the build."""
    try:
        d, b, h = collect_all(package)
    except Exception as exc:
        if required:
            raise SystemExit(f"Build needs '{package}' in the build environment: pip install -r requirements.txt  ({exc})")
        print(f"WARNING: optional package '{package}' is not installed, so it is left out of the build ({exc})")
        return
    if required and not (d or b or h):
        raise SystemExit(f"'{package}' is not installed in the build environment: pip install -r requirements.txt")
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# py7zr's compression back-ends are separate packages with native parts PyInstaller can't see; without them a .7z
# (the DolphinTool and maxcso downloads) can't be unpacked inside the exe.
for package in ("pybcj", "pyppmd", "pyzstd", "inflate64", "brotli", "Cryptodome", "multivolumefile", "texttable", "psutil"):
    bundle(package, required=False)  # not every py7zr version needs every one
for package in ("py7zr", "libtorrent", "mutagen"):  # the app degrades without these when run from source, but a release must have them
    bundle(package, required=True)

datas += [("distill/assets", "distill/assets")]  # the Inter, Outfit and JetBrains Mono font files and the Distill icons

# Qt parts this app never uses: they add hundreds of MB.
excludes = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
            "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
            "PySide6.Qt3DExtras", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuick3D",
            "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtBluetooth", "PySide6.QtNfc",
            "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtRemoteObjects"]

a = Analysis(["Distill.pyw"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)

if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Distill", icon="distill.ico", console=False, upx=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Distill", icon="distill.ico", console=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="Distill", upx=False)
