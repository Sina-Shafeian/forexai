import pandas as pd
import pytest

from forexai.analysis.data_quality import DataQualityError, validate_bars


def _frame(count: int = 120) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=count, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.1] * count,
            "high": [1.2] * count,
            "low": [1.0] * count,
            "close": [1.15] * count,
            "volume": [100] * count,
        },
        index=index,
    )


def test_validate_bars_keeps_real_timeline_without_gap_fill() -> None:
    frame = _frame().drop(_frame().index[30])
    cleaned, report = validate_bars(frame, minimum_bars=100)
    assert len(cleaned) == 119
    assert report.total_bars == 119
    assert cleaned.index[30] - cleaned.index[29] == pd.Timedelta(minutes=30)


def test_impossible_candle_is_rejected() -> None:
    frame = _frame()
    frame.iloc[20, frame.columns.get_loc("high")] = 0.9
    with pytest.raises(DataQualityError, match="impossible"):
        validate_bars(frame, minimum_bars=100)


def test_naive_timestamps_are_rejected() -> None:
    frame = _frame()
    frame.index = frame.index.tz_localize(None)
    with pytest.raises(DataQualityError, match="timezone-aware"):
        validate_bars(frame, minimum_bars=100)

