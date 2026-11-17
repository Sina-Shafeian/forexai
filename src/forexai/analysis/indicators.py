from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta

from forexai.config import StrategyConfig
from forexai.domain.models import Direction


@dataclass(frozen=True)
class TechnicalSnapshot:
    atr: float
    atr_percent_of_price: float
    volatility_percentile: float
    ema_fast: float
    ema_slow: float
    rsi: float
    adx: float
    volatility_regime: str
    trend_regime: str
    trend_direction: Direction | None
    momentum_direction: Direction | None


def compute_technical_snapshot(
    frame: pd.DataFrame, config: StrategyConfig
) -> TechnicalSnapshot:
    close = frame["close"]
    ema_fast = ta.ema(close, length=config.ema_fast)
    ema_slow = ta.ema(close, length=config.ema_slow)
    atr = ta.atr(
        frame["high"], frame["low"], close, length=config.atr_length
    )
    rsi = ta.rsi(close, length=config.rsi_length)
    adx_frame = ta.adx(
        frame["high"], frame["low"], close, length=config.atr_length
    )
    if any(series is None or series.dropna().empty for series in (ema_fast, ema_slow, atr, rsi)):
        raise ValueError("Indicator history is insufficient")
    if adx_frame is None or adx_frame.dropna(how="all").empty:
        raise ValueError("ADX history is insufficient")

    last_close = float(close.iloc[-1])
    last_fast = float(ema_fast.dropna().iloc[-1])
    last_slow = float(ema_slow.dropna().iloc[-1])
    last_atr = float(atr.dropna().iloc[-1])
    last_rsi = float(rsi.dropna().iloc[-1])
    adx_column = next(column for column in adx_frame.columns if column.startswith("ADX_"))
    last_adx = float(adx_frame[adx_column].dropna().iloc[-1])
    atr_window = atr.dropna().tail(100)
    volatility_percentile = float((atr_window <= last_atr).mean())

    trend_direction = None
    if last_close > last_fast > last_slow:
        trend_direction = Direction.BUY
    elif last_close < last_fast < last_slow:
        trend_direction = Direction.SELL

    momentum_direction = None
    if last_rsi >= 55:
        momentum_direction = Direction.BUY
    elif last_rsi <= 45:
        momentum_direction = Direction.SELL

    volatility_regime = (
        "LOW"
        if volatility_percentile < 0.25
        else "HIGH"
        if volatility_percentile > 0.75
        else "NORMAL"
    )
    trend_regime = (
        "TRENDING"
        if last_adx > 25
        else "RANGING"
        if last_adx < 20
        else "TRANSITIONAL"
    )

    return TechnicalSnapshot(
        atr=last_atr,
        atr_percent_of_price=last_atr / last_close,
        volatility_percentile=volatility_percentile,
        ema_fast=last_fast,
        ema_slow=last_slow,
        rsi=last_rsi,
        adx=last_adx,
        volatility_regime=volatility_regime,
        trend_regime=trend_regime,
        trend_direction=trend_direction,
        momentum_direction=momentum_direction,
    )
