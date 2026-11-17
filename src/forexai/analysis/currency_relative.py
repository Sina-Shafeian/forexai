from __future__ import annotations

from collections import defaultdict

import pandas as pd

from forexai.domain.models import Instrument


def compute_currency_strength(
    instruments: list[Instrument],
    h1_frames: dict[str, pd.DataFrame],
    *,
    lookback_bars: int = 24,
) -> dict[str, float]:
    contributions: dict[str, list[float]] = defaultdict(list)
    for instrument in instruments:
        frame = h1_frames.get(instrument.canonical)
        if frame is None or len(frame) <= lookback_bars:
            continue
        pair_return = float(
            frame["close"].iloc[-1] / frame["close"].iloc[-lookback_bars - 1] - 1
        )
        if instrument.kind == "forex":
            contributions[instrument.base].append(pair_return)
            contributions[instrument.quote].append(-pair_return)
        elif instrument.base == "XAU":
            contributions["XAU"].append(pair_return)

    return {
        currency: sum(values) / len(values)
        for currency, values in contributions.items()
        if values
    }


def relative_strength(
    instrument: Instrument, strengths: dict[str, float]
) -> float | None:
    base = strengths.get(instrument.base)
    quote = strengths.get(instrument.quote)
    if base is None or quote is None:
        return None
    return base - quote

