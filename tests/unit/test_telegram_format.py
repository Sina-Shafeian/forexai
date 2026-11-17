from datetime import datetime, timezone

from forexai.adapters.telegram.notifier import format_signal
from forexai.domain.models import Direction, Signal


def test_signal_message_is_persian_and_displays_local_and_utc_times() -> None:
    signal = Signal(
        signal_id="one",
        instrument="EURUSD",
        direction=Direction.BUY,
        entry_low=1.1,
        entry_high=1.1,
        stop_loss=1.09,
        take_profit=1.12,
        risk_reward=1.8,
        confidence_score=80,
        score_breakdown={},
        setup_timeframe="H1",
        signal_created_at_utc=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        trigger_bar_time_utc=datetime(2026, 8, 7, 11, 45, tzinfo=timezone.utc),
        current_market_price=1.1002,
        suggested_entry=1.1,
        entry_deviation=0.0002,
        entry_source="closed M15 trigger-bar close",
        reasons=["EMA trend context aligns"],
        invalidation="test",
        rule_version="test",
        metadata={
            "digits": 5,
            "maximum_entry_deviation": 0.0003,
            "position_size": {
                "account_balance": 100000,
                "account_currency": "USD",
                "risk_percent": 0.5,
                "maximum_loss": 500,
                "suggested_volume_lots": 0.5,
                "calculation_source": "mt5_calc_profit",
                "unavailable_reason": None,
            },
        },
    )
    message = format_signal(signal, "Asia/Tehran")
    assert "سیگنال تحلیلی" in message
    assert "قیمت فعلی بازار (Ask)" in message
    assert "0.5 Lot" in message
    assert "15:30:00" in message
    assert "12:00:00 UTC" in message
    assert "زمان کندل Trigger" in message
