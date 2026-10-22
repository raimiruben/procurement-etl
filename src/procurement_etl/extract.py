"""Extract step: read the three sources (CSV file, JSON file, REST API)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)


def read_purchase_orders(path: Path) -> pd.DataFrame:
    """Read the ERP purchase-order export as text.

    Everything is read as a string on purpose: type conversion happens in the
    transform step, where bad values can be caught and reported instead of
    silently becoming NaN.
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    log.info("Extracted %d purchase-order lines from %s", len(df), path.name)
    return df


def read_suppliers(path: Path) -> list[dict]:
    """Read the supplier master-data export (nested JSON)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    suppliers = payload["suppliers"]
    log.info("Extracted %d supplier records from %s", len(suppliers), path.name)
    return suppliers


def fetch_fx_rates(
    *,
    currencies: list[str],
    base: str,
    on_date: str | None,
    api_url: str,
    timeout: float,
    fallback_path: Path,
    offline: bool = False,
) -> tuple[dict[str, float], str, str]:
    """Get exchange rates (units of each currency per 1 unit of `base`).

    Calls the Frankfurter REST API (European Central Bank reference rates).
    If the API is unreachable or offline mode is on, falls back to a local
    JSON file and says so, so the run log always records where rates came from.

    Returns (rates, source, rate_date).
    """
    wanted = sorted(c for c in currencies if c != base)
    if not offline:
        endpoint = f"{api_url.rstrip('/')}/{on_date or 'latest'}"
        try:
            resp = requests.get(
                endpoint,
                params={"base": base, "symbols": ",".join(wanted)},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            rates = {k: float(v) for k, v in data["rates"].items()}
            missing = set(wanted) - set(rates)
            if missing:
                raise ValueError(f"API response missing currencies: {sorted(missing)}")
            rates[base] = 1.0
            log.info("FX rates from API for %s: %s", data.get("date"), rates)
            return rates, "frankfurter_api", data.get("date", on_date or "")
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("FX API unavailable (%s). Using local fallback file.", exc)

    data = json.loads(fallback_path.read_text(encoding="utf-8"))
    if data.get("base") != base:
        raise ValueError(f"Fallback file base is {data.get('base')}, expected {base}")
    rates = {k: float(v) for k, v in data["rates"].items()}
    rates[base] = 1.0
    missing = set(wanted) - set(rates)
    if missing:
        raise ValueError(f"Fallback file missing currencies: {sorted(missing)}")
    log.info("FX rates from fallback file (%s): %s", data.get("date"), rates)
    return rates, "fallback_file", data.get("date", "")
