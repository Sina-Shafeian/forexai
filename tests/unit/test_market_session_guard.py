from datetime import datetime, timedelta, timezone

from forexai.analysis.dispatch_guards import evaluate_market_session_guard
from forexai.domain.models import (
    DecisionStatus,
    SymbolSnapshot,
    SymbolTradingSession,
)


BROKER_OFFSET = 180


def _snapshot(now_utc: datetime, *, age_seconds: int = 0) -> SymbolSnapshot:
    return SymbolSnapshot(
        canonical="XAUUSD",
        broker_symbol="XAUUSD",
        point=0.01,
        digits=2,
        bid=2400,
        ask=2400.2,
        spread_points=20,
        observed_at=now_utc - timedelta(seconds=age_seconds),
        broker_offset_minutes=BROKER_OFFSET,
    )


def _session(
    symbol: str = "XAUUSD",
    *,
    day: int = 1,
    index: int = 0,
    start_hour: int = 0,
    end_hour: int = 23,
) -> SymbolTradingSession:
    return SymbolTradingSession(
        broker_symbol=symbol,
        day_of_week=day,
        session_index=index,
        start_seconds=start_hour * 3600,
        end_seconds=end_hour * 3600,
    )


def _assess(
    broker_hour: int,
    broker_minute: int,
    sessions,
    *,
    offset: int = BROKER_OFFSET,
    age_seconds: int = 0,
):
    broker_time = datetime(
        2026,
        8,
        10,
        broker_hour,
        broker_minute,
        tzinfo=timezone(timedelta(minutes=offset)),
    )
    now_utc = broker_time.astimezone(timezone.utc)
    snapshot = _snapshot(now_utc, age_seconds=age_seconds)
    snapshot.broker_offset_minutes = offset
    return evaluate_market_session_guard(
        broker_symbol="XAUUSD",
        sessions=sessions,
        now_utc=now_utc,
        broker_offset_minutes=offset,
        snapshot=snapshot,
        block_before_close_minutes=60,
        stale_tick_threshold_seconds=120,
    )


def test_open_market_more_than_sixty_minutes_before_close_passes() -> None:
    result = _assess(21, 30, [_session()])
    assert result.blocked is False
    assert result.diagnostics["minutes_remaining"] == 90


def test_exactly_sixty_minutes_before_close_is_suppressed() -> None:
    result = _assess(22, 0, [_session()])
    assert result.status is DecisionStatus.SUPPRESSED_SESSION_CLOSING


def test_less_than_sixty_minutes_before_close_is_suppressed() -> None:
    result = _assess(22, 15, [_session()])
    assert result.status is DecisionStatus.SUPPRESSED_SESSION_CLOSING
    assert result.diagnostics["minutes_remaining"] == 45


def test_market_outside_session_is_closed() -> None:
    result = _assess(23, 30, [_session()])
    assert result.status is DecisionStatus.SUPPRESSED_MARKET_CLOSED


def test_weekend_without_session_is_closed() -> None:
    saturday = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    result = evaluate_market_session_guard(
        broker_symbol="XAUUSD",
        sessions=[_session(day=1)],
        now_utc=saturday,
        broker_offset_minutes=0,
        snapshot=_snapshot(saturday),
        block_before_close_minutes=60,
        stale_tick_threshold_seconds=120,
    )
    assert result.status is DecisionStatus.SUPPRESSED_MARKET_CLOSED


def test_multiple_sessions_selects_the_current_session() -> None:
    sessions = [
        _session(index=0, start_hour=0, end_hour=12),
        _session(index=1, start_hour=13, end_hour=23),
    ]
    result = _assess(14, 0, sessions)
    assert result.blocked is False
    assert result.diagnostics["session_index"] == 1


def test_break_between_sessions_is_market_closed() -> None:
    sessions = [
        _session(index=0, start_hour=0, end_hour=12),
        _session(index=1, start_hour=13, end_hour=23),
    ]
    assert _assess(12, 30, sessions).status is DecisionStatus.SUPPRESSED_MARKET_CLOSED


def test_different_symbols_use_independent_session_times() -> None:
    broker_time = datetime(
        2026, 8, 10, 21, 30, tzinfo=timezone(timedelta(hours=3))
    )
    now_utc = broker_time.astimezone(timezone.utc)
    snapshot = _snapshot(now_utc)
    eur = evaluate_market_session_guard(
        broker_symbol="EURUSD",
        sessions=[_session("EURUSD", end_hour=23)],
        now_utc=now_utc,
        broker_offset_minutes=180,
        snapshot=snapshot,
        block_before_close_minutes=60,
        stale_tick_threshold_seconds=120,
    )
    xau = evaluate_market_session_guard(
        broker_symbol="XAUUSD",
        sessions=[_session("XAUUSD", end_hour=22)],
        now_utc=now_utc,
        broker_offset_minutes=180,
        snapshot=snapshot,
        block_before_close_minutes=60,
        stale_tick_threshold_seconds=120,
    )
    assert eur.blocked is False
    assert xau.status is DecisionStatus.SUPPRESSED_SESSION_CLOSING


def test_runtime_broker_offset_controls_session_comparison() -> None:
    now_utc = datetime(2026, 8, 10, 19, 15, tzinfo=timezone.utc)
    snapshot = _snapshot(now_utc)
    with_offset = evaluate_market_session_guard(
        broker_symbol="XAUUSD",
        sessions=[_session()],
        now_utc=now_utc,
        broker_offset_minutes=180,
        snapshot=snapshot,
        block_before_close_minutes=60,
        stale_tick_threshold_seconds=120,
    )
    without_offset = evaluate_market_session_guard(
        broker_symbol="XAUUSD",
        sessions=[_session()],
        now_utc=now_utc,
        broker_offset_minutes=0,
        snapshot=snapshot,
        block_before_close_minutes=60,
        stale_tick_threshold_seconds=120,
    )
    assert with_offset.status is DecisionStatus.SUPPRESSED_SESSION_CLOSING
    assert without_offset.blocked is False


def test_unavailable_session_information_suppresses_safely() -> None:
    result = _assess(12, 0, None)
    assert result.status is DecisionStatus.SESSION_STATUS_UNAVAILABLE


def test_stale_tick_during_open_session_suppresses() -> None:
    result = _assess(12, 0, [_session()], age_seconds=121)
    assert result.status is DecisionStatus.STALE_MARKET_DATA


def test_stale_tick_during_closed_session_reports_market_closed() -> None:
    result = _assess(23, 30, [_session()], age_seconds=1000)
    assert result.status is DecisionStatus.SUPPRESSED_MARKET_CLOSED
