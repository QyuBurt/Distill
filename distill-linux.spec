# -*- mode: python ; coding: utf-8 -*-
# Linux (AppImage) build of Distill.  Run from the repo root:
#     pyinstaller --noconfirm distill-linux.spec
# Produces dist/Distill/ which the AppImage recipes wrap.
#
# Same recipe as Distill.spec, with the Windows-only bits dropped: no .ico for the launcher (Windows-only parameter)
# and a one-folder layout (an AppImage is a folder anyway, and a folder build starts fastest).
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []


def bundle(package, required):
    """Pull a package's data files, native libraries and submodules into the build."""
    try:
        d, b, h = collect_all(package)
    except Exception as exc:
        if required:
            raise SystemExit(f"Build needs '{package}': pip install -r requirements.txt  ({exc})")
        print(f"WARNING: optional package '{package}' is not installed, so it is left out of the build ({exc})")
        return
    if required and not (d or b or h):
        raise SystemExit(f"'{package}' is not installed in the build environment: pip install -r requirements.txt")
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# py7zr's compression back-ends are separate packages with native parts PyInstaller can't see.
for package in ("pybcj", "pyppmd", "pyzstd", "inflate64", "brotli", "Cryptodome", "multivolumefile", "texttable", "psutil"):
    bundle(package, required=False)
for package in ("py7zr", "libtorrent", "mutagen"):
    bundle(package, required=True)

datas += [("distill/assets", "distill/assets")]  # fonts and the Distill icons

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

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Distill", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Distill", upx=False)
