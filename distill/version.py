"""The one version number.

Everything else derives from it: `distill.constants.APP_VERSION` adds the `v` browsers and release tags use, and
`pyproject.toml` reads this module through setuptools' dynamic metadata instead of keeping a second copy. The release
workflow stamps this file from the tag (see .github/workflows/build.yml), so a Windows build, a Linux AppImage and a
run from a checkout all report the same version. Keep the assignment a plain string literal: setuptools reads it
without importing the package.
"""
__version__ = "1.5.0"
