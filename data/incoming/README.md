# Incoming source data

The supplied daily-input history ends on **2025-12-31**. `market_quotes.csv` archives 184 Yahoo unadjusted 03900.HK closes from 2026-01-02 through 2026-09-30, with the fetched interval in `market_quotes.coverage.json`. All 173 quote dates through 2026-09-15 match the supplied parameter dates. `shares.csv` contains issuer monthly-return share counts disclosed from 2025-12-04 through 2026-09-04.

`financials.csv` starts with the last supplied financial tuple, first present on 2025-08-22, and marks its provenance `provided:historical_inputs.csv`. It also contains issuer annual and interim statement updates effective **2026-03-31** and **2026-08-26**. `financial_conversion.csv` preserves the published RMB-thousand lines, disclosure dates, CNY/HKD rates and both source URLs. Run `python3 scripts/prepare_financials.py` to verify/recreate the converted HKD-million rows. The 2025 annual announcement is dated 2026-03-30 and the interim announcement 2026-08-25; applying each from the next trading day avoids same-day look-ahead. The conversion uses the FX reference rate on the announcement date, a disclosed approximation rather than CRI's undocumented currency method.

Add later reviewed filing rows to `financials.csv` using the columns in the repository README, or enter them through the local web form. New external rows need direct, reviewable HTTPS URLs; the baseline's `provided:` reference is the one exception because it comes from the task's supplied CSV. `rates.csv` holds 162 exact-date yields from the official HKMA T050303 daily table, in percent per annum, through 2026-08-31. The original workbook snapshot is `data/raw/hkma_T050303.xls`; each archived rate cites the official download URL. The pipeline tries the official historical API, then the official workbook when rates are missing. The separate latest-day indicative series is for candidate inspection only and is not mixed into the production history. Confirm the 364-day bill tenor, percent units, original currency, RMB/HKD conversion, liability mapping and statement availability date before adding financial values.

`python3 scripts/update_data.py` or the web update form reuses the archived quotes for a covered interval, otherwise requests daily closes from Yahoo, with Eastmoney then Stooq as network-error fallbacks. It joins exact-date rates and as-of share/statement inputs, checks quoted dates against supplied parameter dates through 2026-09-15, validates the batch, and publishes prepared history and DTD. The prepared and computed history ends **2026-08-31**. An initial HKMA historical API request returned repeated HTTP 502 responses, but a local recheck on 2026-10-01 returned HTTP 200; its latest `end_of_day` and the T050303 workbook's latest date are both 2026-08-31. The 22 September quote dates still need same-day rates; missing rates stop the next update without changing published input and result. `scripts/fetch_latest.py` remains a one-row candidate inspection command; its JSON is not automatically published.
`python3 scripts/update_data.py` or the web update form reuses the archived quotes for a covered interval, otherwise requests daily closes from Yahoo, with Eastmoney then Stooq as network-error fallbacks. It joins exact-date rates and as-of share/statement inputs, checks quoted dates against supplied parameter dates through 2026-09-15, validates the batch, and publishes prepared history and DTD. The prepared and computed history ends **2026-08-31**. An initial HKMA historical API request returned repeated HTTP 502 responses, but a local recheck on 2026-10-01 returned HTTP 200; its latest `end_of_day` and the T050303 workbook's latest date are both 2026-08-31. The 22 September quote dates still need same-day rates; missing rates stop the next update without changing published input and result. `scripts/fetch_latest.py` remains a one-row candidate inspection command; its JSON is not automatically published.

The web update is intentionally contiguous: with a cutoff of `20260831`, the next accepted start is `20260901`, and an earlier start is rejected to protect historical rows. The page/API also shows the separate archived market and HKMA-rate cutoffs. A calendar end date can be requestable while the rate source is still behind it; in that case the update reports the missing exact dates and publishes no partial batch.

## HKMA date diagnostic

The project diagnostic command is:

```bash
python3 scripts/check_hkma_historical.py --target-date 2026-09-15
```

It reports the maximum parsed `end_of_day` returned by the official historical
API. The verified **2026-08-31** value is therefore an API publication cutoff,
not a calculated rate, an assumed date, or evidence that September was not a
trading period. The field used for the rate itself is `efb_364d`, whose units
are percent per annum. The script is read-only and does not append an
unreviewed row to `rates.csv`.
