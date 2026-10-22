-- Procurement ETL - target schema (PostgreSQL 14+)
-- Safe to run many times.

CREATE SCHEMA IF NOT EXISTS procurement;
SET search_path TO procurement;

-- One row per pipeline execution: the audit trail of every load.
CREATE TABLE IF NOT EXISTS etl_run_log (
    run_id              BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'success', 'failed')),
    rows_extracted      INTEGER,
    duplicates_removed  INTEGER,
    rows_rejected       INTEGER,
    rows_loaded         INTEGER,
    fx_source           TEXT,
    fx_date             DATE,
    error_message       TEXT
);

-- Supplier master data (type 1 dimension: latest values win).
CREATE TABLE IF NOT EXISTS dim_supplier (
    supplier_code       TEXT PRIMARY KEY,
    supplier_name       TEXT NOT NULL,
    country             CHAR(2) NOT NULL,
    default_currency    CHAR(3) NOT NULL,
    payment_terms_days  INTEGER NOT NULL CHECK (payment_terms_days >= 0),
    is_active           BOOLEAN NOT NULL,
    contact_email       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Purchase order lines: the grain is one PO line.
CREATE TABLE IF NOT EXISTS fact_po_line (
    po_number           TEXT NOT NULL,
    line_number         INTEGER NOT NULL,
    supplier_code       TEXT NOT NULL REFERENCES dim_supplier (supplier_code),
    item_code           TEXT NOT NULL,
    item_description    TEXT,
    category            TEXT NOT NULL,
    buyer               TEXT,
    order_date          DATE NOT NULL,
    promised_date       DATE NOT NULL,
    received_date       DATE,
    quantity            NUMERIC(14, 3) NOT NULL CHECK (quantity > 0),
    unit_price          NUMERIC(14, 4) NOT NULL CHECK (unit_price >= 0),
    currency            CHAR(3) NOT NULL,
    amount_local        NUMERIC(16, 2) NOT NULL,
    fx_rate_to_usd      NUMERIC(14, 6) NOT NULL CHECK (fx_rate_to_usd > 0),
    amount_usd          NUMERIC(16, 2) NOT NULL,
    days_late           INTEGER,
    on_time             BOOLEAN,
    run_id              BIGINT NOT NULL REFERENCES etl_run_log (run_id),
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (po_number, line_number),
    CHECK (promised_date >= order_date)
);

CREATE INDEX IF NOT EXISTS ix_fact_po_line_supplier ON fact_po_line (supplier_code);
CREATE INDEX IF NOT EXISTS ix_fact_po_line_order_date ON fact_po_line (order_date);

-- Quarantine: rows that failed validation, with every reason, for follow-up.
CREATE TABLE IF NOT EXISTS rejected_record (
    rejected_id         BIGSERIAL PRIMARY KEY,
    run_id              BIGINT NOT NULL REFERENCES etl_run_log (run_id),
    source              TEXT NOT NULL,
    raw_record          JSONB NOT NULL,
    reasons             TEXT[] NOT NULL,
    rejected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
