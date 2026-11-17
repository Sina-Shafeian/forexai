from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class SessionSnapshot:
    active_session: str
    asian_range_high: float | None
    asian_range_low: float | None
    asian_range_status: str
    previous_day_high: float | None
    previous_day_low: float | None


def analyze_sessions(frame: pd.DataFrame, as_of: datetime) -> SessionSnapshot:
    as_of = as_of.astimezone(timezone.utc)
    active = _active_session(as_of)
    asian_high, asian_low, asian_status = _latest_asian_range(frame, as_of)
    previous_high, previous_low = _previous_trading_day_levels(frame, as_of)
    return SessionSnapshot(
        active_session=active,
        asian_range_high=asian_high,
        asian_range_low=asian_low,
        asian_range_status=asian_status,
        previous_day_high=previous_high,
        previous_day_low=previous_low,
    )


def _inside_local_window(
    as_of: datetime, timezone_name: str, start: time, end: time
) -> bool:
    local = as_of.astimezone(ZoneInfo(timezone_name))
    return start <= local.time().replace(tzinfo=None) < end


def _active_session(as_of: datetime) -> str:
    tokyo = _inside_local_window(
        as_of, "Asia/Tokyo", time(hour=9), time(hour=18)
    )
    london = _inside_local_window(
        as_of, "Europe/London", time(hour=8), time(hour=17)
    )
    new_york = _inside_local_window(
        as_of, "America/New_York", time(hour=8), time(hour=17)
    )
    if london and new_york:
        return "LONDON_NEW_YORK_OVERLAP"
    active = [
        name
        for name, enabled in (
            ("TOKYO", tokyo),
            ("LONDON", london),
            ("NEW_YORK", new_york),
        )
        if enabled
    ]
    return "+".join(active) if active else "OUTSIDE"


def _latest_asian_range(
    frame: pd.DataFrame, as_of: datetime
) -> tuple[float | None, float | None, str]:
    local_index = frame.index.tz_convert("Asia/Tokyo")
    candidates = pd.DataFrame(index=frame.index)
    candidates["local_date"] = local_index.date
    candidates["local_time"] = local_index.time
    candidates["high"] = frame["high"]
    candidates["low"] = frame["low"]
    in_window = candidates[
        (candidates["local_time"] >= time(hour=9))
        & (candidates["local_time"] < time(hour=18))
    ]
    if in_window.empty:
        return None, None, "UNAVAILABLE"
    latest_date = in_window["local_date"].max()
    selected = in_window[in_window["local_date"] == latest_date]
    now_tokyo = as_of.astimezone(ZoneInfo("Asia/Tokyo"))
    status = (
        "DEVELOPING"
        if now_tokyo.date() == latest_date and now_tokyo.time() < time(hour=18)
        else "FINAL"
    )
    return float(selected["high"].max()), float(selected["low"].min()), status


def _previous_trading_day_levels(
    frame: pd.DataFrame, as_of: datetime
) -> tuple[float | None, float | None]:
    ny_index = frame.index.tz_convert("America/New_York")
    trading_dates = (ny_index - timedelta(hours=17)).date
    grouped = pd.DataFrame(
        {"trading_date": trading_dates, "high": frame.high, "low": frame.low},
        index=frame.index,
    )
    current_date = (
        as_of.astimezone(ZoneInfo("America/New_York")) - timedelta(hours=17)
    ).date()
    completed = grouped[grouped["trading_date"] < current_date]
    if completed.empty:
        return None, None
    previous_date = completed["trading_date"].max()
    previous = completed[completed["trading_date"] == previous_date]
    return float(previous.high.max()), float(previous.low.min())

