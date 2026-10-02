"""Small, repeatable adapters for public daily data sources."""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
import csv
import io
import re
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping

HKMA_INDICATIVE_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "daily-monetary-statistics/efbn-indicative-price"
)
HKMA_HISTORICAL_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "monthly-statistical-bulletin/efbn/efbn-yield-daily"
)
HKMA_WORKBOOK_URL = (
    "https://www.hkma.gov.hk/media/eng/doc/market-data-and-statistics/"
    "monthly-statistical-bulletin/T050303.xls"
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/"
EASTMONEY_DAILY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


class DataSourceError(RuntimeError):
    """Raised when an external data source cannot be read or parsed."""


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise DataSourceError(f"{description} must be an object")
    return value


def _number(value: object, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DataSourceError(f"{description} must be a finite number")
    try:
        number = float(value)
    except ValueError as exc:
        raise DataSourceError(f"{description} must be a finite number") from exc
    if not math.isfinite(number):
        raise DataSourceError(f"{description} must be a finite number")
    return number


def _get_json(url: str, timeout: int = 20) -> Mapping[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "greentown-dtd-service/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - convert network/parser details to a stable job error
        raise DataSourceError(f"could not read {url}: {exc}") from exc
    return _mapping(payload, f"response from {url}")


def fetch_hkma_12m_rate(timeout: int = 20) -> Dict[str, object]:
    """Fetch the latest HKMA 12-month EFB indicative yield.

    HKMA's open API returns the latest business day only.  The caller should
    store the returned observation and never back-fill earlier dates with it.
    """

    query = urllib.parse.urlencode({"segment": "IndicativePrice", "offset": 0})
    payload = _get_json(f"{HKMA_INDICATIVE_URL}?{query}", timeout=timeout)
    result = _mapping(payload.get("result", payload), "HKMA result")
    data = result.get("data", result.get("datas", []))
    if not isinstance(data, list):
        raise DataSourceError("HKMA response did not contain a data list")
    candidates = []
    for row in data:
        if not isinstance(row, Mapping):
            continue
        term = str(row.get("term", "")).upper().replace(" ", "")
        if term in {"12M", "12MO", "12MONTH", "12MONTHS", "12YR", "1Y", "1YR"}:
            candidates.append(row)
    if not candidates:
        raise DataSourceError("HKMA response contained no 12-month indicative yield")
    row = candidates[0]
    return {
        "Date": str(row.get("end_of_date", "")).replace("-", ""),
        "Risk_Free_Rate": _number(row.get("yield"), "HKMA yield"),
        "source": HKMA_INDICATIVE_URL + "?segment=IndicativePrice",
        "term": row.get("term"),
    }


def fetch_hkma_historical_rates(start: date, end: date, timeout: int = 20) -> List[Dict[str, object]]:
    """Read HKMA's dated 364-day EFB yields, in percent per annum."""

    if end < start:
        raise DataSourceError("rate end date precedes start date")
    observations: List[Dict[str, object]] = []
    seen = set()
    page_size = 1000
    max_pages = 100
    page_signatures = set()
    for page_number in range(max_pages):
        offset = page_number * page_size
        query = urllib.parse.urlencode({
            "from": start.isoformat(), "to": end.isoformat(),
            "pagesize": page_size, "offset": offset,
        })
        url = f"{HKMA_HISTORICAL_URL}?{query}"
        payload = _get_json(url, timeout=timeout)
        header = payload.get("header")
        if header is not None and _mapping(header, "HKMA header").get("success") is not True:
            raise DataSourceError(f"HKMA historical yield request failed: {header}")
        result = _mapping(payload.get("result"), "HKMA historical result")
        records = result.get("records")
        if not isinstance(records, list):
            raise DataSourceError("HKMA historical result has no records list")
        # Fail fast if a gateway ignores ``offset`` and repeats the same page.
        signature = tuple(json.dumps(record, sort_keys=True, default=str) for record in records)
        if signature in page_signatures:
            raise DataSourceError("HKMA historical yield pagination repeated a page")
        page_signatures.add(signature)
        for record in records:
            row = _mapping(record, "HKMA historical record")
            raw_date = str(row.get("end_of_day", ""))
            try:
                day = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise DataSourceError(f"invalid HKMA historical date: {raw_date!r}") from exc
            if not start <= day <= end:
                continue
            key = day.strftime("%Y%m%d")
            if key in seen:
                raise DataSourceError(f"duplicate HKMA historical yield date {key}")
            seen.add(key)
            if row.get("efb_364d") in (None, "", "N/A"):
                continue
            observations.append({
                "Date": key,
                "Risk_Free_Rate": _number(row["efb_364d"], f"HKMA 364-day yield on {key}"),
                "source": url,
            })
        if len(records) < page_size:
            return sorted(observations, key=lambda row: str(row["Date"]))
    raise DataSourceError(f"HKMA historical yield pagination exceeded {max_pages} pages")


def parse_hkma_workbook_rates(content: bytes, start: date, end: date) -> List[Dict[str, object]]:
    """Extract the 364-day EFB yield from official monthly bulletin table T050303."""

    try:
        import xlrd
        from xlrd.biffh import XLRDError
    except ImportError as exc:
        raise DataSourceError("HKMA Excel fallback requires xlrd; run python3 -m pip install -e .") from exc
    try:
        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_name("T5.3.3")
        if sheet.cell_value(10, 0) != "Date" or sheet.cell_value(10, 7) != "364-day":
            raise DataSourceError("HKMA T050303 date or 364-day column changed")
        observations: List[Dict[str, object]] = []
        seen = set()
        for index in range(12, sheet.nrows):
            cell = sheet.cell(index, 0)
            if cell.ctype not in (xlrd.XL_CELL_DATE, xlrd.XL_CELL_NUMBER):
                continue
            serial = cell.value
            if isinstance(serial, bool) or not isinstance(serial, (int, float)):
                continue
            if not 30000 < serial < 60000:
                continue
            day = xlrd.xldate_as_datetime(float(serial), book.datemode).date()
            if not start <= day <= end:
                continue
            key = day.strftime("%Y%m%d")
            if key in seen:
                raise DataSourceError(f"duplicate HKMA T050303 yield date {key}")
            seen.add(key)
            value = sheet.cell_value(index, 7)
            if value in (None, "", "N/A"):
                continue
            observations.append({
                "Date": key,
                "Risk_Free_Rate": _number(value, f"HKMA T050303 364-day yield on {key}"),
                "source": HKMA_WORKBOOK_URL,
            })
        return sorted(observations, key=lambda row: str(row["Date"]))
    except (XLRDError, IndexError, TypeError, ValueError) as exc:
        raise DataSourceError(f"could not parse HKMA T050303 workbook: {exc}") from exc


def read_hkma_workbook_rates(path: Path, start: date, end: date) -> List[Dict[str, object]]:
    try:
        return parse_hkma_workbook_rates(path.read_bytes(), start, end)
    except OSError as exc:
        raise DataSourceError(f"could not read HKMA T050303 workbook {path}: {exc}") from exc


def fetch_hkma_workbook_rates(start: date, end: date, timeout: int = 30) -> List[Dict[str, object]]:
    request = urllib.request.Request(HKMA_WORKBOOK_URL, headers={"User-Agent": "greentown-dtd-service/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except Exception as exc:  # noqa: BLE001 - normalize network failures
        raise DataSourceError(f"could not read {HKMA_WORKBOOK_URL}: {exc}") from exc
    return parse_hkma_workbook_rates(content, start, end)


def _fetch_yahoo_daily_quote(symbol: str = "3900.HK", timeout: int = 20) -> Dict[str, object]:
    """Fetch the latest completed daily close for a listed security."""

    query = urllib.parse.urlencode({"range": "1mo", "interval": "1d", "events": "history"})
    payload = _get_json(f"{YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol))}?{query}", timeout=timeout)
    chart_response = _mapping(payload.get("chart"), "Yahoo chart")
    results = chart_response.get("result")
    if not isinstance(results, list) or not results:
        raise DataSourceError("Yahoo Finance returned no chart result")
    chart = _mapping(results[0], "Yahoo chart result")
    timestamps = chart.get("timestamp")
    indicators = _mapping(chart.get("indicators"), "Yahoo indicators")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or not quotes:
        raise DataSourceError("Yahoo Finance returned no quote data")
    quote = _mapping(quotes[0], "Yahoo quote")
    closes = quote.get("close")
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise DataSourceError("Yahoo Finance returned invalid daily observations")
    for timestamp, close in reversed(list(zip(timestamps, closes))):
        if close is not None:
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise DataSourceError("Yahoo timestamp must be numeric")
            date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y%m%d")
            return {
                "Date": date,
                "close": _number(close, "Yahoo close"),
                "source": YAHOO_CHART_URL.format(symbol=symbol),
            }
    raise DataSourceError("Yahoo Finance returned no completed close")


def _fetch_yahoo_daily_quotes(
    start: date, end: date, symbol: str = "3900.HK", timeout: int = 20
) -> List[Dict[str, object]]:
    """Return unadjusted daily closes in an inclusive date interval."""

    if end < start:
        raise DataSourceError("quote end date precedes start date")
    query = urllib.parse.urlencode({
        "period1": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
        "interval": "1d",
        "events": "history",
    })
    url = f"{YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol))}?{query}"
    payload = _get_json(url, timeout=timeout)
    chart_response = _mapping(payload.get("chart"), "Yahoo chart")
    results = chart_response.get("result")
    if not isinstance(results, list) or not results:
        raise DataSourceError("Yahoo Finance returned no chart result")
    chart = _mapping(results[0], "Yahoo chart result")
    timestamps = chart.get("timestamp")
    indicators = _mapping(chart.get("indicators"), "Yahoo indicators")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or not quotes:
        raise DataSourceError("Yahoo Finance returned no quote data")
    closes = _mapping(quotes[0], "Yahoo quote").get("close")
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise DataSourceError("Yahoo Finance returned invalid daily observations")
    observations: List[Dict[str, object]] = []
    seen = set()
    for timestamp, close in zip(timestamps, closes):
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise DataSourceError("Yahoo timestamp must be numeric")
        day = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        if day < start or day > end:
            continue
        key = day.strftime("%Y%m%d")
        if key in seen:
            raise DataSourceError(f"Yahoo returned duplicate date {key}")
        seen.add(key)
        if close is None:
            raise DataSourceError(f"Yahoo close is missing on {key}")
        price = _number(close, f"Yahoo close on {key}")
        if price <= 0:
            raise DataSourceError(f"Yahoo close must be positive on {key}")
        observations.append({"Date": key, "close": price, "source": url})
    if not observations:
        raise DataSourceError("Yahoo Finance returned no trading observations in the requested interval")
    return sorted(observations, key=lambda row: str(row["Date"]))


def fetch_stooq_daily_quotes(
    start: date, end: date, symbol: str = "3900.HK", timeout: int = 20
) -> List[Dict[str, object]]:
    """Fetch daily closes from Stooq's public CSV endpoint.

    Stooq uses lowercase exchange suffixes (``3900.hk``). The endpoint is a
    useful price-feed fallback, but it does not provide HKMA rates or shares.
    """

    if end < start:
        raise DataSourceError("quote end date precedes start date")
    stooq_symbol = symbol.lower().replace(".", ".")
    query = urllib.parse.urlencode({
        "s": stooq_symbol,
        "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"),
        "i": "d",
    })
    url = f"{STOOQ_DAILY_URL}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "greentown-dtd-service/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - normalize network errors
        raise DataSourceError(f"could not read {url}: {exc}") from exc
    if text.lstrip().startswith("No data"):
        raise DataSourceError(f"Stooq returned no data for {symbol}")
    rows = list(csv.DictReader(io.StringIO(text)))
    observations: List[Dict[str, object]] = []
    seen = set()
    for row in rows:
        day = row.get("Date", "")
        try:
            parsed_day = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DataSourceError(f"Stooq returned invalid date: {day!r}") from exc
        if parsed_day < start or parsed_day > end:
            continue
        key = parsed_day.strftime("%Y%m%d")
        if key in seen:
            raise DataSourceError(f"Stooq returned duplicate date {key}")
        seen.add(key)
        close = row.get("Close", "")
        if not close:
            raise DataSourceError(f"Stooq close is missing on {key}")
        price = _number(close, f"Stooq close on {key}")
        if price <= 0:
            raise DataSourceError(f"Stooq close must be positive on {key}")
        observations.append({"Date": key, "close": price, "source": url})
    if not observations:
        raise DataSourceError("Stooq returned no trading observations in the requested interval")
    return sorted(observations, key=lambda row: str(row["Date"]))


def fetch_eastmoney_daily_quotes(
    start: date, end: date, symbol: str = "3900.HK", timeout: int = 20
) -> List[Dict[str, object]]:
    """Fetch unadjusted HK-share daily closes from Eastmoney's daily K-line API."""

    if end < start:
        raise DataSourceError("quote end date precedes start date")
    match = re.fullmatch(r"(\d{1,5})\.HK", symbol.upper())
    if match is None:
        raise DataSourceError(f"Eastmoney adapter requires a Hong Kong stock code: {symbol}")
    code = match.group(1).zfill(5)
    query = urllib.parse.urlencode({
        "secid": f"116.{code}", "klt": 101, "fqt": 0,
        "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    })
    url = f"{EASTMONEY_DAILY_URL}?{query}"
    payload = _get_json(url, timeout=timeout)
    if payload.get("rc") != 0:
        raise DataSourceError(f"Eastmoney daily request failed: {payload.get('rc')}")
    data = _mapping(payload.get("data"), "Eastmoney daily data")
    if data.get("code") != code or data.get("market") != 116:
        raise DataSourceError("Eastmoney returned a different security or market")
    klines = data.get("klines")
    if not isinstance(klines, list):
        raise DataSourceError("Eastmoney returned no daily K-line list")
    observations: List[Dict[str, object]] = []
    seen = set()
    for record in klines:
        if not isinstance(record, str):
            raise DataSourceError("Eastmoney daily K-line must be a CSV string")
        fields = next(csv.reader([record]))
        if len(fields) < 3:
            raise DataSourceError("Eastmoney daily K-line is incomplete")
        try:
            day = datetime.strptime(fields[0], "%Y-%m-%d").date()
        except ValueError as exc:
            raise DataSourceError(f"Eastmoney returned invalid date: {fields[0]!r}") from exc
        if not start <= day <= end:
            continue
        key = day.strftime("%Y%m%d")
        if key in seen:
            raise DataSourceError(f"Eastmoney returned duplicate date {key}")
        seen.add(key)
        close = _number(fields[2], f"Eastmoney close on {key}")
        if close <= 0:
            raise DataSourceError(f"Eastmoney close must be positive on {key}")
        observations.append({"Date": key, "close": close, "source": url})
    if not observations:
        raise DataSourceError("Eastmoney returned no trading observations in the requested interval")
    return sorted(observations, key=lambda row: str(row["Date"]))


def fetch_yahoo_daily_quote(symbol: str = "3900.HK", timeout: int = 20) -> Dict[str, object]:
    """Fetch the latest quote, trying alternate public feeds on network errors."""

    try:
        return _fetch_yahoo_daily_quote(symbol=symbol, timeout=timeout)
    except DataSourceError as exc:
        if not str(exc).startswith("could not read "):
            raise
        end = datetime.now(timezone.utc).date()
        try:
            return fetch_eastmoney_daily_quotes(
                end - timedelta(days=31), end, symbol=symbol, timeout=timeout
            )[-1]
        except DataSourceError as eastmoney_error:
            try:
                return fetch_stooq_daily_quotes(
                    end - timedelta(days=31), end, symbol=symbol, timeout=timeout
                )[-1]
            except DataSourceError as stooq_error:
                raise DataSourceError(
                    f"all market feeds failed: Yahoo: {exc}; Eastmoney: {eastmoney_error}; "
                    f"Stooq: {stooq_error}"
                ) from stooq_error


def fetch_yahoo_daily_quotes(
    start: date, end: date, symbol: str = "3900.HK", timeout: int = 20
) -> List[Dict[str, object]]:
    """Fetch Yahoo prices, trying Eastmoney and Stooq on network errors."""

    try:
        return _fetch_yahoo_daily_quotes(start, end, symbol=symbol, timeout=timeout)
    except DataSourceError as exc:
        if not str(exc).startswith("could not read "):
            raise
        try:
            return fetch_eastmoney_daily_quotes(start, end, symbol=symbol, timeout=timeout)
        except DataSourceError as eastmoney_error:
            try:
                return fetch_stooq_daily_quotes(start, end, symbol=symbol, timeout=timeout)
            except DataSourceError as stooq_error:
                raise DataSourceError(
                    f"all market feeds failed: Yahoo: {exc}; Eastmoney: {eastmoney_error}; "
                    f"Stooq: {stooq_error}"
                ) from stooq_error


def build_market_cap_input(
    quote: Mapping[str, object],
    rate: Mapping[str, object],
    shares_outstanding_m: float,
    financials: Mapping[str, object],
    company_id: str = "23286",
) -> Dict[str, object]:
    """Convert a quote and HKMA rate into the project's HKD-million schema."""

    if quote["Date"] != rate["Date"]:
        raise DataSourceError(f"market and rate dates differ: {quote['Date']} vs {rate['Date']}")
    if not math.isfinite(shares_outstanding_m) or shares_outstanding_m <= 0:
        raise DataSourceError("shares_outstanding_m must be finite and positive")
    return {
        "Comp_no": company_id,
        "Date": quote["Date"],
        "CUR_MKT_CAP(HKD)": _number(quote.get("close"), "Yahoo close") * shares_outstanding_m,
        "BS_CUR_LIAB(HKD)": financials["BS_CUR_LIAB(HKD)"],
        "BS_LT_BORROW(HKD)": financials["BS_LT_BORROW(HKD)"],
        "BS_TOT_LIAB2(HKD)": financials["BS_TOT_LIAB2(HKD)"],
        "BS_TOT_ASSET(HKD)": financials["BS_TOT_ASSET(HKD)"],
        "Risk_Free_Rate": rate["Risk_Free_Rate"],
    }
