import itertools
import json
from datetime import datetime
from typing import Self

from bs4 import BeautifulSoup
from pydantic import (
    BaseModel,
    ConfigDict,
    AwareDatetime,
    field_validator,
    model_validator,
    model_serializer
)

from settings import *
from tools import (
    SBERCHAT_BASE_URL,
    Pipeline,
    cleaner,
    group_url_pattern,
    moscow_timezone
)


class BaseMessage(BaseModel):
    """SberChat message base model. """

    sender_name: str | None
    content: str


class Quote(BaseMessage):
    """Quoted message. """

    @model_validator(mode="after")
    def remove_sender_from_content(self) -> Self:
        self.content = (
            self.content.removeprefix(self.sender_name)
            .lstrip()
        )
        return self


msg_dump = dict[str, str | datetime | list[Quote] | None]


class Message(BaseMessage):
    """SberChat message. """

    model_config = ConfigDict(frozen=True)

    message_id: str
    date: AwareDatetime
    sender_url: str | None
    quotes: list[Quote] | None

    @field_validator("date", mode="before")
    @classmethod
    def to_datetime(cls, value: str) -> datetime:
        # remove milliseconds from a timestamp
        since_epoch = int(value) // 1000
        dt = datetime.fromtimestamp(since_epoch)
        return dt.replace(tzinfo=moscow_timezone)

    @model_serializer(when_used="always")
    def rearrange_fields(self) -> msg_dump:
        # exclude `message_id`
        return {
            "date": self.date,
            "sender_url": self.sender_url,
            "sender_name": self.sender_name,
            "quotes": self.quotes,
            "content": self.content
        }

    def __hash__(self) -> int:
        return hash(self.message_id)

    def __eq__(self, other) -> bool:
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.message_id == other.message_id

    def __bool__(self) -> bool:
        return bool(self.content)

    @classmethod
    def patch(cls, this: msg_dump, that: msg_dump) -> None:
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

    def collect_messages(self, soup: BeautifulSoup) -> list[Message]:
        divs = soup.find_all("div", class_=MSG_WRAP_CLS)
        messages = [
            Message(
                message_id=div["data-message-id"],
                date=div["data-message-date"],
                sender_url=self._get_sender_url(div),
                sender_name=self._get_sender_name(
                    div, class_=MSG_SENDER_NAME_CLS
                ),
                quotes=self._collect_quotes(div),
                content=self._get_message_content(div)
            )
            for div in divs
        ]
        return list(reversed(messages))

    def is_done(self, message: Message) -> bool:
        return message.date < self.min_date

    def write_json(self, messages: list[Message]) -> None:
        """Write messages to JSON. """
        # dump messages before patching as Message is a frozen class
        postprocessor = Pipeline(
            self.__class__._unique_reversed,
            lambda messages: [m.model_dump() for m in messages],
            self.__class__._patch
        )
        target_file = f"{WRITE_DIR.rstrip("/")}/{self.name}.json"

        with open(target_file, "w", encoding="utf-8") as fp:
            json.dump(
                obj=postprocessor(messages),
                fp=fp,
                default=str,  # required for the datetime objects
                ensure_ascii=False,
                indent=4
            )

    def _get_sender_url(self, elem) -> str | None:
        tag = elem.select_one(MSG_SENDER_URL_SEL)
        if not tag:
            return None
        return SBERCHAT_BASE_URL + tag["href"]

    def _get_sender_name(self, elem, class_) -> str | None:
        div = elem.find("div", class_=class_)
        if not div:
            return None
        return self.__class__._get_text(div)

    def _get_message_content(self, elem) -> str:
        tags = elem.select(MSG_CONTENT_SEL)
        return self._get_all_texts(tags)

    def _collect_quotes(self, elem) -> list[Quote] | None:
        divs = elem.find_all("div", class_=QUOTE_WRAP_CLS)
        if not divs:
            return None
        return [
            Quote(
                sender_name=self._get_sender_name(
                    div, class_=QUOTE_SENDER_NAME_CLS
                ),
                content=self.__class__._get_text(div)
            )
            for div in divs
        ]

    def _get_all_texts(self, elems) -> str:
        return " ".join(self.__class__._get_text(e) for e in elems)

    @staticmethod
    def _get_text(elem) -> str:
        return cleaner(elem.get_text(strip=True, separator=" "))

    @staticmethod
    def _unique_reversed(messages: list[Message]) -> list[Message]:
        """
        Keep unique messages while preserving the original order,
        i.e. drop duplicates as soups can potentially overlap (?).
        Reverse the result. Eliminate textless messages.
        """
        return list(filter(bool, reversed(dict.fromkeys(messages))))

    @staticmethod
    def _patch(messages: list[msg_dump]) -> list[msg_dump]:
        """Patch a message that lacks sender information. """
        # the first message in a block is always fully tagged
        for previous, current in itertools.pairwise(messages):
            Message.patch(current, previous)
        return messages
