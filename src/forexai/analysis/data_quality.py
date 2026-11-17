from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


class DataQualityError(ValueError):
    pass


@dataclass(frozen=True)
class DataQualityReport:
    total_bars: int
    duplicate_timestamps_removed: int
    missing_values: int
    impossible_candles: int


def validate_bars(
    frame: pd.DataFrame, *, minimum_bars: int
) -> tuple[pd.DataFrame, DataQualityReport]:
    """Validate canonical Forex bars without fabricating missing market bars."""
    missing_columns = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing_columns:
        raise DataQualityError(f"Missing columns: {sorted(missing_columns)}")

    cleaned = frame.copy()
    if not isinstance(cleaned.index, pd.DatetimeIndex):
        raise DataQualityError("Bar index must be a DatetimeIndex")
    if cleaned.index.tz is None:
        raise DataQualityError("Bar timestamps must be timezone-aware UTC")
    cleaned.index = cleaned.index.tz_convert("UTC")
    cleaned = cleaned.sort_index()

    duplicate_count = int(cleaned.index.duplicated(keep="last").sum())
    if duplicate_count:
        cleaned = cleaned[~cleaned.index.duplicated(keep="last")]

    numeric_columns = list(REQUIRED_COLUMNS) + [
        column for column in ("spread", "real_volume") if column in cleaned.columns
    ]
    for column in numeric_columns:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")

    missing_values = int(cleaned[list(REQUIRED_COLUMNS)].isna().sum().sum())
    impossible = (
        (cleaned["high"] < cleaned["low"])
        | (cleaned["high"] < cleaned["open"])
        | (cleaned["high"] < cleaned["close"])
        | (cleaned["low"] > cleaned["open"])
        | (cleaned["low"] > cleaned["close"])
        | (cleaned[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (cleaned["volume"] < 0)
    )
    impossible_count = int(impossible.sum())

    report = DataQualityReport(
        total_bars=len(cleaned),
        duplicate_timestamps_removed=duplicate_count,
        missing_values=missing_values,
        impossible_candles=impossible_count,
    )
    if len(cleaned) < minimum_bars:
        raise DataQualityError(
            f"Insufficient history: received {len(cleaned)}, need {minimum_bars}"
        )
    if missing_values:
        raise DataQualityError(f"Bars contain {missing_values} missing OHLCV values")
    if impossible_count:
        raise DataQualityError(f"Bars contain {impossible_count} impossible candles")
    return cleaned, report

