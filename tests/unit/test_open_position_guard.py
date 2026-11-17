from forexai.analysis.dispatch_guards import evaluate_open_position_guard
from forexai.domain.models import DecisionStatus, Direction, OpenPosition


def _position(symbol: str = "XAUUSD", direction: Direction = Direction.BUY):
    return OpenPosition(
        ticket=123,
        broker_symbol=symbol,
        direction=direction,
        volume=0.41,
        price_open=2400,
        stop_loss=2390,
        take_profit=2420,
    )


def test_same_symbol_same_direction_is_suppressed() -> None:
    result = evaluate_open_position_guard("XAUUSD", [_position()])
    assert result.status is DecisionStatus.SUPPRESSED_EXISTING_POSITION
    assert result.reason == "SUPPRESSED_SYMBOL_ALREADY_OPEN"
    assert result.diagnostics["existing_direction"] == "BUY"


def test_same_symbol_opposite_direction_is_also_suppressed() -> None:
    result = evaluate_open_position_guard(
        "XAUUSD", [_position(direction=Direction.SELL)]
    )
    assert result.status is DecisionStatus.SUPPRESSED_EXISTING_POSITION


def test_position_on_another_symbol_does_not_block() -> None:
    result = evaluate_open_position_guard("EURUSD", [_position()])
    assert result.blocked is False


def test_no_open_position_passes() -> None:
    assert evaluate_open_position_guard("XAUUSD", []).blocked is False


def test_closed_position_is_absent_and_candidate_can_be_reconsidered() -> None:
    open_result = evaluate_open_position_guard("XAUUSD", [_position()])
    closed_result = evaluate_open_position_guard("XAUUSD", [])
    assert open_result.blocked is True
    assert closed_result.blocked is False


def test_positions_failure_suppresses_safely() -> None:
    result = evaluate_open_position_guard("XAUUSD", None)
    assert result.blocked is True
    assert result.status is DecisionStatus.POSITION_STATUS_UNAVAILABLE
