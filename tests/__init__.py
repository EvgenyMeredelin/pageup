"""Unit test package for pageup.

Tests live alongside fixtures that mimic SberChat DOM snippets.  They validate
parsing, CLI wiring, and runner behaviour without launching a real browser.

Module map:
    test_models.py  — ParsingTask HTML parsing, thread fields, main/thread scope
                      isolation, multi-file attachments, Message.__bool__,
                      real DOM excerpt regression, null-byte name rejection
    test_threads.py — discussion-panel Selenium workflow (_close_panel with
                      main-feed refocus fallback, _find_row_in_main_feed,
                      _find_visible_panel,
                      _wait_panel_for_parent, _panel_soup, _wait_panel_closed,
                      no Escape, JS click/focus/scroll helpers,
                      prepare_main_feed_scroll, enrich including transient-failure
                      retry, oldest-first ordering, and chat-closed batch skip;
                      mocked driver)
    test_runner.py  — scroll loop, create_driver wiring, prepare-before-scroll,
                      page-load timeout, progress logging, KeyboardInterrupt and
                      fatal-error partial write, regression (final-batch tail),
                      empty-scroll and stall abort, is_done precedence, in-range
                      extend filter, stall count regression (no duplicate inflation)
    test_cli.py     — Typer CLI (--version, __main__ import guard, python3 -m
                      subprocess, group-url/name validation incl. empty name,
                      path separators, dot names, null bytes, dotted names,
                      run dispatch)
    test_tools.py   — cleaner pipeline, quote finalisation, URL/timezone constants
    test_config.py  — compile-time constants smoke checks
    test_init.py    — package version sync with pyproject.toml
    fixtures.py     — synthetic SberChat DOM HTML builders (imports config selectors)
    data/           — real DOM excerpts for regression tests (e.g. thread panel snapshot)

Run the full suite: ``uv run python -m unittest discover -s tests -v``.
"""
