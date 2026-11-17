from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from forexai.domain.models import Direction


class ExecutionStatus(StrEnum):
    EXECUTION_IN_PROGRESS = "EXECUTION_IN_PROGRESS"
    RESERVED = "RESERVED"
    BLOCKED = "BLOCKED"
    CHECK_FAILED = "CHECK_FAILED"
    SEND_FAILED = "SEND_FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class ExecutionRecord(BaseModel):
    signal_id: str
    symbol: str
    direction: Direction
    suggested_entry: float
    actual_execution_price: float | None = None
    requested_volume: float | None = None
    executed_volume: float | None = None
    sl: float
    tp: float
    order_ticket: int | None = None
    deal_ticket: int | None = None
    position_ticket: int | None = None
    execution_status: ExecutionStatus
    reason: str | None = None
    retcode: int | None = None
    retcode_message: str | None = None
    margin_required: float | None = None
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at_utc: datetime | None = None

    @field_validator("created_at_utc", "executed_at_utc")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Execution timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class ExecutionOutcome(BaseModel):
    status: ExecutionStatus | None = None
    reason: str
    execution_enabled: bool
    actual_execution_price: float | None = None
    requested_volume: float | None = None
    executed_volume: float | None = None
    sl: float | None = None
    tp: float | None = None
    order_ticket: int | None = None
    deal_ticket: int | None = None
    position_ticket: int | None = None
    retcode: int | None = None
    retcode_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.EXECUTED


class SemiExecutionResult(BaseModel):
    signal_id: str
    action: Literal["execute", "recheck"]
    state: ExecutionStatus | None = None
    reason: str
    valid: bool = False
    symbol: str | None = None
    direction: Direction | None = None
    digits: int = 5
    current_price: float | None = None
    suggested_entry: float | None = None
    maximum_entry_deviation: float | None = None
    original_volume: float | None = None
    final_volume: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    position_ticket: int | None = None
    order_ticket: int | None = None
    retcode: int | None = None
    retcode_message: str | None = None
    occurred_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def succeeded(self) -> bool:
        return self.state is ExecutionStatus.EXECUTED
