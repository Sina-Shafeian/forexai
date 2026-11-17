import pandas as pd

from forexai.analysis.market_structure import BreakKind, analyze_structure
from forexai.domain.models import StructureState


def _bullish_wave() -> pd.DataFrame:
    highs = [1.0, 3.0, 2.0, 4.0, 3.0, 5.0, 4.0, 6.0, 7.0]
    lows = [0.0, 1.0, 0.5, 2.0, 1.5, 3.0, 2.5, 4.0, 5.0]
    opens = [(high + low) / 2 for high, low in zip(highs, lows, strict=True)]
    closes = opens.copy()
    closes[-1] = 6.5
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes},
        index=pd.date_range("2026-01-01", periods=len(highs), freq="15min", tz="UTC"),
    )


def test_bullish_structure_and_closed_bar_bos() -> None:
    result = analyze_structure(
        _bullish_wave(), swing_width=1, equality_tolerance=0.0, break_buffer=0.0
    )
    assert result.state is StructureState.BULLISH
    assert result.swing_highs[-1].label == "HH"
    assert result.swing_lows[-1].label == "HL"
    assert result.break_event is not None
    assert result.break_event.kind is BreakKind.BULLISH_BOS


def test_swing_is_not_exposed_before_right_hand_confirmation() -> None:
    frame = _bullish_wave().iloc[:6]
    result = analyze_structure(
        frame, swing_width=1, equality_tolerance=0.0, break_buffer=0.0
    )
    assert all(swing.index <= len(frame) - 2 for swing in result.swing_highs)
    assert all(swing.confirmed_at <= frame.index[-1] for swing in result.swing_highs)

