"""Synthetic HTML fixtures for parser and runner unit tests.

These fragments mimic the SberChat SPA DOM structure using the same CSS-module
class names as pageup.config (e.g. MessageRow-MessageRowWrapper__cls1).
They let tests exercise models.ParsingTask.collect_messages() and runner.run()
without a live browser.

Builder functions:
    message_row()     — one message row with optional sender, text, quotes, files
    quote_block()     — embedded reply preview inside a message
    attachment_block() — file attachment cell

Pre-built composites (import by name in test_models / test_runner):
    SAMPLE_MESSAGE_HTML, TWO_MESSAGES_HTML, CONTINUATION_PAIR_HTML, etc.

Timestamp constants (TS_*) are Unix milliseconds matching data-message-date
attributes; models.Message.to_datetime() divides by 1000 when parsing.
"""

from datetime import datetime

# Selectors come from production config — when SberChat updates DOM class names,
# edit config.py only; fixtures stay aligned without duplicate string literals.
from pageup.config import (
    ATTACH_NAME_CLS,
    ATTACH_SIZE_CLS,
    MSG_ATTACHMENT_CLS,
    MSG_CONTENT_SEL,
    MSG_SENDER_NAME_CLS,
    MSG_SENDER_URL_SEL,
    MSG_WRAP_CLS,
    QUOTE_CONTENT_SEL,
    QUOTE_SENDER_NAME_CLS,
    QUOTE_WRAP_CLS,
)
from pageup.tools import moscow_timezone

# ── Reference timestamps (Unix ms, Moscow calendar dates) ─────────────────────

# 2024-01-15 — used as "old" message in TWO_MESSAGES_HTML and is_done tests.
TS_2024_01_15 = int(
    datetime(2024, 1, 15, 12, 0, tzinfo=moscow_timezone).timestamp() * 1000
)

# 2024-09-01 — typical "in range" message date for parsing tests.
TS_2024_09_01 = int(
    datetime(2024, 9, 1, 12, 0, tzinfo=moscow_timezone).timestamp() * 1000
)

# Valid group URL accepted by ParsingTask.validate_group_url / tools.group_url_pattern.
# Digits-only group id here — pattern requires group\d+ (see tools.group_url_pattern).
GROUP_URL = "https://sberchat.sberbank.ru/#/chat/group123"


# ── HTML builders ─────────────────────────────────────────────────────────────

def _css_class(selector: str) -> str:
    """Return the class part of a simple ``tag.class`` CSS selector.

    config.py stores compound selectors (e.g. ``span.BlockMessageText__cls1``);
    HTML builders need only the class suffix for ``class="..."`` attributes.
    """
    _, _, class_name = selector.partition(".")
    return class_name


def message_row(
    message_id: str,
    date_ms: int,
    *,
    sender_name: str | None = "Alice",
    sender_href: str = "#/chat/private123",
    content: str = "Hello team",
    quotes_html: str = "",
    attachments_html: str = "",
) -> str:
    """Build one SberChat message row HTML fragment.

    Mirrors the DOM shape parsed by ParsingTask.collect_messages():
    outer div with MSG_WRAP_CLS, data-message-id, data-message-date, then
    optional sender link, Lexical text span, quotes, and attachment blocks.

    Pass sender_name=None to simulate a continuation message (no author header).
    """
    sender_block = ""
    if sender_name is not None:
        # Relative href — models._get_sender_url() prepends SBERCHAT_BASE_URL.
        sender_link_cls = _css_class(MSG_SENDER_URL_SEL)
        sender_block = f"""
  <a class="{sender_link_cls}" href="{sender_href}">
    <div class="{MSG_SENDER_NAME_CLS}">{sender_name}</div>
  </a>"""

    content_block = ""
    if content:
        # One or more MSG_CONTENT_SEL spans; joined in _get_message_content().
        content_cls = _css_class(MSG_CONTENT_SEL)
        content_block = f'<span class="{content_cls}">{content}</span>'

    return f"""
<div class="{MSG_WRAP_CLS}"
     data-message-id="{message_id}"
     data-message-date="{date_ms}">
{sender_block}
  {content_block}
{quotes_html}
{attachments_html}
</div>
"""


def quote_block(sender_name: str, content: str, *, with_content_sel: bool = True) -> str:
    """Build a reply/quote block nested inside a message row.

    with_content_sel=True  → uses QUOTE_CONTENT_SEL (normal parsing path).
    with_content_sel=False → omits that div; triggers _get_quote_content() fallback
                             and tools.finalize_quote_content on wrapper text.

    The content element repeats *sender_name* before *content* when
    ``with_content_sel=True``, matching live SberChat DOM.  For the fallback
    path the wrapper text already includes the title div, so the span keeps
    only *content*.
    """
    if with_content_sel:
        quote_content_cls = _css_class(QUOTE_CONTENT_SEL)
        content_html = (
            f'<div class="{quote_content_cls}">{sender_name} {content}</div>'
        )
    else:
        content_html = f"<span>{content}</span>"
    return f"""
  <div class="{QUOTE_WRAP_CLS}">
    <div class="{QUOTE_SENDER_NAME_CLS}">{sender_name}</div>
    {content_html}
  </div>"""


def attachment_block(name: str, size: str | None = "1.2 MB") -> str:
    """Build a file attachment block (MSG_ATTACHMENT_CLS + name/size cells)."""
    size_html = ""
    if size is not None:
        size_html = f'<div class="{ATTACH_SIZE_CLS}">{size}</div>'
    return f"""
  <div class="{MSG_ATTACHMENT_CLS}">
    <div class="{ATTACH_NAME_CLS}">{name}</div>
    {size_html}
  </div>"""


# ── Pre-built fixture composites ──────────────────────────────────────────────

# Full message: text + quote + attachment — primary happy-path parsing test.
SAMPLE_MESSAGE_HTML = message_row(
    "msg-1",
    TS_2024_09_01,
    sender_name="Alice",
    content="Hello team",
    quotes_html=quote_block("Bob", "Earlier text"),
    attachments_html=attachment_block("report.pdf", "1.2 MB"),
)

# Row without data-message-id / data-message-date — collect_messages skips it.
INCOMPLETE_MESSAGE_HTML = f"""
<div class="{MSG_WRAP_CLS}">orphan row</div>
"""

# DOM order: older message first (top), newer second (bottom).
# collect_messages reverses to newest-first ["msg-new", "msg-old"].
TWO_MESSAGES_HTML = message_row(
    "msg-old",
    TS_2024_01_15,
    sender_name="Old Author",
    content="Old message",
) + message_row(
    "msg-new",
    TS_2024_09_01,
    sender_name="New Author",
    content="New message",
)

# Second row has no sender header — exercises Message.patch after write_json.
CONTINUATION_PAIR_HTML = message_row(
    "msg-first",
    TS_2024_09_01,
    sender_name="Alice",
    sender_href="#/chat/private999",
    content="First in group",
) + message_row(
    "msg-second",
    TS_2024_09_01,
    sender_name=None,
    content="Continuation without header",
)

# No text span — message is truthy via attachments only (Message.__bool__).
ATTACHMENT_ONLY_HTML = message_row(
    "msg-file",
    TS_2024_09_01,
    content="",
    attachments_html=attachment_block("slides.pptx", "4.5 MB"),
)

# Quote without QUOTE_CONTENT_SEL — fallback path + time prefix in content string.
QUOTE_FALLBACK_HTML = message_row(
    "msg-quote-fallback",
    TS_2024_09_01,
    quotes_html=quote_block("Bob", "17:22Quoted via fallback", with_content_sel=False),
)

# Absolute https sender href — _get_sender_url should not double-prefix base URL.
_sender_link_cls = _css_class(MSG_SENDER_URL_SEL)
_content_cls = _css_class(MSG_CONTENT_SEL)
ABSOLUTE_SENDER_URL_HTML = f"""
<div class="{MSG_WRAP_CLS}"
     data-message-id="msg-abs"
     data-message-date="{TS_2024_09_01}">
  <a class="{_sender_link_cls}"
     href="https://sberchat.sberbank.ru/#/chat/private999">
    <div class="{MSG_SENDER_NAME_CLS}">Carol</div>
  </a>
  <span class="{_content_cls}">Absolute link</span>
</div>
"""

# Extra whitespace in content — cleaned by tools.cleaner in _get_text().
MESSY_TEXT_HTML = message_row(
    "msg-messy",
    TS_2024_09_01,
    content="  Hello   world  ",
)
