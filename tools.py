import re
from typing import Any
from zoneinfo import ZoneInfo

import emoji

from settings import SBERCHAT_BASE_URL


moscow_timezone = ZoneInfo("Europe/Moscow")

group_url_pattern = re.compile(
    fr"{SBERCHAT_BASE_URL}#/chat/group\d+"
)

time_pattern = re.compile(r"\d\d:\d\d(.+)")


def remove_time_prefix(text: str) -> str:
    """Remove time prefix. """
    return time_pattern.sub(r"\g<1>", text)


def remove_emoji(text: str) -> str:
    """Remove emoji. """
    return emoji.replace_emoji(text)


def remove_redundant_spaces(text: str) -> str:
    """Remove redundant whitespaces. """
    return " ".join(text.split())


class Pipeline:
    """
    pipe = Pipeline(f, g, h) : pipe(v) = h(g(f(v)))
    """

    def __init__(self, *funcs) -> None:
        self.funcs = funcs

    def __call__(self, arg: Any) -> Any:
        for func in self.funcs:
            arg = func(arg)
        return arg


cleaner = Pipeline(
    remove_time_prefix,
    remove_emoji,
    remove_redundant_spaces
)
