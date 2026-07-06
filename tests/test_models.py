"""Unit tests for pageup.models.

Covers:
    Message       — timestamp parsing, equality/hash, __bool__, patch(), serialisation
    Entry         — field_serializer strips None slots from attachments
    ParsingTask   — validation (incl. null-byte and dotted names), collect_messages()
                    (incl. malformed data-message-date skip, thread_reply_count,
                    main/thread scope isolation, image attachment detection,
                    file-block produces no attachment record),
                    collect_thread_reply_entries() (incl. quotes forwarding,
                    message_id included in Entry),
                    is_done(), write_json (incl. thread_replies),
                    message_id normalization (| → _)
    Quote         — minimal Pydantic model smoke test

Uses HTML fixtures from tests.fixtures and tests/data/; no Selenium.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from pydantic import ValidationError

from pageup.config import MSG_LIST_CONTAINER_CLS, MSG_WRAP_CLS, THREAD_PANEL_CLS
from pageup.models import Entry, Message, ParsingTask, Quote
from pageup.tools import moscow_timezone
from tests.fixtures import (
    ABSOLUTE_SENDER_URL_HTML,
    ATTACHMENT_ONLY_HTML,
    BLOCK_MESSAGE_TEXT_HTML,
    CONTINUATION_PAIR_HTML,
    GROUP_URL,
    IMAGE_ATTACHMENT_HTML,
    IMAGE_WITH_TEXT_HTML,
    VIDEO_ATTACHMENT_HTML,
    INCOMPLETE_MESSAGE_HTML,
    MAIN_AND_THREAD_PANEL_HTML,
    MESSAGE_WITH_THREAD_BUBBLE_HTML,
    MESSY_TEXT_HTML,
    QUOTE_FALLBACK_HTML,
    THREAD_PANEL_SAMPLE_HTML,
    image_attachment_block,
    multi_attachment_block,
    quote_block,
    video_attachment_block,
    SAMPLE_MESSAGE_HTML,
    thread_bubble,
    TS_2024_01_15,
    TS_2024_09_01,
    TWO_MESSAGES_HTML,
    message_row,
)


class MessageTests(unittest.TestCase):
    """Message model behaviour independent of HTML parsing."""

    # Fixed epoch for Moscow noon — immune to host local timezone (see to_datetime).
    MOSCOW_NOON_2024_09_01_MS = str(1725181200 * 1000)

    def test_date_from_millisecond_string(self) -> None:
        # SberChat stores data-message-date as ms string; to_datetime divides by 1000.
        msg = Message(
            message_id="a",
            date=str(TS_2024_09_01),
            sender_url=None,
            sender_name=None,
            quotes=None,
            attachments=None,
            content="x",
        )
        self.assertEqual(msg.date.tzinfo, moscow_timezone)
        self.assertEqual(msg.date.year, 2024)
        self.assertEqual(msg.date.month, 9)

    def test_date_from_millisecond_string_ignores_local_tz(self) -> None:
        # Fixed Moscow instant: 2024-09-01 12:00 MSK — must parse correctly
        # even when the host runs in UTC (see models.Message.to_datetime).
        msg = Message(
            message_id="a",
            date=self.MOSCOW_NOON_2024_09_01_MS,
            sender_url=None,
            sender_name=None,
            quotes=None,
            attachments=None,
            content="x",
        )
        self.assertEqual(
            msg.date,
            datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone),
        )

    def test_date_from_naive_datetime_gets_moscow_tz(self) -> None:
        # Naive datetimes from tests are normalised to Moscow for comparison.
        naive = datetime(2025, 1, 1)
        msg = Message(
            message_id="a",
            date=naive,
            sender_url=None,
            sender_name=None,
            quotes=None,
            attachments=None,
            content="x",
        )
        self.assertEqual(msg.date.tzinfo, moscow_timezone)

    def test_bool_with_content(self) -> None:
        msg = self._message(content="text")
        self.assertTrue(msg)

    def test_bool_with_attachments_only(self) -> None:
        # Image attachment rows survive _unique_reversed filtering.
        msg = self._message(
            content="",
            attachments=["downloaded_image_0.png"],
        )
        self.assertTrue(msg)

    def test_rearrange_fields_strips_empty_string_slots(self) -> None:
        # Skipped video placeholders ("") must not appear in JSON output.
        msg = self._message(
            content="hi",
            attachments=["img.png", ""],
        )
        dumped = msg.model_dump()
        self.assertEqual(dumped["attachments"], ["img.png"])

    def test_bool_with_thread_reply_count_only(self) -> None:
        msg = self._message(content="").model_copy(update={"thread_reply_count": 3})
        self.assertTrue(msg)

    def test_bool_with_thread_replies_only(self) -> None:
        base = self._message(content="")
        reply = Entry(
            message_id="reply-1",
            date=base.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="reply",
        )
        msg = base.model_copy(update={"thread_replies": [reply]})
        self.assertTrue(msg)

    def test_bool_false_when_empty(self) -> None:
        # Empty content, no attachments, no thread bubble → falsy.
        msg = self._message(content="")
        self.assertFalse(msg)

    def test_bool_false_when_only_skipped_attachment_slots(self) -> None:
        # Skipped video placeholders ("") must not keep an otherwise empty row.
        msg = self._message(content="", attachments=[""])
        self.assertFalse(msg)

    def test_bool_true_when_pending_attachment_slots(self) -> None:
        msg = self._message(content="", attachments=[None])
        self.assertTrue(msg)

    def test_equality_and_hash_by_message_id(self) -> None:
        # dict.fromkeys deduplication in _unique_reversed relies on this.
        a = self._message(message_id="same")
        b = self._message(message_id="same", content="other")
        c = self._message(message_id="other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len({a, b}), 1)

    def test_equality_requires_message_type(self) -> None:
        msg = self._message()
        self.assertFalse(msg == "same")

    def test_model_dump_includes_message_id(self) -> None:
        dumped = self._message().model_dump()
        self.assertEqual(dumped["message_id"], "id")
        self.assertIn("content", dumped)
        self.assertIn("attachments", dumped)

    def test_patch_copies_sender_from_previous(self) -> None:
        # Simulates continuation message after chronological ordering + dump.
        previous = {
            "sender_url": "https://sberchat.sberbank.ru/#/chat/private1",
            "sender_name": "Alice",
        }
        current = {"sender_url": None, "sender_name": None}
        Message.patch(current, previous)
        self.assertEqual(current["sender_name"], "Alice")
        self.assertEqual(current["sender_url"], previous["sender_url"])

    def test_patch_does_not_overwrite_existing_sender(self) -> None:
        # Partial sender on current row must not be overwritten.
        previous = {"sender_url": "u1", "sender_name": "Alice"}
        current = {"sender_url": "u2", "sender_name": None}
        Message.patch(current, previous)
        self.assertEqual(current["sender_url"], "u2")

    def _message(
        self,
        message_id: str = "id",
        content: str = "hi",
        attachments: list[str | None] | None = None,
    ) -> Message:
        return Message(
            message_id=message_id,
            date=datetime(2025, 1, 1, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name=None,
            quotes=None,
            attachments=attachments,
            content=content,
        )


class ParsingTaskTests(unittest.TestCase):
    """ParsingTask validation, DOM parsing, and JSON write pipeline."""

    def _task(self, min_date: datetime | None = None) -> ParsingTask:
        return ParsingTask(
            name="test",
            group_url=GROUP_URL,
            min_date=min_date or datetime(2024, 6, 1, tzinfo=moscow_timezone),
        )

    def test_rejects_invalid_group_url(self) -> None:
        # Must match tools.group_url_pattern (group chats only).
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="x",
                group_url="https://example.com/not-sberchat",
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_name_with_path_separators(self) -> None:
        # Prevents path traversal in write_dir/name.json output path.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="../evil",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_name_with_forward_slash(self) -> None:
        # `/` alone is enough to reject — not only `..` traversal patterns.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="evil/name",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_name_with_backslash(self) -> None:
        # Same rule as forward slash — plain filename only.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="evil\\name",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_empty_name(self) -> None:
        # Whitespace-only names fail after strip() in ParsingTask.validate_name.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="   ",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_dot_name(self) -> None:
        # "." is a reserved directory entry — must not be used as output filename.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name=".",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_dot_dot_name(self) -> None:
        # ".." references parent directory — blocked even without slashes in --name.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="..",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_name_with_null_byte(self) -> None:
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="evil\x00name",
                group_url=GROUP_URL,
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_accepts_dotted_name(self) -> None:
        # Only exact "." and ".." are forbidden — other dots are allowed.
        task = ParsingTask(
            name="backup.v1",
            group_url=GROUP_URL,
            min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
        )
        self.assertEqual(task.name, "backup.v1")

    def test_rejects_group_url_with_trailing_slash(self) -> None:
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="x",
                group_url=f"{GROUP_URL}/",
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    def test_rejects_group_url_with_query_string(self) -> None:
        # Browser copy-paste often includes ?query — fullmatch rejects those URLs.
        with self.assertRaises(ValidationError):
            ParsingTask(
                name="x",
                group_url=f"{GROUP_URL}?tab=messages",
                min_date=datetime(2024, 1, 1, tzinfo=moscow_timezone),
            )

    # ── collect_messages (HTML → Message list) ───────────────────────────────
    # Uses fixtures.py DOM fragments; exercises config selectors via real parser.

    def test_collect_messages_parses_text_quotes_and_attachments(self) -> None:
        soup = BeautifulSoup(SAMPLE_MESSAGE_HTML, "lxml")
        messages = self._task().collect_messages(soup)

        self.assertEqual(len(messages), 1)
        msg = messages[0]
        self.assertEqual(msg.content, "Hello team")
        self.assertEqual(msg.sender_name, "Alice")
        self.assertEqual(
            msg.sender_url, "https://sberchat.sberbank.ru/#/chat/private123"
        )
        self.assertEqual(len(msg.quotes or []), 1)
        self.assertEqual(msg.quotes[0].content, "Earlier text")
        # SAMPLE_MESSAGE_HTML uses image_attachment_block → one None slot.
        self.assertEqual(msg.attachments, [None])

    def test_collect_messages_skips_rows_without_data_attributes(self) -> None:
        soup = BeautifulSoup(INCOMPLETE_MESSAGE_HTML, "lxml")
        messages = self._task().collect_messages(soup)
        self.assertEqual(messages, [])

    def test_collect_messages_skips_malformed_date(self) -> None:
        # Non-numeric data-message-date must not abort the whole snapshot.
        html = message_row("good", TS_2024_09_01, content="ok") + f"""
<div class="{MSG_WRAP_CLS}"
     data-message-id="bad-date"
     data-message-date="not-a-ms">
  <span>bad row</span>
</div>"""
        soup = BeautifulSoup(html, "lxml")
        messages = self._task().collect_messages(soup)
        self.assertEqual([m.message_id for m in messages], ["good"])

    def test_collect_messages_returns_newest_first(self) -> None:
        # Runner checks new_messages[-1] for is_done — order matters.
        soup = BeautifulSoup(TWO_MESSAGES_HTML, "lxml")
        messages = self._task().collect_messages(soup)
        self.assertEqual([m.message_id for m in messages], ["msg-new", "msg-old"])

    def test_collect_messages_file_attachment_only_no_record(self) -> None:
        # File blocks are skipped by _collect_attachments — only image blocks count.
        soup = BeautifulSoup(ATTACHMENT_ONLY_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(msg.content, "")
        self.assertIsNone(msg.attachments)

    def test_collect_messages_multi_file_attachment_block_no_record(self) -> None:
        # Multi-file blocks contain no image wrap → _collect_attachments returns None.
        html = message_row(
            "msg-multi-file",
            TS_2024_09_01,
            content="",
            attachments_html=multi_attachment_block(
                ("settings.txt", "8.4 КБ"),
                ("settings.json", "830 Б"),
            ),
        )
        msg = self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]
        self.assertIsNone(msg.attachments)

    def test_collect_messages_file_blocks_from_real_dom_excerpt_no_record(self) -> None:
        # File attachment blocks in real DOM produce no attachment record (images only).
        from pathlib import Path

        excerpt = (
            Path(__file__).resolve().parents[2]
            / "obsidian-sber/various/sberchat-example.html"
        )
        if not excerpt.is_file():
            self.skipTest(f"DOM excerpt not found: {excerpt}")
        html = excerpt.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        task = self._task()
        for wrap in soup.find_all("div", class_=MSG_WRAP_CLS):
            if wrap.get("data-message-id", "").startswith("-2511463158072143375"):
                messages = task._collect_row_messages(
                    BeautifulSoup(str(wrap), "lxml"), scope="main"
                )
                self.assertEqual(len(messages), 1)
                # File blocks only → no image slots recorded.
                self.assertIsNone(messages[0].attachments)
                return
        self.fail("Baranyuk Jenkins message row not found in DOM excerpt")

    def test_collect_messages_quote_fallback(self) -> None:
        # QUOTE_CONTENT_SEL missing → full wrapper text + cleaner.
        soup = BeautifulSoup(QUOTE_FALLBACK_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(msg.quotes[0].content, "Quoted via fallback")

    def test_collect_messages_strips_quote_sender_prefix(self) -> None:
        # Live SberChat duplicates quote author name inside QUOTE_CONTENT_SEL text.
        url = "https://gigacode.sberbank.ru/#/instruction/cli?os=windows"
        soup = BeautifulSoup(
            message_row(
                "msg-quote-strip",
                TS_2024_09_01,
                quotes_html=quote_block(
                    "Андрей Аникин",
                    f"лендинг - {url}",
                ),
            ),
            "lxml",
        )
        quote = self._task().collect_messages(soup)[0].quotes[0]
        self.assertEqual(quote.sender_name, "Андрей Аникин")
        self.assertEqual(quote.content, f"лендинг - {url}")

    def test_collect_messages_cleans_text(self) -> None:
        soup = BeautifulSoup(MESSY_TEXT_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(msg.content, "Hello world")

    def test_collect_messages_absolute_sender_url(self) -> None:
        soup = BeautifulSoup(ABSOLUTE_SENDER_URL_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(
            msg.sender_url,
            "https://sberchat.sberbank.ru/#/chat/private999",
        )

    def test_is_done_when_message_before_min_date(self) -> None:
        task = self._task(min_date=datetime(2024, 6, 1, tzinfo=moscow_timezone))
        old = Message(
            message_id="old",
            date=datetime(2024, 1, 15, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name=None,
            quotes=None,
            attachments=None,
            content="x",
        )
        self.assertTrue(task.is_done(old))

    def test_is_done_false_on_min_date_boundary(self) -> None:
        # Messages on min_date itself are still collected (strict < comparison).
        task = self._task(min_date=datetime(2024, 9, 1, tzinfo=moscow_timezone))
        on_day = Message(
            message_id="d",
            date=datetime(2024, 9, 1, 15, 0, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name=None,
            quotes=None,
            attachments=None,
            content="x",
        )
        self.assertFalse(task.is_done(on_day))

    # ── write_json postprocessor (_unique_reversed → _dump → _patch) ─────────

    def test_unique_reversed_deduplicates_and_orders(self) -> None:
        # Overlapping scroll batches produce duplicate message_ids.
        m1 = self._make_message("a", "first", TS_2024_01_15)
        m2 = self._make_message("b", "second", TS_2024_09_01)
        dup = self._make_message("a", "first-dup", TS_2024_01_15)
        result = ParsingTask._unique_reversed([m2, m1, dup])
        self.assertEqual([m.message_id for m in result], ["a", "b"])

    def test_unique_reversed_sorts_chronologically(self) -> None:
        older = self._make_message("a", "first", TS_2024_01_15)
        newer = self._make_message("b", "second", TS_2024_09_01)
        result = ParsingTask._unique_reversed([older, newer])
        self.assertEqual([m.message_id for m in result], ["a", "b"])
        self.assertLess(result[0].date, result[1].date)

    def test_prefer_richer_keeps_existing_replies_on_equal_count(self) -> None:
        base = self._make_message("t1", "parent", TS_2024_09_01)
        disk_reply = Entry(
            message_id="reply-1",
            date=base.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="from disk",
        )
        dom_reply = Entry(
            message_id="reply-1",
            date=base.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="from dom",
        )
        existing = base.model_copy(
            update={"thread_reply_count": 1, "thread_replies": [disk_reply]}
        )
        incoming = base.model_copy(
            update={"thread_reply_count": 1, "thread_replies": [dom_reply]}
        )
        merged = ParsingTask.prefer_richer_message(existing, incoming)
        self.assertEqual(merged.thread_replies[0].content, "from disk")

    def test_unique_reversed_filters_falsy_messages(self) -> None:
        empty = self._make_message("empty", "", TS_2024_09_01)
        good = self._make_message("good", "text", TS_2024_09_01)
        result = ParsingTask._unique_reversed([good, empty])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].message_id, "good")

    def test_unique_reversed_keeps_thread_only_message(self) -> None:
        html = message_row(
            "thread-only",
            TS_2024_09_01,
            content="",
            thread_html=thread_bubble(2),
        )
        threaded = self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]
        self.assertEqual(len(ParsingTask._unique_reversed([threaded])), 1)

    def test_patch_backfills_continuation_in_chronological_list(self) -> None:
        soup = BeautifulSoup(CONTINUATION_PAIR_HTML, "lxml")
        task = self._task()
        raw = task.collect_messages(soup)
        dumped = ParsingTask._dump_messages(ParsingTask._unique_reversed(raw))
        patched = ParsingTask._patch(dumped)
        # Chronological: first then continuation.
        self.assertEqual(patched[0]["sender_name"], "Alice")
        self.assertEqual(patched[1]["sender_name"], "Alice")
        self.assertEqual(
            patched[1]["sender_url"],
            "https://sberchat.sberbank.ru/#/chat/private999",
        )

    def test_write_json_creates_valid_output(self) -> None:
        # End-to-end: parse HTML → write_json → read JSON from disk.
        soup = BeautifulSoup(CONTINUATION_PAIR_HTML, "lxml")
        task = self._task()
        messages = task.collect_messages(soup)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("builtins.print"):
                task.write_json(messages, tmp)
            path = Path(tmp) / "test.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(data[1]["sender_name"], "Alice")
            self.assertIn("message_id", data[0])

    def test_collect_messages_parses_thread_reply_count(self) -> None:
        soup = BeautifulSoup(MESSAGE_WITH_THREAD_BUBBLE_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(msg.thread_reply_count, 3)
        self.assertIsNone(msg.thread_replies)

    def test_parse_thread_reply_count_variants(self) -> None:
        for count in (1, 3, 8):
            html = message_row(
                f"msg-{count}",
                TS_2024_09_01,
                thread_html=thread_bubble(count),
            )
            row = BeautifulSoup(html, "lxml").find("div", class_=MSG_WRAP_CLS)
            self.assertEqual(ParsingTask._parse_thread_reply_count(row), count)

    def test_main_scope_ignores_thread_panel_rows(self) -> None:
        soup = BeautifulSoup(MAIN_AND_THREAD_PANEL_HTML, "lxml")
        messages = self._task().collect_messages(soup)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "msg-main")

    def test_main_scope_ignores_panel_only_dom(self) -> None:
        soup = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")
        messages = self._task().collect_messages(soup)
        self.assertEqual(messages, [])

    def test_collect_messages_rejects_unknown_scope(self) -> None:
        soup = BeautifulSoup(SAMPLE_MESSAGE_HTML, "lxml")
        with self.assertRaises(ValueError):
            self._task().collect_messages(soup, scope="sidebar")

    def test_collect_thread_reply_entries_excludes_root(self) -> None:
        soup = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")
        task = self._task()
        entries = task.collect_thread_reply_entries(soup, "msg-root")
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.message_id for e in entries}, {"reply-1", "reply-2"})
        self.assertEqual(entries[0].content, "First reply")

    def test_thread_scope_ignores_main_feed_when_panel_closed(self) -> None:
        soup = BeautifulSoup(TWO_MESSAGES_HTML, "lxml")
        entries = self._task().collect_thread_reply_entries(soup, "msg-old")
        self.assertEqual(entries, [])

    def test_real_html_thread_panel_parsing(self) -> None:
        fixture = Path(__file__).resolve().parent / "data" / "sberchat-thread-panel-excerpt.html"
        if not fixture.is_file():
            self.skipTest(f"DOM excerpt fixture missing: {fixture}")
        html = fixture.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "lxml")
        task = self._task()
        entries = task.collect_thread_reply_entries(
            soup,
            "-6968472423308848655_-1719553178633880992",
        )
        self.assertEqual(len(entries), 3)
        self.assertIn("export PATH", entries[0].content)

    def test_block_message_div_content(self) -> None:
        soup = BeautifulSoup(BLOCK_MESSAGE_TEXT_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(msg.content, "Block body text")

    def test_write_json_includes_thread_fields(self) -> None:
        base = self._make_message("t1", "parent", TS_2024_09_01)
        reply = Entry(
            message_id="reply-1",
            date=base.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="reply text",
        )
        msg = base.model_copy(
            update={"thread_reply_count": 2, "thread_replies": [reply]}
        )
        task = self._task()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("builtins.print"):
                task.write_json([msg], tmp)
            data = json.loads((Path(tmp) / "test.json").read_text(encoding="utf-8"))
            self.assertEqual(data[0]["thread_reply_count"], 2)
            self.assertEqual(len(data[0]["thread_replies"]), 1)
            self.assertEqual(data[0]["thread_replies"][0]["sender_name"], "Bob")
            self.assertIn("message_id", data[0]["thread_replies"][0])

    def test_prefer_richer_message_keeps_longer_thread(self) -> None:
        base = self._make_message("t1", "parent", TS_2024_09_01)
        short = base.model_copy(update={"thread_replies": None})
        reply = Entry(
            message_id="reply-1",
            date=base.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="reply",
        )
        rich = base.model_copy(
            update={"thread_reply_count": 2, "thread_replies": [reply]}
        )
        self.assertIs(
            ParsingTask.prefer_richer_message(short, rich).thread_replies,
            rich.thread_replies,
        )
        self.assertEqual(
            len(ParsingTask.prefer_richer_message(rich, short).thread_replies or []),
            1,
        )

    def test_prefer_richer_message_preserves_downloaded_attachment(self) -> None:
        # Regression: a message already fully downloaded can resurface in a
        # later scroll batch's fresh DOM re-parse (thread-pending retry) with
        # attachments reset to pending (None slots); the stale re-parse must
        # not clobber the already-downloaded filename.
        base = self._make_message("t1", "parent", TS_2024_09_01)
        downloaded = base.model_copy(update={"attachments": ["t1_0.jpg"]})
        fresh_reparse = base.model_copy(update={"attachments": [None]})
        merged = ParsingTask.prefer_richer_message(downloaded, fresh_reparse)
        self.assertEqual(merged.attachments, ["t1_0.jpg"])

    def test_prefer_richer_message_takes_newly_downloaded_attachment(self) -> None:
        # Normal direction: incoming has a freshly downloaded slot that the
        # existing (pre-download) message doesn't have yet.
        base = self._make_message("t1", "parent", TS_2024_09_01)
        pending = base.model_copy(update={"attachments": [None]})
        downloaded = base.model_copy(update={"attachments": ["t1_0.jpg"]})
        merged = ParsingTask.prefer_richer_message(pending, downloaded)
        self.assertEqual(merged.attachments, ["t1_0.jpg"])

    def test_prefer_richer_message_preserves_attachments_when_incoming_has_none(
        self,
    ) -> None:
        # incoming.attachments can be the bare value None (not a [None] list)
        # when a re-parse finds no attachment blocks at all in the row — this
        # must defer to existing's data, not wipe it, the same as the
        # [None]-slot case above.
        base = self._make_message("t1", "parent", TS_2024_09_01)
        downloaded = base.model_copy(update={"attachments": ["t1_0.jpg"]})
        no_attachments_found = base.model_copy(update={"attachments": None})
        merged = ParsingTask.prefer_richer_message(downloaded, no_attachments_found)
        self.assertEqual(merged.attachments, ["t1_0.jpg"])

    def test_thread_is_complete(self) -> None:
        base = self._make_message("t1", "parent", TS_2024_09_01)
        reply = Entry(
            message_id="reply-1",
            date=base.date,
            sender_url=None,
            sender_name="Bob",
            attachments=None,
            content="reply",
        )
        partial = base.model_copy(
            update={"thread_reply_count": 2, "thread_replies": [reply]}
        )
        complete = base.model_copy(
            update={"thread_reply_count": 1, "thread_replies": [reply]}
        )
        self.assertFalse(ParsingTask.thread_is_complete(partial))
        self.assertTrue(ParsingTask.thread_is_complete(complete))

    def test_file_attachment_with_status_produces_no_record(self) -> None:
        # File blocks (even with status labels) have no image wrap → attachments is None.
        from tests.fixtures import attachment_with_status_block

        html = message_row(
            "status-1",
            TS_2024_09_01,
            content="",
            attachments_html=attachment_with_status_block(
                "report.pdf",
                status="Отклонено",
            ),
        )
        msg = self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]
        self.assertIsNone(msg.attachments)

    def test_image_attachment_detected_as_none_slot(self) -> None:
        # Image attachment block (no filename cell) → one None slot in attachments.
        msg = self._task().collect_messages(
            BeautifulSoup(IMAGE_ATTACHMENT_HTML, "lxml")
        )[0]
        self.assertEqual(msg.attachments, [None])

    def test_image_attachment_makes_message_truthy(self) -> None:
        # A message with only an image attachment (no text) must be kept
        # (Message.__bool__ must be True so it survives filter(bool, ...) in write_json).
        msg = self._task().collect_messages(
            BeautifulSoup(IMAGE_ATTACHMENT_HTML, "lxml")
        )[0]
        self.assertEqual(msg.content, "")
        self.assertTrue(bool(msg))

    def test_image_attachment_alongside_text(self) -> None:
        # Image attachment coexists with message text.
        msg = self._task().collect_messages(
            BeautifulSoup(IMAGE_WITH_TEXT_HTML, "lxml")
        )[0]
        self.assertEqual(msg.content, "See attached")
        self.assertEqual(msg.attachments, [None])

    def test_file_attachment_not_confused_with_image(self) -> None:
        # Regular file attachment block (has name/size but no image wrap) → no record.
        msg = self._task().collect_messages(
            BeautifulSoup(ATTACHMENT_ONLY_HTML, "lxml")
        )[0]
        self.assertIsNone(msg.attachments)

    def test_video_attachment_produces_no_record(self) -> None:
        msg = self._task().collect_messages(
            BeautifulSoup(VIDEO_ATTACHMENT_HTML, "lxml")
        )[0]
        self.assertIsNone(msg.attachments)

    def test_mixed_image_and_video_blocks_count_image_only(self) -> None:
        html = message_row(
            "msg-mixed",
            TS_2024_09_01,
            content="pics",
            attachments_html=image_attachment_block() + video_attachment_block(),
        )
        msg = self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]
        self.assertEqual(msg.attachments, [None])

    def test_collect_thread_reply_entries_forwards_quotes(self) -> None:
        # Thread reply with a quote → quotes field is populated on Entry.
        panel_html = f"""
<div class="{THREAD_PANEL_CLS}">
  <div class="{MSG_LIST_CONTAINER_CLS}">
{message_row("root-q", TS_2024_09_01, sender_name="Alice", content="Root")}
{message_row("reply-q", TS_2024_09_01, sender_name="Bob", content="Agreed",
             quotes_html=quote_block("Alice", "Earlier message"))}
  </div>
</div>
"""
        soup = BeautifulSoup(panel_html, "lxml")
        entries = self._task().collect_thread_reply_entries(soup, "root-q")
        self.assertEqual(len(entries), 1)
        reply = entries[0]
        self.assertEqual(reply.message_id, "reply-q")
        self.assertIsNotNone(reply.quotes)
        self.assertEqual(len(reply.quotes), 1)
        self.assertEqual(reply.quotes[0].sender_name, "Alice")

    def test_collect_thread_reply_entries_no_quotes_is_none(self) -> None:
        # Thread reply without quotes → quotes field is None (not empty list).
        soup = BeautifulSoup(THREAD_PANEL_SAMPLE_HTML, "lxml")
        entries = self._task().collect_thread_reply_entries(soup, "msg-root")
        for reply in entries:
            self.assertIsNone(reply.quotes)

    def test_message_id_pipe_normalized_to_underscore(self) -> None:
        # data-message-id with | → _ at parse time (filesystem-safe filename prefix).
        from pageup.config import MSG_WRAP_CLS

        content_cls = "BlockMessageStyleComponent-BlockMessageText__cls1"
        html = f"""
<div class="{MSG_WRAP_CLS}"
     data-message-id="1553797810759471601|-857953021546601497"
     data-message-date="{TS_2024_09_01}">
  <span class="{content_cls}">hello</span>
</div>"""
        msg = self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]
        self.assertEqual(msg.message_id, "1553797810759471601_-857953021546601497")
        self.assertNotIn("|", msg.message_id)

    def _make_message(self, message_id: str, content: str, date_ms: int) -> Message:
        # Build minimal HTML and parse through the real collect_messages path.
        html = message_row(message_id, date_ms, content=content)
        return self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]


class EntryAndQuoteModelTests(unittest.TestCase):
    """Smoke tests for Quote and Entry Pydantic models."""

    def test_quote_model(self) -> None:
        q = Quote(sender_name="Bob", content="text")
        self.assertEqual(q.sender_name, "Bob")

    def test_entry_field_serializer_strips_none_slots(self) -> None:
        # @field_serializer("attachments") filters None placeholders before JSON output.
        entry = Entry(
            message_id="e1",
            date=datetime(2025, 1, 1, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Alice",
            content="hi",
            attachments=[None, "file_0.png", None, "file_2.jpg"],
        )
        serialized = entry.model_dump()
        self.assertEqual(serialized["attachments"], ["file_0.png", "file_2.jpg"])

    def test_entry_field_serializer_strips_empty_string_slots(self) -> None:
        # Skipped video placeholders ("") must not appear in JSON output.
        entry = Entry(
            message_id="e1b",
            date=datetime(2025, 1, 1, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Alice",
            content="hi",
            attachments=["file_0.png", ""],
        )
        serialized = entry.model_dump()
        self.assertEqual(serialized["attachments"], ["file_0.png"])

    def test_entry_field_serializer_all_none_returns_none(self) -> None:
        # When all slots are None (all pending), serialized attachments is None.
        entry = Entry(
            message_id="e2",
            date=datetime(2025, 1, 1, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name="Alice",
            content="hi",
            attachments=[None, None],
        )
        serialized = entry.model_dump()
        self.assertIsNone(serialized["attachments"])

    def test_entry_includes_message_id(self) -> None:
        # message_id must appear in the serialized Entry (needed for JSON output).
        entry = Entry(
            message_id="xyz",
            date=datetime(2025, 1, 1, tzinfo=moscow_timezone),
            sender_url=None,
            sender_name=None,
            content="text",
        )
        self.assertEqual(entry.model_dump()["message_id"], "xyz")


if __name__ == "__main__":
    unittest.main()
