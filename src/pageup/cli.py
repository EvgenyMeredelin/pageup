"""Command-line interface for pageup.

This module defines the ``pageup`` CLI command that is installed as a
console entry point by ``pyproject.toml``.  It is the primary user-facing
surface of the package; successful runs write ``{write-dir}/{name}.json``.

Execution flow (``main()``):

1. Parse ``--min-date`` from YYYYMMDD and attach ``moscow_timezone``.
2. Build a ``ParsingTask`` (Pydantic validates ``name`` and ``group_url``).
3. Call ``runner.run()`` with device mode, setup countdown, and output directory.

``--trusted-device`` selects Sberbrowser + sberdriver on Sigma
(``config.SBERBROWSER_BINARY``, ``config.SBERBROWSER_DRIVER``).
``--personal-device`` selects Yandex Browser + YandexDriver
(``config.YANDEX_BROWSER_BINARY``, ``config.YANDEX_DRIVER``).
See ``runner.create_driver``.

``runner.run`` owns the browser session; this module only handles argv parsing
and early validation errors before Selenium starts.

Usage examples::

    # Trusted device on Sigma (default) — PYTHONPATH + system python3:
    python3 -m pageup \\
        --name "AI in Dev Community" \\
        --group-url "https://sberchat.sberbank.ru/#/chat/group796209083" \\
        --min-date 20200101

    # Personal device — Yandex Browser + YandexDriver (install-yandexdriver.sh):
    pageup \\
        --name "AI in Dev Community" \\
        --group-url "https://sberchat.sberbank.ru/#/chat/group796209083" \\
        --min-date 20260613 \\
        --personal-device

All parameters that initialise ``ParsingTask`` are exposed as CLI options.
Operational parameters (device mode, startup delay, output directory) are
additional options with sensible defaults.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from pageup import __version__
from pageup.models import ParsingTask
from pageup.runner import run
from pageup.tools import moscow_timezone


# Typer + Pydantic boundary: CLI parses argv strings; ParsingTask validates
# business rules (group URL shape, safe output filename).  Typer does not
# re-validate --name beyond type=str — field_validator on ParsingTask is the gate.

# ── Typer application ─────────────────────────────────────────────────────────
# Single-command CLI: @app.command() on main() registers it as the default
# subcommand when users run `pageup` (no subcommand name required).

app = typer.Typer(
    name="pageup",
    help="Collect SberChat group messages and write them to a JSON file.",
    # Show default values in --help output.
    context_settings={"show_default": True},
)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when --version is passed."""
    if value:
        typer.echo(f"pageup {__version__}")
        # typer.Exit stops option parsing without treating it as a crash.
        raise typer.Exit()


# ── Main command ──────────────────────────────────────────────────────────────

@app.command()
def main(
    # ── ParsingTask parameters ────────────────────────────────────────────────
    name: Annotated[
        str,
        typer.Option(
            "--name", "-n",
            help=(
                "Output file {write-dir}/{name}.json (e.g. --name \"AI in Dev Community\" → "
                "~/projects/pageup-results/AI in Dev Community.json).  Plain filename only — no `/` or `\\`, "
                "not `.` or `..`, no null bytes, not empty or whitespace-only; "
                "leading/trailing spaces are stripped."
            ),
        ),
    ],
    group_url: Annotated[
        str,
        typer.Option(
            "--group-url",
            help=(
                "Full URL of the SberChat group to collect, e.g. "
                "https://sberchat.sberbank.ru/#/chat/group796209083.  "
                "Must match exactly — no trailing slash or query string."
            ),
        ),
    ],
    min_date: Annotated[
        str,
        typer.Option(
            "--min-date",
            help=(
                "Earliest date to collect messages from, in YYYYMMDD format "
                "(e.g. 20250901 for 1 September 2025).  Interpreted as midnight "
                "Moscow time on that calendar day; messages from the full day are "
                "included.  Collection stops when any of these occur: the oldest "
                "visible message predates this cutoff; ~60 s of scrolling with no "
                "parseable message rows; or ~60 s of scrolling with no new "
                "message IDs while history cannot reach this date."
            ),
        ),
    ],
    # ── Operational parameters ────────────────────────────────────────────────
    # Typer bool flag: default True means --trusted-device; --personal-device
    # flips to False (Yandex Browser + YandexDriver, 7 days history limit).
    trusted_device: Annotated[
        bool,
        typer.Option(
            "--trusted-device/--personal-device",
            help=(
                "Device mode: --trusted-device launches Sberbrowser on a Sigma "
                "machine (default); --personal-device uses Yandex Browser and "
                "YandexDriver on a personal (non-trusted) machine.  Personal "
                "mode limits chat history to the last 7 days; run "
                "scripts/install-yandexdriver.sh once before first use.  "
                "Trusted mode requires Sberbrowser and sberdriver at "
                "the paths in pageup.config."
            ),
        ),
    ] = True,
    sleep_time: Annotated[
        int,
        typer.Option(
            "--sleep-time",
            min=1,
            help=(
                "Seconds to wait after navigation before the scroll loop starts.  "
                "Use this window to scroll to the latest message and focus the chat "
                "(cert/OTP usually happen during navigation)."
            ),
        ),
    ] = 60,
    write_dir: Annotated[
        str,
        typer.Option(
            "--write-dir",
            help="Directory in which to write the output JSON file.  Created if missing.",
        ),
    ] = "~/projects/pageup-results",
    # ── Meta ──────────────────────────────────────────────────────────────────
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,  # process before any other validation
            help="Print the pageup version and exit.",
        ),
    ] = False,
) -> None:
    """Collect SberChat group messages and write them to a JSON file."""

    # ── Parse min_date from YYYYMMDD string ───────────────────────────────────
    # strptime validates calendar shape only; timezone is applied below.
    try:
        parsed_dt = datetime.strptime(min_date, "%Y%m%d")
    except ValueError:
        typer.echo(
            f"Error: --min-date '{min_date}' is not a valid YYYYMMDD date.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Attach the Moscow timezone so that the datetime is comparable with
    # the timezone-aware timestamps parsed from SberChat messages.
    # Midnight on the given calendar day in Moscow — matches message timestamps.
    aware_min_date = parsed_dt.replace(tzinfo=moscow_timezone)

    # ── Build and validate the task ───────────────────────────────────────────
    # ParsingTask rejects bad group URLs and unsafe output filenames.
    # ValidationError here surfaces Pydantic field_validator messages to the user.
    try:
        task = ParsingTask(
            name=name,
            group_url=group_url,
            min_date=aware_min_date,
        )
    except ValidationError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)

    # ── Run the browser session ───────────────────────────────────────────────
    # runner.run may raise from Selenium after writing partial output on failure.
    # Typer invokes main() when the installed `pageup` console script runs,
    # or when `python3 -m pageup` loads pageup.__main__ (Sigma deploy).
    # expanduser() so ~/… defaults and CLI paths resolve before mkdir/write.
    run(
        task,
        trusted_device=trusted_device,
        sleep_time=sleep_time,
        write_dir=str(Path(write_dir).expanduser()),
    )
