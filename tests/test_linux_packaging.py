"""Exercise CI's dependency shell without installing packages or using host state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import textwrap
import unittest

from tests.helpers import TempDirTest


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "Linux packaging uses Bash")
class AppImageDependenciesTests(TempDirTest):
    def install_packages(self, modern_glib):
        workflow = (ROOT / ".github/workflows/linux-appimage.yml").read_text(encoding="utf-8")
        step = workflow.split("      - name: Install dependencies\n", 1)[1].split("      - name:", 1)[0]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        commands = self.tmp / "bin"
        commands.mkdir()
        log = self.tmp / "commands.jsonl"
        # No forwarding: even sudo, apt and pip are disposable local recorders.
        stub = f"""#!{sys.executable}
import json, os, pathlib, sys
with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
if pathlib.Path(sys.argv[0]).name == 'apt-cache':
    raise SystemExit(0 if os.environ['MODERN_GLIB'] == '1' else 1)
"""
        for name in ("sudo", "apt-cache", "python", "pip"):
            path = commands / name
            path.write_text(stub, encoding="utf-8")
            path.chmod(0o700)
        env = dict(os.environ, PATH=str(commands), HOME=str(self.tmp / "home"),
                   COMMAND_LOG=str(log), MODERN_GLIB=str(int(modern_glib)))
        for category in ("CONFIG", "DATA", "STATE", "CACHE"):
            env[f"XDG_{category}_HOME"] = str(self.tmp / category.lower())
        result = subprocess.run([shutil.which("bash"), "-eo", "pipefail", "-c", script],
                                cwd=self.tmp, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        installs = [args[3:] for args in calls if args[:3] == ["sudo", "apt-get", "install"]]
        self.assertEqual(len(installs), 1, calls)
        return {arg for arg in installs[0] if not arg.startswith("-")}

    def assert_docker_runtime_parity(self, packages, glib):
        docker = (ROOT / "packaging/build-in-docker.sh").read_text(encoding="utf-8")
        install = docker.split("apt-get install ", 1)[1].split(">/dev/null", 1)[0]
        expected = {word for word in shlex.split(install.replace("\\\n", " ")) if word.startswith("lib")}
        expected.add(glib)
        self.assertIn("libpulse0", packages)
        self.assertEqual({word for word in packages if word.startswith("lib")}, expected)
        self.assertTrue({"file", "curl", "ca-certificates"} <= packages)

    def test_ubuntu_24_runtime_dependencies_match_docker(self):
        self.assert_docker_runtime_parity(self.install_packages(True), "libglib2.0-0t64")

    def test_older_glib_package_fallback_keeps_runtime_dependencies(self):
        self.assert_docker_runtime_parity(self.install_packages(False), "libglib2.0-0")
