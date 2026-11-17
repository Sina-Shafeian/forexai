from datetime import datetime, timezone

from forexai.adapters.telegram.notifier import ConsoleSignalNotifier
from forexai.application.orchestrator import ScanOrchestrator
from forexai.config import load_settings
from forexai.ports.economic_calendar import CalendarUnavailableError


class _UnavailableCalendar:
    def get_events(self, start, end):
        raise CalendarUnavailableError("temporary outage")


def test_orchestrator_converts_provider_outage_to_status(monkeypatch, tmp_path) -> None:
    settings = load_settings()
    settings.app.state_database = tmp_path / "state.sqlite3"
    settings.app.raw_data_directory = tmp_path / "raw"
    monkeypatch.setattr(
        "forexai.application.orchestrator.create_calendar_provider",
        lambda ignored: _UnavailableCalendar(),
    )
    orchestrator = ScanOrchestrator(settings, ConsoleSignalNotifier())
    events, available, warning = orchestrator._calendar_events(
        datetime(2026, 8, 7, tzinfo=timezone.utc)
    )
    assert events == []
    assert available is False
    assert warning == "temporary outage"


def test_console_notifier_is_explicitly_dry_run() -> None:
    assert ConsoleSignalNotifier.is_dry_run is True

