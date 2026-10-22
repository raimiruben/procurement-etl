"""Transform step: clean, standardize, validate and enrich the raw records."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

log = logging.getLogger(__name__)

# Date formats observed in the ERP export (ISO, SAP style, slashes).
DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%Y/%m/%d")
KEY_COLUMNS = ["po_number", "line_number"]
# Canonical category names; anything else is title-cased.
CATEGORIES = {"RAW MATERIAL": "Raw Material", "COMPONENT": "Component",
              "PACKAGING": "Packaging", "MRO": "MRO"}


# ---------------------------------------------------------------- parsing helpers
def clean_text(value: str | None) -> str:
    """Trim and collapse internal whitespace."""
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalize_code(value: str | None) -> str:
    """Codes are compared case-insensitively and without padding: ' sup-001 ' -> 'SUP-001'."""
    return clean_text(value).upper()


def normalize_category(value: str | None) -> str:
    """'PACKAGING' -> 'Packaging', 'mro' -> 'MRO'."""
    text = clean_text(value)
    return CATEGORIES.get(text.upper(), text.title())


def parse_date(value: str | None) -> date | None:
    """Parse any of the known formats. Returns None if blank or unparseable."""
    text = clean_text(value)
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value: str | None) -> float | None:
    """Parse '1,200' or '3.25'. Returns None if blank or not a number."""
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- suppliers
def transform_suppliers(records: list[dict]) -> tuple[pd.DataFrame, int]:
    """Flatten and de-duplicate the supplier master data.

    Duplicates are detected on the normalized code; the first record wins.
    Returns (suppliers, duplicates_removed).
    """
    rows = []
    for rec in records:
        rows.append(
            {
                "supplier_code": normalize_code(rec.get("supplier_code")),
                "supplier_name": clean_text(rec.get("name")),
                "country": normalize_code(rec.get("country")),
                "default_currency": normalize_code(rec.get("default_currency")),
                "payment_terms_days": int(rec.get("payment_terms_days", 0)),
                "is_active": bool(rec.get("active", True)),
                "contact_email": clean_text((rec.get("contact") or {}).get("email")) or None,
            }
        )
    df = pd.DataFrame(rows)
    before = len(df)
    df = df[df["supplier_code"] != ""].drop_duplicates(subset="supplier_code", keep="first")
    removed = before - len(df)
    if removed:
        log.info("Removed %d duplicate/blank supplier record(s)", removed)
    return df.reset_index(drop=True), removed


# ---------------------------------------------------------------- purchase orders
@dataclass
class ValidationResult:
    valid: pd.DataFrame
    rejected: list[dict]          # {"raw_record": {...}, "reasons": [...]}
    duplicates_removed: int


def validate_row(
    row: dict, known_suppliers: set[str], supported_currencies: set[str]
) -> tuple[dict, list[str]]:
    """Parse one raw PO line and return (parsed_values, list_of_problems).

    Every rule is checked so the quarantine shows *all* reasons, not just the first.
    """
    reasons: list[str] = []

    supplier = normalize_code(row.get("supplier_code"))
    if not supplier:
        reasons.append("missing_supplier_code")
    elif supplier not in known_suppliers:
        reasons.append("unknown_supplier")

    order_date = parse_date(row.get("order_date"))
    promised_date = parse_date(row.get("promised_date"))
    received_raw = clean_text(row.get("received_date"))
    received_date = parse_date(received_raw)

    if order_date is None:
        reasons.append("invalid_or_missing_order_date")
    if promised_date is None:
        reasons.append("invalid_or_missing_promised_date")
    if received_raw and received_date is None:
        reasons.append("invalid_received_date")
    if order_date and promised_date and promised_date < order_date:
        reasons.append("promised_before_order")
    if order_date and received_date and received_date < order_date:
        reasons.append("received_before_order")

    quantity = parse_number(row.get("quantity"))
    if quantity is None:
        reasons.append("invalid_quantity")
    elif quantity <= 0:
        reasons.append("non_positive_quantity")

    unit_price = parse_number(row.get("unit_price"))
    if unit_price is None:
        reasons.append("missing_or_invalid_unit_price")
    elif unit_price < 0:
        reasons.append("negative_unit_price")

    currency = normalize_code(row.get("currency"))
    if currency not in supported_currencies:
        reasons.append("unsupported_currency")

    line_number = parse_number(row.get("line_number"))
    po_number = clean_text(row.get("po_number"))
    if not po_number or line_number is None:
        reasons.append("missing_key")

    parsed = {
        "po_number": po_number,
        "line_number": int(line_number) if line_number is not None else None,
        "supplier_code": supplier,
        "item_code": normalize_code(row.get("item_code")),
        "item_description": clean_text(row.get("item_description")) or None,
        "category": normalize_category(row.get("category")),
        "buyer": clean_text(row.get("buyer")).lower() or None,
        "order_date": order_date,
        "promised_date": promised_date,
        "received_date": received_date,
        "quantity": quantity,
        "unit_price": unit_price,
        "currency": currency,
    }
    return parsed, reasons


def transform_purchase_orders(
    raw: pd.DataFrame, known_suppliers: set[str], supported_currencies: set[str]
) -> ValidationResult:
    """Remove exact duplicates, validate every line, and split valid vs rejected."""
    before = len(raw)
    deduped = raw.drop_duplicates(keep="first")
    duplicates_removed = before - len(deduped)
    if duplicates_removed:
        log.info("Removed %d exact duplicate line(s)", duplicates_removed)

    valid_rows, rejected = [], []
    for record in deduped.to_dict(orient="records"):
        parsed, reasons = validate_row(record, known_suppliers, supported_currencies)
        if reasons:
            rejected.append({"raw_record": record, "reasons": reasons})
        else:
            valid_rows.append(parsed)

    valid = pd.DataFrame(valid_rows)

    # Same PO line appearing twice with *different* content: we cannot know which
    # one is right, so both go to quarantine for a human to resolve.
    if not valid.empty:
        conflict_mask = valid.duplicated(subset=KEY_COLUMNS, keep=False)
        if conflict_mask.any():
            conflicts = valid[conflict_mask]
            for record in conflicts.to_dict(orient="records"):
                rejected.append(
                    {"raw_record": _jsonable(record), "reasons": ["duplicate_key_conflict"]}
                )
            valid = valid[~conflict_mask]
            log.warning("Quarantined %d line(s) with conflicting duplicate keys", len(conflicts))

    log.info("Validation: %d valid, %d rejected", len(valid), len(rejected))
    return ValidationResult(valid.reset_index(drop=True), rejected, duplicates_removed)


def enrich(valid: pd.DataFrame, fx_rates: dict[str, float]) -> pd.DataFrame:
    """Add amounts in local currency and USD, and delivery performance fields."""
    df = valid.copy()
    df["amount_local"] = (df["quantity"] * df["unit_price"]).round(2)
    df["fx_rate_to_usd"] = df["currency"].map(fx_rates)
    if df["fx_rate_to_usd"].isna().any():
        missing = sorted(df.loc[df["fx_rate_to_usd"].isna(), "currency"].unique())
        raise ValueError(f"No FX rate for currencies: {missing}")
    df["amount_usd"] = (df["amount_local"] / df["fx_rate_to_usd"]).round(2)

    received = pd.to_datetime(df["received_date"])
    promised = pd.to_datetime(df["promised_date"])
    days_late = (received - promised).dt.days
    df["days_late"] = days_late.astype("Int64")
    df["on_time"] = (days_late <= 0).where(received.notna())
    return df


def _jsonable(record: dict) -> dict:
    """Make a parsed record safe to store as JSONB (dates -> ISO strings)."""
    out = {}
    for key, value in record.items():
        if isinstance(value, (date, datetime)):
            out[key] = value.isoformat()
        elif value is None or (isinstance(value, float) and pd.isna(value)):
            out[key] = None
        else:
            out[key] = value
    return out
