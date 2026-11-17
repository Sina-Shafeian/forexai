from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram import Bot


TELEGRAM_TEST_MESSAGE = (
    "🧪 TEST — اتصال تلگرام ForexAI با موفقیت برقرار است.\n"
    "این فقط تست تنظیمات است؛ هیچ Signal واقعی، چرخه سیگنال یا Order ایجاد نشد."
)


async def send_telegram_test_message(
    *,
    token: str,
    chat_id: str,
    bot_factory: Callable[..., Any] = Bot,
) -> str:
    """Send one isolated setup message without touching signal state."""
    if not token or not chat_id:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured in .env"
        )
    bot = bot_factory(token=token)
    message = await bot.send_message(
        chat_id=chat_id,
        text=TELEGRAM_TEST_MESSAGE,
        disable_web_page_preview=True,
    )
    return str(message.message_id)
