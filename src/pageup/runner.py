"""Selenium driver factory and main scroll-loop for pageup.

This module is responsible for everything that requires a live browser
session:

* **``create_driver``** — instantiates a ``selenium.webdriver.Chrome``
  instance configured for either a personal device (Yandex Browser +
  YandexDriver via ``config.YANDEX_DRIVER``) or a trusted Sigma machine
  (Sberbrowser + sberdriver on Sigma).

* **``run``** — opens the target group URL, waits through navigation (cert/OTP
  may happen there), runs a setup countdown for scroll/focus, then scrolls
  upward collecting messages until ``min_date`` is reached, a scroll safety
  limit fires, the operator interrupts the run, or an unexpected error occurs
  (partial output is written on interrupt or error).

Scroll mechanics
----------------
SberChat is a React SPA that loads chat history lazily as the viewport
scrolls upward.  Each iteration:

1. Captures the current ``driver.page_source`` (the full rendered DOM).
2. Parses it with BeautifulSoup (lxml backend for speed).
3. Extracts all visible message rows.
4. Scrolls up by 20 page-heights via ``ActionChains`` + ``Keys.PAGE_UP``.
5. Sleeps 1 second to allow React to render the newly loaded messages.

The loop terminates when:

* the oldest visible message predates ``min_date`` (``ParsingTask.is_done``);
* no message rows appear for ``MAX_EMPTY_SCROLL_ATTEMPTS`` iterations (~60 s);
* no new ``message_id`` values appear for ``MAX_STALL_SCROLL_ATTEMPTS``
  iterations (~60 s) while rows are still visible;
* the operator raises ``KeyboardInterrupt`` (partial or empty output is still
  written);
* an unexpected error is raised during the run (partial output is written,
  then the exception is re-raised).

Bug fixed vs original implementation
--------------------------------------
The original code skipped the entire final page snapshot on termination,
silently dropping any messages that were visible in the last screen and
still within the requested date range.  This implementation extends the
accumulator with only the in-range subset of the final batch before
writing.
"""

import time
from pathlib import Path

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from pageup.config import (
    MAX_EMPTY_SCROLL_ATTEMPTS,
    MAX_STALL_SCROLL_ATTEMPTS,
    PAGE_LOAD_TIMEOUT_SEC,
    SCROLL_PROGRESS_INTERVAL,
    SETUP_STATUS_INTERVAL_SEC,
    SBERBROWSER_BINARY,
    SBERBROWSER_DRIVER,
    YANDEX_BROWSER_BINARY,
    YANDEX_DRIVER,
)
from pageup.models import Message, ParsingTask

# Browser binaries: Sberbrowser + sberdriver (trusted), Yandex Browser +
# YandexDriver (personal).  Scroll limits MAX_EMPTY_SCROLL_ATTEMPTS and
# MAX_STALL_SCROLL_ATTEMPTS in runner.run().

_SETUP_HINT = "scroll to latest message, click inside chat"


def _log(message: str) -> None:
    """Print a newline status line prefixed for visibility in noisy terminals."""
    print(f"[pageup] {message}", flush=True)


def create_driver(*, trusted_device: bool) -> Chrome:
    """Return a configured ``Chrome`` WebDriver instance.

    Parameters
    ----------
    trusted_device:
        When ``True``, launch Sberbrowser on Sigma with the same WebDriver
        options as commit ``83b9d71`` (binary path + two ``--disable-features``
        flags, default page load, no timeout).

        When ``False``, launch Yandex Browser at ``YANDEX_BROWSER_BINARY`` with
        ``YANDEX_DRIVER``.  Chat history is limited to the last 7 days.

    Trusted Sigma mode: no ``page_load_strategy``, no ``set_page_load_timeout``
    — ``driver.get()`` blocks until navigation and cert/OTP finish.
    Personal mode uses ``eager`` plus ``PAGE_LOAD_TIMEOUT_SEC``.
    """
    if trusted_device:
        # Exact 83b9d71 Sigma driver options — do not add extra Chromium flags
        # (allowlists, excludeSwitches, etc.) without testing on Sigma first.
        options = Options()
        options.binary_location = SBERBROWSER_BINARY
        # Two separate flags (83b9d71).  Chromium keeps only the last value,
        # so SberSync is disabled but SberAuth stays enabled for Kerberos.
        # Never combine into "SberAuth,SberSync" — that disables both (e052dda).
        options.add_argument("--disable-features=SberAuth")
        options.add_argument("--disable-features=SberSync")
        service = Service(SBERBROWSER_DRIVER)
        driver = Chrome(options=options, service=service)
    else:
        # Personal device: Yandex Browser + YandexDriver (not Selenium Manager).
        options = Options()
        options.page_load_strategy = "eager"
        options.binary_location = YANDEX_BROWSER_BINARY
        service = Service(YANDEX_DRIVER)
        driver = Chrome(options=options, service=service)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SEC)

    return driver


def run(
    task: ParsingTask,
    *,
    trusted_device: bool,
    sleep_time: int,
    write_dir: str,
) -> None:
    """Execute a full collection run for *task*.

    Opens the group URL in a browser, waits through navigation (certificate
    and OTP if prompted), runs a setup countdown (*sleep_time*) for the
    operator to scroll to the most recent message and focus the chat, then
    scrolls upward collecting messages until the target date is reached,
    a scroll safety limit fires (~60 s with no rows or no new message IDs),
    or a ``KeyboardInterrupt`` or unexpected error is raised (partial or empty
    output is still written).

    Parameters
    ----------
    task:
        Parsed and validated ``ParsingTask`` instance containing the group
        URL, output name, and minimum date.
    trusted_device:
        Passed through to ``create_driver``.  See its docstring for details.
    sleep_time:
        Seconds to wait after navigation before the scroll loop begins.
        Cert/OTP usually finish during navigation; this window is mainly for
        scrolling to the latest message and focusing the chat window.
    write_dir:
        Destination directory for the output JSON file.  Created
        automatically if it does not exist.
    """
    driver: Chrome | None = None
    messages: list[Message] = []

    try:
        # ── Phase 1: prepare output dir and launch browser ────────────────────
        # write_dir is created before navigation so write_json never fails on missing path.
        try:
            Path(write_dir).mkdir(parents=True, exist_ok=True)
            _log(f"Launching browser (trusted_device={trusted_device})...")
            driver = create_driver(trusted_device=trusted_device)
            # ActionChains queues keyboard events; perform() executes and clears the queue.
            actions = ActionChains(driver)

            _log("Browser started; opening group URL...")
            if trusted_device:
                _log(
                    "Waiting for navigation to finish "
                    "(cert/OTP in browser; blocking, no timeout on Sigma)..."
                )
            else:
                _log(
                    f"Waiting for navigation to finish "
                    f"(cert/OTP in browser; timeout {PAGE_LOAD_TIMEOUT_SEC}s)..."
                )
            try:
                driver.get(task.group_url)
            except TimeoutException:
                # Personal-device only: eager page_load_strategy can return before
                # cert/OTP dialogs finish; we continue and let the operator auth
                # during navigation or the setup countdown window.
                _log(
                    f"Page load timed out after {PAGE_LOAD_TIMEOUT_SEC}s; "
                    "continuing (chat may still be usable)."
                )
            # Navigation may overlap cert/OTP; setup countdown follows for scroll/focus.
            _log(
                f"Navigation complete (title={driver.title!r}, "
                f"url={driver.current_url})"
            )

            _log(f"Proceeding to setup countdown ({sleep_time}s)...")
            _log(f"Selected mode: trusted_device={trusted_device}")
            _log("Task parameters:")
            print(task.model_dump_json(ensure_ascii=False, indent=4), flush=True)

            # Batches are newest-first per collect_messages(); extend_fresh dedupes
            # truthy in-range rows on extend; write_json orders chronologically.

            # ── Phase 2: setup countdown (operator scrolls to latest message) ───
            # Cert/OTP often finish during navigation; this window is for scroll
            # to the latest message and chat focus (required for PAGE_UP).
            for sec in range(sleep_time, 0, -1):
                if sec == sleep_time or sec % SETUP_STATUS_INTERVAL_SEC == 0:
                    _log(f"Setup: {sec}s remaining ({_SETUP_HINT})")
                time.sleep(1)
            _log("Parsing started...")

            # ── Phase 3: scroll loop ──────────────────────────────────────────────
            # Termination order inside each iteration (non-empty branch):
            #   1. is_done on oldest visible row — normal completion
            #   2. stall guard — history exhausted or min_date unreachable
            #   3. extend_fresh (deduped, in-range, truthy rows) and scroll up
            # seen_ids tracks visible message_id for stall detection; collected_ids
            # tracks unique truthy in-range IDs appended to messages.
            empty_scroll_attempts = 0
            seen_ids: set[str] = set()
            collected_ids: set[str] = set()
            stall_attempts = 0
            scroll_iteration = 0

            def extend_fresh(batch: list[Message]) -> None:
                fresh = [
                    m for m in batch
                    if m and not task.is_done(m) and m.message_id not in collected_ids
                ]
                if fresh:
                    collected_ids.update(m.message_id for m in fresh)
                    messages.extend(fresh)

            while True:
                scroll_iteration += 1
                # Parse the current viewport.
                soup = BeautifulSoup(driver.page_source, "lxml")
                new_messages = task.collect_messages(soup)

                # Guard: keep scrolling when the page has not rendered message rows
                # yet (can occur immediately after a large scroll).
                if not new_messages:
                    empty_scroll_attempts += 1
                    if (
                        empty_scroll_attempts % SCROLL_PROGRESS_INTERVAL == 0
                        or empty_scroll_attempts == MAX_EMPTY_SCROLL_ATTEMPTS
                    ):
                        _log(
                            f"No message rows yet "
                            f"(empty attempt {empty_scroll_attempts}/"
                            f"{MAX_EMPTY_SCROLL_ATTEMPTS}); "
                            f"{_SETUP_HINT}"
                        )
                    if empty_scroll_attempts >= MAX_EMPTY_SCROLL_ATTEMPTS:
                        # config.MAX_EMPTY_SCROLL_ATTEMPTS caps idle scrolling (60 s).
                        _log(
                            "Warning: no messages found after repeated scrolling; "
                            "stopping."
                        )
                        _finish(task, messages, write_dir)
                        break
                    actions.send_keys(Keys.PAGE_UP * 20)
                    actions.perform()
                    # Allow React to render newly loaded rows before re-parsing.
                    time.sleep(1)
                    # Empty branch does not reset stall_attempts — the two guards
                    # are independent (see config.MAX_STALL_SCROLL_ATTEMPTS).
                    continue

                empty_scroll_attempts = 0

                if task.is_done(new_messages[-1]):
                    # new_messages[-1] is the oldest row on screen (newest-first list).
                    # Must run before the stall guard: overlapping snapshots can
                    # repeat IDs while the oldest visible row is already before min_date.
                    # The oldest visible message is before min_date.  Capture
                    # only the in-range subset of the current batch (messages
                    # that are at or after min_date) before writing, so that
                    # the final page snapshot is not silently dropped.
                    extend_fresh(new_messages)
                    _finish(task, messages, write_dir)
                    break

                if any(m.message_id not in seen_ids for m in new_messages):
                    # At least one row in this viewport has not been seen before.
                    stall_attempts = 0
                    seen_ids.update(m.message_id for m in new_messages)
                else:
                    # Every visible row was seen in a prior iteration — scrolling
                    # is no longer revealing new history (chat top or min_date gap).
                    stall_attempts += 1
                    if (
                        stall_attempts % SCROLL_PROGRESS_INTERVAL == 0
                        or stall_attempts == MAX_STALL_SCROLL_ATTEMPTS
                    ):
                        _log(
                            f"No new messages "
                            f"(stall attempt {stall_attempts}/"
                            f"{MAX_STALL_SCROLL_ATTEMPTS})"
                        )
                    if stall_attempts >= MAX_STALL_SCROLL_ATTEMPTS:
                        _log(
                            "Warning: no new messages after repeated scrolling; "
                            "stopping (min_date may predate chat history)."
                        )
                        _finish(task, messages, write_dir)
                        break

                # Non-terminal batches: extend_fresh appends only new truthy in-range
                # rows.  Per-row is_done filter handles DOM order that is not strictly
                # chronological (see test_run_is_done_takes_precedence).
                extend_fresh(new_messages)

                if scroll_iteration % SCROLL_PROGRESS_INTERVAL == 0:
                    _log(
                        f"Scroll batch: collected so far={len(collected_ids)}, "
                        f"visible rows={len(new_messages)}"
                    )

                # Keys.PAGE_UP * 20 sends twenty PAGE_UP events in one batch —
                # a large upward jump to trigger SberChat's lazy history loading.
                actions.send_keys(Keys.PAGE_UP * 20)
                actions.perform()
                # Same 1 s pause after each successful parse+scroll iteration.
                time.sleep(1)

        except KeyboardInterrupt:
            # Manual stop during setup, driver launch, navigation, setup countdown, or scroll.
            # Useful when min_date is unreachable or the operator decides to stop early.
            _log("Interrupted by user.")
            _finish(task, messages, write_dir)
        except Exception as exc:
            # Persist partial collection on unexpected errors (Selenium, parser, etc.).
            _log(f"Fatal error: {exc}")
            _finish(task, messages, write_dir)
            raise
    finally:
        # Always close the browser when it was opened, including after errors.
        if driver is not None:
            driver.quit()


def _finish(task: ParsingTask, messages: list[Message], write_dir: str) -> None:
    """Write *messages* to disk and print a summary line.

    Called on normal completion, scroll safety abort, empty-DOM abort,
    KeyboardInterrupt, and unexpected errors — all exit paths persist partial
    collection before returning or re-raising.

    Delegates chronological ordering and sender patching to
    ``ParsingTask.write_json`` (see models.py postprocessor pipeline).
    The summary count matches JSON output (unique, truthy messages).
    """
    Path(write_dir).mkdir(parents=True, exist_ok=True)
    unique = ParsingTask._unique_reversed(messages)
    _log(f"Messages collected: {len(unique)}")
    task.write_json(messages, write_dir)
