import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from models import ParsingTask
from settings import *
from tools import moscow_timezone


if __name__ == "__main__":
    # e.g. sigma device
    if TRUSTED_DEVICE:
        options = Options()
        options.binary_location = SBERBROWSER_BINARY
        options.add_argument("--disable-features=SberAuth")
        options.add_argument("--disable-features=SberSync")
        service = Service(SBERBROWSER_DRIVER)
        driver = Chrome(options, service)
    # personal device: SberChat history limited to one week
    else:
        driver = Chrome()

    actions = ActionChains(driver)

    task = ParsingTask(
        name="SberOS",
        group_url="https://sberchat.sberbank.ru/#/chat/group1800075463",
        min_date=datetime(2025, 9, 1, tzinfo=moscow_timezone)
    )
    task_model_dump = task.model_dump_json(ensure_ascii=False, indent=4)

    Path(WRITE_DIR).mkdir(parents=True, exist_ok=True)
    driver.get(task.group_url)
    messages = []

    print(f"Selected mode: {TRUSTED_DEVICE=}")
    print("Task parameters:", task_model_dump, sep="\n")

    # SLEEP_TIME required to confirm personal certificate, enter OTP password,
    # scroll the chat down to the most recent message, and focus cursor inside.
    pad_width = len(str(SLEEP_TIME))

    for sec in range(SLEEP_TIME, 0, -1):
        print(f"Seconds to start: {sec:0{pad_width}}", end="\r", flush=True)
        time.sleep(1)

    print("Seconds to start: Parsing started...")

    try:
        while True:
            html = driver.page_source
            soup = BeautifulSoup(html, "html.parser")
            new_messages = task.collect_messages(soup)

            if task.is_done(new_messages[-1]):
                print(f"Messages collected: {len(messages)}")
                task.write_json(messages)
                break

            messages.extend(new_messages)
            actions.send_keys(Keys.PAGE_UP * 20)
            actions.perform()
            time.sleep(1)

    # since min_date may be unreachable (e.g. chat created later than expected)
    # we need a manual stopper that takes care about the collected messages
    except KeyboardInterrupt:
        print(f"Messages collected: {len(messages)}")
        task.write_json(messages)
