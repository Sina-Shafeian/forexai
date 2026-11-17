from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram.ext import Application, CallbackQueryHandler

from forexai.adapters.mt5.client import MT5ReadOnlyClient
from forexai.adapters.macro.factory import create_calendar_provider
from forexai.adapters.telegram.notifier import (
    ConsoleSignalNotifier,
    TelegramSignalNotifier,
)
from forexai.adapters.telegram.semi_callbacks import (
    CALLBACK_PATTERN,
    SemiExecutionCallbackHandler,
    resolve_allowed_user_id,
)
from forexai.adapters.storage.sqlite_store import SQLiteDecisionStore
from forexai.adapters.telegram.test_message import send_telegram_test_message
from forexai.application.orchestrator import ScanOrchestrator
from forexai.analysis.dispatch_guards import evaluate_market_session_guard
from forexai.analysis.semi_execution import SemiExecutionCoordinator
from forexai.config import Settings, load_settings
from forexai.domain.models import DecisionStatus
from forexai.ports.economic_calendar import CalendarUnavailableError


class SecretRedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_secrets(rendered, self._secrets)


def redact_secrets(value: str, secrets: list[str]) -> str:
    rendered = value
    for secret in secrets:
        if secret:
            rendered = rendered.replace(secret, "<redacted>")
    return re.sub(
            r"(?i)(?:https?://api\.telegram\.org/)?bot\d+:[A-Za-z0-9_-]+",
            "bot<redacted>",
            rendered,
        )


def _configure_console_encoding() -> None:
    """Keep Persian console output usable on legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _configure_logging(settings: Settings) -> None:
    log_path = settings.project_root / "logs/forexai.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = SecretRedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        [
            settings.secrets.telegram_bot_token or "",
            settings.secrets.fmp_api_key or "",
        ],
    )
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.app.log_level.upper(), logging.INFO))
    for handler in handlers:
        root.addHandler(handler)
    for noisy_logger in ("httpx", "httpcore", "telegram"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def _build_mt5_client(
    settings: Settings, *, require_demo_account: bool | None = None
) -> MT5ReadOnlyClient:
    return MT5ReadOnlyClient(
        settings.mt5.terminal_path,
        require_demo_account=(
            settings.mt5.require_demo_account
            if require_demo_account is None
            else require_demo_account
        ),
        timeout_ms=settings.mt5.initialize_timeout_ms,
        minimum_history_bars=settings.strategy.minimum_history_bars,
        broker_clock_state_path=settings.mt5.broker_clock_state_path,
        offset_round_minutes=settings.mt5.offset_round_minutes,
        maximum_offset_hours=settings.mt5.maximum_offset_hours,
        maximum_tick_age_seconds=settings.mt5.maximum_tick_age_seconds,
        future_tolerance_seconds=settings.mt5.future_tolerance_seconds,
        session_export_relative_path=settings.market_session.export_relative_path,
        session_export_max_age_minutes=(
            settings.market_session.export_max_age_minutes
        ),
    )


def _build_notifier(settings: Settings, console: bool):
    if console or not settings.telegram.enabled:
        return ConsoleSignalNotifier(settings.telegram.display_timezone)
    token = settings.secrets.telegram_bot_token
    chat_id = settings.secrets.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError(
            "Telegram is enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing. "
            "Configure .env or use --console."
        )
    return TelegramSignalNotifier(
        token,
        chat_id,
        settings.telegram.display_timezone,
        semi_execution_enabled=settings.semi_execution.enabled,
    )


def check_environment(settings: Settings) -> int:
    print("Configuration: OK")
    print(f"Project: {settings.project_root}")
    print(f"Instruments: {', '.join(i.canonical for i in settings.enabled_instruments)}")
    print(
        "Telegram: "
        + (
            "configured"
            if settings.secrets.telegram_bot_token and settings.secrets.telegram_chat_id
            else "not configured (console mode is available)"
        )
    )
    calendar_note = f"provider={settings.calendar.provider}"
    if settings.calendar.provider == "myfxbook_rss":
        calendar_note += " (no API key required)"
    elif settings.calendar.provider == "fmp":
        calendar_note += (
            " (API key configured)"
            if settings.secrets.fmp_api_key
            else " (FMP_API_KEY missing)"
        )
    calendar_note += f", fallback={settings.calendar.unavailable_behavior}"
    print(f"Economic calendar: {calendar_note}")

    client = _build_mt5_client(settings)
    with client:
        account = client.verify_demo_account()
        print(
            f"MT5 account: {account['login']} | {account['server']} | "
            f"{account['trade_mode']} | leverage 1:{account['leverage']}"
        )
        for instrument in settings.enabled_instruments:
            try:
                snapshot = client.get_symbol_snapshot(instrument)
                print(
                    f"  {instrument.canonical} -> {instrument.broker_symbol}: "
                    f"bid={snapshot.bid} ask={snapshot.ask} spread={snapshot.spread_points:.1f}pt"
                )
            except Exception as exc:
                print(f"  {instrument.canonical}: ERROR - {exc}")
    return 0


def time_check(settings: Settings) -> int:
    python_local = datetime.now().astimezone()
    python_utc = datetime.now(timezone.utc)
    display_zone = ZoneInfo(settings.telegram.display_timezone)
    display_from_utc = python_utc.astimezone(display_zone)
    instrument = settings.enabled_instruments[0]
    with _build_mt5_client(settings) as client:
        result = client.get_time_diagnostics(instrument, now_utc=python_utc)
    account = result["account"]
    print(f"Python local: {python_local.isoformat()}")
    print(f"Python UTC: {python_utc.isoformat()}")
    print(f"Configured display timezone: {settings.telegram.display_timezone}")
    print(f"UTC converted to display timezone: {display_from_utc.isoformat()}")
    print(
        f"MT5 account/server: {account['login']} | {account['server']} | "
        f"{account['trade_mode']}"
    )
    print(f"Reference symbol: {result['symbol']}")
    print(f"Raw tick epoch: {result['tick_raw_epoch']}")
    print(f"Raw tick interpreted as UTC: {result['tick_raw'].isoformat()}")
    print(f"Normalized tick UTC: {result['tick_normalized'].isoformat()}")
    print(f"Detected broker offset: {result['offset_minutes']:+d} minutes")
    print(f"Tick age: {result['tick_age_seconds']:.3f} seconds")
    for timeframe, bars in result["bars"].items():
        print(f"{timeframe} bars (raw -> normalized UTC):")
        for bar in bars:
            print(f"  {bar['raw'].isoformat()} -> {bar['normalized'].isoformat()}")
    warnings = result["warnings"]
    print(f"Warnings: {', '.join(warnings) if warnings else 'NONE'}")
    return 0


def market_check(settings: Settings) -> int:
    now_utc = datetime.now(timezone.utc)
    with _build_mt5_client(settings) as client:
        try:
            positions = client.get_open_positions()
            positions_error = None
        except Exception as exc:
            positions = None
            positions_error = str(exc)
        catalog = None
        catalog_error = None
        for instrument in settings.enabled_instruments:
            print(f"\n{instrument.canonical} ({instrument.broker_symbol})")
            try:
                snapshot = client.get_symbol_snapshot(instrument)
                if catalog is None and catalog_error is None:
                    try:
                        catalog = client.get_session_catalog(now_utc=now_utc)
                    except Exception as exc:
                        catalog_error = str(exc)
                sessions = (
                    catalog.for_symbol(instrument.broker_symbol)
                    if catalog is not None
                    else None
                )
                result = evaluate_market_session_guard(
                    broker_symbol=instrument.broker_symbol,
                    sessions=sessions,
                    now_utc=now_utc,
                    broker_offset_minutes=snapshot.broker_offset_minutes,
                    snapshot=snapshot,
                    block_before_close_minutes=(
                        settings.market_session.block_before_close_minutes
                    ),
                    stale_tick_threshold_seconds=(
                        settings.market_session.stale_tick_threshold_seconds
                    ),
                )
                if result.status is DecisionStatus.SESSION_STATUS_UNAVAILABLE:
                    market = "UNKNOWN"
                elif result.status is DecisionStatus.SUPPRESSED_MARKET_CLOSED:
                    market = "CLOSED"
                else:
                    market = "OPEN"
                print(f"Market: {market}")
                print(
                    f"Server time: {result.diagnostics.get('broker_time', '-')}"
                )
                print(
                    "Current session: "
                    f"{result.diagnostics.get('session_start', '-')} -> "
                    f"{result.diagnostics.get('session_close', '-')}"
                )
                remaining = result.diagnostics.get("minutes_remaining")
                print(
                    "Minutes until close: "
                    f"{remaining:.1f}" if isinstance(remaining, (int, float)) else
                    "Minutes until close: -"
                )
                print(
                    "Blocked before close: "
                    f"{result.status is DecisionStatus.SUPPRESSED_SESSION_CLOSING}"
                )
                print(f"Session guard: {result.reason or 'PASS'}")
                if catalog_error:
                    print(f"Session detail: {catalog_error}")
            except Exception as exc:
                print(f"Market: UNKNOWN\nSession guard: SESSION_STATUS_UNAVAILABLE")
                print(f"Session detail: {exc}")
            if positions is None:
                print(f"Position guard: POSITION_STATUS_UNAVAILABLE ({positions_error})")
            else:
                same_symbol = [
                    position
                    for position in positions
                    if position.broker_symbol.upper()
                    == instrument.broker_symbol.upper()
                ]
                if not same_symbol:
                    print("Open position: NONE")
                for position in same_symbol:
                    print(
                        f"Open position: ticket={position.ticket} "
                        f"direction={position.direction.value} "
                        f"volume={position.volume} price_open={position.price_open} "
                        f"sl={position.stop_loss} tp={position.take_profit}"
                    )
    return 0


def execution_check(settings: Settings) -> int:
    """Inspect execution readiness without calling any MT5 order operation."""
    now_utc = datetime.now(timezone.utc)
    print(f"Auto execution enabled: {settings.auto_execution.enabled}")
    print(f"Demo-only hard safety: {settings.auto_execution.demo_only}")
    print(f"Market orders only: {settings.auto_execution.market_orders_only}")
    print(f"Magic/comment: {settings.auto_execution.magic} / {settings.auto_execution.comment}")
    with _build_mt5_client(settings, require_demo_account=False) as client:
        account = client.get_account_snapshot()
        terminal = client.get_terminal_diagnostics()
        demo = account.trade_mode == "DEMO"
        allowed = (
            demo
            and account.trade_allowed
            and account.trade_expert
            and terminal["connected"]
            and terminal["trade_allowed"]
            and not terminal["tradeapi_disabled"]
        )
        print(
            f"Account: {account.login} | {account.server} | {account.trade_mode} | "
            f"balance={account.balance:.2f} equity={account.equity:.2f}"
        )
        print(f"Trading allowed by safety/permissions: {allowed}")
        print(
            "Terminal permissions: "
            f"connected={terminal['connected']} "
            f"trade_allowed={terminal['trade_allowed']} "
            f"tradeapi_disabled={terminal['tradeapi_disabled']} "
            f"account_trade_allowed={account.trade_allowed} "
            f"account_trade_expert={account.trade_expert}"
        )
        positions = client.get_open_positions()
        print(f"Open positions: {len(positions)}")
        catalog = None
        catalog_error = None
        for instrument in settings.enabled_instruments:
            snapshot = client.get_symbol_snapshot(instrument)
            if catalog is None and catalog_error is None:
                try:
                    catalog = client.get_session_catalog(now_utc=now_utc)
                except Exception as exc:
                    catalog_error = str(exc)
            sessions = (
                catalog.for_symbol(instrument.broker_symbol)
                if catalog is not None
                else None
            )
            session = evaluate_market_session_guard(
                broker_symbol=instrument.broker_symbol,
                sessions=sessions,
                now_utc=now_utc,
                broker_offset_minutes=snapshot.broker_offset_minutes,
                snapshot=snapshot,
                block_before_close_minutes=(
                    settings.market_session.block_before_close_minutes
                ),
                stale_tick_threshold_seconds=(
                    settings.market_session.stale_tick_threshold_seconds
                ),
            )
            print(
                f"{instrument.canonical}: session={session.reason or 'PASS'} "
                f"trade_mode={snapshot.trade_mode} execution_mode={snapshot.trade_execution_mode} "
                f"volume={snapshot.volume_min:g}..{snapshot.volume_max:g} "
                f"step={snapshot.volume_step:g} stops_level={snapshot.trade_stops_level}pt"
            )
        if catalog_error:
            print(f"Session catalog detail: {catalog_error}")
    print("Diagnostic only: no order was checked or sent.")
    return 0


def semi_execution_check(settings: Settings) -> int:
    """Inspect Semi Execution readiness without invoking MT5 order APIs."""
    token = settings.secrets.telegram_bot_token
    chat_id = settings.secrets.telegram_chat_id
    allowed_user_id = (
        resolve_allowed_user_id(
            chat_id or "", settings.secrets.telegram_allowed_user_id
        )
        if chat_id
        else None
    )
    print(f"Semi execution enabled: {settings.semi_execution.enabled}")
    print(f"Full auto execution enabled: {settings.auto_execution.enabled}")
    print(f"Demo-only hard safety: {settings.semi_execution.demo_only}")
    print(
        "Telegram callback listener: "
        + (
            "READY (starts with 'forexai run')"
            if token and chat_id and allowed_user_id is not None
            else "BLOCKED (token/chat/authorized user is incomplete)"
        )
    )
    store = SQLiteDecisionStore(settings.app.state_database)
    print(f"SQLite execution store: OK | states={store.execution_status_counts()}")
    now_utc = datetime.now(timezone.utc)
    with _build_mt5_client(settings, require_demo_account=False) as client:
        account = client.get_account_snapshot()
        terminal = client.get_terminal_diagnostics()
        positions = client.get_open_positions()
        print(
            f"MT5: connected={terminal['connected']} account={account.trade_mode} "
            f"terminal_trade_allowed={terminal['trade_allowed']} "
            f"account_trade_allowed={account.trade_allowed}"
        )
        print(f"Open positions: {len(positions)}")
        reference = settings.enabled_instruments[0]
        snapshot = client.get_symbol_snapshot(reference)
        try:
            catalog = client.get_session_catalog(now_utc=now_utc)
            sessions = catalog.for_symbol(reference.broker_symbol)
            session = evaluate_market_session_guard(
                broker_symbol=reference.broker_symbol,
                sessions=sessions,
                now_utc=now_utc,
                broker_offset_minutes=snapshot.broker_offset_minutes,
                snapshot=snapshot,
                block_before_close_minutes=(
                    settings.market_session.block_before_close_minutes
                ),
                stale_tick_threshold_seconds=(
                    settings.market_session.stale_tick_threshold_seconds
                ),
            )
            print(
                f"Session exporter: OK | {reference.canonical}="
                f"{session.reason or 'PASS'}"
            )
        except Exception as exc:
            print(f"Session exporter: UNAVAILABLE | {exc}")
    print("Diagnostic only: no order was checked or sent.")
    return 0


async def _scan_once(settings: Settings, console: bool) -> int:
    notifier = _build_notifier(settings, console)
    decisions = await ScanOrchestrator(settings, notifier).scan_once()
    if not decisions:
        print("No new closed trigger bars were available.")
        return 0
    for decision in decisions:
        score = (
            f" score={decision.signal.confidence_score}"
            if decision.signal is not None
            else ""
        )
        print(
            f"{decision.instrument}: {decision.status.value}{score} - "
            f"{'; '.join(decision.reasons)}"
        )
    return 0


async def _run(settings: Settings, console: bool) -> int:
    if console or not settings.telegram.enabled or not settings.semi_execution.enabled:
        notifier = _build_notifier(settings, console)
        await ScanOrchestrator(settings, notifier).run_forever()
        return 0

    token = settings.secrets.telegram_bot_token
    chat_id = settings.secrets.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError(
            "Semi Execution requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
        )
    allowed_user_id = resolve_allowed_user_id(
        chat_id, settings.secrets.telegram_allowed_user_id
    )
    if allowed_user_id is None:
        raise RuntimeError(
            "Semi Execution requires TELEGRAM_ALLOWED_USER_ID when Telegram chat "
            "is not a private positive chat ID"
        )
    application = (
        Application.builder().token(token).concurrent_updates(4).build()
    )
    notifier = TelegramSignalNotifier(
        token,
        chat_id,
        settings.telegram.display_timezone,
        bot=application.bot,
        semi_execution_enabled=True,
    )
    orchestrator = ScanOrchestrator(settings, notifier)
    coordinator = SemiExecutionCoordinator(
        settings,
        SQLiteDecisionStore(settings.app.state_database),
        orchestrator._new_execution_client,
    )
    callbacks = SemiExecutionCallbackHandler(
        coordinator,
        allowed_chat_id=chat_id,
        allowed_user_id=allowed_user_id,
    )
    application.add_handler(
        CallbackQueryHandler(callbacks.handle, pattern=CALLBACK_PATTERN)
    )
    async with application:
        await application.start()
        if application.updater is None:
            raise RuntimeError("Telegram polling updater is unavailable")
        await application.updater.start_polling(
            allowed_updates=["callback_query"], drop_pending_updates=False
        )
        logging.getLogger(__name__).info(
            "Telegram semi-execution callback listener started"
        )
        try:
            await orchestrator.run_forever()
        finally:
            await application.updater.stop()
            await application.stop()
    return 0


async def _telegram_test(settings: Settings) -> int:
    token = settings.secrets.telegram_bot_token
    chat_id = settings.secrets.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured in .env"
        )
    message_id = await send_telegram_test_message(
        token=token,
        chat_id=chat_id,
    )
    print(f"Telegram TEST message sent successfully (message_id={message_id}).")
    return 0


def show_calendar(
    settings: Settings, hours: int, limit: int, importance: str
) -> int:
    provider = create_calendar_provider(settings)
    if provider is None:
        print("Economic calendar provider is disabled.")
        return 1
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=4)
    end = now + timedelta(hours=hours)
    try:
        events = provider.get_events(start, end)
    except CalendarUnavailableError as exc:
        print(f"Economic calendar unavailable: {exc}", file=sys.stderr)
        return 1
    if importance != "All":
        events = [event for event in events if event.importance == importance]
    print(
        f"Provider: {settings.calendar.provider} | Events: {len(events)} | "
        f"UTC window: {start.isoformat()} to {end.isoformat()}"
    )
    for event in events[:limit]:
        print(
            f"{event.timestamp.isoformat()} | {event.country or '-'} | "
            f"{event.currency or '-'} | "
            f"{event.importance} | {event.name} | actual={event.actual or '-'} | "
            f"forecast={event.forecast or '-'} | previous={event.previous or '-'}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forexai",
        description="Forex analysis, signals, and optional Demo-only execution",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root containing config/ (default: detected automatically)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate configuration and MT5 Demo access")
    subparsers.add_parser(
        "telegram-test",
        help="Send one isolated TEST message using Telegram settings from .env",
    )
    subparsers.add_parser(
        "time-check",
        help="Inspect MT5 raw timestamps and runtime UTC normalization",
    )
    subparsers.add_parser(
        "market-check",
        help="Inspect per-symbol MT5 sessions and open-position guards",
    )
    subparsers.add_parser(
        "execution-check",
        help="Inspect Demo execution readiness without placing an order",
    )
    subparsers.add_parser(
        "semi-execution-check",
        help="Inspect Semi Execution readiness without placing an order",
    )
    scan = subparsers.add_parser("scan-once", help="Analyze the latest closed bars once")
    scan.add_argument("--console", action="store_true", help="Print alerts instead of Telegram")
    run = subparsers.add_parser("run", help="Continuously scan for newly closed bars")
    run.add_argument("--console", action="store_true", help="Print alerts instead of Telegram")
    calendar = subparsers.add_parser(
        "calendar", help="Fetch and print normalized economic calendar events"
    )
    calendar.add_argument("--hours", type=int, default=48, choices=range(1, 169))
    calendar.add_argument("--limit", type=int, default=20, choices=range(1, 101))
    calendar.add_argument(
        "--importance",
        choices=("All", "High", "Medium", "Low", "None", "Unknown"),
        default="All",
    )
    return parser


def main() -> int:
    _configure_console_encoding()
    args = build_parser().parse_args()
    settings = load_settings(args.project_root)
    _configure_logging(settings)
    try:
        if args.command == "check":
            return check_environment(settings)
        if args.command == "telegram-test":
            return asyncio.run(_telegram_test(settings))
        if args.command == "time-check":
            return time_check(settings)
        if args.command == "market-check":
            return market_check(settings)
        if args.command == "execution-check":
            return execution_check(settings)
        if args.command == "semi-execution-check":
            return semi_execution_check(settings)
        if args.command == "scan-once":
            return asyncio.run(_scan_once(settings, args.console))
        if args.command == "run":
            return asyncio.run(_run(settings, args.console))
        if args.command == "calendar":
            return show_calendar(settings, args.hours, args.limit, args.importance)
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 130
    except Exception as exc:
        logging.getLogger(__name__).exception("Command failed")
        safe_error = redact_secrets(
            str(exc),
            [
                settings.secrets.telegram_bot_token or "",
                settings.secrets.telegram_chat_id or "",
                settings.secrets.fmp_api_key or "",
            ],
        )
        print(f"ERROR: {safe_error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
