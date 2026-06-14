"""Unit tests for pageup.tools.

Exercises the text-cleaning pipeline, quote content normalisation, and shared
constants used by models.py and cli.py.  No browser or HTML parsing — pure
string / regex behaviour.
"""

import unittest

from pageup.tools import (
    cleaner,
    dom_message_id,
    finalize_quote_content,
    group_url_pattern,
    moscow_timezone,
    normalize_message_id,
    Pipeline,
    remove_emoji,
    remove_redundant_whitespaces,
    remove_time_prefix,
    SBERCHAT_BASE_URL,
    strip_leading_sender_name,
)


class PipelineTests(unittest.TestCase):
    """Pipeline composition used by cleaner and write_json postprocessor."""

    def test_pipeline_chains_functions(self) -> None:
        # Left-to-right: strip then lower (see tools.Pipeline.__call__).
        pipe = Pipeline(str.strip, str.lower)
        self.assertEqual(pipe("  Hello  "), "hello")

    def test_pipeline_single_function(self) -> None:
        self.assertEqual(Pipeline(str.upper)("abc"), "ABC")


class TextCleanerTests(unittest.TestCase):
    """Individual steps and the combined cleaner pipeline."""

    def test_remove_time_prefix_at_start(self) -> None:
        # Matches quote wrappers that embed HH:MM before content text.
        self.assertEqual(remove_time_prefix("17:22Hello"), "Hello")

    def test_remove_time_prefix_no_match(self) -> None:
        self.assertEqual(remove_time_prefix("no time here"), "no time here")

    def test_remove_emoji(self) -> None:
        self.assertEqual(remove_emoji("hi 👋 there"), "hi  there")

    def test_remove_redundant_whitespaces(self) -> None:
        self.assertEqual(remove_redundant_whitespaces("  a   b\n c  "), "a b c")

    def test_cleaner_pipeline(self) -> None:
        # Full order: time → emoji → whitespace (see tools.cleaner definition).
        self.assertEqual(cleaner("17:22  hi 👋  "), "hi")

    def test_strip_leading_sender_name(self) -> None:
        self.assertEqual(
            strip_leading_sender_name("Bob Earlier text", "Bob"),
            "Earlier text",
        )

    def test_strip_leading_sender_name_unicode(self) -> None:
        self.assertEqual(
            strip_leading_sender_name(
                "Андрей Аникин лендинг - https://example.com",
                "Андрей Аникин",
            ),
            "лендинг - https://example.com",
        )

    def test_strip_leading_sender_name_no_match(self) -> None:
        self.assertEqual(
            strip_leading_sender_name("спасибо, Андрей", "Андрей"),
            "спасибо, Андрей",
        )

    def test_strip_leading_sender_name_none_sender(self) -> None:
        self.assertEqual(
            strip_leading_sender_name("Someone said hi", None),
            "Someone said hi",
        )

    def test_strip_leading_sender_name_only_name(self) -> None:
        self.assertEqual(strip_leading_sender_name("Bob", "Bob"), "")

    def test_finalize_quote_content(self) -> None:
        # Exercises sender strip → time strip → whitespace (fallback DOM order).
        self.assertEqual(
            finalize_quote_content("Bob 17:22 Quoted text", "Bob"),
            "Quoted text",
        )

    def test_finalize_quote_content_unicode(self) -> None:
        self.assertEqual(
            finalize_quote_content(
                "Андрей Аникин лендинг - https://example.com",
                "Андрей Аникин",
            ),
            "лендинг - https://example.com",
        )


class ConstantsTests(unittest.TestCase):
    """URL pattern and timezone shared with ParsingTask validators.

    group_url_pattern is the single source of truth — cli and models tests
    mirror these cases with different entry points (Typer vs Pydantic).
    """

    def test_group_url_pattern_accepts_valid_group(self) -> None:
        url = f"{SBERCHAT_BASE_URL}#/chat/group796209083"
        self.assertIsNotNone(group_url_pattern.fullmatch(url))

    def test_group_url_pattern_rejects_private_chat(self) -> None:
        # Private DM URLs must fail — collector targets group chats only.
        url = f"{SBERCHAT_BASE_URL}#/chat/private123"
        self.assertIsNone(group_url_pattern.fullmatch(url))

    def test_group_url_pattern_rejects_trailing_slash(self) -> None:
        url = f"{SBERCHAT_BASE_URL}#/chat/group123/"
        self.assertIsNone(group_url_pattern.fullmatch(url))

    def test_group_url_pattern_rejects_query_string(self) -> None:
        url = f"{SBERCHAT_BASE_URL}#/chat/group123?foo=1"
        self.assertIsNone(group_url_pattern.fullmatch(url))

    def test_moscow_timezone_name(self) -> None:
        self.assertEqual(moscow_timezone.key, "Europe/Moscow")


class MessageIdNormalizationTests(unittest.TestCase):
    """normalize_message_id / dom_message_id round-trip for Selenium vs JSON."""

    def test_pipe_becomes_underscore(self) -> None:
        raw = "5621764540321829361|5243477299780701419"
        normalized = normalize_message_id(raw)
        self.assertEqual(normalized, "5621764540321829361_5243477299780701419")
        self.assertEqual(dom_message_id(normalized), raw)

    def test_hyphen_in_second_segment_preserved(self) -> None:
        raw = "1553797810759471601|-857953021546601497"
        normalized = normalize_message_id(raw)
        self.assertEqual(normalized, "1553797810759471601_-857953021546601497")
        self.assertEqual(dom_message_id(normalized), raw)


class RowSelectorTests(unittest.TestCase):
    def test_row_selector_uses_dom_pipe_form(self) -> None:
        from pageup.threads import _row_selector

        normalized = "5621764540321829361_5243477299780701419"
        self.assertIn(
            'data-message-id="5621764540321829361|5243477299780701419"',
            _row_selector(normalized),
        )


if __name__ == "__main__":
    unittest.main()
