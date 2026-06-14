"""Unit tests for pageup.config constants.

Smoke checks that compile-time constants are present and sane, including
scroll safety limits (``MAX_EMPTY_SCROLL_ATTEMPTS``, ``MAX_STALL_SCROLL_ATTEMPTS``).
Does not validate selectors against a live SberChat DOM — that is covered indirectly
by tests.test_models HTML fixture parsing.
"""

import unittest

from pageup.config import (
    ATTACH_NAME_CLS,
    ATTACH_SIZE_CLS,
    MAX_EMPTY_SCROLL_ATTEMPTS,
    MAX_STALL_SCROLL_ATTEMPTS,
    MSG_ATTACHMENT_CLS,
    MSG_CONTENT_SEL,
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
            MSG_ATTACHMENT_CLS,
            ATTACH_NAME_CLS,
            ATTACH_SIZE_CLS,
            QUOTE_WRAP_CLS,
            QUOTE_SENDER_NAME_CLS,
            QUOTE_CONTENT_SEL,
        ):
            self.assertTrue(value)

    def test_max_empty_scroll_attempts_is_positive(self) -> None:
        # runner.run() empty-DOM branch; paired with MAX_STALL_SCROLL_ATTEMPTS
        # (both 60 → ~60 s of 1 s sleeps before each abort path).
        self.assertEqual(MAX_EMPTY_SCROLL_ATTEMPTS, 60)

    def test_max_stall_scroll_attempts_is_positive(self) -> None:
        # runner.run() non-empty branch when seen_ids stops growing.
        self.assertEqual(MAX_STALL_SCROLL_ATTEMPTS, 60)

    def test_page_load_timeout_is_positive(self) -> None:
        self.assertGreater(PAGE_LOAD_TIMEOUT_SEC, 0)

    def test_status_intervals_are_positive(self) -> None:
        self.assertGreater(SETUP_STATUS_INTERVAL_SEC, 0)
        self.assertGreater(SCROLL_PROGRESS_INTERVAL, 0)


if __name__ == "__main__":
    unittest.main()
