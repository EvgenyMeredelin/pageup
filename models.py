import itertools
import json
from datetime import datetime
from typing import Annotated

from bs4 import BeautifulSoup
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator
)

from settings import *
from tools import (
    Pipeline,
    cleaner,
    group_url_pattern,
    moscow_timezone
)


message_dump = dict[str, str | None]


class Message(BaseModel):
    """SberChat message model. """

    model_config = ConfigDict(frozen=True)

    message_id: Annotated[str, Field(exclude=True)]
    date: AwareDatetime
    sender_url: str | None
    sender_name: str | None
    quote: str | None
    content: str

    @field_validator("date", mode="before")
    @classmethod
    def to_datetime(cls, value: str) -> datetime:
        # remove milliseconds from a timestamp
        since_epoch = int(value) // 1000
        dt = datetime.fromtimestamp(since_epoch)
        return dt.replace(tzinfo=moscow_timezone)

    def __hash__(self) -> int:
        return hash(self.message_id)

    def __eq__(self, other) -> bool:
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.message_id == other.message_id

    def __bool__(self) -> bool:
        return bool(self.content)

    @classmethod
    def patch(cls, this: message_dump, that: message_dump) -> None:
        """Patch a message that lacks sender information. """
        if not this["sender_url"] and not this["sender_name"]:
            this["sender_url"] = that["sender_url"]
            this["sender_name"] = that["sender_name"]
        return None


class ParsingTask(BaseModel):
    """A task to collect messages from a certain SberChat group. """

    group_url: str
    min_date: AwareDatetime
    name: str

    @field_validator("group_url", mode="after")
    @classmethod
    def validate_group_url(cls, value: str) -> str:
        if not group_url_pattern.fullmatch(value):
            raise ValueError("`group_url` must be a SberChat group")
        return value

    def is_done(self, message: Message) -> bool:
        """Whether a task is done and thus should be terminated. """
        return message.date < self.min_date

    def collect_messages(self, soup: BeautifulSoup) -> list[Message]:
        """Collect messages from a parsed soup. """
        messages = []

        for elem in soup.select(MESSAGE_SELECTOR):
            sender_url = sender_name = quote = None

            if tag := elem.select_one(SENDER_URL_SELECTOR):
                sender_url = SBERCHAT_BASE_URL + tag["href"]

            if tag := elem.select_one(SENDER_NAME_SELECTOR):
                sender_name = tag.get_text(strip=True)

            if tags := elem.select(QUOTE_SELECTOR):
                quote = self.__class__._get_text(tags, sep=" >>> ")

            tags = elem.select(CONTENT_SELECTOR)
            content = self.__class__._get_text(tags, sep=" ")

            message = Message(
                message_id=elem["data-message-id"],
                date=elem["data-message-date"],
                sender_url=sender_url,
                sender_name=sender_name,
                quote=quote,
                content=content
            )
            messages.append(message)

        return list(reversed(messages))

    @staticmethod
    def _get_text(tags, sep: str) -> str:
        """Get child texts of the tags cleaned and concatenated. """
        return sep.join(
            cleaner(tag.get_text(strip=True, separator=sep))
            for tag in tags
        )

    @staticmethod
    def _unique_reversed(messages: list[Message]) -> list[Message]:
        """
        Keep unique messages while preserving the original order
        (i.e. drop duplicates as soups can potentially overlap).
        Reverse the result. Eliminate textless messages.
        """
        return list(filter(bool, reversed(dict.fromkeys(messages))))

    @staticmethod
    def _patch(messages: list[message_dump]) -> list[message_dump]:
        """Patch a message that lacks sender information. """
        # the first message in a block is always fully tagged
        for previous, current in itertools.pairwise(messages):
            Message.patch(current, previous)
        return messages

    def write_json(self, messages: list[Message]) -> None:
        """Write messages to JSON. """
        target_file = f"{WRITE_DIR.rstrip("/")}/{self.name}.json"

        # dump messages before patching as Message is a frozen class
        postprocessor = Pipeline(
            self.__class__._unique_reversed,
            lambda messages: [m.model_dump() for m in messages],
            self.__class__._patch
        )

        with open(target_file, "w", encoding="utf-8") as fp:
            json.dump(
                obj=postprocessor(messages),
                fp=fp,
                default=str,  # required for the datetime objects
                ensure_ascii=False,
                indent=4
            )
