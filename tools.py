import re
from typing import Any
from zoneinfo import ZoneInfo

import emoji


SBERCHAT_BASE_URL = "https://sberchat.sberbank.ru/"
group_url_pattern = re.compile(
    fr"{SBERCHAT_BASE_URL}#/chat/group\d+"
)
time_pattern = re.compile(r"\d\d:\d\d(.+)")
moscow_timezone = ZoneInfo("Europe/Moscow")


def remove_time_prefix(string: str) -> str:
    """Remove time prefix from a string. """
    return time_pattern.sub(r"\g<1>", string)


def remove_emoji(string: str) -> str:
    """Remove emoji from a string. """
    return emoji.replace_emoji(string)


def remove_redundant_whitespaces(string: str) -> str:
    """Remove redundant whitespaces from a string. """
    return " ".join(string.split())


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
    remove_redundant_whitespaces
)
