from datetime import datetime, timedelta, timezone

import pytest

from forexai.analysis.time_normalization import (
    BROKER_OFFSET_CHANGED,
    FUTURE_TIMESTAMP,
    STALE_TICK,
    detect_broker_clock,
    normalize_broker_epoch,
)


def test_runtime_detects_three_hour_broker_shift_without_hardcoding() -> None:
    now = datetime(2026, 8, 7, 15, 0, 38, tzinfo=timezone.utc)
    raw = (now + timedelta(hours=3)).timestamp()
    result = detect_broker_clock(raw, now_utc=now)
    assert result.offset_minutes == 180
    assert result.normalized_time_utc == now
    assert result.tick_age_seconds == pytest.approx(0)
    assert result.warnings == ()


def test_offset_change_stale_tick_and_future_timestamp_are_explicit() -> None:
    now = datetime(2026, 8, 7, 15, tzinfo=timezone.utc)
    changed = detect_broker_clock(
        (now + timedelta(hours=3)).timestamp(),
        now_utc=now,
        previous_offset_minutes=120,
    )
    assert BROKER_OFFSET_CHANGED in changed.warnings

    stale = detect_broker_clock(
        (now + timedelta(hours=3, minutes=-5)).timestamp(),
        now_utc=now,
        previous_offset_minutes=180,
    )
    assert STALE_TICK in stale.warnings

    future = detect_broker_clock(
        (now + timedelta(seconds=10)).timestamp(),
        now_utc=now,
        previous_offset_minutes=0,
    )
    assert FUTURE_TIMESTAMP in future.warnings


def test_bar_epoch_uses_detected_offset_and_rejects_naive_now() -> None:
    raw = datetime(2026, 8, 7, 18, tzinfo=timezone.utc).timestamp()
    assert normalize_broker_epoch(raw, 180) == datetime(
        2026, 8, 7, 15, tzinfo=timezone.utc
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        detect_broker_clock(raw, now_utc=datetime(2026, 8, 7, 15))
