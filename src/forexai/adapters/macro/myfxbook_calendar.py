from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from forexai.domain.models import EconomicEvent
from forexai.ports.economic_calendar import CalendarUnavailableError


LOGGER = logging.getLogger(__name__)


# Myfxbook calendar links identify the reporting economy. Euro-area members are
# normalized to EUR because that is the tradable currency affected by the event.
COUNTRY_MARKETS: dict[str, tuple[str, str | None]] = {
    "united-states": ("US", "USD"),
    "united-kingdom": ("GB", "GBP"),
    "euro-area": ("EU", "EUR"),
    "european-union": ("EU", "EUR"),
    "germany": ("DE", "EUR"),
    "france": ("FR", "EUR"),
    "italy": ("IT", "EUR"),
    "spain": ("ES", "EUR"),
    "netherlands": ("NL", "EUR"),
    "belgium": ("BE", "EUR"),
    "austria": ("AT", "EUR"),
    "finland": ("FI", "EUR"),
    "ireland": ("IE", "EUR"),
    "portugal": ("PT", "EUR"),
    "greece": ("GR", "EUR"),
    "luxembourg": ("LU", "EUR"),
    "slovakia": ("SK", "EUR"),
    "slovenia": ("SI", "EUR"),
    "estonia": ("EE", "EUR"),
    "latvia": ("LV", "EUR"),
    "lithuania": ("LT", "EUR"),
    "cyprus": ("CY", "EUR"),
    "malta": ("MT", "EUR"),
    "japan": ("JP", "JPY"),
    "switzerland": ("CH", "CHF"),
    "canada": ("CA", "CAD"),
    "australia": ("AU", "AUD"),
    "new-zealand": ("NZ", "NZD"),
    "china": ("CN", "CNY"),
}


class MyfxbookRSSCalendar:
    """No-key Myfxbook RSS calendar provider for personal/demo use."""

    def __init__(
        self,
        *,
        endpoint: str,
        cache_path: Path,
        cache_minutes: int,
        stale_cache_if_error_hours: int,
        user_agent: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._endpoint = endpoint
        self._cache_path = cache_path
        self._cache_ttl = timedelta(minutes=cache_minutes)
        self._stale_if_error = timedelta(hours=stale_cache_if_error_hours)
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds

    def get_events(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end < start:
            raise ValueError("Calendar end must not precede start")

        cached = self._read_cache(max_age=self._cache_ttl)
        if cached is not None:
            events = parse_myfxbook_rss(cached, start=start, end=end)
            LOGGER.debug("Loaded %d calendar events from fresh RSS cache", len(events))
            return events

        try:
            response = httpx.get(
                self._endpoint,
                headers={
                    "Accept": "application/rss+xml, application/xml, text/xml",
                    "User-Agent": self._user_agent,
                },
                follow_redirects=True,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.content
            # Parse before replacing a known-good cache.
            events = parse_myfxbook_rss(payload, start=start, end=end)
            self._write_cache(payload)
            LOGGER.info(
                "Myfxbook calendar refreshed: %d events in requested window",
                len(events),
            )
            return events
        except (httpx.HTTPError, CalendarUnavailableError) as exc:
            stale = self._read_cache(max_age=self._stale_if_error)
            if stale is not None:
                events = parse_myfxbook_rss(stale, start=start, end=end)
                LOGGER.warning(
                    "Myfxbook unavailable (%s); using stale calendar cache with %d events",
                    exc,
                    len(events),
                )
                return events
            raise CalendarUnavailableError(
                f"Myfxbook RSS calendar unavailable and no usable cache exists: {exc}"
            ) from exc

    def _read_cache(self, *, max_age: timedelta) -> bytes | None:
        if not self._cache_path.exists():
            return None
        try:
            modified = datetime.fromtimestamp(
                self._cache_path.stat().st_mtime, tz=timezone.utc
            )
            if datetime.now(timezone.utc) - modified > max_age:
                return None
            return self._cache_path.read_bytes()
        except OSError:
            return None

    def _write_cache(self, payload: bytes) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(self._cache_path)


def parse_myfxbook_rss(
    payload: bytes | str, *, start: datetime, end: datetime
) -> list[EconomicEvent]:
    """Convert Myfxbook RSS into the project's provider-neutral event model."""
    start = _as_utc(start)
    end = _as_utc(end)
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise CalendarUnavailableError(f"Invalid Myfxbook RSS XML: {exc}") from exc
    if root.tag.lower() != "rss":
        raise CalendarUnavailableError("Myfxbook response is not an RSS document")

    events: list[EconomicEvent] = []
    for item in root.findall("./channel/item"):
        title = _node_text(item.find("title"))
        link = _node_text(item.find("link"))
        published = _node_text(item.find("pubDate"))
        description = _node_text(item.find("description"))
        if not title or not link or not published:
            continue
        try:
            timestamp = _as_utc(parsedate_to_datetime(published))
        except (TypeError, ValueError):
            continue
        if not start <= timestamp <= end:
            continue

        country_slug = _country_slug_from_link(link)
        country, currency = COUNTRY_MARKETS.get(
            country_slug, (country_slug.upper() or "UNKNOWN", None)
        )
        values = _parse_description(description)
        events.append(
            EconomicEvent(
                timestamp=timestamp,
                country=country,
                currency=currency,
                name=title,
                importance=values.importance,
                previous=values.previous,
                forecast=values.forecast,
                actual=values.actual,
            )
        )
    return sorted(events, key=lambda event: event.timestamp)


class _CalendarDescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[str] = []
        self.importance = "Unknown"
        self._inside_cell = False
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "td":
            self._inside_cell = True
            self._cell_parts = []
        if tag.lower() == "span":
            classes = dict(attrs).get("class", "") or ""
            if "high-impact" in classes:
                self.importance = "High"
            elif "medium-impact" in classes or "med-impact" in classes:
                self.importance = "Medium"
            elif "low-impact" in classes:
                self.importance = "Low"
            elif "no-impact" in classes:
                self.importance = "None"

    def handle_data(self, data: str) -> None:
        if self._inside_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "td" and self._inside_cell:
            self.cells.append(" ".join("".join(self._cell_parts).split()))
            self._inside_cell = False
            self._cell_parts = []


class _DescriptionValues:
    def __init__(
        self,
        *,
        importance: str,
        previous: str | None,
        forecast: str | None,
        actual: str | None,
    ) -> None:
        self.importance = importance
        self.previous = previous
        self.forecast = forecast
        self.actual = actual


def _parse_description(description: str) -> _DescriptionValues:
    parser = _CalendarDescriptionParser()
    parser.feed(unescape(description))
    cells = parser.cells
    return _DescriptionValues(
        importance=parser.importance,
        previous=_optional_cell(cells, 2),
        forecast=_optional_cell(cells, 3),
        actual=_optional_cell(cells, 4),
    )


def _optional_cell(cells: list[str], index: int) -> str | None:
    if index >= len(cells):
        return None
    value = cells[index].strip()
    return value or None


def _country_slug_from_link(link: str) -> str:
    parts = [part for part in urlparse(link).path.split("/") if part]
    try:
        marker = parts.index("forex-economic-calendar")
        return parts[marker + 1].lower()
    except (ValueError, IndexError):
        return "unknown"


def _node_text(node: ElementTree.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

