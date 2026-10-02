#!/usr/bin/env python3
"""Inspect the dated HKMA 364-day EFB history without publishing data."""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = ("https://api.hkma.gov.hk/public/market-data-and-statistics/"
           "monthly-statistical-bulletin/efbn/efbn-yield-daily")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def parse_date(value: object) -> date | None:
    """Parse the date formats used by the HKMA endpoint."""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        text = text[:10]
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def request_json(params: dict[str, int], attempts: int, timeout: float) -> dict[str, Any]:
    """Request one page and retry transient HTTP/network failures."""
    last_error = "unknown error"
    for attempt in range(1, attempts + 1):
        url = f"{API_URL}?{urlencode(params)}"
        request = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "greentown-dtd-service-historical-check/1.0",
        })
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                payload = json.loads(response.read().decode("utf-8"))
            print(f"HTTP status={status} attempt={attempt} offset={params['offset']}")
            if not isinstance(payload, dict):
                raise RuntimeError("API response is not a JSON object")
            return payload
        except HTTPError as error:
            last_error = f"HTTP {error.code}"
            try:
                body = error.read(500).decode("utf-8", errors="replace").replace("\n", " ")
                if body:
                    last_error += f": {body}"
            except OSError:
                pass
            print(f"{last_error} attempt={attempt} offset={params['offset']}")
            if error.code not in RETRYABLE_STATUS:
                raise RuntimeError(last_error) from error
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            last_error = str(error)
            print(f"request error attempt={attempt} offset={params['offset']}: {last_error}")
        if attempt < attempts:
            delay = min(2 ** (attempt - 1), 16)
            print(f"retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"HKMA request failed after {attempts} attempts: {last_error}")


def fetch_records(page_size: int, max_pages: int, attempts: int, timeout: float) -> list[dict[str, Any]]:
    """Read all available pages and fail if the endpoint repeats a page."""
    records: list[dict[str, Any]] = []
    signatures: set[str] = set()
    for page_number in range(max_pages):
        offset = page_number * page_size
        payload = request_json({"pagesize": page_size, "offset": offset}, attempts, timeout)
        header = payload.get("header")
        if isinstance(header, dict) and header.get("success") is False:
            raise RuntimeError(f"HKMA API returned an error: {header}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("HKMA API response has no result object")
        page = result.get("records")
        if not isinstance(page, list):
            raise RuntimeError("HKMA API result.records is not a list")
        if not page:
            print("empty page; pagination complete")
            break
        typed_page = [row for row in page if isinstance(row, dict)]
        signature = json.dumps(typed_page, sort_keys=True, default=str)
        if signature in signatures:
            raise RuntimeError("HKMA API repeated a page; offset may be ignored")
        signatures.add(signature)
        records.extend(typed_page)
        dates = [d for d in (parse_date(row.get("end_of_day")) for row in typed_page) if d]
        if dates:
            print(f"page={page_number + 1} records={len(typed_page)} date_range={min(dates).isoformat()}..{max(dates).isoformat()}")
        else:
            print(f"page={page_number + 1} records={len(typed_page)} no parseable dates")
        if len(page) < page_size:
            print("page shorter than pagesize; pagination complete")
            break
    else:
        raise RuntimeError(f"pagination exceeded max_pages={max_pages}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect HKMA published historical 364-day EFB yields.")
    parser.add_argument("--target-date", default="2026-09-15", help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    target = parse_date(args.target_date)
    if target is None:
        parser.error("--target-date must be YYYY-MM-DD or YYYYMMDD")
    if min(args.page_size, args.max_pages, args.attempts) <= 0 or args.timeout <= 0:
        parser.error("page size, max pages, attempts and timeout must be positive")
    print(f"target_date={target.isoformat()}")
    records = fetch_records(args.page_size, args.max_pages, args.attempts, args.timeout)
    dated = [(d, row) for row in records if (d := parse_date(row.get("end_of_day"))) is not None]
    if not dated:
        print("API returned no parseable end_of_day observations")
        return 1
    earliest = min(d for d, _ in dated)
    latest = max(d for d, _ in dated)
    print(f"records_read={len(records)}")
    print(f"api_earliest_observation={earliest.isoformat()}")
    print(f"api_latest_observation={latest.isoformat()}")
    matches = [row for day, row in dated if day == target]
    if matches:
        print(f"matched_records={len(matches)}")
        for row in matches:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    else:
        print(f"no exact record for target_date={target.isoformat()}")
        if target > latest:
            print("target is later than the latest published API observation")
        elif target < earliest:
            print("target is earlier than the earliest published API observation")
        else:
            print("target is within the API range but has no row; it may be a non-trading day or missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
