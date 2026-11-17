from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from forexai.adapters.mt5.execution import AutoExecutionSafetyError
from forexai.adapters.storage.sqlite_store import SQLiteDecisionStore
from forexai.adapters.telegram.notifier import (
    TelegramSignalNotifier,
    build_signal_keyboard,
)
from forexai.adapters.telegram.semi_callbacks import (
    SemiExecutionCallbackHandler,
    format_semi_result,
    resolve_allowed_user_id,
)
from forexai.analysis.semi_execution import SemiExecutionCoordinator
from forexai.config import load_settings
from forexai.domain.execution import ExecutionStatus, SemiExecutionResult
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
        signal: Signal,
        *,
        snapshot=None,
        sessions=None,
        positions_sequence=None,
        check_result=None,
        send_result=None,
        permissions_error=None,
        send_error=None,
        send_delay=0,
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
            comment="Done",
            order=7001,
            deal=8001,
            price=self.snapshot.ask,
        )
        self.permissions_error = permissions_error
        self.send_error = send_error
        self.send_delay = send_delay
        self.order_check_calls = 0
        self.order_send_calls = 0
        self.position_calls = 0
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

    def validate_symbol_for_market_order(self, snapshot, direction):
        return None

    def get_session_catalog(self, **kwargs):
        return _Catalog(self.sessions)

    def get_open_positions_for_symbol(self, symbol):
        self.position_calls += 1
        return self.positions_sequence.pop(0) if self.positions_sequence else []

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
        if self.send_delay:
            time.sleep(self.send_delay)
        if self.send_error:
            raise self.send_error
        return self.send_result


def _settings(tmp_path: Path):
    settings = load_settings().model_copy(deep=True)
    settings.app.state_database = tmp_path / "state.sqlite3"
    settings.auto_execution.enabled = False
    settings.semi_execution.enabled = True
    return settings


def _signal(
    direction=Direction.BUY,
    *,
    signal_id="semi-signal",
    entry_type=EntryType.MARKET,
    age_minutes=0,
):
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
        signal_created_at_utc=now - timedelta(minutes=age_minutes),
        trigger_bar_time_utc=now - timedelta(minutes=15),
        current_market_price=1.1002 if buy else 1.1000,
        suggested_entry=1.1002 if buy else 1.1000,
        entry_type=entry_type,
        entry_deviation=0,
        entry_source="test",
        reasons=["test"],
        invalidation="test",
        rule_version="test",
        metadata={
            "maximum_entry_deviation": 0.001,
            "digits": 5,
            "position_size": {
                "account_balance": 10_000,
                "account_currency": "USD",
                "risk_percent": 0.5,
                "maximum_loss": 50,
                "suggested_volume_lots": 0.05,
                "calculation_source": "mt5_calc_profit",
                "unavailable_reason": None,
            },
        },
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


def _open_sessions(minutes_to_close=120):
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


def _position(signal):
    return OpenPosition(
        ticket=9001,
        broker_symbol="EURUSD",
        direction=signal.direction,
        volume=0.05,
        price_open=1.10021 if signal.direction is Direction.BUY else 1.09999,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        magic=26081101,
    )


def _coordinator(tmp_path, signal, client):
    settings = _settings(tmp_path)
    store = SQLiteDecisionStore(settings.app.state_database)
    assert store.reserve_signal(signal)
    return SemiExecutionCoordinator(settings, store, lambda: client), store


def test_inline_keyboard_uses_short_signal_id_only() -> None:
    keyboard = build_signal_keyboard("abc123")
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert callbacks == ["exec:abc123", "check:abc123"]
    assert all(len(item.encode("utf-8")) <= 64 for item in callbacks)


def test_telegram_signal_contains_buttons_but_sends_no_order() -> None:
    class _Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    bot = _Bot()
    signal = _signal()
    notifier = TelegramSignalNotifier(
        "test-token",
        "123",
        bot=bot,
        semi_execution_enabled=True,
    )
    asyncio.run(notifier.send(signal))
    keyboard = bot.calls[0]["reply_markup"]
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert callbacks == [f"exec:{signal.signal_id}", f"check:{signal.signal_id}"]


def test_persisted_signal_is_callback_source_of_truth(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = SQLiteDecisionStore(settings.app.state_database)
    signal = _signal()
    assert store.reserve_signal(signal)
    loaded = store.get_signal(signal.signal_id)
    assert loaded is not None
    assert loaded.signal_id == signal.signal_id
    assert loaded.direction is signal.direction
    assert loaded.suggested_entry == signal.suggested_entry
    assert loaded.stop_loss == signal.stop_loss
    assert loaded.take_profit == signal.take_profit
    assert loaded.metadata["position_size"]["suggested_volume_lots"] == 0.05


def test_click_valid_demo_executes_check_then_send_and_verifies_position(tmp_path) -> None:
    signal = _signal(Direction.BUY)
    client = _FakeClient(signal)
    coordinator, store = _coordinator(tmp_path, signal, client)

    result = coordinator.execute(signal.signal_id)

    assert result.state is ExecutionStatus.EXECUTED
    assert client.order_check_calls == 1
    assert client.order_send_calls == 1
    assert client.request["price"] == client.snapshot.ask
    assert client.request["sl"] == signal.stop_loss
    assert client.request["tp"] == signal.take_profit
    assert client.request["comment"] == "ForexAI-Semi"
    assert result.position_ticket == 9001
    assert store.get_execution(signal.signal_id).execution_status is ExecutionStatus.EXECUTED


def test_successful_sell_uses_latest_bid(tmp_path) -> None:
    signal = _signal(Direction.SELL)
    client = _FakeClient(signal)
    coordinator, _ = _coordinator(tmp_path, signal, client)
    assert coordinator.execute(signal.signal_id).succeeded
    assert client.request["price"] == client.snapshot.bid


@pytest.mark.parametrize(
    ("signal_update", "client_kwargs", "expected"),
    [
        ({"signal_created_at_utc": datetime.now(timezone.utc) - timedelta(minutes=16)}, {}, "SIGNAL_EXPIRED"),
        ({"suggested_entry": 1.0900}, {}, "ENTRY_MISSED"),
        ({}, {"sessions": []}, "SUPPRESSED_MARKET_CLOSED"),
        ({}, {"sessions": _open_sessions(60)}, "SUPPRESSED_SESSION_CLOSING"),
        ({}, {"snapshot": _snapshot(age_seconds=121)}, "STALE_MARKET_DATA"),
        ({}, {"positions_sequence": [[_position(_signal())]]}, "EXISTING_POSITION_ON_SYMBOL"),
        ({"entry_type": EntryType.BUY_LIMIT}, {}, "UNSUPPORTED_ENTRY_TYPE"),
        ({"stop_loss": 1.1010}, {}, "INVALID_STOPS"),
        ({}, {"snapshot": _snapshot().model_copy(update={"trade_contract_size": 0})}, "INVALID_VOLUME"),
    ],
)
def test_semi_preflight_blocks_without_send(
    tmp_path, signal_update, client_kwargs, expected
) -> None:
    signal = _signal(signal_id=f"block-{expected}").model_copy(update=signal_update)
    client = _FakeClient(signal, **client_kwargs)
    coordinator, _ = _coordinator(tmp_path, signal, client)
    result = coordinator.execute(signal.signal_id)
    assert result.reason == expected
    if expected in {"SIGNAL_EXPIRED", "ENTRY_MISSED"}:
        assert result.state is ExecutionStatus.EXPIRED
    assert client.order_send_calls == 0


def test_real_account_is_hard_blocked_before_check_or_send(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(
        signal,
        permissions_error=AutoExecutionSafetyError(
            "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED", "not Demo"
        ),
    )
    coordinator, _ = _coordinator(tmp_path, signal, client)
    result = coordinator.execute(signal.signal_id)
    assert result.reason == "SEMI_EXECUTION_REAL_ACCOUNT_BLOCKED"
    assert client.order_check_calls == client.order_send_calls == 0


@pytest.mark.parametrize(
    ("retcode", "expected"),
    [(10019, "ORDER_CHECK_FAILED"), (10014, "ORDER_CHECK_FAILED")],
)
def test_margin_or_order_check_failure_never_sends(
    tmp_path, retcode, expected
) -> None:
    signal = _signal(signal_id=f"check-{retcode}")
    client = _FakeClient(
        signal,
        check_result=SimpleNamespace(retcode=retcode, comment="Rejected check"),
    )
    coordinator, _ = _coordinator(tmp_path, signal, client)
    result = coordinator.execute(signal.signal_id)
    assert result.reason == expected
    assert result.retcode == retcode
    assert client.order_send_calls == 0


def test_order_send_failure_and_ambiguous_result_are_not_retried(tmp_path) -> None:
    rejected = _signal(signal_id="send-rejected")
    rejected_client = _FakeClient(
        rejected,
        send_result=SimpleNamespace(
            retcode=10006, comment="Rejected", order=0, deal=0, price=0
        ),
    )
    rejected_coordinator, _ = _coordinator(tmp_path, rejected, rejected_client)
    assert rejected_coordinator.execute(rejected.signal_id).reason == "ORDER_SEND_FAILED"

    ambiguous = _signal(signal_id="send-ambiguous")
    ambiguous_client = _FakeClient(
        ambiguous, send_error=ConnectionError("response lost")
    )
    ambiguous_coordinator, _ = _coordinator(tmp_path, ambiguous, ambiguous_client)
    first = ambiguous_coordinator.execute(ambiguous.signal_id)
    second = ambiguous_coordinator.execute(ambiguous.signal_id)
    assert first.reason == second.reason == "EXECUTION_RESULT_AMBIGUOUS"
    assert ambiguous_client.order_send_calls == 1


def test_double_click_and_restart_send_only_one_order(tmp_path) -> None:
    signal = _signal()
    client = _FakeClient(signal)
    coordinator, store = _coordinator(tmp_path, signal, client)
    first = coordinator.execute(signal.signal_id)
    second = coordinator.execute(signal.signal_id)
    restarted = SemiExecutionCoordinator(
        _settings(tmp_path), store, lambda: pytest.fail("must not reconnect")
    ).execute(signal.signal_id)
    assert first.succeeded
    assert second.reason == restarted.reason == "ALREADY_EXECUTED"
    assert client.order_send_calls == 1


def test_concurrent_duplicate_callbacks_send_one_order(tmp_path) -> None:
    signal = _signal(signal_id="concurrent-click")
    client = _FakeClient(signal, send_delay=0.1)
    coordinator, _ = _coordinator(tmp_path, signal, client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(coordinator.execute, [signal.signal_id] * 2))
    assert client.order_send_calls == 1
    assert any(item.succeeded for item in results)
    assert any(
        item.reason in {"EXECUTION_IN_PROGRESS", "ALREADY_EXECUTED"}
        for item in results
    )


def test_recheck_is_read_only_and_reports_valid_or_missed(tmp_path) -> None:
    valid = _signal(signal_id="recheck-valid")
    valid_client = _FakeClient(valid, positions_sequence=[[]])
    valid_coordinator, _ = _coordinator(tmp_path, valid, valid_client)
    valid_result = valid_coordinator.recheck(valid.signal_id)
    assert valid_result.valid is True
    assert valid_result.reason == "ENTRY_VALID"
    assert valid_result.current_price == valid_client.snapshot.ask
    assert valid_result.final_volume == 0.05
    assert valid_client.order_check_calls == valid_client.order_send_calls == 0

    missed = _signal(signal_id="recheck-missed").model_copy(
        update={"suggested_entry": 1.0900}
    )
    missed_client = _FakeClient(missed, positions_sequence=[[]])
    missed_coordinator, _ = _coordinator(tmp_path, missed, missed_client)
    missed_result = missed_coordinator.recheck(missed.signal_id)
    assert missed_result.reason == "ENTRY_MISSED"
    assert missed_result.state is ExecutionStatus.EXPIRED
    assert missed_client.order_check_calls == missed_client.order_send_calls == 0


class _FakeQuery:
    def __init__(self, data, user_id=123, chat_id=123):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            replies=[],
        )

        async def reply_text(*args, **kwargs):
            self.message.replies.append((args, kwargs))

        self.message.reply_text = reply_text

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)


def test_callback_handler_executes_only_authorized_explicit_click() -> None:
    class _Coordinator:
        def __init__(self):
            self.calls = []

        def execute(self, signal_id):
            self.calls.append(("execute", signal_id))
            return SemiExecutionResult(
                signal_id=signal_id,
                action="execute",
                state=ExecutionStatus.EXECUTED,
                reason="AUTO_EXECUTION_SUCCESS",
                valid=True,
                symbol="EURUSD",
                direction=Direction.BUY,
            )

        def recheck(self, signal_id):
            self.calls.append(("recheck", signal_id))
            return SemiExecutionResult(
                signal_id=signal_id,
                action="recheck",
                reason="ENTRY_VALID",
                valid=True,
            )

    coordinator = _Coordinator()
    handler = SemiExecutionCallbackHandler(
        coordinator, allowed_chat_id="123", allowed_user_id=123
    )
    query = _FakeQuery("exec:abc123")
    asyncio.run(handler.handle(SimpleNamespace(callback_query=query), None))
    assert coordinator.calls == [("execute", "abc123")]
    assert query.message.replies
    assert query.edits

    unauthorized = _FakeQuery("exec:abc123", user_id=999)
    asyncio.run(handler.handle(SimpleNamespace(callback_query=unauthorized), None))
    assert coordinator.calls == [("execute", "abc123")]


def test_private_chat_authorization_and_persian_result_format() -> None:
    assert resolve_allowed_user_id("123", None) == 123
    assert resolve_allowed_user_id("-100123", None) is None
    result = SemiExecutionResult(
        signal_id="sample",
        action="execute",
        state=ExecutionStatus.EXECUTED,
        reason="AUTO_EXECUTION_SUCCESS",
        valid=True,
        symbol="USDCHF",
        direction=Direction.BUY,
        current_price=0.81183,
        suggested_entry=0.81180,
        original_volume=3.98,
        final_volume=3.83,
        stop_loss=0.81080,
        take_profit=0.81380,
        position_ticket=12345,
    )
    text = format_semi_result(result)
    assert "معامله با موفقیت اجرا شد" in text
    assert "3.98 Lot" in text and "3.83 Lot" in text
    assert "12345" in text
