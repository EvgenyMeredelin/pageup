"""Unit tests for pageup.models.

Covers:
    Message       — timestamp parsing, equality/hash, __bool__, patch(), serialisation
    ParsingTask   — validation (incl. null-byte and dotted names), collect_messages()
                    (incl. malformed data-message-date skip), is_done(), write_json
    Quote/Attachment — minimal Pydantic model smoke tests

Uses HTML fixtures from tests.fixtures; no Selenium or filesystem beyond temp dirs.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from pydantic import ValidationError

from pageup.config import MSG_WRAP_CLS
from pageup.models import Attachment, Message, ParsingTask, Quote
from pageup.tools import moscow_timezone
from tests.fixtures import (
    ABSOLUTE_SENDER_URL_HTML,
    ATTACHMENT_ONLY_HTML,
    CONTINUATION_PAIR_HTML,
    GROUP_URL,
    INCOMPLETE_MESSAGE_HTML,
    MESSY_TEXT_HTML,
    QUOTE_FALLBACK_HTML,
    quote_block,
    SAMPLE_MESSAGE_HTML,
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
        # Attachment-only rows survive _unique_reversed filtering.
        msg = self._message(
            content="",
            attachments=[Attachment(name="f.pdf", size=None)],
        )
        self.assertTrue(msg)

    def test_bool_false_when_empty(self) -> None:
        # Empty content + no attachments → falsy, dropped in post-processing.
        msg = self._message(content="")
        self.assertFalse(msg)

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

    def test_model_dump_excludes_message_id(self) -> None:
        # rearrange_fields serializer omits internal dedup key from JSON output.
        dumped = self._message().model_dump()
        self.assertNotIn("message_id", dumped)
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
        attachments: list[Attachment] | None = None,
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
        self.assertEqual(len(msg.attachments or []), 1)
        self.assertEqual(msg.attachments[0].name, "report.pdf")

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

    def test_collect_messages_attachment_only(self) -> None:
        soup = BeautifulSoup(ATTACHMENT_ONLY_HTML, "lxml")
        msg = self._task().collect_messages(soup)[0]
        self.assertEqual(msg.content, "")
        self.assertEqual(msg.attachments[0].name, "slides.pptx")

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

    def test_unique_reversed_filters_falsy_messages(self) -> None:
        empty = self._make_message("empty", "", TS_2024_09_01)
        good = self._make_message("good", "text", TS_2024_09_01)
        result = ParsingTask._unique_reversed([good, empty])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].message_id, "good")

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
            self.assertNotIn("message_id", data[0])

    def _make_message(self, message_id: str, content: str, date_ms: int) -> Message:
        # Build minimal HTML and parse through the real collect_messages path.
        html = message_row(message_id, date_ms, content=content)
        return self._task().collect_messages(BeautifulSoup(html, "lxml"))[0]


class QuoteAndAttachmentModelTests(unittest.TestCase):
    """Smoke tests for nested Pydantic models used inside Message."""

    def test_quote_model(self) -> None:
        q = Quote(sender_name="Bob", content="text")
        self.assertEqual(q.sender_name, "Bob")

    def test_attachment_optional_size(self) -> None:
        a = Attachment(name="file.bin", size=None)
        self.assertIsNone(a.size)


if __name__ == "__main__":
    unittest.main()
