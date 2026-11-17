from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from forexai.adapters.macro.factory import create_calendar_provider
from forexai.adapters.mt5.client import MT5ReadOnlyClient
from forexai.adapters.mt5.execution import MT5DemoExecutionClient
from forexai.adapters.storage.parquet_store import ParquetMarketStore
from forexai.adapters.storage.sqlite_store import SQLiteDecisionStore
from forexai.analysis.correlation_guard import select_diversified_signals
from forexai.analysis.auto_execution import AutoExecutionCoordinator
from forexai.analysis.currency_relative import compute_currency_strength
from forexai.analysis.dispatch_guards import (
    DispatchGuardResult,
    evaluate_market_session_guard,
    evaluate_open_position_guard,
)
from forexai.analysis.event_guard import EventAssessment, assess_event_risk
from forexai.analysis.position_sizing import recommend_position_size
from forexai.analysis.signal_engine import analyze_instrument
from forexai.config import Settings
from forexai.domain.models import AnalysisDecision, DecisionStatus, EconomicEvent
from forexai.ports.economic_calendar import CalendarUnavailableError
from forexai.ports.notifier import SignalNotifier


LOGGER = logging.getLogger(__name__)


class ScanOrchestrator:
    def __init__(self, settings: Settings, notifier: SignalNotifier) -> None:
        self._settings = settings
        self._notifier = notifier
        self._decisions = SQLiteDecisionStore(settings.app.state_database)
        self._market_store = ParquetMarketStore(settings.app.raw_data_directory)
        self._last_scanned_trigger: dict[str, datetime] = {}
        self._auto_executor = AutoExecutionCoordinator(
            settings, self._decisions, self._new_execution_client
        )

    async def scan_once(self) -> list[AnalysisDecision]:
        as_of = datetime.now(timezone.utc)
        settings = self._settings
        client = self._new_mt5_client()
        frames_by_symbol: dict[str, dict[str, pd.DataFrame]] = {}
        snapshots = {}
        with client:
            for instrument in settings.enabled_instruments:
                try:
                    snapshots[instrument.canonical] = client.get_symbol_snapshot(instrument)
                    frames_by_symbol[instrument.canonical] = {}
                    for timeframe in dict.fromkeys(
                        settings.scan.timeframes.model_dump().values()
                    ):
                        frame = client.get_closed_bars(
                            instrument, timeframe, settings.scan.bars_per_timeframe
                        )
                        frames_by_symbol[instrument.canonical][timeframe] = frame
                        self._market_store.write_snapshot(
                            instrument.canonical, timeframe, frame
                        )
                except Exception as exc:
                    LOGGER.exception("Market data failed for %s", instrument.canonical)
                    decision = AnalysisDecision(
                        instrument=instrument.canonical,
                        status=DecisionStatus.NO_TRADE,
                        reasons=[f"Market data unavailable: {exc}"],
                    )
                    self._decisions.record_decision(decision)

        h1_frames = {
            symbol: frames[settings.scan.timeframes.setup]
            for symbol, frames in frames_by_symbol.items()
            if settings.scan.timeframes.setup in frames
        }
        strengths = compute_currency_strength(
            settings.enabled_instruments, h1_frames
        )
        events, calendar_available, calendar_error = self._calendar_events(as_of)

        decisions: list[AnalysisDecision] = []
        for instrument in settings.enabled_instruments:
            frames = frames_by_symbol.get(instrument.canonical)
            snapshot = snapshots.get(instrument.canonical)
            if not frames or snapshot is None:
                continue
            trigger_time = frames[settings.scan.timeframes.trigger].index[-1].to_pydatetime()
            if self._last_scanned_trigger.get(instrument.canonical) == trigger_time:
                continue
            self._last_scanned_trigger[instrument.canonical] = trigger_time

            assessment = assess_event_risk(
                instrument,
                events,
                as_of=as_of,
                before_minutes=settings.calendar.high_impact_before_minutes,
                after_minutes=settings.calendar.high_impact_after_minutes,
            )
            if calendar_error and assessment.warning is None:
                assessment = EventAssessment(
                    blocked=False,
                    warning=calendar_error,
                    relevant_events=assessment.relevant_events,
                )
            try:
                decision = analyze_instrument(
                    instrument=instrument,
                    frames=frames,
                    snapshot=snapshot,
                    currency_strengths=strengths,
                    event_assessment=assessment,
                    calendar_available=calendar_available,
                    settings=settings,
                    as_of=as_of,
                )
            except Exception as exc:
                LOGGER.exception("Analysis failed for %s", instrument.canonical)
                decision = AnalysisDecision(
                    instrument=instrument.canonical,
                    trigger_bar_time=trigger_time,
                    status=DecisionStatus.NO_TRADE,
                    reasons=[f"Analysis error: {exc}"],
                )
            decisions.append(decision)

        already_sent = self._decisions.sent_count_for_utc_day(as_of)
        remaining_capacity = max(
            0, settings.scoring.max_alerts_per_utc_day - already_sent
        )
        candidate_signals = [
            decision.signal
            for decision in decisions
            if decision.status is DecisionStatus.CANDIDATE and decision.signal is not None
        ]
        selected, correlation_rejections = select_diversified_signals(
            candidate_signals,
            settings.enabled_instruments,
            h1_frames,
            lookback_bars=settings.scoring.gates.correlation_lookback_bars,
            maximum_directional_correlation=(
                settings.scoring.gates.maximum_directional_correlation
            ),
            max_same_currency_direction_alerts=(
                settings.scoring.gates.max_same_currency_direction_alerts
            ),
            limit=remaining_capacity,
        )
        rejected_by_id = {
            rejection.signal_id: rejection.reason for rejection in correlation_rejections
        }
        for decision in decisions:
            if decision.signal and decision.signal.signal_id in rejected_by_id:
                reason = rejected_by_id[decision.signal.signal_id]
                decision.status = DecisionStatus.REJECTED
                decision.signal.status = DecisionStatus.REJECTED
                decision.reasons = [reason]
        selected = self._apply_pre_dispatch_controls(
            selected, decisions, datetime.now(timezone.utc)
        )
        for decision in decisions:
            self._decisions.record_decision(decision)
        for signal in selected:
            await self._send_signal(signal)
        return decisions

    def _new_mt5_client(self) -> MT5ReadOnlyClient:
        settings = self._settings
        return MT5ReadOnlyClient(
            settings.mt5.terminal_path,
            require_demo_account=settings.mt5.require_demo_account,
            timeout_ms=settings.mt5.initialize_timeout_ms,
            minimum_history_bars=settings.strategy.minimum_history_bars,
            broker_clock_state_path=settings.mt5.broker_clock_state_path,
            offset_round_minutes=settings.mt5.offset_round_minutes,
            maximum_offset_hours=settings.mt5.maximum_offset_hours,
            maximum_tick_age_seconds=settings.mt5.maximum_tick_age_seconds,
            future_tolerance_seconds=settings.mt5.future_tolerance_seconds,
            session_export_relative_path=(
                settings.market_session.export_relative_path
            ),
            session_export_max_age_minutes=(
                settings.market_session.export_max_age_minutes
            ),
        )

    def _new_execution_client(self) -> MT5DemoExecutionClient:
        settings = self._settings
        return MT5DemoExecutionClient(
            settings.mt5.terminal_path,
            timeout_ms=settings.mt5.initialize_timeout_ms,
            minimum_history_bars=settings.strategy.minimum_history_bars,
            broker_clock_state_path=settings.mt5.broker_clock_state_path,
            offset_round_minutes=settings.mt5.offset_round_minutes,
            maximum_offset_hours=settings.mt5.maximum_offset_hours,
            maximum_tick_age_seconds=settings.mt5.maximum_tick_age_seconds,
            future_tolerance_seconds=settings.mt5.future_tolerance_seconds,
            session_export_relative_path=(
                settings.market_session.export_relative_path
            ),
            session_export_max_age_minutes=(
                settings.market_session.export_max_age_minutes
            ),
        )

    def _apply_pre_dispatch_controls(
        self, signals, decisions: list[AnalysisDecision], as_of: datetime
    ):
        if not signals:
            return []
        instruments = {
            instrument.canonical: instrument
            for instrument in self._settings.enabled_instruments
        }
        allowed = []
        try:
            with self._new_mt5_client() as client:
                try:
                    positions = client.get_open_positions()
                except Exception as exc:
                    LOGGER.warning("Open position status unavailable: %s", exc)
                    positions = None
                account = None
                catalog = None
                catalog_attempted = False
                for signal in signals:
                    instrument = instruments[signal.instrument]
                    try:
                        snapshot = client.get_symbol_snapshot(instrument)
                    except Exception as exc:
                        self._suppress_signal(
                            signal,
                            decisions,
                            DispatchGuardResult(
                                blocked=True,
                                status=DecisionStatus.STALE_MARKET_DATA,
                                reason="STALE_MARKET_DATA",
                                diagnostics={"error": str(exc)},
                            ),
                        )
                        continue
                    if not catalog_attempted:
                        catalog_attempted = True
                        try:
                            catalog = client.get_session_catalog(now_utc=as_of)
                        except Exception as exc:
                            LOGGER.warning("Market session status unavailable: %s", exc)
                    sessions = (
                        catalog.for_symbol(instrument.broker_symbol)
                        if catalog is not None
                        else None
                    )
                    session_result = evaluate_market_session_guard(
                        broker_symbol=instrument.broker_symbol,
                        sessions=sessions,
                        now_utc=as_of,
                        broker_offset_minutes=snapshot.broker_offset_minutes,
                        snapshot=snapshot,
                        block_before_close_minutes=(
                            self._settings.market_session.block_before_close_minutes
                        ),
                        stale_tick_threshold_seconds=(
                            self._settings.market_session.stale_tick_threshold_seconds
                        ),
                    )
                    if session_result.blocked:
                        self._suppress_signal(
                            signal, decisions, session_result
                        )
                        continue
                    position_result = evaluate_open_position_guard(
                        instrument.broker_symbol, positions
                    )
                    if position_result.blocked:
                        self._suppress_signal(
                            signal, decisions, position_result
                        )
                        continue
                    if account is None:
                        try:
                            account = client.get_account_snapshot()
                        except Exception as exc:
                            signal.metadata["position_size_unavailable_reason"] = str(
                                exc
                            )
                    if account is None:
                        allowed.append(signal)
                        continue
                    recommendation = recommend_position_size(
                        account=account,
                        snapshot=snapshot,
                        direction=signal.direction,
                        entry_price=signal.current_market_price,
                        stop_loss=signal.stop_loss,
                        risk_percent=self._settings.risk.risk_per_trade_percent,
                        profit_calculator=client.calculate_profit,
                    )
                    signal.metadata["position_size"] = recommendation.model_dump()
                    signal.metadata["market_session"] = session_result.diagnostics
                    allowed.append(signal)
        except Exception as exc:
            LOGGER.warning("Pre-dispatch MT5 controls unavailable: %s", exc)
            for signal in signals:
                if signal in allowed:
                    continue
                self._suppress_signal(
                    signal,
                    decisions,
                    DispatchGuardResult(
                        blocked=True,
                        status=DecisionStatus.SESSION_STATUS_UNAVAILABLE,
                        reason="SESSION_STATUS_UNAVAILABLE",
                        diagnostics={"error": str(exc)},
                    ),
                )
        return allowed

    @staticmethod
    def _suppress_signal(
        signal,
        decisions: list[AnalysisDecision],
        result: DispatchGuardResult,
    ) -> None:
        if result.status is None:
            raise ValueError("A blocked guard result requires a status")
        signal.status = result.status
        signal.metadata["dispatch_guard"] = result.diagnostics
        decision = next(
            (
                item
                for item in decisions
                if item.signal is not None
                and item.signal.signal_id == signal.signal_id
            ),
            None,
        )
        if decision is not None:
            decision.status = result.status
            decision.reasons = [result.reason or result.status.value]
            decision.diagnostics["dispatch_guard"] = result.diagnostics
        detail = " ".join(
            f"{key}={value}" for key, value in result.diagnostics.items()
        )
        LOGGER.info(
            "%s: %s%s",
            signal.instrument,
            result.reason or result.status.value,
            f" {detail}" if detail else "",
        )

    async def run_forever(self) -> None:
        LOGGER.info("ForexAI scan loop started")
        while True:
            try:
                await self.scan_once()
            except Exception:
                LOGGER.exception("Scan cycle failed")
            await asyncio.sleep(self._settings.scan.poll_seconds)

    def _calendar_events(
        self, as_of: datetime
    ) -> tuple[list[EconomicEvent], bool, str | None]:
        provider = create_calendar_provider(self._settings)
        if provider is None:
            LOGGER.warning("Economic calendar provider is disabled")
            return [], False, "Economic calendar disabled"
        start = as_of - timedelta(hours=self._settings.calendar.lookback_hours)
        end = as_of + timedelta(hours=self._settings.calendar.lookahead_hours)
        try:
            events = provider.get_events(start, end)
            LOGGER.info(
                "Economic calendar provider=%s returned %d events",
                self._settings.calendar.provider,
                len(events),
            )
            return events, True, None
        except CalendarUnavailableError as exc:
            LOGGER.warning(
                "Economic calendar unavailable; fallback=%s; reason=%s",
                self._settings.calendar.unavailable_behavior,
                exc,
            )
            return [], False, str(exc)

    async def _send_signal(self, signal) -> None:
        if getattr(self._notifier, "is_dry_run", False):
            await self._notifier.send(signal)
            return
        if not self._decisions.reserve_signal(signal):
            LOGGER.info("Duplicate signal suppressed: %s", signal.signal_id)
            return
        if self._settings.auto_execution.enabled:
            outcome = self._auto_executor.execute(signal)
            signal.metadata["auto_execution"] = outcome.model_dump(mode="json")
        try:
            external_id = await self._notifier.send(signal)
        except Exception:
            LOGGER.exception("Signal delivery failed: %s", signal.signal_id)
            self._last_scanned_trigger.pop(signal.instrument, None)
            return
        self._decisions.mark_sent(signal.signal_id, external_id)
