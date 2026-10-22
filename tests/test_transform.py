from datetime import date

import pandas as pd
import pytest

from procurement_etl.transform import (
    enrich,
    normalize_category,
    normalize_code,
    parse_date,
    parse_number,
    transform_purchase_orders,
    transform_suppliers,
    validate_row,
)

SUPPLIERS = {"SUP-001", "SUP-002"}
CURRENCIES = {"USD", "EUR"}


def good_row(**overrides) -> dict:
    row = {
        "po_number": "4500000001",
        "line_number": "10",
        "supplier_code": "SUP-001",
        "item_code": "RM-1001",
        "item_description": "Medical grade PVC resin",
        "category": "Raw Material",
        "order_date": "2026-03-01",
        "promised_date": "2026-03-20",
        "received_date": "2026-03-18",
        "quantity": "1,200",
        "unit_price": "2.50",
        "currency": "USD",
        "buyer": "rdejesus",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-03-05", date(2026, 3, 5)),
        ("05-Mar-2026", date(2026, 3, 5)),
        ("2026/03/05", date(2026, 3, 5)),
        (" 2026-03-05 ", date(2026, 3, 5)),
        ("31/02/2026", None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [("1,200", 1200.0), ("3.25", 3.25), ("", None), ("abc", None)])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_normalize_code():
    assert normalize_code("  sup-001 ") == "SUP-001"


@pytest.mark.parametrize("raw, expected", [("PACKAGING", "Packaging"), ("mro", "MRO"), ("raw material", "Raw Material")])
def test_normalize_category(raw, expected):
    assert normalize_category(raw) == expected


def test_valid_row_has_no_reasons():
    parsed, reasons = validate_row(good_row(), SUPPLIERS, CURRENCIES)
    assert reasons == []
    assert parsed["quantity"] == 1200.0
    assert parsed["order_date"] == date(2026, 3, 1)


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"supplier_code": ""}, "missing_supplier_code"),
        ({"supplier_code": "SUP-999"}, "unknown_supplier"),
        ({"order_date": "31/02/2026"}, "invalid_or_missing_order_date"),
        ({"received_date": "2026-02-01"}, "received_before_order"),
        ({"promised_date": "2026-02-01"}, "promised_before_order"),
        ({"quantity": "-5"}, "non_positive_quantity"),
        ({"quantity": "0"}, "non_positive_quantity"),
        ({"unit_price": ""}, "missing_or_invalid_unit_price"),
        ({"currency": "XYZ"}, "unsupported_currency"),
    ],
)
def test_each_rule_is_detected(overrides, reason):
    _, reasons = validate_row(good_row(**overrides), SUPPLIERS, CURRENCIES)
    assert reason in reasons


def test_all_problems_are_reported_not_just_the_first():
    _, reasons = validate_row(good_row(quantity="-1", currency="XYZ"), SUPPLIERS, CURRENCIES)
    assert {"non_positive_quantity", "unsupported_currency"} <= set(reasons)


def test_exact_duplicates_removed_and_key_conflicts_quarantined():
    raw = pd.DataFrame(
        [
            good_row(),
            good_row(),                                    # exact duplicate -> dropped
            good_row(line_number="20"),
            good_row(line_number="20", unit_price="9.99"),  # same key, different data
        ]
    )
    result = transform_purchase_orders(raw, SUPPLIERS, CURRENCIES)
    assert result.duplicates_removed == 1
    assert len(result.valid) == 1
    assert [r["reasons"] for r in result.rejected] == [["duplicate_key_conflict"]] * 2
    # every row is accounted for
    assert result.duplicates_removed + len(result.valid) + len(result.rejected) == len(raw)


def test_enrich_converts_currency_and_flags_lateness():
    raw = pd.DataFrame(
        [
            good_row(currency="EUR", quantity="100", unit_price="0.86",
                     promised_date="2026-03-20", received_date="2026-03-25"),
            good_row(line_number="20", received_date=""),
        ]
    )
    result = transform_purchase_orders(raw, SUPPLIERS, CURRENCIES)
    df = enrich(result.valid, {"USD": 1.0, "EUR": 0.86}).set_index("line_number")
    assert df.loc[10, "amount_local"] == 86.0
    assert df.loc[10, "amount_usd"] == 100.0
    assert df.loc[10, "days_late"] == 5
    assert df.loc[10, "on_time"] == False  # noqa: E712
    assert pd.isna(df.loc[20, "on_time"])  # not received yet -> unknown, not "late"


def test_enrich_fails_loudly_on_missing_rate():
    raw = pd.DataFrame([good_row(currency="EUR")])
    result = transform_purchase_orders(raw, SUPPLIERS, CURRENCIES)
    with pytest.raises(ValueError, match="No FX rate"):
        enrich(result.valid, {"USD": 1.0})


def test_supplier_master_deduplicated_on_normalized_code():
    records = [
        {"supplier_code": "SUP-003", "name": "MedPack", "country": "mx",
         "default_currency": "MXN", "payment_terms_days": 30, "active": True},
        {"supplier_code": " sup-003", "name": "MEDPACK ", "country": "MX",
         "default_currency": "MXN", "payment_terms_days": 30, "active": True},
    ]
    df, removed = transform_suppliers(records)
    assert removed == 1
    assert df.loc[0, "supplier_name"] == "MedPack"
    assert df.loc[0, "country"] == "MX"
