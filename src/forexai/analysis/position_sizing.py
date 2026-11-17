from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, ROUND_FLOOR

from forexai.domain.models import (
    AccountSnapshot,
    Direction,
    PositionSizeRecommendation,
    SymbolSnapshot,
)


ProfitCalculator = Callable[[Direction, str, float, float, float], float | None]


def recommend_position_size(
    *,
    account: AccountSnapshot,
    snapshot: SymbolSnapshot,
    direction: Direction,
    entry_price: float,
    stop_loss: float,
    risk_percent: float,
    profit_calculator: ProfitCalculator | None = None,
) -> PositionSizeRecommendation:
    """Return fixed-fractional MT5 volume in lots without placing any trade."""
    maximum_loss = account.balance * risk_percent / 100
    loss_per_lot: float | None = None
    source = "unavailable"

    if profit_calculator is not None:
        estimated = profit_calculator(
            direction,
            snapshot.broker_symbol,
            1.0,
            entry_price,
            stop_loss,
        )
        if estimated is not None and estimated != 0:
            loss_per_lot = abs(estimated)
            source = "mt5_calc_profit"

    if loss_per_lot is None:
        tick_value = snapshot.trade_tick_value_loss or snapshot.trade_tick_value
        if snapshot.trade_tick_size > 0 and tick_value > 0:
            loss_per_lot = (
                abs(entry_price - stop_loss) / snapshot.trade_tick_size * tick_value
            )
            source = "symbol_tick_spec_fallback"

    if not loss_per_lot or maximum_loss <= 0:
        return _unavailable(
            account, risk_percent, maximum_loss, source, "loss_per_lot unavailable"
        )
    if snapshot.volume_step <= 0 or snapshot.volume_min <= 0:
        return _unavailable(
            account, risk_percent, maximum_loss, source, "invalid MT5 volume limits"
        )

    raw_lots = maximum_loss / loss_per_lot
    lots = _round_down_to_step(raw_lots, snapshot.volume_step)
    if lots < snapshot.volume_min:
        return PositionSizeRecommendation(
            account_balance=account.balance,
            account_currency=account.currency,
            risk_percent=risk_percent,
            maximum_loss=maximum_loss,
            suggested_volume_lots=0,
            loss_per_lot=loss_per_lot,
            calculation_source=source,
            unavailable_reason=(
                f"risk-based volume {lots:g} is below MT5 minimum "
                f"{snapshot.volume_min:g} lot"
            ),
        )
    if snapshot.volume_max > 0:
        lots = min(lots, _round_down_to_step(snapshot.volume_max, snapshot.volume_step))
    return PositionSizeRecommendation(
        account_balance=account.balance,
        account_currency=account.currency,
        risk_percent=risk_percent,
        maximum_loss=maximum_loss,
        suggested_volume_lots=lots,
        loss_per_lot=loss_per_lot,
        calculation_source=source,
    )


def _round_down_to_step(value: float, step: float) -> float:
    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step))
    units = (value_decimal / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_decimal)


def _unavailable(
    account: AccountSnapshot,
    risk_percent: float,
    maximum_loss: float,
    source: str,
    reason: str,
) -> PositionSizeRecommendation:
    return PositionSizeRecommendation(
        account_balance=account.balance,
        account_currency=account.currency,
        risk_percent=risk_percent,
        maximum_loss=maximum_loss,
        suggested_volume_lots=0,
        calculation_source=source,
        unavailable_reason=reason,
    )
