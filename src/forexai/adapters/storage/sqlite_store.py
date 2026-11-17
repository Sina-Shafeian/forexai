from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from forexai.domain.execution import ExecutionRecord, ExecutionStatus
from forexai.domain.models import AnalysisDecision, DecisionStatus, Signal


class SQLiteDecisionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    trigger_bar_time TEXT,
                    status TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    signal_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_instrument_time
                    ON decisions(instrument, trigger_bar_time);

                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    instrument TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    trigger_bar_time TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence_score INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    external_message_id TEXT,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_sent_at ON signals(sent_at);

                CREATE TABLE IF NOT EXISTS executions (
                    signal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    suggested_entry REAL NOT NULL,
                    actual_execution_price REAL,
                    requested_volume REAL,
                    executed_volume REAL,
                    sl REAL NOT NULL,
                    tp REAL NOT NULL,
                    order_ticket INTEGER,
                    deal_ticket INTEGER,
                    position_ticket INTEGER,
                    execution_status TEXT NOT NULL,
                    reason TEXT,
                    retcode INTEGER,
                    retcode_message TEXT,
                    margin_required REAL,
                    created_at_utc TEXT NOT NULL,
                    executed_at_utc TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_executions_created_at
                    ON executions(created_at_utc);
                """
            )

    def record_decision(self, decision: AnalysisDecision) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO decisions (
                    created_at, instrument, trigger_bar_time, status,
                    reasons_json, diagnostics_json, signal_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    decision.instrument,
                    decision.trigger_bar_time.isoformat()
                    if decision.trigger_bar_time
                    else None,
                    decision.status.value,
                    json.dumps(decision.reasons, ensure_ascii=False),
                    json.dumps(decision.diagnostics, ensure_ascii=False, default=str),
                    decision.signal.signal_id if decision.signal else None,
                ),
            )

    def reserve_signal(self, signal: Signal) -> bool:
        """Reserve an id, allowing retry only when no prior send succeeded."""
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT sent_at, external_message_id FROM signals WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["external_message_id"] or "").startswith("console:"):
                    connection.execute(
                        "DELETE FROM signals WHERE signal_id = ?", (signal.signal_id,)
                    )
                else:
                    return existing["sent_at"] is None
            try:
                connection.execute(
                    """
                    INSERT INTO signals (
                        signal_id, instrument, direction, trigger_bar_time,
                        issued_at, status, confidence_score, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.signal_id,
                        signal.instrument,
                        signal.direction.value,
                        signal.trigger_bar_time.isoformat(),
                        signal.issued_at.isoformat(),
                        signal.status.value,
                        signal.confidence_score,
                        signal.model_dump_json(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_sent(self, signal_id: str, external_message_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE signals
                SET status = ?, external_message_id = ?, sent_at = ?
                WHERE signal_id = ?
                """,
                (
                    DecisionStatus.SENT.value,
                    external_message_id,
                    datetime.now(timezone.utc).isoformat(),
                    signal_id,
                ),
            )

    def sent_count_for_utc_day(self, day: datetime) -> int:
        day = day.astimezone(timezone.utc)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM signals
                WHERE sent_at BETWEEN ? AND ?
                  AND COALESCE(external_message_id, '') NOT LIKE 'console:%'
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        return int(row["count"])

    def reserve_execution(self, record: ExecutionRecord) -> bool:
        """Atomically reserve a signal id before any call that can place an order."""
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO executions (
                        signal_id, symbol, direction, suggested_entry, sl, tp,
                        execution_status, reason, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.signal_id,
                        record.symbol,
                        record.direction.value,
                        record.suggested_entry,
                        record.sl,
                        record.tp,
                        record.execution_status.value,
                        record.reason,
                        record.created_at_utc.isoformat(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def update_execution(self, record: ExecutionRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE executions SET
                    actual_execution_price = ?, requested_volume = ?,
                    executed_volume = ?, order_ticket = ?, deal_ticket = ?,
                    position_ticket = ?, execution_status = ?, reason = ?,
                    retcode = ?, retcode_message = ?, margin_required = ?,
                    executed_at_utc = ?
                WHERE signal_id = ?
                """,
                (
                    record.actual_execution_price,
                    record.requested_volume,
                    record.executed_volume,
                    record.order_ticket,
                    record.deal_ticket,
                    record.position_ticket,
                    record.execution_status.value,
                    record.reason,
                    record.retcode,
                    record.retcode_message,
                    record.margin_required,
                    record.executed_at_utc.isoformat()
                    if record.executed_at_utc
                    else None,
                    record.signal_id,
                ),
            )

    def get_execution(self, signal_id: str) -> ExecutionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        if row is None:
            return None
        return ExecutionRecord(
            signal_id=row["signal_id"],
            symbol=row["symbol"],
            direction=row["direction"],
            suggested_entry=row["suggested_entry"],
            actual_execution_price=row["actual_execution_price"],
            requested_volume=row["requested_volume"],
            executed_volume=row["executed_volume"],
            sl=row["sl"],
            tp=row["tp"],
            order_ticket=row["order_ticket"],
            deal_ticket=row["deal_ticket"],
            position_ticket=row["position_ticket"],
            execution_status=row["execution_status"],
            reason=row["reason"],
            retcode=row["retcode"],
            retcode_message=row["retcode_message"],
            margin_required=row["margin_required"],
            created_at_utc=datetime.fromisoformat(row["created_at_utc"]),
            executed_at_utc=(
                datetime.fromisoformat(row["executed_at_utc"])
                if row["executed_at_utc"]
                else None
            ),
        )

    def get_signal(self, signal_id: str) -> Signal | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        if row is None:
            return None
        return Signal.model_validate_json(row["payload_json"])

    def execution_status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT execution_status, COUNT(*) AS count
                FROM executions GROUP BY execution_status
                """
            ).fetchall()
        return {str(row["execution_status"]): int(row["count"]) for row in rows}
