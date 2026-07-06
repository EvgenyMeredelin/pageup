"""Unit tests for pageup.runner.

create_driver tests verify WebDriver options/service wiring (Sberbrowser +
sberdriver and Yandex Browser + YandexDriver) without a real browser.
run tests mock WebDriver.page_source via PropertyMock (side_effect for multiple
scroll iterations) and patch write_json to avoid disk I/O.

Key scenarios:
    Final-batch in-range tail preserved when is_done triggers (regression fix)
    KeyboardInterrupt during mkdir, driver launch, navigation, setup countdown, or
    scroll loop calls _finish / write_json
    Unexpected errors during scroll call _finish with partial collection before re-raise
    Navigation TimeoutException on driver.get continues the run
    Empty DOM triggers scroll instead of hang
    MAX_EMPTY_SCROLL_ATTEMPTS aborts after repeated empty pages
    MAX_STALL_SCROLL_ATTEMPTS aborts when history is exhausted before min_date
    Stall counter resets when a fresh message_id appears
    is_done completion takes precedence over stall abort
    is_done with no in-range rows on final snapshot writes empty JSON
    Non-terminal extend excludes rows before min_date (DOM order != chronology)
    Stall progress logging mirrors empty-scroll progress logging
    Stall does not inflate collected-so-far or Messages collected counts
    ThreadRetryExtendTests verifies thread retry for already-collected rows
    enrich_fresh_threads and prepare_main_feed_scroll are patched in setUp
    (thread Selenium workflow tested separately in test_threads.py;
    PrepareBeforeScrollTests verifies prepare is invoked before PAGE_UP,
    including on the empty-DOM scroll branch)
"""

import re
import unittest
from datetime import datetime
from unittest.mock import MagicMock, PropertyMock, patch

from bs4 import BeautifulSoup

from selenium.common.exceptions import TimeoutException, WebDriverException

from pageup.config import (
    MAX_EMPTY_SCROLL_ATTEMPTS,
    MAX_STALL_SCROLL_ATTEMPTS,
    PAGE_LOAD_TIMEOUT_SEC,
    SBERBROWSER_BINARY,
    SBERBROWSER_DRIVER,
    YANDEX_BROWSER_BINARY,
    YANDEX_DRIVER,
)
from pageup.models import ParsingTask
from pageup.runner import create_driver, run
from pageup.tools import moscow_timezone
from tests.fixtures import (
    GROUP_URL,
    TS_2024_01_15,
    TS_2024_09_01,
    image_attachment_block,
    message_row,
    thread_bubble,
    TWO_MESSAGES_HTML,
)


class CreateDriverTests(unittest.TestCase):
    """Driver factory branches: Sberbrowser + sberdriver vs Yandex Browser + YandexDriver."""

    @patch("pageup.runner.Chrome")
    @patch("pageup.runner.Service")
    def test_trusted_device_configures_sberbrowser(
        self, mock_service, mock_chrome
    ) -> None:
        # Patch at runner module level — create_driver imports Chrome/Service there.
        mock_chrome.return_value = MagicMock()
        create_driver(trusted_device=True)
        mock_chrome.assert_called_once()
        kwargs = mock_chrome.call_args.kwargs
        options = kwargs["options"]
        # Must match 83b9d71 Sigma driver setup exactly.
        self.assertEqual(options.binary_location, SBERBROWSER_BINARY)
        self.assertNotEqual(options.page_load_strategy, "eager")
        # Two separate flags (83b9d71).  Chromium keeps the last value only —
        # never combine into "SberAuth,SberSync" (e052dda regression).
        self.assertIn("--disable-features=SberAuth", options.arguments)
        self.assertIn("--disable-features=SberSync", options.arguments)
        self.assertFalse(
            any("SberAuth,SberSync" in arg for arg in options.arguments),
            "Combined disable-features disables SberAuth and breaks Kerberos",
        )
        self.assertEqual(kwargs["service"], mock_service.return_value)
        mock_service.assert_called_once_with(SBERBROWSER_DRIVER)
        mock_chrome.return_value.set_page_load_timeout.assert_not_called()
        mock_chrome.return_value.execute_cdp_cmd.assert_called_once_with(
            "Page.setDownloadBehavior", {"behavior": "deny"}
        )

    @patch("pageup.runner.Chrome")
    @patch("pageup.runner.Service")
    def test_personal_device_configures_yandex_browser(
        self, mock_service, mock_chrome
    ) -> None:
        # Personal branch must pass YANDEX_DRIVER to Service, not SBERBROWSER_DRIVER.
        mock_chrome.return_value = MagicMock()
        create_driver(trusted_device=False)
        mock_chrome.assert_called_once()
        kwargs = mock_chrome.call_args.kwargs
        options = kwargs["options"]
        self.assertEqual(options.binary_location, YANDEX_BROWSER_BINARY)
        self.assertEqual(options.page_load_strategy, "eager")
        self.assertEqual(kwargs["service"], mock_service.return_value)
        mock_service.assert_called_once_with(YANDEX_DRIVER)
        mock_chrome.return_value.set_page_load_timeout.assert_called_once_with(
            PAGE_LOAD_TIMEOUT_SEC
        )
        mock_chrome.return_value.execute_cdp_cmd.assert_called_once_with(
            "Page.setDownloadBehavior", {"behavior": "deny"}
        )

    @patch("pageup.runner.Chrome")
    @patch("pageup.runner.Service")
    def test_cdp_webdriver_exception_is_swallowed(
        self, mock_service, mock_chrome
    ) -> None:
        # Regression guard: a 0.2.0 build lacked the WebDriverException import,
        # turning this expected, caught failure into an uncaught NameError that
        # left Sberbrowser orphaned on data:, (see runner.py create_driver).
        mock_driver = MagicMock()
        mock_driver.execute_cdp_cmd.side_effect = WebDriverException("cdp unsupported")
        mock_chrome.return_value = mock_driver

        driver = create_driver(trusted_device=True)

        self.assertIs(driver, mock_driver)
        mock_driver.quit.assert_not_called()

    @patch("pageup.runner.Chrome")
    @patch("pageup.runner.Service")
    def test_unexpected_failure_quits_driver_before_reraising(
        self, mock_service, mock_chrome
    ) -> None:
        # Any failure other than the expected CDP WebDriverException must not
        # leak the already-launched browser window: quit it, then propagate.
        mock_driver = MagicMock()
        mock_driver.execute_cdp_cmd.side_effect = RuntimeError("boom")
        mock_chrome.return_value = mock_driver

        with self.assertRaises(RuntimeError):
            create_driver(trusted_device=True)

        mock_driver.quit.assert_called_once()

    @patch("pageup.runner.Chrome")
    @patch("pageup.runner.Service")
    def test_personal_device_timeout_failure_quits_driver_before_reraising(
        self, mock_service, mock_chrome
    ) -> None:
        # Personal-device branch: set_page_load_timeout runs before the CDP call
        # and must be covered by the same cleanup guard.
        mock_driver = MagicMock()
        mock_driver.set_page_load_timeout.side_effect = WebDriverException("no such window")
        mock_chrome.return_value = mock_driver

        with self.assertRaises(WebDriverException):
            create_driver(trusted_device=False)

        mock_driver.quit.assert_called_once()
        mock_driver.execute_cdp_cmd.assert_not_called()


class RunTests(unittest.TestCase):
    """Scroll loop behaviour with mocked driver and shortened sleep_time."""

    def setUp(self) -> None:
        # Thread enrichment requires live Selenium — pass through in unit tests.
        self._enrich_patcher = patch(
            "pageup.runner.enrich_fresh_threads",
            side_effect=lambda driver, task, fresh, thread_collected_ids, thread_open_attempts=None, write_dir=None, on_message_enriched=None: fresh,
        )
        self._enrich_patcher.start()
        # prepare_main_feed_scroll uses real WebDriverWait; MagicMock drivers
        # appear to have an open panel forever → 10 s timeouts per scroll step.
        self._prepare_patcher = patch("pageup.runner.prepare_main_feed_scroll")
        self._prepare_patcher.start()

    def tearDown(self) -> None:
        self._prepare_patcher.stop()
        self._enrich_patcher.stop()

    # page_source is a property on WebDriver — PropertyMock side_effect yields
    # different HTML on each loop iteration without a real browser.

    def _task(self) -> ParsingTask:
        return ParsingTask(
            name="runtest",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_stops_and_writes_in_range_tail(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # Regression: final page must not drop in-range messages when is_done fires.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()

        new_only = message_row("msg-new", TS_2024_09_01, content="new")
        # Batch 1: single new message; batch 2: old+new, oldest triggers is_done.
        type(mock_driver).page_source = PropertyMock(
            side_effect=[new_only, TWO_MESSAGES_HTML]
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_driver.quit.assert_called_once()
        mock_write.assert_called_once()
        messages = mock_write.call_args.args[0]
        # First batch adds msg-new; second batch overlaps but does not re-append it.
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "msg-new")
        self.assertNotIn("msg-old", {m.message_id for m in messages})

    # ── KeyboardInterrupt (partial output) ───────────────────────────────────
    # run() catches KeyboardInterrupt at any phase and always calls _finish so
    # partial collections are written; driver.quit runs in finally.

    @patch("builtins.print")
    @patch("pageup.runner.create_driver")
    def test_run_keyboard_interrupt_during_mkdir_finishes(
        self, mock_create, _mock_print
    ) -> None:
        # Earliest possible interrupt — before the browser is even launched.
        # first mkdir (Phase 1) raises; the second is _finish's own mkdir, which
        # must still succeed so partial (here empty) output is written.
        with patch(
            "pageup.runner.Path.mkdir",
            side_effect=[KeyboardInterrupt(), None],
        ) as mock_mkdir:
            with patch.object(ParsingTask, "write_json") as mock_write:
                run(
                    self._task(),
                    trusted_device=False,
                    sleep_time=1,
                    write_dir="/tmp/runtest-out",
                )

        mock_create.assert_not_called()
        self.assertEqual(mock_mkdir.call_count, 2)
        mock_write.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.create_driver")
    def test_run_keyboard_interrupt_during_driver_create_finishes(
        self, mock_create, _mock_print
    ) -> None:
        # Interrupt while the WebDriver is starting (create_driver raises).
        # driver stays None, so the finally block must not call driver.quit,
        # but _finish still runs to persist the empty collection.
        mock_create.side_effect = KeyboardInterrupt

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.create_driver")
    def test_run_keyboard_interrupt_during_navigation_finishes(
        self, mock_create, mock_actions_cls, _mock_print
    ) -> None:
        # Interrupt during driver.get() — the operator aborts auth/navigation.
        # The driver exists here, so both _finish and driver.quit must run.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        mock_driver.get.side_effect = KeyboardInterrupt

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_keyboard_interrupt_during_countdown_finishes(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()

        def sleep_side_effect(_seconds: float) -> None:
            # Interrupt during setup countdown (before scroll loop starts).
            if mock_sleep.call_count == 1:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=2,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_keyboard_interrupt_finishes_partial(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=[message_row("new", TS_2024_09_01, content="new")]
        )

        def sleep_side_effect(_seconds: float) -> None:
            # Interrupt during scroll loop (after setup countdown).
            if mock_sleep.call_count > 1:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_writes_partial_on_unexpected_scroll_error(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        good_html = message_row("partial", TS_2024_09_01, content="keep")
        type(mock_driver).page_source = PropertyMock(
            side_effect=[good_html, RuntimeError("DOM read failed")]
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            with self.assertRaises(RuntimeError):
                run(
                    self._task(),
                    trusted_device=False,
                    sleep_time=1,
                    write_dir="/tmp/runtest-out",
                )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()
        messages = mock_write.call_args.args[0]
        self.assertEqual([m.message_id for m in messages], ["partial"])

    # ── Empty-DOM abort (MAX_EMPTY_SCROLL_ATTEMPTS) ──────────────────────────
    # When collect_messages returns [] the runner scrolls without extending;
    # stall_attempts is not reset in this branch (independent guard).

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_scrolls_when_no_messages_visible(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions = MagicMock()
        mock_actions_cls.return_value = mock_actions

        empty_html = "<html><body></body></html>"
        new_html = message_row("new", TS_2024_01_15, content="old enough")
        type(mock_driver).page_source = PropertyMock(
            side_effect=[empty_html, new_html]
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        # Scrolled once on empty page, then collected and terminated.
        self.assertGreaterEqual(mock_actions.perform.call_count, 1)
        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_stops_after_max_empty_scroll_attempts(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # Never yields message rows — hits MAX_EMPTY_SCROLL_ATTEMPTS guard.
        # Exactly MAX reads: empty_scroll_attempts reaches MAX on the last one.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=["<html></html>"] * MAX_EMPTY_SCROLL_ATTEMPTS
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_continues_after_navigation_timeout(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        # Personal-device eager page load can raise TimeoutException before
        # cert/OTP finish; run() must swallow it and proceed to the scroll loop
        # (asserted via the "Parsing started" log) rather than abort.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        mock_driver.get.side_effect = TimeoutException("page load timed out")
        done_html = message_row("old", TS_2024_01_15, content="old enough")
        type(mock_driver).page_source = PropertyMock(side_effect=[done_html])

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_driver.get.assert_called_once()
        mock_write.assert_called_once()
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("Page load timed out", printed)
        self.assertIn("Parsing started", printed)

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_logs_launch_before_navigation(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        # Ordering guard: the "Launching browser" log must precede driver.get()
        # so an operator watching the terminal sees the launch notice before the
        # (blocking, on Sigma) navigation step.  call_order interleaves prints
        # and a navigation sentinel to assert their relative order.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        call_order: list[str] = []

        def print_side(*args, **kwargs) -> None:
            if args:
                call_order.append(str(args[0]))

        def get_side(_url: str) -> None:
            call_order.append("__navigation__")

        mock_print.side_effect = print_side
        mock_driver.get.side_effect = get_side
        done_html = message_row("old", TS_2024_01_15, content="old enough")
        type(mock_driver).page_source = PropertyMock(side_effect=[done_html])

        with patch.object(ParsingTask, "write_json"):
            run(
                self._task(),
                trusted_device=True,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        launch_idx = next(
            i for i, msg in enumerate(call_order) if "Launching browser" in msg
        )
        nav_idx = call_order.index("__navigation__")
        self.assertLess(launch_idx, nav_idx)

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_logs_empty_scroll_progress(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        # Progress logging fires every SCROLL_PROGRESS_INTERVAL (10) empty
        # attempts — assert the "10/" milestone line so a stuck run is visible
        # to the operator rather than silently scrolling.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=["<html></html>"] * MAX_EMPTY_SCROLL_ATTEMPTS
        )

        with patch.object(ParsingTask, "write_json"):
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("No message rows yet", printed)
        self.assertIn("empty attempt 10/", printed)

    # ── Stall-scroll abort (MAX_STALL_SCROLL_ATTEMPTS) ───────────────────────
    # PropertyMock side_effect simulates repeated page_source snapshots.  Stall
    # tests use min_date far before fixture timestamps so is_done never fires
    # unless the test explicitly includes an old enough message row.

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_stops_when_history_exhausted_without_reaching_min_date(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        single_msg = message_row("msg-a", TS_2024_09_01, content="only")
        # First read discovers msg-a; reads 2..N increment stall; read N+1 aborts.
        type(mock_driver).page_source = PropertyMock(
            side_effect=[single_msg] * (MAX_STALL_SCROLL_ATTEMPTS + 1)
        )
        task = ParsingTask(
            name="runtest",
            group_url=GROUP_URL,
            min_date=datetime(2020, 1, 1, tzinfo=moscow_timezone),
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                task,
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()
        messages = mock_write.call_args.args[0]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "msg-a")
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("min_date may predate chat history", printed)
        self.assertIn("Messages collected: 1", printed)

    @patch("builtins.print")
    @patch("pageup.runner.MAX_STALL_SCROLL_ATTEMPTS", 5)
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_stall_resets_when_fresh_message_appears(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        msg_a = message_row("msg-a", TS_2024_09_01, content="a")
        msg_ab = msg_a + message_row("msg-b", TS_2024_09_01, content="b")
        # Four identical snapshots stall; msg-b appears → reset; five more stall → abort at 5.
        page_source_mock = PropertyMock(
            side_effect=[msg_a] * 4 + [msg_ab] + [msg_a] * 5
        )
        type(mock_driver).page_source = page_source_mock
        task = ParsingTask(
            name="runtest",
            group_url=GROUP_URL,
            min_date=datetime(2020, 1, 1, tzinfo=moscow_timezone),
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                task,
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()
        self.assertEqual(page_source_mock.call_count, 10)
        messages = mock_write.call_args.args[0]
        self.assertEqual(len(messages), 2)
        self.assertEqual({m.message_id for m in messages}, {"msg-a", "msg-b"})
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("min_date may predate chat history", printed)

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_is_done_takes_precedence_over_stall(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        ts_may = int(
            datetime(2024, 5, 1, 12, 0, tzinfo=moscow_timezone).timestamp() * 1000
        )
        ts_jul = int(
            datetime(2024, 7, 1, 12, 0, tzinfo=moscow_timezone).timestamp() * 1000
        )
        ts_dec = int(
            datetime(2024, 12, 1, 12, 0, tzinfo=moscow_timezone).timestamp() * 1000
        )
        # DOM order is viewport order (top = oldest visible), not chronological.
        msg_jul = message_row("msg-jul", ts_jul, content="jul")
        msg_may = message_row("msg-may", ts_may, content="may")
        msg_dec = message_row("msg-dec", ts_dec, content="dec")
        iter1_html = msg_jul + msg_may + msg_dec
        iter2_html = msg_may + msg_dec

        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=[iter1_html, iter2_html]
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        mock_driver.quit.assert_called_once()
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertNotIn("min_date may predate chat history", printed)
        messages = mock_write.call_args.args[0]
        ids = {m.message_id for m in messages}
        # msg-may (May) is before min_date (Jun) — excluded by per-row is_done filter.
        self.assertEqual(ids, {"msg-dec", "msg-jul"})

    @patch("builtins.print")
    @patch("pageup.runner.MAX_STALL_SCROLL_ATTEMPTS", 10)
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_logs_stall_scroll_progress(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        # Stall-branch counterpart to the empty-scroll progress test: with the
        # cap patched to 10, repeating the same snapshot must log "stall attempt
        # 10/10".  11 reads = 1 discovery + 10 stalls reaching the cap.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        single_msg = message_row("msg-a", TS_2024_09_01, content="only")
        type(mock_driver).page_source = PropertyMock(side_effect=[single_msg] * 11)
        task = ParsingTask(
            name="runtest",
            group_url=GROUP_URL,
            min_date=datetime(2020, 1, 1, tzinfo=moscow_timezone),
        )

        with patch.object(ParsingTask, "write_json"):
            run(
                task,
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("No new messages", printed)
        self.assertIn("stall attempt 10/10", printed)

    @patch("builtins.print")
    @patch("pageup.runner.MAX_STALL_SCROLL_ATTEMPTS", 10)
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_stall_does_not_inflate_collected_count(
        self, mock_create, mock_sleep, mock_actions_cls, mock_print
    ) -> None:
        # Repeated identical viewport during stall must not grow collected so far.
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        single_msg = message_row("msg-a", TS_2024_09_01, content="only")
        type(mock_driver).page_source = PropertyMock(side_effect=[single_msg] * 11)
        task = ParsingTask(
            name="runtest",
            group_url=GROUP_URL,
            min_date=datetime(2020, 1, 1, tzinfo=moscow_timezone),
        )

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                task,
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        messages = mock_write.call_args.args[0]
        self.assertEqual(len(messages), 1)
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        counts = [
            int(m.group(1))
            for m in re.finditer(r"collected so far=(\d+)", printed)
        ]
        self.assertTrue(counts)
        self.assertEqual(len(set(counts)), 1)
        self.assertEqual(counts[0], 1)
        self.assertIn("Messages collected: 1", printed)

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_run_is_done_with_no_in_range_rows(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # All visible rows predate min_date — nothing in-range to extend, prior batches empty.
        old_html = message_row("msg-old", TS_2024_01_15, content="too old")
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(side_effect=[old_html])

        with patch.object(ParsingTask, "write_json") as mock_write:
            run(
                self._task(),
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/runtest-out",
            )

        mock_write.assert_called_once()
        messages = mock_write.call_args.args[0]
        self.assertEqual(messages, [])


class PrepareBeforeScrollTests(unittest.TestCase):
    """Runner invokes prepare_main_feed_scroll before main PAGE_UP (not patched in RunTests)."""

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    @patch(
        "pageup.runner.enrich_fresh_threads",
        side_effect=lambda driver, task, fresh, thread_collected_ids, thread_open_attempts=None, write_dir=None, on_message_enriched=None: fresh,
    )
    @patch("pageup.runner.prepare_main_feed_scroll")
    def test_prepare_called_before_scroll(
        self,
        mock_prepare,
        _mock_enrich,
        mock_create,
        mock_sleep,
        mock_actions_cls,
        _mock_print,
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()

        new_only = message_row("msg-new", TS_2024_09_01, content="new")
        old_html = message_row("msg-old", TS_2024_01_15, content="too old")
        type(mock_driver).page_source = PropertyMock(
            side_effect=[new_only, old_html]
        )

        task = ParsingTask(
            name="preparetest",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch.object(ParsingTask, "write_json"):
            run(
                task,
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/preparetest-out",
            )

        mock_prepare.assert_called()
        mock_prepare.assert_called_with(mock_driver)

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    @patch(
        "pageup.runner.enrich_fresh_threads",
        side_effect=lambda driver, task, fresh, thread_collected_ids, thread_open_attempts=None, write_dir=None, on_message_enriched=None: fresh,
    )
    @patch("pageup.runner.prepare_main_feed_scroll")
    def test_prepare_called_on_empty_dom_scroll(
        self,
        mock_prepare,
        _mock_enrich,
        mock_create,
        mock_sleep,
        mock_actions_cls,
        _mock_print,
    ) -> None:
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()

        empty_html = "<html><body></body></html>"
        old_html = message_row("msg-old", TS_2024_01_15, content="too old")
        type(mock_driver).page_source = PropertyMock(
            side_effect=[empty_html, old_html]
        )

        task = ParsingTask(
            name="prepareempty",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch.object(ParsingTask, "write_json"):
            run(
                task,
                trusted_device=False,
                sleep_time=1,
                write_dir="/tmp/prepareempty-out",
            )

        mock_prepare.assert_called()
        mock_prepare.assert_called_with(mock_driver)


class ThreadRetryExtendTests(unittest.TestCase):
    """Runner re-opens thread bubbles for already-collected messages when needed."""

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    @patch("pageup.runner.prepare_main_feed_scroll")
    def test_retries_thread_for_already_collected_message(
        self,
        mock_prepare,
        mock_create,
        mock_sleep,
        mock_actions_cls,
        _mock_print,
    ) -> None:
        threaded_html = message_row(
            "parent-1",
            TS_2024_09_01,
            content="Question",
            thread_html=(
                '<div class="MessageThreadPanel-MessageThreadPanelWrapper__cls1">'
                '<span class="MessageThreadPanel-MessageThreadPanelTitle__cls1">'
                "1 ответ</span></div>"
            ),
        )
        old_html = message_row("msg-old", TS_2024_01_15, content="too old")
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=[threaded_html, threaded_html, old_html]
        )

        def enrich(
            driver,
            task,
            fresh,
            thread_collected_ids,
            thread_open_attempts=None,
            write_dir=None,
            on_message_enriched=None,
        ):
            from pageup.models import Entry

            attempts = thread_open_attempts if thread_open_attempts is not None else {}
            if attempts.get("parent-1", 0) >= 1:
                return [
                    fresh[0].model_copy(
                        update={
                            "thread_replies": [
                                Entry(
                                    message_id="reply-1",
                                    date=datetime(
                                        2024, 9, 1, 12, 0, tzinfo=moscow_timezone
                                    ),
                                    sender_url=None,
                                    sender_name="Bob",
                                    attachments=None,
                                    content="Answer",
                                )
                            ]
                        }
                    )
                ]
            if thread_open_attempts is not None:
                thread_open_attempts["parent-1"] = attempts.get("parent-1", 0) + 1
            return fresh

        task = ParsingTask(
            name="retrytest",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch("pageup.runner.enrich_fresh_threads", side_effect=enrich):
            with patch.object(ParsingTask, "write_json") as mock_write:
                run(
                    task,
                    trusted_device=False,
                    sleep_time=1,
                    write_dir="/tmp/retrytest-out",
                )

        messages = mock_write.call_args.args[0]
        self.assertEqual(len(messages), 1)
        self.assertIsNotNone(messages[0].thread_replies)


class EnrichmentFailureTests(unittest.TestCase):
    """Regression: a failure during thread enrichment or image download must not
    lose already-parsed messages/results from the same batch (see
    extend_fresh_with_threads incremental _merge and the on_message_enriched /
    on_message_downloaded callbacks in runner.py/threads.py).
    """

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_connection_error_before_any_candidate_still_writes_batch(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # A dropped browser connection (or Ctrl+C) while opening the first
        # thread panel used to wipe out the *entire* batch, including
        # messages with no thread bubble that needed no enrichment at all.
        plain_html = message_row("plain-1", TS_2024_09_01, content="plain text")
        threaded_html = message_row(
            "threaded-1",
            TS_2024_09_01,
            content="has a thread",
            thread_html=thread_bubble(2),
        )
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=[plain_html + threaded_html, "<html></html>"]
        )

        def enrich_raises(
            driver,
            task,
            fresh,
            thread_collected_ids,
            thread_open_attempts=None,
            write_dir=None,
            on_message_enriched=None,
        ):
            raise ConnectionResetError(104, "Connection reset by peer")

        task = ParsingTask(
            name="enrichfail",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch("pageup.runner.enrich_fresh_threads", side_effect=enrich_raises):
            with patch.object(ParsingTask, "write_json") as mock_write:
                with self.assertRaises(ConnectionResetError):
                    run(
                        task,
                        trusted_device=False,
                        sleep_time=1,
                        write_dir="/tmp/enrichfail-out",
                    )

        mock_write.assert_called_once()
        messages = mock_write.call_args.args[0]
        message_ids = {m.message_id for m in messages}
        self.assertIn("plain-1", message_ids)
        self.assertIn("threaded-1", message_ids)

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_connection_error_on_second_candidate_preserves_first_replies(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # If a batch has two thread bubbles and the connection drops while
        # opening the second one, the first candidate's already-collected
        # replies must survive via the on_message_enriched callback.
        from pageup.models import Entry

        first_html = message_row(
            "parent-1",
            TS_2024_09_01,
            content="First question",
            thread_html=thread_bubble(1),
        )
        second_html = message_row(
            "parent-2",
            TS_2024_09_01,
            content="Second question",
            thread_html=thread_bubble(1),
        )
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=[first_html + second_html, "<html></html>"]
        )

        def enrich_first_then_raises(
            driver,
            task,
            fresh,
            thread_collected_ids,
            thread_open_attempts=None,
            write_dir=None,
            on_message_enriched=None,
        ):
            first = next(m for m in fresh if m.message_id == "parent-1")
            enriched_first = first.model_copy(
                update={
                    "thread_replies": [
                        Entry(
                            message_id="reply-1",
                            date=datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone),
                            sender_url=None,
                            sender_name="Bob",
                            attachments=None,
                            content="Answer",
                        )
                    ]
                }
            )
            if on_message_enriched is not None:
                on_message_enriched(enriched_first)
            raise ConnectionResetError(104, "Connection reset by peer")

        task = ParsingTask(
            name="threadfail",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch(
            "pageup.runner.enrich_fresh_threads", side_effect=enrich_first_then_raises
        ):
            with patch.object(ParsingTask, "write_json") as mock_write:
                with self.assertRaises(ConnectionResetError):
                    run(
                        task,
                        trusted_device=False,
                        sleep_time=1,
                        write_dir="/tmp/threadfail-out",
                    )

        mock_write.assert_called_once()
        messages = mock_write.call_args.args[0]
        by_id = {m.message_id: m for m in messages}
        self.assertIn("parent-1", by_id)
        self.assertIn("parent-2", by_id)
        self.assertIsNotNone(by_id["parent-1"].thread_replies)
        self.assertEqual(len(by_id["parent-1"].thread_replies), 1)
        self.assertEqual(by_id["parent-1"].thread_replies[0].message_id, "reply-1")

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_connection_error_on_second_image_download_preserves_first(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # Same class of bug as the thread-reply case above, but in
        # download_fresh_images: if downloading the second message's image
        # fails, the first message's already-downloaded attachment must
        # survive via the on_message_downloaded callback.
        first_html = message_row("img-1", TS_2024_09_01, content="first")
        second_html = message_row("img-2", TS_2024_09_01, content="second")
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        type(mock_driver).page_source = PropertyMock(
            side_effect=[first_html + second_html, "<html></html>"]
        )

        def enrich_passthrough(
            driver,
            task,
            fresh,
            thread_collected_ids,
            thread_open_attempts=None,
            write_dir=None,
            on_message_enriched=None,
        ):
            return fresh

        def download_first_then_raises(
            driver, write_dir, messages, on_message_downloaded=None
        ):
            first = next(m for m in messages if m.message_id == "img-1")
            downloaded_first = first.model_copy(
                update={"attachments": ["img-1_0.jpg"]}
            )
            if on_message_downloaded is not None:
                on_message_downloaded(downloaded_first)
            raise ConnectionResetError(104, "Connection reset by peer")

        task = ParsingTask(
            name="imagefail",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch("pageup.runner.enrich_fresh_threads", side_effect=enrich_passthrough):
            with patch(
                "pageup.runner.download_fresh_images",
                side_effect=download_first_then_raises,
            ):
                with patch.object(ParsingTask, "write_json") as mock_write:
                    with self.assertRaises(ConnectionResetError):
                        run(
                            task,
                            trusted_device=False,
                            sleep_time=1,
                            write_dir="/tmp/imagefail-out",
                        )

        mock_write.assert_called_once()
        messages = mock_write.call_args.args[0]
        by_id = {m.message_id: m for m in messages}
        self.assertIn("img-1", by_id)
        self.assertIn("img-2", by_id)
        self.assertEqual(by_id["img-1"].attachments, ["img-1_0.jpg"])

    @patch("builtins.print")
    @patch("pageup.runner.ActionChains")
    @patch("pageup.runner.time.sleep")
    @patch("pageup.runner.create_driver")
    def test_thread_pending_requeue_does_not_clobber_downloaded_attachment(
        self, mock_create, mock_sleep, mock_actions_cls, _mock_print
    ) -> None:
        # Regression: a message with both an image and a thread needing a
        # retry across scroll batches must not have its already-downloaded
        # attachment reset to pending when the second batch's fresh DOM
        # re-parse is merged (extend_fresh_with_threads pass 1), even though
        # that second batch's own enrichment then fails outright.
        from pageup.models import Entry

        html = message_row(
            "multi-1",
            TS_2024_09_01,
            content="has image and thread",
            attachments_html=image_attachment_block(),
            thread_html=thread_bubble(2),
        )
        mock_driver = MagicMock()
        mock_create.return_value = mock_driver
        mock_actions_cls.return_value = MagicMock()
        # Same message reappears verbatim on the second scroll batch — still
        # visible, thread not yet fully collected (1 of 2 replies so far).
        type(mock_driver).page_source = PropertyMock(
            side_effect=[html, html, "<html></html>"]
        )

        call_count = {"enrich": 0}

        def enrich_partial_then_raises(
            driver,
            task,
            fresh,
            thread_collected_ids,
            thread_open_attempts=None,
            write_dir=None,
            on_message_enriched=None,
        ):
            call_count["enrich"] += 1
            if call_count["enrich"] == 1:
                message = fresh[0]
                enriched = message.model_copy(
                    update={
                        "thread_replies": [
                            Entry(
                                message_id="reply-1",
                                date=datetime(
                                    2024, 9, 1, 12, 0, tzinfo=moscow_timezone
                                ),
                                sender_url=None,
                                sender_name="Bob",
                                attachments=None,
                                content="Answer",
                            )
                        ]
                    }
                )
                if on_message_enriched is not None:
                    on_message_enriched(enriched)
                return [enriched]
            # Second batch: connection drops before any further progress.
            raise ConnectionResetError(104, "Connection reset by peer")

        def download_fills_attachment(
            driver, write_dir, messages, on_message_downloaded=None
        ):
            updated = []
            for message in messages:
                downloaded = message.model_copy(
                    update={"attachments": ["multi-1_0.jpg"]}
                )
                if on_message_downloaded is not None:
                    on_message_downloaded(downloaded)
                updated.append(downloaded)
            return updated

        task = ParsingTask(
            name="requeuefail",
            group_url=GROUP_URL,
            min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

        with patch(
            "pageup.runner.enrich_fresh_threads",
            side_effect=enrich_partial_then_raises,
        ):
            with patch(
                "pageup.runner.download_fresh_images",
                side_effect=download_fills_attachment,
            ):
                with patch.object(ParsingTask, "write_json") as mock_write:
                    with self.assertRaises(ConnectionResetError):
                        run(
                            task,
                            trusted_device=False,
                            sleep_time=1,
                            write_dir="/tmp/requeuefail-out",
                        )

        mock_write.assert_called_once()
        messages = mock_write.call_args.args[0]
        by_id = {m.message_id: m for m in messages}
        self.assertIn("multi-1", by_id)
        self.assertEqual(by_id["multi-1"].attachments, ["multi-1_0.jpg"])
        # The batch-1 thread reply must also survive the batch-2 re-queue —
        # pass 1's stale re-parse (thread_replies=None) must not clobber it.
        self.assertIsNotNone(by_id["multi-1"].thread_replies)
        self.assertEqual(len(by_id["multi-1"].thread_replies), 1)
        self.assertEqual(by_id["multi-1"].thread_replies[0].message_id, "reply-1")


if __name__ == "__main__":
    unittest.main()
