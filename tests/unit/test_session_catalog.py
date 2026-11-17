from datetime import datetime, timedelta, timezone

import pytest

from forexai.adapters.mt5.session_catalog import (
    SessionCatalogError,
    load_session_catalog,
)


def _write_catalog(path, generated_epoch: float, *, complete: bool = True) -> None:
    end = "END\n" if complete else ""
    path.write_text(
        "FOREXAI_SESSION_EXPORT_V1\n"
        f"generated_server_epoch;{generated_epoch}\n"
        "symbol;XAUUSD\n"
        "session;XAUUSD;1;0;0;82800\n"
        f"{end}",
        encoding="utf-8",
    )


def test_valid_export_is_normalized_and_loaded(tmp_path) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    path = tmp_path / "sessions.csv"
    _write_catalog(path, (now + timedelta(hours=3)).timestamp())
    catalog = load_session_catalog(
        path,
        broker_offset_minutes=180,
        now_utc=now,
        maximum_age_minutes=15,
    )
    assert catalog.generated_at_utc == now
    assert len(catalog.for_symbol("XAUUSD")) == 1
    assert catalog.for_symbol("EURUSD") is None


def test_incomplete_or_stale_export_is_rejected(tmp_path) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    incomplete = tmp_path / "incomplete.csv"
    _write_catalog(incomplete, now.timestamp(), complete=False)
    with pytest.raises(SessionCatalogError, match="incomplete"):
        load_session_catalog(
            incomplete,
            broker_offset_minutes=0,
            now_utc=now,
            maximum_age_minutes=15,
        )

    stale = tmp_path / "stale.csv"
    _write_catalog(stale, (now - timedelta(minutes=16)).timestamp())
    with pytest.raises(SessionCatalogError, match="stale"):
        load_session_catalog(
            stale,
            broker_offset_minutes=0,
            now_utc=now,
            maximum_age_minutes=15,
        )
