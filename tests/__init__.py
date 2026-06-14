"""Unit test package for pageup.

Tests live alongside fixtures that mimic SberChat DOM snippets.  They validate
parsing, CLI wiring, and runner behaviour without launching a real browser.

Module map:
    test_models.py  — ParsingTask HTML parsing, quote normalisation, JSON output,
                      malformed data-message-date skip, null-byte name rejection
    test_runner.py  — scroll loop, create_driver wiring, page-load timeout,
                      progress logging, KeyboardInterrupt and fatal-error partial
                      write, regression (final-batch tail), empty-scroll and
                      stall abort, is_done precedence, in-range extend filter,
                      stall count regression (no duplicate inflation)
    test_cli.py     — Typer CLI (--version, __main__ import guard, python3 -m
                      subprocess, group-url/name validation incl. empty name,
                      path separators, dot names, null bytes, dotted names,
                      run dispatch)
    test_tools.py   — cleaner pipeline, quote finalisation, URL/timezone constants
    test_config.py  — compile-time constants smoke checks
    test_init.py    — package version sync with pyproject.toml
    fixtures.py     — synthetic SberChat DOM HTML builders (imports config selectors)

Run the full suite: ``uv run python -m unittest discover -s tests -v``.
"""
