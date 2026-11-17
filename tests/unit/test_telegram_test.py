import asyncio
from types import SimpleNamespace

from forexai.adapters.telegram.test_message import (
    TELEGRAM_TEST_MESSAGE,
    send_telegram_test_message,
)
from forexai.cli import build_parser


class _FakeBot:
    instances = []

    def __init__(self, *, token: str) -> None:
        self.token = token
        self.calls = []
        self.__class__.instances.append(self)

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=12345)


def test_telegram_test_sends_exactly_one_isolated_test_message() -> None:
    _FakeBot.instances.clear()
    message_id = asyncio.run(
        send_telegram_test_message(
            token="test-token",
            chat_id="987654",
            bot_factory=_FakeBot,
        )
    )
    assert message_id == "12345"
    assert len(_FakeBot.instances) == 1
    bot = _FakeBot.instances[0]
    assert bot.token == "test-token"
    assert bot.calls == [
        {
            "chat_id": "987654",
            "text": TELEGRAM_TEST_MESSAGE,
            "disable_web_page_preview": True,
        }
    ]
    assert "TEST" in TELEGRAM_TEST_MESSAGE
    assert "signal" in TELEGRAM_TEST_MESSAGE.lower()
    assert "order" in TELEGRAM_TEST_MESSAGE.lower()


def test_cli_exposes_telegram_test_command() -> None:
    args = build_parser().parse_args(["telegram-test"])
    assert args.command == "telegram-test"


def test_cli_exposes_time_check_command() -> None:
    args = build_parser().parse_args(["time-check"])
    assert args.command == "time-check"


def test_cli_exposes_market_check_command() -> None:
    args = build_parser().parse_args(["market-check"])
    assert args.command == "market-check"
