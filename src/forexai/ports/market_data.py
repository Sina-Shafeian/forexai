from __future__ import annotations

from typing import Protocol

import pandas as pd

from forexai.domain.models import Instrument, SymbolSnapshot


class MarketDataProvider(Protocol):
    """Read-only market-data boundary. Execution is intentionally absent."""

    def initialize(self) -> None: ...

    def shutdown(self) -> None: ...

    def verify_demo_account(self) -> dict: ...

    def get_symbol_snapshot(self, instrument: Instrument) -> SymbolSnapshot: ...

    def get_closed_bars(
        self, instrument: Instrument, timeframe: str, count: int
    ) -> pd.DataFrame: ...

