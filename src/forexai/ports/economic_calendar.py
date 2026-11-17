from __future__ import annotations

from datetime import datetime
from typing import Protocol

from forexai.domain.models import EconomicEvent


class EconomicCalendarProvider(Protocol):
    def get_events(self, start: datetime, end: datetime) -> list[EconomicEvent]: ...


class CalendarUnavailableError(RuntimeError):
    """Calendar state is unknown and must not be represented as no events."""

