"""Keep the frozen Linux bundle's libraries out of host tools."""
from __future__ import annotations

import os
import subprocess
import sys
from functools import wraps

_installed = False


def host_env(environ=None) -> dict:
    """Copy the environment, undoing PyInstaller's Linux library and Qt path overrides."""
    env = dict(os.environ if environ is None else environ)
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        return env
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        bundle = getattr(sys, "_MEIPASS", None)
        library_path = env.get("LD_LIBRARY_PATH", "")
        if bundle and library_path == bundle:
            env.pop("LD_LIBRARY_PATH", None)
        elif bundle and library_path.startswith(bundle + ":"):
            env["LD_LIBRARY_PATH"] = library_path[len(bundle) + 1:]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bundle = os.path.normpath(bundle)
        for name in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            entries = env.get(name, "").split(":")
            host_entries = [entry for entry in entries if not (
                os.path.normpath(entry) == bundle or os.path.normpath(entry).startswith(bundle + "/"))]
            if host_entries == entries:
                continue
            if name + "_ORIG" in env:
                env[name] = env.pop(name + "_ORIG")
            elif host_entries:
                env[name] = ":".join(host_entries)
            else:
                env.pop(name, None)
    return env


def install() -> None:
    """Install once, before Qt or worker threads start. Source and Windows runs are unchanged."""
    global _installed
    if _installed or not (getattr(sys, "frozen", False) and sys.platform.startswith("linux")):
        return
    from PySide6.QtGui import QDesktopServices

    popen_init = subprocess.Popen.__init__
    open_url = QDesktopServices.openUrl

    @wraps(popen_init)
    def host_popen(self, *args, **kwargs):
        # env is Popen's eleventh positional argument (after cwd).
        if len(args) > 10:
            args = (*args[:10], host_env(args[10]), *args[11:])
        else:
            kwargs["env"] = host_env(kwargs.get("env"))
        return popen_init(self, *args, **kwargs)

    @wraps(open_url)
    def host_open_url(*args, **kwargs):
        # Qt launches xdg-open outside subprocess. These calls run on the UI thread.
        names = ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "QT_PLUGIN_PATH", "QT_PLUGIN_PATH_ORIG",
                 "QML2_IMPORT_PATH", "QML2_IMPORT_PATH_ORIG")
        original = {name: os.environ.get(name) for name in names}
        cleaned = host_env()
        try:
            for name in names:
                if name in cleaned:
                    os.environ[name] = cleaned[name]
                else:
                    os.environ.pop(name, None)
            return open_url(*args, **kwargs)
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    subprocess.Popen.__init__ = host_popen
    QDesktopServices.openUrl = staticmethod(host_open_url)
    _installed = True
