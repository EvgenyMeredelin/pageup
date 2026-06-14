"""Unit tests for pageup.config constants.

Smoke checks that compile-time constants are present and sane, including
scroll safety limits (``MAX_EMPTY_SCROLL_ATTEMPTS``, ``MAX_STALL_SCROLL_ATTEMPTS``,
``MAIN_SCROLL_SLEEP_SEC``, ``MAX_THREAD_STALL_ATTEMPTS``,
``MAX_THREAD_N_MINUS_ONE_STALL``, ``MAX_THREAD_BOOTSTRAP_DOWN_STEPS``,
``MAX_THREAD_ROW_SEARCH_STEPS``, ``MAX_THREAD_OPEN_ATTEMPTS``,
``MAX_THREAD_PANEL_CLOSE_ATTEMPTS``, ``THREAD_PANEL_OPEN_TIMEOUT_SEC``,
``THREAD_PANEL_CLOSE_TIMEOUT_SEC``).
Does not validate selectors against a live SberChat DOM — that is covered indirectly
by tests.test_models HTML fixture parsing.
"""

import unittest

from pageup.config import (
    IMAGE_GALLERY_CLOSE_ICON_ARIA,
    IMAGE_GALLERY_CLOSE_TIMEOUT_SEC,
    IMAGE_GALLERY_DOWNLOAD_BTN_SEL,
    IMAGE_GALLERY_IMG_CLS,
    IMAGE_GALLERY_FETCH_TIMEOUT_SEC,
    IMAGE_GALLERY_LOAD_TIMEOUT_SEC,
    IMAGE_GALLERY_OPEN_TIMEOUT_SEC,
    IMAGE_GALLERY_TOPBAR_CLS,
    IMAGE_GALLERY_V2_DIALOG_CLS,
    IMAGE_GALLERY_V2_WRAP_CLS,
    IMAGE_GALLERY_WRAP_CLS,
    IMAGE_GALLERY_WRAP_CLASSES,
    MAIN_SCROLL_SLEEP_SEC,
    MAX_EMPTY_SCROLL_ATTEMPTS,
    MAX_STALL_SCROLL_ATTEMPTS,
    MAX_THREAD_BOOTSTRAP_DOWN_STEPS,
    MAX_THREAD_N_MINUS_ONE_STALL,
    MAX_THREAD_OPEN_ATTEMPTS,
    MAX_THREAD_PANEL_CLOSE_ATTEMPTS,
    MAX_THREAD_ROW_SEARCH_STEPS,
    MAX_THREAD_STALL_ATTEMPTS,
    MIN_FULL_IMAGE_PX,
    MSG_ATTACHMENT_CLS,
    MSG_CONTENT_SEL,
    MSG_IMAGE_MEDIA_CLICKABLE_SUBCLS,
    MSG_IMAGE_WRAP_CLS,
    MSG_VIDEO_MEDIA_PREFIXES,
    MSG_LIST_CONTAINER_CLS,
    MSG_SENDER_NAME_CLS,
    MSG_SENDER_URL_SEL,
    MSG_WRAP_CLS,
    PAGE_LOAD_TIMEOUT_SEC,
    QUOTE_CONTENT_SEL,
    QUOTE_SENDER_NAME_CLS,
    QUOTE_WRAP_CLS,
    SCROLL_PROGRESS_INTERVAL,
    SETUP_STATUS_INTERVAL_SEC,
    SBERBROWSER_BINARY,
    SBERBROWSER_DRIVER,
    THREAD_BUBBLE_CLS,
    THREAD_BUBBLE_TITLE_CLS,
    THREAD_PANEL_CLS,
    THREAD_PANEL_CLOSE_SEL,
    THREAD_PANEL_CLOSE_TIMEOUT_SEC,
    THREAD_PANEL_OPEN_TIMEOUT_SEC,
    YANDEX_BROWSER_BINARY,
    YANDEX_DRIVER,
)


class ConfigTests(unittest.TestCase):
    """Sanity checks on constants imported by runner and models.

    These tests catch accidental edits (empty selectors, relative paths) but
    do not prove selectors still match live SberChat DOM — that is covered by
    test_models collect_messages fixtures and manual Sigma runs.
    """

    def test_browser_paths_are_absolute(self) -> None:
        # Selenium Service and Options.binary_location require absolute paths.
        # YANDEX_DRIVER uses expanduser() in config — still absolute after ~ resolution.
        self.assertTrue(SBERBROWSER_BINARY.startswith("/"))
        self.assertTrue(SBERBROWSER_DRIVER.startswith("/"))
        self.assertTrue(YANDEX_BROWSER_BINARY.startswith("/"))
        self.assertTrue(YANDEX_DRIVER.startswith("/"))

    def test_selector_constants_are_non_empty(self) -> None:
        # Empty class names would silently match nothing in BeautifulSoup.
        for value in (
            MSG_WRAP_CLS,
            MSG_SENDER_URL_SEL,
            MSG_SENDER_NAME_CLS,
            MSG_CONTENT_SEL,
            MSG_LIST_CONTAINER_CLS,
            MSG_ATTACHMENT_CLS,
            MSG_IMAGE_WRAP_CLS,
            MSG_IMAGE_MEDIA_CLICKABLE_SUBCLS,
            IMAGE_GALLERY_WRAP_CLS,
            IMAGE_GALLERY_V2_WRAP_CLS,
            IMAGE_GALLERY_V2_DIALOG_CLS,
            IMAGE_GALLERY_IMG_CLS,
            IMAGE_GALLERY_TOPBAR_CLS,
            IMAGE_GALLERY_CLOSE_ICON_ARIA,
            IMAGE_GALLERY_DOWNLOAD_BTN_SEL,
            QUOTE_WRAP_CLS,
            QUOTE_SENDER_NAME_CLS,
            QUOTE_CONTENT_SEL,
            THREAD_BUBBLE_CLS,
            THREAD_BUBBLE_TITLE_CLS,
            THREAD_PANEL_CLS,
            THREAD_PANEL_CLOSE_SEL,
        ):
            self.assertTrue(value)

    def test_video_media_prefixes_are_non_empty(self) -> None:
        self.assertGreater(len(MSG_VIDEO_MEDIA_PREFIXES), 0)
        for prefix in MSG_VIDEO_MEDIA_PREFIXES:
            self.assertTrue(prefix)

    def test_max_empty_scroll_attempts_is_positive(self) -> None:
        # runner.run() empty-DOM branch (~60 iterations before abort).
        self.assertEqual(MAX_EMPTY_SCROLL_ATTEMPTS, 60)

    def test_main_scroll_sleep_sec(self) -> None:
        # Sleep between scroll iterations; 0.5 s gives React ~two render cycles.
        self.assertEqual(MAIN_SCROLL_SLEEP_SEC, 0.5)
        self.assertIsInstance(MAIN_SCROLL_SLEEP_SEC, float)

    def test_max_stall_scroll_attempts_is_positive(self) -> None:
        # runner.run() non-empty branch when seen_ids stops growing.
        self.assertEqual(MAX_STALL_SCROLL_ATTEMPTS, 8)

    def test_page_load_timeout_is_positive(self) -> None:
        self.assertGreater(PAGE_LOAD_TIMEOUT_SEC, 0)

    def test_status_intervals_are_positive(self) -> None:
        self.assertGreater(SETUP_STATUS_INTERVAL_SEC, 0)
        self.assertGreater(SCROLL_PROGRESS_INTERVAL, 0)

    def test_thread_timing_constants_are_positive(self) -> None:
        self.assertEqual(MAX_THREAD_STALL_ATTEMPTS, 60)
        self.assertEqual(MAX_THREAD_N_MINUS_ONE_STALL, 5)
        self.assertLess(MAX_THREAD_N_MINUS_ONE_STALL, MAX_THREAD_STALL_ATTEMPTS)
        self.assertEqual(MAX_THREAD_BOOTSTRAP_DOWN_STEPS, 15)
        self.assertEqual(MAX_THREAD_ROW_SEARCH_STEPS, 20)
        self.assertEqual(MAX_THREAD_OPEN_ATTEMPTS, 3)
        self.assertEqual(MAX_THREAD_PANEL_CLOSE_ATTEMPTS, 3)
        self.assertEqual(THREAD_PANEL_CLOSE_TIMEOUT_SEC, 3)
        self.assertEqual(THREAD_PANEL_OPEN_TIMEOUT_SEC, 10)
        self.assertEqual(MIN_FULL_IMAGE_PX, 90)
        self.assertEqual(IMAGE_GALLERY_OPEN_TIMEOUT_SEC, 10)
        self.assertEqual(IMAGE_GALLERY_LOAD_TIMEOUT_SEC, 15)
        self.assertEqual(IMAGE_GALLERY_FETCH_TIMEOUT_SEC, 25)
        self.assertEqual(IMAGE_GALLERY_CLOSE_TIMEOUT_SEC, 3)
        self.assertEqual(len(IMAGE_GALLERY_WRAP_CLASSES), 3)
        self.assertIn(IMAGE_GALLERY_WRAP_CLS, IMAGE_GALLERY_WRAP_CLASSES)


if __name__ == "__main__":
    unittest.main()
