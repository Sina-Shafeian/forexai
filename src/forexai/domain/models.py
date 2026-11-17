from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class EntryType(StrEnum):
    MARKET = "MARKET"
    BUY_LIMIT = "BUY_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_LIMIT = "SELL_LIMIT"
    SELL_STOP = "SELL_STOP"


class StructureState(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    MIXED = "MIXED"
    UNCONFIRMED = "UNCONFIRMED"


class Alignment(StrEnum):
    ALIGNED = "ALIGNED"
    CONFLICTED = "CONFLICTED"
    INSUFFICIENT = "INSUFFICIENT"


class DecisionStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"
    BLOCKED_NEWS = "BLOCKED_NEWS"
    SENT = "SENT"
    NO_TRADE = "NO_TRADE"
    STALE_ENTRY = "STALE_ENTRY"
    ENTRY_MISSED = "ENTRY_MISSED"
    SUPPRESSED_EXISTING_POSITION = "SUPPRESSED_EXISTING_POSITION"
    SUPPRESSED_SESSION_CLOSING = "SUPPRESSED_SESSION_CLOSING"
    SUPPRESSED_MARKET_CLOSED = "SUPPRESSED_MARKET_CLOSED"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    SESSION_STATUS_UNAVAILABLE = "SESSION_STATUS_UNAVAILABLE"
    POSITION_STATUS_UNAVAILABLE = "POSITION_STATUS_UNAVAILABLE"


class Instrument(BaseModel):
    canonical: str
    broker_symbol: str
    kind: str = "forex"
    base: str
    quote: str
    enabled: bool = True

    @field_validator("canonical", "broker_symbol", "base", "quote")
    @classmethod
    def uppercase_identifiers(cls, value: str) -> str:
        return value.strip().upper()


class SymbolSnapshot(BaseModel):
    canonical: str
    broker_symbol: str
    point: float = Field(gt=0)
    digits: int = Field(ge=0)
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    spread_points: float = Field(ge=0)
    observed_at: datetime
    raw_observed_at: datetime | None = None
    broker_offset_minutes: int = 0
    time_warnings: list[str] = Field(default_factory=list)
    trade_contract_size: float = Field(default=0, ge=0)
    trade_tick_size: float = Field(default=0, ge=0)
    trade_tick_value: float = Field(default=0, ge=0)
    trade_tick_value_loss: float = Field(default=0, ge=0)
    volume_min: float = Field(default=0, ge=0)
    volume_max: float = Field(default=0, ge=0)
    volume_step: float = Field(default=0, ge=0)
    trade_mode: int = 0
    trade_execution_mode: int = 0
    filling_mode: int = 0
    order_mode: int = 0
    trade_stops_level: int = Field(default=0, ge=0)

    @field_validator("observed_at", "raw_observed_at")
    @classmethod
    def snapshot_times_must_be_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("MT5 timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class AccountSnapshot(BaseModel):
    login: int
    server: str
    currency: str
    balance: float = Field(ge=0)
    leverage: int = Field(gt=0)
    trade_mode: str
    equity: float = 0
    margin: float = 0
    free_margin: float = 0
    margin_level: float = 0
    trade_allowed: bool = False
    trade_expert: bool = False


class OpenPosition(BaseModel):
    ticket: int
    broker_symbol: str
    direction: Direction
    volume: float = Field(gt=0)
    price_open: float = Field(gt=0)
    stop_loss: float = Field(ge=0)
    take_profit: float = Field(ge=0)
    magic: int = 0


class SymbolTradingSession(BaseModel):
    broker_symbol: str
    day_of_week: int = Field(ge=0, le=6)
    session_index: int = Field(ge=0)
    start_seconds: int = Field(ge=0, le=86_400)
    end_seconds: int = Field(ge=0, le=86_400)


class PositionSizeRecommendation(BaseModel):
    account_balance: float = Field(ge=0)
    account_currency: str
    risk_percent: float = Field(gt=0)
    maximum_loss: float = Field(ge=0)
    suggested_volume_lots: float = Field(ge=0)
    loss_per_lot: float | None = Field(default=None, ge=0)
    calculation_source: str
    unavailable_reason: str | None = None

    @property
    def suggested_lots(self) -> float:
        return self.suggested_volume_lots


class EconomicEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    timestamp: datetime
    name: str
    currency: str | None = None
    country: str | None = None
    importance: str = Field(
        default="Unknown",
        validation_alias=AliasChoices("importance", "impact"),
    )
    actual: float | str | None = None
    forecast: float | str | None = None
    previous: float | str | None = None

    @property
    def impact(self) -> str:
        """Backward-compatible name used by the existing Event Guard."""
        return self.importance

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class Signal(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    signal_id: str
    instrument: str
    direction: Direction
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit: float
    risk_reward: float = Field(ge=0)
    confidence_score: int = Field(ge=0, le=100)
    score_breakdown: dict[str, int]
    setup_timeframe: str
    signal_created_at_utc: datetime = Field(
        validation_alias=AliasChoices("signal_created_at_utc", "issued_at")
    )
    trigger_bar_time_utc: datetime = Field(
        validation_alias=AliasChoices("trigger_bar_time_utc", "trigger_bar_time")
    )
    current_market_price: float = Field(gt=0)
    suggested_entry: float = Field(gt=0)
    entry_type: EntryType = EntryType.MARKET
    entry_deviation: float = Field(ge=0)
    entry_source: str
    reasons: list[str]
    invalidation: str
    nearby_event_warning: str | None = None
    rule_version: str
    status: DecisionStatus = DecisionStatus.CANDIDATE
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("signal_created_at_utc", "trigger_bar_time_utc")
    @classmethod
    def signal_times_must_be_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Signal timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @property
    def issued_at(self) -> datetime:
        """Compatibility accessor for existing storage integrations."""
        return self.signal_created_at_utc

    @property
    def trigger_bar_time(self) -> datetime:
        """Compatibility accessor for existing storage integrations."""
        return self.trigger_bar_time_utc

    @classmethod
    def make_id(
        cls,
        instrument: str,
        direction: Direction,
        setup_timeframe: str,
        trigger_bar_time: datetime,
        rule_version: str,
    ) -> str:
        if trigger_bar_time.tzinfo is None:
            raise ValueError("trigger_bar_time must be timezone-aware")
        identity = "|".join(
            (
                instrument.upper(),
                direction.value,
                setup_timeframe.upper(),
                trigger_bar_time.astimezone(timezone.utc).isoformat(),
                rule_version,
            )
        )
        return sha256(identity.encode("utf-8")).hexdigest()[:24]


class AnalysisDecision(BaseModel):
    instrument: str
    trigger_bar_time: datetime | None = None
    status: DecisionStatus
    reasons: list[str]
    signal: Signal | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
