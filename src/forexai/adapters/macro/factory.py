from __future__ import annotations

from forexai.adapters.macro.fmp_calendar import FMPEconomicCalendar
from forexai.adapters.macro.myfxbook_calendar import MyfxbookRSSCalendar
from forexai.config import Settings
from forexai.ports.economic_calendar import EconomicCalendarProvider


def create_calendar_provider(
    settings: Settings,
) -> EconomicCalendarProvider | None:
    calendar = settings.calendar
    if calendar.provider == "disabled":
        return None
    if calendar.provider == "myfxbook_rss":
        return MyfxbookRSSCalendar(
            endpoint=calendar.myfxbook_endpoint,
            cache_path=settings.project_root
            / "data/state/myfxbook_economic_calendar.xml",
            cache_minutes=calendar.cache_minutes,
            stale_cache_if_error_hours=calendar.stale_cache_if_error_hours,
            user_agent=calendar.user_agent,
        )
    if calendar.provider == "fmp":
        return FMPEconomicCalendar(
            endpoint=calendar.fmp_endpoint,
            api_key=settings.secrets.fmp_api_key,
            cache_path=settings.project_root
            / "data/state/fmp_economic_calendar.json",
            cache_minutes=calendar.cache_minutes,
        )
    raise ValueError(f"Unsupported calendar provider: {calendar.provider}")

