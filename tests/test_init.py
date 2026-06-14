"""Unit tests for package metadata in pageup.__init__.

Keeps __version__ in sync with pyproject.toml [project].version and
cli --version output.  Bump both together when releasing.
"""

import tomllib
import unittest
from pathlib import Path

from pageup import __version__


class PackageTests(unittest.TestCase):
    """Package metadata: version string type and sync with pyproject.toml."""

    def test_version_is_non_empty_string(self) -> None:
        self.assertIsInstance(__version__, str)
        self.assertTrue(__version__)

    def test_version_matches_pyproject(self) -> None:
        # Single source of truth: pyproject.toml [project].version.
        # Also update pageup.__init__.__version__ when bumping releases.
        # tests/ → repo root (parents[0]=tests, parents[1]=pageup).
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with pyproject.open("rb") as fp:
            project_version = tomllib.load(fp)["project"]["version"]
        self.assertEqual(__version__, project_version)


if __name__ == "__main__":
    unittest.main()
