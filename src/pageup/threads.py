"""Selenium workflow for collecting SberChat thread (discussion) replies and
downloading image attachments.

When a main-channel message shows a green "N ответа" bubble, replies live in
a lazy-loaded side panel ("Обсуждение").  This module opens that panel, scrolls
its message list, parses replies via ``ParsingTask.collect_thread_reply_entries``,
and returns enriched ``Message`` copies with ``thread_replies`` populated.
Already-collected message IDs (``thread_collected_ids``) are skipped once a
thread fully succeeds, the group chat closes, or ``MAX_THREAD_OPEN_ATTEMPTS``
is exhausted.  Partial and transient failures leave the ID retryable.

``download_fresh_images`` downloads image attachments for main-feed messages by
clicking each inline image preview (``MessageMedia-MessageMediaWrapper__clickable``)
to open SberChat's full-screen gallery, fetching the full-resolution ``<img>`` in-page
(``blob:``, ``https:``, or ``data:`` via ``execute_async_script``),
saving the result to ``{write_dir}/attachments/`` with an extension inferred
from magic bytes (falling back to the data URL MIME type), and returning updated
``Message`` copies with the saved filename replacing the ``None`` slot in
``attachments``.  Inline feed thumbnails (~90 px) are never saved — only gallery
images whose pixel dimensions exceed ``MIN_FULL_IMAGE_PX``.  Logs use
``🖼️ Image: saved …`` on success and ``⚠️ Image:`` on failures.  Thread-reply image
downloads happen inside ``_open_thread_and_collect`` while the thread panel is
still open.

``prepare_main_feed_scroll`` closes any open image gallery and thread panel,
then focuses the main feed so ``PAGE_UP`` in the runner scroll loop targets
the chat history, not the thread pane.  Before opening a bubble, ``_find_row_in_main_feed`` scrolls the feed until
SberChat mounts the row (virtualized lists drop nodes that are off-screen).
Row lookup and bubble clicks re-query the DOM by ``message_id`` (converted
from normalized JSON form back to the live ``|`` separator via
``dom_message_id``) with stale-element and missing-row retries — cached
WebElement handles are not kept across scroll steps.  If a row is virtualized away between
``_scroll_feed_to_row`` and ``_click_thread_bubble``, ``_open_thread_and_collect``
re-locates it (up to three times) before propagating the failure.
Thread bubbles in each batch are processed oldest-first so earlier rows stay in
the DOM while later threads open.  Scroll containers are focused and scrolled
via JavaScript — thread bubble clicks use JavaScript only; inline image
attachments are clicked deliberately to open the full-screen gallery for
original-resolution downloads (feed ``<img>`` blobs are ~90 px previews).
Panel close tries the header button, then refocuses the main feed
when the panel stays visible.  Panel open waits until the visible panel's root
``data-message-id`` matches the clicked message; close waits poll until no
visible panel remains.  Inner reply collection parses only the panel HTML and
logs ``inner scroll N/M replies`` during long threads.  Escape is not sent
proactively — in SberChat it navigates out of the open group chat back to the
chat list.
"""

import base64
import time
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from pageup.config import (
    IMAGE_GALLERY_CLOSE_ICON_ARIA,
    IMAGE_GALLERY_CLOSE_TIMEOUT_SEC,
    IMAGE_GALLERY_FETCH_TIMEOUT_SEC,
    IMAGE_GALLERY_TOPBAR_CLS,
    IMAGE_GALLERY_WRAP_CLASSES,
    MAX_THREAD_BOOTSTRAP_DOWN_STEPS,
    MAX_THREAD_N_MINUS_ONE_STALL,
    MAX_THREAD_OPEN_ATTEMPTS,
    MAX_THREAD_PANEL_CLOSE_ATTEMPTS,
    MAX_THREAD_ROW_SEARCH_STEPS,
    MAX_THREAD_STALL_ATTEMPTS,
    MIN_FULL_IMAGE_PX,
    MSG_ATTACHMENT_CLS,
    MSG_IMAGE_MEDIA_CLICKABLE_SUBCLS,
    MSG_IMAGE_WRAP_CLS,
    MSG_LIST_CONTAINER_CLS,
    SCROLL_PROGRESS_INTERVAL,
    THREAD_BUBBLE_CLS,
    THREAD_PANEL_CLOSE_SEL,
    THREAD_PANEL_CLOSE_TIMEOUT_SEC,
    THREAD_PANEL_CLS,
    THREAD_PANEL_OPEN_TIMEOUT_SEC,
)
from pageup.models import Entry, Message, ParsingTask
from pageup.tools import dom_message_id, normalize_message_id


def _log(message: str) -> None:
    print(f"[pageup] {message}", flush=True)


# execute_async_script must exceed IMAGE_GALLERY_FETCH_TIMEOUT_SEC (ms in JS).
_IMAGE_SCRIPT_TIMEOUT_SEC: int = IMAGE_GALLERY_FETCH_TIMEOUT_SEC + 5


def _css_attr_value(value: str) -> str:
    """Escape a string for use inside a CSS attribute selector."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _safe_click(driver: Chrome, element) -> None:
    """Click *element* via JavaScript (avoids native hit targets on child media/links)."""
    driver.execute_script("arguments[0].click();", element)


def _focus_scroll_target(driver: Chrome, element) -> None:
    """Focus a scroll container without clicking — prevents opening media or links."""
    driver.execute_script(
        """
        const el = arguments[0];
        el.setAttribute('tabindex', '-1');
        el.focus({preventScroll: true});
        """,
        element,
    )


def _scroll_container(
    driver: Chrome,
    container,
    *,
    pages: float = 5,
    direction: int = -1,
) -> None:
    """Scroll *container* by *pages* viewport heights (*direction* −1 up, 1 down)."""
    driver.execute_script(
        """
        const el = arguments[0];
        const delta = el.clientHeight * 0.8 * arguments[1] * arguments[2];
        const maxScroll = Math.max(0, el.scrollHeight - el.clientHeight);
        el.scrollTop = Math.max(0, Math.min(maxScroll, el.scrollTop + delta));
        """,
        container,
        pages,
        direction,
    )


def _scroll_container_page_up(driver: Chrome, container, *, pages: float = 5) -> None:
    """Scroll *container* upward by *pages* viewport heights via ``scrollTop``."""
    _scroll_container(driver, container, pages=pages, direction=-1)


def _scroll_container_page_down(driver: Chrome, container, *, pages: float = 5) -> None:
    """Scroll *container* downward by *pages* viewport heights via ``scrollTop``."""
    _scroll_container(driver, container, pages=pages, direction=1)


def _bootstrap_thread_panel_scroll(driver: Chrome) -> None:
    """Scroll the open thread panel to its bottom so lazy replies mount."""
    try:
        panel = _find_visible_panel(driver)
        scroll_area = panel.find_element(By.CLASS_NAME, MSG_LIST_CONTAINER_CLS)
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return

    prev_height = -1
    for step in range(MAX_THREAD_BOOTSTRAP_DOWN_STEPS):
        try:
            height = driver.execute_script(
                "return arguments[0].scrollHeight;",
                scroll_area,
            )
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight;",
                scroll_area,
            )
        except (StaleElementReferenceException, WebDriverException):
            try:
                panel = _find_visible_panel(driver)
                scroll_area = panel.find_element(By.CLASS_NAME, MSG_LIST_CONTAINER_CLS)
                height = driver.execute_script(
                    "return arguments[0].scrollHeight;",
                    scroll_area,
                )
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
                return
        time.sleep(0.3)
        if height == prev_height and step > 0:
            break
        prev_height = height
        try:
            _scroll_container_page_down(driver, scroll_area, pages=3)
        except (StaleElementReferenceException, WebDriverException):
            pass
        time.sleep(0.3)


def _row_selector(message_id: str) -> str:
    """Build a CSS selector for a main-feed or panel row by normalized *message_id*."""
    dom_id = dom_message_id(message_id)
    return f'[data-message-id="{_css_attr_value(dom_id)}"]'


def _main_feed_row_present(driver: Chrome, message_id: str) -> bool:
    """Return True when *message_id* is mounted in the main feed."""
    try:
        main_feed = _find_main_feed_container(driver)
        return bool(main_feed.find_elements(By.CSS_SELECTOR, _row_selector(message_id)))
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return False


def _get_main_feed_row(driver: Chrome, message_id: str):
    """Return a freshly located main-feed row for *message_id*."""
    selector = _row_selector(message_id)
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            main_feed = _find_main_feed_container(driver)
            rows = main_feed.find_elements(By.CSS_SELECTOR, selector)
            if rows:
                return rows[0]
        except StaleElementReferenceException as exc:
            last_exc = exc
            time.sleep(0.1)
            continue
    if last_exc is not None:
        raise last_exc
    raise NoSuchElementException(
        f"Message row {message_id!r} not found in main feed"
    )


def _find_row_in_main_feed(driver: Chrome, message_id: str) -> None:
    """Scroll the main feed until *message_id* is mounted in the DOM."""
    if _main_feed_row_present(driver, message_id):
        return

    _log(
        f"Thread: message {message_id!r} not in DOM — "
        f"scrolling main feed to locate row."
    )
    try:
        main_feed = _find_main_feed_container(driver)
        start_scroll = driver.execute_script("return arguments[0].scrollTop;", main_feed)
    except (StaleElementReferenceException, WebDriverException):
        start_scroll = 0

    for _ in range(MAX_THREAD_ROW_SEARCH_STEPS):
        try:
            main_feed = _find_main_feed_container(driver)
            _scroll_container(driver, main_feed, pages=3, direction=-1)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            pass
        time.sleep(0.25)
        if _main_feed_row_present(driver, message_id):
            return

    try:
        main_feed = _find_main_feed_container(driver)
        driver.execute_script(
            "arguments[0].scrollTop = arguments[1];",
            main_feed,
            start_scroll,
        )
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass

    for _ in range(MAX_THREAD_ROW_SEARCH_STEPS):
        try:
            main_feed = _find_main_feed_container(driver)
            _scroll_container(driver, main_feed, pages=3, direction=1)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            pass
        time.sleep(0.25)
        if _main_feed_row_present(driver, message_id):
            return

    raise NoSuchElementException(
        f"Message row {message_id!r} not in main feed after "
        f"{MAX_THREAD_ROW_SEARCH_STEPS} scroll steps up and down"
    )


def _scroll_feed_to_row(driver: Chrome, message_id: str) -> None:
    """Bring the main-feed row for *message_id* into view.

    Uses ``getBoundingClientRect`` so the calculation is correct regardless of
    how many intermediate positioned ancestors the row has (``offsetTop`` only
    goes up to the nearest positioned ancestor, not to the feed container).
    """
    selector = _row_selector(message_id)
    for _ in range(3):
        try:
            main_feed = _find_main_feed_container(driver)
            rows = main_feed.find_elements(By.CSS_SELECTOR, selector)
            if not rows:
                return
            row = rows[0]
            driver.execute_script(
                """
                const row = arguments[0];
                const feed = arguments[1];
                const rowRect = row.getBoundingClientRect();
                const feedRect = feed.getBoundingClientRect();
                const rowAbsTop = rowRect.top - feedRect.top + feed.scrollTop;
                feed.scrollTop = Math.max(0, rowAbsTop - feed.clientHeight * 0.35);
                """,
                row,
                main_feed,
            )
            return
        except StaleElementReferenceException:
            time.sleep(0.1)


def _click_thread_bubble(driver: Chrome, message_id: str) -> None:
    """Click the thread bubble on the main-feed row for *message_id*."""
    last_exc: StaleElementReferenceException | None = None
    for _ in range(3):
        try:
            row = _get_main_feed_row(driver, message_id)
            bubble = row.find_element(By.CLASS_NAME, THREAD_BUBBLE_CLS)
            _safe_click(driver, bubble)
            return
        except StaleElementReferenceException as exc:
            last_exc = exc
            time.sleep(0.2)
    # Loop exhaustion means every attempt caught StaleElementReferenceException
    # (NoSuchElementException propagates immediately without setting last_exc).
    if last_exc is not None:
        raise last_exc


def _find_main_feed_container(driver: Chrome):
    """Return the main chat ``MessageList`` container (not inside ThreadContent)."""
    containers = driver.find_elements(By.CLASS_NAME, MSG_LIST_CONTAINER_CLS)
    for container in containers:
        in_panel = container.find_elements(
            By.XPATH,
            f"./ancestor::div[contains(@class, '{THREAD_PANEL_CLS}')]",
        )
        if not in_panel:
            return container
    raise NoSuchElementException("Main feed MessageList container not found")


def _find_visible_panel(driver: Chrome):
    """Return the visible discussion panel element, if any."""
    for panel in driver.find_elements(By.CLASS_NAME, THREAD_PANEL_CLS):
        try:
            if panel.is_displayed():
                return panel
        except StaleElementReferenceException:
            continue
    raise NoSuchElementException("Visible discussion panel not found")


def _panel_is_open(driver: Chrome) -> bool:
    """Return True when a visible discussion panel is on screen."""
    try:
        _find_visible_panel(driver)
        return True
    except NoSuchElementException:
        return False


def _focus_main_feed(driver: Chrome) -> bool:
    """Focus the main chat message list. Returns False when the feed is absent."""
    try:
        main_feed = _find_main_feed_container(driver)
        _focus_scroll_target(driver, main_feed)
        return True
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return False


def _wait_panel_closed(driver: Chrome, timeout: float) -> bool:
    """Return True when no visible discussion panel remains on screen."""
    try:
        WebDriverWait(driver, timeout).until(lambda d: not _panel_is_open(d))
        return True
    except TimeoutException:
        return not _panel_is_open(driver)


def _panel_root_message_id(driver: Chrome) -> str | None:
    """Return the root ``data-message-id`` shown in the open discussion panel."""
    try:
        panel = _find_visible_panel(driver)
        row = panel.find_element(By.CSS_SELECTOR, "[data-message-id]")
        message_id = row.get_attribute("data-message-id")
        if not message_id:
            return None
        return normalize_message_id(message_id)
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return None


def _wait_panel_for_parent(
    driver: Chrome,
    parent_id: str,
    timeout: float,
) -> bool:
    """Return True when the visible panel shows *parent_id* as its root message."""
    def ready(_driver: Chrome) -> bool:
        if not _panel_is_open(_driver):
            return False
        return _panel_root_message_id(_driver) == parent_id

    try:
        WebDriverWait(driver, timeout).until(ready)
        return True
    except TimeoutException:
        return _panel_is_open(driver) and _panel_root_message_id(driver) == parent_id


def _panel_soup(driver: Chrome) -> BeautifulSoup:
    """Parse only the visible discussion panel (not the full page DOM)."""
    panel = _find_visible_panel(driver)
    html = panel.get_attribute("outerHTML") or ""
    return BeautifulSoup(html, "lxml")


def _main_feed_available(driver: Chrome) -> bool:
    """Return True when the group chat message list is present in the DOM."""
    try:
        _find_main_feed_container(driver)
        return True
    except NoSuchElementException:
        return False


def _close_panel(driver: Chrome) -> bool:
    """Close the discussion panel if open. Returns True when the panel is gone."""
    if not _panel_is_open(driver):
        return True

    for attempt in range(MAX_THREAD_PANEL_CLOSE_ATTEMPTS):
        try:
            panel = _find_visible_panel(driver)
            close_btn = panel.find_element(By.CSS_SELECTOR, THREAD_PANEL_CLOSE_SEL)
            _safe_click(driver, close_btn)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            pass

        if _wait_panel_closed(driver, THREAD_PANEL_CLOSE_TIMEOUT_SEC):
            return True

        _log(
            f"Thread: panel still open after close attempt "
            f"{attempt + 1}/{MAX_THREAD_PANEL_CLOSE_ATTEMPTS} — refocusing main feed."
        )
        _focus_main_feed(driver)
        time.sleep(0.3)
        if not _panel_is_open(driver):
            return True

    if _panel_is_open(driver):
        _log("Thread: could not close discussion panel cleanly.")
        return False
    return True


def prepare_main_feed_scroll(driver: Chrome) -> None:
    """Close overlays and focus the main feed for ``PAGE_UP``."""
    _close_image_gallery(driver)
    _close_panel(driver)
    if not _focus_main_feed(driver):
        _log(
            "Thread: main feed MessageList not found — "
            "ensure the group chat stays open during the run."
        )


def _collect_thread_panel_replies(
    driver: Chrome,
    task: ParsingTask,
    *,
    parent_id: str,
    expected_count: int,
) -> list[Entry]:
    """Scroll inside an open thread panel until replies are collected or stall.

    Inner steps adjust ``scrollTop`` (~five viewport heights per step, alternating
    direction) with 1 s pauses; stalling stops after ``MAX_THREAD_STALL_ATTEMPTS``
    (~60 s with no new reply IDs).  When the collected count is exactly N−1 of
    the expected count and no change occurs for ``MAX_THREAD_N_MINUS_ONE_STALL``
    consecutive steps, the loop exits early (the missing reply is likely deleted
    or unparseable).
    """
    accumulated: dict[str, Entry] = {}
    # Start at -1 so the first iteration (before any scroll) does not consume a
    # stall slot — the effective budget is MAX_THREAD_STALL_ATTEMPTS steps, or
    # MAX_THREAD_N_MINUS_ONE_STALL steps when stuck at exactly N−1 replies.
    stall_attempts = -1
    stall_limit = MAX_THREAD_STALL_ATTEMPTS
    scroll_step = 0

    while True:
        scroll_step += 1
        prev_count = len(accumulated)

        root_id = _panel_root_message_id(driver)
        if root_id is not None and root_id != parent_id:
            _log(
                f"Thread: panel no longer shows message {parent_id!r} "
                f"(panel root={root_id!r}) during inner scroll — stopping."
            )
            break

        soup = _panel_soup(driver)
        for reply in task.collect_thread_reply_entries(soup, parent_id):
            accumulated[reply.message_id] = reply

        if len(accumulated) >= expected_count:
            break

        if len(accumulated) == prev_count:
            stall_attempts += 1
            n_minus_one = (
                expected_count > 1
                and len(accumulated) >= expected_count - 1
                and len(accumulated) < expected_count
            )
            # Fast exit: if exactly one reply short and stuck, don't burn the
            # entire 60 s budget — deleted or unparseable replies never appear.
            fast_exit = n_minus_one and stall_attempts >= MAX_THREAD_N_MINUS_ONE_STALL
            full_stall = stall_attempts >= stall_limit
            if fast_exit or full_stall:
                if n_minus_one:
                    _log(
                        f"⚠️ Thread: stopping at {len(accumulated)}/{expected_count} "
                        f"replies for message {parent_id!r} after {stall_attempts} "
                        f"stall steps — bubble count may include unparseable rows."
                    )
                break
        else:
            stall_attempts = 0

        if (
            len(accumulated) != prev_count
            or scroll_step == 1
            or scroll_step % SCROLL_PROGRESS_INTERVAL == 0
        ):
            _log(
                f"Thread: inner scroll {len(accumulated)}/{expected_count} "
                f"replies for message {parent_id!r}"
            )

        try:
            panel = _find_visible_panel(driver)
            scroll_area = panel.find_element(By.CLASS_NAME, MSG_LIST_CONTAINER_CLS)
            direction = -1 if scroll_step % 2 else 1
            _scroll_container(driver, scroll_area, pages=5, direction=direction)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            if stall_attempts >= stall_limit:
                break
        time.sleep(1)

    return sorted(accumulated.values(), key=lambda entry: entry.date)


def _download_thread_reply_images(
    driver: Chrome,
    write_dir: str,
    entries: list[Entry],
) -> None:
    """Download image attachments for thread replies while the panel is open.

    Opens each reply row's inline image gallery, fetches full-resolution bytes
    in-page, and mutates ``reply.attachments`` in place (``Entry`` is not frozen).
    Silently skips replies with no undownloaded image attachments or rows that
    cannot be located.
    """
    if not any(_needs_image_download(reply.attachments) for reply in entries):
        return
    try:
        driver.set_script_timeout(_IMAGE_SCRIPT_TIMEOUT_SEC)
    except WebDriverException:
        pass
    try:
        panel = _find_visible_panel(driver)
    except (NoSuchElementException, WebDriverException):
        return
    for reply in entries:
        if not _needs_image_download(reply.attachments):
            continue
        assert reply.attachments is not None
        try:
            rows = panel.find_elements(
                By.CSS_SELECTOR,
                _row_selector(reply.message_id),
            )
            if not rows:
                continue
            row = rows[0]
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue
        new_attachments = _download_images_for_row(
            driver, write_dir, reply.message_id, row, reply.attachments
        )
        if new_attachments is not reply.attachments:
            reply.attachments = new_attachments


def _open_thread_and_collect(
    driver: Chrome,
    task: ParsingTask,
    message: Message,
    *,
    write_dir: str | None = None,
) -> list[Entry] | None:
    """Open the thread panel for *message* and return collected replies.

    When *write_dir* is provided, image attachments on thread replies are
    downloaded (via ``_download_thread_reply_images``) before the panel closes.
    """
    expected = message.thread_reply_count
    if not expected or expected <= 0:
        return None

    _close_panel(driver)

    try:
        # locate → position → click with re-locate on NoSuchElementException so that
        # a row virtualized between _scroll_feed_to_row and _click_thread_bubble is
        # re-found rather than failing the entire thread open attempt.
        _find_row_in_main_feed(driver, message.message_id)
        for _locate_attempt in range(3):
            _scroll_feed_to_row(driver, message.message_id)
            time.sleep(0.3)
            try:
                _click_thread_bubble(driver, message.message_id)
                break
            except NoSuchElementException:
                if _locate_attempt == 2:
                    raise
                _find_row_in_main_feed(driver, message.message_id)

        if not _wait_panel_for_parent(
            driver, message.message_id, THREAD_PANEL_OPEN_TIMEOUT_SEC
        ):
            root_id = _panel_root_message_id(driver)
            _log(
                f"Thread: discussion panel did not open for message "
                f"{message.message_id!r} (panel root={root_id!r}, "
                f"expected {expected} replies)"
            )
            return None
        time.sleep(0.5)

        _bootstrap_thread_panel_scroll(driver)

        replies = _collect_thread_panel_replies(
            driver,
            task,
            parent_id=message.message_id,
            expected_count=expected,
        )
        if not replies:
            _log(
                f"Thread: failed to collect replies for message "
                f"{message.message_id!r} (expected {expected})"
            )
            return None

        # Download reply image attachments while the panel is still open.
        # _collect_thread_panel_replies builds replies from its own accumulated
        # dict; collect_thread_reply_entries re-parses to get Entry objects
        # whose .attachments we can mutate in-place (Entry is not frozen).
        # Wrapped in a try/except so a panel that closes unexpectedly between
        # collection and download does not discard already-accumulated replies.
        if write_dir is not None:
            try:
                entries = task.collect_thread_reply_entries(
                    _panel_soup(driver), message.message_id
                )
                if entries:
                    _download_thread_reply_images(driver, write_dir, entries)
                    # Only replace replies when entries covers all accumulated
                    # replies; for long threads some rows may be virtualized and
                    # a partial replacement would silently discard reply objects.
                    if len(entries) >= len(replies):
                        replies = list(entries)
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
                pass  # Panel closed between collection and image download; keep replies.

        return replies
    except (TimeoutException, NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
        _log(
            f"Thread: failed to collect replies for message "
            f"{message.message_id!r} (expected {expected}): {exc}"
        )
        return None
    finally:
        _close_panel(driver)


# ── Image attachment download ─────────────────────────────────────────────────

# Sentinel: gallery opened a video player, not a still image.
_VIDEO_GALLERY_SKIP = object()

# Base selector: all MessageMedia clickables inside attachment blocks.
# threads._image_click_targets keeps only those that contain MSG_IMAGE_WRAP_CLS
# (inline images).  Video previews use a sibling clickable without an inner wrap.
_IMAGE_ATTACHMENT_CLICKABLE_SEL: str = (
    f'.{MSG_ATTACHMENT_CLS} [class*="{MSG_IMAGE_MEDIA_CLICKABLE_SUBCLS}"]'
)


def _image_click_targets(row_element) -> list:
    """Return clickable inline image previews within *row_element*."""
    try:
        candidates = row_element.find_elements(
            By.CSS_SELECTOR, _IMAGE_ATTACHMENT_CLICKABLE_SEL
        )
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return []
    targets: list = []
    for el in candidates:
        try:
            el.find_element(By.CLASS_NAME, MSG_IMAGE_WRAP_CLS)
        except (NoSuchElementException, StaleElementReferenceException):
            continue
        targets.append(el)
    return targets


# Shared gallery helpers — fixed-position overlays have offsetParent === null.
_GALLERY_JS_IS_SHOWN: str = """
function galleryIsShown(el) {
  if (!el) return false;
  const st = window.getComputedStyle(el);
  if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) {
    return false;
  }
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
function galleryVisibleWraps(wrapClasses) {
  const out = [];
  const seen = new Set();
  for (const wrapClass of wrapClasses) {
    for (const el of document.getElementsByClassName(wrapClass)) {
      if (galleryIsShown(el) && !seen.has(el)) { seen.add(el); out.push(el); }
    }
  }
  return out;
}
function galleryTopbars(topbarClass, wrapClasses) {
  const bars = [];
  const seen = new Set();
  for (const wrap of galleryVisibleWraps(wrapClasses)) {
    const topbar = wrap.getElementsByClassName(topbarClass)[0];
    if (topbar && !seen.has(topbar)) { seen.add(topbar); bars.push(topbar); }
  }
  for (const topbar of document.getElementsByClassName(topbarClass)) {
    if (galleryIsShown(topbar) && !seen.has(topbar)) { seen.add(topbar); bars.push(topbar); }
  }
  return bars;
}
function pauseGalleryVideos() {
  for (const v of document.querySelectorAll('video')) {
    try { v.pause(); } catch (e) {}
  }
}
function galleryHasVideo(wrapClasses) {
  for (const root of galleryVisibleWraps(wrapClasses)) {
    if (root.querySelector('video')) return true;
    for (const el of root.querySelectorAll('[class*="VideoMedia-"], [class*="PhotoVideoMedia-"]')) {
      return true;
    }
  }
  return false;
}
"""

# Async JS: poll the open gallery for the largest loaded <img>, fetch its bytes
# (blob:, https:, or data: URL) in-page, and return a data URL.  Does not trigger
# the gallery "Скачать" button or browser Downloads — pageup writes attachments
# itself under {write_dir}/attachments/.
_FETCH_GALLERY_IMAGE_JS: str = (
    _GALLERY_JS_IS_SHOWN
    + """
const wrapClasses = arguments[0];
const minPx       = arguments[1];
const timeoutMs   = arguments[2];
const cb          = arguments[arguments.length - 1];
const deadline    = Date.now() + timeoutMs;

function canvasDataUrl(img) {
  const c = document.createElement('canvas');
  c.width = img.naturalWidth;
  c.height = img.naturalHeight;
  c.getContext('2d').drawImage(img, 0, 0);
  return c.toDataURL('image/png');
}

function imgToDataUrl(img) {
  return new Promise((resolve, reject) => {
    const src = img.currentSrc || img.src;
    if (!src) { reject(); return; }
    if (src.startsWith('data:')) { resolve(src); return; }
    const toDataUrl = (blob) => {
      const rd = new FileReader();
      rd.onloadend = () => resolve(rd.result);
      rd.onerror   = () => reject();
      rd.readAsDataURL(blob);
    };
    const tryCanvas = () => {
      try { resolve(canvasDataUrl(img)); }
      catch (e) { reject(e); }
    };
    if (src.startsWith('blob:') || src.startsWith('http')) {
      fetch(src, { credentials: 'include' })
        .then(r => { if (!r.ok) throw new Error('fetch failed'); return r.blob(); })
        .then(toDataUrl)
        .catch(tryCanvas);
    } else {
      reject();
    }
  });
}

(function poll() {
  const roots = galleryVisibleWraps(wrapClasses);
  if (!roots.length) {
    if (Date.now() >= deadline) { cb(null); return; }
    setTimeout(poll, 100);
    return;
  }
  if (galleryHasVideo(wrapClasses)) {
    pauseGalleryVideos();
    cb({ skipVideo: true });
    return;
  }
  let best = null, bestMax = 0;
  for (const root of roots) {
    for (const img of root.querySelectorAll('img')) {
      const w = img.naturalWidth  || 0;
      const h = img.naturalHeight || 0;
      const m = Math.max(w, h);
      if (img.complete && m > bestMax) { bestMax = m; best = img; }
    }
  }
  if (best && bestMax > minPx) {
    imgToDataUrl(best)
      .then(dataUrl => cb({ dataUrl, w: best.naturalWidth, h: best.naturalHeight }))
      .catch(() => {
        if (Date.now() >= deadline) cb(null);
        else setTimeout(poll, 100);
      });
    return;
  }
  if (Date.now() >= deadline) { cb(null); return; }
  setTimeout(poll, 100);
})();
"""
)

# Maps MIME sub-types (from data URL prefix) to file extensions.
_MIME_TO_EXT: dict[str, str] = {
    "jpeg": "jpg",
    "jpg": "jpg",
    "png": "png",
    "webp": "webp",
    "gif": "gif",
    "bmp": "bmp",
    "tiff": "tiff",
}


def _dataurl_ext(data_url: str) -> str:
    """Return a file extension inferred from a data URL MIME prefix.

    ``data:image/jpeg;base64,...`` → ``"jpg"``.
    Used as a fallback by ``_resolve_image_ext`` when magic-byte sniffing
    does not recognise the payload.  Falls back to ``"bin"`` for unknown types.
    """
    # data_url starts with "data:image/TYPE;base64,..."
    if not data_url.startswith("data:"):
        return "bin"
    mime_part = data_url[5:data_url.index(",")] if "," in data_url else ""
    # mime_part is e.g. "image/jpeg;base64"
    subtype = mime_part.split("/")[-1].split(";")[0].lower()
    return _MIME_TO_EXT.get(subtype, "bin")


def _image_ext_from_bytes(raw: bytes) -> str | None:
    """Return a file extension inferred from image magic bytes, or ``None``."""
    if not raw:
        return None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw.startswith(b"BM"):
        return "bmp"
    if raw.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    return None


def _resolve_image_ext(data_url: str, raw: bytes) -> str:
    """Return the best file extension for decoded image *raw*.

    Prefers magic-byte detection over the data URL MIME prefix so that
    ``application/octet-stream`` payloads from SberChat still save as ``.png`` etc.
    """
    ext = _image_ext_from_bytes(raw)
    if ext is not None:
        return ext
    return _dataurl_ext(data_url)


def _jpeg_pixel_size(raw: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from a JPEG SOF marker, or ``None``."""
    i = 2
    while i < len(raw) - 8:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        ):
            height = int.from_bytes(raw[i + 5 : i + 7], "big")
            width = int.from_bytes(raw[i + 7 : i + 9], "big")
            return width, height
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9):
            i += 2
            continue
        if i + 3 >= len(raw):
            break
        seg_len = int.from_bytes(raw[i + 2 : i + 4], "big")
        if seg_len < 2:
            break
        i += 2 + seg_len
    return None


def _image_pixel_max_dim(raw: bytes) -> int | None:
    """Return the larger pixel dimension encoded in *raw*, when known."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        return max(width, height)
    jpeg = _jpeg_pixel_size(raw)
    if jpeg is not None:
        return max(jpeg)
    return None


def _gallery_is_open(driver: Chrome) -> bool:
    """Return True when the image gallery overlay is visible."""
    try:
        for wrap_cls in IMAGE_GALLERY_WRAP_CLASSES:
            for el in driver.find_elements(By.CLASS_NAME, wrap_cls):
                if el.is_displayed():
                    return True
    except WebDriverException:
        pass
    return False


# JS: click the gallery ✕ control.  Top-bar uses flex-end, so the close button is
# the *first* <button> in DOM (rightmost on screen); buttons[-1] is «Скачать».
_CLOSE_IMAGE_GALLERY_JS: str = (
    _GALLERY_JS_IS_SHOWN
    + """
const wrapClasses = arguments[0];
const topbarClass = arguments[1];
const closeIcon   = arguments[2];
function clickClose(topbar) {
  pauseGalleryVideos();
  const closeSvg = topbar.querySelector('svg[aria-label="' + closeIcon + '"]');
  const closeBtn = closeSvg && closeSvg.closest('button');
  if (closeBtn) { closeBtn.click(); return true; }
  const buttons = topbar.querySelectorAll('button');
  if (buttons.length) { buttons[0].click(); return true; }
  return false;
}
for (const topbar of galleryTopbars(topbarClass, wrapClasses)) {
  if (clickClose(topbar)) return true;
}
return false;
"""
)


def _close_image_gallery_selenium(driver: Chrome) -> None:
    """Close the gallery via Selenium when the in-page script misses the overlay."""
    for topbar in _gallery_topbar_elements(driver):
        try:
            for svg in topbar.find_elements(
                By.CSS_SELECTOR,
                f'svg[aria-label="{IMAGE_GALLERY_CLOSE_ICON_ARIA}"]',
            ):
                btn = svg.find_element(By.XPATH, "./ancestor::button[1]")
                _safe_click(driver, btn)
                return
            buttons = topbar.find_elements(By.TAG_NAME, "button")
            if buttons:
                _safe_click(driver, buttons[0])
                return
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue


def _gallery_topbar_elements(driver: Chrome) -> list:
    """Return visible gallery top-bar elements (deduplicated)."""
    seen: set[str] = set()
    topbars: list = []
    for wrap_cls in IMAGE_GALLERY_WRAP_CLASSES:
        try:
            for wrap in driver.find_elements(By.CLASS_NAME, wrap_cls):
                if not wrap.is_displayed():
                    continue
                try:
                    topbar = wrap.find_element(By.CLASS_NAME, IMAGE_GALLERY_TOPBAR_CLS)
                except NoSuchElementException:
                    continue
                key = topbar.id or str(id(topbar))
                if key not in seen:
                    seen.add(key)
                    topbars.append(topbar)
        except WebDriverException:
            continue
    try:
        for topbar in driver.find_elements(By.CLASS_NAME, IMAGE_GALLERY_TOPBAR_CLS):
            if topbar.is_displayed():
                key = topbar.id or str(id(topbar))
                if key not in seen:
                    seen.add(key)
                    topbars.append(topbar)
    except WebDriverException:
        pass
    return topbars


def _close_image_gallery(driver: Chrome) -> None:
    """Close the image gallery via its top-bar ✕ button (never «Скачать»)."""
    closed = False
    try:
        closed = bool(
            driver.execute_script(
                _CLOSE_IMAGE_GALLERY_JS,
                list(IMAGE_GALLERY_WRAP_CLASSES),
                IMAGE_GALLERY_TOPBAR_CLS,
                IMAGE_GALLERY_CLOSE_ICON_ARIA,
            )
        )
    except WebDriverException:
        pass
    if not closed and _gallery_is_open(driver):
        _close_image_gallery_selenium(driver)
    try:
        WebDriverWait(driver, IMAGE_GALLERY_CLOSE_TIMEOUT_SEC).until_not(
            _gallery_is_open
        )
    except TimeoutException:
        if _gallery_is_open(driver):
            _close_image_gallery_selenium(driver)
            try:
                WebDriverWait(driver, IMAGE_GALLERY_CLOSE_TIMEOUT_SEC).until_not(
                    _gallery_is_open
                )
            except TimeoutException:
                pass
            if _gallery_is_open(driver):
                _log("⚠️ Image: gallery overlay still open after close attempts")


def _fetch_gallery_image(
    driver: Chrome,
) -> tuple[bytes, str, tuple[int, int]] | object | None:
    """Poll the open gallery and return ``(raw, data_url, (w, h))``.

    Returns ``_VIDEO_GALLERY_SKIP`` when the overlay is a video player.
    Returns ``None`` on timeout or fetch failure.
    """
    try:
        payload: dict | None = driver.execute_async_script(
            _FETCH_GALLERY_IMAGE_JS,
            list(IMAGE_GALLERY_WRAP_CLASSES),
            MIN_FULL_IMAGE_PX,
            IMAGE_GALLERY_FETCH_TIMEOUT_SEC * 1000,
        )
    except WebDriverException:
        return None
    if payload and payload.get("skipVideo"):
        return _VIDEO_GALLERY_SKIP
    if not payload or not payload.get("dataUrl"):
        return None
    data_url = payload["dataUrl"]
    comma_pos = data_url.find(",")
    if comma_pos == -1:
        return None
    try:
        raw = base64.b64decode(data_url[comma_pos + 1 :])
    except Exception:
        return None
    dims = (int(payload["w"]), int(payload["h"]))
    return raw, data_url, dims


def _download_image_elements(
    driver: Chrome,
    write_dir: str,
    message_id: str,
    row_element,
    existing_attachments: list[str | None],
) -> list[str | None]:
    """Download full-resolution images via the gallery and return updated slots.

    For each pending ``None`` slot, re-queries clickable previews on *row_element*,
    opens the gallery with a JS click, fetches the largest gallery ``<img>`` in-page
    (``blob:`` or ``https:``, never the browser «Скачать» button), saves to
    ``{write_dir}/attachments/``, and closes the gallery before the next image.
    Video galleries are skipped immediately (slot set to ``""``, omitted from JSON).
    """
    files_dir = Path(write_dir) / "attachments"
    files_dir.mkdir(parents=True, exist_ok=True)

    updated = list(existing_attachments)
    skipped_due_to_shortage = False
    previews_found = 0

    for att_idx, slot in enumerate(updated):
        if slot is not None:
            continue
        pending_ordinal = sum(
            1 for i in range(att_idx) if existing_attachments[i] is None
        )
        click_targets = _image_click_targets(row_element)
        previews_found = len(click_targets)
        if pending_ordinal >= previews_found:
            skipped_due_to_shortage = True
            break
        click_target = click_targets[pending_ordinal]
        _close_image_gallery(driver)
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                click_target,
            )
            _safe_click(driver, click_target)
        except WebDriverException:
            _log(
                f"⚠️ Image: could not click preview for {message_id}_{att_idx}"
            )
            _close_image_gallery(driver)
            continue

        fetched = _fetch_gallery_image(driver)
        if fetched is _VIDEO_GALLERY_SKIP:
            _log(
                f"⚠️ Image: skipping video attachment for {message_id}_{att_idx}"
            )
            updated[att_idx] = ""
            _close_image_gallery(driver)
            continue
        if fetched is None:
            _log(
                f"⚠️ Image: full-size image did not load for {message_id}_{att_idx} "
                f"(still ≤{MIN_FULL_IMAGE_PX}px, fetch failed, or timed out)"
            )
            _close_image_gallery(driver)
            continue
        raw, data_url, dims = fetched

        max_dim = _image_pixel_max_dim(raw) or max(dims)
        if max_dim <= MIN_FULL_IMAGE_PX:
            _log(
                f"⚠️ Image: rejecting {message_id}_{att_idx} "
                f"({max_dim}px — preview size, not original)"
            )
            _close_image_gallery(driver)
            continue

        ext = _resolve_image_ext(data_url, raw)
        file_id = f"{message_id}_{att_idx}.{ext}"
        try:
            (files_dir / file_id).write_bytes(raw)
        except OSError as exc:
            _log(
                f"⚠️ Image: could not write {file_id} ({exc})"
            )
            _close_image_gallery(driver)
            continue
        updated[att_idx] = file_id
        _log(
            f"🖼️ Image: saved {file_id} ({len(raw):,} bytes, {dims[0]}×{dims[1]} px)"
        )
        _close_image_gallery(driver)

    if skipped_due_to_shortage:
        remaining = sum(
            1
            for i, slot in enumerate(updated)
            if slot is None and existing_attachments[i] is None
        )
        if remaining:
            _log(
                f"⚠️ Image: {remaining} slot(s) still pending for {message_id!r} "
                f"(found {previews_found} clickable preview(s))"
            )

    return updated


def _needs_image_download(attachments: list[str | None] | None) -> bool:
    """Return True when *attachments* contains at least one undownloaded image slot."""
    if not attachments:
        return False
    return any(slot is None for slot in attachments)


def _download_images_for_row(
    driver: Chrome,
    write_dir: str,
    message_id: str,
    row_element,
    attachments: list[str | None],
) -> list[str | None]:
    """Open each inline image gallery in *row_element* and download originals.

    Returns an updated attachment list; unchanged if no click targets are found
    or all downloads fail.
    """
    click_targets = _image_click_targets(row_element)
    if not click_targets:
        if any(slot is None for slot in attachments):
            _log(
                f"⚠️ Image: no clickable previews in row for {message_id!r}"
            )
        return attachments
    return _download_image_elements(
        driver, write_dir, message_id, row_element, attachments
    )


def download_fresh_images(
    driver: Chrome,
    write_dir: str,
    messages: list[Message],
    *,
    on_message_downloaded: Callable[[Message], None] | None = None,
) -> list[Message]:
    """Download image attachments for *messages* that have undownloaded images.

    For each message with at least one pending ``None`` slot in ``attachments``,
    scrolls the main-feed row into view, clicks each inline image preview
    (``MessageMedia-MessageMediaWrapper__clickable``) to open the gallery, and
    downloads the full-resolution gallery ``<img>`` via in-page fetch
    (``blob:``, ``https:``, or ``data:`` URL through ``execute_async_script``).

    Saved files land in ``{write_dir}/attachments/`` with names
    ``{message_id}_{attachment_index}.{ext}``.  Failed downloads leave the slot
    as ``None``; video attachments are marked ``""`` (skipped, omitted from JSON).
    The message is still returned with its other fields intact.

    Returns a new list with the same ordering as *messages*; messages without
    image attachments are returned unchanged.

    When *on_message_downloaded* is provided, it is called with each message as
    soon as its own download step finishes (whether or not it needed any
    downloads), rather than only after every message in *messages* has been
    processed.  This lets the caller persist progress immediately, so a later
    message's failure (dropped connection, Ctrl+C, etc.) cannot erase an
    earlier message's already-downloaded image attachments.
    """
    if not any(_needs_image_download(m.attachments) for m in messages):
        return messages

    try:
        driver.set_script_timeout(_IMAGE_SCRIPT_TIMEOUT_SEC)
    except WebDriverException:
        pass

    updated_messages: list[Message] = []
    for message in messages:
        if not _needs_image_download(message.attachments):
            updated_messages.append(message)
            if on_message_downloaded is not None:
                on_message_downloaded(message)
            continue

        assert message.attachments is not None  # narrowing: _needs_image_download checked
        try:
            _find_row_in_main_feed(driver, message.message_id)
            _scroll_feed_to_row(driver, message.message_id)
            row = _get_main_feed_row(driver, message.message_id)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
            _log(
                f"⚠️ Image: could not locate row {message.message_id!r} "
                f"for image download: {exc}"
            )
            updated_messages.append(message)
            if on_message_downloaded is not None:
                on_message_downloaded(message)
            continue

        new_attachments = _download_images_for_row(
            driver, write_dir, message.message_id, row, message.attachments
        )
        if new_attachments is message.attachments:
            updated_message = message
        else:
            updated_message = message.model_copy(
                update={"attachments": new_attachments}
            )
        updated_messages.append(updated_message)
        if on_message_downloaded is not None:
            on_message_downloaded(updated_message)

    return updated_messages


def enrich_fresh_threads(
    driver: Chrome,
    task: ParsingTask,
    fresh: list[Message],
    *,
    thread_collected_ids: set[str],
    thread_open_attempts: dict[str, int] | None = None,
    write_dir: str | None = None,
    on_message_enriched: Callable[[Message], None] | None = None,
) -> list[Message]:
    """Return *fresh* messages with ``thread_replies`` populated where applicable.

    Skips messages without a thread bubble and IDs already present in
    *thread_collected_ids* (fully completed threads and exhausted open attempts
    are not re-opened).  Partial collections and transient failures while the
    group chat stays open leave the ID out of *thread_collected_ids* so the
    runner can retry on a later scroll batch until ``MAX_THREAD_OPEN_ATTEMPTS``.
    When the group chat closes (main feed MessageList absent), skips remaining
    thread bubbles in the batch instead of retrying each one.
    Calls ``prepare_main_feed_scroll`` after thread-panel work so the main feed
    retains focus before the next runner ``PAGE_UP``.

    When *write_dir* is provided, thread-reply image attachments are downloaded
    inside ``_open_thread_and_collect`` while each panel is still open.

    When *on_message_enriched* is provided, it is called with each message as
    soon as its replies are collected — i.e. as the loop below progresses,
    rather than only after every candidate in *fresh* has been processed.  This
    lets the caller persist progress immediately, so a later candidate's
    failure (dropped connection, Ctrl+C, etc.) cannot erase an earlier
    candidate's already-collected replies.
    """
    open_attempts = thread_open_attempts if thread_open_attempts is not None else {}
    updates: dict[str, Message] = {}
    thread_work = False
    chat_open = True
    thread_candidates = sorted(
        (
            message
            for message in fresh
            if (
                message.thread_reply_count
                and message.thread_reply_count > 0
                and message.message_id not in thread_collected_ids
            )
        ),
        key=lambda message: message.date,
    )

    for message in thread_candidates:
        if not chat_open:
            thread_collected_ids.add(message.message_id)
            continue

        attempts = open_attempts.get(message.message_id, 0)
        if attempts >= MAX_THREAD_OPEN_ATTEMPTS:
            thread_collected_ids.add(message.message_id)
            continue

        thread_work = True
        open_attempts[message.message_id] = attempts + 1
        _log(
            f"Thread: opening discussion for message {message.message_id!r} "
            f"({message.thread_reply_count} replies expected)"
        )
        replies = _open_thread_and_collect(driver, task, message, write_dir=write_dir)

        if replies is None:
            if not _main_feed_available(driver):
                chat_open = False
                thread_collected_ids.add(message.message_id)
                _log(
                    "Thread: group chat no longer open — "
                    "skipping remaining thread bubbles."
                )
            elif open_attempts[message.message_id] >= MAX_THREAD_OPEN_ATTEMPTS:
                thread_collected_ids.add(message.message_id)
            continue

        got = len(replies)
        expected = message.thread_reply_count
        if got < expected:
            _log(
                f"⚠️ Thread: collected {got}/{expected} replies for message "
                f"{message.message_id!r} (partial)"
            )
        else:
            thread_collected_ids.add(message.message_id)
            _log(
                f"✅ Thread: collected {got}/{expected} replies for message "
                f"{message.message_id!r}"
            )

        enriched_message = message.model_copy(update={"thread_replies": replies})
        updates[message.message_id] = enriched_message
        if on_message_enriched is not None:
            on_message_enriched(enriched_message)

    enriched = [
        updates.get(message.message_id, message)
        for message in fresh
    ]
    if thread_work:
        prepare_main_feed_scroll(driver)
    return enriched
