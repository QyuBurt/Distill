"""Catalog install patterns must not claim a program that is not theirs.

Several entries ended their `exe` pattern with a bare `|\\.AppImage$`, which has no
path anchor and no name component: it matches *every* AppImage on the machine, so
Distill could decide an emulator was installed because an unrelated AppImage existed
in a folder it scanned. `locate_exe`/`scan_folder` match against
`Path.relative_to(...).as_posix()`, so every path here is normalised to forward
slashes before it is matched, exactly as the scanners do.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "distill" / "assets" / "emulators.json"

# Paths that belong to something else entirely. A catalog entry that matches any of
# these would offer to "launch" or "update" a program it did not install.
UNRELATED = (
    "/opt/some-tool/Unrelated-Emulator-9.9.AppImage",
    "/home/someone/Applications/OtherProgram-1.0.AppImage",
    "C:/Program Files/Other/other.exe",
    "/opt/retroarch-cores/other.AppImage",
)

# One realistic installed program per entry that this change touched, as the
# scanner sees it after `as_posix()`.
OWN_PROGRAM = {
    "retroarch": "/home/someone/Applications/RetroArch-Linux-x86_64/retroarch",
    "duckstation": "/home/someone/Applications/DuckStation/duckstation-qt-x64-ReleaseLTCG.exe",
    "pcsx2": "/home/someone/Applications/pcsx2-v2.0/pcsx2-qt",
    "rpcs3": "/home/someone/Applications/rpcs3-v0.0.34/rpcs3",
    "cemu": "/home/someone/Applications/Cemu/Cemu",
}


def catalog() -> list[dict]:
    return json.loads(ASSETS.read_text(encoding="utf-8"))["emulators"]


def pattern_of(entry: dict) -> str:
    """The `exe` pattern, whichever shape the entry uses (top level or a nested 'linux' block)."""
    if entry.get("exe"):
        return str(entry["exe"])
    return str((entry.get("linux") or {}).get("exe") or "")


class CatalogPatternTests(unittest.TestCase):
    def test_no_entry_claims_an_unrelated_program(self):
        for entry in catalog():
            pattern = pattern_of(entry)
            if not pattern:
                continue
            with self.subTest(emulator=entry["id"], pattern=pattern):
                rx = re.compile(pattern, re.IGNORECASE)
                for path in UNRELATED:
                    self.assertIsNone(
                        rx.search(Path(path).as_posix()),
                        f"{entry['id']} matches the unrelated program {path} with {pattern!r}",
                    )

    def test_each_touched_entry_still_recognises_its_own_program(self):
        entries = {e["id"]: e for e in catalog()}
        for emu_id, program in OWN_PROGRAM.items():
            with self.subTest(emulator=emu_id):
                rx = re.compile(pattern_of(entries[emu_id]), re.IGNORECASE)
                self.assertIsNotNone(rx.search(Path(program).as_posix()),
                                     f"{emu_id} no longer recognises its own program {program}")

    def test_pcsx2_and_rpcs3_and_cemu_still_recognise_an_appimage_of_their_own(self):
        entries = {e["id"]: e for e in catalog()}
        for emu_id, name in (("pcsx2", "pcsx2-v2.0-x64.AppImage"), ("rpcs3", "rpcs3.AppImage"), ("cemu", "Cemu.AppImage")):
            with self.subTest(emulator=emu_id):
                rx = re.compile(pattern_of(entries[emu_id]), re.IGNORECASE)
                self.assertIsNotNone(rx.search(f"/home/someone/Applications/{name}"))


if __name__ == "__main__":
    unittest.main()
