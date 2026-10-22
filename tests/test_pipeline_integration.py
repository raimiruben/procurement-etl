"""End-to-end test against a real PostgreSQL database.

Skipped automatically when no database is reachable at DATABASE_URL.
"""

import psycopg
import pytest

from procurement_etl import load
from procurement_etl.config import Settings
from procurement_etl.pipeline import run

settings = Settings()


def _db_available() -> bool:
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not reachable")


def test_pipeline_is_idempotent_and_reconciled():
    first = run(settings, offline=True)
    second = run(settings, offline=True)

    # Same input twice -> same result, no duplicated rows.
    for key in ("rows_extracted", "duplicates_removed", "rows_rejected", "rows_loaded", "total_usd"):
        assert first[key] == second[key]

    with psycopg.connect(settings.database_url) as conn:
        total_rows = conn.execute("SELECT COUNT(*) FROM procurement.fact_po_line").fetchone()[0]
        status = conn.execute(
            "SELECT status FROM procurement.etl_run_log WHERE run_id = %s", (second["run_id"],)
        ).fetchone()[0]
        otd_rows = conn.execute("SELECT COUNT(*) FROM procurement.vw_supplier_otd").fetchone()[0]

    assert total_rows == second["rows_loaded"]
    assert status == "success"
    assert otd_rows > 0


def test_failed_reconciliation_rolls_back_and_is_logged(monkeypatch):
    """Simulate a partial load: the pipeline must refuse to commit it."""
    real_upsert = load.upsert_po_lines

    def upsert_only_half(conn, lines, run_id):
        return real_upsert(conn, lines.iloc[: len(lines) // 2], run_id)

    monkeypatch.setattr(load, "upsert_po_lines", upsert_only_half)
    with pytest.raises(load.ReconciliationError):
        run(settings, offline=True)

    with psycopg.connect(settings.database_url) as conn:
        run_id, status, error = conn.execute(
            "SELECT run_id, status, error_message FROM procurement.etl_run_log "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        rows_from_failed_run = conn.execute(
            "SELECT COUNT(*) FROM procurement.fact_po_line WHERE run_id = %s", (run_id,)
        ).fetchone()[0]

    assert status == "failed"
    assert "row count" in error
    assert rows_from_failed_run == 0
