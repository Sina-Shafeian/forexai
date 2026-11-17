from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from forexai.analysis.time_normalization import normalize_broker_epoch
from forexai.domain.models import SymbolTradingSession


class SessionCatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionCatalog:
    generated_at_utc: datetime
    symbols: frozenset[str]
    sessions: tuple[SymbolTradingSession, ...]

    def for_symbol(self, broker_symbol: str) -> list[SymbolTradingSession] | None:
        normalized = broker_symbol.upper()
        if normalized not in self.symbols:
            return None
        return [
            session
            for session in self.sessions
            if session.broker_symbol.upper() == normalized
        ]


def load_session_catalog(
    path: Path,
    *,
    broker_offset_minutes: int,
    now_utc: datetime,
    maximum_age_minutes: int,
    future_tolerance_seconds: int = 5,
) -> SessionCatalog:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.reader(handle, delimiter=";") if row]
    except OSError as exc:
        raise SessionCatalogError(f"Session export unavailable: {path}: {exc}") from exc
    if not rows or rows[0] != ["FOREXAI_SESSION_EXPORT_V1"]:
        raise SessionCatalogError("Session export header is invalid")
    if rows[-1] != ["END"]:
        raise SessionCatalogError("Session export is incomplete")

    generated_epoch: float | None = None
    symbols: set[str] = set()
    sessions: list[SymbolTradingSession] = []
    try:
        for row in rows[1:-1]:
            if row[0] == "generated_server_epoch" and len(row) == 2:
                generated_epoch = float(row[1])
            elif row[0] == "symbol" and len(row) == 2:
                symbols.add(row[1].strip().upper())
            elif row[0] == "session" and len(row) == 6:
                sessions.append(
                    SymbolTradingSession(
                        broker_symbol=row[1],
                        day_of_week=int(row[2]),
                        session_index=int(row[3]),
                        start_seconds=int(row[4]),
                        end_seconds=int(row[5]),
                    )
                )
            else:
                raise SessionCatalogError(f"Invalid session export row: {row}")
    except (TypeError, ValueError) as exc:
        raise SessionCatalogError(f"Invalid session export value: {exc}") from exc
    if generated_epoch is None or not symbols:
        raise SessionCatalogError("Session export metadata is incomplete")

    generated_utc = normalize_broker_epoch(
        generated_epoch, broker_offset_minutes
    )
    age_seconds = (
        now_utc.astimezone(timezone.utc) - generated_utc
    ).total_seconds()
    if age_seconds < -future_tolerance_seconds:
        raise SessionCatalogError("Session export timestamp is in the future")
    if age_seconds > maximum_age_minutes * 60:
        raise SessionCatalogError(
            f"Session export is stale ({age_seconds / 60:.1f} minutes old)"
        )
    return SessionCatalog(
        generated_at_utc=generated_utc,
        symbols=frozenset(symbols),
        sessions=tuple(sessions),
    )
