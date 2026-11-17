from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from forexai.domain.models import EconomicEvent, Instrument


MAJOR_EVENT_TERMS = (
    "CPI",
    "PCE",
    "NONFARM",
    "NON-FARM",
    "NFP",
    "GDP",
    "PMI",
    "INTEREST RATE",
    "RATE DECISION",
    "FOMC",
    "CENTRAL BANK",
)


@dataclass(frozen=True)
class EventAssessment:
    blocked: bool
    warning: str | None
    relevant_events: tuple[EconomicEvent, ...]


def assess_event_risk(
    instrument: Instrument,
    events: list[EconomicEvent],
    *,
    as_of: datetime,
    before_minutes: int,
    after_minutes: int,
) -> EventAssessment:
    as_of = as_of.astimezone(timezone.utc)
    currencies = {instrument.quote}
    if instrument.kind == "forex":
        currencies.add(instrument.base)
    relevant = [
        event
        for event in events
        if event.currency in currencies and _is_high_impact(event)
    ]
    relevant.sort(key=lambda event: abs((event.timestamp - as_of).total_seconds()))
    blocked_event = next(
        (
            event
            for event in relevant
            if as_of - timedelta(minutes=after_minutes)
            <= event.timestamp
            <= as_of + timedelta(minutes=before_minutes)
        ),
        None,
    )
    nearest = relevant[0] if relevant else None
    warning = None
    if nearest is not None:
        delta_minutes = int((nearest.timestamp - as_of).total_seconds() / 60)
        timing = f"in {delta_minutes}m" if delta_minutes >= 0 else f"{abs(delta_minutes)}m ago"
        warning = f"{nearest.currency} {nearest.name} ({nearest.impact}) {timing}"
    return EventAssessment(
        blocked=blocked_event is not None,
        warning=warning,
        relevant_events=tuple(relevant),
    )


def _is_high_impact(event: EconomicEvent) -> bool:
    if event.impact.strip().upper() == "HIGH":
        return True
    event_name = event.name.upper()
    return any(term in event_name for term in MAJOR_EVENT_TERMS)

