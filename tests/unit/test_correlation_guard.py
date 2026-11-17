from datetime import datetime, timezone

import pandas as pd

from forexai.analysis.correlation_guard import select_diversified_signals
from forexai.domain.models import Direction, Instrument, Signal


def _signal(symbol: str, direction: Direction, score: int) -> Signal:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    return Signal(
        signal_id=f"{symbol}-{direction.value}",
        instrument=symbol,
        direction=direction,
        entry_low=1.0,
        entry_high=1.0,
        stop_loss=0.9 if direction is Direction.BUY else 1.1,
        take_profit=1.2 if direction is Direction.BUY else 0.8,
        risk_reward=2.0,
        confidence_score=score,
        score_breakdown={},
        setup_timeframe="H1",
        issued_at=now,
        trigger_bar_time=now,
        current_market_price=1.0,
        suggested_entry=1.0,
        entry_deviation=0.0,
        entry_source="test",
        reasons=["test"],
        invalidation="test",
        rule_version="test",
    )


def test_directionally_correlated_lower_score_signal_is_rejected() -> None:
    index = pd.date_range("2026-01-01", periods=150, freq="h", tz="UTC")
    first_prices = pd.Series([1 + index * 0.001 for index in range(150)])
    second_prices = first_prices * 1.2
    frames = {
        "EURUSD": pd.DataFrame({"close": first_prices.to_numpy()}, index=index),
        "GBPUSD": pd.DataFrame({"close": second_prices.to_numpy()}, index=index),
    }
    instruments = [
        Instrument(
            canonical="EURUSD", broker_symbol="EURUSD", base="EUR", quote="USD"
        ),
        Instrument(
            canonical="GBPUSD", broker_symbol="GBPUSD", base="GBP", quote="USD"
        ),
    ]
    higher = _signal("EURUSD", Direction.BUY, 90)
    lower = _signal("GBPUSD", Direction.BUY, 80)

    selected, rejected = select_diversified_signals(
        [lower, higher],
        instruments,
        frames,
        lookback_bars=120,
        maximum_directional_correlation=0.8,
        max_same_currency_direction_alerts=2,
        limit=5,
    )
    assert [signal.signal_id for signal in selected] == [higher.signal_id]
    assert rejected[0].signal_id == lower.signal_id
    assert "correlation" in rejected[0].reason.lower()
