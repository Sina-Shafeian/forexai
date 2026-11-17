from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from forexai.analysis.currency_relative import relative_strength
from forexai.analysis.entry import evaluate_market_entry
from forexai.analysis.event_guard import EventAssessment
from forexai.analysis.indicators import compute_technical_snapshot
from forexai.analysis.market_structure import StructureAnalysis, analyze_structure
from forexai.analysis.sessions import analyze_sessions
from forexai.config import Settings
from forexai.domain.models import (
    AnalysisDecision,
    DecisionStatus,
    Direction,
    EntryType,
    Instrument,
    Signal,
    StructureState,
    SymbolSnapshot,
)


def analyze_instrument(
    *,
    instrument: Instrument,
    frames: dict[str, pd.DataFrame],
    snapshot: SymbolSnapshot,
    currency_strengths: dict[str, float],
    event_assessment: EventAssessment,
    calendar_available: bool,
    settings: Settings,
    as_of: datetime,
) -> AnalysisDecision:
    timeframes = settings.scan.timeframes
    required = {
        timeframes.highest,
        timeframes.context,
        timeframes.setup,
        timeframes.trigger,
    }
    missing = required - set(frames)
    if missing:
        return _no_trade(instrument, [f"Missing timeframes: {sorted(missing)}"])

    trigger_frame = frames[timeframes.trigger]
    trigger_time = trigger_frame.index[-1].to_pydatetime()
    trigger_close_time = trigger_time + _timeframe_duration(timeframes.trigger)
    age_minutes = (
        as_of.astimezone(timezone.utc) - trigger_close_time
    ).total_seconds() / 60
    if age_minutes > settings.scan.max_signal_age_minutes:
        return _no_trade(
            instrument,
            [f"Latest closed {timeframes.trigger} bar is stale ({age_minutes:.1f}m)"],
            trigger_time,
        )

    point = snapshot.point
    strategy = settings.strategy
    analyses: dict[str, StructureAnalysis] = {}
    for timeframe in required:
        analyses[timeframe] = analyze_structure(
            frames[timeframe],
            swing_width=strategy.swing_width,
            equality_tolerance=strategy.equality_tolerance_points * point,
            break_buffer=strategy.break_buffer_points * point,
        )

    highest = analyses[timeframes.highest]
    context = analyses[timeframes.context]
    setup = analyses[timeframes.setup]
    trigger = analyses[timeframes.trigger]

    direction = _aligned_direction(context.state, setup.state)
    diagnostics = {
        "structure": {
            name: _structure_diagnostics(analysis)
            for name, analysis in analyses.items()
        }
    }
    if direction is None:
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.NO_TRADE,
            reasons=[
                f"{timeframes.context}/{timeframes.setup} structure is not aligned"
            ],
            diagnostics=diagnostics,
        )

    if settings.scoring.gates.reject_conflicting_context and _opposes(
        highest.state, direction
    ):
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.REJECTED,
            reasons=[f"{timeframes.highest} structure conflicts with {direction.value}"],
            diagnostics=diagnostics,
        )

    if trigger.break_event is None or trigger.break_event.direction is not direction:
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.NO_TRADE,
            reasons=[
                f"No closed-bar {timeframes.trigger} structure trigger for {direction.value}"
            ],
            diagnostics=diagnostics,
        )

    if event_assessment.blocked:
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.BLOCKED_NEWS,
            reasons=[event_assessment.warning or "High-impact event blackout"],
            diagnostics=diagnostics,
        )
    if (
        settings.calendar.unavailable_behavior == "block"
        and not calendar_available
    ):
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.BLOCKED_NEWS,
            reasons=["Economic calendar unavailable; alerting fails closed"],
            diagnostics=diagnostics,
        )

    technical = compute_technical_snapshot(trigger_frame, strategy)
    bar_spread_points = (
        float(trigger_frame["spread"].iloc[-1])
        if "spread" in trigger_frame.columns
        else 0.0
    )
    effective_spread_points = max(snapshot.spread_points, bar_spread_points)
    spread_price = effective_spread_points * snapshot.point
    spread_to_atr = spread_price / technical.atr
    diagnostics["technical"] = asdict(technical)
    diagnostics["spread_to_atr"] = spread_to_atr
    if spread_to_atr > settings.scoring.gates.maximum_spread_to_atr:
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.REJECTED,
            reasons=[
                f"Spread/ATR {spread_to_atr:.3f} exceeds configured maximum"
            ],
            diagnostics=diagnostics,
        )

    latest_reference = trigger.latest_low if direction is Direction.BUY else trigger.latest_high
    if latest_reference is None:
        return _no_trade(instrument, ["No confirmed trigger-timeframe invalidation swing"], trigger_time)

    entry = float(trigger_frame["close"].iloc[-1])
    stop_buffer = max(
        strategy.stop_buffer_points * point,
        spread_price,
    )
    stop = (
        latest_reference.price - stop_buffer
        if direction is Direction.BUY
        else latest_reference.price + stop_buffer
    )
    risk = entry - stop if direction is Direction.BUY else stop - entry
    if risk <= 0:
        return _no_trade(instrument, ["Invalid entry/stop geometry"], trigger_time)
    target = (
        entry + strategy.target_r_multiple * risk
        if direction is Direction.BUY
        else entry - strategy.target_r_multiple * risk
    )
    reward = target - entry if direction is Direction.BUY else entry - target
    strategy_risk_reward = reward / risk
    if strategy_risk_reward < strategy.minimum_risk_reward:
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.REJECTED,
            reasons=[f"Risk/reward {strategy_risk_reward:.2f} is below minimum"],
            diagnostics=diagnostics,
        )

    session = analyze_sessions(trigger_frame, as_of)
    pair_relative = relative_strength(instrument, currency_strengths)
    score, breakdown, reasons = _score_candidate(
        direction=direction,
        highest=highest,
        trigger=trigger,
        technical=technical,
        active_session=session.active_session,
        spread_to_atr=spread_to_atr,
        relative_value=pair_relative,
        settings=settings,
    )
    reasons.extend(
        [
            f"{timeframes.context}/{timeframes.setup} structure aligned {direction.value}",
            f"{timeframes.trigger} {trigger.break_event.kind.value} confirmed on closed bar",
            f"Strategy risk/reward 1:{strategy_risk_reward:.2f}",
        ]
    )
    diagnostics["sessions"] = asdict(session)
    diagnostics["currency_relative"] = pair_relative
    diagnostics["score_breakdown"] = breakdown

    entry_evaluation = evaluate_market_entry(
        direction=direction,
        snapshot=snapshot,
        suggested_entry=entry,
        stop_loss=stop,
        take_profit=target,
        atr=technical.atr,
        maximum_deviation_atr_fraction=(
            settings.entry.maximum_entry_deviation_atr_fraction
        ),
    )

    signal = Signal(
        signal_id=Signal.make_id(
            instrument.canonical,
            direction,
            timeframes.setup,
            trigger_time,
            strategy.rule_version,
        ),
        instrument=instrument.canonical,
        direction=direction,
        entry_low=round(entry, snapshot.digits),
        entry_high=round(entry, snapshot.digits),
        stop_loss=round(stop, snapshot.digits),
        take_profit=round(target, snapshot.digits),
        risk_reward=round(entry_evaluation.executable_risk_reward, 3),
        confidence_score=score,
        score_breakdown=breakdown,
        setup_timeframe=timeframes.setup,
        signal_created_at_utc=as_of,
        trigger_bar_time_utc=trigger_time,
        current_market_price=round(
            entry_evaluation.current_market_price, snapshot.digits
        ),
        suggested_entry=round(entry, snapshot.digits),
        entry_type=EntryType.MARKET,
        entry_deviation=round(entry_evaluation.entry_deviation, snapshot.digits),
        entry_source=f"closed {timeframes.trigger} trigger-bar close",
        reasons=_deduplicate(reasons),
        invalidation=(
            f"Closed {timeframes.trigger} candle beyond structural stop "
            f"{stop:.{snapshot.digits}f}"
        ),
        nearby_event_warning=event_assessment.warning,
        rule_version=strategy.rule_version,
        metadata={
            "digits": snapshot.digits,
            "broker_symbol": snapshot.broker_symbol,
            "active_session": session.active_session,
            "spread_points": effective_spread_points,
            "strategy_risk_reward": round(strategy_risk_reward, 3),
            "maximum_entry_deviation": entry_evaluation.maximum_deviation,
            "broker_offset_minutes": snapshot.broker_offset_minutes,
            "time_warnings": snapshot.time_warnings,
            "trade_contract_size": snapshot.trade_contract_size,
            "trade_tick_size": snapshot.trade_tick_size,
            "trade_tick_value": snapshot.trade_tick_value,
            "trade_tick_value_loss": snapshot.trade_tick_value_loss,
            "volume_min": snapshot.volume_min,
            "volume_max": snapshot.volume_max,
            "volume_step": snapshot.volume_step,
        },
    )
    if score < settings.scoring.minimum_to_send:
        signal.status = DecisionStatus.REJECTED
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=DecisionStatus.REJECTED,
            reasons=[
                f"Score {score} is below {settings.scoring.minimum_to_send}"
            ],
            signal=signal,
            diagnostics=diagnostics,
        )
    if entry_evaluation.status is not None:
        signal.status = entry_evaluation.status
        return AnalysisDecision(
            instrument=instrument.canonical,
            trigger_bar_time=trigger_time,
            status=entry_evaluation.status,
            reasons=[entry_evaluation.reason or entry_evaluation.status.value],
            signal=signal,
            diagnostics=diagnostics,
        )
    return AnalysisDecision(
        instrument=instrument.canonical,
        trigger_bar_time=trigger_time,
        status=DecisionStatus.CANDIDATE,
        reasons=[f"Candidate passed all gates with score {score}"],
        signal=signal,
        diagnostics=diagnostics,
    )


def _score_candidate(
    *,
    direction: Direction,
    highest: StructureAnalysis,
    trigger: StructureAnalysis,
    technical,
    active_session: str,
    spread_to_atr: float,
    relative_value: float | None,
    settings: Settings,
) -> tuple[int, dict[str, int], list[str]]:
    weights = settings.scoring.weights
    reasons: list[str] = []

    mtf_score = 20
    if not _opposes(highest.state, direction):
        mtf_score += 5

    setup_score = 15
    sweep_aligned = trigger.sweep is not None and (
        (direction is Direction.BUY and trigger.sweep.side == "SELL_SIDE")
        or (direction is Direction.SELL and trigger.sweep.side == "BUY_SIDE")
    )
    if sweep_aligned:
        setup_score += 5
        reasons.append(f"Aligned {trigger.sweep.side.lower()} liquidity sweep")

    technical_score = 0
    if technical.trend_direction is direction:
        technical_score += 8
        reasons.append("EMA trend context aligns")
    if technical.momentum_direction is direction:
        technical_score += 7
        reasons.append(f"RSI momentum aligns ({technical.rsi:.1f})")

    if technical.trend_regime == "TRENDING" and technical.volatility_regime == "NORMAL":
        regime_score = 15
        reasons.append("ADX trend and normal-volatility regime align")
    elif technical.trend_regime == "TRENDING":
        regime_score = 10
        reasons.append(
            f"ADX trend is present in {technical.volatility_regime.lower()} volatility"
        )
    elif technical.trend_regime == "TRANSITIONAL":
        regime_score = 5
    else:
        regime_score = 0

    if relative_value is None:
        relative_score = 0
    elif abs(relative_value) < 1e-8:
        relative_score = 7
    elif (direction is Direction.BUY and relative_value > 0) or (
        direction is Direction.SELL and relative_value < 0
    ):
        relative_score = 15
        reasons.append("Base-versus-quote relative strength aligns")
    else:
        relative_score = 0

    session_score = 0
    if active_session == "LONDON_NEW_YORK_OVERLAP":
        session_score += 6
    elif active_session in {"LONDON", "NEW_YORK", "LONDON+NEW_YORK"}:
        session_score += 5
    elif "TOKYO" in active_session:
        session_score += 3
    if spread_to_atr <= settings.scoring.gates.maximum_spread_to_atr / 2:
        session_score += 4
    else:
        session_score += 2

    breakdown = {
        "multi_timeframe_structure": min(
            mtf_score, weights.multi_timeframe_structure
        ),
        "setup_trigger": min(setup_score, weights.setup_trigger),
        "trend_momentum": min(technical_score, weights.trend_momentum),
        "regime_volatility": min(regime_score, weights.regime_volatility),
        "currency_relative": min(relative_score, weights.currency_relative),
        "session_execution": min(session_score, weights.session_execution),
    }
    return sum(breakdown.values()), breakdown, reasons


def _aligned_direction(
    context: StructureState, setup: StructureState
) -> Direction | None:
    if context is StructureState.BULLISH and setup is StructureState.BULLISH:
        return Direction.BUY
    if context is StructureState.BEARISH and setup is StructureState.BEARISH:
        return Direction.SELL
    return None


def _opposes(state: StructureState, direction: Direction) -> bool:
    return (state is StructureState.BULLISH and direction is Direction.SELL) or (
        state is StructureState.BEARISH and direction is Direction.BUY
    )


def _structure_diagnostics(analysis: StructureAnalysis) -> dict:
    return {
        "state": analysis.state.value,
        "latest_high": asdict(analysis.latest_high) if analysis.latest_high else None,
        "latest_low": asdict(analysis.latest_low) if analysis.latest_low else None,
        "break_event": asdict(analysis.break_event) if analysis.break_event else None,
        "sweep": asdict(analysis.sweep) if analysis.sweep else None,
    }


def _no_trade(
    instrument: Instrument, reasons: list[str], trigger_time: datetime | None = None
) -> AnalysisDecision:
    return AnalysisDecision(
        instrument=instrument.canonical,
        trigger_bar_time=trigger_time,
        status=DecisionStatus.NO_TRADE,
        reasons=reasons,
    )


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _timeframe_duration(timeframe: str):
    mapping = {
        "M1": timedelta(minutes=1),
        "M5": timedelta(minutes=5),
        "M15": timedelta(minutes=15),
        "M30": timedelta(minutes=30),
        "H1": timedelta(hours=1),
        "H4": timedelta(hours=4),
        "D1": timedelta(days=1),
    }
    try:
        return mapping[timeframe.upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported timeframe duration: {timeframe}") from exc
