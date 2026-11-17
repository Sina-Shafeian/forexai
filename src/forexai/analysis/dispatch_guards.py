from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from forexai.domain.models import (
    DecisionStatus,
    OpenPosition,
    SymbolSnapshot,
    SymbolTradingSession,
)


@dataclass(frozen=True)
class DispatchGuardResult:
    blocked: bool
    status: DecisionStatus | None = None
    reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def evaluate_open_position_guard(
    broker_symbol: str,
    positions: list[OpenPosition] | None,
) -> DispatchGuardResult:
    if positions is None:
        return DispatchGuardResult(
            blocked=True,
            status=DecisionStatus.POSITION_STATUS_UNAVAILABLE,
            reason="POSITION_STATUS_UNAVAILABLE",
        )
    existing = next(
        (
            position
            for position in positions
            if position.broker_symbol.upper() == broker_symbol.upper()
        ),
        None,
    )
    if existing is None:
        return DispatchGuardResult(blocked=False)
    return DispatchGuardResult(
        blocked=True,
        status=DecisionStatus.SUPPRESSED_EXISTING_POSITION,
        reason="SUPPRESSED_SYMBOL_ALREADY_OPEN",
        diagnostics={
            "ticket": existing.ticket,
            "existing_direction": existing.direction.value,
            "existing_volume": existing.volume,
            "price_open": existing.price_open,
            "sl": existing.stop_loss,
            "tp": existing.take_profit,
        },
    )


def evaluate_market_session_guard(
    *,
    broker_symbol: str,
    sessions: list[SymbolTradingSession] | None,
    now_utc: datetime,
    broker_offset_minutes: int,
    snapshot: SymbolSnapshot,
    block_before_close_minutes: int,
    stale_tick_threshold_seconds: int,
) -> DispatchGuardResult:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    broker_zone = timezone(timedelta(minutes=broker_offset_minutes))
    broker_now = now_utc.astimezone(broker_zone)
    base_diagnostics = {
        "broker_symbol": broker_symbol,
        "broker_time": broker_now.isoformat(),
        "broker_offset_minutes": broker_offset_minutes,
    }
    if sessions is None:
        return DispatchGuardResult(
            blocked=True,
            status=DecisionStatus.SESSION_STATUS_UNAVAILABLE,
            reason="SESSION_STATUS_UNAVAILABLE",
            diagnostics=base_diagnostics,
        )
    active = _find_active_session(broker_now, sessions)
    if active is None:
        return DispatchGuardResult(
            blocked=True,
            status=DecisionStatus.SUPPRESSED_MARKET_CLOSED,
            reason="SUPPRESSED_MARKET_CLOSED",
            diagnostics=base_diagnostics,
        )

    session, session_start, session_close = active
    minutes_remaining = (session_close - broker_now).total_seconds() / 60
    diagnostics = {
        **base_diagnostics,
        "session_index": session.session_index,
        "session_start": session_start.isoformat(),
        "session_close": session_close.isoformat(),
        "minutes_remaining": minutes_remaining,
    }
    tick_age_seconds = (
        now_utc.astimezone(timezone.utc) - snapshot.observed_at
    ).total_seconds()
    diagnostics["tick_age_seconds"] = tick_age_seconds
    if (
        tick_age_seconds > stale_tick_threshold_seconds
        or tick_age_seconds < -5
        or {"STALE_TICK", "FUTURE_TIMESTAMP"}.intersection(
            snapshot.time_warnings
        )
    ):
        return DispatchGuardResult(
            blocked=True,
            status=DecisionStatus.STALE_MARKET_DATA,
            reason="STALE_MARKET_DATA",
            diagnostics=diagnostics,
        )
    if minutes_remaining <= block_before_close_minutes:
        return DispatchGuardResult(
            blocked=True,
            status=DecisionStatus.SUPPRESSED_SESSION_CLOSING,
            reason="SUPPRESSED_SESSION_CLOSING",
            diagnostics=diagnostics,
        )
    return DispatchGuardResult(blocked=False, diagnostics=diagnostics)


def _find_active_session(
    broker_now: datetime,
    sessions: list[SymbolTradingSession],
) -> tuple[SymbolTradingSession, datetime, datetime] | None:
    for session_date in (broker_now.date() - timedelta(days=1), broker_now.date()):
        mql_day = _mql_day_of_week(session_date)
        day_midnight = datetime.combine(
            session_date, time.min, tzinfo=broker_now.tzinfo
        )
        for session in sorted(sessions, key=lambda item: item.session_index):
            if session.day_of_week != mql_day:
                continue
            start = day_midnight + timedelta(seconds=session.start_seconds)
            end_seconds = session.end_seconds
            if end_seconds <= session.start_seconds:
                end_seconds += 86_400
            end = day_midnight + timedelta(seconds=end_seconds)
            if start <= broker_now < end:
                return session, start, end
    return None


def _mql_day_of_week(value: date) -> int:
    # MQL5 uses Sunday=0, Monday=1, ..., Saturday=6.
    return (value.weekday() + 1) % 7
