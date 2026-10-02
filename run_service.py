#!/usr/bin/env python3
"""Run the local DTD web service."""

from pathlib import Path
import argparse
import csv
import html
import json
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from dtd_service.pipeline import add_reviewed_source, update_data  # noqa: E402
from dtd_service.sources import DataSourceError  # noqa: E402


def _load_results(path: Path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _latest_csv_date(path: Path, date_field: str = "Date"):
    """Return the latest valid YYYYMMDD in a small source archive."""

    if not path.exists():
        return None
    try:
        rows = _load_results(path)
        dates = [
            row.get(date_field, "")
            for row in rows
            if row.get(date_field, "").isdigit() and len(row.get(date_field, "")) == 8
        ]
        return max(dates) if dates else None
    except (OSError, csv.Error):
        return None


def _source_cutoffs(root: Path):
    """Expose archived source coverage separately from published output coverage."""

    incoming = root / "data" / "incoming"
    return {
        "market_source_cutoff": _latest_csv_date(incoming / "market_quotes.csv"),
        "rate_source_cutoff": _latest_csv_date(incoming / "rates.csv"),
    }


def _freshness(rows):
    if not rows:
        return "missing", None
    try:
        age = (datetime.now(ZoneInfo("Asia/Hong_Kong")).date() -
               datetime.strptime(rows[-1]["Date"], "%Y%m%d").date()).days
    except (ValueError, KeyError):
        return "invalid", None
    if age < 0:
        return "invalid", age
    return ("stale" if age > 7 else "ok"), age


def _update_window(rows):
    """Describe the append-only date window shown by the web form."""

    valid_dates = [
        row.get("Date", "")
        for row in rows
        if row.get("Date", "").isdigit() and len(row.get("Date", "")) == 8
    ]
    cutoff = max(valid_dates) if valid_dates else None
    next_start = None
    if cutoff:
        try:
            next_start = (
                datetime.strptime(cutoff, "%Y%m%d").date() + timedelta(days=1)
            ).strftime("%Y%m%d")
        except ValueError:
            next_start = None
    now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    latest_completed_day = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
    return {
        "existing_input_cutoff": cutoff,
        "next_allowed_start": next_start,
        # This is a calendar guard, not a promise that every source has published
        # a trading observation through this date.
        "latest_allowed_end": latest_completed_day.strftime("%Y%m%d"),
        "latest_calendar_request_end": latest_completed_day.strftime("%Y%m%d"),
    }


def _update_action(error: str):
    lowered = error.lower()
    if "day after existing input cutoff" in lowered:
        return "Start with the next allowed date shown above; existing dates cannot be overwritten."
    if "missing exact-date hkma" in lowered:
        return "Supply reviewed exact-date HKMA 364-day EFB yields, then retry; no partial batch was published."
    return "Review the error, correct the source or date inputs, and retry; no partial batch was published."


def make_handler(results_path: Path, root: Path = ROOT):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            rows = _load_results(results_path) if results_path.exists() else []
            ok = [r for r in rows if r.get("status") == "ok"]
            freshness, age_days = _freshness(rows)
            update_window = _update_window(rows)
            if parsed.path == "/api/health":
                self._send(200, json.dumps({
                    "status": freshness, "rows": len(rows), "successful_rows": len(ok),
                    "input_date_end": update_window["existing_input_cutoff"],
                    "data_age_calendar_days": age_days,
                    **update_window,
                    **_source_cutoffs(root),
                }), "application/json")
                return
            if parsed.path == "/api/results":
                self._send(200, json.dumps(rows[-100:], ensure_ascii=True), "application/json")
                return
            if parsed.path != "/":
                self._send(404, "Not found")
                return
            latest = ok[-1] if ok else {}
            recent_rows = "".join(
                "<tr>"
                + "".join(f"<td>{html.escape(row.get(field, ''))}</td>" for field in (
                    "Date", "dtd", "implied_asset_value", "parameter_source", "status", "flags"
                ))
                + "</tr>"
                for row in reversed(rows[-10:])
            )
            page = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Greentown DTD Service</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1050px;margin:40px auto;padding:0 20px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:right}}th:first-child,td:first-child{{text-align:left}}.good{{color:#087f5b}}.warn{{color:#b05a00}}</style></head>
<body><h1>Greentown China DTD Service</h1>
<p>Local batch output: <code>{html.escape(str(results_path))}</code></p>
<p>Input cutoff: <b>{html.escape(rows[-1].get('Date', '') if rows else '')}</b> | Rows: <b>{len(rows)}</b> | Successful: <b class='good'>{len(ok)}</b> | Failed: <b class='warn'>{len(rows)-len(ok)}</b></p>
<p>Data status: <b class='{'good' if freshness == 'ok' else 'warn'}'>{html.escape(freshness)}</b>{f' ({age_days} calendar days since the latest input)' if age_days is not None else ''}</p>
<h2>Latest successful result</h2><table><tr><th>Date</th><th>DTD</th><th>AV (HKD m)</th><th>Threshold (HKD m)</th><th>Flags</th></tr>
<tr><td>{html.escape(latest.get('Date',''))}</td><td>{html.escape(latest.get('dtd',''))}</td><td>{html.escape(latest.get('implied_asset_value',''))}</td><td>{html.escape(latest.get('default_threshold',''))}</td><td>{html.escape(latest.get('flags',''))}</td></tr></table>
<h2>Recent observations</h2><table><tr><th>Date</th><th>DTD</th><th>AV (HKD m)</th><th>Parameter</th><th>Status</th><th>Flags</th></tr>{recent_rows}</table>
<h2>Update data</h2>
<p class='note'>Updates are append-only. Existing input dates are protected from overwrite.
The next accepted start date is <b>{html.escape(update_window['next_allowed_start'] or 'unavailable')}</b>;
the latest calendar date accepted for a request is <b>{html.escape(update_window['latest_calendar_request_end'])}</b>.
The archived market source currently reaches <b>{html.escape(_source_cutoffs(root)['market_source_cutoff'] or 'unavailable')}</b>;
the archived exact-date HKMA rate source reaches <b>{html.escape(_source_cutoffs(root)['rate_source_cutoff'] or 'unavailable')}</b>.
These source cutoffs can be earlier than the calendar date. Leave Start date blank to use the next accepted date automatically.
A missing exact-date HKMA 364-day EFB yield stops the entire requested interval without publishing a partial batch.</p>
<form method='post' action='/api/update'>
<label>Start date <input name='start' placeholder='YYYYMMDD'></label>
<label>End date <input name='end' placeholder='YYYYMMDD'></label>
<button type='submit'>Fetch and calculate</button></form>
<h2>Reviewed source observations</h2>
<form method='post' action='/api/sources'><input type='hidden' name='kind' value='rates'>
<h3>HKMA 12-month yield</h3><label>Date <input name='Date' placeholder='YYYYMMDD' required></label>
<label>Yield (%) <input name='Risk_Free_Rate' type='number' step='any' required></label>
<label>Source URL <input name='source_url' type='url' required></label>
<label><input name='reviewed' type='checkbox' required> Dated source and units reviewed</label>
<button type='submit'>Record rate</button></form>
<form method='post' action='/api/sources'><input type='hidden' name='kind' value='shares'>
<h3>Share count</h3><label>First available date <input name='effective_date' placeholder='YYYYMMDD' required></label>
<label>Shares (millions) <input name='shares_outstanding_m' type='number' step='any' required></label>
<label>Source URL <input name='source_url' type='url' required></label>
<label><input name='reviewed' type='checkbox' required> Dated source and units reviewed</label>
<button type='submit'>Record shares</button></form>
<form method='post' action='/api/sources'><input type='hidden' name='kind' value='financials'>
<h3>Financial statement</h3><label>First available date <input name='effective_date' placeholder='YYYYMMDD' required></label>
<label>Current liabilities (HKD m) <input name='BS_CUR_LIAB(HKD)' type='number' step='any' required></label>
<label>Long-term borrowings (HKD m) <input name='BS_LT_BORROW(HKD)' type='number' step='any' required></label>
<label>Total liabilities (HKD m) <input name='BS_TOT_LIAB2(HKD)' type='number' step='any' required></label>
<label>Total assets (HKD m) <input name='BS_TOT_ASSET(HKD)' type='number' step='any' required></label>
<label>Source URL <input name='source_url' type='url' required></label>
<label><input name='reviewed' type='checkbox' required> Dated source and units reviewed</label>
<button type='submit'>Record statement</button></form>
<p>JSON endpoints: <a href='/api/health'>/api/health</a> and <a href='/api/results'>/api/results</a>.</p></body></html>"""
            self._send(200, page)

        def do_POST(self):  # noqa: N802
            route = urlparse(self.path).path
            if route not in ("/api/update", "/api/sources"):
                self._send(404, "Not found")
                return
            size = int(self.headers.get("Content-Length", "0"))
            if size > 4096:
                self._send(413, "Request too large")
                return
            body = self.rfile.read(size).decode("utf-8")
            try:
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    arguments = json.loads(body)
                    if not isinstance(arguments, dict):
                        raise ValueError("request must be a JSON object")
                else:
                    from urllib.parse import parse_qs
                    arguments = {key: values[0] for key, values in parse_qs(body).items()}
                for key in ("start", "end"):
                    if key in arguments and not isinstance(arguments[key], str):
                        raise ValueError(f"{key} must be a YYYYMMDD string")
                if route == "/api/update":
                    summary = update_data(root, arguments.get("start") or None, arguments.get("end") or None)
                    status, payload = 200, {"status": "ok", "summary": summary}
                else:
                    kind = arguments.get("kind")
                    if not isinstance(kind, str):
                        raise ValueError("kind must be a string")
                    row = add_reviewed_source(root, kind, arguments)
                    status, payload = 201, {"status": "recorded", "kind": kind, "date": row.get("Date", row.get("effective_date"))}
            except (DataSourceError, ValueError, OSError) as exc:
                payload = {"status": "error", "error": str(exc)}
                if route == "/api/update":
                    current_rows = _load_results(results_path) if results_path.exists() else []
                    payload["update_window"] = _update_window(current_rows)
                    payload["source_cutoffs"] = _source_cutoffs(root)
                    payload["action"] = _update_action(str(exc))
                status = 422
            self._send(status, json.dumps(payload), "application/json")

        def log_message(self, fmt, *args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--results", type=Path, default=ROOT / "data" / "processed" / "computed_dtd.csv")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.results))
    print(f"DTD service listening at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping service")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
