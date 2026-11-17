from __future__ import annotations

from html import escape
import re
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from forexai.domain.models import Direction, Signal


class TelegramSignalNotifier:
    is_dry_run = False

    def __init__(
        self,
        token: str,
        chat_id: str,
        display_timezone: str = "Asia/Tehran",
        *,
        bot: Bot | None = None,
        semi_execution_enabled: bool = False,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID are required")
        self._bot = bot or Bot(token=token)
        self._chat_id = chat_id
        self._display_timezone = display_timezone
        self._semi_execution_enabled = semi_execution_enabled

    async def send(self, signal: Signal) -> str:
        message = await self._bot.send_message(
            chat_id=self._chat_id,
            text=format_signal(signal, self._display_timezone),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=(
                build_signal_keyboard(signal.signal_id)
                if self._semi_execution_enabled
                else None
            ),
        )
        return str(message.message_id)


def build_signal_keyboard(signal_id: str) -> InlineKeyboardMarkup:
    execute_data = f"exec:{signal_id}"
    check_data = f"check:{signal_id}"
    if len(execute_data.encode("utf-8")) > 64 or len(check_data.encode("utf-8")) > 64:
        raise ValueError("signal_id is too long for Telegram callback_data")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ اجرای معامله", callback_data=execute_data)],
            [InlineKeyboardButton("🔄 بررسی ورود الآن", callback_data=check_data)],
        ]
    )


def build_terminal_keyboard(signal_id: str, label: str) -> InlineKeyboardMarkup:
    callback_data = f"state:{signal_id}"
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("signal_id is too long for Telegram callback_data")
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=callback_data)]]
    )


class ConsoleSignalNotifier:
    is_dry_run = True

    def __init__(self, display_timezone: str = "Asia/Tehran") -> None:
        self._display_timezone = display_timezone

    async def send(self, signal: Signal) -> str:
        print(
            re.sub(
                r"</?[^>]+>", "", format_signal(signal, self._display_timezone)
            )
        )
        return f"console:{signal.signal_id}"


def format_signal(signal: Signal, display_timezone: str = "Asia/Tehran") -> str:
    digits = int(signal.metadata.get("digits", 5))
    direction = "خرید" if signal.direction is Direction.BUY else "فروش"
    quote_side = "Ask" if signal.direction is Direction.BUY else "Bid"
    reasons = "\n".join(
        f"• {escape(_persian_reason(reason))}" for reason in signal.reasons
    )
    event_line = (
        f"\n<b>هشدار رویداد:</b> {escape(signal.nearby_event_warning)}"
        if signal.nearby_event_warning
        else ""
    )
    max_deviation = float(signal.metadata.get("maximum_entry_deviation", 0))
    created_local, created_utc = _format_both_times(
        signal.signal_created_at_utc, display_timezone
    )
    trigger_local, trigger_utc = _format_both_times(
        signal.trigger_bar_time_utc, display_timezone
    )
    sizing = _format_position_size(signal)
    execution = _format_auto_execution(signal, digits)
    return (
        f"<b>سیگنال تحلیلی ForexAI | {escape(signal.instrument)}</b>\n"
        f"<b>جهت:</b> {direction} ({signal.direction.value})\n"
        f"<b>امتیاز همگرایی:</b> {signal.confidence_score}/100\n"
        f"<b>تایم‌فریم Setup:</b> {escape(signal.setup_timeframe)}\n\n"
        f"<b>اطلاعات ورود</b>\n"
        f"<b>قیمت فعلی بازار ({quote_side}):</b> "
        f"{signal.current_market_price:.{digits}f}\n"
        f"<b>ورود پیشنهادی:</b> {signal.suggested_entry:.{digits}f}\n"
        f"<b>نوع ورود:</b> {signal.entry_type.value}\n"
        f"<b>منبع ورود:</b> {escape(_entry_source_fa(signal.entry_source))}\n"
        f"<b>انحراف ورود:</b> {signal.entry_deviation:.{digits}f} "
        f"(حداکثر {max_deviation:.{digits}f})\n"
        f"<b>حد ضرر:</b> {signal.stop_loss:.{digits}f}\n"
        f"<b>حد سود:</b> {signal.take_profit:.{digits}f}\n"
        f"<b>نسبت سود به زیان با قیمت فعلی:</b> 1:{signal.risk_reward:.2f}\n"
        f"{sizing}{execution}\n"
        f"<b>دلایل تحلیلی</b>\n{reasons}\n\n"
        f"<b>شرط ابطال:</b> {escape(_invalidation_fa(signal.invalidation))}"
        f"{event_line}\n\n"
        f"<b>زمان ایجاد سیگنال:</b> {created_local}\n"
        f"<b>زمان ایجاد UTC:</b> {created_utc}\n"
        f"<b>زمان کندل Trigger:</b> {trigger_local}\n"
        f"<b>زمان کندل Trigger در UTC:</b> {trigger_utc}\n"
        f"<b>وضعیت:</b> {_status_fa(signal.status.value)}\n"
        "<i>این امتیاز احتمال برد نیست و حجم پیشنهادی دستور معامله محسوب نمی‌شود.</i>"
    )


def _format_position_size(signal: Signal) -> str:
    position = signal.metadata.get("position_size")
    if not isinstance(position, dict):
        reason = signal.metadata.get(
            "position_size_unavailable_reason", "اطلاعات حساب MT5 در دسترس نیست"
        )
        return f"<b>حجم پیشنهادی:</b> ناموجود ({escape(str(reason))})\n"
    currency = escape(str(position["account_currency"]))
    unavailable = position.get("unavailable_reason")
    lots = float(position["suggested_volume_lots"])
    lot_text = f"{lots:g} Lot" if lots > 0 else "ناموجود"
    detail = f" ({escape(str(unavailable))})" if unavailable else ""
    return (
        "<b>مدیریت حجم (واحد MT5 = Lot)</b>\n"
        f"<b>موجودی حساب:</b> {float(position['account_balance']):,.2f} {currency}\n"
        f"<b>ریسک هر تحلیل:</b> {float(position['risk_percent']):g}%\n"
        f"<b>حداکثر زیان پیشنهادی:</b> "
        f"{float(position['maximum_loss']):,.2f} {currency}\n"
        f"<b>حجم پیشنهادی:</b> {lot_text}{detail}\n"
    )


def _format_auto_execution(signal: Signal, digits: int) -> str:
    execution = signal.metadata.get("auto_execution")
    if not isinstance(execution, dict):
        return ""
    if execution.get("status") == "EXECUTED":
        price = float(execution.get("actual_execution_price") or 0)
        volume = float(execution.get("executed_volume") or 0)
        sl = float(execution.get("sl") or signal.stop_loss)
        tp = float(execution.get("tp") or signal.take_profit)
        ticket = execution.get("position_ticket") or execution.get("order_ticket")
        return (
            "\n<b>اجرای خودکار: موفق</b>\n"
            f"<b>قیمت واقعی ورود:</b> {price:.{digits}f}\n"
            f"<b>حجم اجراشده:</b> {volume:g} Lot\n"
            f"<b>SL ثبت‌شده:</b> {sl:.{digits}f}\n"
            f"<b>TP ثبت‌شده:</b> {tp:.{digits}f}\n"
            f"<b>Order/Position Ticket:</b> {escape(str(ticket or '-'))}\n"
        )
    reason = str(execution.get("reason") or "AUTO_EXECUTION_UNKNOWN")
    return (
        "\n<b>اجرای خودکار انجام نشد</b>\n"
        f"<b>دلیل:</b> {escape(_execution_reason_fa(reason))}\n"
    )


def _execution_reason_fa(reason: str) -> str:
    return {
        "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED": "حساب فعال Demo نیست",
        "AUTO_EXECUTION_UNSUPPORTED_ENTRY_TYPE": "نوع ورود MARKET نیست",
        "AUTO_EXECUTION_ENTRY_MISSED": "قیمت از محدوده ورود خارج شده است",
        "AUTO_EXECUTION_EXISTING_POSITION": "روی Symbol یک Position باز وجود دارد",
        "AUTO_EXECUTION_MARKET_CLOSED": "بازار بسته است",
        "AUTO_EXECUTION_SESSION_CLOSING": "تا پایان Session حداکثر ۶۰ دقیقه مانده است",
        "AUTO_EXECUTION_SESSION_STATUS_UNAVAILABLE": "وضعیت Session قابل تأیید نیست",
        "AUTO_EXECUTION_STALE_TICK": "Tick بازار قدیمی است",
        "AUTO_EXECUTION_INVALID_VOLUME": "حجم MT5 معتبر نیست",
        "AUTO_EXECUTION_INVALID_STOPS": "SL/TP با محدودیت‌های Symbol سازگار نیست",
        "AUTO_EXECUTION_ORDER_CHECK_FAILED": "بررسی order_check ناموفق بود",
        "AUTO_EXECUTION_ORDER_SEND_FAILED": "ارسال سفارش ناموفق بود",
        "AUTO_EXECUTION_AMBIGUOUS_RESULT": "نتیجه اجرا قطعی نیست؛ ارسال مجدد انجام نمی‌شود",
        "AUTO_EXECUTION_DUPLICATE_SIGNAL": "این signal_id قبلاً پردازش شده است",
        "AUTO_EXECUTION_PREFLIGHT_FAILED": "پیش‌بررسی اجرای خودکار ناموفق بود",
    }.get(reason, reason)


def _format_both_times(value, display_timezone: str) -> tuple[str, str]:
    local = value.astimezone(ZoneInfo(display_timezone))
    return (
        local.strftime(f"%Y-%m-%d %H:%M:%S %Z ({display_timezone})"),
        value.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def _status_fa(status: str) -> str:
    return {
        "CANDIDATE": "کاندید ارسال",
        "SENT": "ارسال‌شده",
        "REJECTED": "ردشده",
        "BLOCKED_NEWS": "مسدود به‌دلیل خبر",
        "NO_TRADE": "بدون سیگنال",
        "STALE_ENTRY": "قیمت ورود قدیمی",
        "ENTRY_MISSED": "فرصت ورود ازدست‌رفته",
    }.get(status, status)


def _persian_reason(reason: str) -> str:
    replacements = (
        ("EMA trend context aligns", "جهت روند EMA همسو است"),
        ("Base-versus-quote relative strength aligns", "قدرت نسبی ارز پایه و مظنه همسو است"),
        ("ADX trend and normal-volatility regime align", "روند ADX با رژیم نوسان عادی همسو است"),
    )
    for source, translated in replacements:
        if reason == source:
            return translated
    if reason.startswith("RSI momentum aligns"):
        return reason.replace("RSI momentum aligns", "مومنتوم RSI همسو است")
    if reason.startswith("Aligned ") and reason.endswith(" liquidity sweep"):
        side = reason.removeprefix("Aligned ").removesuffix(" liquidity sweep")
        return f"Liquidity Sweep همسو در سمت {side}"
    if " structure aligned " in reason:
        return reason.replace(" structure aligned ", " ساختار همسو با جهت ")
    if " confirmed on closed bar" in reason:
        return reason.replace(" confirmed on closed bar", " روی کندل بسته تأیید شد")
    if reason.startswith("Strategy risk/reward"):
        return reason.replace("Strategy risk/reward", "نسبت سود به زیان Strategy")
    if reason.startswith("ADX trend is present"):
        return reason.replace("ADX trend is present", "روند ADX برقرار است")
    return reason


def _entry_source_fa(source: str) -> str:
    match = re.fullmatch(r"closed (\w+) trigger-bar close", source)
    if match:
        return f"قیمت بسته‌شدن کندل Trigger در تایم‌فریم {match.group(1)}"
    return source


def _invalidation_fa(value: str) -> str:
    match = re.fullmatch(
        r"Closed (\w+) candle beyond structural stop ([0-9.]+)", value
    )
    if match:
        return (
            f"بسته‌شدن کندل {match.group(1)} آن‌سوی حد ضرر ساختاری "
            f"{match.group(2)}"
        )
    return value
