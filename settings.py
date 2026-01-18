# selectors and settings

MESSAGE_SELECTOR = (
    ".MessageRow-MessageRowWrapper__isBlockMessage"
)

SENDER_URL_SELECTOR = (
    ".MessageTitle-LinkToAuthor__cls2"
    ".MessageTitle-LinkToAuthor__cls1"
    ".UnstyledLink__cls2"
    ".UnstyledLink__cls1"
)

SENDER_NAME_SELECTOR = (
    ".CustomStatusIcon-ChatHeaderStatusTitle__cls2"
    ".CustomStatusIcon-ChatHeaderStatusTitle__cls1"
)

QUOTE_SELECTOR = (
    ".Reply-MessageTextContent__cls2"
    ".Reply-MessageTextContent__cls1"
)

CONTENT_SELECTOR = (
    ".BlockMessageStyleComponent-BlockMessageText__cls2"
    ".BlockMessageStyleComponent-BlockMessageText__cls1"
)

SBERCHAT_BASE_URL = "https://sberchat.sberbank.ru/"

SLEEP_TIME = 60
WRITE_DIR = "./results"

TRUSTED_DEVICE: int | bool = 0
SBERBROWSER_BINARY = "/opt/Sberbrowser/sberbrowser/sberbrowser"
SBERBROWSER_DRIVER = "/opt/sberdriver/sberdriver"
