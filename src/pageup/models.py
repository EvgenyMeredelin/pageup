"""Pydantic data models for the pageup package.

Hierarchy
---------
``Quote``
    A reply/quote block embedded in a message.  Stores the quoted
    sender's name and cleaned text (duplicate sender prefix removed from
    ``content`` when the DOM repeats it).

``Entry``
    A SberChat message entry: base class for both main-channel messages
    and thread replies.  Stores ``message_id``, timestamp, sender,
    content, quotes, and attachments (downloaded image filenames).

``Message``
    A main-channel SberChat message.  Inherits from ``Entry``.
    Immutable (``frozen=True``), hashable by ``message_id``, and
    serialisable to the JSON output format via a custom
    ``model_serializer``.  Adds ``thread_reply_count`` and
    ``thread_replies``.

``ParsingTask``
    Encapsulates the parameters of a collection run (target group, date
    range, output name) together with all DOM-parsing logic.  The
    ``collect_messages`` and ``collect_thread_reply_entries`` methods are
    the primary HTML entry points; ``pageup.runner`` and ``pageup.threads``
    call them during the main scroll loop and thread-panel workflow.
"""

import itertools
import json
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    ValidationError,
    field_serializer,
    field_validator,
    model_serializer,
)

from pageup.config import (
    # All selectors below are CSS-module class names or compound selectors —
    # edit config.py when SberChat updates its DOM, not this import list.
    MSG_ATTACHMENT_CLS,
    MSG_CONTENT_SEL,
    MSG_IMAGE_WRAP_CLS,
    MSG_VIDEO_MEDIA_PREFIXES,
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
from pageup.tools import (
    SBERCHAT_BASE_URL,
    Pipeline,
    cleaner,
    finalize_quote_content,
    group_url_pattern,
    moscow_timezone,
    normalize_message_id,
)


# ── Type aliases ──────────────────────────────────────────────────────────────

# Type of the dict produced by Message.rearrange_fields (used before the
# final JSON serialisation step so that _patch can mutate sender fields).
# Plain dicts are used here because Message.patch mutates rows in place after dump.
type MsgDump = dict[str, str | int | datetime | list | None]

# Parsed once: _scope_root returns this when a scope must yield no message rows
# (thread panel closed, or main feed obscured by an open panel).
_EMPTY_SCOPE_SOUP = BeautifulSoup("<html></html>", "lxml")


def _tag_attr_str(value: str | list[str] | None) -> str | None:
    """Normalise a BeautifulSoup attribute value to a single string.

    BS4 may return str or list[str] for attributes like class_ and href
    depending on parser and element shape.  data-message-id is always a
    single string in production DOM; this helper keeps parsing code uniform.
    """
    if value is None:
        return None
    if isinstance(value, list):
        # Multi-valued attributes: take the first entry (e.g. duplicate href).
        return value[0] if value else None
    return value


# ── Quote ─────────────────────────────────────────────────────────────────────

class Quote(BaseModel):
    """An embedded reply / quote block inside a SberChat message.

    Attributes
    ----------
    sender_name:
        Display name of the user whose message is being quoted.
        ``None`` when the sender element is not present in the DOM
        (e.g. deleted accounts).
    content:
        Cleaned text of the quoted message, without a duplicate sender-name
        prefix when SberChat embeds it in the content element.
    """

    sender_name: str | None
    content: str


# ── Entry ─────────────────────────────────────────────────────────────────────

class Entry(BaseModel):
    """A SberChat message entry (main-channel message or thread reply).

    Base class for ``Message``.  Thread replies are also plain ``Entry``
    instances (``Message`` adds ``thread_reply_count`` and
    ``thread_replies``).

    Attributes
    ----------
    message_id:
        Internal SberChat identifier read from the ``data-message-id``
        attribute (``|`` replaced with ``_`` for filesystem safety).
        Included in JSON output to correlate with downloaded image filenames.
    date:
        Timestamp as a timezone-aware ``datetime`` (Moscow time).
        Parsed from the Unix-millisecond ``data-message-date`` attribute.
    sender_url:
        Absolute URL of the sender's SberChat profile page, or ``None``
        for continuation messages (backfilled by
        ``collect_thread_reply_entries``).
    sender_name:
        Display name of the sender, or ``None`` for continuation messages.
    content:
        Cleaned text body.  Empty string when the entry carries only
        attachments or a thread bubble.
    quotes:
        Embedded reply/quote blocks, or ``None`` when absent.
    attachments:
        Filenames of downloaded image attachments (e.g.
        ``["1553…_0.png"]``), or ``None`` when absent.  ``None`` slots
        in the in-memory list represent images pending download;
        the ``@field_serializer`` strips them before JSON output.
    """

    message_id: str
    date: AwareDatetime
    sender_url: str | None
    sender_name: str | None
    content: str
    quotes: list[Quote] | None = None
    attachments: list[str | None] | None = None

    @field_validator("date", mode="before")
    @classmethod
    def to_datetime(cls, value: str | datetime) -> datetime:
        """Convert a Unix-millisecond string to a Moscow-aware datetime.

        Millisecond strings use ``fromtimestamp(..., tz=moscow_timezone)`` so
        results are correct regardless of the host system's local timezone.
        """
        if isinstance(value, datetime):
            # Naive datetimes (e.g. from tests) are treated as Moscow wall time.
            if value.tzinfo is None:
                return value.replace(tzinfo=moscow_timezone)
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit() or (
                stripped.startswith("-") and stripped[1:].isdigit()
            ):
                since_epoch = int(stripped) // 1000
                return datetime.fromtimestamp(since_epoch, tz=moscow_timezone)
            try:
                parsed = datetime.fromisoformat(stripped)
            except ValueError:
                pass
            else:
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=moscow_timezone)
                return parsed
        # SberChat stores timestamps as milliseconds since the Unix epoch.
        since_epoch = int(value) // 1000
        return datetime.fromtimestamp(since_epoch, tz=moscow_timezone)

    @field_serializer("attachments")
    def serialize_attachments(
        self, v: list[str | None] | None
    ) -> list[str] | None:
        """Strip ``None`` and skipped (``""``) placeholders; return ``None`` when empty."""
        if not v:
            return None
        result = [a for a in v if a]
        return result if result else None


# ── Message ───────────────────────────────────────────────────────────────────

class Message(Entry):
    """A main-channel SberChat message.

    Inherits ``message_id``, ``date``, ``sender_url``, ``sender_name``,
    ``content``, ``quotes``, and ``attachments`` from ``Entry``.

    The model is frozen (immutable) so that instances can be used as dict
    keys for deduplication via ``dict.fromkeys``.

    Attributes
    ----------
    thread_reply_count:
        Reply count from the green thread bubble (e.g. ``3`` for ``"3 ответа"``).
        ``None`` when the message has no thread bubble.
    thread_replies:
        Replies collected from the discussion panel, excluding the root
        message.  ``None`` when there is no bubble or collection failed.

    Truthiness (``__bool__``): messages with content, pending or downloaded
    attachments, a thread bubble (``thread_reply_count``), or collected
    ``thread_replies`` are kept during post-processing.  Skipped video slots
    (``""``) do not count as attachments; empty rows without any of the above
    are dropped.
    """

    # frozen=True makes instances hashable and immutable — required for
    # dict.fromkeys deduplication in ParsingTask._unique_reversed.
    # Entry is not frozen so thread-reply attachments can be mutated in place
    # during image download.
    model_config = ConfigDict(frozen=True)

    thread_reply_count: int | None = None
    thread_replies: list[Entry] | None = None

    @model_serializer(when_used="always")
    def rearrange_fields(self) -> MsgDump:
        """Serialise to a dict with a human-friendly field order."""
        atts = self.attachments
        return {
            "message_id": self.message_id,
            "date": self.date,
            "sender_url": self.sender_url,
            "sender_name": self.sender_name,
            "content": self.content,
            "thread_reply_count": self.thread_reply_count,
            "thread_replies": self.thread_replies,
            "quotes": self.quotes,
            "attachments": (
                ([a for a in atts if a] or None)
                if atts
                else None
            ),
        }

    def __hash__(self) -> int:
        """Hash by ``message_id`` for use in ``dict.fromkeys`` deduplication."""
        return hash(self.message_id)

    def __eq__(self, other: object) -> bool:
        """Two messages are equal when they share the same ``message_id``."""
        if not isinstance(other, Message):
            return False
        return self.message_id == other.message_id

    def __bool__(self) -> bool:
        """A message is truthy when it has content, attachments, or thread data.

        Pending image slots (``None``) count as attachments; skipped video
        placeholders (``""``) do not.  Textless, attachment-free rows without a
        thread bubble or collected replies (rare reaction-only artefacts) are
        falsy and filtered out during post-processing.
        """
        return (
            bool(self.content)
            or any(
                slot is None or bool(slot)
                for slot in (self.attachments or [])
            )
            or bool(self.thread_reply_count)
            or bool(self.thread_replies)
        )

    @classmethod
    def patch(cls, this: MsgDump, that: MsgDump) -> None:
        """Backfill ``sender_url`` and ``sender_name`` on *this* from *that*.

        Only when *this* lacks **both** sender fields (continuation messages).
        Called from ``ParsingTask._patch`` with *that* chronologically earlier.
        Partial sender info on *this* is left unchanged.
        """
        if not this["sender_url"] and not this["sender_name"]:
            # Both fields must be missing — partial sender info is kept as-is.
            this["sender_url"] = that["sender_url"]
            this["sender_name"] = that["sender_name"]


# ── ParsingTask ───────────────────────────────────────────────────────────────

class ParsingTask(BaseModel):
    """Parameters and parsing logic for a single collection run.

    One ``ParsingTask`` instance corresponds to one invocation of the
    ``pageup`` CLI command.  The runner feeds successive ``BeautifulSoup``
    snapshots to ``collect_messages``, calls ``pageup.threads`` to enrich
    uncollected messages with discussion-panel replies and to restore main-feed
    focus before each scroll step, then runs ``write_json`` once the target date
    is reached.

    Attributes
    ----------
    name:
        Logical name for the chat; output file is ``{write_dir}/{name}.json``.
    group_url:
        Fully-qualified URL of the SberChat group, e.g.
        ``https://sberchat.sberbank.ru/#/chat/group796209083``.
    min_date:
        Timezone-aware datetime.  Collection stops when any of these occur:
        the oldest visible message predates this cutoff; ~30 s of scrolling
        with no parseable message rows; or ~4 s of scrolling with no new
        ``message_id`` values while history cannot reach this date.
    """

    name: str
    group_url: str
    min_date: AwareDatetime

    @field_validator("name", mode="after")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Reject empty names and unsafe characters in the output filename."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("`name` must not be empty")
        if stripped in {".", ".."} or "/" in stripped or "\\" in stripped:
            # Reject `.` / `..` even without slashes (reserved directory names) and
            # any path separator so write_json cannot escape write_dir.
            raise ValueError(
                "`name` must be a plain filename — no `/` or `\\`, not `.` or `..`"
            )
        if "\0" in stripped:
            raise ValueError("`name` must not contain null bytes")
        # Return stripped so accidental leading/trailing spaces do not reach the path.
        return stripped

    @field_validator("group_url", mode="after")
    @classmethod
    def validate_group_url(cls, value: str) -> str:
        """Reject URLs that do not point to a SberChat group chat."""
        # fullmatch — entire string must match; trailing slashes or query params fail.
        if not group_url_pattern.fullmatch(value):
            raise ValueError("`group_url` must be a SberChat group URL")
        return value

    # ── Public interface ──────────────────────────────────────────────────────

    def collect_messages(self, soup: BeautifulSoup, *, scope: str = "main") -> list[Message]:
        """Parse all message rows currently visible in *soup*.

        Returns a list ordered newest-first (index 0 = most recent message
        visible on screen, last index = oldest).  This matches the scroll
        direction: the caller checks the last element to decide whether the
        target date has been reached.

        Parameters
        ----------
        soup:
            Parsed HTML snapshot of the current browser page.
        scope:
            ``"main"`` parses the main chat feed (excludes rows inside an
            open thread panel).  ``"thread"`` parses rows inside the
            discussion panel only.
        """
        messages = self._collect_row_messages(soup, scope=scope)
        return list(reversed(messages))

    def collect_thread_reply_entries(
        self,
        soup: BeautifulSoup,
        parent_id: str,
    ) -> list[Entry]:
        """Parse thread-panel replies, excluding the root *parent_id* message."""
        rows = [
            m
            for m in self._collect_row_messages(soup, scope="thread")
            if m.message_id != parent_id
        ]
        if not rows:
            return []

        dumps = [m.model_dump() for m in rows]
        for previous, current in itertools.pairwise(dumps):
            Message.patch(current, previous)

        entries: list[Entry] = []
        for message, dump in zip(rows, dumps, strict=True):
            entries.append(Entry(
                message_id=message.message_id,
                date=message.date,
                sender_url=dump["sender_url"],
                sender_name=dump["sender_name"],
                quotes=message.quotes,
                attachments=message.attachments,
                content=message.content,
            ))
        return entries

    def is_done(self, message: Message) -> bool:
        """Return ``True`` when *message* is older than ``min_date``.

        Used by the scroll loop to detect that it has scrolled far enough
        back in history to cover the requested date range.

        Strict ``<`` comparison: messages on ``min_date`` itself are still
        collected (e.g. min_date 2024-09-01 includes all of 1 September).
        """
        return message.date < self.min_date

    @staticmethod
    def prefer_richer_message(existing: Message, incoming: Message) -> Message:
        """Return *incoming* enriched with the more complete ``thread_replies``."""
        old_count = len(existing.thread_replies or [])
        new_count = len(incoming.thread_replies or [])
        thread_reply_count = (
            incoming.thread_reply_count or existing.thread_reply_count
        )

        if new_count > old_count:
            return incoming.model_copy(update={"thread_reply_count": thread_reply_count})
        if old_count > new_count:
            return incoming.model_copy(
                update={
                    "thread_replies": existing.thread_replies,
                    "thread_reply_count": thread_reply_count,
                }
            )
        if old_count > 0 and existing.thread_replies and incoming.thread_replies:
            # Equal non-zero counts: keep existing replies (they may have richer data
            # from a prior full collection pass, e.g. sender names that were patched).
            return incoming.model_copy(
                update={
                    "thread_replies": existing.thread_replies,
                    "thread_reply_count": thread_reply_count,
                }
            )
        return incoming.model_copy(update={"thread_reply_count": thread_reply_count})

    @staticmethod
    def thread_is_complete(message: Message) -> bool:
        """Return True when collected thread replies meet the bubble count."""
        expected = message.thread_reply_count
        if not expected or expected <= 0:
            return True
        replies = message.thread_replies
        return replies is not None and len(replies) >= expected

    def write_json(self, messages: list[Message], write_dir: str) -> None:
        """Deduplicate, order chronologically, patch, and write *messages* to a JSON file.

        The output file is placed at ``{write_dir}/{self.name}.json``.
        Post-processing steps applied in order:

        1. **Unique + sort** — deduplicate by ``message_id`` (batches
           from successive page snapshots overlap) and sort by ``date``
           to chronological order (oldest first).
        2. **Dump** — convert each ``Message`` to a plain dict via
           ``model_dump()``.
        3. **Patch** — backfill missing sender info on continuation messages.

        Parameters
        ----------
        messages:
            Accumulated message list from the runner.  May be in any order;
            ``_unique_reversed`` deduplicates and sorts chronologically before
            writing.
        write_dir:
            Directory in which to write the output file.  Created by the
            caller before this method is invoked.
        """
        # tools.Pipeline chains post-processing: dedupe+sort → dict dump → patch.
        # Same Pipeline class as tools.cleaner; keeps write_json declarative.
        postprocessor = Pipeline(
            ParsingTask._unique_reversed,
            ParsingTask._dump_messages,
            ParsingTask._patch,
        )
        target_file = Path(write_dir) / f"{self.name}.json"
        with open(target_file, "w", encoding="utf-8") as fp:
            json.dump(
                obj=postprocessor(messages),
                fp=fp,
                # default=str serialises datetime fields to ISO-like strings in JSON.
                # ensure_ascii=False keeps Cyrillic sender names readable in the file.
                default=str,
                ensure_ascii=False,
                indent=4,
            )
        # Prefix matches runner._log(); JSON body is printed separately (multi-line).
        print(f"[pageup] Output written to: {target_file}", flush=True)

    # ── Private DOM helpers ───────────────────────────────────────────────────

    def _collect_row_messages(self, soup: BeautifulSoup, *, scope: str) -> list[Message]:
        """Parse message rows from *soup* in DOM order (oldest first)."""
        if scope not in {"main", "thread"}:
            raise ValueError(f"unknown scope {scope!r}; expected 'main' or 'thread'")
        root = self._scope_root(soup, scope=scope)
        divs: list[Tag] = root.find_all("div", class_=MSG_WRAP_CLS)
        messages: list[Message] = []
        for div in divs:
            raw_id = _tag_attr_str(div.get("data-message-id"))
            message_id = normalize_message_id(raw_id) if raw_id else raw_id
            message_date = _tag_attr_str(div.get("data-message-date"))
            if not message_id or not message_date:
                continue
            try:
                messages.append(
                    Message(
                        message_id=message_id,
                        date=message_date,
                        sender_url=self._get_sender_url(div),
                        sender_name=self._get_sender_name(div, class_=MSG_SENDER_NAME_CLS),
                        quotes=self._collect_quotes(div),
                        attachments=self._collect_attachments(div),
                        content=self._get_message_content(div),
                        thread_reply_count=(
                            self._parse_thread_reply_count(div)
                            if scope == "main"
                            else None
                        ),
                    )
                )
            except ValidationError:
                continue
        return messages

    @staticmethod
    def _scope_root(soup: BeautifulSoup, *, scope: str) -> Tag | BeautifulSoup:
        """Return the DOM subtree to scan for ``MSG_WRAP_CLS`` message rows.

        Main scope prefers the ``MessageList`` container outside ``ThreadContent``.
        When the discussion panel is open but the main list is unavailable, returns
        an empty document so panel rows are not collected as main-channel messages.

        Thread scope parses inside the open panel only.  When the panel is absent
        from *soup*, returns an empty document so main-feed rows are not mistaken
        for thread replies.
        """
        if scope == "thread":
            panel = soup.find("div", class_=THREAD_PANEL_CLS)
            if panel is None:
                return _EMPTY_SCOPE_SOUP
            container = panel.find("div", class_=MSG_LIST_CONTAINER_CLS)
            return container if container is not None else panel

        containers = soup.find_all("div", class_=MSG_LIST_CONTAINER_CLS)
        for container in containers:
            if container.find_parent("div", class_=THREAD_PANEL_CLS) is None:
                return container
        if soup.find("div", class_=THREAD_PANEL_CLS) is None:
            return soup
        return _EMPTY_SCOPE_SOUP

    @staticmethod
    def _parse_thread_reply_count(row: Tag) -> int | None:
        bubble = row.find("div", class_=THREAD_BUBBLE_CLS)
        if bubble is None:
            return None
        title = bubble.find("span", class_=THREAD_BUBBLE_TITLE_CLS)
        if title is None:
            title = bubble.find(class_=THREAD_BUBBLE_TITLE_CLS)
        if title is None:
            return None
        text = title.get_text(strip=True)
        match = re.search(r"(\d+)\s+ответ(?:а|ов)?", text)
        if not match:
            return None
        return int(match.group(1))

    def _get_sender_url(self, elem: Tag) -> str | None:
        """Extract the sender's absolute profile URL from a message element."""
        tag = elem.select_one(MSG_SENDER_URL_SEL)
        if not tag:
            return None
        href = _tag_attr_str(tag.get("href"))
        if not href:
            return None
        if href.startswith("http://") or href.startswith("https://"):
            return href
        # href is typically a fragment (e.g. #/chat/private…).
        return SBERCHAT_BASE_URL + href

    def _get_sender_name(self, elem: Tag, *, class_: str) -> str | None:
        """Extract and clean the sender's display name from *elem*."""
        div = elem.find("div", class_=class_)
        if not div:
            return None
        return ParsingTask._get_text(div)

    def _get_message_content(self, elem: Tag) -> str:
        """Extract and concatenate message body text nodes in *elem*."""
        tags = elem.select(MSG_CONTENT_SEL)
        if not tags:
            return ""
        # Prefer outermost matches so nested spans inside a block wrapper
        # are not concatenated twice when both carry MSG_CONTENT_SEL.
        tags = [t for t in tags if not any(t in p.descendants for p in tags if p is not t)]
        return " ".join(ParsingTask._get_text(t) for t in tags)

    def _collect_quotes(self, elem: Tag) -> list[Quote] | None:
        """Extract all embedded reply/quote blocks from *elem*.

        Returns ``None`` (not an empty list) when no quotes are present,
        so that the field serialises as JSON ``null`` rather than ``[]``.

        Quote ``content`` is passed through ``finalize_quote_content`` so a
        duplicate sender prefix and fallback time tokens are removed.
        """
        divs: list[Tag] = elem.find_all("div", class_=QUOTE_WRAP_CLS)
        if not divs:
            return None
        quotes: list[Quote] = []
        for div in divs:
            sender_name = self._get_sender_name(
                div, class_=QUOTE_SENDER_NAME_CLS
            )
            content = finalize_quote_content(
                self._get_quote_content(div), sender_name
            )
            quotes.append(Quote(sender_name=sender_name, content=content))
        return quotes

    def _get_quote_content(self, quote_div: Tag) -> str:
        """Extract raw quote text from its reply wrapper.

        Uses ``QUOTE_CONTENT_SEL`` when present.  Falls back to the full
        wrapper text (e.g. file-only quotes).  Sender-name deduplication and
        time-prefix stripping happen in ``finalize_quote_content``.
        """
        content_tag = quote_div.select_one(QUOTE_CONTENT_SEL)
        if content_tag:
            return ParsingTask._get_text(content_tag)
        # Fallback: clean the whole wrapper text; finalize_quote_content strips
        # sender/time prefixes that survive when the name precedes HH:MM.
        return ParsingTask._get_text(quote_div)

    def _collect_attachments(self, elem: Tag) -> list[str | None] | None:
        """Extract image attachment slots from *elem*.

        Returns ``None`` when the message carries no image attachments.

        Only image attachment blocks (those containing ``MSG_IMAGE_WRAP_CLS``)
        are recorded.  Video attachments (``VideoMedia-`` / ``PhotoVideoMedia-``)
        and file blocks are skipped.  Each image slot is ``None`` until
        ``threads.download_fresh_images`` fills it with the saved filename.
        """
        blocks: list[Tag] = elem.find_all("div", class_=MSG_ATTACHMENT_CLS)
        if not blocks:
            return None
        image_count = sum(
            1 for block in blocks if ParsingTask._is_image_attachment_block(block)
        )
        return [None] * image_count or None

    @staticmethod
    def _is_image_attachment_block(block: Tag) -> bool:
        """Return True for inline image blocks, False for video/file blocks."""
        if block.find(class_=MSG_IMAGE_WRAP_CLS) is None:
            return False
        if block.find("video") is not None:
            return False
        for el in block.find_all(True):
            raw = el.get("class")
            if not raw:
                continue
            class_list = raw if isinstance(raw, list) else [raw]
            for cls in class_list:
                if any(cls.startswith(prefix) for prefix in MSG_VIDEO_MEDIA_PREFIXES):
                    return False
        return True

    # ── Static post-processing helpers ────────────────────────────────────────

    @staticmethod
    def _get_text(elem: Tag) -> str:
        """Return cleaned plain text extracted from a BeautifulSoup element."""
        return cleaner(elem.get_text(strip=True, separator=" "))

    @staticmethod
    def _unique_reversed(messages: list[Message]) -> list[Message]:
        """Deduplicate and sort a message list chronologically (oldest first).

        Successive page snapshots overlap, so the accumulated list can
        contain the same message multiple times.  ``dict.fromkeys``
        drops duplicates by ``message_id`` via ``Message.__hash__`` and
        ``Message.__eq__``, keeping the first occurrence.

        The unique set is then sorted by ``(date, message_id)``; input
        order does not affect the result.  ``message_id`` breaks ties for
        same-second rows (e.g. continuation messages after their group
        header).  Truthy-falsy filtering (``filter(bool, …)``) removes
        rows with no content, attachments, thread bubble, or collected
        thread replies (see ``Message.__bool__``).
        """
        unique = list(dict.fromkeys(messages))
        ordered = sorted(unique, key=lambda message: (message.date, message.message_id))
        return list(filter(bool, ordered))

    @staticmethod
    def _dump_messages(messages: list[Message]) -> list[MsgDump]:
        """Convert a list of ``Message`` instances to plain dicts."""
        return [m.model_dump() for m in messages]

    @staticmethod
    def _patch(messages: list[MsgDump]) -> list[MsgDump]:
        """Backfill missing sender fields on continuation messages.

        SberChat only shows the author header on the first message in a
        consecutive group from the same sender.  Walking the sorted list in
        pairs and copying sender info forward fills the gaps.
        """
        for previous, current in itertools.pairwise(messages):
            # Chronological walk: continuation rows copy sender from the row above.
            # pairwise never patches index 0 — the first message has no predecessor.
            Message.patch(current, previous)
        return messages
