from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import threading

import pandas as pd

from forexai.analysis.data_quality import validate_bars
from forexai.analysis.time_normalization import (
    BrokerClockResult,
    BrokerClockState,
    detect_broker_clock,
    normalize_broker_epoch,
)
from forexai.adapters.mt5.session_catalog import (
    SessionCatalog,
    SessionCatalogError,
    load_session_catalog,
)
from forexai.domain.models import (
    AccountSnapshot,
    Direction,
    Instrument,
    OpenPosition,
    SymbolSnapshot,
)


LOGGER = logging.getLogger(__name__)
_MT5_RUNTIME_LOCK = threading.RLock()


class MT5ConnectionError(RuntimeError):
    pass


class MT5ReadOnlyClient:
    """MetaTrader 5 market-data client with no execution surface."""

    def __init__(
        self,
        terminal_path: Path,
        *,
        require_demo_account: bool = True,
        timeout_ms: int = 60_000,
        minimum_history_bars: int = 120,
        broker_clock_state_path: Path | None = None,
        offset_round_minutes: int = 15,
        maximum_offset_hours: int = 14,
        maximum_tick_age_seconds: int = 120,
        future_tolerance_seconds: int = 5,
        session_export_relative_path: Path = Path("ForexAI/symbol_sessions.csv"),
        session_export_max_age_minutes: int = 15,
    ) -> None:
        self._terminal_path = terminal_path
        self._require_demo_account = require_demo_account
        self._timeout_ms = timeout_ms
        self._minimum_history_bars = minimum_history_bars
        self._clock_state = (
            BrokerClockState(broker_clock_state_path)
            if broker_clock_state_path is not None
            else None
        )
        self._offset_round_minutes = offset_round_minutes
        self._maximum_offset_hours = maximum_offset_hours
        self._maximum_tick_age_seconds = maximum_tick_age_seconds
        self._future_tolerance_seconds = future_tolerance_seconds
        self._session_export_relative_path = session_export_relative_path
        self._session_export_max_age_minutes = session_export_max_age_minutes
        self._clock_result: BrokerClockResult | None = None
        self._session_catalog: SessionCatalog | None = None
        self._mt5 = None
        self._runtime_lock_acquired = False

    def initialize(self) -> None:
        _MT5_RUNTIME_LOCK.acquire()
        self._runtime_lock_acquired = True
        try:
            try:
                import MetaTrader5 as mt5
            except ImportError as exc:
                raise MT5ConnectionError(
                    "MetaTrader5 package is not installed; run 'uv sync --dev'"
                ) from exc

            if not self._terminal_path.exists():
                raise MT5ConnectionError(
                    f"MetaTrader 5 terminal not found: {self._terminal_path}"
                )
            if not mt5.initialize(
                path=str(self._terminal_path), timeout=self._timeout_ms
            ):
                raise MT5ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")
            self._mt5 = mt5
            try:
                self.verify_demo_account()
            except Exception:
                mt5.shutdown()
                self._mt5 = None
                raise
        except Exception:
            self._release_runtime_lock()
            raise

    def shutdown(self) -> None:
        try:
            if self._mt5 is not None:
                self._mt5.shutdown()
                self._mt5 = None
        finally:
            self._release_runtime_lock()

    def _release_runtime_lock(self) -> None:
        if self._runtime_lock_acquired:
            self._runtime_lock_acquired = False
            _MT5_RUNTIME_LOCK.release()

    def __enter__(self) -> "MT5ReadOnlyClient":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.shutdown()

    def verify_demo_account(self) -> dict:
        mt5 = self._require_initialized()
        account = mt5.account_info()
        if account is None:
            raise MT5ConnectionError(
                "MT5 has no active account session. Log in to the Demo account in the terminal."
            )
        demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if self._require_demo_account and account.trade_mode != demo_mode:
            raise MT5ConnectionError(
                "Safety block: the active MT5 account is not a Demo account."
            )
        return {
            "login": int(account.login),
            "server": account.server,
            "currency": account.currency,
            "balance": float(account.balance),
            "leverage": int(account.leverage),
            "trade_mode": "DEMO" if account.trade_mode == demo_mode else "NON_DEMO",
            "equity": float(getattr(account, "equity", 0) or 0),
            "margin": float(getattr(account, "margin", 0) or 0),
            "free_margin": float(getattr(account, "margin_free", 0) or 0),
            "margin_level": float(getattr(account, "margin_level", 0) or 0),
            "trade_allowed": bool(getattr(account, "trade_allowed", False)),
            "trade_expert": bool(getattr(account, "trade_expert", False)),
        }

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot.model_validate(self.verify_demo_account())

    def get_open_positions(self) -> list[OpenPosition]:
        mt5 = self._require_initialized()
        raw_positions = mt5.positions_get()
        if raw_positions is None:
            raise MT5ConnectionError(
                f"positions_get failed: {mt5.last_error()}"
            )
        return self._normalize_positions(raw_positions)

    def get_open_positions_for_symbol(self, broker_symbol: str) -> list[OpenPosition]:
        mt5 = self._require_initialized()
        raw_positions = mt5.positions_get(symbol=broker_symbol)
        if raw_positions is None:
            raise MT5ConnectionError(
                f"positions_get(symbol={broker_symbol}) failed: {mt5.last_error()}"
            )
        return self._normalize_positions(raw_positions)

    def _normalize_positions(self, raw_positions) -> list[OpenPosition]:
        mt5 = self._require_initialized()
        buy_type = getattr(mt5, "POSITION_TYPE_BUY", 0)
        sell_type = getattr(mt5, "POSITION_TYPE_SELL", 1)
        positions: list[OpenPosition] = []
        for raw in raw_positions:
            if raw.type == buy_type:
                direction = Direction.BUY
            elif raw.type == sell_type:
                direction = Direction.SELL
            else:
                raise MT5ConnectionError(
                    f"Unsupported MT5 position type {raw.type} for ticket {raw.ticket}"
                )
            positions.append(
                OpenPosition(
                    ticket=int(raw.ticket),
                    broker_symbol=str(raw.symbol),
                    direction=direction,
                    volume=float(raw.volume),
                    price_open=float(raw.price_open),
                    stop_loss=float(raw.sl),
                    take_profit=float(raw.tp),
                    magic=int(getattr(raw, "magic", 0) or 0),
                )
            )
        return positions

    def get_session_catalog(
        self, *, now_utc: datetime | None = None
    ) -> SessionCatalog:
        if self._session_catalog is not None:
            return self._session_catalog
        mt5 = self._require_initialized()
        if self._clock_result is None:
            raise SessionCatalogError(
                "A symbol snapshot is required before loading broker sessions"
            )
        terminal = mt5.terminal_info()
        if terminal is None or not getattr(terminal, "commondata_path", None):
            raise SessionCatalogError("MT5 Common Data path is unavailable")
        common_files = (Path(terminal.commondata_path) / "Files").resolve()
        relative = self._session_export_relative_path
        if relative.is_absolute() or ".." in relative.parts:
            raise SessionCatalogError("Session export path must be relative")
        export_path = (common_files / relative).resolve()
        if common_files != export_path.parent and common_files not in export_path.parents:
            raise SessionCatalogError("Session export path escapes MT5 Common Files")
        self._session_catalog = load_session_catalog(
            export_path,
            broker_offset_minutes=self._clock_result.offset_minutes,
            now_utc=now_utc or datetime.now(timezone.utc),
            maximum_age_minutes=self._session_export_max_age_minutes,
            future_tolerance_seconds=self._future_tolerance_seconds,
        )
        return self._session_catalog

    def get_symbol_snapshot(self, instrument: Instrument) -> SymbolSnapshot:
        mt5 = self._require_initialized()
        if not mt5.symbol_select(instrument.broker_symbol, True):
            raise MT5ConnectionError(
                f"Could not select symbol {instrument.broker_symbol}: {mt5.last_error()}"
            )
        info = mt5.symbol_info(instrument.broker_symbol)
        tick = mt5.symbol_info_tick(instrument.broker_symbol)
        if info is None or tick is None:
            raise MT5ConnectionError(
                f"No symbol metadata/tick for {instrument.broker_symbol}"
            )
        point = float(info.point)
        if point <= 0 or tick.bid <= 0 or tick.ask <= 0:
            raise MT5ConnectionError(
                f"Invalid quote metadata for {instrument.broker_symbol}"
            )
        tick_epoch = tick.time_msc / 1000 if tick.time_msc else tick.time
        clock = self._ensure_clock(instrument.broker_symbol, tick_epoch)
        quote_spread_points = (float(tick.ask) - float(tick.bid)) / point
        metadata_spread_points = float(getattr(info, "spread", 0) or 0)
        return SymbolSnapshot(
            canonical=instrument.canonical,
            broker_symbol=instrument.broker_symbol,
            point=point,
            digits=int(info.digits),
            bid=float(tick.bid),
            ask=float(tick.ask),
            spread_points=max(quote_spread_points, metadata_spread_points),
            observed_at=clock.normalized_time_utc,
            raw_observed_at=clock.raw_time_utc_interpretation,
            broker_offset_minutes=clock.offset_minutes,
            time_warnings=list(clock.warnings),
            trade_contract_size=float(getattr(info, "trade_contract_size", 0) or 0),
            trade_tick_size=float(getattr(info, "trade_tick_size", 0) or 0),
            trade_tick_value=float(getattr(info, "trade_tick_value", 0) or 0),
            trade_tick_value_loss=float(
                getattr(info, "trade_tick_value_loss", 0) or 0
            ),
            volume_min=float(getattr(info, "volume_min", 0) or 0),
            volume_max=float(getattr(info, "volume_max", 0) or 0),
            volume_step=float(getattr(info, "volume_step", 0) or 0),
            trade_mode=int(getattr(info, "trade_mode", 0) or 0),
            trade_execution_mode=int(getattr(info, "trade_exemode", 0) or 0),
            filling_mode=int(getattr(info, "filling_mode", 0) or 0),
            order_mode=int(getattr(info, "order_mode", 0) or 0),
            trade_stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
        )

    def get_terminal_diagnostics(self) -> dict:
        """Return terminal permissions without checking or sending an order."""
        mt5 = self._require_initialized()
        terminal = mt5.terminal_info()
        if terminal is None:
            raise MT5ConnectionError(
                f"terminal_info failed: {mt5.last_error()}"
            )
        return {
            "connected": bool(getattr(terminal, "connected", False)),
            "trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
            "tradeapi_disabled": bool(
                getattr(terminal, "tradeapi_disabled", False)
            ),
            "dlls_allowed": bool(getattr(terminal, "dlls_allowed", False)),
            "commondata_path": str(getattr(terminal, "commondata_path", "") or ""),
        }

    def get_closed_bars(
        self, instrument: Instrument, timeframe: str, count: int
    ) -> pd.DataFrame:
        mt5 = self._require_initialized()
        if not mt5.symbol_select(instrument.broker_symbol, True):
            raise MT5ConnectionError(
                f"Could not select symbol {instrument.broker_symbol}"
            )
        mt5_timeframe = self._timeframe_value(timeframe)
        # Position zero is the forming bar. Starting from one enforces closed bars.
        rates = mt5.copy_rates_from_pos(
            instrument.broker_symbol, mt5_timeframe, 1, count
        )
        if rates is None or len(rates) == 0:
            raise MT5ConnectionError(
                f"No {timeframe} bars for {instrument.broker_symbol}: {mt5.last_error()}"
            )

        frame = pd.DataFrame(rates)
        raw_times = frame.pop("time")
        if self._clock_result is None:
            tick = mt5.symbol_info_tick(instrument.broker_symbol)
            if tick is None:
                raise MT5ConnectionError(
                    f"No tick available to normalize {instrument.broker_symbol} bars"
                )
            tick_epoch = tick.time_msc / 1000 if tick.time_msc else tick.time
            self._ensure_clock(instrument.broker_symbol, tick_epoch)
        frame.index = pd.to_datetime(raw_times, unit="s", utc=True) - pd.to_timedelta(
            self._clock_result.offset_minutes, unit="m"
        )
        frame.index.name = "timestamp"
        frame = frame.rename(columns={"tick_volume": "volume"})
        ordered = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "spread",
            "real_volume",
        ]
        frame, _ = validate_bars(
            frame[[column for column in ordered if column in frame.columns]],
            minimum_bars=self._minimum_history_bars,
        )
        return frame

    def calculate_profit(
        self,
        direction: Direction,
        broker_symbol: str,
        volume_lots: float,
        entry_price: float,
        exit_price: float,
    ) -> float | None:
        """Estimate P/L in account currency; this does not create an MT5 order."""
        mt5 = self._require_initialized()
        action = (
            mt5.ORDER_TYPE_BUY if direction is Direction.BUY else mt5.ORDER_TYPE_SELL
        )
        result = mt5.order_calc_profit(
            action,
            broker_symbol,
            float(volume_lots),
            float(entry_price),
            float(exit_price),
        )
        return float(result) if result is not None else None

    def get_time_diagnostics(
        self, instrument: Instrument, *, now_utc: datetime | None = None
    ) -> dict:
        mt5 = self._require_initialized()
        if not mt5.symbol_select(instrument.broker_symbol, True):
            raise MT5ConnectionError(f"Could not select {instrument.broker_symbol}")
        tick = mt5.symbol_info_tick(instrument.broker_symbol)
        if tick is None:
            raise MT5ConnectionError(f"No tick for {instrument.broker_symbol}")
        tick_epoch = tick.time_msc / 1000 if tick.time_msc else tick.time
        self._clock_result = None
        effective_now = now_utc or datetime.now(timezone.utc)
        clock = self._ensure_clock(
            instrument.broker_symbol,
            tick_epoch,
            now_utc=effective_now,
        )
        bars: dict[str, list[dict[str, datetime]]] = {}
        diagnostic_warnings = list(clock.warnings)
        for timeframe in ("M15", "H1"):
            rates = mt5.copy_rates_from_pos(
                instrument.broker_symbol, self._timeframe_value(timeframe), 0, 3
            )
            if rates is None:
                bars[timeframe] = []
                continue
            bars[timeframe] = [
                {
                    "raw": datetime.fromtimestamp(
                        int(rate["time"]), tz=timezone.utc
                    ),
                    "normalized": normalize_broker_epoch(
                        int(rate["time"]), clock.offset_minutes
                    ),
                }
                for rate in rates
            ]
            if bars[timeframe]:
                latest = bars[timeframe][-1]["normalized"]
                duration_seconds = 900 if timeframe == "M15" else 3600
                age_seconds = (
                    effective_now.astimezone(timezone.utc) - latest
                ).total_seconds()
                if age_seconds < -self._future_tolerance_seconds:
                    diagnostic_warnings.append(
                        f"FUTURE_TIMESTAMP_{timeframe}_BAR"
                    )
                if age_seconds > duration_seconds * 2 + self._maximum_tick_age_seconds:
                    diagnostic_warnings.append(f"STALE_{timeframe}_BAR")
        return {
            "account": self.verify_demo_account(),
            "symbol": instrument.broker_symbol,
            "tick_raw_epoch": tick_epoch,
            "tick_raw": clock.raw_time_utc_interpretation,
            "tick_normalized": clock.normalized_time_utc,
            "offset_minutes": clock.offset_minutes,
            "tick_age_seconds": clock.tick_age_seconds,
            "warnings": list(dict.fromkeys(diagnostic_warnings)),
            "bars": bars,
        }

    def _require_initialized(self):
        if self._mt5 is None:
            raise MT5ConnectionError("MT5 client is not initialized")
        return self._mt5

    def _ensure_clock(
        self,
        broker_symbol: str,
        tick_epoch: float,
        *,
        now_utc: datetime | None = None,
    ) -> BrokerClockResult:
        if self._clock_result is not None:
            return self._clock_result
        account = self.verify_demo_account()
        account_key = f"{account['server']}|{account['login']}"
        previous = self._clock_state.load(account_key) if self._clock_state else None
        result = detect_broker_clock(
            tick_epoch,
            now_utc=now_utc or datetime.now(timezone.utc),
            previous_offset_minutes=previous,
            offset_round_minutes=self._offset_round_minutes,
            maximum_offset_hours=self._maximum_offset_hours,
            maximum_tick_age_seconds=self._maximum_tick_age_seconds,
            future_tolerance_seconds=self._future_tolerance_seconds,
        )
        if self._clock_state and result.detection_confident:
            self._clock_state.save(
                account_key, result.offset_minutes, datetime.now(timezone.utc)
            )
        if result.warnings:
            LOGGER.warning(
                "MT5 time normalization symbol=%s offset_minutes=%s warnings=%s",
                broker_symbol,
                result.offset_minutes,
                ",".join(result.warnings),
            )
        else:
            LOGGER.info(
                "MT5 time normalization symbol=%s offset_minutes=%s tick_age_seconds=%.3f",
                broker_symbol,
                result.offset_minutes,
                result.tick_age_seconds,
            )
        self._clock_result = result
        return result

    def _timeframe_value(self, timeframe: str) -> int:
        mt5 = self._require_initialized()
        mapping = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        try:
            return mapping[timeframe.upper()]
        except KeyError as exc:
            raise ValueError(f"Unsupported timeframe: {timeframe}") from exc
