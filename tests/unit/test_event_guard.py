from datetime import datetime, timedelta, timezone

from forexai.analysis.event_guard import assess_event_risk
from forexai.domain.models import EconomicEvent, Instrument


def test_relevant_high_impact_event_blocks_signal() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    event = EconomicEvent(
        timestamp=now + timedelta(minutes=30),
        name="US CPI",
        currency="USD",
        impact="High",
    )
    pair = Instrument(
        canonical="EURUSD",
        broker_symbol="EURUSD",
        base="EUR",
        quote="USD",
    )
    result = assess_event_risk(
        pair, [event], as_of=now, before_minutes=60, after_minutes=30
    )
    assert result.blocked is True
    assert "CPI" in (result.warning or "")


def test_unrelated_currency_does_not_block() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    event = EconomicEvent(
        timestamp=now + timedelta(minutes=10),
        name="Japan CPI",
        currency="JPY",
        impact="High",
    )
    pair = Instrument(
        canonical="EURUSD",
        broker_symbol="EURUSD",
        base="EUR",
        quote="USD",
    )
    result = assess_event_risk(
        pair, [event], as_of=now, before_minutes=60, after_minutes=30
    )
    assert result.blocked is False

