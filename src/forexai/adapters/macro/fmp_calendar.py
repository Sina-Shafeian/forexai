from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from forexai.domain.models import EconomicEvent
from forexai.ports.economic_calendar import CalendarUnavailableError


COUNTRY_TO_CURRENCY = {
    "US": "USD",
    "EU": "EUR",
    "EA": "EUR",
    "UK": "GBP",
    "GB": "GBP",
    "JP": "JPY",
    "CH": "CHF",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
}


class FMPEconomicCalendar:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        cache_path: Path,
        cache_minutes: int,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._cache_path = cache_path
        self._cache_ttl = timedelta(minutes=cache_minutes)
        self._timeout_seconds = timeout_seconds

    def get_events(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        start = _as_utc(start)
        end = _as_utc(end)
        if end < start:
            raise ValueError("Calendar end must not precede start")

        cached = self._read_fresh_cache(start, end)
        if cached is not None:
            return cached
        if not self._api_key:
            raise CalendarUnavailableError(
                "FMP_API_KEY is missing; calendar state is unknown"
            )

        try:
            response = httpx.get(
                self._endpoint,
                params={
                    "from": start.date().isoformat(),
                    "to": end.date().isoformat(),
                    "apikey": self._api_key,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise CalendarUnavailableError(f"FMP network error: {exc}") from exc

        if response.status_code == 402:
            raise CalendarUnavailableError(
                "FMP Economic Calendar is not included in the current subscription"
            )
        if response.status_code == 429:
            raise CalendarUnavailableError("FMP rate limit exceeded")
        if response.status_code >= 400:
            raise CalendarUnavailableError(
                f"FMP returned HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CalendarUnavailableError("FMP returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise CalendarUnavailableError("FMP response is not an event list")

        fetched_at = datetime.now(timezone.utc)
        self._write_cache(start, end, fetched_at, payload)
        return self._parse_and_filter(payload, start, end)

    def _read_fresh_cache(
        self, start: datetime, end: datetime
    ) -> list[EconomicEvent] | None:
        if not self._cache_path.exists():
            return None
        try:
            cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
            fetched_at = _parse_timestamp(cached["fetched_at"])
            cached_start = _parse_timestamp(cached["start"])
            cached_end = _parse_timestamp(cached["end"])
            if datetime.now(timezone.utc) - fetched_at > self._cache_ttl:
                return None
            if cached_start > start or cached_end < end:
                return None
            return self._parse_and_filter(cached["events"], start, end)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_cache(
        self,
        start: datetime,
        end: datetime,
        fetched_at: datetime,
        payload: list[dict],
    ) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "events": payload,
        }
        self._cache_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _parse_and_filter(
        payload: list[dict], start: datetime, end: datetime
    ) -> list[EconomicEvent]:
        events: list[EconomicEvent] = []
        for item in payload:
            raw_time = item.get("date") or item.get("timestamp")
            if not raw_time:
                continue
            try:
                event_time = _parse_timestamp(raw_time)
            except (TypeError, ValueError):
                continue
            if not start <= event_time <= end:
                continue
            country = str(item.get("country") or "").upper() or None
            currency = str(item.get("currency") or "").upper() or None
            currency = currency or COUNTRY_TO_CURRENCY.get(country or "")
            events.append(
                EconomicEvent(
                    timestamp=event_time,
                    name=str(item.get("event") or item.get("name") or "Unknown event"),
                    currency=currency,
                    country=country,
                    impact=str(item.get("impact") or "Unknown"),
                    actual=item.get("actual"),
                    forecast=item.get("estimate", item.get("forecast")),
                    previous=item.get("previous"),
                )
            )
        return sorted(events, key=lambda event: event.timestamp)


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    normalized = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

