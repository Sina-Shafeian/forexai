import ast
from types import SimpleNamespace

import pytest

from forexai.adapters.mt5.client import MT5ReadOnlyClient
from forexai.adapters.mt5.execution import (
    AutoExecutionSafetyError,
    MT5DemoExecutionClient,
)
from pathlib import Path


def test_mt5_adapter_exposes_no_execution_method() -> None:
    public_methods = {
        name for name in dir(MT5ReadOnlyClient) if not name.startswith("_")
    }
    assert not any("order" in name.lower() or "trade" in name.lower() for name in public_methods)
    assert public_methods >= {
        "initialize",
        "shutdown",
        "verify_demo_account",
        "get_symbol_snapshot",
        "get_closed_bars",
    }


def test_mt5_order_send_is_isolated_to_demo_execution_adapter() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    calls = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "order_send"
            ):
                calls.append(path.relative_to(source_root).as_posix())
    assert calls == ["forexai/adapters/mt5/execution.py"]


def test_execution_adapter_has_hard_demo_gate_and_restricted_surface() -> None:
    source = Path(__file__).resolve().parents[2].joinpath(
        "src/forexai/adapters/mt5/execution.py"
    ).read_text(encoding="utf-8")
    assert "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED" in source
    assert "verify_execution_demo_account()" in source
    assert "TRADE_ACTION_DEAL" in source
    assert "ORDER_TYPE_BUY" in source
    assert "ORDER_TYPE_SELL" in source
    assert {"check_market_order", "send_market_order"} <= {
        name for name in dir(MT5DemoExecutionClient) if not name.startswith("_")
    }


def test_real_account_cannot_reach_underlying_order_send() -> None:
    class _RealMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0
        TRADE_ACTION_DEAL = 1
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1

        def __init__(self) -> None:
            self.send_calls = 0

        def account_info(self):
            return SimpleNamespace(
                login=1,
                server="Real",
                currency="USD",
                balance=1000,
                leverage=100,
                trade_mode=2,
                equity=1000,
                margin=0,
                margin_free=1000,
                margin_level=0,
                trade_allowed=True,
                trade_expert=True,
            )

        def order_send(self, request):
            self.send_calls += 1
            return None

    mt5 = _RealMT5()
    client = MT5DemoExecutionClient(Path("unused-terminal.exe"))
    client._mt5 = mt5
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "type": mt5.ORDER_TYPE_BUY,
        "sl": 1.0,
        "tp": 2.0,
    }
    with pytest.raises(AutoExecutionSafetyError) as exc:
        client.send_market_order(request)
    assert exc.value.reason == "AUTO_EXECUTION_REAL_ACCOUNT_BLOCKED"
    assert mt5.send_calls == 0
