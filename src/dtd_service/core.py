"""Core DTD calculation, validation and incremental merge logic.

The implementation intentionally uses only the Python standard library.  That
keeps the daily job reproducible in a small production container while making
the numerical steps visible to a reviewer.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

INPUT_FIELDS = [
    "Comp_no",
    "Date",
    "CUR_MKT_CAP(HKD)",
    "BS_CUR_LIAB(HKD)",
    "BS_LT_BORROW(HKD)",
    "BS_TOT_LIAB2(HKD)",
    "BS_TOT_ASSET(HKD)",
    "Risk_Free_Rate",
]
PARAM_FIELDS = ["COMPANY_ID", "REAL_DTD_DATE", "DTD_SIGMA", "DTD_WEIGHT"]


class CalculationError(ValueError):
    """Raised when an input cannot produce a defensible DTD result."""


def normal_cdf(x: float) -> float:
    """Standard normal CDF without a third-party dependency."""

    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _equity_value(asset_value: float, threshold: float, rate: float, sigma: float, tau: float) -> float:
    d_plus = (math.log(asset_value / threshold) + (rate + 0.5 * sigma * sigma) * tau) / (sigma * math.sqrt(tau))
    d_minus = d_plus - sigma * math.sqrt(tau)
    return asset_value * normal_cdf(d_plus) - threshold * math.exp(-rate * tau) * normal_cdf(d_minus)


def solve_asset_value(
    equity_value: float,
    threshold: float,
    rate: float,
    sigma: float,
    tau: float = 1.0,
    tolerance: float = 1e-11,
    max_iterations: int = 200,
) -> Tuple[float, float, int, float]:
    """Solve the Merton equity equation by bracketed bisection.

    Returns ``(implied_asset_value, dtd, iterations, equation_residual)``.
    Bisection is slower than a Newton solver but remains stable for the daily
    batch and has an explicit convergence check.
    """

    values = {
        "equity_value": equity_value,
        "threshold": threshold,
        "sigma": sigma,
        "tau": tau,
    }
    if any(not math.isfinite(value) or value <= 0.0 for value in values.values()):
        raise CalculationError("equity, threshold, sigma and tau must be finite and positive")
    if not math.isfinite(rate):
        raise CalculationError("risk-free rate must be finite")

    low = max(1e-12, equity_value * 1e-12)
    high = max(equity_value + threshold, threshold, 1.0)
    while _equity_value(high, threshold, rate, sigma, tau) < equity_value:
        high *= 2.0
        if high > 1e18:
            raise CalculationError("could not bracket implied asset value")

    iterations = 0
    for iterations in range(1, max_iterations + 1):
        mid = 0.5 * (low + high)
        model_equity = _equity_value(mid, threshold, rate, sigma, tau)
        if model_equity < equity_value:
            low = mid
        else:
            high = mid
        if high - low <= tolerance * max(1.0, mid):
            break

    asset_value = 0.5 * (low + high)
    residual = _equity_value(asset_value, threshold, rate, sigma, tau) - equity_value
    # The bisection interval is scaled to asset value; a one-cent-level
    # residual in HKD millions is already much tighter than the input data.
    if abs(residual) > max(1e-5, tolerance * max(1.0, equity_value)):
        raise CalculationError(f"solver did not converge: residual={residual:.6g}")
    dtd = math.log(asset_value / threshold) / (sigma * math.sqrt(tau))
    return asset_value, dtd, iterations, residual


def _float(row: Mapping[str, str], field: str) -> float:
    value = row.get(field, "")
    if value is None or str(value).strip() == "":
        raise CalculationError(f"missing value for {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CalculationError(f"invalid numeric value for {field}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise CalculationError(f"non-finite numeric value for {field}")
    return parsed


def _date(row: Mapping[str, str], field: str) -> str:
    value = str(row.get(field, "")).strip()
    if len(value) != 8 or not value.isdigit():
        raise CalculationError(f"{field} must use YYYYMMDD: {value!r}")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise CalculationError(f"{field} is not a calendar date: {value!r}") from exc
    return value


def validate_input_rows(rows: Sequence[Mapping[str, str]]) -> List[str]:
    """Return non-fatal quality flags for a set of daily input rows."""

    flags: List[str] = []
    dates = [str(row.get("Date", "")) for row in rows]
    if len(dates) != len(set(dates)):
        flags.append("duplicate_input_dates")
    for row in rows:
        try:
            cl = _float(row, "BS_CUR_LIAB(HKD)")
            ltb = _float(row, "BS_LT_BORROW(HKD)")
            tl = _float(row, "BS_TOT_LIAB2(HKD)")
            _float(row, "CUR_MKT_CAP(HKD)")
            _float(row, "Risk_Free_Rate")
            _date(row, "Date")
            if tl < cl + ltb:
                flags.append("negative_other_liabilities")
        except CalculationError:
            flags.append("invalid_input_row")
    return sorted(set(flags))


def _parameter_selector(
    date: str, company_id: str, parameters: Sequence[Mapping[str, str]]
) -> Tuple[Optional[Mapping[str, str]], str]:
    company_rows = [
        row for row in parameters
        if str(row.get("COMPANY_ID", "")) == company_id
    ]
    if not company_rows:
        return None, "missing"
    for row in company_rows:
        if str(row.get("REAL_DTD_DATE", "")) == date:
            return row, "matched"
    latest = max(company_rows, key=lambda row: str(row["REAL_DTD_DATE"]))
    if date > str(latest["REAL_DTD_DATE"]):
        return latest, "carried_forward"
    return None, "missing"


def compute_dtd_row(
    input_row: Mapping[str, str],
    parameters: Sequence[Mapping[str, str]],
    cri_row: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    """Validate and calculate one daily record, retaining an audit trail."""

    output: Dict[str, object] = {field: input_row.get(field, "") for field in INPUT_FIELDS}
    output.update({"status": "failed", "flags": "", "error": ""})
    try:
        date = _date(input_row, "Date")
        company_id = str(input_row.get("Comp_no", "")).strip()
        if not company_id:
            raise CalculationError("missing company identifier")
        parameter, parameter_source = _parameter_selector(date, company_id, parameters)
        if parameter is None:
            raise CalculationError("no parameter record for company on or before calculation date")
        sigma = _float(parameter, "DTD_SIGMA")
        weight = _float(parameter, "DTD_WEIGHT")
        market_cap = _float(input_row, "CUR_MKT_CAP(HKD)")
        current_liabilities = _float(input_row, "BS_CUR_LIAB(HKD)")
        long_term_borrowings = _float(input_row, "BS_LT_BORROW(HKD)")
        total_liabilities = _float(input_row, "BS_TOT_LIAB2(HKD)")
        rate_pct = _float(input_row, "Risk_Free_Rate")
        other_liabilities = total_liabilities - current_liabilities - long_term_borrowings
        threshold = current_liabilities + 0.5 * long_term_borrowings + weight * other_liabilities
        flags = []
        if parameter_source == "carried_forward":
            flags.append("parameter_carried_forward")
        if other_liabilities < 0:
            flags.append("negative_other_liabilities")
        asset_value, dtd, iterations, residual = solve_asset_value(
            market_cap, threshold, rate_pct / 100.0, sigma
        )
        output.update(
            {
                "COMPANY_ID": company_id,
                "parameter_date": parameter["REAL_DTD_DATE"],
                "parameter_source": parameter_source,
                "DTD_SIGMA": sigma,
                "DTD_WEIGHT": weight,
                "other_liabilities": other_liabilities,
                "default_threshold": threshold,
                "implied_asset_value": asset_value,
                "dtd": dtd,
                "equation_residual": residual,
                "solver_iterations": iterations,
                "status": "ok",
                "flags": ";".join(flags),
            }
        )
        if cri_row is not None and str(cri_row.get("DTD_VALUE", "")).strip() != "":
            if str(cri_row.get("COMPANY_ID", "")) != company_id:
                raise CalculationError("CRI comparison row belongs to another company")
            cri_dtd = _float(cri_row, "DTD_VALUE")
            output["cri_dtd"] = cri_dtd
            output["dtd_difference"] = dtd - cri_dtd
        else:
            output["cri_dtd"] = ""
            output["dtd_difference"] = ""
    except CalculationError as exc:
        output["status"] = "failed"
        output["error"] = str(exc)
    return output


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def merge_daily_inputs(
    existing_rows: Sequence[Mapping[str, str]], incoming_rows: Sequence[Mapping[str, str]]
) -> List[Dict[str, str]]:
    """Merge new daily observations while protecting existing dates.

    A duplicate date is accepted only when every supplied value is identical;
    a changed historical observation raises an error instead of overwriting it.
    """

    merged: Dict[str, Dict[str, str]] = {}
    for row in existing_rows:
        date = _date(row, "Date")
        if date in merged:
            raise ValueError(f"duplicate existing input date {date}")
        merged[date] = dict(row)
    incoming_seen = set()
    for row in incoming_rows:
        date = _date(row, "Date")
        if date in incoming_seen:
            raise ValueError(f"duplicate incoming input date {date}")
        incoming_seen.add(date)
        if date in merged:
            if any(str(merged[date].get(field, "")) != str(row.get(field, "")) for field in INPUT_FIELDS):
                raise ValueError(f"incoming row would alter existing historical date {date}")
            continue
        merged[date] = dict(row)
    return [merged[date] for date in sorted(merged)]


def run_batch(
    input_path: Path,
    parameter_path: Path,
    output_path: Path,
    cri_path: Optional[Path] = None,
) -> Dict[str, object]:
    """Run the same calculation for historical and incremental batches."""

    inputs = _read_csv(input_path)
    parameters = _read_csv(parameter_path)
    cri_rows = _read_csv(cri_path) if cri_path else []
    for name, rows, fields in (
        ("input", inputs, ("Date",)),
        ("parameter", parameters, ("COMPANY_ID", "REAL_DTD_DATE")),
        ("CRI", cri_rows, ("COMPANY_ID", "REAL_DTD_DATE")),
    ):
        seen = set()
        for row in rows:
            for field in fields:
                if "DATE" in field.upper() or field == "Date":
                    _date(row, field)
            key = tuple(row.get(field, "") for field in fields)
            if key in seen:
                raise CalculationError(f"duplicate {name} record: {key}")
            seen.add(key)
    cri_by_date = {str(row.get("REAL_DTD_DATE")): row for row in cri_rows}
    outputs = [compute_dtd_row(row, parameters, cri_by_date.get(str(row.get("Date")))) for row in inputs]
    fields = INPUT_FIELDS + [
        "COMPANY_ID",
        "parameter_date",
        "parameter_source",
        "DTD_SIGMA",
        "DTD_WEIGHT",
        "other_liabilities",
        "default_threshold",
        "implied_asset_value",
        "dtd",
        "equation_residual",
        "solver_iterations",
        "status",
        "flags",
        "error",
        "cri_dtd",
        "dtd_difference",
    ]
    _write_csv(output_path, outputs, fields)
    ok = [row for row in outputs if row.get("status") == "ok"]
    differences: List[float] = []
    for row in ok:
        difference = row.get("dtd_difference")
        if isinstance(difference, float):
            differences.append(difference)
    summary: Dict[str, object] = {
        "input_rows": len(inputs),
        "successful_rows": len(ok),
        "failed_rows": len(outputs) - len(ok),
        "input_date_start": min((str(row.get("Date")) for row in inputs), default=None),
        "input_date_end": max((str(row.get("Date")) for row in inputs), default=None),
        "comparable_rows": len(differences),
        "cri_missing_dtd_rows": len(ok) - len(differences),
        "mean_signed_difference": sum(differences) / len(differences) if differences else None,
        "mean_absolute_difference": sum(abs(d) for d in differences) / len(differences) if differences else None,
        "max_absolute_difference": max((abs(d) for d in differences), default=None),
        "output_path": str(output_path),
    }
    return summary


def write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
