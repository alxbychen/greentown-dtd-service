"""Prepare sourced daily inputs, then calculate and publish a validated batch."""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .core import INPUT_FIELDS, merge_daily_inputs, run_batch, write_json
from .sources import (
    DataSourceError, build_market_cap_input, fetch_hkma_historical_rates,
    fetch_hkma_workbook_rates, read_hkma_workbook_rates, fetch_yahoo_daily_quotes,
)

FINANCIAL_FIELDS = INPUT_FIELDS[3:7]
PROVENANCE_FIELDS = [
    "Date", "market_source", "rate_source", "shares_source", "shares_effective_date",
    "financial_source", "financial_effective_date",
]
MARKET_FIELDS = ["Date", "close", "source_url"]


def _coverage_intervals(path: Path) -> List[tuple[str, str]]:
    if not path.exists():
        return []
    try:
        coverage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataSourceError(f"invalid market coverage metadata: {path}") from exc
    entries = coverage.get("intervals", []) if isinstance(coverage, dict) else []
    if isinstance(coverage, dict) and "start" in coverage and "end" in coverage:
        entries = [coverage]
    if not isinstance(entries, list):
        raise DataSourceError("market coverage intervals must be a list")
    intervals = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DataSourceError("invalid market coverage interval")
        start, end = str(entry.get("start", "")), str(entry.get("end", ""))
        if _day(end) < _day(start):
            raise DataSourceError("market coverage interval end precedes start")
        intervals.append((start, end))
    return sorted(intervals)


def _merged_coverage(intervals: Sequence[tuple[str, str]]) -> List[Dict[str, str]]:
    merged: List[tuple[str, str]] = []
    for start, end in sorted(intervals):
        if merged and _day(start) <= _day(merged[-1][1]) + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [{"start": start, "end": end} for start, end in merged]


def _day(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise DataSourceError(f"invalid YYYYMMDD date: {value!r}") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise DataSourceError(f"invalid YYYYMMDD date: {value!r}")
    return parsed


def _positive(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise DataSourceError(f"{field} must be a positive finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DataSourceError(f"{field} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise DataSourceError(f"{field} must be a positive finite number")
    return number


def _read_csv(path: Path, fields: Sequence[str], date_field: str) -> List[Dict[str, str]]:
    if not path.exists():
        raise DataSourceError(f"required source file is missing: {path}")
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not set(fields).issubset(reader.fieldnames):
            raise DataSourceError(f"{path} must contain columns: {', '.join(fields)}")
        rows = list(reader)
    seen = set()
    for row in rows:
        key = row.get(date_field, "")
        _day(key)
        if key in seen:
            raise DataSourceError(f"duplicate {date_field} {key} in {path}")
        seen.add(key)
        if not row.get("source_url", "").strip() and "source_url" in fields:
            raise DataSourceError(f"missing source_url for {key} in {path}")
    return sorted(rows, key=lambda row: row[date_field])


def _as_of(rows: Sequence[Mapping[str, str]], day: str, name: str) -> Mapping[str, str]:
    eligible = [row for row in rows if row["effective_date"] <= day]
    if not eligible:
        raise DataSourceError(f"no approved {name} effective on or before {day}")
    return eligible[-1]


def _latest_completed_day() -> date:
    now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    return now.date() if now.hour >= 18 else now.date() - timedelta(days=1)


def add_reviewed_source(root: Path, kind: str, values: Mapping[str, object]) -> Dict[str, str]:
    """Record a dated, user-reviewed rate, share count or filing observation."""

    specifications = {
        "rates": ("Date", ["Date", "Risk_Free_Rate", "source_url"]),
        "shares": ("effective_date", ["effective_date", "shares_outstanding_m", "source_url"]),
        "financials": ("effective_date", ["effective_date", *FINANCIAL_FIELDS, "source_url"]),
    }
    if kind not in specifications:
        raise DataSourceError("kind must be rates, shares or financials")
    if values.get("reviewed") not in (True, "true", "on"):
        raise DataSourceError("confirm that the dated source and units were reviewed")
    date_field, fields = specifications[kind]
    row = {field: str(values.get(field, "")).strip() for field in fields}
    day = _day(row[date_field])
    if day > _latest_completed_day():
        raise DataSourceError("source effective date cannot be in the future")
    source = urlparse(row["source_url"])
    if source.scheme != "https" or not source.hostname:
        raise DataSourceError("source_url must be a direct HTTPS source")
    if kind == "rates":
        try:
            rate = float(row["Risk_Free_Rate"])
        except ValueError as exc:
            raise DataSourceError("Risk_Free_Rate must be a finite percentage") from exc
        if not math.isfinite(rate):
            raise DataSourceError("Risk_Free_Rate must be a finite percentage")
    elif kind == "shares":
        _positive(row["shares_outstanding_m"], "shares_outstanding_m")
    else:
        for field in FINANCIAL_FIELDS:
            _positive(row[field], field)
    path = root / "data" / "incoming" / f"{kind}.csv"
    previous = _read_csv(path, fields, date_field) if path.exists() else []
    if any(old[date_field] == row[date_field] for old in previous):
        raise DataSourceError(f"{kind} already has an observation for {row[date_field]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted([*previous, row], key=lambda item: item[date_field]))
    os.replace(staged, path)
    return row


def archive_market_quotes(root: Path, start: date, end: date) -> Dict[str, object]:
    """Fetch and preserve dated unadjusted closes for reproducible future updates."""

    path = root / "data" / "incoming" / "market_quotes.csv"
    coverage_path = path.with_suffix(".coverage.json")
    old_coverage = _coverage_intervals(coverage_path)
    previous = _read_csv(path, MARKET_FIELDS, "Date") if path.exists() else []
    by_date = {row["Date"]: row for row in previous}
    quotes = fetch_yahoo_daily_quotes(start, end)
    if not quotes:
        raise DataSourceError("market source returned no trading observations")
    quote_dates = [str(quote["Date"]) for quote in quotes]
    if quote_dates != sorted(set(quote_dates)):
        raise DataSourceError("market source returned duplicate or unordered trading dates")
    for quote in quotes:
        day = _day(str(quote["Date"]))
        if not start <= day <= end:
            raise DataSourceError(f"market source date {quote['Date']} is outside requested interval")
        close = _positive(quote.get("close"), f"market close on {quote['Date']}")
        source = str(quote.get("source", "")).strip()
        if urlparse(source).scheme != "https":
            raise DataSourceError(f"market source URL missing or invalid on {quote['Date']}")
        row = {"Date": day.strftime("%Y%m%d"), "close": str(close), "source_url": source}
        if row["Date"] in by_date and float(by_date[row["Date"]]["close"]) != close:
            raise DataSourceError(f"archived market close differs on {row['Date']}")
        by_date[row["Date"]] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=MARKET_FIELDS)
        writer.writeheader()
        writer.writerows(by_date[day] for day in sorted(by_date))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staged_coverage = Path(stream.name)
        json.dump({"intervals": _merged_coverage([
            *old_coverage, (start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        ])}, stream)
    os.replace(staged, path)
    os.replace(staged_coverage, coverage_path)
    return {"observations": len(quotes), "archive_rows": len(by_date), "first_date": min(by_date), "last_date": max(by_date)}


def update_data(root: Path, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, object]:
    """Fetch prices and exact-date HKMA rates, then join reviewed as-of inputs."""

    raw_path = root / "historical_inputs.csv"
    processed = root / "data" / "processed"
    prepared_path = processed / "prepared_inputs.csv"
    input_path = prepared_path if prepared_path.exists() else raw_path
    existing = _read_csv(input_path, INPUT_FIELDS, "Date")
    if not existing:
        raise DataSourceError("historical input file is empty")
    last_date = existing[-1]["Date"]
    next_day = _day(last_date) + timedelta(days=1)
    first = _day(start) if start else next_day
    last = _day(end) if end else _latest_completed_day()
    if first != next_day:
        raise DataSourceError(f"start must be the day after existing input cutoff: {next_day:%Y%m%d}")
    if last > _latest_completed_day():
        raise DataSourceError("end cannot exceed the latest completed Hong Kong market day")
    if last < first:
        raise DataSourceError("no new dates in requested interval")

    incoming = root / "data" / "incoming"
    rate_fields = ["Date", "Risk_Free_Rate", "source_url"]
    rate_path = incoming / "rates.csv"
    rates = _read_csv(rate_path, rate_fields, "Date") if rate_path.exists() else []
    shares = _read_csv(incoming / "shares.csv", ["effective_date", "shares_outstanding_m", "source_url"], "effective_date")
    financials = _read_csv(incoming / "financials.csv", ["effective_date", *FINANCIAL_FIELDS, "source_url"], "effective_date")
    rate_by_date = {row["Date"]: row for row in rates}
    market_path = incoming / "market_quotes.csv"
    coverage_path = market_path.with_suffix(".coverage.json")
    archived_quotes = _read_csv(market_path, MARKET_FIELDS, "Date") if market_path.exists() else []
    coverage = _coverage_intervals(coverage_path)
    selected = [row for row in archived_quotes if first <= _day(row["Date"]) <= last]
    if (selected and any(start <= first.strftime("%Y%m%d") and end >= last.strftime("%Y%m%d")
                         for start, end in coverage)):
        quotes = [{"Date": row["Date"], "close": row["close"], "source": row["source_url"]} for row in selected]
    else:
        quotes = fetch_yahoo_daily_quotes(first, last)
    if not quotes:
        raise DataSourceError("market source returned no trading observations")
    quote_dates = [str(quote["Date"]) for quote in quotes]
    if quote_dates != sorted(set(quote_dates)):
        raise DataSourceError("market source returned duplicate or unordered trading dates")
    for quote in quotes:
        day = _day(str(quote["Date"]))
        if not first <= day <= last:
            raise DataSourceError(f"market source date {quote['Date']} is outside requested interval")
        _positive(quote.get("close"), f"market close on {quote['Date']}")
        source = urlparse(str(quote.get("source", "")))
        if source.scheme != "https" or source.hostname not in {
            "query1.finance.yahoo.com", "push2his.eastmoney.com", "stooq.com"
        }:
            raise DataSourceError(f"market source URL missing or unrecognized on {quote['Date']}")
    # The supplied parameter dates are a dated trading-day cross-check through
    # their cutoff. They cannot prove coverage after the final parameter date.
    parameter_rows = _read_csv(
        root / "parameters.csv", ["COMPANY_ID", "REAL_DTD_DATE"], "REAL_DTD_DATE"
    )
    expected_dates = {
        row["REAL_DTD_DATE"] for row in parameter_rows
        if row["COMPANY_ID"] == "23286" and first <= _day(row["REAL_DTD_DATE"]) <= last
    }
    missing_quotes = sorted(expected_dates - set(quote_dates))
    if missing_quotes:
        raise DataSourceError(
            f"market source omitted {len(missing_quotes)} supplied trading dates: "
            + ", ".join(missing_quotes[:8])
        )
    missing = [day for day in quote_dates if day not in rate_by_date]
    fetched_rates: List[Dict[str, str]] = []
    historical_error: Optional[DataSourceError] = None
    if missing:
        try:
            historical = fetch_hkma_historical_rates(first, last)
        except DataSourceError as exc:
            historical_error = exc
            historical = []
        for observation in historical:
            day = str(observation["Date"])
            if day in missing:
                row = {
                    "Date": day, "Risk_Free_Rate": str(observation["Risk_Free_Rate"]),
                    "source_url": str(observation["source"]),
                }
                rate_by_date[day] = row
                fetched_rates.append(row)
        missing = [day for day in quote_dates if day not in rate_by_date]
    workbook_error: Optional[DataSourceError] = None
    if missing:
        workbook_path = root / "data" / "raw" / "hkma_T050303.xls"
        try:
            workbook = (read_hkma_workbook_rates(workbook_path, first, last)
                        if workbook_path.exists() else [])
            workbook_by_date = {str(row["Date"]): row for row in workbook}
            if any(day not in workbook_by_date for day in missing):
                workbook = fetch_hkma_workbook_rates(first, last)
        except DataSourceError as exc:
            workbook_error = exc
            workbook = []
        for observation in workbook:
            day = str(observation["Date"])
            if day in missing:
                row = {
                    "Date": day, "Risk_Free_Rate": str(observation["Risk_Free_Rate"]),
                    "source_url": str(observation["source"]),
                }
                rate_by_date[day] = row
                fetched_rates.append(row)
        missing = [day for day in quote_dates if day not in rate_by_date]
    if missing:
        preview = ", ".join(missing[:8])
        detail = f"; historical API: {historical_error}" if historical_error else ""
        detail += f"; official workbook: {workbook_error}" if workbook_error else ""
        raise DataSourceError(f"missing exact-date HKMA 12M rates for {len(missing)} trading dates: {preview}{detail}")

    new_rows: List[Dict[str, str]] = []
    audit: List[Dict[str, str]] = []
    for quote in quotes:
        day = str(quote["Date"])
        rate = rate_by_date[day]
        share = _as_of(shares, day, "share count")
        financial = _as_of(financials, day, "financial statement")
        share_count = _positive(share["shares_outstanding_m"], "shares_outstanding_m")
        numeric_financials = {field: _positive(financial[field], field) for field in FINANCIAL_FIELDS}
        try:
            numeric_rate = float(rate["Risk_Free_Rate"])
        except ValueError as exc:
            raise DataSourceError(f"invalid HKMA yield on {day}") from exc
        if not math.isfinite(numeric_rate):
            raise DataSourceError(f"invalid HKMA yield on {day}")
        prepared = build_market_cap_input(
            quote, {"Date": day, "Risk_Free_Rate": numeric_rate}, share_count, numeric_financials
        )
        new_rows.append({field: str(prepared[field]) for field in INPUT_FIELDS})
        audit.append({
            "Date": day, "market_source": str(quote["source"]),
            "rate_source": rate["source_url"], "shares_source": share["source_url"],
            "shares_effective_date": share["effective_date"],
            "financial_source": financial["source_url"],
            "financial_effective_date": financial["effective_date"],
        })
    merged = merge_daily_inputs(existing, new_rows)
    processed.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=processed) as temp_dir:
        temp = Path(temp_dir)
        staged_inputs = temp / "prepared_inputs.csv"
        staged_output = temp / "computed_dtd.csv"
        with staged_inputs.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=INPUT_FIELDS)
            writer.writeheader()
            writer.writerows(merged)
        summary = run_batch(
            staged_inputs, root / "parameters.csv", staged_output,
            root / "full_historical_dtd_output.csv",
        )
        if summary["failed_rows"]:
            raise DataSourceError(f"batch has {summary['failed_rows']} failed calculations; nothing published")
        staged_rates: Optional[Path] = None
        if fetched_rates:
            staged_rates = temp / "rates.csv"
            with staged_rates.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=rate_fields)
                writer.writeheader()
                writer.writerows(sorted([*rates, *fetched_rates], key=lambda row: row["Date"]))
        provenance_path = processed / "input_provenance.csv"
        previous_audit = _read_csv(provenance_path, PROVENANCE_FIELDS, "Date") if provenance_path.exists() else []
        staged_audit = temp / "input_provenance.csv"
        with staged_audit.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=PROVENANCE_FIELDS)
            writer.writeheader()
            writer.writerows(previous_audit + audit)
        summary["output_path"] = "data/processed/computed_dtd.csv"
        summary["new_rows"] = len(new_rows)
        staged_summary = temp / "comparison_summary.json"
        write_json(staged_summary, summary)
        if staged_rates is not None:
            os.replace(staged_rates, rate_path)
        os.replace(staged_inputs, prepared_path)
        os.replace(staged_output, processed / "computed_dtd.csv")
        os.replace(staged_audit, provenance_path)
        os.replace(staged_summary, processed / "comparison_summary.json")
    return summary
