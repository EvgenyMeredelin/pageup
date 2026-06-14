"""Pydantic data models for the pageup package.

Hierarchy
---------
``Attachment``
    A file or media attachment embedded in a message (filename + size).

``Quote``
    A reply/quote block embedded in a message.  Stores the quoted
    sender's name and cleaned text (duplicate sender prefix removed from
    ``content`` when the DOM repeats it).

``Message``
    A single SberChat message.  Immutable (``frozen=True``), hashable by
    ``message_id``, and serialisable to the JSON output format via a
    custom ``model_serializer``.

``ParsingTask``
    Encapsulates the parameters of a collection run (target group, date
    range, output name) together with all DOM-parsing logic.  The
    ``collect_messages`` method is the primary entry point called from
    the scroll loop in ``pageup.runner``.
"""

import itertools
import json
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_serializer,
)

from pageup.config import (
    # All selectors below are CSS-module class names or compound selectors —
    # edit config.py when SberChat updates its DOM, not this import list.
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
from pageup.tools import (
    SBERCHAT_BASE_URL,
    Pipeline,
    cleaner,
    finalize_quote_content,
    group_url_pattern,
    moscow_timezone,
)


# ── Type aliases ──────────────────────────────────────────────────────────────

# Type of the dict produced by Message.rearrange_fields (used before the
# final JSON serialisation step so that _patch can mutate sender fields).
# Plain dicts are used here because Message.patch mutates rows in place after dump.
type MsgDump = dict[str, str | datetime | list | None]


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


# ── Attachment ────────────────────────────────────────────────────────────────

class Attachment(BaseModel):
    """A file or media attachment embedded in a SberChat message.

    Attributes
    ----------
    name:
        The original filename as displayed in the chat (e.g. ``report.pdf``).
    size:
        Human-readable file size string as shown by SberChat
        (e.g. ``"17.2 КБ"``).  ``None`` when the size element is absent
        (rare for inline images).
    """

    name: str
    size: str | None


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


# ── Message ───────────────────────────────────────────────────────────────────

class Message(BaseModel):
    """A single SberChat message.

    The model is frozen (immutable) so that instances can be used as dict
    keys for deduplication via ``dict.fromkeys``.

    Attributes
    ----------
    message_id:
        Internal SberChat identifier read from the ``data-message-id``
        attribute.  Excluded from JSON output; used only for deduplication.
    date:
        Message timestamp as a timezone-aware ``datetime`` (Moscow time).
        Parsed from the Unix-millisecond ``data-message-date`` attribute.
    sender_url:
        Absolute URL of the sender's SberChat profile page.  ``None`` for
        continuation messages where SberChat omits the author header.
    sender_name:
        Display name of the sender.  ``None`` for the same continuation
        messages; backfilled by ``_patch`` during post-processing.
    quotes:
        List of reply/quote blocks embedded in this message, or ``None``
        if there are none.
    attachments:
        List of file/media attachments, or ``None`` if there are none.
    content:
        Cleaned text content of the message.  Empty string when the
        message carries only attachments with no text.
    """

    # frozen=True makes instances hashable and immutable — required for
    # dict.fromkeys deduplication in ParsingTask._unique_reversed.
    model_config = ConfigDict(frozen=True)

    message_id: str
    date: AwareDatetime
    sender_url: str | None
    sender_name: str | None
    quotes: list[Quote] | None
    attachments: list[Attachment] | None
    content: str

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
        # SberChat stores timestamps as milliseconds since the Unix epoch.
        # Use Moscow tz explicitly so parsing is correct regardless of the
        # host system's local timezone (e.g. UTC on a personal machine).
        since_epoch = int(value) // 1000
        return datetime.fromtimestamp(since_epoch, tz=moscow_timezone)

    @model_serializer(when_used="always")
    def rearrange_fields(self) -> MsgDump:
        """Serialise to a dict with a human-friendly field order.

        ``message_id`` is intentionally excluded — it is an internal
        deduplication key, not meaningful to downstream consumers.
        """
        return {
            "date": self.date,
            "sender_url": self.sender_url,
            "sender_name": self.sender_name,
            "quotes": self.quotes,
            "attachments": self.attachments,
            "content": self.content,
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
        """A message is truthy when it has text content or file attachments.

        Textless, attachment-free messages (rare artefacts that can appear
        when SberChat renders reaction-only or system rows) are falsy and
        will be filtered out during post-processing.
        """
        return bool(self.content) or bool(self.attachments)

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
    ``pageup`` CLI command.  The runner in ``pageup.runner`` feeds
    successive ``BeautifulSoup`` snapshots to ``collect_messages`` and
    calls ``write_json`` once the target date is reached.

    Attributes
    ----------
    name:
        Logical name for the chat; output file is ``{write_dir}/{name}.json``.
    group_url:
        Fully-qualified URL of the SberChat group, e.g.
        ``https://sberchat.sberbank.ru/#/chat/group796209083``.
    min_date:
        Timezone-aware datetime.  Collection stops when any of these occur:
        the oldest visible message predates this cutoff; ~60 s of scrolling
        with no parseable message rows; or ~60 s of scrolling with no new
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

    def collect_messages(self, soup: BeautifulSoup) -> list[Message]:
        """Parse all message rows currently visible in *soup*.

        Returns a list ordered newest-first (index 0 = most recent message
        visible on screen, last index = oldest).  This matches the scroll
        direction: the caller checks the last element to decide whether the
        target date has been reached.

        Parameters
        ----------
        soup:
            Parsed HTML snapshot of the current browser page.
        """
        divs: list[Tag] = soup.find_all("div", class_=MSG_WRAP_CLS)
        messages = []
        for div in divs:
            message_id = _tag_attr_str(div.get("data-message-id"))
            message_date = _tag_attr_str(div.get("data-message-date"))
            if not message_id or not message_date:
                # Skip partial DOM rows (e.g. placeholders while React loads).
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
                    )
                )
            except ValidationError:
                # Skip rows with malformed attributes (e.g. non-numeric date).
                continue
        # DOM order is top (oldest) → bottom (newest).  Reversing gives
        # newest-first, which is what the scroll-loop caller expects.
        return list(reversed(messages))

    def is_done(self, message: Message) -> bool:
        """Return ``True`` when *message* is older than ``min_date``.

        Used by the scroll loop to detect that it has scrolled far enough
        back in history to cover the requested date range.

        Strict ``<`` comparison: messages on ``min_date`` itself are still
        collected (e.g. min_date 2024-09-01 includes all of 1 September).
        """
        return message.date < self.min_date

    def write_json(self, messages: list[Message], write_dir: str) -> None:
        """Deduplicate, order chronologically, patch, and write *messages* to a JSON file.

        The output file is placed at ``{write_dir}/{self.name}.json``.
        Post-processing steps applied in order:

        1. **Unique + reverse** — deduplicate by ``message_id`` (batches
           from successive page snapshots overlap) and reverse to chronological
           order (oldest first).
        2. **Dump** — convert each ``Message`` to a plain dict via
           ``model_dump()``.
        3. **Patch** — backfill missing sender info on continuation messages.

        Parameters
        ----------
        messages:
            Accumulated message list from the runner (unique by
            ``message_id``, newest-first).  ``_unique_reversed`` still
            deduplicates defensively before writing.
        write_dir:
            Directory in which to write the output file.  Created by the
            caller before this method is invoked.
        """
        # tools.Pipeline chains post-processing: dedupe+reverse → dict dump → patch.
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
        """Extract and concatenate all Lexical text spans in *elem*."""
        tags = elem.select(MSG_CONTENT_SEL)
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

    def _collect_attachments(self, elem: Tag) -> list[Attachment] | None:
        """Extract file and media attachments from *elem*.

        Returns ``None`` when the message carries no attachments.
        """
        blocks: list[Tag] = elem.find_all("div", class_=MSG_ATTACHMENT_CLS)
        if not blocks:
            return None
        attachments = []
        for block in blocks:
            name_tag = block.find(class_=ATTACH_NAME_CLS)
            size_tag = block.find(class_=ATTACH_SIZE_CLS)
            name = ParsingTask._get_text(name_tag) if name_tag else ""
            if not name:
                # Skip attachment blocks that have no parseable filename
                # (e.g. inline images rendered without a filename cell).
                continue
            attachments.append(Attachment(
                name=name,
                size=ParsingTask._get_text(size_tag) if size_tag else None,
            ))
        return attachments or None

    # ── Static post-processing helpers ────────────────────────────────────────

    @staticmethod
    def _get_text(elem: Tag) -> str:
        """Return cleaned plain text extracted from a BeautifulSoup element."""
        return cleaner(elem.get_text(strip=True, separator=" "))

    @staticmethod
    def _unique_reversed(messages: list[Message]) -> list[Message]:
        """Deduplicate and reverse a message list.

        Successive page snapshots overlap, so the accumulated list can
        contain the same message multiple times.  ``dict.fromkeys``
        preserves insertion order while dropping duplicates (relies on
        ``Message.__hash__`` and ``Message.__eq__``).

        After deduplication the list is reversed to produce chronological
        (oldest-first) order.  Truthy-falsy filtering (``filter(bool, …)``)
        removes textless, attachment-free messages.
        """
        # dict.fromkeys keeps the first snapshot seen while scrolling; reversed()
        # then yields chronological oldest-first order for JSON output.
        return list(filter(bool, reversed(dict.fromkeys(messages))))

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
