from datetime import datetime, timezone

from forexai.analysis.position_sizing import recommend_position_size
from forexai.domain.models import (
    AccountSnapshot,
    Direction,
    SymbolSnapshot,
)


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        login=1,
        server="Demo",
        currency="USD",
        balance=100_000,
        leverage=100,
        trade_mode="DEMO",
    )


def _snapshot(symbol: str, step: float = 0.01) -> SymbolSnapshot:
    return SymbolSnapshot(
        canonical=symbol,
        broker_symbol=symbol,
        point=0.00001 if symbol == "EURUSD" else 0.01,
        digits=5 if symbol == "EURUSD" else 2,
        bid=1.1 if symbol == "EURUSD" else 2400,
        ask=1.1002 if symbol == "EURUSD" else 2400.2,
        spread_points=20,
        observed_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        trade_contract_size=100_000 if symbol == "EURUSD" else 100,
        trade_tick_size=0.00001 if symbol == "EURUSD" else 0.01,
        trade_tick_value_loss=1,
        volume_min=0.01,
        volume_max=500,
        volume_step=step,
    )


def test_mt5_profit_calculation_drives_forex_lot_recommendation() -> None:
    recommendation = recommend_position_size(
        account=_account(),
        snapshot=_snapshot("EURUSD"),
        direction=Direction.BUY,
        entry_price=1.1,
        stop_loss=1.09,
        risk_percent=0.5,
        profit_calculator=lambda *_: -1000,
    )
    assert recommendation.maximum_loss == 500
    assert recommendation.suggested_volume_lots == 0.5
    assert recommendation.calculation_source == "mt5_calc_profit"


def test_xau_volume_is_rounded_down_to_mt5_step() -> None:
    recommendation = recommend_position_size(
        account=_account(),
        snapshot=_snapshot("XAUUSD", step=0.01),
        direction=Direction.SELL,
        entry_price=2400,
        stop_loss=2421,
        risk_percent=0.5,
        profit_calculator=lambda *_: -2100,
    )
    assert recommendation.suggested_volume_lots == 0.23


def test_volume_below_minimum_is_not_rounded_up() -> None:
    recommendation = recommend_position_size(
        account=_account(),
        snapshot=_snapshot("XAUUSD"),
        direction=Direction.BUY,
        entry_price=2400,
        stop_loss=2300,
        risk_percent=0.5,
        profit_calculator=lambda *_: -100_000,
    )
    assert recommendation.suggested_volume_lots == 0
    assert "below MT5 minimum" in recommendation.unavailable_reason


def test_volume_is_capped_at_symbol_maximum() -> None:
    snapshot = _snapshot("EURUSD", step=0.01)
    snapshot.volume_max = 2.0
    recommendation = recommend_position_size(
        account=_account(),
        snapshot=snapshot,
        direction=Direction.BUY,
        entry_price=1.1,
        stop_loss=1.099,
        risk_percent=0.5,
        profit_calculator=lambda *_: -10,
    )
    assert recommendation.suggested_volume_lots == 2.0
