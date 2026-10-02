#!/usr/bin/env python3
"""Convert disclosed RMB-thousand statement lines into reviewed HKD-million inputs."""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FIELDS = [
    "effective_date", "BS_CUR_LIAB(HKD)", "BS_LT_BORROW(HKD)",
    "BS_TOT_LIAB2(HKD)", "BS_TOT_ASSET(HKD)", "source_url",
]


def _amount(row: dict[str, str], field: str) -> Decimal:
    try:
        number = Decimal(row[field])
    except (KeyError, InvalidOperation) as exc:
        raise ValueError(f"invalid {field} on {row.get('effective_date', '?')}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return number


def convert_row(row: dict[str, str]) -> dict[str, str]:
    """Map non-current debt and lease lines without using future statements."""

    effective = row["effective_date"]
    publication = row["publication_date"]
    statement = row["statement_date"]
    try:
        for value in (effective, publication, statement):
            if datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d") != value:
                raise ValueError
    except ValueError as exc:
        raise ValueError(f"invalid financial date on {effective}") from exc
    if not statement < publication < effective:
        raise ValueError(f"invalid financial effective date {effective}")
    for field in ("filing_url", "fx_url"):
        source = urlparse(row[field])
        if source.scheme != "https" or not source.hostname:
            raise ValueError(f"invalid {field} on {effective}")
    if urlparse(row["fx_url"]).path.rsplit("/", 1)[-1] != datetime.strptime(
        publication, "%Y%m%d"
    ).strftime("%Y-%m-%d"):
        raise ValueError(f"FX observation must match publication date on {effective}")
    current = _amount(row, "current_liabilities_rmb_thousand")
    noncurrent = _amount(row, "noncurrent_liabilities_rmb_thousand")
    borrowings = sum(
        (_amount(row, field) for field in (
            "noncurrent_bank_borrowings_rmb_thousand",
            "noncurrent_senior_notes_rmb_thousand",
            "noncurrent_corporate_bonds_rmb_thousand",
            "noncurrent_lease_liabilities_rmb_thousand",
        )), Decimal(0)
    )
    if borrowings > noncurrent:
        raise ValueError(f"non-current borrowing lines exceed non-current liabilities on {effective}")
    assets_less_current = _amount(row, "total_assets_less_current_liabilities_rmb_thousand")
    factor = _amount(row, "cny_hkd") / Decimal(1000)
    return {
        "effective_date": effective,
        "BS_CUR_LIAB(HKD)": format(current * factor, "f"),
        "BS_LT_BORROW(HKD)": format(borrowings * factor, "f"),
        "BS_TOT_LIAB2(HKD)": format((current + noncurrent) * factor, "f"),
        "BS_TOT_ASSET(HKD)": format((current + assets_less_current) * factor, "f"),
        "source_url": row["filing_url"],
    }


def main() -> None:
    incoming = ROOT / "data" / "incoming"
    with (incoming / "financial_conversion.csv").open(newline="", encoding="utf-8") as stream:
        converted = [convert_row(row) for row in csv.DictReader(stream)]
    path = incoming / "financials.csv"
    with path.open(newline="", encoding="utf-8") as stream:
        existing = list(csv.DictReader(stream))
    by_date = {row["effective_date"]: row for row in existing}
    if len(by_date) != len(existing):
        raise ValueError("duplicate financial effective date")
    for row in converted:
        previous = by_date.get(row["effective_date"])
        if previous is not None and previous != row:
            raise ValueError(f"existing financial row differs on {row['effective_date']}")
        by_date[row["effective_date"]] = row
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(by_date[day] for day in sorted(by_date))
    print(f"Reviewed financial rows: {len(by_date)}")


if __name__ == "__main__":
    main()
