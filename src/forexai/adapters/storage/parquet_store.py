from __future__ import annotations

from pathlib import Path

import pandas as pd


class ParquetMarketStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write_snapshot(
        self, canonical_symbol: str, timeframe: str, frame: pd.DataFrame
    ) -> Path:
        if frame.empty:
            raise ValueError("Cannot persist an empty bar frame")
        last_bar = frame.index[-1].strftime("%Y%m%dT%H%M%SZ")
        target_dir = self._root / canonical_symbol / timeframe
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"bars_through_{last_bar}.parquet"
        if not target.exists():
            frame.to_parquet(target, engine="pyarrow", compression="zstd")
        return target

