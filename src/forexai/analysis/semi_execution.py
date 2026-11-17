from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Callable

from forexai.adapters.mt5.execution import AutoExecutionSafetyError
from forexai.adapters.storage.sqlite_store import SQLiteDecisionStore
from forexai.analysis.auto_execution import (
    AutoExecutionCoordinator,
    _session_reason,
    _validate_stops,
    _validate_volume,
)
from forexai.analysis.dispatch_guards import evaluate_market_session_guard
from forexai.analysis.position_sizing import recommend_position_size
from forexai.config import Settings
from forexai.domain.execution import (
    ExecutionOutcome,
    ExecutionRecord,
    ExecutionStatus,
    SemiExecutionResult,
)
from forexai.domain.models import Direction, EntryType, Signal


LOGGER = logging.getLogger(__name__)


class SemiExecutionCoordinator:
    """User-triggered execution and read-only recheck for persisted signals."""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteDecisionStore,
        client_factory: Callable[[], object],
    ) -> None:
        self._settings = settings
        self._store = store
        self._client_factory = client_factory
        self._instruments = {
            item.canonical: item for item in settings.enabled_instruments
        }
        execution_settings = settings.model_copy(deep=True)
        execution_settings.auto_execution.enabled = True
        execution_settings.auto_execution.magic = settings.semi_execution.magic
        execution_settings.auto_execution.comment = settings.semi_execution.comment
        execution_settings.auto_execution.maximum_price_deviation_points = (
            settings.semi_execution.maximum_price_deviation_points
        )
        self._market_executor = AutoExecutionCoordinator(
            execution_settings,
            store,
            client_factory,
            reservation_status=ExecutionStatus.EXECUTION_IN_PROGRESS,
        )

    def execute(self, signal_id: str) -> SemiExecutionResult:
        if not self._settings.semi_execution.enabled:
            return self._result(signal_id, "execute", "SEMI_EXECUTION_DISABLED")
        if self._settings.auto_execution.enabled:
            return self._result(signal_id, "execute", "FULL_AUTO_MUST_BE_DISABLED")
        signal = self._store.get_signal(signal_id)
        if signal is None:
            return self._result(signal_id, "execute", "SIGNAL_NOT_FOUND")
        existing = self._store.get_execution(signal_id)
        if existing is not None:
            return self._existing_result(signal, existing, "execute")
        if self._expired(signal):
            return self._reserve_expired(signal, "execute")

        outcome = self._market_executor.execute(signal)
        record = self._store.get_execution(signal_id)
        if record is None:
            return self._from_outcome(signal, outcome, "EXECUTION_RESULT_AMBIGUOUS")
        mapped_reason = _map_reason(outcome.reason)
        if outcome.status is ExecutionStatus.EXECUTED:
            final_state = ExecutionStatus.EXECUTED
        elif outcome.status is ExecutionStatus.AMBIGUOUS:
            final_state = ExecutionStatus.AMBIGUOUS
        elif mapped_reason == "ENTRY_MISSED":
            final_state = ExecutionStatus.EXPIRED
        elif record.execution_status is ExecutionStatus.EXECUTION_IN_PROGRESS:
            # Another process owns this signal id and is still executing it.
            return self._existing_result(signal, record, "execute")
        else:
            final_state = ExecutionStatus.FAILED
        record.execution_status = final_state
        record.reason = mapped_reason
        self._store.update_execution(record)
        LOGGER.info(
            "Semi execution signal_id=%s state=%s reason=%s",
            signal_id,
            final_state.value,
            mapped_reason,
        )
        return self._from_record(signal, record, "execute")

    def recheck(self, signal_id: str) -> SemiExecutionResult:
        if not self._settings.semi_execution.enabled:
            return self._result(signal_id, "recheck", "SEMI_EXECUTION_DISABLED")
        signal = self._store.get_signal(signal_id)
        if signal is None:
            return self._result(signal_id, "recheck", "SIGNAL_NOT_FOUND")
        existing = self._store.get_execution(signal_id)
        if existing is not None and existing.execution_status in {
            ExecutionStatus.EXECUTED,
            ExecutionStatus.AMBIGUOUS,
            ExecutionStatus.EXECUTION_IN_PROGRESS,
        }:
            return self._existing_result(signal, existing, "recheck")
        if self._expired(signal):
            return self._signal_result(
                signal,
                action="recheck",
                state=ExecutionStatus.EXPIRED,
                reason="SIGNAL_EXPIRED",
            )
        if signal.entry_type is not EntryType.MARKET:
            return self._signal_result(
                signal, action="recheck", reason="UNSUPPORTED_ENTRY_TYPE"
            )
        instrument = self._instruments.get(signal.instrument)
        if instrument is None:
            return self._signal_result(
                signal, action="recheck", reason="UNKNOWN_SYMBOL"
            )
        try:
            with self._client_factory() as client:
                client.validate_permissions()
                account = client.get_account_snapshot()
                snapshot = client.get_symbol_snapshot(instrument)
                client.validate_symbol_for_market_order(snapshot, signal.direction)
                now_utc = datetime.now(timezone.utc)
                try:
                    sessions = client.get_session_catalog(
                        now_utc=now_utc
                    ).for_symbol(instrument.broker_symbol)
                except Exception as exc:
                    LOGGER.warning(
                        "Semi recheck session unavailable signal_id=%s error=%s",
                        signal.signal_id,
                        exc,
                    )
                    sessions = None
                session = evaluate_market_session_guard(
                    broker_symbol=instrument.broker_symbol,
                    sessions=sessions,
                    now_utc=now_utc,
                    broker_offset_minutes=snapshot.broker_offset_minutes,
                    snapshot=snapshot,
                    block_before_close_minutes=(
                        self._settings.market_session.block_before_close_minutes
                    ),
                    stale_tick_threshold_seconds=(
                        self._settings.market_session.stale_tick_threshold_seconds
                    ),
                )
                if session.blocked:
                    return self._signal_result(
                        signal,
                        action="recheck",
                        reason=_map_reason(_session_reason(session.status)),
                    )
                if client.get_open_positions_for_symbol(instrument.broker_symbol):
                    return self._signal_result(
                        signal,
                        action="recheck",
                        reason="EXISTING_POSITION_ON_SYMBOL",
                    )
                current_price = (
                    snapshot.ask
                    if signal.direction is Direction.BUY
                    else snapshot.bid
                )
                maximum_deviation = float(
                    signal.metadata.get("maximum_entry_deviation", 0) or 0
                )
                if maximum_deviation <= 0:
                    return self._signal_result(
                        signal,
                        action="recheck",
                        reason="ENTRY_DEVIATION_UNAVAILABLE",
                        current_price=current_price,
                    )
                if abs(current_price - signal.suggested_entry) > maximum_deviation:
                    return self._signal_result(
                        signal,
                        action="recheck",
                        state=ExecutionStatus.EXPIRED,
                        reason="ENTRY_MISSED",
                        current_price=current_price,
                    )
                recommendation = recommend_position_size(
                    account=account,
                    snapshot=snapshot,
                    direction=signal.direction,
                    entry_price=current_price,
                    stop_loss=signal.stop_loss,
                    risk_percent=self._settings.risk.risk_per_trade_percent,
                    profit_calculator=client.calculate_profit,
                )
                volume_error = _validate_volume(
                    snapshot, recommendation.suggested_volume_lots
                )
                if volume_error:
                    return self._signal_result(
                        signal,
                        action="recheck",
                        reason="INVALID_VOLUME",
                        current_price=current_price,
                    )
                if _validate_stops(signal, snapshot, current_price):
                    return self._signal_result(
                        signal,
                        action="recheck",
                        reason="INVALID_STOPS",
                        current_price=current_price,
                    )
                return self._signal_result(
                    signal,
                    action="recheck",
                    reason="ENTRY_VALID",
                    valid=True,
                    current_price=current_price,
                    final_volume=recommendation.suggested_volume_lots,
                )
        except AutoExecutionSafetyError as exc:
            return self._signal_result(
                signal, action="recheck", reason=_map_reason(exc.reason)
            )
        except Exception as exc:
            LOGGER.exception("Semi recheck failed signal_id=%s", signal.signal_id)
            return self._signal_result(
                signal,
                action="recheck",
                reason="RECHECK_UNAVAILABLE",
            )

    def _reserve_expired(
        self, signal: Signal, action: str
    ) -> SemiExecutionResult:
        record = self._new_record(signal, ExecutionStatus.EXPIRED, "SIGNAL_EXPIRED")
        if not self._store.reserve_execution(record):
            existing = self._store.get_execution(signal.signal_id)
            if existing is not None:
                return self._existing_result(signal, existing, action)
        return self._from_record(signal, record, action)

    def _expired(self, signal: Signal) -> bool:
        maximum_age = timedelta(
            minutes=self._settings.semi_execution.signal_max_age_minutes
        )
        return datetime.now(timezone.utc) - signal.signal_created_at_utc > maximum_age

    @staticmethod
    def _new_record(
        signal: Signal, status: ExecutionStatus, reason: str
    ) -> ExecutionRecord:
        return ExecutionRecord(
            signal_id=signal.signal_id,
            symbol=signal.instrument,
            direction=signal.direction,
            suggested_entry=signal.suggested_entry,
            sl=signal.stop_loss,
            tp=signal.take_profit,
            execution_status=status,
            reason=reason,
        )

    def _existing_result(
        self, signal: Signal, record: ExecutionRecord, action: str
    ) -> SemiExecutionResult:
        reason = {
            ExecutionStatus.EXECUTED: "ALREADY_EXECUTED",
            ExecutionStatus.EXECUTION_IN_PROGRESS: "EXECUTION_IN_PROGRESS",
            ExecutionStatus.AMBIGUOUS: "EXECUTION_RESULT_AMBIGUOUS",
            ExecutionStatus.EXPIRED: "SIGNAL_EXPIRED",
        }.get(record.execution_status, record.reason or "EXECUTION_ALREADY_PROCESSED")
        result = self._from_record(signal, record, action)
        result.reason = reason
        return result

    def _from_outcome(
        self, signal: Signal, outcome: ExecutionOutcome, reason: str
    ) -> SemiExecutionResult:
        return self._signal_result(
            signal,
            action="execute",
            state=outcome.status,
            reason=reason,
            current_price=outcome.actual_execution_price,
            final_volume=outcome.executed_volume or outcome.requested_volume,
            position_ticket=outcome.position_ticket,
            order_ticket=outcome.order_ticket,
            retcode=outcome.retcode,
            retcode_message=outcome.retcode_message,
        )

    def _from_record(
        self, signal: Signal, record: ExecutionRecord, action: str
    ) -> SemiExecutionResult:
        return self._signal_result(
            signal,
            action=action,
            state=record.execution_status,
            reason=record.reason or record.execution_status.value,
            valid=record.execution_status is ExecutionStatus.EXECUTED,
            current_price=record.actual_execution_price,
            final_volume=record.executed_volume or record.requested_volume,
            position_ticket=record.position_ticket,
            order_ticket=record.order_ticket,
            retcode=record.retcode,
            retcode_message=record.retcode_message,
        )

    @staticmethod
    def _result(signal_id: str, action: str, reason: str) -> SemiExecutionResult:
        return SemiExecutionResult(
            signal_id=signal_id,
            action=action,
            reason=reason,
        )

    @staticmethod
    def _signal_result(
        signal: Signal,
        *,
        action: str,
        reason: str,
        state: ExecutionStatus | None = None,
        valid: bool = False,
        current_price: float | None = None,
        final_volume: float | None = None,
        position_ticket: int | None = None,
        order_ticket: int | None = None,
        retcode: int | None = None,
        retcode_message: str | None = None,
    ) -> SemiExecutionResult:
        sizing = signal.metadata.get("position_size")
        original_volume = (
            float(sizing.get("suggested_volume_lots", 0) or 0)
            if isinstance(sizing, dict)
            else None
        )
        return SemiExecutionResult(
            signal_id=signal.signal_id,
            action=action,
            state=state,
            reason=reason,
            valid=valid,
            symbol=signal.instrument,
            direction=signal.direction,
            digits=int(signal.metadata.get("digits", 5)),
            current_price=current_price,
            suggested_entry=signal.suggested_entry,
            maximum_entry_deviation=float(
                signal.metadata.get("maximum_entry_deviation", 0) or 0
            ),
            original_volume=original_volume,
            final_volume=final_volume,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_ticket=position_ticket,
            order_ticket=order_ticket,
            retcode=retcode,
            retcode_message=retcode_message,
        )


def _map_reason(reason: str) -> str:
    return {
        "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED": "SEMI_EXECUTION_REAL_ACCOUNT_BLOCKED",
        "AUTO_EXECUTION_UNSUPPORTED_ENTRY_TYPE": "UNSUPPORTED_ENTRY_TYPE",
        "AUTO_EXECUTION_ENTRY_MISSED": "ENTRY_MISSED",
        "AUTO_EXECUTION_EXISTING_POSITION": "EXISTING_POSITION_ON_SYMBOL",
        "AUTO_EXECUTION_MARKET_CLOSED": "SUPPRESSED_MARKET_CLOSED",
        "AUTO_EXECUTION_SESSION_CLOSING": "SUPPRESSED_SESSION_CLOSING",
        "AUTO_EXECUTION_SESSION_STATUS_UNAVAILABLE": "SESSION_STATUS_UNAVAILABLE",
        "AUTO_EXECUTION_STALE_TICK": "STALE_MARKET_DATA",
        "AUTO_EXECUTION_INVALID_VOLUME": "INVALID_VOLUME",
        "AUTO_EXECUTION_INVALID_STOPS": "INVALID_STOPS",
        "AUTO_EXECUTION_ORDER_CHECK_FAILED": "ORDER_CHECK_FAILED",
        "AUTO_EXECUTION_ORDER_SEND_FAILED": "ORDER_SEND_FAILED",
        "AUTO_EXECUTION_AMBIGUOUS_RESULT": "EXECUTION_RESULT_AMBIGUOUS",
        "AUTO_EXECUTION_TERMINAL_DISCONNECTED": "MT5_DISCONNECTED",
        "AUTO_EXECUTION_TERMINAL_TRADING_DISABLED": "TRADING_PERMISSION_UNAVAILABLE",
        "AUTO_EXECUTION_ACCOUNT_TRADING_DISABLED": "TRADING_PERMISSION_UNAVAILABLE",
        "AUTO_EXECUTION_SYMBOL_TRADING_DISABLED": "SYMBOL_NOT_TRADEABLE",
        "AUTO_EXECUTION_SYMBOL_DIRECTION_BLOCKED": "SYMBOL_DIRECTION_BLOCKED",
        "AUTO_EXECUTION_MARKET_ORDER_DISABLED": "UNSUPPORTED_ENTRY_TYPE",
    }.get(reason, reason)
