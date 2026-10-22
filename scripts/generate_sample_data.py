"""Generate a reproducible, intentionally messy procurement dataset.

The data is synthetic. It imitates the kind of export you get from an ERP
(SAP / Oracle) purchase order report: mixed date formats, stray whitespace,
inconsistent casing, thousands separators, duplicates and a few broken rows.

Usage:
    python scripts/generate_sample_data.py
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
N_LINES = 600
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"

SUPPLIERS = [
    # code, name, country, currency, payment terms (days), active, reliability (0-1)
    ("SUP-001", "Caribe Plastics SRL", "DO", "USD", 30, True, 0.92),
    ("SUP-002", "Precision Wire GmbH", "DE", "EUR", 45, True, 0.85),
    ("SUP-003", "MedPack de Mexico", "MX", "MXN", 30, True, 0.70),
    ("SUP-004", "Shenzhen Micro Parts", "CN", "CNY", 60, True, 0.60),
    ("SUP-005", "Atlantic Resins Inc", "US", "USD", 30, True, 0.88),
    ("SUP-006", "Nordic Tubing AB", "SE", "EUR", 45, True, 0.80),
    ("SUP-007", "Santo Domingo Labels", "DO", "USD", 15, True, 0.95),
    ("SUP-008", "Legacy Metals Corp", "US", "USD", 30, False, 0.50),
]

ITEMS = [
    # item code, description, category, base unit price (in USD)
    ("RM-1001", "Medical grade PVC resin", "Raw Material", 2.35),
    ("RM-1002", "Polycarbonate pellets", "Raw Material", 3.10),
    ("RM-1003", "Stainless steel wire 0.3mm", "Raw Material", 0.85),
    ("CP-2001", "Luer lock connector", "Component", 0.12),
    ("CP-2002", "Silicone O-ring 5mm", "Component", 0.04),
    ("CP-2003", "Micro PCB assembly", "Component", 4.75),
    ("PK-3001", "Sterile pouch 6x9", "Packaging", 0.09),
    ("PK-3002", "Corrugated shipper box", "Packaging", 0.65),
    ("PK-3003", "Printed IFU leaflet", "Packaging", 0.03),
    ("MR-4001", "Nitrile gloves (box)", "MRO", 7.50),
]

BUYERS = ["rdejesus", "mperez", "jgarcia", "lmartinez"]
FX_TO_USD = {"USD": 1.0, "EUR": 0.86, "MXN": 18.4, "CNY": 7.12}


def fmt_date(d: date, rng: random.Random) -> str:
    """Return the date in one of the formats seen in real ERP exports."""
    style = rng.choices(["iso", "sap", "slash"], weights=[0.7, 0.2, 0.1])[0]
    if style == "iso":
        return d.isoformat()
    if style == "sap":
        return d.strftime("%d-%b-%Y")  # 05-Mar-2026
    return d.strftime("%Y/%m/%d")


def messy_code(code: str, rng: random.Random) -> str:
    r = rng.random()
    if r < 0.06:
        return f"  {code.lower()} "
    if r < 0.10:
        return code + " "
    return code


def main() -> None:
    rng = random.Random(SEED)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    start = date(2026, 1, 5)
    po_counter = 4500012000

    while len(rows) < N_LINES:
        po_counter += 1
        sup = rng.choice(SUPPLIERS[:7] if rng.random() > 0.03 else SUPPLIERS)
        code, _, _, currency, _, _, reliability = sup
        order_date = start + timedelta(days=rng.randint(0, 260))
        lead_time = rng.randint(7, 45)
        promised = order_date + timedelta(days=lead_time)
        for line in range(1, rng.randint(1, 4) + 1):
            item = rng.choice(ITEMS)
            qty = rng.choice([50, 100, 250, 500, 1000, 1200, 2500, 5000])
            price_usd = item[3] * rng.uniform(0.9, 1.15)
            unit_price = round(price_usd * FX_TO_USD[currency], 4)

            if rng.random() < reliability:
                received = promised - timedelta(days=rng.randint(0, 3))
            else:
                received = promised + timedelta(days=rng.randint(1, 20))
            # Orders placed recently may not have been received yet.
            received_str = "" if received > date(2026, 9, 30) else fmt_date(received, rng)

            qty_str = f"{qty:,}" if qty >= 1000 and rng.random() < 0.5 else str(qty)
            rows.append(
                {
                    "po_number": str(po_counter),
                    "line_number": str(line * 10),
                    "supplier_code": messy_code(code, rng),
                    "item_code": item[0],
                    "item_description": item[1],
                    "category": item[2] if rng.random() > 0.05 else item[2].upper(),
                    "order_date": fmt_date(order_date, rng),
                    "promised_date": fmt_date(promised, rng),
                    "received_date": received_str,
                    "quantity": qty_str,
                    "unit_price": f"{unit_price}",
                    "currency": currency if rng.random() > 0.08 else currency.lower(),
                    "buyer": rng.choice(BUYERS),
                }
            )

    rows = rows[:N_LINES]

    # --- Inject known data-quality problems (documented in README) ---
    broken = [
        ("quantity", "-250"),          # negative quantity
        ("quantity", "0"),             # zero quantity
        ("unit_price", ""),            # missing price
        ("unit_price", "abc"),         # non-numeric price
        ("currency", "XYZ"),           # unsupported currency
        ("supplier_code", "SUP-999"),  # supplier not in master data
        ("supplier_code", ""),         # missing supplier
        ("order_date", "31/02/2026"),  # impossible date
        ("order_date", ""),            # missing order date
    ]
    for idx, (field, value) in zip(rng.sample(range(len(rows)), len(broken)), broken):
        rows[idx][field] = value

    # received before ordered (logical inconsistency)
    victim = rows[rng.randrange(len(rows))]
    victim["received_date"] = "2025-12-01"

    # exact duplicates (typical of re-exported reports)
    for idx in rng.sample(range(len(rows)), 8):
        rows.append(dict(rows[idx]))
    rng.shuffle(rows)

    with open(RAW_DIR / "purchase_orders.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    suppliers_json = []
    for code, name, country, currency, terms, active, _ in SUPPLIERS:
        suppliers_json.append(
            {
                "supplier_code": code,
                "name": name,
                "country": country,
                "default_currency": currency,
                "payment_terms_days": terms,
                "active": active,
                "contact": {"email": f"orders@{name.split()[0].lower()}.example"},
            }
        )
    # A duplicated master record with different casing/whitespace.
    dup = dict(suppliers_json[2])
    dup["supplier_code"] = " sup-003"
    dup["name"] = "MEDPACK DE MEXICO "
    suppliers_json.append(dup)

    with open(RAW_DIR / "suppliers.json", "w", encoding="utf-8") as fh:
        json.dump({"exported_at": "2026-09-30T23:00:00Z", "suppliers": suppliers_json}, fh, indent=2)

    print(f"purchase_orders.csv: {len(rows)} rows | suppliers.json: {len(suppliers_json)} records")


if __name__ == "__main__":
    main()
