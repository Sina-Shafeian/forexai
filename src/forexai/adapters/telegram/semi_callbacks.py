from __future__ import annotations

import asyncio
from html import escape
import logging
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from forexai.adapters.telegram.notifier import build_terminal_keyboard
from forexai.analysis.semi_execution import SemiExecutionCoordinator
from forexai.domain.execution import ExecutionStatus, SemiExecutionResult
from forexai.domain.models import Direction


LOGGER = logging.getLogger(__name__)
CALLBACK_PATTERN = re.compile(
    r"^(?P<action>exec|check|state):(?P<signal_id>[A-Za-z0-9_-]{1,40})$"
)


class SemiExecutionCallbackHandler:
    def __init__(
        self,
        coordinator: SemiExecutionCoordinator,
        *,
        allowed_chat_id: str,
        allowed_user_id: int | None,
    ) -> None:
        self._coordinator = coordinator
        self._allowed_chat_id = str(allowed_chat_id)
        self._allowed_user_id = allowed_user_id

    async def handle(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        match = CALLBACK_PATTERN.fullmatch(str(query.data or ""))
        if match is None:
            await query.answer("درخواست نامعتبر است.", show_alert=True)
            return
        if not self._authorized(query):
            LOGGER.warning("Unauthorized semi-execution callback rejected")
            await query.answer("اجازه اجرای این عملیات را ندارید.", show_alert=True)
            return
        action = match.group("action")
        signal_id = match.group("signal_id")
        if action == "state":
            await query.answer("این دکمه دیگر قابل اجرا نیست.", show_alert=True)
            return

        await query.answer("در حال اعتبارسنجی اطلاعات زنده MT5…")
        operation = (
            self._coordinator.execute
            if action == "exec"
            else self._coordinator.recheck
        )
        result = await asyncio.to_thread(operation, signal_id)
        LOGGER.info(
            "Semi callback action=%s signal_id=%s state=%s reason=%s",
            action,
            signal_id,
            result.state.value if result.state else "NONE",
            result.reason,
        )
        if query.message is not None:
            await query.message.reply_text(
                format_semi_result(result),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        terminal_label = _terminal_label(result)
        if terminal_label is not None:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=build_terminal_keyboard(signal_id, terminal_label)
                )
            except TelegramError as exc:
                LOGGER.warning(
                    "Could not update semi-execution button state signal_id=%s: %s",
                    signal_id,
                    exc,
                )

    def _authorized(self, query) -> bool:
        message = query.message
        user = query.from_user
        if message is None or user is None or self._allowed_user_id is None:
            return False
        return (
            str(message.chat.id) == self._allowed_chat_id
            and int(user.id) == self._allowed_user_id
        )


def resolve_allowed_user_id(
    configured_chat_id: str, explicit_user_id: int | None
) -> int | None:
    if explicit_user_id is not None:
        return explicit_user_id
    try:
        chat_id = int(configured_chat_id)
    except (TypeError, ValueError):
        return None
    return chat_id if chat_id > 0 else None


def format_semi_result(result: SemiExecutionResult) -> str:
    if result.action == "recheck":
        heading = "✅ ورود هنوز معتبر است" if result.valid else "❌ ورود دیگر معتبر نیست"
    elif result.succeeded:
        heading = "✅ معامله با موفقیت اجرا شد"
    elif result.reason == "ALREADY_EXECUTED":
        heading = "ℹ️ این سیگنال قبلاً اجرا شده است"
    else:
        heading = "❌ معامله اجرا نشد"
    lines = [f"<b>{heading}</b>"]
    if result.symbol:
        lines.append(f"<b>نماد:</b> {escape(result.symbol)}")
    if result.direction:
        direction = "خرید" if result.direction is Direction.BUY else "فروش"
        lines.append(f"<b>جهت:</b> {direction} ({result.direction.value})")
    lines.append(f"<b>دلیل:</b> {escape(_reason_fa(result.reason))}")
    digits = result.digits
    if result.current_price is not None:
        lines.append(f"<b>قیمت فعلی/واقعی:</b> {result.current_price:.{digits}f}")
    if result.suggested_entry is not None:
        lines.append(f"<b>ورود پیشنهادی:</b> {result.suggested_entry:.{digits}f}")
    if result.maximum_entry_deviation is not None:
        lines.append(
            f"<b>حداکثر انحراف مجاز:</b> {result.maximum_entry_deviation:.{digits}f}"
        )
    if result.original_volume is not None:
        lines.append(f"<b>حجم هنگام Signal:</b> {result.original_volume:g} Lot")
    if result.final_volume is not None:
        lines.append(f"<b>حجم نهایی:</b> {result.final_volume:g} Lot")
    if result.stop_loss is not None:
        lines.append(f"<b>حد ضرر:</b> {result.stop_loss:.{digits}f}")
    if result.take_profit is not None:
        lines.append(f"<b>حد سود:</b> {result.take_profit:.{digits}f}")
    ticket = result.position_ticket or result.order_ticket
    if ticket:
        lines.append(f"<b>Position Ticket:</b> {ticket}")
    lines.append(
        f"<b>زمان بررسی UTC:</b> {result.occurred_at_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    return "\n".join(lines)


def _terminal_label(result: SemiExecutionResult) -> str | None:
    if result.state is ExecutionStatus.EXECUTED:
        return "✅ اجرا شد"
    if result.state is ExecutionStatus.AMBIGUOUS:
        return "⚠️ نتیجه نامشخص"
    if result.state is ExecutionStatus.EXPIRED or result.reason in {
        "SIGNAL_EXPIRED",
        "ENTRY_MISSED",
    }:
        return "❌ منقضی"
    if result.action == "execute" and result.state is ExecutionStatus.FAILED:
        return "❌ قابل اجرا نیست"
    return None


def _reason_fa(reason: str) -> str:
    return {
        "ENTRY_VALID": "قیمت و شرایط ورود همچنان معتبر است",
        "SIGNAL_EXPIRED": "سیگنال دیگر قابل اجرا نیست",
        "ENTRY_MISSED": "قیمت از محدوده ورود معتبر خارج شده است",
        "EXISTING_POSITION_ON_SYMBOL": "Position دیگری روی این Symbol باز است",
        "SUPPRESSED_MARKET_CLOSED": "بازار بسته است",
        "SUPPRESSED_SESSION_CLOSING": "کمتر یا مساوی ۶۰ دقیقه تا پایان Session مانده است",
        "SESSION_STATUS_UNAVAILABLE": "وضعیت Session قابل تأیید نیست",
        "STALE_MARKET_DATA": "Tick بازار تازه نیست",
        "UNSUPPORTED_ENTRY_TYPE": "نسخه فعلی فقط MARKET را پشتیبانی می‌کند",
        "INVALID_STOPS": "حد ضرر یا حد سود با محدودیت Broker سازگار نیست",
        "INVALID_VOLUME": "حجم پیشنهادی در MT5 معتبر نیست",
        "ORDER_CHECK_FAILED": "حجم، Margin یا request توسط MT5 تأیید نشد",
        "ORDER_SEND_FAILED": "سرور MT5 سفارش را نپذیرفت",
        "EXECUTION_RESULT_AMBIGUOUS": "نتیجه اجرا قطعی نیست؛ ارسال مجدد انجام نمی‌شود",
        "SEMI_EXECUTION_REAL_ACCOUNT_BLOCKED": "حساب فعال Demo نیست",
        "TRADING_PERMISSION_UNAVAILABLE": "مجوز معامله در MT5 فعال نیست",
        "ALREADY_EXECUTED": "این signal_id قبلاً اجرا شده است",
        "EXECUTION_IN_PROGRESS": "اجرای همین signal_id هم‌اکنون در حال پردازش است",
        "EXECUTION_ALREADY_PROCESSED": "این signal_id قبلاً پردازش شده است",
        "SIGNAL_NOT_FOUND": "Signal در SQLite پیدا نشد",
        "SEMI_EXECUTION_DISABLED": "Semi-Automatic Execution غیرفعال است",
        "FULL_AUTO_MUST_BE_DISABLED": "Full Auto باید غیرفعال باقی بماند",
        "MT5_DISCONNECTED": "اتصال MT5 برقرار نیست",
        "RECHECK_UNAVAILABLE": "اعتبارسنجی زنده در دسترس نیست",
    }.get(reason, reason)
