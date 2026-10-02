# Greentown China Daily DTD Service

This repository is a locally runnable implementation of the CRI written task. It prepares daily inputs, solves the supplied Merton-style asset value equation, calculates Distance-to-Default (DTD), keeps an audit trail for each row, and exposes the latest batch through a small web service. [The report](docs/report.md) includes a requirement-by-requirement audit.

The service uses the Python standard library plus `xlrd` to read HKMA's official Excel table when its API fails. The numerical solver is bracketed bisection with an explicit residual check, so the same code path is used for historical batch runs and future daily rows. The computed history now ends on **2026-08-31**. The remaining 22 September trading dates have archived closes but no verified exact-date HKMA 364-day EFB yields as of 2026-10-01.

## Quick start

```bash
python3 -m pip install -e .
python3 scripts/build_outputs.py
python3 -m unittest discover -s tests -v
python3 run_service.py
```

Then open `http://127.0.0.1:8000/`. The JSON endpoints are `/api/health` and `/api/results`. Health reports `stale` when the latest input is more than seven calendar days old; this is a monitoring threshold, not an exchange-calendar check. The page and health response distinguish the published input cutoff, the next append-only start date, the latest calendar date accepted for a request, and the actual archived market/rate source cutoffs. The calendar date is only a request guard; it does not mean HKMA has published a rate for every trading day through that date.

The package uses the standard `src` layout. The editable install makes
`dtd_service` importable from any working directory, and `pyrightconfig.json`
configures Pyright/Pylance to resolve imports directly from `src` in the editor.
The scripts also retain a small local path bootstrap so they can be run from a
fresh checkout before installation.

The editable install supplies `xlrd`, needed for the official `.xls` fallback.

The build command writes:

- `data/processed/computed_dtd.csv`: prepared inputs, parameters, AV, DTD, solver diagnostics, flags, and CRI comparison fields.
- `data/processed/comparison_detail.csv`: every comparable date ranked by absolute computed-versus-CRI DTD difference.
- `data/processed/comparison_summary.json`: row counts and comparison error metrics.
- After an update, `data/processed/prepared_inputs.csv`: supplied history plus sourced new rows.
- After an update, `data/processed/input_provenance.csv`: URLs and effective dates for each new row.

## Repository layout

- `src/dtd_service/core.py`: validation, Merton equation, DTD calculation, carry-forward selection, and protected incremental merge.
- `src/dtd_service/sources.py`: HKMA historical and latest-rate adapters, plus Yahoo, Eastmoney and Stooq price adapters for opt-in updates.
- `src/dtd_service/pipeline.py`: historical backfill, exact-date source joins, validation and publication.
- `scripts/build_outputs.py`: reproducible historical batch.
- `scripts/update_data.py`: backfill and daily update command.
- `scripts/fetch_latest.py`: inspects one latest quote/indicative-rate candidate after the user supplies the latest share count and financial statement values; this distinct HKMA series is not a production backfill source.
- `scripts/check_hkma_historical.py`: read-only diagnostic for the official dated HKMA 364-day EFB history; reports the returned date range and checks one target date without writing input data.
- `scripts/prepare_financials.py`: reproduces the HKD-million annual/interim filing rows from the archived RMB lines and dated exchange rates.
- `run_service.py`: dependency-free HTTP interface.
- `tests/`: unit and supplied-data regression tests.
- `data/raw/`: copies of the supplied CSV inputs.
- `data/processed/`: generated outputs used in the report.
- `docs/report.md`: submission report covering workflow, comparison, event study, assumptions, limitations, and AI use.

The `tests/` directory is part of the submission. It provides the reproducible checks for the Merton solver, CRI comparison, parameter carry-forward, input validation, source parsing, append-only updates, and publication-on-success behavior. The tests do not require live network access.

## Backfill and daily update workflow

The issuer's monthly returns provide the reviewed `data/incoming/shares.csv` schedule through its 2026-09-04 disclosure. `data/incoming/market_quotes.csv` contains 184 Yahoo closes through 2026-09-30, with source URLs and request coverage. `data/incoming/financials.csv` contains the supplied carry-forward baseline plus 2026-03-31 and 2026-08-26 filing updates; `financial_conversion.csv` records the RMB-thousand components and disclosure-day CNY/HKD conversion. `data/incoming/rates.csv` archives 162 dated HKMA yields through 2026-08-31, extracted from the official T050303 workbook snapshot at `data/raw/hkma_T050303.xls`. The pipeline first tries the official dated API, then uses the bundled workbook or downloads its latest official version for dates still missing.

```text
rates.csv: Date,Risk_Free_Rate,source_url
shares.csv: effective_date,shares_outstanding_m,source_url
financials.csv: effective_date,BS_CUR_LIAB(HKD),BS_LT_BORROW(HKD),BS_TOT_LIAB2(HKD),BS_TOT_ASSET(HKD),source_url
```

Use `YYYYMMDD` dates. There must be one exact-date HKMA 364-day EFB yield, in percentage units, for **each** new trading date. `source_url` identifies the original dated source, not this repository. Shares are in millions; financial fields are HKD millions. An `effective_date` is the first trading date on which the reviewed filing value can be used. Supply updates as additional rows in the relevant schedule. Do not use a later filing for an earlier date.

```bash
python3 scripts/update_data.py --end 20260930
# Then, on subsequent days:
python3 scripts/update_data.py
```

The command reuses the covered archive or requests unadjusted `3900.HK` daily closes for the missing date range, then multiplies by the approved effective share count. It tries Yahoo Finance first; on a network error or rate limit it tries Eastmoney's Hong Kong daily K-line API, then Stooq. The provenance file records the URL actually used. It checks the market-date coverage against the supplied parameter dates through 2026-09-15, then joins each day to an **exact-date** official HKMA historical 364-day EFB yield, archiving fetched observations. The official API and T050303 workbook are same-table sources; the workbook is published monthly and can lag the latest trading date. A reviewed `rates.csv` row can fill a same-series gap. The pipeline carries approved financial values forward, merges without changing supplied dates, calculates DTD, and publishes prepared input, output, summary and provenance files. A missing or invalid input stops publication. The original `historical_inputs.csv` remains unchanged.

The local web service exposes the same operation through the update form or `POST /api/update` with optional `start` and `end` dates in JSON. Its three `POST /api/sources` forms let a reviewer record a dated HKMA yield, share count or filing observation with a direct HTTPS source URL. They require a review checkbox, reject duplicate effective dates, and preserve the existing schedule. Recording an observation does not publish a DTD result; run the update after the sources are complete. The service returns a 422 error with the missing data detail if it cannot update. For example:

```bash
curl -X POST http://127.0.0.1:8000/api/update -H 'Content-Type: application/json' -d '{"end":"20260915"}'
```

Updates are contiguous and append-only. With a published cutoff of `20260831`, a request beginning `20260801` is rejected because it would revisit historical dates; the next accepted start is `20260901`. A request for `20260901` onward may still fail when an exact-date 364-day HKMA yield has not been published. Such a failure leaves the existing prepared inputs and computed output unchanged, and the response identifies the missing dates and the archived source cutoffs. After the official source publishes the missing observations, the same request can be retried. This protects historical records and avoids mixing indicative, interpolated, or different-tenor rates into the required series.

`scripts/build_outputs.py` uses `prepared_inputs.csv` when present; otherwise it recalculates the supplied 2024-2025 history.

## Single-row source check

The daily workflow is deliberately opt-in because a market feed does not provide a safe replacement for financial statement values or share count. A typical update is:

```bash
python3 scripts/fetch_latest.py \
  --shares-outstanding-m 2500 \
  --current-liabilities 303215.33845444146 \
  --long-term-borrowings 131735.00486963318 \
  --total-liabilities 436947.8691728547 \
  --total-assets 567688.0500928201
```

The command reads the latest completed close for `3900.HK` from Yahoo Finance and the latest 12-month Exchange Fund Bill indicative yield from HKMA's public API. It writes a reviewable JSON candidate under `data/incoming/` and fails if either source is unavailable or dates differ. This indicative series differs from the required historical 364-day yield and is **not** ingested by `update_data.py`; the command is only a source-inspection aid.

HKMA's indicative-price API returns the latest business day only. Its separate [daily historical EFB yield API](https://data.gov.hk/en-data/dataset/hk-hkma-t05-t050303efbn-yield-daily) exposes dated `efb_364d` records. Initial requests produced repeated HTTP 502 responses, but a local recheck on 2026-10-01 returned HTTP 200 with data through 2026-08-31. HKMA also publishes the [same table as Excel](https://www.hkma.gov.hk/eng/market-data-and-statistics/monthly-statistical-bulletin/), whose archived snapshot also ends on 2026-08-31. No historical yield is inferred from the CRI comparison output or replaced with today's rate.

## HKMA historical coverage check

To inspect the official dated HKMA series without changing project data, run:

```bash
python3 scripts/check_hkma_historical.py --target-date 2026-09-15
```

The diagnostic requests the historical endpoint in pages, reports the parsed
`end_of_day` range, and checks whether the requested date has an exact row. It
does not write to `data/incoming/rates.csv`; reviewed observations must still
pass the normal source and update workflow. The verification on 2026-10-01
returned HTTP 200 and observations through **2026-08-31**. That value is the
latest observation date obtained by taking the maximum parsed `end_of_day` from
the returned records, not a yield value and not an interpolated estimate. A
missing 2026-09-15 or 2026-09-30 row means the official historical series had
not published that exact date at the time of the check.

## Model convention

For each date:

```text
OL = total_liabilities - current_liabilities - long_term_borrowings
L  = current_liabilities + 0.5 * long_term_borrowings + delta * OL
E  = V * N(d+) - L * exp(-r*tau) * N(d-)
DTD = log(V / L) / (sigma * sqrt(tau))
```

`Risk_Free_Rate` is supplied in percentage units and is divided by 100. `DTD_SIGMA` is already a decimal annualized volatility. `tau` is one year. For a date after the latest parameter record, the latest available `sigma` and `delta` are carried forward and flagged as `parameter_carried_forward`.

## Reproducibility notes

The checked-in processed output combines supplied 2024-2025 inputs with 162 sourced 2026 rows. The official T050303 snapshot and the successfully rechecked API both end on 2026-08-31, so the computed-input cutoff is still short of the latest completed trading day, 2026-09-30. A comparison with supplied 2024-2025 rates found three changed values and one date missing in the current official workbook; supplied historical rows remain untouched. Yahoo's 2025-12-31 close was about HK$8.39 in an earlier source check, whereas the supplied market cap and issuer share count imply HK$8.47, a roughly 0.94% difference. Public source URLs, transformations, and the remaining data requirements are documented in `docs/data_sources.md` and `data/incoming/README.md`.
