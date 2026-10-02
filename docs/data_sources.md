# Data sources and preparation

## Supplied CRI files

The three supplied CSV files are preserved at the repository root and copied to `data/raw/`:

| File | Coverage | Use |
| --- | --- | --- |
| `historical_inputs.csv` | 2024-01-02 to 2025-12-31, 492 rows | Daily market cap, liabilities, assets, and risk-free rate |
| `parameters.csv` | 2024-01-02 to 2026-09-15, 665 rows | Supplied annualized sigma and other-liability weight |
| `full_historical_dtd_output.csv` | 2024-01-02 to 2026-09-15, 665 rows | CRI reference DTD for comparison |

The task PDF is the authoritative specification for column definitions, units, and model equations.

## Public update sources

- [HKMA historical daily EFB yields](https://data.gov.hk/en-data/dataset/hk-hkma-t05-t050303efbn-yield-daily) use `https://api.hkma.gov.hk/public/market-data-and-statistics/monthly-statistical-bulletin/efbn/efbn-yield-daily`. The official [OpenAPI schema](https://apidocs.hkma.gov.hk/wp-content/uploads/swagger/T050303-swagger.json) defines dated `result.records` with `end_of_day` and `efb_364d`; the [field guide](https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/monthly-statistical-bulletin/efbn/efbn-yield-daily/) defines `efb_364d` as a 364-day Exchange Fund Bill yield in percent per annum. The adapter requests a date range and paginates. The correct government data-set page was reachable on 2026-10-01 and links to this API; the shorter `hk-hkma-efbn-yield-daily` path returns 404. Initial requests timed out with repeated HTTP 502 responses, but a local verification at 2026-10-01 19:50 returned HTTP 200 and 1,000 records covering 2022-08-03 to 2026-08-31. The API is therefore usable, but it had not published the 22 September observations needed for the current extension.
- [HKMA monthly statistical bulletin table 5.3.3](https://www.hkma.gov.hk/eng/market-data-and-statistics/monthly-statistical-bulletin/) has an independent [T050303 Excel download](https://www.hkma.gov.hk/media/eng/doc/market-data-and-statistics/monthly-statistical-bulletin/T050303.xls) for **daily figures** of Exchange Fund Bills and Notes yields. The `364-day` EFB column is the same instrument/tenor as API field `efb_364d`; dates are Excel dates and yields are percent per annum. The official workbook snapshot is archived at `data/raw/hkma_T050303.xls`, and `data/incoming/rates.csv` preserves 162 exact-date 2026 values and their source URL. The site marked this table last updated 2026-09-03; its final observation is 2026-08-31. The code uses this workbook if API data are missing and can fetch a later published version automatically. The workbook states that yield figures are powered by LSEG.
- The separate [HKMA indicative pricing API](https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/daily-monetary-statistics/efbn-indicative-price/) at `https://api.hkma.gov.hk/public/market-data-and-statistics/daily-monetary-statistics/efbn-indicative-price?segment=IndicativePrice` provides the latest business day only. It is used only by the single-row candidate inspection script. The batch pipeline does **not** mix it into the historical `efb_364d` series because the two rate conventions have not been reconciled.
- Yahoo Finance chart endpoint for the listed security's daily close: `https://query1.finance.yahoo.com/v8/finance/chart/3900.HK?range=1mo&interval=1d&events=history`. This is a convenience market-price feed, so a production deployment should replace it with an approved licensed feed if required by policy.
- Historical close retrieval uses the same Yahoo chart endpoint with `period1`, `period2` and `interval=1d`. The unadjusted close is used, since market capitalization needs the traded price rather than a split/dividend-adjusted return series. The fetch interval is inclusive at the user-facing end date. `data/incoming/market_quotes.csv` archives **184 rows from 2026-01-02 to 2026-09-30**, with the precise request URL per row and coverage metadata. Its 173 dates through the 2026-09-15 parameter cutoff agree exactly with the supplied parameter dates. The archive lets a rerun reuse those prices even when Yahoo later rate-limits the connection.
- [Eastmoney Hong Kong daily K-line API](https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=116.03900&klt=101&fqt=0&beg=20260901&end=20260915&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61) is the first fallback after a Yahoo network error or rate limit. `secid=116.03900` identifies the Hong Kong listing; `fqt=0` requests unadjusted daily data. The second K-line number after the date is the close, in HKD for this listing. An actual response on 2026-10-01 contained 11 trading observations from 2026-09-01 to 2026-09-15, but subsequent requests disconnected, so full-period coverage and repeatability are not established. The adapter validates security, market, dates, duplicates and prices before use. This is a third-party data source, not an HKEX official record.
- [Stooq daily CSV](https://stooq.com/q/d/l/?s=3900.hk&i=d) is the final fallback on a Yahoo network-level failure if Eastmoney also fails. Its expected CSV schema is `Date` and `Close`, but the live request returned a browser-verification page, so 03900.HK availability and its price convention remain **unverified**. The adapter rejects that response. Neither fallback replaces a Yahoo response that is structurally invalid, because it needs review.
- Greentown China's [announcement archive](https://www.greentownchina.com/en/ir/announcements.php) and [financial reports](https://www.greentownchina.com/en/ir/reports.php) are primary sources for dated issuer disclosures. The [November 2025 monthly return](https://doc.irasia.com/listco/hk/greentownchina/announcement/a251204a.pdf) and each following monthly return through the [August 2026 return](https://doc.irasia.com/listco/hk/greentownchina/announcement/a260904a.pdf) state 2,539,598,690 issued shares and zero treasury shares. Their public submission dates and source URLs are recorded in `data/incoming/shares.csv`.
- The [2025 annual results](https://doc.irasia.com/listco/hk/greentownchina/annual/2025/res.pdf), dated **2026-03-30**, and [2026 interim results](https://doc.irasia.com/listco/hk/greentownchina/interim/2026/int.pdf), dated **2026-08-25**, supply the RMB-thousand statement lines on page 5 of each PDF. The [2026-03-30](https://api.frankfurter.dev/v1/2026-03-30?base=CNY&symbols=HKD) and [2026-08-25](https://api.frankfurter.dev/v1/2026-08-25?base=CNY&symbols=HKD) CNY/HKD reference rates are 1.1335 and 1.1664. `data/incoming/financial_conversion.csv` records these raw lines, rates, release dates and URLs; `scripts/prepare_financials.py` converts them to `financials.csv` effective the next trading day.
- HKEX disclosure search for issuer announcements under stock code 03900: [HKEX title search](https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en).

## Reproducing the 2026-08-31 cutoff

The date **2026-08-31** is an observed source-coverage cutoff. It was obtained
by reading the official historical API successfully (HTTP 200 on 2026-10-01),
parsing each record's `end_of_day` field, and taking the maximum parsed date.
The same maximum date was confirmed in the official T050303 workbook. The
`efb_364d` field in those records is the 364-day EFB yield in percent per
annum; **8.31 is not a yield of 8.31%**. No interpolation, current indicative
quote, or other tenor is used when an exact date is absent. The diagnostic is
reproducible with:

```bash
python3 scripts/check_hkma_historical.py --target-date 2026-09-15
```

This proves only that 2026-08-31 was the latest date returned by the source at
the time of the check. It does not prove that later trading days did not occur;
it records the publication lag that prevents those rows from being admitted to
the checked-in batch.

## Transformations and controls

- HKMA yield values are retained in percentage units in the prepared input, matching the supplied schema; the calculation converts them to decimals internally.
- The official workbook identifies the 364-day EFB yield and has all 162 archived quote dates through 2026-08-31. The archived CSV uses `Date,Risk_Free_Rate,source_url`; rates remain in percentage units. A comparison with the supplied 2024-2025 inputs found three differing same-date rates (2025-02-12, 2025-05-06, 2025-05-27) and no official workbook row for 2025-08-05. This can reflect source-vintage or preparation differences; supplied history is never rewritten. No HIBOR, US Treasury rate, indicative 12-month quote or current rate is substituted for an earlier date.
- Approved share-count changes belong in `data/incoming/shares.csv` as `effective_date,shares_outstanding_m,source_url`. Approved filing values belong in `data/incoming/financials.csv` as `effective_date,BS_CUR_LIAB(HKD),BS_LT_BORROW(HKD),BS_TOT_LIAB2(HKD),BS_TOT_ASSET(HKD),source_url`. An update uses the most recent row available as of each trading date; the provenance file records which row was used. The existing share count did not change across the archived monthly returns. A future changed share count should be effective no earlier than the first trading close after publication.
- For each filing, current liabilities and non-current liabilities are the reported totals; long-term borrowings sum the **non-current** bank and other borrowings, senior notes, corporate debt instruments and lease liabilities. Total liabilities add current and non-current totals. Total assets add current liabilities to the reported "total assets less current liabilities" line; this reconciles to reported net assets in both filings. Each RMB-thousand amount is multiplied by announcement-day CNY/HKD and divided by 1,000 to get HKD millions. This is a transparent conversion assumption; CRI's historical FX timing and classification rules are unknown. The annual and interim values first apply on 2026-03-31 and 2026-08-26, respectively, so neither is applied before announcement.
- Yahoo close is multiplied by a user-supplied share count in millions to create HKD millions of market capitalization. The share count is never guessed by the adapter.
- Current liabilities, long-term borrowings, total liabilities, and total assets are supplied by the latest approved statement. For a test day with no new statement, the previous approved values can be carried forward and the decision should be recorded in the incoming JSON.
- Duplicate archive, market, parameter and input dates; missing or unrecognized market source URLs; missing exact-date rates or as-of share/statement rows; invalid numeric values; and missing Yahoo closes stop an update before publication. Out-of-range and unordered quote dates are rejected. Through the parameter cutoff, supplied parameter dates provide an independent trading-date check for missing quote rows. After that cutoff, no exchange-calendar check is implemented; recent feed completeness remains a limitation. The daily input merge refuses to change existing dates. A failed calculation also stops publication. The original supplied history is never overwritten.
- Parameters are matched by date. For a date after the latest parameter record, the latest record is carried forward and the output is flagged. No parameter estimation is performed.

## Data cutoff

The reproducible checked-in batch has daily inputs and DTD through **2026-08-31**: 492 supplied rows plus 162 newly prepared rows. The supplied parameter and CRI files extend through **2026-09-15**. There are 184 archived Yahoo quote rows through **2026-09-30**, a dated share schedule, and two converted statement updates. The historical API and official workbook were both verified, but their latest observation is still 2026-08-31. The remaining **22 September dates lack exact-date HKMA 364-day EFB yields**, so the requirement to update through the latest completed trading day (**2026-09-30** at this 2026-10-01 review) remains unmet. The earlier 2025-12-31 Yahoo close check was about HK$8.39; supplied CRI market cap divided by issuer shares implies HK$8.47 (roughly 0.94% higher), an additional market-cap comparability limitation.
