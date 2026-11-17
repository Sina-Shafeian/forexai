from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forexai.domain.models import Instrument


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppConfig(BaseModel):
    name: str
    environment: Literal["demo"] = "demo"
    timezone: Literal["UTC"] = "UTC"
    log_level: str = "INFO"
    state_database: Path
    raw_data_directory: Path


class MT5Config(BaseModel):
    terminal_path: Path
    require_demo_account: bool = True
    initialize_timeout_ms: int = Field(default=60_000, gt=0)
    broker_clock_state_path: Path = Path("data/state/broker_clock.json")
    offset_round_minutes: int = Field(default=15, ge=1, le=60)
    maximum_offset_hours: int = Field(default=14, ge=1, le=24)
    maximum_tick_age_seconds: int = Field(default=120, ge=1)
    future_tolerance_seconds: int = Field(default=5, ge=0)


class TimeframeConfig(BaseModel):
    highest: str = "D1"
    context: str = "H4"
    setup: str = "H1"
    trigger: str = "M15"


class ScanConfig(BaseModel):
    poll_seconds: int = Field(default=20, ge=5)
    bars_per_timeframe: int = Field(default=500, ge=120)
    timeframes: TimeframeConfig
    completed_bars_only: Literal[True] = True
    max_signal_age_minutes: int = Field(default=20, gt=0)


class TelegramConfig(BaseModel):
    enabled: bool = True
    parse_mode: str = "HTML"
    display_timezone: str = "Asia/Tehran"


class EntryConfig(BaseModel):
    maximum_entry_deviation_atr_fraction: float = Field(default=0.15, gt=0)


class RiskConfig(BaseModel):
    account_balance_source: Literal["mt5"] = "mt5"
    risk_per_trade_percent: float = Field(default=0.5, gt=0, le=100)


class MarketSessionConfig(BaseModel):
    block_before_close_minutes: int = Field(default=60, ge=0)
    stale_tick_threshold_seconds: int = Field(default=120, ge=1)
    export_relative_path: Path = Path("ForexAI/symbol_sessions.csv")
    export_max_age_minutes: int = Field(default=15, ge=1)


class AutoExecutionConfig(BaseModel):
    enabled: bool = False
    demo_only: Literal[True] = True
    market_orders_only: Literal[True] = True
    magic: int = Field(default=26_081_101, gt=0)
    comment: str = Field(default="ForexAI", min_length=1, max_length=31)
    maximum_price_deviation_points: int = Field(default=20, ge=0)


class SemiExecutionConfig(BaseModel):
    enabled: bool = True
    demo_only: Literal[True] = True
    market_orders_only: Literal[True] = True
    signal_max_age_minutes: int = Field(default=15, ge=1, le=240)
    magic: int = Field(default=26_081_101, gt=0)
    comment: str = Field(default="ForexAI-Semi", min_length=1, max_length=31)
    maximum_price_deviation_points: int = Field(default=20, ge=0)


class StrategyConfig(BaseModel):
    name: str
    rule_version: str
    swing_width: int = Field(default=2, ge=1, le=10)
    equality_tolerance_points: float = Field(default=2, ge=0)
    break_buffer_points: float = Field(default=1, ge=0)
    stop_buffer_points: float = Field(default=2, ge=0)
    target_r_multiple: float = Field(default=2, gt=0)
    minimum_risk_reward: float = Field(default=1.8, gt=0)
    atr_length: int = Field(default=14, ge=2)
    ema_fast: int = Field(default=20, ge=2)
    ema_slow: int = Field(default=50, ge=3)
    rsi_length: int = Field(default=14, ge=2)
    minimum_history_bars: int = Field(default=120, ge=50)

    @model_validator(mode="after")
    def validate_periods_and_reward(self) -> "StrategyConfig":
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be smaller than ema_slow")
        if self.target_r_multiple < self.minimum_risk_reward:
            raise ValueError("target_r_multiple must meet minimum_risk_reward")
        return self


class ScoreWeights(BaseModel):
    multi_timeframe_structure: int
    setup_trigger: int
    trend_momentum: int
    regime_volatility: int
    currency_relative: int
    session_execution: int

    @model_validator(mode="after")
    def total_must_equal_100(self) -> "ScoreWeights":
        if sum(self.model_dump().values()) != 100:
            raise ValueError("scoring weights must total exactly 100")
        return self


class GateConfig(BaseModel):
    maximum_spread_to_atr: float = Field(default=0.12, gt=0)
    correlation_lookback_bars: int = Field(default=120, ge=30)
    maximum_directional_correlation: float = Field(default=0.80, ge=0, le=1)
    max_same_currency_direction_alerts: int = Field(default=2, ge=1)
    reject_conflicting_context: bool = True
    reject_duplicate_signal: bool = True


class ScoringConfig(BaseModel):
    minimum_to_send: int = Field(default=75, ge=0, le=100)
    max_alerts_per_utc_day: int = Field(default=5, ge=1)
    weights: ScoreWeights
    gates: GateConfig


class CalendarConfig(BaseModel):
    provider: Literal["myfxbook_rss", "fmp", "disabled"] = "myfxbook_rss"
    unavailable_behavior: Literal["block", "warn_and_continue"] = (
        "warn_and_continue"
    )
    cache_minutes: int = Field(default=30, ge=1)
    stale_cache_if_error_hours: int = Field(default=6, ge=0)
    lookback_hours: int = Field(default=4, ge=0)
    lookahead_hours: int = Field(default=24, ge=1)
    high_impact_before_minutes: int = Field(default=60, ge=0)
    high_impact_after_minutes: int = Field(default=30, ge=0)
    myfxbook_endpoint: str
    fmp_endpoint: str
    user_agent: str = "ForexAI/0.1"


class SecretSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = Field(default=None, repr=False)
    fmp_api_key: str | None = Field(default=None, repr=False)
    telegram_allowed_user_id: int | None = Field(default=None, repr=False)


class Settings(BaseModel):
    project_root: Path
    app: AppConfig
    mt5: MT5Config
    scan: ScanConfig
    telegram: TelegramConfig
    entry: EntryConfig
    risk: RiskConfig
    market_session: MarketSessionConfig
    auto_execution: AutoExecutionConfig
    semi_execution: SemiExecutionConfig
    strategy: StrategyConfig
    scoring: ScoringConfig
    calendar: CalendarConfig
    instruments: list[Instrument]
    secrets: SecretSettings

    @property
    def enabled_instruments(self) -> list[Instrument]:
        return [instrument for instrument in self.instruments if instrument.enabled]


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Required configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration root must be an object: {path}")
    return loaded


def _resolve_project_path(project_root: Path, value: Path) -> Path:
    return value if value.is_absolute() else project_root / value


def load_settings(project_root: Path | None = None) -> Settings:
    root = project_root or Path(os.getenv("FOREXAI_PROJECT_ROOT", PROJECT_ROOT))
    root = root.resolve()
    config_dir = root / "config"

    app_data = _read_yaml(config_dir / "app.yaml")
    instruments_data = _read_yaml(config_dir / "instruments.yaml")
    scoring_data = _read_yaml(config_dir / "scoring.yaml")
    events_data = _read_yaml(config_dir / "events.yaml")

    app = AppConfig.model_validate(app_data["app"])
    app.state_database = _resolve_project_path(root, app.state_database)
    app.raw_data_directory = _resolve_project_path(root, app.raw_data_directory)

    mt5 = MT5Config.model_validate(app_data["mt5"])
    mt5.terminal_path = _resolve_project_path(root, mt5.terminal_path)
    mt5.broker_clock_state_path = _resolve_project_path(
        root, mt5.broker_clock_state_path
    )

    return Settings(
        project_root=root,
        app=app,
        mt5=mt5,
        scan=ScanConfig.model_validate(app_data["scan"]),
        telegram=TelegramConfig.model_validate(app_data["telegram"]),
        entry=EntryConfig.model_validate(app_data.get("entry", {})),
        risk=RiskConfig.model_validate(app_data.get("risk", {})),
        market_session=MarketSessionConfig.model_validate(
            app_data.get("market_session", {})
        ),
        auto_execution=AutoExecutionConfig.model_validate(
            app_data.get("auto_execution", {})
        ),
        semi_execution=SemiExecutionConfig.model_validate(
            app_data.get("semi_execution", {})
        ),
        strategy=StrategyConfig.model_validate(app_data["strategy"]),
        scoring=ScoringConfig.model_validate(scoring_data["scoring"]),
        calendar=CalendarConfig.model_validate(events_data["calendar"]),
        instruments=[
            Instrument.model_validate(item) for item in instruments_data["instruments"]
        ],
        secrets=SecretSettings(_env_file=root / ".env"),
    )
