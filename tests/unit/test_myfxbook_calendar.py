from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from forexai.adapters.macro.myfxbook_calendar import (
    MyfxbookRSSCalendar,
    parse_myfxbook_rss,
)
from forexai.ports.economic_calendar import CalendarUnavailableError


FIXTURE = Path(__file__).parents[1] / "fixtures/myfxbook_calendar.xml"


def test_rss_maps_to_provider_neutral_event_contract() -> None:
    events = parse_myfxbook_rss(
        FIXTURE.read_bytes(),
        start=datetime(2026, 8, 7, 0, tzinfo=timezone.utc),
        end=datetime(2026, 8, 8, 0, tzinfo=timezone.utc),
    )
    assert len(events) == 2
    nfp = events[0]
    assert nfp.timestamp == datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)
    assert nfp.country == "US"
    assert nfp.currency == "USD"
    assert nfp.name == "United States Non Farm Payrolls"
    assert nfp.importance == "High"
    assert nfp.previous == "20.0K"
    assert nfp.forecast == "80.0K"
    assert nfp.actual == "-23.0K"
    assert "importance" in nfp.model_dump()


def test_rss_filters_to_requested_window() -> None:
    events = parse_myfxbook_rss(
        FIXTURE.read_bytes(),
        start=datetime(2026, 8, 8, 0, tzinfo=timezone.utc),
        end=datetime(2026, 8, 9, 0, tzinfo=timezone.utc),
    )
    assert events == []


def test_network_failure_without_cache_is_explicit(monkeypatch, tmp_path) -> None:
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fail)
    provider = MyfxbookRSSCalendar(
        endpoint="https://example.invalid/calendar.xml",
        cache_path=tmp_path / "calendar.xml",
        cache_minutes=30,
        stale_cache_if_error_hours=6,
        user_agent="ForexAI-Test",
    )
    with pytest.raises(CalendarUnavailableError, match="no usable cache"):
        provider.get_events(
            datetime(2026, 8, 7, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 8, 0, tzinfo=timezone.utc),
        )


def test_recent_cache_avoids_network(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "calendar.xml"
    cache.write_bytes(FIXTURE.read_bytes())

    def fail(*args, **kwargs):
        raise AssertionError("network should not be called for a fresh cache")

    monkeypatch.setattr(httpx, "get", fail)
    provider = MyfxbookRSSCalendar(
        endpoint="https://example.invalid/calendar.xml",
        cache_path=cache,
        cache_minutes=30,
        stale_cache_if_error_hours=6,
        user_agent="ForexAI-Test",
    )
    events = provider.get_events(
        datetime(2026, 8, 7, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 8, 0, tzinfo=timezone.utc),
    )
    assert len(events) == 2

