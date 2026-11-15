"""Building from source: distro detection and the script Distill hands to a terminal."""
from __future__ import annotations

import subprocess
import os
import json
import unittest

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from tests.helpers import POSIX_ONLY, TempDirTest, make_tool
from distill import source_builds as builds


class FamilyTests(unittest.TestCase):
    def test_the_main_families_are_recognised(self):
        self.assertEqual(builds.family_of('ID=bazzite\nID_LIKE="fedora"\n'), "fedora")
        self.assertEqual(builds.family_of("ID=ubuntu\nID_LIKE=debian\n"), "debian")
        self.assertEqual(builds.family_of("ID=arch\n"), "arch")
        self.assertEqual(builds.family_of("ID=manjaro\nID_LIKE=arch\n"), "arch")

    def test_an_unknown_distribution_is_not_guessed(self):
        self.assertEqual(builds.family_of("ID=gentoo\n"), "")
        self.assertEqual(builds.family_of(""), "")


@POSIX_ONLY  # bash
class ScriptTests(TempDirTest):
    def test_docker_packaging_pins_amd64_even_when_the_host_defaults_to_arm(self):
        from pathlib import Path

        fakebin = self.tmp / "fake-docker"
        fakebin.mkdir()
        arguments = self.tmp / "docker-arguments.json"
        make_tool(fakebin / "docker", f'''import json, sys
from pathlib import Path
Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))
''')
        script = Path(__file__).resolve().parents[1] / "packaging/build-in-docker.sh"
        result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=10,
                                env={**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                                     "DOCKER_DEFAULT_PLATFORM": "linux/arm64", "DISTILL_BUILD_IMAGE": "example/build:custom"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads(arguments.read_text())
        self.assertEqual(args[:3], ["run", "--platform", "linux/amd64"])
        self.assertIn("example/build:custom", args)
        self.assertIn("ARCH=x86_64", args[-1])

    def atomic_build(self, missing_module="", *, version="v148", existing=False, git_failure=""):
        """Run the generated script with fake build tools; no package manager or network can run."""
        fakebin = self.tmp / "fakebin"
        fakebin.mkdir()
        for name in ("gcc", "g++", "ninja", "curl"):
            make_tool(fakebin / name, "pass")
        for name in ("sudo", "dnf", "rpm-ostree"):
            make_tool(fakebin / name, 'print("UNEXPECTED_PACKAGE_INSTALL"); raise SystemExit(99)')
        make_tool(fakebin / "pkg-config", f'import sys; sys.exit(1 if sys.argv[-1] == {missing_module!r} else 0)')
        make_tool(fakebin / "git", f'''import sys, json
from pathlib import Path
args = sys.argv[1:]
log = Path({str(self.tmp / 'git-commands.json')!r})
commands = json.loads(log.read_text()) if log.exists() else []
commands.append(args)
log.write_text(json.dumps(commands))
head = Path({str(self.tmp / 'git-head')!r})
if not head.exists():
    head.write_text("a" * 40)
failure = {git_failure!r}
if args[0] == "clone":
    (Path(args[-1]) / ".git").mkdir(parents=True)
elif args[0] == "fetch":
    if failure == "fetch":
        raise SystemExit(1)
elif args[:2] == ["remote", "set-head"]:
    pass
elif args[0] == "symbolic-ref":
    print("refs/remotes/origin/main")
elif args[:2] == ["rev-parse", "--verify"]:
    if args[-1] == "HEAD":
        print(head.read_text())
    elif args[-1] == "refs/tags/v148^{{commit}}":
        print("b" * 40)
    elif args[-1] == "refs/remotes/origin/main^{{commit}}":
        print("c" * 40)
    else:
        raise SystemExit(1)
elif args[:2] == ["checkout", "--detach"]:
    if failure == "checkout":
        raise SystemExit(1)
    if failure != "verify":
        head.write_text(args[-1])
else:
    raise SystemExit("Unexpected git operation: " + repr(args))
''')
        make_tool(fakebin / "cmake", '''import sys
from pathlib import Path
if "--version" in sys.argv:
    print("cmake version 3.30.0")
elif "--build" in sys.argv:
    exe = Path(sys.argv[sys.argv.index("--build") + 1]) / "rundir/bin/ares"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\\nexit 0\\n")
    exe.chmod(0o755)
else:
    print("FAKE_CONFIGURED")
''')
        release = self.tmp / "os-release"
        release.write_text('ID=bazzite\nID_LIKE="fedora"\n')
        marker = self.tmp / "ostree-booted"
        marker.touch()
        target = self.tmp / "ares"
        if existing:
            (target / "src/.git").mkdir(parents=True)
        text = builds.script_for("ares", target, version)
        text = text.replace("/etc/os-release", builds.shell_quote(str(release)))
        text = text.replace("/run/ostree-booted", builds.shell_quote(str(marker)))
        script = self.tmp / "atomic-build.sh"
        script.write_text(text)
        result = subprocess.run(["bash", str(script)], env={**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"]},
                                capture_output=True, text=True, input="\n", timeout=20)
        self.assertNotIn("UNEXPECTED_PACKAGE_INSTALL", result.stdout + result.stderr)
        return result, target

    def test_source_marker_advances_an_existing_checkout_to_the_remote_default_branch(self):
        result, _target = self.atomic_build(version="source", existing=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FAKE_CONFIGURED", result.stdout)
        self.assertEqual((self.tmp / "git-head").read_text(), "c" * 40)
        commands = json.loads((self.tmp / "git-commands.json").read_text())
        self.assertIn(["fetch", "--tags", "--depth", "1", "origin", "+refs/heads/*:refs/remotes/origin/*"], commands)
        self.assertIn(["remote", "set-head", "origin", "--auto"], commands)
        self.assertIn(["checkout", "--detach", "c" * 40], commands)
        self.assertFalse(any("source" in command for command in commands))

    def test_source_build_checks_out_the_requested_release_tag_exactly(self):
        result, _target = self.atomic_build(version="v148", existing=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = json.loads((self.tmp / "git-commands.json").read_text())
        self.assertIn(["rev-parse", "--verify", "refs/tags/v148^{commit}"], commands)
        self.assertIn(["checkout", "--detach", "b" * 40], commands)
        self.assertEqual((self.tmp / "git-head").read_text(), "b" * 40)

    def test_fresh_source_clone_with_no_tag_builds_the_verified_default_branch(self):
        result, target = self.atomic_build(version="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = json.loads((self.tmp / "git-commands.json").read_text())
        self.assertEqual(commands[0][0], "clone")
        self.assertEqual((self.tmp / "git-head").read_text(), "c" * 40)
        self.assertTrue((target / "bin/ares").is_file())

    def assert_source_failure(self, failure, expected, version="source"):
        result, target = self.atomic_build(version=version, existing=True, git_failure=failure)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(expected, result.stdout)
        self.assertNotIn("FAKE_CONFIGURED", result.stdout)
        self.assertFalse((target / "bin/ares").exists())
        self.assertEqual((self.tmp / "git-head").read_text(), "a" * 40)

    def test_failed_source_fetch_aborts_before_building(self):
        self.assert_source_failure("fetch", "Could not fetch the source")

    def test_failed_source_checkout_aborts_before_building(self):
        self.assert_source_failure("checkout", "Could not check out source target")

    def test_source_head_mismatch_aborts_before_building(self):
        self.assert_source_failure("verify", "HEAD does not match the resolved target")

    def test_missing_source_release_tag_aborts_before_building(self):
        self.assert_source_failure("", "Could not resolve source target", version="missing-tag")

    def test_atomic_fedora_with_layered_dependencies_reaches_the_build(self):
        result, target = self.atomic_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FAKE_CONFIGURED", result.stdout)
        self.assertTrue((target / "bin/ares").is_file())

    def test_atomic_fedora_with_missing_dependencies_stops_before_building(self):
        result, target = self.atomic_build(missing_module="gtk+-3.0")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gtk+-3.0", result.stdout)
        self.assertIn("sudo rpm-ostree install", result.stdout)
        self.assertIn("reboot", result.stdout)
        self.assertNotIn("FAKE_CONFIGURED", result.stdout)
        self.assertFalse(target.exists())

    def test_the_ares_script_is_valid_bash(self):
        target = self.tmp / "ares"
        script = builds.write_script("ares", target, "v148")
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & 0o111)  # it has to be runnable
        check = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_the_script_carries_every_family_and_the_chosen_version(self):
        text = builds.script_for("ares", self.tmp / "ares", "v148")
        for expected in ("apt install", "dnf install", "pacman -S", "rpm-ostree install", "v148",
                         "build/rundir", "bin/ares", "cmake --build"):
            self.assertIn(expected, text)

    def test_a_target_with_spaces_survives_the_shell(self):
        text = builds.script_for("ares", self.tmp / "my emulators" / "ares", "")
        self.assertIn("my emulators/ares", text)  # single-quoted, so the space is not a word break
        self.assertEqual(builds.shell_quote("it's"), "'it'\\''s'")

    def test_the_atomic_fedora_case_stops_before_touching_anything(self):
        """Bazzite and friends layer packages and need a reboot: the script must say so, not half-install."""
        text = builds.script_for("ares", self.tmp / "ares")
        atomic = text.index("/run/ostree-booted")
        deps = text.index("dnf install -y")
        self.assertLess(atomic, deps)  # the check comes before any package is installed


if __name__ == "__main__":
    unittest.main()
