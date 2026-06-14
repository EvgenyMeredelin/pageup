"""Compile-time constants for the pageup package.

All values are declared ``Final`` so that static type-checkers can flag
accidental reassignments.  Nothing in this module is intended to be
user-configurable at runtime — operational parameters (trusted-device
mode, sleep time, output directory) are exposed as CLI options in
``pageup.cli``.

Consumers:
    runner.py   — SBERBROWSER_*, YANDEX_BROWSER_BINARY, YANDEX_DRIVER,
                  MAX_EMPTY_SCROLL_ATTEMPTS, MAX_STALL_SCROLL_ATTEMPTS,
                  PAGE_LOAD_TIMEOUT_SEC, SETUP_STATUS_INTERVAL_SEC,
                  SCROLL_PROGRESS_INTERVAL
    models.py   — every MSG_* / QUOTE_* / ATTACH_* selector when parsing HTML

Three groups of constants are defined here:

1. **Browser paths** — filesystem locations of browser binaries:

   * **Sberbrowser** (trusted-device / Sigma) — binary and sberdriver
     (ChromeDriver-compatible wrapper).
   * **Yandex Browser** (personal-device) — browser binary and matching
     **YandexDriver** (not stock ChromeDriver — Selenium Manager cannot pair
     them).  Install YandexDriver from https://github.com/yandex/YandexDriver
     (first three version components must match Yandex Browser, e.g. 26.4.1.x).

   If paths differ on your machine, edit the constants below.  No
   virtual-environment rebuild is required — changes apply on the next
   ``pageup`` run.

2. **CSS-module selectors** — class names and CSS selectors extracted
   from the SberChat SPA DOM.  SberChat uses CSS Modules, which appends
   a ``__cls1`` / ``__cls2`` suffix to every class name.  BeautifulSoup's
   ``class_=`` parameter does a set-membership check, so matching on
   ``__cls1`` is sufficient even when the element also carries ``__cls2``.

3. **Scroll safety and runner timing** — ``MAX_EMPTY_SCROLL_ATTEMPTS`` caps
   how long the runner keeps scrolling when no message rows appear in the
   DOM; ``MAX_STALL_SCROLL_ATTEMPTS`` caps scrolling when rows are visible
   but no new ``message_id`` values appear.  ``PAGE_LOAD_TIMEOUT_SEC`` limits
   ``driver.get()`` on personal-device SPA routes only (Sigma uses blocking
   navigation with no timeout).  ``SETUP_STATUS_INTERVAL_SEC`` and
   ``SCROLL_PROGRESS_INTERVAL`` control how often setup and scroll progress
   lines are printed.
"""

import os
from typing import Final

# ── Scroll loop ───────────────────────────────────────────────────────────────

# Safety limit used in runner.run(): if the DOM never yields parseable message
# rows (wrong page, chat not loaded, focus lost), we stop instead of scrolling
# forever.  Each failed attempt scrolls up once and sleeps 1 s → 60 s total.
MAX_EMPTY_SCROLL_ATTEMPTS: Final[int] = 60

# Safety limit used in runner.run(): if scrolling no longer discovers unseen
# message_id values (e.g. min_date predates chat history), we stop instead of
# scrolling forever.  Each stall iteration scrolls up once and sleeps 1 s → 60 s.
MAX_STALL_SCROLL_ATTEMPTS: Final[int] = 60

# runner.create_driver(trusted_device=False): Selenium page-load timeout for
# driver.get() on SPA hash routes.  Trusted Sigma mode does not set this —
# driver.get() blocks until navigation and cert/OTP finish.
PAGE_LOAD_TIMEOUT_SEC: Final[int] = 120

# runner.run(): newline status interval during setup countdown and scroll loop.
SETUP_STATUS_INTERVAL_SEC: Final[int] = 10
SCROLL_PROGRESS_INTERVAL: Final[int] = 10

# ── Sberbrowser paths (Sigma / trusted-device mode only) ─────────────────────

# Absolute path to the Sberbrowser executable on a Sigma machine.
# Used by runner.create_driver() when --trusted-device is active (default).
SBERBROWSER_BINARY: Final[str] = "/opt/Sberbrowser/sberbrowser/sberbrowser"

# sberdriver — ChromeDriver-compatible wrapper shipped with Sberbrowser.
# Passed to selenium.webdriver.chrome.service.Service().
SBERBROWSER_DRIVER: Final[str] = "/opt/sberdriver/sberdriver"

# ── Yandex Browser + YandexDriver (personal-device mode only) ────────────────

# Default Linux package entry point for Yandex Browser.
# Used by runner.create_driver() when --personal-device is active.
# On other OS installs, edit this constant (no venv rebuild required).
YANDEX_BROWSER_BINARY: Final[str] = "/usr/bin/yandex-browser"

# YandexDriver — ChromeDriver fork matched to Yandex Browser versions.
# Selenium Manager downloads stock chromedriver and fails with "Wrong
# browser/driver version" for Yandex; use YandexDriver instead.
# Install: scripts/install-yandexdriver.sh (defaults to ~/.local/bin/yandexdriver).
# expanduser() resolves ~ at import time so Selenium Service gets an absolute path.
YANDEX_DRIVER: Final[str] = os.path.expanduser("~/.local/bin/yandexdriver")

# ── Message row ───────────────────────────────────────────────────────────────

# Outermost <div> for each chat message in the infinite list.
# ParsingTask.collect_messages() finds all such divs, then reads
# data-message-id and data-message-date from each (see models.py).
MSG_WRAP_CLS: Final[str] = "MessageRow-MessageRowWrapper__cls1"

# Profile link inside the message header; href is usually "#/chat/private…".
# Compound CSS selector (tag + class); used with select_one(), not class_=.
# models._get_sender_url() prepends SBERCHAT_BASE_URL from tools.py.
MSG_SENDER_URL_SEL: Final[str] = "a.MessageTitle-LinkToAuthor__cls1"

# Display name text node (e.g. "Иван Петров").
MSG_SENDER_NAME_CLS: Final[str] = "CustomStatusIcon-ChatHeaderStatusTitle__cls1"

# Lexical-rendered body text; a message may contain multiple matching spans.
# Compound selector; select() in _get_message_content() may return several nodes.
MSG_CONTENT_SEL: Final[str] = "span.BlockMessageStyleComponent-BlockMessageText__cls1"

# ── File / media attachments ──────────────────────────────────────────────────

# Wrapper around a file or image attachment block inside a message row.
MSG_ATTACHMENT_CLS: Final[str] = "BlockMessageStyleComponent-DocumentBlock__cls1"

# Filename label shown in the attachment cell (e.g. "report.pdf").
ATTACH_NAME_CLS: Final[str] = "Title-TitleContent__cls1"

# Human-readable size subtitle (e.g. "1.2 МБ"); may be absent for inline images.
ATTACH_SIZE_CLS: Final[str] = "MessageFileCellV2-FileSubtitle__cls1"

# ── Quoted / reply messages ───────────────────────────────────────────────────

# Embedded "reply to …" preview block inside a message.
QUOTE_WRAP_CLS: Final[str] = "Reply-ReplyWrapper__cls1"

# Quoted author's name inside the reply header.
QUOTE_SENDER_NAME_CLS: Final[str] = "MessageTitle-MessageReplyTitleWrapper__cls1"

# Text portion of a quote.  Live DOM often duplicates the sender name inside
# this element; tools.finalize_quote_content strips it using the title field.
# When missing, models._get_quote_content() falls back to the whole wrapper
# and relies on tools.cleaner plus finalize_quote_content.
QUOTE_CONTENT_SEL: Final[str] = "div.Reply-MessageTextContent__cls1"
