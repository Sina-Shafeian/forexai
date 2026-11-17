from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from forexai.adapters.mt5.execution import AutoExecutionSafetyError
from forexai.adapters.storage.sqlite_store import SQLiteDecisionStore
from forexai.analysis.auto_execution import AutoExecutionCoordinator
from forexai.application.orchestrator import ScanOrchestrator
from forexai.config import load_settings
from forexai.domain.execution import ExecutionStatus
from forexai.domain.models import (
    AccountSnapshot,
    Direction,
    EntryType,
    OpenPosition,
    Signal,
    SymbolSnapshot,
    SymbolTradingSession,
)


class _Catalog:
    def __init__(self, sessions):
        self.sessions = sessions

    def for_symbol(self, symbol):
        return self.sessions


class _FakeClient:
    def __init__(
        self,
        *,
        signal: Signal,
        snapshot: SymbolSnapshot | None = None,
        sessions=None,
        positions_sequence=None,
        check_result=None,
        send_result=None,
        permissions_error: AutoExecutionSafetyError | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self.signal = signal
        self.snapshot = snapshot or _snapshot()
        self.sessions = sessions if sessions is not None else _open_sessions()
        self.positions_sequence = list(
            positions_sequence
            if positions_sequence is not None
            else [[], [], [_position(signal)]]
        )
        self.check_result = check_result or SimpleNamespace(retcode=0, comment="Done")
        self.send_result = send_result or SimpleNamespace(
            retcode=10009,
            comment="Request completed",
            order=7001,
            deal=8001,
            price=self.snapshot.ask,
            volume=0.05,
        )
        self.permissions_error = permissions_error
        self.send_error = send_error
        self.order_send_calls = 0
        self.order_check_calls = 0
        self.positions_calls = 0
        self.request = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def validate_permissions(self):
        if self.permissions_error:
            raise self.permissions_error
        return {"terminal": {"connected": True, "trade_allowed": True}}

    def get_account_snapshot(self):
        return AccountSnapshot(
            login=1,
            server="Demo",
            currency="USD",
            balance=10_000,
            leverage=100,
            trade_mode="DEMO",
            equity=10_000,
            margin=0,
            free_margin=10_000,
            margin_level=0,
            trade_allowed=True,
            trade_expert=True,
        )

    def get_symbol_snapshot(self, instrument):
        return self.snapshot

    def get_session_catalog(self, **kwargs):
        return _Catalog(self.sessions)

    def get_open_positions_for_symbol(self, symbol):
        self.positions_calls += 1
        if self.positions_sequence:
            return self.positions_sequence.pop(0)
        return []

    def calculate_profit(self, *args):
        return -1000.0

    def build_market_request(self, **kwargs):
        signal = kwargs["signal"]
        self.request = {
            "symbol": kwargs["snapshot"].broker_symbol,
            "type": signal.direction.value,
            "price": kwargs["price"],
            "volume": kwargs["volume"],
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "magic": kwargs["magic"],
            "comment": kwargs["comment"],
        }
        return self.request

    def calculate_margin(self, *args):
        return 50.0

    def check_market_order(self, request):
        self.order_check_calls += 1
        return self.check_result

    def send_market_order(self, request):
        self.order_send_calls += 1
        if self.send_error:
            raise self.send_error
        return self.send_result


def _settings(tmp_path: Path, *, enabled: bool = True):
    settings = load_settings().model_copy(deep=True)
    settings.app.state_database = tmp_path / "state.sqlite3"
    settings.auto_execution.enabled = enabled
    return settings


def _signal(direction=Direction.BUY, *, entry_type=EntryType.MARKET, signal_id="sig-1"):
    now = datetime.now(timezone.utc)
    buy = direction is Direction.BUY
    return Signal(
        signal_id=signal_id,
        instrument="EURUSD",
        direction=direction,
        entry_low=1.1002 if buy else 1.1000,
        entry_high=1.1002 if buy else 1.1000,
        stop_loss=1.0992 if buy else 1.1010,
        take_profit=1.1022 if buy else 1.0980,
        risk_reward=2,
        confidence_score=80,
        score_breakdown={},
        setup_timeframe="H1",
        signal_created_at_utc=now,
        trigger_bar_time_utc=now - timedelta(minutes=15),
        current_market_price=1.1002 if buy else 1.1000,
        suggested_entry=1.1002 if buy else 1.1000,
        entry_type=entry_type,
        entry_deviation=0,
        entry_source="test",
        reasons=["test"],
        invalidation="test",
        rule_version="test",
        metadata={"maximum_entry_deviation": 0.001, "digits": 5},
    )


def _snapshot(*, age_seconds=0):
    return SymbolSnapshot(
        canonical="EURUSD",
        broker_symbol="EURUSD",
        point=0.00001,
        digits=5,
        bid=1.1000,
        ask=1.1002,
        spread_points=20,
        observed_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        trade_contract_size=100_000,
        trade_tick_size=0.00001,
        trade_tick_value=1,
        trade_tick_value_loss=1,
        volume_min=0.01,
        volume_max=100,
        volume_step=0.01,
        trade_mode=4,
        trade_execution_mode=1,
        filling_mode=1,
        order_mode=1,
        trade_stops_level=10,
    )


def _open_sessions(*, minutes_to_close=120):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    end = now + timedelta(minutes=minutes_to_close)
    return [
        SymbolTradingSession(
            broker_symbol="EURUSD",
            day_of_week=(start.weekday() + 1) % 7,
            session_index=0,
            start_seconds=start.hour * 3600 + start.minute * 60 + start.second,
            end_seconds=end.hour * 3600 + end.minute * 60 + end.second,
        )
    ]


def _position(signal, *, magic=26081101):
    return OpenPosition(
        ticket=9001,
        broker_symbol="EURUSD",
        direction=signal.direction,
        volume=0.05,
        price_open=1.10021 if signal.direction is Direction.BUY else 1.09999,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        magic=magic,
    )


def _run(tmp_path, client, signal=None, *, enabled=True):
    signal = signal or client.signal
    settings = _settings(tmp_path, enabled=enabled)
    store = SQLiteDecisionStore(settings.app.state_database)
    coordinator = AutoExecutionCoordinator(settings, store, lambda: client)
    return coordinator.execute(signal), store


def test_disabled_never_creates_client_or_sends(tmp_path) -> None:
    settings = _settings(tmp_path, enabled=False)
    store = SQLiteDecisionStore(settings.app.state_database)
    signal = _signal()
    client = _FakeClient(signal=signal)
    factory_calls = 0

    def client_factory():
        nonlocal factory_calls
        factory_calls += 1
        return client

    coordinator = AutoExecutionCoordinator(
        settings, store, client_factory
    )
    outcome = coordinator.execute(signal)
    assert outcome.execution_enabled is False
    assert factory_calls == 0
    assert client.order_check_calls == 0
    assert client.order_send_calls == 0
    assert store.get_execution("sig-1") is None


def test_signal_only_runtime_sends_telegram_without_execution_adapter(tmp_path) -> None:
    class _Notifier:
        is_dry_run = False

        def __init__(self) -> None:
            self.sent = []

        async def send(self, signal):
            self.sent.append(signal.signal_id)
            return "telegram:1"

    settings = _settings(tmp_path, enabled=False)
    notifier = _Notifier()
    orchestrator = ScanOrchestrator(settings, notifier)
    orchestrator._auto_executor = SimpleNamespace(
        execute=lambda signal: pytest.fail(
            "Signal-only runtime must not invoke the execution adapter"
        )
    )
    signal = _signal(signal_id="signal-only-runtime")

    asyncio.run(orchestrator._send_signal(signal))

    assert notifier.sent == [signal.signal_id]
    assert "auto_execution" not in signal.metadata


def test_run_local_starts_one_plain_signal_bot_instance() -> None:
    script = Path(__file__).resolve().parents[2].joinpath(
        "scripts/run_local.ps1"
    ).read_text(encoding="utf-8")
    assert script.count("uv run forexai run") == 1
    assert "auto_execution" not in script.lower()
    assert "FOREXAI" not in script


def test_real_account_is_hard_blocked(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(
        signal=signal,
        permissions_error=AutoExecutionSafetyError(
            "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED", "not Demo"
        ),
    )
    outcome, _ = _run(tmp_path, client)
    assert outcome.reason == "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED"
    assert client.order_send_calls == 0


def test_valid_demo_buy_uses_ask_and_persists_sl_tp(tmp_path) -> None:
    signal = _signal(Direction.BUY)
    client = _FakeClient(signal=signal)
    outcome, store = _run(tmp_path, client)
    record = store.get_execution(signal.signal_id)
    assert outcome.status is ExecutionStatus.EXECUTED
    assert client.request["price"] == client.snapshot.ask
    assert client.request["sl"] == signal.stop_loss
    assert client.request["tp"] == signal.take_profit
    assert record.position_ticket == 9001
    assert record.executed_volume == 0.05


def test_valid_demo_sell_uses_bid(tmp_path) -> None:
    signal = _signal(Direction.SELL)
    client = _FakeClient(signal=signal)
    outcome, _ = _run(tmp_path, client)
    assert outcome.status is ExecutionStatus.EXECUTED
    assert client.request["price"] == client.snapshot.bid


@pytest.mark.parametrize(
    ("client_changes", "signal_changes", "expected"),
    [
        ({"positions_sequence": [[_position(_signal())]]}, {}, "AUTO_EXECUTION_EXISTING_POSITION"),
        ({"sessions": []}, {}, "AUTO_EXECUTION_MARKET_CLOSED"),
        ({"sessions": _open_sessions(minutes_to_close=60)}, {}, "AUTO_EXECUTION_SESSION_CLOSING"),
        ({"snapshot": _snapshot(age_seconds=121)}, {}, "AUTO_EXECUTION_STALE_TICK"),
        ({}, {"suggested_entry": 1.0900}, "AUTO_EXECUTION_ENTRY_MISSED"),
        ({"snapshot": _snapshot().model_copy(update={"trade_contract_size": 0})}, {}, "AUTO_EXECUTION_INVALID_VOLUME"),
        ({}, {"stop_loss": 1.1010}, "AUTO_EXECUTION_INVALID_STOPS"),
    ],
)
def test_preflight_blocks_without_send(
    tmp_path, client_changes, signal_changes, expected
) -> None:
    signal = _signal().model_copy(update=signal_changes)
    client = _FakeClient(signal=signal, **client_changes)
    outcome, _ = _run(tmp_path, client, signal)
    assert outcome.reason == expected
    assert client.order_send_calls == 0


def test_unsupported_pending_entry_is_blocked(tmp_path) -> None:
    signal = _signal(entry_type=EntryType.BUY_LIMIT)
    client = _FakeClient(signal=signal)
    outcome, _ = _run(tmp_path, client)
    assert outcome.reason == "AUTO_EXECUTION_UNSUPPORTED_ENTRY_TYPE"
    assert client.order_send_calls == 0


def test_order_check_failure_never_sends(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(
        signal=signal,
        check_result=SimpleNamespace(retcode=10019, comment="No money"),
    )
    outcome, _ = _run(tmp_path, client)
    assert outcome.status is ExecutionStatus.CHECK_FAILED
    assert outcome.retcode == 10019
    assert client.order_send_calls == 0


def test_order_send_rejection_is_reported(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(
        signal=signal,
        send_result=SimpleNamespace(
            retcode=10006, comment="Rejected", order=0, deal=0
        ),
    )
    outcome, _ = _run(tmp_path, client)
    assert outcome.status is ExecutionStatus.SEND_FAILED
    assert outcome.retcode == 10006
    assert client.order_send_calls == 1


def test_unknown_send_retcode_is_ambiguous_and_not_retried(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(
        signal=signal,
        send_result=SimpleNamespace(
            retcode=19999, comment="Unknown", order=0, deal=0, price=0
        ),
    )
    outcome, _ = _run(tmp_path, client)
    assert outcome.status is ExecutionStatus.AMBIGUOUS
    assert outcome.reason == "AUTO_EXECUTION_AMBIGUOUS_RESULT"
    assert client.order_send_calls == 1


def test_ambiguous_send_is_persisted_and_never_retried(tmp_path) -> None:
    signal = _signal()
    first = _FakeClient(signal=signal, send_error=ConnectionError("lost response"))
    outcome, store = _run(tmp_path, first)
    assert outcome.status is ExecutionStatus.AMBIGUOUS
    assert first.order_send_calls == 1

    second = _FakeClient(signal=signal)
    settings = _settings(tmp_path)
    restarted = AutoExecutionCoordinator(
        settings,
        SQLiteDecisionStore(settings.app.state_database),
        lambda: second,
    )
    duplicate = restarted.execute(signal)
    assert duplicate.reason == "AUTO_EXECUTION_DUPLICATE_SIGNAL"
    assert second.order_send_calls == 0
    assert store.get_execution(signal.signal_id).execution_status is ExecutionStatus.AMBIGUOUS


def test_successful_signal_is_not_reexecuted_after_restart(tmp_path) -> None:
    signal = _signal()
    first = _FakeClient(signal=signal)
    outcome, _ = _run(tmp_path, first)
    assert outcome.status is ExecutionStatus.EXECUTED

    second = _FakeClient(signal=signal)
    settings = _settings(tmp_path)
    restarted = AutoExecutionCoordinator(
        settings,
        SQLiteDecisionStore(settings.app.state_database),
        lambda: second,
    )
    persisted = restarted.execute(signal)
    assert persisted.status is ExecutionStatus.EXECUTED
    assert persisted.position_ticket == 9001
    assert second.order_send_calls == 0


def test_position_is_rechecked_after_order_check_and_before_send(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(
        signal=signal,
        positions_sequence=[[], [_position(signal)]],
    )
    outcome, _ = _run(tmp_path, client)
    assert outcome.reason == "AUTO_EXECUTION_EXISTING_POSITION"
    assert client.order_check_calls == 1
    assert client.positions_calls == 2
    assert client.order_send_calls == 0
