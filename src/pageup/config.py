"""Compile-time constants for the pageup package.

All values are declared ``Final`` so that static type-checkers can flag
accidental reassignments.  Nothing in this module is intended to be
user-configurable at runtime — operational parameters (trusted-device
mode, sleep time, output directory) are exposed as CLI options in
``pageup.cli``.

Consumers:
    runner.py   — SBERBROWSER_*, YANDEX_BROWSER_BINARY, YANDEX_DRIVER,
                  MAX_EMPTY_SCROLL_ATTEMPTS, MAX_STALL_SCROLL_ATTEMPTS,
                  MAIN_SCROLL_SLEEP_SEC,
                  PAGE_LOAD_TIMEOUT_SEC, SETUP_STATUS_INTERVAL_SEC,
                  SCROLL_PROGRESS_INTERVAL; imports ``pageup.threads`` for
                  enrich_fresh_threads, download_fresh_images, and
                  prepare_main_feed_scroll
    threads.py  — MAX_THREAD_STALL_ATTEMPTS, MAX_THREAD_N_MINUS_ONE_STALL,
                  MAX_THREAD_BOOTSTRAP_DOWN_STEPS,
                  MAX_THREAD_ROW_SEARCH_STEPS, MAX_THREAD_OPEN_ATTEMPTS,
                  MAX_THREAD_PANEL_CLOSE_ATTEMPTS,
                  THREAD_PANEL_OPEN_TIMEOUT_SEC,
                  THREAD_PANEL_CLOSE_TIMEOUT_SEC, SCROLL_PROGRESS_INTERVAL,
                  THREAD_* selectors, MSG_LIST_CONTAINER_CLS,
                  MSG_ATTACHMENT_CLS, MSG_IMAGE_WRAP_CLS,
                  MSG_IMAGE_MEDIA_CLICKABLE_SUBCLS,
                  IMAGE_GALLERY_* selectors/timeouts, MIN_FULL_IMAGE_PX
    models.py   — every MSG_* / QUOTE_* / THREAD_* selector when parsing HTML

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
   but no new ``message_id`` values appear; ``MAIN_SCROLL_SLEEP_SEC`` is the
   sleep duration between consecutive scroll steps (0.5 s by default);
   ``MAX_THREAD_STALL_ATTEMPTS`` caps thread-panel inner scroll (JS
   ``scrollTop`` ~five viewport heights per step with 1 s pauses);
   ``MAX_THREAD_N_MINUS_ONE_STALL`` provides a fast exit when the collected
   count is exactly N−1 and has not changed for this many steps
   (deleted/unparseable replies will never appear);
   ``MAX_THREAD_BOOTSTRAP_DOWN_STEPS`` scrolls a newly opened panel to its
   bottom so lazy replies mount; ``MAX_THREAD_ROW_SEARCH_STEPS`` scrolls the
   main feed to locate a virtualized row before opening its thread bubble;
   ``MAX_THREAD_OPEN_ATTEMPTS`` caps how many times the same bubble is opened
   per run when collection keeps failing;
   ``THREAD_PANEL_OPEN_TIMEOUT_SEC`` waits for panel open after bubble
   clicks; ``THREAD_PANEL_CLOSE_TIMEOUT_SEC`` waits after each close attempt;
   ``MAX_THREAD_PANEL_CLOSE_ATTEMPTS`` caps close-button + main-feed refocus in
   ``_close_panel``.
   ``PAGE_LOAD_TIMEOUT_SEC`` limits ``driver.get()`` on personal-device SPA
   routes only (Sigma uses blocking navigation with no timeout).
   ``SETUP_STATUS_INTERVAL_SEC`` and ``SCROLL_PROGRESS_INTERVAL`` control
   how often setup and scroll progress lines are printed.
"""

import os
from typing import Final

# ── Scroll loop ───────────────────────────────────────────────────────────────

# Safety limit used in runner.run(): if the DOM never yields parseable message
# rows (wrong page, chat not loaded, focus lost), we stop instead of scrolling
# forever.  Each failed attempt scrolls up once and sleeps MAIN_SCROLL_SLEEP_SEC
# → 30 s total at 0.5 s default.
MAX_EMPTY_SCROLL_ATTEMPTS: Final[int] = 60

# Safety limit used in runner.run(): if scrolling no longer discovers unseen
# message_id values (e.g. min_date predates chat history), we stop instead of
# scrolling forever.  Each stall iteration scrolls up once and sleeps
# MAIN_SCROLL_SLEEP_SEC → 4 s total at 0.5 s default.
MAX_STALL_SCROLL_ATTEMPTS: Final[int] = 8

# runner.run(): sleep between consecutive scroll iterations (both empty-DOM and
# non-empty branches).  0.5 s gives React ~two render cycles to mount new rows;
# 1 s was the original value but is unnecessarily slow for well-loaded chats.
MAIN_SCROLL_SLEEP_SEC: Final[float] = 0.5

# runner.create_driver(trusted_device=False): Selenium page-load timeout for
# driver.get() on SPA hash routes.  Trusted Sigma mode does not set this —
# driver.get() blocks until navigation and cert/OTP finish.
PAGE_LOAD_TIMEOUT_SEC: Final[int] = 120

# runner.run(): newline status interval during setup countdown and scroll loop.
SETUP_STATUS_INTERVAL_SEC: Final[int] = 10
SCROLL_PROGRESS_INTERVAL: Final[int] = 10

# threads._collect_thread_panel_replies(): inner scroll cap when a thread panel stops
# revealing new reply message_id values (~60 s total; JS scrollTop ~five viewport
# heights per step with 1 s pauses — same timing cadence as the main loop).
MAX_THREAD_STALL_ATTEMPTS: Final[int] = 60

# threads._collect_thread_panel_replies(): fast-exit when the collected count is
# exactly N−1 and has not changed for this many consecutive stall steps.  The
# bubble count can include deleted or unparseable replies that will never appear,
# so waiting the full 60 s budget is wasteful.  Set ≥ MAX_THREAD_STALL_ATTEMPTS
# to disable the fast exit and always wait the full budget.
MAX_THREAD_N_MINUS_ONE_STALL: Final[int] = 5

# threads._bootstrap_thread_panel_scroll(): scroll down after panel open so lazy-
# loaded replies below the root message mount before the upward collection pass.
MAX_THREAD_BOOTSTRAP_DOWN_STEPS: Final[int] = 15

# threads._find_row_in_main_feed(): scroll the main feed up then down (~0.25 s per
# step) searching for a message row SberChat removed from the live DOM.
MAX_THREAD_ROW_SEARCH_STEPS: Final[int] = 20

# threads.enrich_fresh_threads(): open the same bubble at most this many times
# per run when collection keeps failing while the group chat stays open.
MAX_THREAD_OPEN_ATTEMPTS: Final[int] = 3

# threads._open_thread_and_collect(): wait for panel to appear after bubble click.
THREAD_PANEL_OPEN_TIMEOUT_SEC: Final[int] = 10

# threads._close_panel(): wait for panel to disappear after each close attempt.
# Shorter than open timeout so fallbacks (main-feed focus) run sooner.
THREAD_PANEL_CLOSE_TIMEOUT_SEC: Final[int] = 3

# threads._close_panel(): close-button + main-feed refocus attempts before giving up.
MAX_THREAD_PANEL_CLOSE_ATTEMPTS: Final[int] = 3

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

# Message body text (block or inline).  Thread replies often use a <div> wrapper.
MSG_CONTENT_SEL: Final[str] = ".BlockMessageStyleComponent-BlockMessageText__cls1"

# Main chat infinite list — excludes MessageList inside ThreadContent when panel open.
MSG_LIST_CONTAINER_CLS: Final[str] = "MessageList-MessagesContainer__cls1"

# ── Thread / discussion panel ─────────────────────────────────────────────────

# Green "N ответа" bubble under a main-channel message (click target).
THREAD_BUBBLE_CLS: Final[str] = "MessageThreadPanel-MessageThreadPanelWrapper__cls1"

# Reply count label inside the bubble (e.g. "3 ответа").
THREAD_BUBBLE_TITLE_CLS: Final[str] = "MessageThreadPanel-MessageThreadPanelTitle__cls1"

# Side panel container opened by the bubble ("Обсуждение").
THREAD_PANEL_CLS: Final[str] = "ThreadContent-ThreadWrapper__cls1"

# Close button in the thread panel header.
THREAD_PANEL_CLOSE_SEL: Final[str] = 'button[aria-label="Закрыть окно"]'

# ── File / media attachments ──────────────────────────────────────────────────

# Wrapper around a file or image attachment block inside a message row.
MSG_ATTACHMENT_CLS: Final[str] = "BlockMessageStyleComponent-DocumentBlock__cls1"

# Inner wrapper for inline image attachments.  Present only when the attachment
# is an image (no filename cell); used by _collect_attachments to distinguish
# image blocks from file blocks, and by download_fresh_images to scope the
# click target search within a message row.
MSG_IMAGE_WRAP_CLS: Final[str] = "MessageImage-MessageImageWrapper__cls1"

# Inline video attachments share MessageMedia clickables but use VideoMedia /
# PhotoVideoMedia wrappers — never treat them as image slots or click targets.
MSG_VIDEO_MEDIA_PREFIXES: Final[tuple[str, ...]] = (
    "VideoMedia-",
    "PhotoVideoMedia-",
)

# Clickable overlay on inline chat images (opens the full-screen gallery).
# Modifier class on MessageMedia-MessageMediaWrapper; matched via substring in
# threads._image_click_targets because it has no __clsN suffix.
MSG_IMAGE_MEDIA_CLICKABLE_SUBCLS: Final[str] = (
    "MessageMedia-MessageMediaWrapper__clickable"
)

# Full-screen image gallery opened by clicking an inline attachment.
# Overlays are position:fixed — never gate visibility on offsetParent.
IMAGE_GALLERY_WRAP_CLS: Final[str] = "ImageGalleryUi-ImageGalleryUiWrapper__cls1"
IMAGE_GALLERY_V2_WRAP_CLS: Final[str] = "ImageGalleryV2-ImageGalleryContent__cls1"
IMAGE_GALLERY_V2_DIALOG_CLS: Final[str] = "ImageGalleryV2-DialogImage__cls1"
IMAGE_GALLERY_WRAP_CLASSES: Final[tuple[str, ...]] = (
    IMAGE_GALLERY_WRAP_CLS,
    IMAGE_GALLERY_V2_WRAP_CLS,
    IMAGE_GALLERY_V2_DIALOG_CLS,
)
IMAGE_GALLERY_IMG_CLS: Final[str] = "Image-ImageElement__cls1"
IMAGE_GALLERY_TOPBAR_CLS: Final[str] = "ImageGalleryUi-ImageGalleryTopBar__cls1"
# Close icon in the gallery top bar (first button in DOM; flex-end renders it rightmost).
IMAGE_GALLERY_CLOSE_ICON_ARIA: Final[str] = "sbc_line_close_line"
# Download control inside the open gallery — never click; Chrome saves image.png → ~/Downloads.
IMAGE_GALLERY_DOWNLOAD_BTN_SEL: Final[str] = (
    '[class*="ImageGalleryUi-ImageGalleryUiWrapper"], '
    '[class*="ImageGalleryV2-DialogImage"] '
    'button[aria-label="Скачать"]'
)

# Gallery timing: FETCH covers open+load polling in _FETCH_GALLERY_IMAGE_JS; CLOSE
# is used by _close_image_gallery.  OPEN and LOAD are summands of FETCH only.
IMAGE_GALLERY_OPEN_TIMEOUT_SEC: Final[int] = 10
IMAGE_GALLERY_LOAD_TIMEOUT_SEC: Final[int] = 15
IMAGE_GALLERY_FETCH_TIMEOUT_SEC: Final[int] = (
    IMAGE_GALLERY_OPEN_TIMEOUT_SEC + IMAGE_GALLERY_LOAD_TIMEOUT_SEC
)
IMAGE_GALLERY_CLOSE_TIMEOUT_SEC: Final[int] = 3

# SberChat inline thumbnails (FastThumb) are 90 px on the long edge; reject
# payloads at or below this size so we never persist preview blobs as originals.
MIN_FULL_IMAGE_PX: Final[int] = 90

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
