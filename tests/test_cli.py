"""Unit tests for pageup.cli.

Uses typer.testing.CliRunner to invoke the Typer app without a subprocess.
runner.run is mocked so tests never launch Yandex Browser/YandexDriver or Sberbrowser/sberdriver.

Validates:
    --version output
    pageup.__main__ import does not invoke the CLI (Sigma PYTHONPATH deploy)
    python3 -m pageup --version via subprocess (Sigma entry path)
    YYYYMMDD min_date parsing errors
    ParsingTask validation surfaced through CLI (group URL shape, name safety)
    --group-url trailing slash and query-string rejection
    Unsafe --name values (plain filename only: `/` and `\\`, not `.` or `..`,
    empty/whitespace-only, null bytes; dotted names such as backup.v1 allowed)
    --sleep-time minimum
    Default trusted_device=True vs --personal-device and explicit --trusted-device
    Correct kwargs passed to runner.run on success
"""

import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from pageup import __version__
from pageup.cli import app
from tests.fixtures import GROUP_URL


class CliTests(unittest.TestCase):
    """Invoke Typer app in-process via CliRunner (no subprocess)."""

    runner = CliRunner()

    def test_version(self) -> None:
        # is_eager --version must exit before requiring other options.
        result = self.runner.invoke(app, ["--version"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn(__version__, result.output)

    def test_main_module_importable_without_cli(self) -> None:
        # python3 -m pageup uses __main__; accidental import must not parse argv.
        importlib.import_module("pageup.__main__")

    def test_main_module_version_subprocess(self) -> None:
        # Sigma entry path: system python3 -m pageup --version
        result = subprocess.run(
            [sys.executable, "-m", "pageup", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)

    def test_invalid_min_date(self) -> None:
        # cli.main parses min_date before building ParsingTask.
        result = self.runner.invoke(
            app,
            ["--name", "x", "--group-url", GROUP_URL, "--min-date", "20251301"],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not a valid YYYYMMDD", result.output)

    def test_invalid_group_url(self) -> None:
        # Non-SberChat URLs fail ParsingTask.group_url validation via CLI.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "x",
                "--group-url",
                "https://example.com/bad",
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("group_url", result.output)

    # ── ParsingTask field validation (mirrors models.py validators) ──────────

    def test_invalid_group_url_trailing_slash(self) -> None:
        # Mirrors tools.group_url_pattern.fullmatch — trailing slash must fail.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "x",
                "--group-url",
                f"{GROUP_URL}/",
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("group_url", result.output)

    def test_invalid_group_url_query_string(self) -> None:
        # Browser URLs with ?query must fail — fullmatch rejects non-canonical copies.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "x",
                "--group-url",
                f"{GROUP_URL}?tab=messages",
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("group_url", result.output)

    def test_invalid_name_with_path_separator(self) -> None:
        # Prevents path traversal in write_dir/name.json output path.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "../evil",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_invalid_name_with_forward_slash_rejected(self) -> None:
        # Plain `/` in the filename must fail even without `..` traversal.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "evil/name",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_invalid_name_with_backslash_rejected(self) -> None:
        # Windows-style separators must fail the same as forward slashes.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "evil\\name",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_invalid_empty_name_rejected(self) -> None:
        # Whitespace-only names fail ParsingTask.validate_name after strip().
        result = self.runner.invoke(
            app,
            [
                "--name",
                "   ",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_invalid_dot_name_rejected(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "--name",
                ".",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_invalid_dot_dot_name_rejected(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "--name",
                "..",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_accepts_dotted_name(self) -> None:
        # Only exact "." and ".." are forbidden — other dots are valid filenames.
        with patch("pageup.cli.run") as mock_run:
            result = self.runner.invoke(
                app,
                [
                    "--name",
                    "backup.v1",
                    "--group-url",
                    GROUP_URL,
                    "--min-date",
                    "20250901",
                ],
            )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(mock_run.call_args.args[0].name, "backup.v1")

    def test_rejects_null_byte_in_name(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "--name",
                "evil\x00name",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("name", result.output)

    def test_sleep_time_below_minimum_rejected(self) -> None:
        # Typer enforces min=1 on --sleep-time before runner.run is called.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "x",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
                "--sleep-time",
                "0",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    # ── Successful dispatch to runner.run ────────────────────────────────────

    @patch("pageup.cli.run")
    def test_success_invokes_run_with_parsed_task(self, mock_run) -> None:
        # Patch at cli boundary so Typer wiring is tested without mocking runner internals.
        # --personal-device sets trusted_device=False (Yandex Browser + YandexDriver).
        result = self.runner.invoke(
            app,
            [
                "--name",
                "SberOS",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
                "--personal-device",
                "--sleep-time",
                "1",
                "--write-dir",
                "/tmp/out",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        mock_run.assert_called_once()
        task = mock_run.call_args.args[0]
        self.assertEqual(task.name, "SberOS")
        self.assertEqual(task.group_url, GROUP_URL)
        self.assertEqual(task.min_date.year, 2025)
        self.assertEqual(task.min_date.month, 9)
        self.assertEqual(task.min_date.day, 1)
        kwargs = mock_run.call_args.kwargs
        self.assertFalse(kwargs["trusted_device"])
        self.assertEqual(kwargs["sleep_time"], 1)
        self.assertEqual(kwargs["write_dir"], "/tmp/out")

    @patch("pageup.cli.run")
    def test_explicit_trusted_device_flag(self, mock_run) -> None:
        result = self.runner.invoke(
            app,
            [
                "--name",
                "SberOS",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
                "--trusted-device",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(mock_run.call_args.kwargs["trusted_device"])

    @patch("pageup.cli.run")
    def test_trusted_device_is_default(self, mock_run) -> None:
        # Sigma / Sberbrowser + sberdriver mode without explicit flag.
        result = self.runner.invoke(
            app,
            [
                "--name",
                "SberOS",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(mock_run.call_args.kwargs["trusted_device"])

    @patch("pageup.cli.run")
    def test_default_write_dir_expanded(self, mock_run) -> None:
        result = self.runner.invoke(
            app,
            [
                "--name",
                "SberOS",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        expected = str(Path("~/projects/pageup-results").expanduser())
        self.assertEqual(mock_run.call_args.kwargs["write_dir"], expected)

    @patch("pageup.cli.run")
    def test_explicit_tilde_write_dir_expanded(self, mock_run) -> None:
        result = self.runner.invoke(
            app,
            [
                "--name",
                "SberOS",
                "--group-url",
                GROUP_URL,
                "--min-date",
                "20250901",
                "--write-dir",
                "~/tmp/out",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        expected = str(Path("~/tmp/out").expanduser())
        self.assertEqual(mock_run.call_args.kwargs["write_dir"], expected)


if __name__ == "__main__":
    unittest.main()
