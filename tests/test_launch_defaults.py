"""Every emulator in the catalog has a decided default for starting a game from the Games library.

An emulator is either given a template in games.STANDALONE_ARGS (which must hand over the game and ask for full screen), or it is named in
NO_SWITCH below with the reason. A new emulator added to emulators.json fails here until one or the other is done, so nothing is left
launching by accident with a bare file path.
"""
import json
import unittest
from pathlib import Path

from distill import games as gm
from distill.constants import ASSETS_DIR

# Left at "the game's file" on purpose: no full-screen switch could be confirmed for them, or they can't be given a game file at all.
NO_SWITCH = {
    "retroarch": "built by retroarch_command(), which already adds -f",
    "project64": "no documented full-screen switch; set Options > Settings > 'Full screen on loading a ROM'",
    "bigpemu": "no documented full-screen switch; set it in BigPEmu's video settings",
    "ymir": "full-screen switch not confirmed for the installed build",
    "vita3k": "starts an installed title by its title id (-r), not by a file",
    "kytyps5": "PS5 loaders take their game from their own config, not the command line",
    "anyps5": "PS5 loaders take their game from their own config, not the command line",
    "eglantine": "Switch 2 fork with no documented command line yet",
    "lichii": "Switch 2 fork with no documented command line yet",
    "bluestacks": "an Android player: it installs apps, it does not open a game file",
}
# What a default must contain to ask for full screen, per emulator (one flag each is enough).
FULLSCREEN = {
    "duckstation": "-fullscreen", "pcsx2": "-fullscreen", "ppsspp": "--fullscreen", "shadps4": "-f", "dolphin": "Dolphin.Display.Fullscreen=True",
    "cemu": "-f", "ryujinx": "-f", "eden": "-f", "sudachi": "-f", "azahar": "-f", "lime3ds": "-f", "melonds": "-f", "mgba": "-f",
    "mesen2": "--fullscreen", "snes9x": "-fullscreen", "rmg": "-f", "xenia-canary": "--fullscreen=true", "xemu": "-full-screen",
    "flycast": "window:fullscreen=yes", "blastem": "-f", "ares": "--fullscreen", "mame": "-nowindow", "fbneo": "-f",
    "dosbox-staging": "--fullscreen", "scummvm": "-f",
}
# Given the game's file, but no full-screen switch could be confirmed: they start full screen from their own settings.
FULLSCREEN_IN_SETTINGS = {"rpcs3": "Configuration > GUI > 'Start games in fullscreen mode'"}
# Plays the game by something other than its file path: a name, or a folder.
NOT_BY_FILE = {"mame": "{name}", "fbneo": "{name}", "scummvm": "--path={dir}"}


class LaunchDefaults(unittest.TestCase):
    def setUp(self):
        self.ids = [e["id"] for e in json.loads((Path(ASSETS_DIR) / "emulators.json").read_text("utf-8"))["emulators"]]
        self.game = gm.Game("g", "My Game (USA)", "PlayStation", str(Path("/roms/ps1/My Game (USA).cue")))

    def test_every_emulator_is_decided(self):
        for i in self.ids:
            self.assertTrue(i in gm.STANDALONE_ARGS or i in NO_SWITCH, f"{i} has neither a launch template nor a reason it has none")
        for i in (*gm.STANDALONE_ARGS, *NO_SWITCH):
            self.assertIn(i, self.ids, f"{i} is not in the catalog any more")
        self.assertFalse(set(gm.STANDALONE_ARGS) & set(NO_SWITCH))

    def test_every_template_hands_over_the_game_and_asks_for_full_screen(self):
        for i, template in gm.STANDALONE_ARGS.items():
            args = gm.launch_args(template, self.game)
            how = NOT_BY_FILE.get(i)
            if how is None:
                self.assertIn(str(Path(self.game.path)), args, f"{i} is not given the game's file")
            else:
                want = how.replace("{name}", "My Game (USA)").replace("{dir}", str(Path(self.game.path).parent))
                self.assertTrue(any(want in a for a in args), f"{i}: {args}")
            if i in FULLSCREEN:
                self.assertTrue(any(FULLSCREEN[i] in a for a in args), f"{i} does not ask for full screen: {args}")
            else:
                self.assertIn(i, FULLSCREEN_IN_SETTINGS, f"{i} has no full-screen switch and no note saying why")

    def test_a_template_with_only_the_folder_does_not_get_the_file_added(self):
        # ScummVM would read a trailing file path as an unknown game id and refuse to start
        args = gm.launch_args(gm.STANDALONE_ARGS["scummvm"], self.game)
        self.assertEqual(args, ["-f", "--auto-detect", f"--path={Path(self.game.path).parent}"])

    def test_every_template_survives_an_exe_with_spaces_in_its_path(self):
        for i in gm.STANDALONE_ARGS:
            plan = gm.standalone_plan(self.game, gm.Standalone(i, i, Path("/Program Files/Emu One/emu.exe")))
            self.assertEqual(plan.cmd[0], str(Path("/Program Files/Emu One/emu.exe")))
            self.assertTrue(all(isinstance(a, str) and a for a in plan.cmd), i)


if __name__ == "__main__":
    unittest.main()
