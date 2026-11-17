from forexai.adapters.macro.factory import create_calendar_provider
from forexai.adapters.macro.myfxbook_calendar import MyfxbookRSSCalendar
from forexai.config import AutoExecutionConfig, SemiExecutionConfig, load_settings


def test_project_configuration_is_internally_consistent() -> None:
    settings = load_settings()
    assert len(settings.enabled_instruments) == 7
    assert settings.scoring.minimum_to_send == 75
    assert settings.scoring.max_alerts_per_utc_day == 5
    assert sum(settings.scoring.weights.model_dump().values()) == 100
    assert settings.mt5.require_demo_account is True
    assert settings.scan.completed_bars_only is True
    assert settings.calendar.provider == "myfxbook_rss"
    assert settings.calendar.unavailable_behavior == "warn_and_continue"
    assert settings.telegram.display_timezone == "Asia/Tehran"
    assert settings.risk.account_balance_source == "mt5"
    assert settings.risk.risk_per_trade_percent == 0.5
    assert settings.market_session.block_before_close_minutes == 60
    assert settings.auto_execution.enabled is False
    assert settings.auto_execution.demo_only is True
    assert settings.auto_execution.market_orders_only is True
    assert settings.auto_execution.magic == 26081101
    assert settings.semi_execution.enabled is True
    assert settings.semi_execution.demo_only is True
    assert settings.semi_execution.market_orders_only is True
    assert settings.semi_execution.signal_max_age_minutes == 15


def test_xau_has_a_separate_instrument_profile() -> None:
    settings = load_settings()
    xau = next(item for item in settings.instruments if item.canonical == "XAUUSD")
    assert xau.kind == "commodity_fx"
    assert xau.base == "XAU"
    assert xau.quote == "USD"


def test_demo_and_market_only_execution_safety_cannot_be_disabled() -> None:
    import pytest

    assert AutoExecutionConfig().enabled is False
    with pytest.raises(ValueError):
        AutoExecutionConfig(demo_only=False)
    with pytest.raises(ValueError):
        AutoExecutionConfig(market_orders_only=False)
    with pytest.raises(ValueError):
        SemiExecutionConfig(demo_only=False)
    with pytest.raises(ValueError):
        SemiExecutionConfig(market_orders_only=False)


def test_default_calendar_factory_requires_no_key() -> None:
    settings = load_settings()
    provider = create_calendar_provider(settings)
    assert isinstance(provider, MyfxbookRSSCalendar)
