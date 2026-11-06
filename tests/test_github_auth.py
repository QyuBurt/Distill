"""The GitHub token: environment first, then the optional local_keys file; sent as a Bearer header, and never when there isn't one."""
from __future__ import annotations

import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (puts the project on sys.path)
from distill import github_auth as ga


class TokenTests(unittest.TestCase):
    def test_no_token_leaves_headers_alone(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(ga, "_LOCAL_TOKEN", ""):
            h = {"User-Agent": "x"}
            self.assertEqual(ga.github_headers(h), h)

    def test_environment_beats_the_local_file_and_is_sent_as_bearer(self):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "env"}, clear=True), mock.patch.object(ga, "_LOCAL_TOKEN", "file"):
            self.assertEqual(ga.github_token(), "env")
            self.assertEqual(ga.github_headers({"a": "b"}), {"a": "b", "Authorization": "Bearer env"})
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(ga, "_LOCAL_TOKEN", " file "):
            self.assertEqual(ga.github_token(), "file")

    def test_every_tool_check_goes_through_the_helper(self):
        import re
        from pathlib import Path
        src = (Path(ga.__file__).parent / "tools.py").read_text(encoding="utf-8")
        for m in re.finditer(r'headers = (.*vnd\.github\+json.*)', src):
            self.assertTrue(m.group(1).startswith("github_headers("), m.group(0))


if __name__ == "__main__":
    unittest.main()
