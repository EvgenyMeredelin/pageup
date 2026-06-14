"""Synthetic HTML fixtures for parser and runner unit tests.

These fragments mimic the SberChat SPA DOM structure using the same CSS-module
class names as pageup.config (e.g. MessageRow-MessageRowWrapper__cls1).
They let tests exercise models.ParsingTask.collect_messages(),
ParsingTask.collect_thread_reply_entries(), and runner.run() without a live browser.

Builder functions:
    message_row()           — one message row with optional sender, text, quotes, files
    quote_block()           — embedded reply preview inside a message
    attachment_block()      — file attachment cell
    image_attachment_block() — inline image attachment block (no filename cell)
    thread_bubble()         — green "N ответа" bubble on a main-channel message
    message_list_wrapper()  — main-feed MessageList container wrapper
    thread_panel_html()     — open ThreadContent panel with root + reply rows

Pre-built composites (import by name in test_models / test_runner / test_threads):
    SAMPLE_MESSAGE_HTML, TWO_MESSAGES_HTML, CONTINUATION_PAIR_HTML,
    MESSAGE_WITH_THREAD_BUBBLE_HTML, THREAD_PANEL_SAMPLE_HTML,
    MAIN_AND_THREAD_PANEL_HTML, etc.

Timestamp constants (TS_*) are Unix milliseconds matching data-message-date
attributes; models.Message.to_datetime() divides by 1000 when parsing.
"""

from datetime import datetime

# Selectors come from production config — when SberChat updates DOM class names,
# edit config.py only; fixtures stay aligned without duplicate string literals.
from pageup.config import (
    MSG_ATTACHMENT_CLS,
    MSG_CONTENT_SEL,
    MSG_IMAGE_WRAP_CLS,
    MSG_LIST_CONTAINER_CLS,
    MSG_SENDER_NAME_CLS,
    MSG_SENDER_URL_SEL,
    MSG_WRAP_CLS,
    QUOTE_CONTENT_SEL,
    QUOTE_SENDER_NAME_CLS,
    QUOTE_WRAP_CLS,
    THREAD_BUBBLE_CLS,
    THREAD_BUBBLE_TITLE_CLS,
    THREAD_PANEL_CLS,
)

# File-attachment cell class names — hardcoded here since they were removed from
# config.py when file-attachment recording was dropped.  These constants are only
# needed to build test HTML that exercises the "file block → no record" path.
_ATTACH_NAME_CLS = "Title-TitleContent__cls1"
_ATTACH_SIZE_CLS = "MessageFileCellV2-FileSubtitle__cls1"
_ATTACH_STATUS_CLS = "DocumentCheckStatus-DocumentCheckStatusText__cls1"
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
    thread_html: str = "",
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
{thread_html}
</div>
"""


def thread_bubble(reply_count: int) -> str:
    """Build a green thread reply-count bubble (e.g. ``3 ответа``)."""
    label = "1 ответ" if reply_count == 1 else f"{reply_count} ответов"
    if reply_count in {2, 3, 4}:
        label = f"{reply_count} ответа"
    return f"""
  <div class="{THREAD_BUBBLE_CLS}">
    <span class="{THREAD_BUBBLE_TITLE_CLS}">{label}</span>
  </div>"""


def message_list_wrapper(inner_html: str) -> str:
    """Wrap message rows in a main-feed ``MessageList`` container."""
    return f"""
<div class="{MSG_LIST_CONTAINER_CLS}">
{inner_html}
</div>
"""


def thread_panel_html(*, parent_id: str, replies: list[tuple[str, str, str]]) -> str:
    """Build a minimal open ``ThreadContent`` panel with root + replies.

    *replies* is a list of ``(message_id, sender_name, content)`` tuples.
    """
    root_row = message_row(
        parent_id,
        TS_2024_09_01,
        sender_name="Root Author",
        content="Root question",
    )
    reply_rows = "".join(
        message_row(mid, TS_2024_09_01, sender_name=name, content=text)
        for mid, name, text in replies
    )
    return f"""
<div class="{THREAD_PANEL_CLS}">
  <div class="{MSG_LIST_CONTAINER_CLS}">
{root_row}
{reply_rows}
  </div>
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
    """Build a file attachment block (MSG_ATTACHMENT_CLS + name/size cells).

    File blocks are no longer recorded by _collect_attachments (only image
    blocks with MSG_IMAGE_WRAP_CLS produce attachment entries).  This builder
    exists to verify the "file block → no record" behaviour in tests.
    """
    size_html = ""
    if size is not None:
        size_html = f'<div class="{_ATTACH_SIZE_CLS}">{size}</div>'
    return f"""
  <div class="{MSG_ATTACHMENT_CLS}">
    <div class="{_ATTACH_NAME_CLS}">{name}</div>
    {size_html}
  </div>"""


def attachment_with_status_block(
    name: str,
    *,
    size: str | None = None,
    status: str | None = None,
) -> str:
    """Build a file cell with optional size and upload-status labels.

    Used in tests that verify file blocks produce no attachment record.
    """
    size_html = f'<div class="{_ATTACH_SIZE_CLS}">{size}</div>' if size else ""
    status_html = (
        f'<div class="{_ATTACH_STATUS_CLS}">{status}</div>' if status else ""
    )
    return f"""
  <div class="{MSG_ATTACHMENT_CLS}">
    <div class="MessageFileCellV2-MessageFileCellWrapper__cls1">
      <div class="{_ATTACH_NAME_CLS}">{name}</div>
      {size_html}
      {status_html}
    </div>
  </div>"""


def image_attachment_block() -> str:
    """Build an inline image attachment block (MSG_ATTACHMENT_CLS + MSG_IMAGE_WRAP_CLS).

    Mirrors the DOM shape for messages with shared images — no filename cell,
    just a wrapper containing an image component.  Used to test that
    ``_collect_attachments`` records a ``None`` placeholder for these blocks.
    """
    return f"""
  <div class="{MSG_ATTACHMENT_CLS}">
    <div class="MessageContentMedia-MessageContentMediaWrapper__cls1">
      <div data-image-thumb="123456789" class="MessageMedia-MessageMediaWrapper__cls1 MessageMedia-MessageMediaWrapper__clickable">
        <div class="{MSG_IMAGE_WRAP_CLS}">
          <img src="blob:https://sberchat.sberbank.ru/abc123" />
        </div>
      </div>
    </div>
  </div>"""


def video_attachment_block() -> str:
    """Build an inline video attachment block (VideoMedia, not MessageImage).

    Video previews reuse MessageMedia clickables but must not produce image slots.
    """
    return f"""
  <div class="{MSG_ATTACHMENT_CLS}">
    <div class="MessageContentMedia-MessageContentMediaWrapper__cls1">
      <div class="PhotoVideoMedia-PhotoVideoMediaWrapper__cls1 PhotoVideoMedia-PhotoVideoMediaWrapper__cls2">
        <div class="VideoMedia-VideoPreview__cls1 VideoMedia-VideoPreview__cls2">
          <div class="{MSG_IMAGE_WRAP_CLS}">
            <img src="blob:https://sberchat.sberbank.ru/video-thumb" />
          </div>
          <div class="MessageMedia-MessageMediaWrapper__cls1 MessageMedia-MessageMediaWrapper__clickable"></div>
        </div>
      </div>
    </div>
  </div>"""


def multi_attachment_block(
    *files: tuple[str, str | None],
) -> str:
    """Build one DocumentBlock containing multiple file cells (live SberChat shape).

    Used in tests that verify multi-file blocks produce no attachment record.
    """
    cells = []
    for name, size in files:
        size_html = ""
        if size is not None:
            size_html = (
                f'<div class="{_ATTACH_SIZE_CLS}">'
                f'<div class="Subtitle-SubtitleContent__cls1">{size}</div>'
                f"</div>"
            )
        cells.append(
            f"""
    <div class="MessageFileCellV2-MessageFileCellWrapper__cls1">
      <div class="{_ATTACH_NAME_CLS}">{name}</div>
      {size_html}
    </div>"""
        )
    return f"""
  <div class="{MSG_ATTACHMENT_CLS}">
    {"".join(cells)}
  </div>"""


# ── Pre-built fixture composites ──────────────────────────────────────────────

# Full message: text + quote + image attachment — primary happy-path parsing test.
# Uses an image block (not a file block) so _collect_attachments produces a slot.
SAMPLE_MESSAGE_HTML = message_row(
    "msg-1",
    TS_2024_09_01,
    sender_name="Alice",
    content="Hello team",
    quotes_html=quote_block("Bob", "Earlier text"),
    attachments_html=image_attachment_block(),
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

# No text span — truthy via attachments or thread bubble (Message.__bool__).
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

MESSAGE_WITH_THREAD_BUBBLE_HTML = message_list_wrapper(
    message_row(
        "msg-thread",
        TS_2024_09_01,
        sender_name="Alice",
        content="Question with thread",
        thread_html=thread_bubble(3),
    )
)

THREAD_PANEL_SAMPLE_HTML = thread_panel_html(
    parent_id="msg-root",
    replies=[
        ("reply-1", "Bob", "First reply"),
        ("reply-2", "Carol", "Second reply"),
    ],
)

MAIN_AND_THREAD_PANEL_HTML = message_list_wrapper(
    message_row("msg-main", TS_2024_09_01, content="Main only")
) + THREAD_PANEL_SAMPLE_HTML.replace("msg-root", "msg-main")

# Message with an inline image attachment — exercises the image-detection branch
# of _collect_attachments (MSG_ATTACHMENT_CLS block containing MSG_IMAGE_WRAP_CLS).
IMAGE_ATTACHMENT_HTML = message_row(
    "msg-image",
    TS_2024_09_01,
    content="",
    attachments_html=image_attachment_block(),
)

# Message with both a text body and an image attachment.
IMAGE_WITH_TEXT_HTML = message_row(
    "msg-image-text",
    TS_2024_09_01,
    content="See attached",
    attachments_html=image_attachment_block(),
)

# Inline video — must not produce an image attachment slot.
VIDEO_ATTACHMENT_HTML = message_row(
    "msg-video",
    TS_2024_09_01,
    content="",
    attachments_html=video_attachment_block(),
)

BLOCK_MESSAGE_TEXT_HTML = message_list_wrapper(f"""
<div class="{MSG_WRAP_CLS}"
     data-message-id="msg-block"
     data-message-date="{TS_2024_09_01}">
  <div class="{_css_class(MSG_CONTENT_SEL)}">Block body text</div>
</div>
""")
