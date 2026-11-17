from __future__ import annotations

from dataclasses import dataclass

from forexai.domain.models import DecisionStatus, Direction, SymbolSnapshot


@dataclass(frozen=True)
class EntryEvaluation:
    current_market_price: float
    suggested_entry: float
    entry_deviation: float
    maximum_deviation: float
    executable_risk_reward: float
    status: DecisionStatus | None
    reason: str | None


def evaluate_market_entry(
    *,
    direction: Direction,
    snapshot: SymbolSnapshot,
    suggested_entry: float,
    stop_loss: float,
    take_profit: float,
    atr: float,
    maximum_deviation_atr_fraction: float,
) -> EntryEvaluation:
    current = snapshot.ask if direction is Direction.BUY else snapshot.bid
    deviation = abs(current - suggested_entry)
    maximum_deviation = atr * maximum_deviation_atr_fraction
    risk = current - stop_loss if direction is Direction.BUY else stop_loss - current
    reward = (
        take_profit - current
        if direction is Direction.BUY
        else current - take_profit
    )
    risk_reward = reward / risk if risk > 0 and reward > 0 else 0.0
    status: DecisionStatus | None = None
    reason: str | None = None
    if {"STALE_TICK", "FUTURE_TIMESTAMP"}.intersection(snapshot.time_warnings):
        status = DecisionStatus.STALE_ENTRY
        reason = "Current Bid/Ask timestamp is stale or invalid"
    elif deviation > maximum_deviation:
        status = DecisionStatus.ENTRY_MISSED
        reason = (
            f"Current executable price deviated {deviation:.{snapshot.digits}f}; "
            f"maximum is {maximum_deviation:.{snapshot.digits}f}"
        )
    elif risk_reward <= 0:
        status = DecisionStatus.ENTRY_MISSED
        reason = "Current executable price invalidates entry geometry"
    return EntryEvaluation(
        current_market_price=current,
        suggested_entry=suggested_entry,
        entry_deviation=deviation,
        maximum_deviation=maximum_deviation,
        executable_risk_reward=risk_reward,
        status=status,
        reason=reason,
    )
