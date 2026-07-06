"""pageup — SberChat group message and thread-reply collector.

Collects messages and discussion-thread replies from a SberChat group by
driving a Chromium-based browser via Selenium, parsing the rendered HTML with
BeautifulSoup, and writing the results to JSON.

Typical usage via the installed CLI entry point (dev) or ``python3 -m pageup``
on Sigma (``PYTHONPATH`` to the library bundle)::

    # Trusted device on Sigma (default) — Sberbrowser + sberdriver, full history:
    python3 -m pageup --name "AI in Dev Community" \\
             --group-url "https://sberchat.sberbank.ru/#/chat/group796209083" \\
             --min-date 20200101

    # Personal device — Yandex Browser + YandexDriver; 7 days history cap:
    pageup --name "AI in Dev Community" \\
             --group-url "https://sberchat.sberbank.ru/#/chat/group796209083" \\
             --min-date 20260613 \\
             --personal-device

Device modes (see ``runner.create_driver`` and ``config`` browser paths):

    --trusted-device (default) — Sberbrowser + sberdriver on Sigma; full history.
    --personal-device          — Yandex Browser + YandexDriver; 7 days history cap.

Package layout (see also ``pyproject.toml``):
    __main__.py — ``python3 -m pageup`` entry (Sigma deploy)
    cli.py      — Typer entry point; parses argv and calls runner.run()
    runner.py   — Selenium session, PAGE_UP scroll loop, uncollected thread enrichment, panel close + main-feed focus, status lines
    threads.py  — discussion-panel open/scroll/close (JS scroll/focus, no message
                  body clicks), bubble/close JS click, thread reply enrichment
    models.py   — Pydantic models and HTML parsing logic
    config.py   — CSS selectors and browser paths (compile-time constants)
    tools.py    — Shared text cleaning, quote normalisation, URL validation

End-to-end data path:
    CLI argv → ParsingTask → runner scroll loop → page_source HTML
    → collect_messages (BeautifulSoup + config selectors)
    → enrich_fresh_threads (open uncollected discussion panels, collect replies)
    → prepare_main_feed_scroll (close panel, focus main feed before PAGE_UP)
    → accumulated Message list → write_json (dedupe, sort, patch) → JSON file
"""

# Public version string; mirrored in pyproject.toml [project].version and
# printed by ``pageup --version`` or ``python3 -m pageup --version`` (cli._version_callback).
__version__ = "0.2.1"
