from __future__ import annotations

from pathlib import Path
from typing import Any

from forexai.adapters.mt5.client import MT5ConnectionError, MT5ReadOnlyClient
from forexai.domain.models import Direction, Signal, SymbolSnapshot


class AutoExecutionSafetyError(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class MT5DemoExecutionClient(MT5ReadOnlyClient):
    """Narrow MARKET-order adapter with an independent hard Demo gate."""

    def __init__(self, terminal_path: Path, **kwargs: Any) -> None:
        # The parent may inspect a non-Demo account. Every execution method below
        # independently enforces Demo before it can reach MetaTrader's order API.
        kwargs["require_demo_account"] = False
        super().__init__(terminal_path, **kwargs)

    def verify_execution_demo_account(self) -> dict:
        account = self.verify_demo_account()
        if account["trade_mode"] != "DEMO":
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED",
                "Hard safety block: the active MT5 account is not Demo.",
            )
        return account

    def validate_permissions(self) -> dict:
        account = self.verify_execution_demo_account()
        terminal = self.get_terminal_diagnostics()
        if not terminal["connected"]:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_TERMINAL_DISCONNECTED",
                "MT5 terminal is disconnected.",
            )
        if terminal["tradeapi_disabled"] or not terminal["trade_allowed"]:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_TERMINAL_TRADING_DISABLED",
                "MT5 terminal does not allow Python trading operations.",
            )
        if not account["trade_allowed"] or not account["trade_expert"]:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_ACCOUNT_TRADING_DISABLED",
                "The Demo account does not permit expert trading.",
            )
        return {"account": account, "terminal": terminal}

    def validate_symbol_for_market_order(
        self, snapshot: SymbolSnapshot, direction: Direction
    ) -> None:
        mt5 = self._require_initialized()
        full = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
        long_only = getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", 1)
        short_only = getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", 2)
        if snapshot.trade_mode not in {full, long_only, short_only}:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_SYMBOL_TRADING_DISABLED",
                f"Symbol trade mode {snapshot.trade_mode} cannot open a position.",
            )
        if snapshot.trade_mode == long_only and direction is Direction.SELL:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_SYMBOL_DIRECTION_BLOCKED",
                "Symbol is long-only.",
            )
        if snapshot.trade_mode == short_only and direction is Direction.BUY:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_SYMBOL_DIRECTION_BLOCKED",
                "Symbol is short-only.",
            )
        market_flag = getattr(mt5, "SYMBOL_ORDER_MARKET", 1)
        if not snapshot.order_mode & market_flag:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_MARKET_ORDER_DISABLED",
                "Market orders are disabled for the symbol.",
            )

    def build_market_request(
        self,
        *,
        signal: Signal,
        snapshot: SymbolSnapshot,
        price: float,
        volume: float,
        magic: int,
        comment: str,
        deviation_points: int,
    ) -> dict:
        mt5 = self._require_initialized()
        self.verify_execution_demo_account()
        self.validate_symbol_for_market_order(snapshot, signal.direction)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": snapshot.broker_symbol,
            "volume": float(volume),
            "type": (
                mt5.ORDER_TYPE_BUY
                if signal.direction is Direction.BUY
                else mt5.ORDER_TYPE_SELL
            ),
            "price": float(price),
            "sl": float(signal.stop_loss),
            "tp": float(signal.take_profit),
            "deviation": int(deviation_points),
            "magic": int(magic),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._select_filling_mode(snapshot),
        }
        self._validate_restricted_request(request)
        return request

    def calculate_margin(
        self, direction: Direction, symbol: str, volume: float, price: float
    ) -> float | None:
        mt5 = self._require_initialized()
        order_type = (
            mt5.ORDER_TYPE_BUY if direction is Direction.BUY else mt5.ORDER_TYPE_SELL
        )
        value = mt5.order_calc_margin(order_type, symbol, volume, price)
        return float(value) if value is not None else None

    def check_market_order(self, request: dict):
        mt5 = self._require_initialized()
        self.verify_execution_demo_account()
        self._validate_restricted_request(request)
        return mt5.order_check(request)

    def send_market_order(self, request: dict):
        mt5 = self._require_initialized()
        self.verify_execution_demo_account()
        self._validate_restricted_request(request)
        return mt5.order_send(request)

    def _select_filling_mode(self, snapshot: SymbolSnapshot) -> int:
        mt5 = self._require_initialized()
        fok_flag = getattr(mt5, "SYMBOL_FILLING_FOK", 1)
        ioc_flag = getattr(mt5, "SYMBOL_FILLING_IOC", 2)
        if snapshot.filling_mode & fok_flag:
            return mt5.ORDER_FILLING_FOK
        if snapshot.filling_mode & ioc_flag:
            return mt5.ORDER_FILLING_IOC
        market_execution = getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2)
        if snapshot.trade_execution_mode != market_execution:
            return mt5.ORDER_FILLING_RETURN
        raise AutoExecutionSafetyError(
            "AUTO_EXECUTION_FILLING_MODE_UNAVAILABLE",
            "No supported filling policy is available for Market Execution.",
        )

    def _validate_restricted_request(self, request: dict) -> None:
        mt5 = self._require_initialized()
        if request.get("action") != mt5.TRADE_ACTION_DEAL:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_UNSUPPORTED_ENTRY_TYPE",
                "Only immediate deal requests are allowed.",
            )
        if request.get("type") not in {mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL}:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_UNSUPPORTED_ENTRY_TYPE",
                "Only MARKET BUY/SELL types are allowed.",
            )
        if float(request.get("sl", 0) or 0) <= 0 or float(
            request.get("tp", 0) or 0
        ) <= 0:
            raise AutoExecutionSafetyError(
                "AUTO_EXECUTION_INVALID_STOPS",
                "A market order must contain both SL and TP.",
            )
