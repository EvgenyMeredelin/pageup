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
"""

import re
import unittest
from datetime import datetime
from unittest.mock import MagicMock, PropertyMock, patch

from selenium.common.exceptions import TimeoutException

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
    message_row,
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


class RunTests(unittest.TestCase):
    """Scroll loop behaviour with mocked driver and shortened sleep_time."""

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


if __name__ == "__main__":
    unittest.main()
