from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Callable

from forexai.adapters.mt5.execution import AutoExecutionSafetyError
from forexai.adapters.storage.sqlite_store import SQLiteDecisionStore
from forexai.analysis.dispatch_guards import evaluate_market_session_guard
from forexai.analysis.position_sizing import recommend_position_size
from forexai.config import Settings
from forexai.domain.execution import ExecutionOutcome, ExecutionRecord, ExecutionStatus
from forexai.domain.models import DecisionStatus, Direction, EntryType, Signal


LOGGER = logging.getLogger(__name__)

_AMBIGUOUS_RETCODES = {10008, 10010, 10012, 10031}
_KNOWN_FAILURE_RETCODES = {
    10004, 10006, 10007, 10011, 10013, 10014, 10015, 10016, 10017,
    10018, 10019, 10020, 10021, 10022, 10023, 10024, 10025, 10026,
    10027, 10028, 10029, 10030, 10032, 10033, 10034, 10035, 10036,
    10038, 10039, 10040, 10041, 10042, 10043, 10044, 10045, 10046,
}


class AutoExecutionCoordinator:
    def __init__(
        self,
        settings: Settings,
        store: SQLiteDecisionStore,
        client_factory: Callable[[], object],
        reservation_status: ExecutionStatus = ExecutionStatus.RESERVED,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client_factory = client_factory
        self._reservation_status = reservation_status
        self._instruments = {
            instrument.canonical: instrument
            for instrument in settings.enabled_instruments
        }

    def execute(self, signal: Signal) -> ExecutionOutcome:
        if not self._settings.auto_execution.enabled:
            return ExecutionOutcome(
                execution_enabled=False,
                reason="AUTO_EXECUTION_DISABLED",
            )

        existing = self._store.get_execution(signal.signal_id)
        if existing is not None:
            return self._outcome_from_existing(existing)

        record = ExecutionRecord(
            signal_id=signal.signal_id,
            symbol=signal.instrument,
            direction=signal.direction,
            suggested_entry=signal.suggested_entry,
            sl=signal.stop_loss,
            tp=signal.take_profit,
            execution_status=self._reservation_status,
        )
        if not self._store.reserve_execution(record):
            existing = self._store.get_execution(signal.signal_id)
            if existing is not None:
                return self._outcome_from_existing(existing)
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                execution_enabled=True,
                reason="AUTO_EXECUTION_DUPLICATE_SIGNAL",
            )

        if signal.entry_type is not EntryType.MARKET:
            return self._finish(
                record,
                ExecutionStatus.BLOCKED,
                "AUTO_EXECUTION_UNSUPPORTED_ENTRY_TYPE",
            )

        instrument = self._instruments.get(signal.instrument)
        if instrument is None:
            return self._finish(
                record,
                ExecutionStatus.BLOCKED,
                "AUTO_EXECUTION_UNKNOWN_SYMBOL",
            )

        order_send_started = False
        try:
            with self._client_factory() as client:
                permissions = client.validate_permissions()
                account = client.get_account_snapshot()
                snapshot = client.get_symbol_snapshot(instrument)
                now_utc = datetime.now(timezone.utc)
                try:
                    catalog = client.get_session_catalog(now_utc=now_utc)
                    sessions = catalog.for_symbol(instrument.broker_symbol)
                except Exception as exc:
                    LOGGER.warning(
                        "Execution session status unavailable signal_id=%s error=%s",
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
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        _session_reason(session.status),
                    )

                # BUY uses the refreshed Ask; SELL uses the refreshed Bid.
                actual_price = (
                    snapshot.ask
                    if signal.direction is Direction.BUY
                    else snapshot.bid
                )
                maximum_deviation = float(
                    signal.metadata.get("maximum_entry_deviation", 0) or 0
                )
                if maximum_deviation <= 0:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        "AUTO_EXECUTION_ENTRY_DEVIATION_UNAVAILABLE",
                    )
                if abs(actual_price - signal.suggested_entry) > maximum_deviation:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        "AUTO_EXECUTION_ENTRY_MISSED",
                    )

                recommendation = recommend_position_size(
                    account=account,
                    snapshot=snapshot,
                    direction=signal.direction,
                    entry_price=actual_price,
                    stop_loss=signal.stop_loss,
                    risk_percent=self._settings.risk.risk_per_trade_percent,
                    profit_calculator=client.calculate_profit,
                )
                signal.metadata["position_size"] = recommendation.model_dump()
                volume = recommendation.suggested_volume_lots
                record.requested_volume = volume
                volume_error = _validate_volume(snapshot, volume)
                if volume_error:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        "AUTO_EXECUTION_INVALID_VOLUME",
                        message=volume_error,
                    )
                stops_error = _validate_stops(signal, snapshot, actual_price)
                if stops_error:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        "AUTO_EXECUTION_INVALID_STOPS",
                        message=stops_error,
                    )

                request = client.build_market_request(
                    signal=signal,
                    snapshot=snapshot,
                    price=actual_price,
                    volume=volume,
                    magic=self._settings.auto_execution.magic,
                    comment=self._settings.auto_execution.comment,
                    deviation_points=(
                        self._settings.auto_execution.maximum_price_deviation_points
                    ),
                )
                record.margin_required = client.calculate_margin(
                    signal.direction,
                    instrument.broker_symbol,
                    volume,
                    actual_price,
                )
                LOGGER.info(
                    "Execution preflight signal_id=%s account=%s terminal=%s "
                    "balance=%.2f equity=%.2f margin=%.2f free_margin=%.2f "
                    "margin_level=%.2f required_margin=%s",
                    signal.signal_id,
                    account.trade_mode,
                    permissions["terminal"],
                    account.balance,
                    account.equity,
                    account.margin,
                    account.free_margin,
                    account.margin_level,
                    record.margin_required,
                )

                # Read once immediately before order_check.
                first_positions = client.get_open_positions_for_symbol(
                    instrument.broker_symbol
                )
                if first_positions:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        "AUTO_EXECUTION_EXISTING_POSITION",
                    )
                check = client.check_market_order(request)
                check_retcode = _field(check, "retcode")
                check_message = str(_field(check, "comment") or "")
                LOGGER.info(
                    "MT5 order_check signal_id=%s retcode=%s comment=%s",
                    signal.signal_id,
                    check_retcode,
                    check_message,
                )
                if check is None or check_retcode != 0:
                    record.retcode = _int_or_none(check_retcode)
                    return self._finish(
                        record,
                        ExecutionStatus.CHECK_FAILED,
                        "AUTO_EXECUTION_ORDER_CHECK_FAILED",
                        message=check_message or "order_check returned no successful result",
                    )

                # Revalidate time/session and positions after order_check and
                # immediately before order_send to narrow the race window.
                final_session = evaluate_market_session_guard(
                    broker_symbol=instrument.broker_symbol,
                    sessions=sessions,
                    now_utc=datetime.now(timezone.utc),
                    broker_offset_minutes=snapshot.broker_offset_minutes,
                    snapshot=snapshot,
                    block_before_close_minutes=(
                        self._settings.market_session.block_before_close_minutes
                    ),
                    stale_tick_threshold_seconds=(
                        self._settings.market_session.stale_tick_threshold_seconds
                    ),
                )
                if final_session.blocked:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        _session_reason(final_session.status),
                    )
                second_positions = client.get_open_positions_for_symbol(
                    instrument.broker_symbol
                )
                if second_positions:
                    return self._finish(
                        record,
                        ExecutionStatus.BLOCKED,
                        "AUTO_EXECUTION_EXISTING_POSITION",
                    )

                order_send_started = True
                result = client.send_market_order(request)
                send_retcode = _field(result, "retcode")
                send_message = str(_field(result, "comment") or "")
                record.retcode = _int_or_none(send_retcode)
                record.retcode_message = send_message
                record.order_ticket = _positive_int(_field(result, "order"))
                record.deal_ticket = _positive_int(_field(result, "deal"))
                result_price = _positive_float(_field(result, "price"))
                if result_price is not None:
                    record.actual_execution_price = result_price
                LOGGER.info(
                    "MT5 order_send signal_id=%s retcode=%s comment=%s "
                    "order=%s deal=%s",
                    signal.signal_id,
                    send_retcode,
                    send_message,
                    record.order_ticket,
                    record.deal_ticket,
                )
                if result is None or send_retcode in _AMBIGUOUS_RETCODES:
                    return self._finish(
                        record,
                        ExecutionStatus.AMBIGUOUS,
                        "AUTO_EXECUTION_AMBIGUOUS_RESULT",
                        message=send_message or "order_send result is ambiguous",
                    )
                if send_retcode in _KNOWN_FAILURE_RETCODES:
                    return self._finish(
                        record,
                        ExecutionStatus.SEND_FAILED,
                        "AUTO_EXECUTION_ORDER_SEND_FAILED",
                        message=send_message,
                    )
                if send_retcode != 10009:
                    return self._finish(
                        record,
                        ExecutionStatus.AMBIGUOUS,
                        "AUTO_EXECUTION_AMBIGUOUS_RESULT",
                        message=send_message or f"Unknown retcode: {send_retcode}",
                    )

                positions = client.get_open_positions_for_symbol(
                    instrument.broker_symbol
                )
                position = _verified_position(
                    positions,
                    signal,
                    snapshot,
                    volume,
                    self._settings.auto_execution.magic,
                )
                if position is None:
                    return self._finish(
                        record,
                        ExecutionStatus.AMBIGUOUS,
                        "AUTO_EXECUTION_AMBIGUOUS_RESULT",
                        message="Position verification failed after successful retcode",
                    )
                record.position_ticket = position.ticket
                record.actual_execution_price = position.price_open
                record.executed_volume = position.volume
                record.executed_at_utc = datetime.now(timezone.utc)
                return self._finish(
                    record,
                    ExecutionStatus.EXECUTED,
                    "AUTO_EXECUTION_SUCCESS",
                )
        except AutoExecutionSafetyError as exc:
            return self._finish(
                record,
                ExecutionStatus.BLOCKED,
                exc.reason,
                message=str(exc),
            )
        except Exception as exc:
            LOGGER.exception("Auto execution failed signal_id=%s", signal.signal_id)
            status = (
                ExecutionStatus.AMBIGUOUS
                if order_send_started
                else ExecutionStatus.BLOCKED
            )
            reason = (
                "AUTO_EXECUTION_AMBIGUOUS_RESULT"
                if order_send_started
                else "AUTO_EXECUTION_PREFLIGHT_FAILED"
            )
            return self._finish(record, status, reason, message=str(exc))

    def _finish(
        self,
        record: ExecutionRecord,
        status: ExecutionStatus,
        reason: str,
        *,
        message: str | None = None,
    ) -> ExecutionOutcome:
        record.execution_status = status
        record.reason = reason
        if message:
            record.retcode_message = message
        self._store.update_execution(record)
        LOGGER.info(
            "Auto execution signal_id=%s status=%s reason=%s",
            record.signal_id,
            status.value,
            reason,
        )
        return _outcome(record, execution_enabled=True)

    @staticmethod
    def _outcome_from_existing(record: ExecutionRecord) -> ExecutionOutcome:
        if record.execution_status is ExecutionStatus.EXECUTED:
            return _outcome(record, execution_enabled=True)
        return ExecutionOutcome(
            status=ExecutionStatus.BLOCKED,
            execution_enabled=True,
            reason="AUTO_EXECUTION_DUPLICATE_SIGNAL",
            actual_execution_price=record.actual_execution_price,
            requested_volume=record.requested_volume,
            executed_volume=record.executed_volume,
            sl=record.sl,
            tp=record.tp,
            order_ticket=record.order_ticket,
            deal_ticket=record.deal_ticket,
            position_ticket=record.position_ticket,
            retcode=record.retcode,
            retcode_message=record.retcode_message,
        )


def _validate_volume(snapshot, volume: float) -> str | None:
    if (
        snapshot.trade_contract_size <= 0
        or snapshot.trade_tick_size <= 0
        or (snapshot.trade_tick_value_loss or snapshot.trade_tick_value) <= 0
    ):
        return "Required contract/tick specifications are unavailable"
    if snapshot.volume_min <= 0 or snapshot.volume_step <= 0:
        return "Invalid symbol volume limits"
    if volume < snapshot.volume_min or volume <= 0:
        return "Calculated volume is below volume_min"
    if snapshot.volume_max > 0 and volume > snapshot.volume_max:
        return "Calculated volume exceeds volume_max"
    units = Decimal(str(volume)) / Decimal(str(snapshot.volume_step))
    if units != units.to_integral_value():
        return "Calculated volume is not aligned to volume_step"
    return None


def _validate_stops(signal: Signal, snapshot, price: float) -> str | None:
    sl = signal.stop_loss
    tp = signal.take_profit
    if sl <= 0 or tp <= 0:
        return "Both SL and TP are required"
    if signal.direction is Direction.BUY and not (sl < price < tp):
        return "BUY requires SL < Ask < TP"
    if signal.direction is Direction.SELL and not (tp < price < sl):
        return "SELL requires TP < Bid < SL"
    minimum = snapshot.trade_stops_level * snapshot.point
    tolerance = snapshot.point / 10
    if minimum > 0 and (
        abs(price - sl) + tolerance < minimum
        or abs(tp - price) + tolerance < minimum
    ):
        return "SL/TP violate the broker minimum stops distance"
    return None


def _verified_position(positions, signal, snapshot, volume, magic):
    tolerance = max(snapshot.point * 1.5, 1e-9)
    volume_tolerance = max(snapshot.volume_step / 2, 1e-9)
    for position in positions:
        if position.broker_symbol.upper() != snapshot.broker_symbol.upper():
            continue
        if position.direction is not signal.direction or position.magic != magic:
            continue
        if abs(position.volume - volume) > volume_tolerance:
            continue
        if position.price_open <= 0:
            continue
        if abs(position.stop_loss - signal.stop_loss) > tolerance:
            continue
        if abs(position.take_profit - signal.take_profit) > tolerance:
            continue
        return position
    return None


def _session_reason(status: DecisionStatus | None) -> str:
    return {
        DecisionStatus.SUPPRESSED_MARKET_CLOSED: "AUTO_EXECUTION_MARKET_CLOSED",
        DecisionStatus.SUPPRESSED_SESSION_CLOSING: "AUTO_EXECUTION_SESSION_CLOSING",
        DecisionStatus.STALE_MARKET_DATA: "AUTO_EXECUTION_STALE_TICK",
        DecisionStatus.SESSION_STATUS_UNAVAILABLE: (
            "AUTO_EXECUTION_SESSION_STATUS_UNAVAILABLE"
        ),
    }.get(status, "AUTO_EXECUTION_SESSION_BLOCKED")


def _field(value, name: str):
    return getattr(value, name, None) if value is not None else None


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive_int(value) -> int | None:
    parsed = _int_or_none(value)
    return parsed if parsed and parsed > 0 else None


def _positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _outcome(record: ExecutionRecord, *, execution_enabled: bool) -> ExecutionOutcome:
    return ExecutionOutcome(
        status=record.execution_status,
        reason=record.reason or record.execution_status.value,
        execution_enabled=execution_enabled,
        actual_execution_price=record.actual_execution_price,
        requested_volume=record.requested_volume,
        executed_volume=record.executed_volume,
        sl=record.sl,
        tp=record.tp,
        order_ticket=record.order_ticket,
        deal_ticket=record.deal_ticket,
        position_ticket=record.position_ticket,
        retcode=record.retcode,
        retcode_message=record.retcode_message,
    )
