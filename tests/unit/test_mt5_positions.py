from pathlib import Path
from types import SimpleNamespace

import pytest

from forexai.adapters.mt5.client import MT5ConnectionError, MT5ReadOnlyClient
from forexai.domain.models import Direction


class _FakeMT5:
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1

    def __init__(self, positions) -> None:
        self._positions = positions

    def positions_get(self):
        return self._positions

    @staticmethod
    def last_error():
        return 500, "test failure"


def _raw(position_type: int, symbol: str = "XAUUSD"):
    return SimpleNamespace(
        ticket=123,
        symbol=symbol,
        type=position_type,
        volume=0.41,
        price_open=2400,
        sl=2390,
        tp=2420,
    )


def test_mt5_positions_are_mapped_to_internal_directions() -> None:
    client = MT5ReadOnlyClient(Path("terminal64.exe"))
    client._mt5 = _FakeMT5([_raw(0), _raw(1, "EURUSD")])
    positions = client.get_open_positions()
    assert positions[0].direction is Direction.BUY
    assert positions[0].volume == 0.41
    assert positions[1].direction is Direction.SELL


def test_mt5_positions_failure_is_not_treated_as_empty() -> None:
    client = MT5ReadOnlyClient(Path("terminal64.exe"))
    client._mt5 = _FakeMT5(None)
    with pytest.raises(MT5ConnectionError, match="positions_get failed"):
        client.get_open_positions()
