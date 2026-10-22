"""Load step: write to PostgreSQL idempotently and reconcile what was written."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

FACT_COLUMNS = [
    "po_number", "line_number", "supplier_code", "item_code", "item_description",
    "category", "buyer", "order_date", "promised_date", "received_date", "quantity",
    "unit_price", "currency", "amount_local", "fx_rate_to_usd", "amount_usd",
    "days_late", "on_time",
]

SUPPLIER_COLUMNS = [
    "supplier_code", "supplier_name", "country", "default_currency",
    "payment_terms_days", "is_active", "contact_email",
]


class ReconciliationError(RuntimeError):
    """Raised when the database does not match what the pipeline meant to load."""


def apply_schema(conn: psycopg.Connection, sql_dir: Path) -> None:
    """Run every .sql file in order (they are idempotent)."""
    for path in sorted(sql_dir.glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))
        log.info("Applied %s", path.name)
    conn.commit()


def start_run(conn: psycopg.Connection) -> int:
    run_id = conn.execute(
        "INSERT INTO procurement.etl_run_log DEFAULT VALUES RETURNING run_id"
    ).fetchone()[0]
    conn.commit()  # committed on its own so a failed load still leaves a trace
    return run_id


def finish_run(conn: psycopg.Connection, run_id: int, *, status: str, **metrics) -> None:
    conn.execute(
        """
        UPDATE procurement.etl_run_log
           SET finished_at = now(), status = %(status)s,
               rows_extracted = %(rows_extracted)s, duplicates_removed = %(duplicates_removed)s,
               rows_rejected = %(rows_rejected)s, rows_loaded = %(rows_loaded)s,
               fx_source = %(fx_source)s, fx_date = %(fx_date)s,
               error_message = %(error_message)s
         WHERE run_id = %(run_id)s
        """,
        {
            "run_id": run_id,
            "status": status,
            "rows_extracted": metrics.get("rows_extracted"),
            "duplicates_removed": metrics.get("duplicates_removed"),
            "rows_rejected": metrics.get("rows_rejected"),
            "rows_loaded": metrics.get("rows_loaded"),
            "fx_source": metrics.get("fx_source"),
            "fx_date": metrics.get("fx_date") or None,
            "error_message": metrics.get("error_message"),
        },
    )
    conn.commit()


def _records(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    """DataFrame -> list of tuples with pandas NA/NaT converted to None."""
    clean = df[columns].astype(object).where(df[columns].notna(), None)
    return [tuple(row) for row in clean.itertuples(index=False, name=None)]


def upsert_suppliers(conn: psycopg.Connection, suppliers: pd.DataFrame) -> int:
    cols = ", ".join(SUPPLIER_COLUMNS)
    placeholders = ", ".join(["%s"] * len(SUPPLIER_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in SUPPLIER_COLUMNS[1:])
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO procurement.dim_supplier ({cols}) VALUES ({placeholders})
            ON CONFLICT (supplier_code) DO UPDATE SET {updates}, updated_at = now()
            """,
            _records(suppliers, SUPPLIER_COLUMNS),
        )
    return len(suppliers)


def upsert_po_lines(conn: psycopg.Connection, lines: pd.DataFrame, run_id: int) -> int:
    """Insert new PO lines and update existing ones (re-running is safe)."""
    cols = ", ".join(FACT_COLUMNS + ["run_id"])
    placeholders = ", ".join(["%s"] * (len(FACT_COLUMNS) + 1))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in FACT_COLUMNS[2:])
    rows = [r + (run_id,) for r in _records(lines, FACT_COLUMNS)]
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO procurement.fact_po_line ({cols}) VALUES ({placeholders})
            ON CONFLICT (po_number, line_number) DO UPDATE
               SET {updates}, run_id = EXCLUDED.run_id, loaded_at = now()
            """,
            rows,
        )
    return len(rows)


def insert_rejected(conn: psycopg.Connection, rejected: list[dict], run_id: int) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO procurement.rejected_record (run_id, source, raw_record, reasons)
            VALUES (%s, %s, %s, %s)
            """,
            [(run_id, "purchase_orders.csv", Jsonb(r["raw_record"]), r["reasons"]) for r in rejected],
        )
    return len(rejected)


def reconcile(conn: psycopg.Connection, run_id: int, expected: pd.DataFrame, rejected_count: int) -> None:
    """Compare row counts and USD totals in the database against the in-memory batch."""
    db_rows, db_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_usd), 0) FROM procurement.fact_po_line WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    db_rejected = conn.execute(
        "SELECT COUNT(*) FROM procurement.rejected_record WHERE run_id = %s", (run_id,)
    ).fetchone()[0]

    expected_total = round(float(expected["amount_usd"].sum()), 2)
    problems = []
    if db_rows != len(expected):
        problems.append(f"row count {db_rows} != expected {len(expected)}")
    if abs(float(db_total) - expected_total) > 0.01:
        problems.append(f"USD total {float(db_total):.2f} != expected {expected_total:.2f}")
    if db_rejected != rejected_count:
        problems.append(f"rejected count {db_rejected} != expected {rejected_count}")
    if problems:
        raise ReconciliationError("; ".join(problems))
    log.info("Reconciliation OK: %d rows, USD %.2f, %d rejected", db_rows, expected_total, db_rejected)
