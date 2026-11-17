from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from forexai.domain.models import Direction, Instrument, Signal


@dataclass(frozen=True)
class CorrelationRejection:
    signal_id: str
    reason: str


def select_diversified_signals(
    candidates: list[Signal],
    instruments: list[Instrument],
    h1_frames: dict[str, pd.DataFrame],
    *,
    lookback_bars: int,
    maximum_directional_correlation: float,
    max_same_currency_direction_alerts: int,
    limit: int,
) -> tuple[list[Signal], list[CorrelationRejection]]:
    """Rank candidates and avoid repeated exposure to the same market theme."""
    instrument_map = {item.canonical: item for item in instruments}
    ranked = sorted(candidates, key=lambda item: item.confidence_score, reverse=True)
    selected: list[Signal] = []
    rejections: list[CorrelationRejection] = []
    exposure_counts: dict[tuple[str, int], int] = {}

    for candidate in ranked:
        if len(selected) >= limit:
            rejections.append(
                CorrelationRejection(candidate.signal_id, "Daily alert capacity exhausted")
            )
            continue
        instrument = instrument_map[candidate.instrument]
        exposure = _currency_exposure(candidate, instrument)
        crowded = next(
            (
                key
                for key in exposure
                if exposure_counts.get(key, 0) >= max_same_currency_direction_alerts
            ),
            None,
        )
        if crowded is not None:
            currency, sign = crowded
            stance = "long" if sign > 0 else "short"
            rejections.append(
                CorrelationRejection(
                    candidate.signal_id,
                    f"Currency exposure guard: already {stance} {currency} in selected alerts",
                )
            )
            continue

        correlated_with = _find_directional_correlation(
            candidate,
            selected,
            h1_frames,
            lookback_bars,
            maximum_directional_correlation,
        )
        if correlated_with is not None:
            other, correlation = correlated_with
            rejections.append(
                CorrelationRejection(
                    candidate.signal_id,
                    f"Directional return correlation {correlation:.2f} with {other.instrument}",
                )
            )
            continue

        selected.append(candidate)
        for key in exposure:
            exposure_counts[key] = exposure_counts.get(key, 0) + 1
    return selected, rejections


def _find_directional_correlation(
    candidate: Signal,
    selected: list[Signal],
    frames: dict[str, pd.DataFrame],
    lookback_bars: int,
    threshold: float,
) -> tuple[Signal, float] | None:
    candidate_frame = frames.get(candidate.instrument)
    if candidate_frame is None:
        return None
    candidate_returns = candidate_frame["close"].pct_change().dropna().tail(lookback_bars)
    candidate_sign = 1 if candidate.direction is Direction.BUY else -1
    for other in selected:
        other_frame = frames.get(other.instrument)
        if other_frame is None:
            continue
        other_returns = other_frame["close"].pct_change().dropna().tail(lookback_bars)
        joined = pd.concat([candidate_returns, other_returns], axis=1, join="inner").dropna()
        if len(joined) < 30:
            continue
        raw_correlation = float(
            joined.iloc[:, 0].corr(joined.iloc[:, 1], method="spearman")
        )
        if pd.isna(raw_correlation):
            continue
        other_sign = 1 if other.direction is Direction.BUY else -1
        directional = raw_correlation * candidate_sign * other_sign
        if directional >= threshold:
            return other, directional
    return None


def _currency_exposure(
    signal: Signal, instrument: Instrument
) -> set[tuple[str, int]]:
    base_sign = 1 if signal.direction is Direction.BUY else -1
    return {(instrument.base, base_sign), (instrument.quote, -base_sign)}

