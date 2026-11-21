"""A game's own launch settings (distill/launchprofile.py and how games.plan_launch / spawn use them): real commands are run."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
import os
from pathlib import Path

from tests import helpers  # noqa: F401
from distill import games as gm
from distill import launchprofile as lp

q = lp.shell_quote


class ProfileTests(unittest.TestCase):
    def test_a_profile_with_nothing_in_it_is_kept_as_nothing(self):
        self.assertTrue(lp.Profile().empty())
        self.assertEqual(lp.Profile().to_json(), "")
        self.assertTrue(lp.Profile(args="  ", cwd=" ").empty())

    def test_a_profile_round_trips(self):
        p = lp.Profile(args="-f {rom}", cwd="/games", env="A=1\nB=2", pre="mount", post="unmount", admin=True)
        self.assertEqual(lp.Profile.from_json(p.to_json()), p)

    def test_only_what_is_set_is_stored(self):
        self.assertEqual(lp.Profile(post="x").to_json(), '{"post": "x"}')

    def test_anything_unreadable_is_an_empty_profile(self):
        for text in ("", "not json", "[1, 2]", "42", '{"args": 5, "admin": "yes", "nonsense": "x"}'):
            self.assertTrue(lp.Profile.from_json(text).empty(), text)

    def test_a_good_field_survives_a_bad_one(self):
        self.assertEqual(lp.Profile.from_json('{"args": "-f", "cwd": 7}'), lp.Profile(args="-f"))


class EnvTests(unittest.TestCase):
    def test_lines_become_variables(self):
        self.assertEqual(lp.parse_env("A=1\n  B = two words \n# comment\n\nnot a pair\nC=\n=nokey\nD E=3"), {"A": "1", "B": "two words", "C": ""})

    def test_an_equals_sign_in_a_value_is_kept(self):
        self.assertEqual(lp.parse_env("OPTS=a=b,c=d"), {"OPTS": "a=b,c=d"})


class ExpandTests(unittest.TestCase):
    def test_placeholders_are_filled_and_quoted(self):
        rom_path = "/my games/Metroid (USA).sfc"
        out = lp.expand("run {rom} in {dir} as {name}", rom_path)
        self.assertIn(q(os.path.normpath(rom_path)), out)
        self.assertIn(q(os.path.normpath(os.path.dirname(rom_path))), out)
        self.assertTrue(out.endswith(q("Metroid (USA)")))


class Hooks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.script = self.root / "mark.py"
        self.script.write_text("import sys\nopen(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + '\\n')\n")
        self.fail_script = self.root / "fail.py"
        self.fail_script.write_text("import sys\nsys.exit(3)\n")

    def mark(self, marker: Path, *words: str) -> str:
        return f"{q(sys.executable)} {q(str(self.script))} {q(str(marker))} {' '.join(words)}"

    def test_a_command_runs_and_is_waited_for(self):
        marker = self.root / "out.txt"
        self.assertEqual(lp.run_hook(self.mark(marker, "hello"), "/g/Metroid.sfc"), 0)
        self.assertEqual(marker.read_text().strip(), "hello")

    def test_the_games_file_is_available_to_the_command(self):
        marker = self.root / "out.txt"
        rom = self.root / "My Game.sfc"
        lp.run_hook(self.mark(marker, "{name}"), str(rom))
        self.assertEqual(marker.read_text().strip(), "My Game")

    def test_nothing_to_run_is_fine(self):
        self.assertEqual(lp.run_hook("   ", "/g/x.sfc"), 0)
        self.assertIsNone(lp.run_hook("", "/g/x.sfc", wait=False))

    def test_a_failing_command_says_so(self):
        with self.assertRaises(lp.HookError) as ctx:
            lp.run_hook(f"{q(sys.executable)} {q(str(self.fail_script))}", "/g/x.sfc", what="The command to run before the game")
        self.assertIn("code 3", str(ctx.exception))
        self.assertIn("not started", str(ctx.exception))

    def test_a_command_that_takes_too_long_stops_the_game_starting(self):
        slow = f'{q(sys.executable)} -c "import time; time.sleep(30)"'
        with self.assertRaises(lp.HookError) as ctx:
            lp.run_hook(slow, "/g/x.sfc", timeout=0.4)
        self.assertIn("longer than", str(ctx.exception))

    def test_a_command_started_without_waiting_runs_on_its_own(self):
        marker = self.root / "later.txt"
        self.assertIsNone(lp.run_hook(self.mark(marker, "later"), "/g/x.sfc", wait=False))
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(marker.read_text().strip(), "later")


def game(root: Path, name="Metroid.sfc", system="SNES") -> gm.Game:
    rom = root / name
    rom.write_bytes(b"rom")
    return gm.Game(id="g1", title="Metroid", system=system, path=str(rom))


class ApplyProfileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.game = game(self.root)
        self.emu = gm.Standalone("snes9x", "Snes9x", self.root / "snes9x.exe")
        (self.root / "snes9x.exe").write_bytes(b"")

    def plan(self, profile, choice="snes9x", setup=None):
        return gm.plan_launch(self.game, choice, setup, "", {"snes9x": self.emu}, profile=profile)

    def test_no_profile_changes_nothing(self):
        base = self.plan(None)
        self.assertIs(gm.apply_profile(base, lp.Profile(), self.game, True), base)
        self.assertEqual(self.plan(lp.Profile()).cmd, base.cmd)

    def test_a_standalone_emulators_options_are_replaced_for_this_game(self):
        plan = self.plan(lp.Profile(args="--special -f {rom}"))
        self.assertEqual(plan.cmd, [str(self.emu.exe), "--special", "-f", self.game.path])

    def test_the_file_is_added_when_the_options_never_mention_it(self):
        plan = self.plan(lp.Profile(args="--windowed"))
        self.assertEqual(plan.cmd, [str(self.emu.exe), "--windowed", self.game.path])

    def test_a_mac_app_keeps_the_part_that_opens_it(self):
        base = gm.LaunchPlan(["open", "-a", "/Apps/Emu.app", "--args", "old"], "/Apps", "Emu")
        got = gm.apply_profile(base, lp.Profile(args="-x {rom}"), self.game, True)
        self.assertEqual(got.cmd, ["open", "-a", "/Apps/Emu.app", "--args", "-x", self.game.path])

    def test_retroarch_gets_the_options_added_to_its_command(self):
        base = gm.LaunchPlan(["/ra/retroarch", "-L", "core.dll", "game.sfc"], "/ra", "RetroArch · Snes9x")
        got = gm.apply_profile(base, lp.Profile(args="--appendconfig {dir}/extra.cfg"), self.game, False)
        self.assertEqual(got.cmd[:4], base.cmd)  # (what was there stays, in order)
        self.assertEqual(got.cmd[4], "--appendconfig")
        self.assertTrue(got.cmd[5].endswith("extra.cfg"))
        self.assertTrue(got.cmd[5].startswith(str(Path(self.game.path).parent)))  # ({dir} was filled in)

    def test_unclosed_quotes_are_said_in_words(self):
        base = gm.LaunchPlan(["/ra/retroarch"], "/ra", "RetroArch · X")
        with self.assertRaises(gm.GameError):
            gm.apply_profile(base, lp.Profile(args='--a "oops'), self.game, False)

    def test_the_working_folder_is_used_and_must_exist(self):
        folder = self.root / "work"
        folder.mkdir()
        self.assertEqual(self.plan(lp.Profile(cwd=str(folder))).cwd, str(folder))
        self.assertEqual(self.plan(lp.Profile(cwd="{dir}")).cwd, str(self.root))
        with self.assertRaises(gm.GameError) as ctx:
            self.plan(lp.Profile(cwd=str(self.root / "nowhere")))
        self.assertIn("doesn't exist", str(ctx.exception))

    def test_environment_and_commands_ride_with_the_plan(self):
        plan = self.plan(lp.Profile(env="A=1\nB=2", pre=" mount ", post="unmount"))
        self.assertEqual((plan.env, plan.pre, plan.post, plan.game_path), ({"A": "1", "B": "2"}, "mount", "unmount", self.game.path))

    def test_administrator_is_only_for_windows(self):
        plan = self.plan(lp.Profile(admin=True))
        self.assertEqual(plan.admin, sys.platform == "win32")

    def test_a_game_that_cannot_be_started_still_says_why(self):
        with self.assertRaises(gm.GameError):
            gm.plan_launch(self.game, "nothere", None, "", {}, profile=lp.Profile(args="-x"))


class SpawnTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.script = self.root / "emu.py"
        self.script.write_text("import os, sys\nopen(sys.argv[1], 'a').write('started ' + os.environ.get('MYVAR', '-') + ' ' + os.getcwd() + '\\n')\n")
        self.mark = self.root / "mark.py"
        self.mark.write_text("import sys\nopen(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + '\\n')\n")

    def mark_cmd(self, out: Path, word: str) -> str:
        return f"{q(sys.executable)} {q(str(self.mark))} {q(str(out))} {word}"

    def test_environment_and_folder_reach_the_program(self):
        out = self.root / "out.txt"
        plan = gm.LaunchPlan([sys.executable, str(self.script), str(out)], str(self.root), "T", env={"MYVAR": "hello"})
        gm.spawn(plan).wait()
        self.assertEqual(out.read_text().split(), ["started", "hello", str(self.root.resolve()) if sys.platform != "win32" else str(self.root)])

    def test_a_program_without_a_profile_is_started_as_before(self):
        out = self.root / "out.txt"
        gm.spawn(gm.LaunchPlan([sys.executable, str(self.script), str(out)], str(self.root), "T")).wait()
        self.assertTrue(out.read_text().startswith("started -"))

    def test_the_command_before_runs_first_and_a_failing_one_stops_the_game(self):
        order = self.root / "order.txt"
        plan = gm.LaunchPlan([sys.executable, str(self.script), str(order)], str(self.root), "T", pre=self.mark_cmd(order, "before"), game_path="/g/x.sfc")
        gm.spawn(plan).wait()
        self.assertEqual([line.split()[0] for line in order.read_text().splitlines()], ["before", "started"])
        out = self.root / "never.txt"
        bad = gm.LaunchPlan([sys.executable, str(self.script), str(out)], str(self.root), "T", pre=f'{q(sys.executable)} -c "import sys; sys.exit(1)"', game_path="/g/x.sfc")
        with self.assertRaises(gm.GameError):
            gm.spawn(bad)
        self.assertFalse(out.exists())

    def test_the_command_after_runs_when_the_program_has_closed_however_short_it_was(self):
        out, done = self.root / "out.txt", self.root / "done.txt"
        plan = gm.LaunchPlan([sys.executable, str(self.script), str(out)], str(self.root), "T", post=self.mark_cmd(done, "after"), game_path="/g/x.sfc")
        proc = gm.spawn(plan)
        sessions = []
        gm.watch_session(proc, sessions.append, gm.post_hook(plan)).join(10)
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(done.read_text().strip(), "after")
        self.assertEqual(sessions, [])  # (too short to count as played: but the command still ran)

    def test_no_command_after_means_nothing_to_call(self):
        self.assertIsNone(gm.post_hook(gm.LaunchPlan(["x"], ".", "T")))


class StoreTests(unittest.TestCase):
    def test_profiles_are_kept_per_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = gm.GameStore(Path(tmp) / "games.db")
            self.assertTrue(store.profile("g1").empty())
            store.set_profile("g1", lp.Profile(args="-f", admin=True))
            self.assertEqual(store.profile("g1"), lp.Profile(args="-f", admin=True))
            self.assertTrue(store.profile("g2").empty())
            store.set_profile("g1", lp.Profile())  # emptied: nothing is kept
            self.assertTrue(store.profile("g1").empty())
            self.assertEqual(store.setting("profile:g1", "x"), "")

    def test_the_revision_moves_on_a_write_and_stays_put_for_a_read(self):
        """The Games page skips rebuilding its grid on the way back from a card when this has not moved."""
        with tempfile.TemporaryDirectory() as tmp:
            store = gm.GameStore(Path(tmp) / "games.db")
            before = store.revision
            store.totals(), store.systems(), store.games(), store.setting("x", "")
            self.assertEqual(store.revision, before)
            store.set_favorite("g1", True)
            self.assertGreater(store.revision, before)


if __name__ == "__main__":
    unittest.main()
