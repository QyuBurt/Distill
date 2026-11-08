import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from distill import afteractions as aa


class CommandLineTests(unittest.TestCase):
    def test_tokenizing(self):
        self.assertEqual(aa.tokenize('sync.bat "C:\\My Files\\out" {list}  x'), ["sync.bat", "C:\\My Files\\out", "{list}", "x"])
        self.assertEqual(aa.tokenize('  '), [])
        self.assertEqual(aa.tokenize('a "" b'), ["a", "", "b"])  # an empty quoted word is still a word
        self.assertEqual(aa.tokenize('say "hello world"!'), ["say", "hello world!"])
        with self.assertRaises(ValueError):
            aa.tokenize('a "b')

    def test_expansion_is_only_for_the_four_words(self):
        got = aa.expand(["x", "{folder}", "--n={count}", "{other}", "{failed}/{list}"],
                        {"folder": "D:\\dl", "count": "3", "failed": "1", "list": "L.txt"})
        self.assertEqual(got, ["x", "D:\\dl", "--n=3", "{other}", "1/L.txt"])

    def test_a_name_with_shell_characters_stays_one_argument(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "args.txt"
        script = tmp / "show.py"
        script.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('\\n'.join(sys.argv[2:]), encoding='utf-8')\n")
        folder = tmp / "a & b; echo hi"
        folder.mkdir()
        proc, err = aa.start_command(f'"{sys.executable}" "{script}" "{out}" {{folder}} {{count}} {{failed}} {{list}}',
                                     folder, [folder / "x y.zip", folder / "z.7z"], 2)
        self.assertEqual(err, "")
        self.assertEqual(proc.wait(timeout=30), 0)
        folder_arg, count, failed, listing = out.read_text(encoding="utf-8").split("\n")
        self.assertEqual((folder_arg, count, failed), (str(folder), "2", "2"))  # 2 files in the list, 2 failures reported
        self.assertEqual(Path(listing).read_text(encoding="utf-8").splitlines(), [str(folder / "x y.zip"), str(folder / "z.7z")])

    def test_problems_are_reported_not_raised(self):
        self.assertEqual(aa.start_command("", ".", [], 0), (None, ""))
        proc, err = aa.start_command('"unclosed', ".", [], 0)
        self.assertTrue(proc is None and "quote" in err)
        proc, err = aa.start_command("definitely-not-a-real-program-xyz {folder}", ".", [], 0)
        self.assertTrue(proc is None and "couldn't find" in err)


class PowerTests(unittest.TestCase):
    def run_fake(self, code=0, err=""):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=code, stderr=err, stdout="")
        return calls, run

    def test_windows(self):
        calls, run = self.run_fake()
        self.assertEqual(aa.perform_power("shutdown", "Windows", run), "")
        self.assertEqual(aa.perform_power("hibernate", "Windows", run), "")
        self.assertEqual(calls, [["shutdown", "/s", "/t", "0"], ["shutdown", "/h"]])
        slept = []
        self.assertEqual(aa.perform_power("sleep", "Windows", run, suspend=lambda: slept.append(1) or True), "")
        self.assertEqual((slept, len(calls)), ([1], 2))  # sleep goes through the API, not a command
        self.assertIn("refused", aa.perform_power("sleep", "Windows", run, suspend=lambda: False))

    def test_linux_and_mac(self):
        calls, run = self.run_fake()
        for action in ("sleep", "hibernate", "shutdown"):
            self.assertEqual(aa.perform_power(action, "Linux", run), "")
        self.assertEqual(calls, [["systemctl", "suspend"], ["systemctl", "hibernate"], ["systemctl", "poweroff"]])
        calls.clear()
        aa.perform_power("hibernate", "Darwin", run)
        aa.perform_power("shutdown", "Darwin", run)
        self.assertEqual(calls[0], ["pmset", "sleepnow"])
        self.assertEqual(calls[1][0], "osascript")

    def test_failures_come_back_as_text(self):
        _calls, run = self.run_fake(1, "Access denied\nmore")
        self.assertEqual(aa.perform_power("shutdown", "Linux", run), "Access denied")
        self.assertIn("unknown", aa.perform_power("explode", "Linux", run))

        def missing(argv, **kw):
            raise FileNotFoundError("systemctl not found")
        self.assertIn("not found", aa.perform_power("shutdown", "Linux", missing))

    def test_the_choices_line_up(self):
        self.assertEqual(len(aa.POWER), len(aa.POWER_LABELS))
        self.assertEqual(aa.POWER[0], "")
        self.assertTrue(all(a in aa.POWER_PHRASES for a in aa.POWER[1:]))


if __name__ == "__main__":
    unittest.main()
