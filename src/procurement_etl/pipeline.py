"""Command-line entry point: extract -> transform -> load -> reconcile.

Examples:
    python -m procurement_etl.pipeline                     # full run
    python -m procurement_etl.pipeline --fx-date 2026-09-30
    python -m procurement_etl.pipeline --offline           # no API call
    python -m procurement_etl.pipeline --dry-run           # no database writes
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

import psycopg

from procurement_etl import extract, load, transform
from procurement_etl.config import Settings

log = logging.getLogger("procurement_etl")


def run(
    settings: Settings,
    *,
    po_file: Path | None = None,
    suppliers_file: Path | None = None,
    fx_date: str | None = None,
    offline: bool = False,
    dry_run: bool = False,
) -> dict:
    po_file = po_file or settings.raw_dir / "purchase_orders.csv"
    suppliers_file = suppliers_file or settings.raw_dir / "suppliers.json"

    # ---- Extract
    raw_po = extract.read_purchase_orders(po_file)
    raw_suppliers = extract.read_suppliers(suppliers_file)
    rates, fx_source, rate_date = extract.fetch_fx_rates(
        currencies=list(settings.supported_currencies),
        base=settings.base_currency,
        on_date=fx_date,
        api_url=settings.fx_api_url,
        timeout=settings.fx_timeout_seconds,
        fallback_path=settings.raw_dir / "fx_rates_fallback.json",
        offline=offline,
    )

    # ---- Transform
    suppliers, _ = transform.transform_suppliers(raw_suppliers)
    result = transform.transform_purchase_orders(
        raw_po,
        known_suppliers=set(suppliers["supplier_code"]),
        supported_currencies=set(settings.supported_currencies),
    )
    lines = transform.enrich(result.valid, rates)

    inactive = set(suppliers.loc[~suppliers["is_active"], "supplier_code"])
    n_inactive = int(lines["supplier_code"].isin(inactive).sum())
    if n_inactive:
        log.warning("%d loaded line(s) belong to suppliers now marked inactive", n_inactive)

    metrics = {
        "rows_extracted": len(raw_po),
        "duplicates_removed": result.duplicates_removed,
        "rows_rejected": len(result.rejected),
        "rows_loaded": len(lines),
        "fx_source": fx_source,
        "fx_date": rate_date,
    }
    # Every extracted row must be accounted for before anything is written.
    accounted = metrics["duplicates_removed"] + metrics["rows_rejected"] + metrics["rows_loaded"]
    if accounted != metrics["rows_extracted"]:
        raise load.ReconciliationError(
            f"Row accounting failed: {metrics['rows_extracted']} extracted vs {accounted} accounted"
        )

    reasons = Counter(reason for r in result.rejected for reason in r["reasons"])
    metrics["rejection_reasons"] = dict(reasons.most_common())
    metrics["total_usd"] = round(float(lines["amount_usd"].sum()), 2)

    if dry_run:
        log.info("Dry run: nothing written to the database")
        return metrics

    # ---- Load
    with psycopg.connect(settings.database_url) as conn:
        load.apply_schema(conn, settings.sql_dir)
        run_id = load.start_run(conn)
        try:
            load.upsert_suppliers(conn, suppliers)
            load.upsert_po_lines(conn, lines, run_id)
            load.insert_rejected(conn, result.rejected, run_id)
            load.reconcile(conn, run_id, lines, len(result.rejected))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            load.finish_run(
                conn, run_id, status="failed", error_message=str(exc)[:1000],
                **{**metrics, "rows_loaded": 0},
            )
            log.error("Run %d failed and was rolled back: %s", run_id, exc)
            raise
        load.finish_run(conn, run_id, status="success", **metrics)
    metrics["run_id"] = run_id
    log.info("Run %d finished successfully", run_id)
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Procurement ETL pipeline")
    parser.add_argument("--po-file", type=Path, help="Purchase-order CSV (default: data/raw)")
    parser.add_argument("--suppliers-file", type=Path, help="Supplier JSON (default: data/raw)")
    parser.add_argument("--fx-date", help="Rate date YYYY-MM-DD (default: latest)")
    parser.add_argument("--offline", action="store_true", help="Skip the FX API, use fallback file")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing to the database")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        metrics = run(
            Settings(),
            po_file=args.po_file,
            suppliers_file=args.suppliers_file,
            fx_date=args.fx_date,
            offline=args.offline,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - surface any failure as a non-zero exit
        log.error("Pipeline failed: %s", exc)
        return 1

    print("\nRun summary")
    for key, value in metrics.items():
        print(f"  {key:<20} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
