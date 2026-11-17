from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import pandas as pd

from forexai.domain.models import Direction, StructureState


class SwingKind(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class BreakKind(StrEnum):
    BULLISH_BOS = "BULLISH_BOS"
    BEARISH_BOS = "BEARISH_BOS"
    BULLISH_CHOCH = "BULLISH_CHOCH"
    BEARISH_CHOCH = "BEARISH_CHOCH"
    BULLISH_STRUCTURE_BREAK = "BULLISH_STRUCTURE_BREAK"
    BEARISH_STRUCTURE_BREAK = "BEARISH_STRUCTURE_BREAK"


@dataclass(frozen=True)
class Swing:
    kind: SwingKind
    index: int
    timestamp: datetime
    confirmed_at: datetime
    price: float
    label: str | None = None


@dataclass(frozen=True)
class BreakEvent:
    kind: BreakKind
    direction: Direction
    timestamp: datetime
    broken_level: float
    close: float
    prior_state: StructureState


@dataclass(frozen=True)
class LiquiditySweep:
    side: str
    reference_type: str
    reference_price: float
    excursion: float
    timestamp: datetime


@dataclass(frozen=True)
class StructureAnalysis:
    state: StructureState
    swing_highs: tuple[Swing, ...]
    swing_lows: tuple[Swing, ...]
    break_event: BreakEvent | None
    sweep: LiquiditySweep | None

    @property
    def latest_high(self) -> Swing | None:
        return self.swing_highs[-1] if self.swing_highs else None

    @property
    def latest_low(self) -> Swing | None:
        return self.swing_lows[-1] if self.swing_lows else None


def analyze_structure(
    frame: pd.DataFrame,
    *,
    swing_width: int,
    equality_tolerance: float,
    break_buffer: float,
) -> StructureAnalysis:
    highs, lows = _confirmed_swings(frame, swing_width)
    highs = _label_swings(highs, equality_tolerance)
    lows = _label_swings(lows, equality_tolerance)
    state = _classify_state(highs, lows)
    break_event = _detect_break(frame, state, highs, lows, break_buffer)
    sweep = _detect_sweep(frame, highs, lows, break_buffer)
    return StructureAnalysis(
        state=state,
        swing_highs=tuple(highs),
        swing_lows=tuple(lows),
        break_event=break_event,
        sweep=sweep,
    )


def _confirmed_swings(
    frame: pd.DataFrame, width: int
) -> tuple[list[Swing], list[Swing]]:
    if width < 1:
        raise ValueError("swing_width must be at least one")
    highs: list[Swing] = []
    lows: list[Swing] = []
    high_values = frame["high"].to_numpy()
    low_values = frame["low"].to_numpy()
    for index in range(width, len(frame) - width):
        left_highs = high_values[index - width : index]
        right_highs = high_values[index + 1 : index + width + 1]
        left_lows = low_values[index - width : index]
        right_lows = low_values[index + 1 : index + width + 1]
        timestamp = frame.index[index].to_pydatetime()
        confirmed_at = frame.index[index + width].to_pydatetime()
        if (high_values[index] > left_highs).all() and (
            high_values[index] > right_highs
        ).all():
            highs.append(
                Swing(
                    kind=SwingKind.HIGH,
                    index=index,
                    timestamp=timestamp,
                    confirmed_at=confirmed_at,
                    price=float(high_values[index]),
                )
            )
        if (low_values[index] < left_lows).all() and (
            low_values[index] < right_lows
        ).all():
            lows.append(
                Swing(
                    kind=SwingKind.LOW,
                    index=index,
                    timestamp=timestamp,
                    confirmed_at=confirmed_at,
                    price=float(low_values[index]),
                )
            )
    return highs, lows


def _label_swings(swings: list[Swing], tolerance: float) -> list[Swing]:
    labelled: list[Swing] = []
    previous: Swing | None = None
    for swing in swings:
        label = None
        if previous is not None:
            difference = swing.price - previous.price
            if abs(difference) <= tolerance:
                label = "EH" if swing.kind is SwingKind.HIGH else "EL"
            elif swing.kind is SwingKind.HIGH:
                label = "HH" if difference > 0 else "LH"
            else:
                label = "HL" if difference > 0 else "LL"
        labelled_swing = Swing(**{**swing.__dict__, "label": label})
        labelled.append(labelled_swing)
        previous = swing
    return labelled


def _classify_state(highs: list[Swing], lows: list[Swing]) -> StructureState:
    if len(highs) < 2 or len(lows) < 2:
        return StructureState.UNCONFIRMED
    high_label = highs[-1].label
    low_label = lows[-1].label
    if high_label == "HH" and low_label == "HL":
        return StructureState.BULLISH
    if high_label == "LH" and low_label == "LL":
        return StructureState.BEARISH
    return StructureState.MIXED


def _detect_break(
    frame: pd.DataFrame,
    state: StructureState,
    highs: list[Swing],
    lows: list[Swing],
    buffer: float,
) -> BreakEvent | None:
    if not highs or not lows:
        return None
    last_close = float(frame["close"].iloc[-1])
    timestamp = frame.index[-1].to_pydatetime()
    latest_high = highs[-1].price
    latest_low = lows[-1].price

    if state is StructureState.BULLISH:
        if last_close > latest_high + buffer:
            kind, direction, level = BreakKind.BULLISH_BOS, Direction.BUY, latest_high
        elif last_close < latest_low - buffer:
            kind, direction, level = BreakKind.BEARISH_CHOCH, Direction.SELL, latest_low
        else:
            return None
    elif state is StructureState.BEARISH:
        if last_close < latest_low - buffer:
            kind, direction, level = BreakKind.BEARISH_BOS, Direction.SELL, latest_low
        elif last_close > latest_high + buffer:
            kind, direction, level = BreakKind.BULLISH_CHOCH, Direction.BUY, latest_high
        else:
            return None
    else:
        if last_close > latest_high + buffer:
            kind = BreakKind.BULLISH_STRUCTURE_BREAK
            direction, level = Direction.BUY, latest_high
        elif last_close < latest_low - buffer:
            kind = BreakKind.BEARISH_STRUCTURE_BREAK
            direction, level = Direction.SELL, latest_low
        else:
            return None
    return BreakEvent(
        kind=kind,
        direction=direction,
        timestamp=timestamp,
        broken_level=level,
        close=last_close,
        prior_state=state,
    )


def _detect_sweep(
    frame: pd.DataFrame,
    highs: list[Swing],
    lows: list[Swing],
    buffer: float,
) -> LiquiditySweep | None:
    if len(frame) < 1:
        return None
    candle = frame.iloc[-1]
    timestamp = frame.index[-1].to_pydatetime()
    if highs:
        reference = highs[-1].price
        if float(candle.high) > reference + buffer and float(candle.close) <= reference:
            return LiquiditySweep(
                side="BUY_SIDE",
                reference_type="CONFIRMED_SWING_HIGH",
                reference_price=reference,
                excursion=float(candle.high) - reference,
                timestamp=timestamp,
            )
    if lows:
        reference = lows[-1].price
        if float(candle.low) < reference - buffer and float(candle.close) >= reference:
            return LiquiditySweep(
                side="SELL_SIDE",
                reference_type="CONFIRMED_SWING_LOW",
                reference_price=reference,
                excursion=reference - float(candle.low),
                timestamp=timestamp,
            )
    return None

