from datetime import datetime, timezone

import pytest

from forexai.analysis.entry import evaluate_market_entry
from forexai.domain.models import DecisionStatus, Direction, SymbolSnapshot


def _snapshot(**changes) -> SymbolSnapshot:
    values = {
        "canonical": "EURUSD",
        "broker_symbol": "EURUSD",
        "point": 0.00001,
        "digits": 5,
        "bid": 1.1000,
        "ask": 1.1002,
        "spread_points": 20,
        "observed_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
    }
    values.update(changes)
    return SymbolSnapshot(**values)


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(Direction.BUY, 1.1002), (Direction.SELL, 1.1000)],
)
def test_market_entry_uses_ask_for_buy_and_bid_for_sell(direction, expected) -> None:
    result = evaluate_market_entry(
        direction=direction,
        snapshot=_snapshot(),
        suggested_entry=1.1001,
        stop_loss=1.0990 if direction is Direction.BUY else 1.1010,
        take_profit=1.1030 if direction is Direction.BUY else 1.0970,
        atr=0.002,
        maximum_deviation_atr_fraction=0.15,
    )
    assert result.current_market_price == expected
    assert result.status is None


def test_entry_missed_and_stale_entry_are_distinct() -> None:
    missed = evaluate_market_entry(
        direction=Direction.BUY,
        snapshot=_snapshot(ask=1.1020),
        suggested_entry=1.1000,
        stop_loss=1.0990,
        take_profit=1.1040,
        atr=0.002,
        maximum_deviation_atr_fraction=0.15,
    )
    assert missed.status is DecisionStatus.ENTRY_MISSED

    stale = evaluate_market_entry(
        direction=Direction.BUY,
        snapshot=_snapshot(time_warnings=["STALE_TICK"]),
        suggested_entry=1.1000,
        stop_loss=1.0990,
        take_profit=1.1040,
        atr=0.002,
        maximum_deviation_atr_fraction=0.15,
    )
    assert stale.status is DecisionStatus.STALE_ENTRY
