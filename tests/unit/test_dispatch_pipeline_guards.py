from datetime import datetime, timedelta, timezone

from forexai.application.orchestrator import ScanOrchestrator
from forexai.config import load_settings
from forexai.domain.models import (
    AnalysisDecision,
    DecisionStatus,
    Direction,
    OpenPosition,
    Signal,
    SymbolSnapshot,
    SymbolTradingSession,
)


class _Notifier:
    is_dry_run = True


class _Catalog:
    def __init__(self, sessions) -> None:
        self._sessions = sessions

    def for_symbol(self, broker_symbol):
        return self._sessions


class _Client:
    def __init__(self, snapshot, positions, sessions) -> None:
        self._snapshot = snapshot
        self._positions = positions
        self._catalog = _Catalog(sessions)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get_open_positions(self):
        return self._positions

    def get_symbol_snapshot(self, instrument):
        return self._snapshot

    def get_session_catalog(self, **kwargs):
        return self._catalog


def test_existing_position_is_removed_before_dispatch(tmp_path) -> None:
    settings = load_settings().model_copy(deep=True)
    settings.app.state_database = tmp_path / "state.sqlite3"
    settings.app.raw_data_directory = tmp_path / "raw"
    orchestrator = ScanOrchestrator(settings, _Notifier())
    now = datetime(2026, 8, 10, 18, 30, tzinfo=timezone.utc)
    snapshot = SymbolSnapshot(
        canonical="XAUUSD",
        broker_symbol="XAUUSD",
        point=0.01,
        digits=2,
        bid=2400,
        ask=2400.2,
        spread_points=20,
        observed_at=now,
        broker_offset_minutes=180,
    )
    position = OpenPosition(
        ticket=1,
        broker_symbol="XAUUSD",
        direction=Direction.BUY,
        volume=0.41,
        price_open=2400,
        stop_loss=2390,
        take_profit=2420,
    )
    session = SymbolTradingSession(
        broker_symbol="XAUUSD",
        day_of_week=1,
        session_index=0,
        start_seconds=0,
        end_seconds=23 * 3600,
    )
    signal = Signal(
        signal_id="guard-test",
        instrument="XAUUSD",
        direction=Direction.SELL,
        entry_low=2400,
        entry_high=2400,
        stop_loss=2410,
        take_profit=2380,
        risk_reward=2,
        confidence_score=80,
        score_breakdown={},
        setup_timeframe="H1",
        signal_created_at_utc=now,
        trigger_bar_time_utc=now - timedelta(minutes=15),
        current_market_price=2400,
        suggested_entry=2400,
        entry_deviation=0,
        entry_source="test",
        reasons=["test"],
        invalidation="test",
        rule_version="test",
    )
    decision = AnalysisDecision(
        instrument="XAUUSD",
        status=DecisionStatus.CANDIDATE,
        reasons=["candidate"],
        signal=signal,
    )
    fake_client = _Client(snapshot, [position], [session])
    orchestrator._new_mt5_client = lambda: fake_client

    allowed = orchestrator._apply_pre_dispatch_controls(
        [signal], [decision], now
    )

    assert allowed == []
    assert decision.status is DecisionStatus.SUPPRESSED_EXISTING_POSITION
    assert decision.reasons == ["SUPPRESSED_SYMBOL_ALREADY_OPEN"]
