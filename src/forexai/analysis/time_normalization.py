from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path


LOGGER = logging.getLogger(__name__)

STALE_TICK = "STALE_TICK"
FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
BROKER_OFFSET_CHANGED = "BROKER_OFFSET_CHANGED"
OFFSET_UNCERTAIN = "BROKER_OFFSET_UNCERTAIN"


@dataclass(frozen=True)
class BrokerClockResult:
    raw_time_utc_interpretation: datetime
    normalized_time_utc: datetime
    offset_minutes: int
    tick_age_seconds: float
    warnings: tuple[str, ...]
    detection_confident: bool


def detect_broker_clock(
    raw_epoch_seconds: float,
    *,
    now_utc: datetime,
    previous_offset_minutes: int | None = None,
    offset_round_minutes: int = 15,
    maximum_offset_hours: int = 14,
    maximum_tick_age_seconds: int = 120,
    future_tolerance_seconds: int = 5,
) -> BrokerClockResult:
    """Infer a broker epoch shift from a live tick without assuming a fixed offset."""
    now = _as_utc(now_utc)
    raw = datetime.fromtimestamp(raw_epoch_seconds, tz=timezone.utc)
    round_seconds = offset_round_minutes * 60
    observed_delta = (raw - now).total_seconds()
    candidate_seconds = round(observed_delta / round_seconds) * round_seconds
    candidate_minutes = int(candidate_seconds // 60)
    candidate_normalized = raw - timedelta(seconds=candidate_seconds)
    candidate_age = (now - candidate_normalized).total_seconds()
    candidate_plausible = (
        abs(candidate_seconds) <= maximum_offset_hours * 3600
        and -future_tolerance_seconds
        <= candidate_age
        <= maximum_tick_age_seconds
    )

    warnings: list[str] = []
    if candidate_plausible:
        offset_minutes = candidate_minutes
        confident = True
        if (
            previous_offset_minutes is not None
            and previous_offset_minutes != offset_minutes
        ):
            warnings.append(BROKER_OFFSET_CHANGED)
    elif previous_offset_minutes is not None:
        offset_minutes = previous_offset_minutes
        confident = False
        warnings.append(OFFSET_UNCERTAIN)
    else:
        offset_minutes = (
            candidate_minutes
            if abs(candidate_seconds) <= maximum_offset_hours * 3600
            else 0
        )
        confident = False
        warnings.append(OFFSET_UNCERTAIN)

    normalized = raw - timedelta(minutes=offset_minutes)
    age = (now - normalized).total_seconds()
    if age > maximum_tick_age_seconds:
        warnings.append(STALE_TICK)
    if age < -future_tolerance_seconds:
        warnings.append(FUTURE_TIMESTAMP)
    return BrokerClockResult(
        raw_time_utc_interpretation=raw,
        normalized_time_utc=normalized,
        offset_minutes=offset_minutes,
        tick_age_seconds=age,
        warnings=tuple(dict.fromkeys(warnings)),
        detection_confident=confident,
    )


def normalize_broker_epoch(
    raw_epoch_seconds: float, offset_minutes: int
) -> datetime:
    raw = datetime.fromtimestamp(raw_epoch_seconds, tz=timezone.utc)
    return raw - timedelta(minutes=offset_minutes)


class BrokerClockState:
    """Small project-local state store used only to detect server offset changes."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, account_key: str) -> int | None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            value = payload.get(account_key, {}).get("offset_minutes")
            return int(value) if value is not None else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Broker clock state could not be read: %s", exc)
            return None

    def save(
        self, account_key: str, offset_minutes: int, detected_at_utc: datetime
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {}
        try:
            if self._path.exists():
                payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = {}
        payload[account_key] = {
            "offset_minutes": offset_minutes,
            "detected_at_utc": _as_utc(detected_at_utc).isoformat(),
        }
        try:
            self._path.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            LOGGER.warning("Broker clock state could not be written: %s", exc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(timezone.utc)
