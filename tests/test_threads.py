"""Unit tests for pageup.threads Selenium workflow.

Covers enrich_fresh_threads (skip plain messages, enrich success/failure/partial,
transient failure retry vs max-attempt marking, oldest-first ordering,
already-collected IDs, chat-closed batch skip, failure with main feed still
attempts next thread), _collect_thread_panel_replies inner-scroll dedupe and
stall at N−1/N, _close_panel with main-feed refocus (no Escape),
_find_row_in_main_feed, _scroll_feed_to_row, _click_thread_bubble locate-retry,
_find_visible_panel, _wait_panel_for_parent, _wait_panel_closed, _panel_soup,
JS click/focus/scroll, prepare_main_feed_scroll, _find_main_feed_container,
download_fresh_images (gallery full-resolution download, _resolve_image_ext /
magic-byte extension detection, _needs_image_download helper, no-op fast path,
row-not-found graceful skip).
Uses mocked driver — no live browser.
"""

import unittest
import base64
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)

from pageup.models import Entry, Message, ParsingTask
from pageup.threads import enrich_fresh_threads
from pageup.tools import moscow_timezone
from tests.fixtures import GROUP_URL, THREAD_PANEL_SAMPLE_HTML, thread_bubble, TS_2024_09_01, message_row


class EnrichFreshThreadsTests(unittest.TestCase):
    def _task(self) -> ParsingTask:
        return ParsingTask(
            name="threadtest",
            group_url=GROUP_URL,
            min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
        )

    def _threaded_message(self, *, reply_count: int = 2) -> Message:
        html = message_row(
            "parent-1",
            TS_2024_09_01,
            content="Question",
            thread_html=thread_bubble(reply_count),
        )
        return self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]

    def test_skips_messages_without_thread_bubble(self) -> None:
        html = message_row("plain-1", TS_2024_09_01, content="No thread")
        plain = self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]
        driver = MagicMock()
        with patch("pageup.threads.prepare_main_feed_scroll") as mock_prepare:
            result = enrich_fresh_threads(
                driver,
                self._task(),
                [plain],
                thread_collected_ids=set(),
            )
        self.assertEqual(result, [plain])
        driver.find_element.assert_not_called()
        mock_prepare.assert_not_called()

    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_enriches_message_with_replies(
        self, mock_collect, mock_prepare
    ) -> None:
        msg = self._threaded_message(reply_count=1)
        reply = Entry(
            message_id="reply-1",
            date=msg.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="Answer",
        )
        mock_collect.return_value = [reply]
        driver = MagicMock()
        collected: set[str] = set()
        result = enrich_fresh_threads(
            driver,
            self._task(),
            [msg],
            thread_collected_ids=collected,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].thread_replies, [reply])
        self.assertIn("parent-1", collected)
        mock_prepare.assert_called_once_with(driver)

    @patch("pageup.threads._main_feed_available", return_value=True)
    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_failure_leaves_thread_replies_null(
        self, mock_collect, mock_prepare, mock_feed
    ) -> None:
        msg = self._threaded_message()
        mock_collect.return_value = None
        driver = MagicMock()
        collected: set[str] = set()
        result = enrich_fresh_threads(
            driver,
            self._task(),
            [msg],
            thread_collected_ids=collected,
        )
        self.assertIsNone(result[0].thread_replies)
        self.assertEqual(collected, set())
        mock_prepare.assert_called_once_with(driver)
        mock_feed.assert_called_once_with(driver)

    @patch("pageup.threads._main_feed_available", return_value=True)
    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    @patch("pageup.threads.MAX_THREAD_OPEN_ATTEMPTS", 3)
    def test_transient_failure_not_marked_until_max_attempts(
        self, mock_collect, mock_prepare, mock_feed
    ) -> None:
        msg = self._threaded_message()
        mock_collect.return_value = None
        driver = MagicMock()
        collected: set[str] = set()
        attempts: dict[str, int] = {}

        for expected_count in (1, 2):
            enrich_fresh_threads(
                driver,
                self._task(),
                [msg],
                thread_collected_ids=collected,
                thread_open_attempts=attempts,
            )
            self.assertEqual(collected, set())
            self.assertEqual(attempts["parent-1"], expected_count)

        enrich_fresh_threads(
            driver,
            self._task(),
            [msg],
            thread_collected_ids=collected,
            thread_open_attempts=attempts,
        )
        self.assertEqual(collected, {"parent-1"})
        self.assertEqual(attempts["parent-1"], 3)
        self.assertEqual(mock_collect.call_count, 3)

    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_processes_thread_candidates_oldest_first(
        self, mock_collect, mock_prepare
    ) -> None:
        html_newer = message_row(
            "parent-new",
            str(1725271200 * 1000),
            content="Newer question",
            thread_html=thread_bubble(1),
        )
        html_older = message_row(
            "parent-old",
            str(1725181200 * 1000),
            content="Older question",
            thread_html=thread_bubble(1),
        )
        task = self._task()
        newer = task.collect_messages(BeautifulSoup(html_newer, "lxml"))[0]
        older = task.collect_messages(BeautifulSoup(html_older, "lxml"))[0]
        mock_collect.return_value = None
        driver = MagicMock()
        with patch("pageup.threads._main_feed_available", return_value=True):
            enrich_fresh_threads(
                driver,
                task,
                [newer, older],
                thread_collected_ids=set(),
            )
        self.assertEqual(
            [call.args[2].message_id for call in mock_collect.call_args_list],
            ["parent-old", "parent-new"],
        )

    @patch("pageup.threads._main_feed_available", return_value=True)
    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_failure_with_main_feed_still_attempts_next_thread(
        self, mock_collect, mock_prepare, mock_feed
    ) -> None:
        msg1 = self._threaded_message(reply_count=1)
        html2 = message_row(
            "parent-2",
            TS_2024_09_01,
            content="Second question",
            thread_html=thread_bubble(2),
        )
        msg2 = self._task().collect_messages(BeautifulSoup(html2, "lxml"))[0]
        mock_collect.side_effect = [None, None]
        driver = MagicMock()
        collected: set[str] = set()
        enrich_fresh_threads(
            driver,
            self._task(),
            [msg1, msg2],
            thread_collected_ids=collected,
        )
        self.assertEqual(mock_collect.call_count, 2)
        self.assertEqual(mock_feed.call_count, 2)

    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_partial_collection_allows_retry(
        self, mock_collect, mock_prepare
    ) -> None:
        msg = self._threaded_message()
        reply = Entry(
            message_id="reply-1",
            date=msg.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="Partial answer",
        )
        mock_collect.return_value = [reply]
        driver = MagicMock()
        collected: set[str] = set()
        result = enrich_fresh_threads(
            driver,
            self._task(),
            [msg],
            thread_collected_ids=collected,
        )
        self.assertEqual(len(result[0].thread_replies), 1)
        self.assertNotIn("parent-1", collected)
        mock_prepare.assert_called_once_with(driver)

    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_skips_already_collected_thread_ids(
        self, mock_collect, mock_prepare
    ) -> None:
        msg = self._threaded_message()
        driver = MagicMock()
        result = enrich_fresh_threads(
            driver,
            self._task(),
            [msg],
            thread_collected_ids={"parent-1"},
        )
        self.assertIsNone(result[0].thread_replies)
        mock_collect.assert_not_called()
        mock_prepare.assert_not_called()

    @patch("pageup.threads.prepare_main_feed_scroll")
    @patch("pageup.threads._open_thread_and_collect")
    def test_skips_remaining_threads_when_chat_closes(
        self, mock_collect, mock_prepare
    ) -> None:
        msg1 = self._threaded_message(reply_count=1)
        html2 = message_row(
            "parent-2",
            TS_2024_09_01,
            content="Second question",
            thread_html=thread_bubble(2),
        )
        msg2 = self._task().collect_messages(BeautifulSoup(html2, "lxml"))[0]
        mock_collect.return_value = None
        driver = MagicMock()
        collected: set[str] = set()
        with patch(
            "pageup.threads._main_feed_available", side_effect=[False]
        ):
            result = enrich_fresh_threads(
                driver,
                self._task(),
                [msg1, msg2],
                thread_collected_ids=collected,
            )
        mock_collect.assert_called_once()
        self.assertEqual(collected, {"parent-1", "parent-2"})
        self.assertIsNone(result[0].thread_replies)
        self.assertIsNone(result[1].thread_replies)
        mock_prepare.assert_called_once_with(driver)

    @patch("pageup.threads._log")
    @patch("pageup.threads._panel_soup")
    @patch("pageup.threads._panel_root_message_id", return_value="msg-root")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    def test_collect_thread_panel_replies_accumulates(
        self, mock_scroll, mock_sleep, mock_root, mock_panel_soup, mock_log
    ) -> None:
        from pageup.threads import _collect_thread_panel_replies

        task = self._task()
        driver = MagicMock()
        panel_soup = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")
        mock_panel_soup.return_value = panel_soup

        reply1 = Entry(
            message_id="reply-1",
            date=datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="First reply",
        )
        reply2 = Entry(
            message_id="reply-2",
            date=datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Carol",
            attachments=None,
            content="Second",
        )

        with patch(
            "pageup.models.ParsingTask.collect_thread_reply_entries",
            side_effect=[
                [reply1],
                [reply1, reply2],
            ],
        ):
            replies = _collect_thread_panel_replies(
                driver,
                task,
                parent_id="msg-root",
                expected_count=2,
            )
        self.assertEqual(len(replies), 2)
        mock_panel_soup.assert_called()

    @patch("pageup.threads._log")
    @patch("pageup.threads._panel_soup")
    @patch("pageup.threads._panel_root_message_id", return_value="wrong-root")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    def test_collect_stops_when_panel_root_differs(
        self, mock_scroll, mock_sleep, mock_root, mock_panel_soup, mock_log
    ) -> None:
        from pageup.threads import _collect_thread_panel_replies

        task = self._task()
        driver = MagicMock()
        mock_panel_soup.return_value = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")

        replies = _collect_thread_panel_replies(
            driver,
            task,
            parent_id="msg-root",
            expected_count=2,
        )
        self.assertEqual(replies, [])
        mock_log.assert_called()
        self.assertIn("panel root", mock_log.call_args.args[0])

    @patch("pageup.threads._find_visible_panel")
    @patch("pageup.threads._log")
    @patch("pageup.threads._panel_soup")
    @patch("pageup.threads._panel_root_message_id", return_value="parent-1")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    @patch("pageup.threads.MAX_THREAD_STALL_ATTEMPTS", 5)
    def test_single_reply_thread_uses_full_stall_limit(
        self,
        mock_scroll,
        mock_sleep,
        mock_root,
        mock_panel_soup,
        mock_log,
        mock_find_panel,
    ) -> None:
        from pageup.threads import _collect_thread_panel_replies

        task = self._task()
        driver = MagicMock()
        panel = MagicMock()
        mock_find_panel.return_value = panel
        panel.find_element.return_value = MagicMock()
        mock_panel_soup.return_value = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")

        with patch(
            "pageup.models.ParsingTask.collect_thread_reply_entries",
            return_value=[],
        ):
            replies = _collect_thread_panel_replies(
                driver,
                task,
                parent_id="parent-1",
                expected_count=1,
            )
        self.assertEqual(replies, [])
        # First iteration is free (stall_attempts starts at -1), so all
        # MAX_THREAD_STALL_ATTEMPTS=5 scroll steps execute before breaking.
        self.assertEqual(mock_scroll.call_count, 5)

    @patch("pageup.threads._find_visible_panel")
    @patch("pageup.threads._log")
    @patch("pageup.threads._panel_soup")
    @patch("pageup.threads._panel_root_message_id", return_value="parent-1")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    @patch("pageup.threads.MAX_THREAD_N_MINUS_ONE_STALL", 100)
    @patch("pageup.threads.MAX_THREAD_STALL_ATTEMPTS", 2)
    def test_stall_budget_when_one_reply_short(
        self,
        mock_scroll,
        mock_sleep,
        mock_root,
        mock_panel_soup,
        mock_log,
        mock_find_panel,
    ) -> None:
        from pageup.threads import _collect_thread_panel_replies

        task = self._task()
        driver = MagicMock()
        panel = MagicMock()
        mock_find_panel.return_value = panel
        panel.find_element.return_value = MagicMock()
        mock_panel_soup.return_value = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")
        reply = Entry(
            message_id="reply-1",
            date=datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="Only reply",
        )

        with patch(
            "pageup.models.ParsingTask.collect_thread_reply_entries",
            return_value=[reply],
        ):
            replies = _collect_thread_panel_replies(
                driver,
                task,
                parent_id="parent-1",
                expected_count=2,
            )
        self.assertEqual(len(replies), 1)
        self.assertEqual(mock_scroll.call_count, 2)
        self.assertIn("unparseable rows", mock_log.call_args.args[0])

    @patch("pageup.threads._find_visible_panel")
    @patch("pageup.threads._log")
    @patch("pageup.threads._panel_soup")
    @patch("pageup.threads._panel_root_message_id", return_value="parent-1")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    @patch("pageup.threads.MAX_THREAD_N_MINUS_ONE_STALL", 2)
    @patch("pageup.threads.MAX_THREAD_STALL_ATTEMPTS", 60)
    def test_n_minus_one_fast_exit_before_full_stall_budget(
        self,
        mock_scroll,
        mock_sleep,
        mock_root,
        mock_panel_soup,
        mock_log,
        mock_find_panel,
    ) -> None:
        """N-1/N fast exit fires well before the full 60 s stall budget."""
        from pageup.threads import _collect_thread_panel_replies

        task = self._task()
        driver = MagicMock()
        panel = MagicMock()
        mock_find_panel.return_value = panel
        panel.find_element.return_value = MagicMock()
        mock_panel_soup.return_value = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")
        reply = Entry(
            message_id="reply-1",
            date=datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="Only reply",
        )

        with patch(
            "pageup.models.ParsingTask.collect_thread_reply_entries",
            return_value=[reply],
        ):
            replies = _collect_thread_panel_replies(
                driver,
                task,
                parent_id="parent-1",
                expected_count=2,
            )
        self.assertEqual(len(replies), 1)
        # Fast exit fires at MAX_THREAD_N_MINUS_ONE_STALL=2 stall steps (not 60)
        # so only 2 scrolls are issued: iteration 1 (progress) + iteration 2 (first stall).
        self.assertEqual(mock_scroll.call_count, 2)
        self.assertIn("unparseable rows", mock_log.call_args.args[0])

    @patch("pageup.threads._find_visible_panel")
    @patch("pageup.threads._log")
    @patch("pageup.threads._panel_soup")
    @patch("pageup.threads._panel_root_message_id", return_value="parent-1")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    @patch("pageup.threads.MAX_THREAD_STALL_ATTEMPTS", 2)
    def test_single_reply_failure_does_not_log_unparseable(
        self,
        mock_scroll,
        mock_sleep,
        mock_root,
        mock_panel_soup,
        mock_log,
        mock_find_panel,
    ) -> None:
        from pageup.threads import _collect_thread_panel_replies

        task = self._task()
        driver = MagicMock()
        panel = MagicMock()
        mock_find_panel.return_value = panel
        panel.find_element.return_value = MagicMock()
        mock_panel_soup.return_value = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")

        with patch(
            "pageup.models.ParsingTask.collect_thread_reply_entries",
            return_value=[],
        ):
            replies = _collect_thread_panel_replies(
                driver,
                task,
                parent_id="parent-1",
                expected_count=1,
            )
        self.assertEqual(replies, [])
        for call in mock_log.call_args_list:
            self.assertNotIn("unparseable rows", call.args[0])


class ClosePanelTests(unittest.TestCase):
    @patch("pageup.threads._panel_is_open", return_value=False)
    def test_skips_work_when_panel_already_closed(self, mock_open) -> None:
        from pageup.threads import _close_panel

        driver = MagicMock()
        self.assertTrue(_close_panel(driver))
        driver.find_element.assert_not_called()

    @patch("pageup.threads._focus_main_feed")
    @patch("pageup.threads._safe_click")
    @patch("pageup.threads._wait_panel_closed", return_value=True)
    @patch("pageup.threads._panel_is_open", side_effect=[True, False])
    def test_closes_via_button(
        self, mock_open, mock_wait, mock_click, mock_focus
    ) -> None:
        from pageup.threads import _close_panel

        driver = MagicMock()
        panel = MagicMock()
        panel.is_displayed.return_value = True
        driver.find_elements.return_value = [panel]
        self.assertTrue(_close_panel(driver))
        driver.find_elements.assert_called()
        mock_click.assert_called()
        mock_focus.assert_not_called()

    @patch("pageup.threads._focus_main_feed")
    @patch("pageup.threads._safe_click")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._wait_panel_closed", return_value=False)
    @patch("pageup.threads._panel_is_open", side_effect=[True, False])
    def test_closes_via_main_feed_refocus(
        self, mock_open, mock_wait, mock_sleep, mock_click, mock_focus
    ) -> None:
        from pageup.threads import _close_panel

        driver = MagicMock()
        panel = MagicMock()
        panel.is_displayed.return_value = True
        driver.find_elements.return_value = [panel]
        self.assertTrue(_close_panel(driver))
        mock_focus.assert_called_once_with(driver)

    @patch("pageup.threads._focus_main_feed")
    @patch("pageup.threads._safe_click")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._wait_panel_closed", return_value=False)
    @patch("pageup.threads._panel_is_open", side_effect=[True, False])
    def test_closes_after_close_button_timeout(
        self, mock_open, mock_wait, mock_sleep, mock_click, mock_focus
    ) -> None:
        from pageup.threads import _close_panel

        driver = MagicMock()
        panel = MagicMock()
        panel.is_displayed.return_value = True
        driver.find_elements.return_value = [panel]
        self.assertTrue(_close_panel(driver))

    @patch("pageup.threads._log")
    @patch("pageup.threads._focus_main_feed")
    @patch("pageup.threads._safe_click")
    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._wait_panel_closed", return_value=False)
    @patch("pageup.threads._panel_is_open", return_value=True)
    def test_returns_false_when_panel_stays_open(
        self, mock_open, mock_wait, mock_sleep, mock_click, mock_focus, mock_log
    ) -> None:
        from pageup.config import MAX_THREAD_PANEL_CLOSE_ATTEMPTS
        from pageup.threads import _close_panel

        driver = MagicMock()
        panel = MagicMock()
        panel.is_displayed.return_value = True
        driver.find_elements.return_value = [panel]
        self.assertFalse(_close_panel(driver))
        self.assertEqual(mock_focus.call_count, MAX_THREAD_PANEL_CLOSE_ATTEMPTS)
        self.assertEqual(
            mock_log.call_count,
            MAX_THREAD_PANEL_CLOSE_ATTEMPTS + 1,
        )


class WaitPanelClosedTests(unittest.TestCase):
    @patch("pageup.threads._panel_is_open", return_value=False)
    @patch("pageup.threads.WebDriverWait")
    def test_returns_true_when_no_visible_panel(
        self, mock_wait_cls, mock_open
    ) -> None:
        from pageup.threads import _wait_panel_closed

        driver = MagicMock()
        mock_wait_cls.return_value.until.return_value = None
        self.assertTrue(_wait_panel_closed(driver, 3))
        mock_wait_cls.return_value.until.assert_called_once()

    @patch("pageup.threads._panel_is_open", return_value=True)
    @patch("pageup.threads.WebDriverWait")
    def test_returns_false_when_visible_panel_remains(
        self, mock_wait_cls, mock_open
    ) -> None:
        from pageup.threads import _wait_panel_closed

        driver = MagicMock()
        mock_wait_cls.return_value.until.side_effect = TimeoutException()
        self.assertFalse(_wait_panel_closed(driver, 3))


class WaitPanelForParentTests(unittest.TestCase):
    @patch("pageup.threads._panel_root_message_id", return_value="parent-1")
    @patch("pageup.threads._panel_is_open", return_value=True)
    @patch("pageup.threads.WebDriverWait")
    def test_returns_true_when_root_matches(
        self, mock_wait_cls, mock_open, mock_root
    ) -> None:
        from pageup.threads import _wait_panel_for_parent

        driver = MagicMock()
        mock_wait_cls.return_value.until.return_value = None
        self.assertTrue(_wait_panel_for_parent(driver, "parent-1", 10))

    @patch("pageup.threads._panel_root_message_id", return_value="other-parent")
    @patch("pageup.threads._panel_is_open", return_value=True)
    @patch("pageup.threads.WebDriverWait")
    def test_returns_false_when_root_differs(
        self, mock_wait_cls, mock_open, mock_root
    ) -> None:
        from pageup.threads import _wait_panel_for_parent

        driver = MagicMock()
        mock_wait_cls.return_value.until.side_effect = TimeoutException()
        self.assertFalse(_wait_panel_for_parent(driver, "parent-1", 10))


class SafeClickTests(unittest.TestCase):
    def test_uses_javascript_click(self) -> None:
        from pageup.threads import _safe_click

        driver = MagicMock()
        element = MagicMock()
        _safe_click(driver, element)
        driver.execute_script.assert_called_once_with(
            "arguments[0].click();", element
        )
        element.click.assert_not_called()


class FocusScrollTargetTests(unittest.TestCase):
    def test_sets_tabindex_and_focuses(self) -> None:
        from pageup.threads import _focus_scroll_target

        driver = MagicMock()
        element = MagicMock()
        _focus_scroll_target(driver, element)
        driver.execute_script.assert_called_once()
        self.assertIn("tabindex", driver.execute_script.call_args.args[0])


class ScrollContainerPageUpTests(unittest.TestCase):
    def test_adjusts_scroll_top(self) -> None:
        from pageup.threads import _scroll_container_page_up

        driver = MagicMock()
        container = MagicMock()
        _scroll_container_page_up(driver, container, pages=5)
        driver.execute_script.assert_called_once()
        self.assertIn("scrollTop", driver.execute_script.call_args.args[0])


class FindRowInMainFeedTests(unittest.TestCase):
    @patch("pageup.threads._find_main_feed_container")
    def test_returns_when_row_present_without_scrolling(
        self, mock_feed_cls
    ) -> None:
        from pageup.threads import _find_row_in_main_feed

        driver = MagicMock()
        main_feed = MagicMock()
        mock_feed_cls.return_value = main_feed
        main_feed.find_elements.return_value = [MagicMock()]

        _find_row_in_main_feed(driver, "parent-1")
        driver.execute_script.assert_not_called()

    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._scroll_container")
    @patch("pageup.threads._find_main_feed_container")
    def test_scrolls_feed_when_row_missing_initially(
        self, mock_feed_cls, mock_scroll, mock_sleep
    ) -> None:
        from pageup.threads import _find_row_in_main_feed

        driver = MagicMock()
        row = MagicMock()
        main_feed = MagicMock()
        mock_feed_cls.return_value = main_feed
        main_feed.find_elements.side_effect = [[], [row]]
        driver.execute_script.return_value = 0

        _find_row_in_main_feed(driver, "parent-1")
        mock_scroll.assert_called()
        mock_sleep.assert_called()


class ClickThreadBubbleTests(unittest.TestCase):
    @patch("pageup.threads._safe_click")
    @patch("pageup.threads._get_main_feed_row")
    def test_clicks_bubble_on_fresh_row(self, mock_get_row, mock_click) -> None:
        from pageup.threads import _click_thread_bubble

        driver = MagicMock()
        row = MagicMock()
        bubble = MagicMock()
        row.find_element.return_value = bubble
        mock_get_row.return_value = row

        _click_thread_bubble(driver, "parent-1")
        mock_click.assert_called_once_with(driver, bubble)

    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._safe_click")
    @patch("pageup.threads._get_main_feed_row")
    def test_retries_on_stale_element(
        self, mock_get_row, mock_click, mock_sleep
    ) -> None:
        from pageup.threads import _click_thread_bubble

        driver = MagicMock()
        row = MagicMock()
        bubble = MagicMock()
        row.find_element.return_value = bubble
        mock_get_row.side_effect = [
            StaleElementReferenceException("stale"),
            row,
        ]

        _click_thread_bubble(driver, "parent-1")
        self.assertEqual(mock_get_row.call_count, 2)
        mock_click.assert_called_once_with(driver, bubble)


class ScrollFeedToRowTests(unittest.TestCase):
    @patch("pageup.threads._find_main_feed_container")
    def test_executes_bounding_rect_scroll(self, mock_feed_cls) -> None:
        """JS uses getBoundingClientRect, not offsetTop."""
        from pageup.threads import _scroll_feed_to_row

        driver = MagicMock()
        main_feed = MagicMock()
        row = MagicMock()
        mock_feed_cls.return_value = main_feed
        main_feed.find_elements.return_value = [row]

        _scroll_feed_to_row(driver, "parent-1")

        script = driver.execute_script.call_args[0][0]
        self.assertIn("getBoundingClientRect", script)
        self.assertNotIn("offsetTop", script)

    @patch("pageup.threads._find_main_feed_container")
    def test_returns_silently_when_row_absent(self, mock_feed_cls) -> None:
        from pageup.threads import _scroll_feed_to_row

        driver = MagicMock()
        main_feed = MagicMock()
        mock_feed_cls.return_value = main_feed
        main_feed.find_elements.return_value = []

        _scroll_feed_to_row(driver, "parent-1")
        driver.execute_script.assert_not_called()


class OpenThreadAndCollectLocateRetryTests(unittest.TestCase):
    """_open_thread_and_collect retries locate→scroll→click on NoSuchElementException."""

    def _make_task(self) -> ParsingTask:
        return ParsingTask(
            name="test",
            group_url=GROUP_URL,
            min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
        )

    def _make_message(self) -> Message:
        html = message_row(
            "parent-1",
            TS_2024_09_01,
            content="hi",
            thread_html=thread_bubble(2),
        )
        return self._make_task().collect_messages(BeautifulSoup(html, "lxml"))[0]

    @patch("pageup.threads.time.sleep")
    @patch("pageup.threads._collect_thread_panel_replies")
    @patch("pageup.threads._bootstrap_thread_panel_scroll")
    @patch("pageup.threads._wait_panel_for_parent", return_value=True)
    @patch("pageup.threads._click_thread_bubble")
    @patch("pageup.threads._scroll_feed_to_row")
    @patch("pageup.threads._find_row_in_main_feed")
    @patch("pageup.threads._close_panel", return_value=True)
    def test_retries_click_when_row_vanishes_between_scroll_and_click(
        self,
        mock_close,
        mock_find_row,
        mock_scroll_to,
        mock_click,
        mock_wait_panel,
        mock_bootstrap,
        mock_collect,
        mock_sleep,
    ) -> None:
        from pageup.threads import _open_thread_and_collect

        reply = Entry(
            message_id="reply-1",
            date="2024-09-01T10:01:00+03:00",
            sender_url=None,
            sender_name="Alice",
            attachments=None,
            content="reply",
        )
        mock_collect.return_value = [reply]
        mock_click.side_effect = [NoSuchElementException("gone"), None]

        result = _open_thread_and_collect(MagicMock(), self._make_task(), self._make_message())

        self.assertEqual(mock_find_row.call_count, 2)
        self.assertEqual(mock_click.call_count, 2)
        self.assertIsNotNone(result)


class PrepareMainFeedScrollTests(unittest.TestCase):
    @patch("pageup.threads._focus_main_feed", return_value=True)
    @patch("pageup.threads._close_panel", return_value=True)
    @patch("pageup.threads._close_image_gallery")
    def test_focuses_main_feed_after_close(
        self, mock_close_gallery, mock_close, mock_focus
    ) -> None:
        from pageup.threads import prepare_main_feed_scroll

        driver = MagicMock()
        prepare_main_feed_scroll(driver)
        mock_close_gallery.assert_called_once_with(driver)
        mock_close.assert_called_once_with(driver)
        mock_focus.assert_called_once_with(driver)

    @patch("pageup.threads._log")
    @patch("pageup.threads._focus_main_feed", return_value=False)
    @patch("pageup.threads._close_panel", return_value=True)
    @patch("pageup.threads._close_image_gallery")
    def test_logs_when_main_feed_missing(
        self, mock_close_gallery, mock_close, mock_focus, mock_log
    ) -> None:
        from pageup.threads import prepare_main_feed_scroll

        driver = MagicMock()
        prepare_main_feed_scroll(driver)
        mock_close_gallery.assert_called_once_with(driver)
        mock_close.assert_called_once_with(driver)
        mock_log.assert_called_once()
        self.assertIn("group chat", mock_log.call_args.args[0])


class FindMainFeedContainerTests(unittest.TestCase):
    def test_panel_is_open_requires_visible_panel(self) -> None:
        from pageup.threads import _panel_is_open

        hidden = MagicMock()
        hidden.is_displayed.return_value = False
        driver = MagicMock()
        driver.find_elements.return_value = [hidden]
        self.assertFalse(_panel_is_open(driver))

    def test_find_visible_panel_skips_hidden_dom_node(self) -> None:
        from pageup.threads import _find_visible_panel

        hidden = MagicMock()
        hidden.is_displayed.return_value = False
        visible = MagicMock()
        visible.is_displayed.return_value = True
        driver = MagicMock()
        driver.find_elements.return_value = [hidden, visible]
        self.assertIs(_find_visible_panel(driver), visible)

    def test_find_visible_panel_raises_when_none_visible(self) -> None:
        from pageup.threads import _find_visible_panel

        hidden = MagicMock()
        hidden.is_displayed.return_value = False
        driver = MagicMock()
        driver.find_elements.return_value = [hidden]
        with self.assertRaises(NoSuchElementException):
            _find_visible_panel(driver)

    def test_returns_main_container_when_listed_first(self) -> None:
        from pageup.threads import _find_main_feed_container

        main_container = MagicMock()
        main_container.find_elements.return_value = []
        driver = MagicMock()
        driver.find_elements.return_value = [main_container]
        self.assertIs(_find_main_feed_container(driver), main_container)

    def test_returns_main_container_when_panel_also_present(self) -> None:
        from pageup.threads import _find_main_feed_container

        main_container = MagicMock()
        main_container.find_elements.return_value = []

        panel_container = MagicMock()
        panel_container.find_elements.return_value = [MagicMock()]

        driver = MagicMock()
        driver.find_elements.return_value = [panel_container, main_container]
        self.assertIs(_find_main_feed_container(driver), main_container)

    def test_raises_when_no_container(self) -> None:
        from pageup.threads import _find_main_feed_container

        driver = MagicMock()
        driver.find_elements.return_value = []
        with self.assertRaises(NoSuchElementException):
            _find_main_feed_container(driver)

    def test_raises_when_only_panel_container(self) -> None:
        from pageup.threads import _find_main_feed_container

        panel_container = MagicMock()
        panel_container.find_elements.return_value = [MagicMock()]
        driver = MagicMock()
        driver.find_elements.return_value = [panel_container]
        with self.assertRaises(NoSuchElementException):
            _find_main_feed_container(driver)


class DataurlExtTests(unittest.TestCase):
    """Unit tests for the _dataurl_ext MIME-to-extension mapping."""

    def test_jpeg_maps_to_jpg(self) -> None:
        from pageup.threads import _dataurl_ext
        self.assertEqual(_dataurl_ext("data:image/jpeg;base64,/9j/abc"), "jpg")

    def test_png_maps_to_png(self) -> None:
        from pageup.threads import _dataurl_ext
        self.assertEqual(_dataurl_ext("data:image/png;base64,iVBOR"), "png")

    def test_webp_maps_to_webp(self) -> None:
        from pageup.threads import _dataurl_ext
        self.assertEqual(_dataurl_ext("data:image/webp;base64,RIFF"), "webp")

    def test_unknown_mime_falls_back_to_bin(self) -> None:
        from pageup.threads import _dataurl_ext
        self.assertEqual(_dataurl_ext("data:image/tga;base64,abc"), "bin")

    def test_non_data_url_falls_back_to_bin(self) -> None:
        from pageup.threads import _dataurl_ext
        self.assertEqual(_dataurl_ext("blob:https://sberchat/abc"), "bin")


class ImageExtResolveTests(unittest.TestCase):
    """Unit tests for magic-byte extension detection and _resolve_image_ext."""

    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4
    _JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4
    _WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 4

    def test_octet_stream_png_bytes(self) -> None:
        from pageup.threads import _resolve_image_ext

        data_url = "data:application/octet-stream;base64,abc"
        self.assertEqual(_resolve_image_ext(data_url, self._PNG), "png")

    def test_octet_stream_jpeg_bytes(self) -> None:
        from pageup.threads import _resolve_image_ext

        data_url = "data:application/octet-stream;base64,abc"
        self.assertEqual(_resolve_image_ext(data_url, self._JPEG), "jpg")

    def test_bytes_override_wrong_mime(self) -> None:
        from pageup.threads import _resolve_image_ext

        data_url = "data:image/png;base64,abc"
        self.assertEqual(_resolve_image_ext(data_url, self._JPEG), "jpg")

    def test_known_mime_with_matching_bytes(self) -> None:
        from pageup.threads import _resolve_image_ext

        self.assertEqual(
            _resolve_image_ext("data:image/png;base64,x", self._PNG),
            "png",
        )

    def test_unknown_mime_and_unknown_bytes(self) -> None:
        from pageup.threads import _resolve_image_ext

        self.assertEqual(
            _resolve_image_ext("data:application/octet-stream;base64,x", b"NOTIMAGE"),
            "bin",
        )

    def test_empty_bytes_falls_back_to_mime(self) -> None:
        from pageup.threads import _resolve_image_ext

        self.assertEqual(
            _resolve_image_ext("data:image/jpeg;base64,", b""),
            "jpg",
        )

    def test_non_data_url_with_png_bytes(self) -> None:
        from pageup.threads import _resolve_image_ext

        self.assertEqual(_resolve_image_ext("blob:https://sberchat/abc", self._PNG), "png")

    def test_webp_header(self) -> None:
        from pageup.threads import _image_ext_from_bytes

        self.assertEqual(_image_ext_from_bytes(self._WEBP), "webp")

    def test_gif_header(self) -> None:
        from pageup.threads import _image_ext_from_bytes

        self.assertEqual(_image_ext_from_bytes(b"GIF89a" + b"\x00" * 4), "gif")

    def test_bmp_header(self) -> None:
        from pageup.threads import _image_ext_from_bytes

        self.assertEqual(_image_ext_from_bytes(b"BM" + b"\x00" * 4), "bmp")

    def test_tiff_header(self) -> None:
        from pageup.threads import _image_ext_from_bytes

        self.assertEqual(_image_ext_from_bytes(b"II*\x00" + b"\x00" * 4), "tiff")


class CloseImageGalleryTests(unittest.TestCase):
    """_close_image_gallery must click ✕, not the «Скачать» button."""

    def test_clicks_close_icon_not_last_button(self) -> None:
        from pageup.config import IMAGE_GALLERY_CLOSE_ICON_ARIA, IMAGE_GALLERY_WRAP_CLASSES
        from pageup.threads import _close_image_gallery

        driver = MagicMock()
        driver.execute_script.return_value = True
        _close_image_gallery(driver)
        driver.execute_script.assert_called_once()
        js, wrap_classes, topbar_cls, close_icon = driver.execute_script.call_args.args
        self.assertEqual(list(wrap_classes), list(IMAGE_GALLERY_WRAP_CLASSES))
        self.assertEqual(close_icon, IMAGE_GALLERY_CLOSE_ICON_ARIA)
        self.assertIn("galleryIsShown", js)
        self.assertIn("buttons[0].click()", js)
        self.assertNotIn("offsetParent", js)
        self.assertNotIn("buttons[-1]", js)

    def test_selenium_fallback_when_js_close_fails(self) -> None:
        from pageup.threads import _close_image_gallery

        driver = MagicMock()
        driver.execute_script.return_value = False

        with patch("pageup.threads._gallery_is_open", side_effect=[True, False]):
            with patch(
                "pageup.threads._close_image_gallery_selenium"
            ) as mock_sel:
                with patch("pageup.threads.WebDriverWait") as mock_wait_cls:
                    mock_wait_cls.return_value.until_not.return_value = None
                    _close_image_gallery(driver)

        driver.execute_script.assert_called_once()
        mock_sel.assert_called_once()


class FetchGalleryImageTests(unittest.TestCase):
    def test_passes_wrap_classes_to_async_script(self) -> None:
        import base64
        from pageup.config import IMAGE_GALLERY_WRAP_CLASSES
        from pageup.threads import _fetch_gallery_image

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "w": 200,
            "h": 150,
        }
        result = _fetch_gallery_image(driver)
        self.assertIsNotNone(result)
        args = driver.execute_async_script.call_args.args
        self.assertEqual(list(args[1]), list(IMAGE_GALLERY_WRAP_CLASSES))
        self.assertEqual(args[3], 25_000)

    def test_returns_skip_sentinel_for_video_gallery(self) -> None:
        from pageup.threads import _VIDEO_GALLERY_SKIP, _fetch_gallery_image

        driver = MagicMock()
        driver.execute_async_script.return_value = {"skipVideo": True}
        self.assertIs(_fetch_gallery_image(driver), _VIDEO_GALLERY_SKIP)

    def test_gallery_has_video_matches_video_media_prefixes(self) -> None:
        from pageup.threads import _FETCH_GALLERY_IMAGE_JS

        self.assertIn("VideoMedia-", _FETCH_GALLERY_IMAGE_JS)
        self.assertIn("PhotoVideoMedia-", _FETCH_GALLERY_IMAGE_JS)
        self.assertNotIn('[class*="Video-"]', _FETCH_GALLERY_IMAGE_JS)


class ImageClickTargetsTests(unittest.TestCase):
    """_image_click_targets must match image DOM, not video sibling clickables."""

    def test_keeps_clickable_with_image_wrap_descendant(self) -> None:
        from pageup.threads import _image_click_targets

        wrap = MagicMock()
        clickable = MagicMock()
        clickable.find_element.return_value = wrap
        row = MagicMock()
        row.find_elements.return_value = [clickable]

        result = _image_click_targets(row)
        self.assertEqual(result, [clickable])
        clickable.find_element.assert_called_once()

    def test_skips_clickable_without_image_wrap(self) -> None:
        from pageup.threads import _image_click_targets

        video_clickable = MagicMock()
        video_clickable.find_element.side_effect = NoSuchElementException()
        row = MagicMock()
        row.find_elements.return_value = [video_clickable]

        self.assertEqual(_image_click_targets(row), [])


class DownloadImageElementsTests(unittest.TestCase):
    """Integration tests for _download_image_elements gallery download path."""

    @staticmethod
    def _row_with_click_targets(*targets: MagicMock) -> MagicMock:
        row = MagicMock()
        row.find_elements.side_effect = [list(targets)] * 4
        return row

    def test_gallery_png_saves_at_full_resolution(self) -> None:
        from pageup.threads import _download_image_elements

        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (200).to_bytes(4, "big")
            + (150).to_bytes(4, "big")
            + b"\x00" * 20
        )
        gallery_img = MagicMock()
        click_target = MagicMock()
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "w": 200,
            "h": 150,
        }

        with patch("pageup.threads._safe_click"), patch(
            "pageup.threads._close_image_gallery"
        ), patch("pageup.threads._log") as mock_log:
            with tempfile.TemporaryDirectory() as tmp:
                result = _download_image_elements(
                    driver,
                    tmp,
                    "msg123",
                    self._row_with_click_targets(click_target),
                    [None],
                )
                self.assertEqual(result, ["msg123_0.png"])
                saved = Path(tmp) / "attachments" / "msg123_0.png"
                self.assertTrue(saved.is_file())
                self.assertEqual(saved.read_bytes(), png)
                self.assertIn(
                    "🖼️ Image: saved msg123_0.png",
                    mock_log.call_args.args[0],
                )

    def test_wide_image_passes_max_dimension_gate(self) -> None:
        from pageup.threads import _download_image_elements

        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (640).to_bytes(4, "big")
            + (48).to_bytes(4, "big")
            + b"\x00" * 20
        )
        gallery_img = MagicMock()
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "w": 640,
            "h": 48,
        }

        with patch("pageup.threads._safe_click"), patch(
            "pageup.threads._close_image_gallery"
        ):
            with tempfile.TemporaryDirectory() as tmp:
                result = _download_image_elements(
                    driver,
                    tmp,
                    "msg456",
                    self._row_with_click_targets(MagicMock()),
                    [None],
                )
                self.assertEqual(result, ["msg456_0.png"])

    def test_skips_video_gallery_without_waiting(self) -> None:
        from pageup.threads import _VIDEO_GALLERY_SKIP, _download_image_elements

        driver = MagicMock()
        driver.execute_async_script.return_value = {"skipVideo": True}

        with patch("pageup.threads._safe_click"), patch(
            "pageup.threads._close_image_gallery"
        ), patch("pageup.threads._log") as mock_log:
            with tempfile.TemporaryDirectory() as tmp:
                result = _download_image_elements(
                    driver,
                    tmp,
                    "msg-vid",
                    self._row_with_click_targets(MagicMock()),
                    [None],
                )

        self.assertEqual(result, [""])
        self.assertTrue(
            any(
                "skipping video attachment" in call.args[0]
                for call in mock_log.call_args_list
            )
        )

    def test_requeries_click_targets_for_each_pending_slot(self) -> None:
        from pageup.threads import _download_image_elements

        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (200).to_bytes(4, "big")
            + (200).to_bytes(4, "big")
            + b"\x00" * 20
        )
        row = MagicMock()
        row.find_elements.side_effect = [
            [MagicMock(name="preview-0"), MagicMock(name="preview-1")],
            [MagicMock(name="preview-0"), MagicMock(name="preview-1")],
        ]
        driver = MagicMock()
        driver.execute_async_script.side_effect = [
            {
                "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
                "w": 200,
                "h": 200,
            },
            {
                "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
                "w": 200,
                "h": 200,
            },
        ]

        with patch("pageup.threads._safe_click"), patch(
            "pageup.threads._close_image_gallery"
        ):
            with tempfile.TemporaryDirectory() as tmp:
                result = _download_image_elements(
                    driver, tmp, "msg-multi", row, [None, None]
                )

        self.assertEqual(result, ["msg-multi_0.png", "msg-multi_1.png"])
        self.assertEqual(row.find_elements.call_count, 2)

    def test_rejects_preview_sized_payload(self) -> None:
        from pageup.threads import _download_image_elements

        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (90).to_bytes(4, "big")
            + (60).to_bytes(4, "big")
            + b"\x00" * 20
        )
        gallery_img = MagicMock()
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "w": 200,
            "h": 150,
        }

        with patch("pageup.threads._safe_click"), patch(
            "pageup.threads._close_image_gallery"
        ), patch("pageup.threads._log") as mock_log:
            with tempfile.TemporaryDirectory() as tmp:
                result = _download_image_elements(
                    driver,
                    tmp,
                    "msg123",
                    self._row_with_click_targets(MagicMock()),
                    [None],
                )

        self.assertEqual(result, [None])
        self.assertTrue(
            any(
                "⚠️ Image: rejecting msg123_0" in call.args[0]
                for call in mock_log.call_args_list
            )
        )

    def test_logs_when_fewer_previews_than_pending_slots(self) -> None:
        from pageup.threads import _download_image_elements

        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (200).to_bytes(4, "big")
            + (200).to_bytes(4, "big")
            + b"\x00" * 20
        )
        row = MagicMock()
        row.find_elements.side_effect = [
            [MagicMock()],
            [],
        ]
        driver = MagicMock()
        driver.execute_async_script.return_value = {
            "dataUrl": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "w": 200,
            "h": 200,
        }

        with patch("pageup.threads._safe_click"), patch(
            "pageup.threads._close_image_gallery"
        ), patch("pageup.threads._log") as mock_log:
            with tempfile.TemporaryDirectory() as tmp:
                result = _download_image_elements(
                    driver, tmp, "msg-two", row, [None, None]
                )

        self.assertEqual(result, ["msg-two_0.png", None])
        self.assertTrue(
            any(
                "⚠️ Image: 1 slot(s) still pending" in call.args[0]
                for call in mock_log.call_args_list
            )
        )


class DownloadImagesForRowTests(unittest.TestCase):
    def test_logs_when_row_has_no_clickable_previews(self) -> None:
        from pageup.threads import _download_images_for_row

        row = MagicMock()
        row.find_elements.return_value = []
        with patch("pageup.threads._log") as mock_log:
            result = _download_images_for_row(
                MagicMock(), "/tmp/test", "msg1", row, [None]
            )

        self.assertEqual(result, [None])
        self.assertIn(
            "⚠️ Image: no clickable previews in row for 'msg1'",
            mock_log.call_args.args[0],
        )


class ImagePixelMaxDimTests(unittest.TestCase):
    """Unit tests for _image_pixel_max_dim."""

    def test_png_ihdr(self) -> None:
        from pageup.threads import _image_pixel_max_dim

        raw = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (800).to_bytes(4, "big")
            + (600).to_bytes(4, "big")
        )
        self.assertEqual(_image_pixel_max_dim(raw), 800)

    def test_jpeg_sof(self) -> None:
        from pageup.threads import _image_pixel_max_dim

        raw = (
            b"\xff\xd8\xff"
            + b"\xc0"
            + b"\x00\x11"
            + b"\x08"
            + (480).to_bytes(2, "big")
            + (640).to_bytes(2, "big")
            + b"\x00" * 4
        )
        self.assertEqual(_image_pixel_max_dim(raw), 640)


class NeedsImageDownloadTests(unittest.TestCase):
    """Tests for the _needs_image_download helper."""

    def test_returns_false_for_none_attachments(self) -> None:
        from pageup.threads import _needs_image_download
        self.assertFalse(_needs_image_download(None))

    def test_returns_false_when_all_downloaded(self) -> None:
        from pageup.threads import _needs_image_download
        self.assertFalse(
            _needs_image_download(["report_0.jpg"])
        )

    def test_returns_false_when_all_downloaded_multiple(self) -> None:
        from pageup.threads import _needs_image_download
        self.assertFalse(
            _needs_image_download(["123_0.jpg", "123_1.png"])
        )

    def test_returns_true_when_slot_is_none(self) -> None:
        from pageup.threads import _needs_image_download
        self.assertTrue(
            _needs_image_download([None])
        )

    def test_returns_false_when_slot_is_skipped_video_marker(self) -> None:
        from pageup.threads import _needs_image_download
        self.assertFalse(_needs_image_download([""]))

    def test_returns_true_when_mixed_slots(self) -> None:
        from pageup.threads import _needs_image_download
        self.assertTrue(
            _needs_image_download(["123_0.jpg", None])
        )


class DownloadFreshImagesTests(unittest.TestCase):
    """Tests for download_fresh_images — no-op fast path and error handling."""

    def test_returns_unchanged_when_no_images(self) -> None:
        from pageup.threads import download_fresh_images
        from pageup.models import Message
        msg = Message(
            message_id="m1",
            date=str(TS_2024_09_01),
            sender_url=None,
            sender_name="Alice",
            quotes=None,
            attachments=["already_downloaded_0.jpg"],
            content="text",
        )
        driver = MagicMock()
        result = download_fresh_images(driver, "/tmp/test", [msg])
        self.assertEqual(result, [msg])
        driver.set_script_timeout.assert_not_called()

    def test_returns_unchanged_list_when_no_messages(self) -> None:
        from pageup.threads import download_fresh_images
        driver = MagicMock()
        result = download_fresh_images(driver, "/tmp/test", [])
        self.assertEqual(result, [])
        driver.set_script_timeout.assert_not_called()

    def test_logs_and_skips_when_row_not_found(self) -> None:
        from pageup.threads import download_fresh_images
        from pageup.models import Message
        msg = Message(
            message_id="img-1",
            date=str(TS_2024_09_01),
            sender_url=None,
            sender_name="Alice",
            quotes=None,
            attachments=[None],
            content="",
        )
        driver = MagicMock()
        driver.set_script_timeout = MagicMock()
        with patch("pageup.threads._find_row_in_main_feed",
                   side_effect=NoSuchElementException("not found")):
            with patch("pageup.threads._log") as mock_log:
                result = download_fresh_images(driver, "/tmp/test", [msg])
        self.assertEqual(len(result), 1)
        # Row not found → attachment slot stays None (download skipped).
        self.assertEqual(result[0].attachments, [None])
        self.assertTrue(any("img-1" in str(call) for call in mock_log.call_args_list))


if __name__ == "__main__":
    unittest.main()
