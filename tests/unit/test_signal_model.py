from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from forexai.domain.models import Direction, Signal


def test_signal_id_is_deterministic_and_rule_versioned() -> None:
    bar_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    first = Signal.make_id("EURUSD", Direction.BUY, "H1", bar_time, "0.1.0")
    second = Signal.make_id("EURUSD", Direction.BUY, "H1", bar_time, "0.1.0")
    changed = Signal.make_id("EURUSD", Direction.BUY, "H1", bar_time, "0.2.0")
    assert first == second
    assert first != changed


def test_signal_timestamps_are_explicit_utc_fields_and_reject_naive_values() -> None:
    common = dict(
        signal_id="one",
        instrument="EURUSD",
        direction=Direction.BUY,
        entry_low=1.0,
        entry_high=1.0,
        stop_loss=0.9,
        take_profit=1.2,
        risk_reward=2,
        confidence_score=80,
        score_breakdown={},
        setup_timeframe="H1",
        current_market_price=1.0,
        suggested_entry=1.0,
        entry_deviation=0,
        entry_source="test",
        reasons=[],
        invalidation="test",
        rule_version="test",
    )
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signal = Signal(
        **common, signal_created_at_utc=aware, trigger_bar_time_utc=aware
    )
    assert signal.model_dump()["signal_created_at_utc"] == aware
    assert signal.issued_at == aware
    with pytest.raises(ValidationError, match="timezone-aware"):
        Signal(
            **common,
            signal_created_at_utc=datetime(2026, 1, 1),
            trigger_bar_time_utc=aware,
        )
