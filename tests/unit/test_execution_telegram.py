from datetime import datetime, timedelta, timezone

from forexai.adapters.telegram.notifier import format_signal
from forexai.domain.models import Direction, Signal


def _signal(metadata):
    now = datetime.now(timezone.utc)
    return Signal(
        signal_id="telegram-exec",
        instrument="EURUSD",
        direction=Direction.BUY,
        entry_low=1.1,
        entry_high=1.1,
        stop_loss=1.09,
        take_profit=1.12,
        risk_reward=2,
        confidence_score=80,
        score_breakdown={},
        setup_timeframe="H1",
        signal_created_at_utc=now,
        trigger_bar_time_utc=now - timedelta(minutes=15),
        current_market_price=1.1,
        suggested_entry=1.1,
        entry_deviation=0,
        entry_source="test",
        reasons=["test"],
        invalidation="test",
        rule_version="test",
        metadata=metadata,
    )


def test_disabled_execution_leaves_telegram_format_unchanged() -> None:
    text = format_signal(_signal({"digits": 5}))
    assert "اجرای خودکار" not in text


def test_success_and_blocked_execution_are_reported() -> None:
    success = format_signal(
        _signal(
            {
                "digits": 5,
                "auto_execution": {
                    "status": "EXECUTED",
                    "actual_execution_price": 1.10021,
                    "executed_volume": 0.05,
                    "sl": 1.09,
                    "tp": 1.12,
                    "position_ticket": 9001,
                },
            }
        )
    )
    blocked = format_signal(
        _signal(
            {
                "digits": 5,
                "auto_execution": {
                    "status": "BLOCKED",
                    "reason": "AUTO_EXECUTION_EXISTING_POSITION",
                },
            }
        )
    )
    assert "اجرای خودکار: موفق" in success
    assert "1.10021" in success and "9001" in success
    assert "اجرای خودکار انجام نشد" in blocked
    assert "Position باز" in blocked
