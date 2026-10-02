# Greentown China daily DTD service report

## 1. Objective and workflow

The service treats DTD as a production signal. A daily run validates the input row, matches the same-date parameter record (or flags a carry-forward only after the parameter cutoff), derives the default threshold, solves implied asset value, calculates DTD, and writes both the result and the diagnostics. The historical batch and incremental rows use the same calculation function. The update command and web action fetch a range of daily closes, join sourced rates and reviewed as-of inputs, and recalculate the full batch.

The web form makes the incremental contract visible. It reports the published input cutoff, the next accepted start date, the latest calendar date that can be requested, and the separate market/rate archive cutoffs. After `20260831` is published, `20260801` is rejected because it would revisit protected history; `20260901` is the next accepted start. If HKMA has not published an exact-date 364-day yield for a requested trading date, the whole request fails before publication and the existing output remains unchanged. The calendar request limit therefore does not claim that the public sources are complete through that day.

The workflow has four operational stages:

1. **Ingest** unadjusted daily market closes and exact-date HKMA 12-month Exchange Fund Bill yields, while obtaining shares and statement values from approved dated sources.
2. **Prepare and validate** dates, identifiers, units, duplicates, missing values, and liability relationships. Existing dates are protected from overwrite.
3. **Compute** AV and DTD with the supplied sigma and delta. A carried-forward parameter is explicit in the output flags.
4. **Serve and review** the output through the local HTTP service and its JSON endpoints. The update form and `POST /api/update` trigger the same backfill/incremental pipeline as the command line; the local `POST /api/sources` forms record reviewed dated rate, share and filing observations.

Historical rates are not available through the latest-day HKMA indicative-pricing API, but HKMA publishes a separate dated daily EFB-yield series. An initial request to the historical API timed out with repeated HTTP 502 responses. A local recheck on 2026-10-01 at 19:50 returned HTTP 200 and 1,000 records, with observations from 2022-08-03 through 2026-08-31. The API was therefore reachable; the remaining issue is publication lag rather than a confirmed endpoint outage. HKMA's official monthly statistical bulletin also publishes table T050303 as Excel, with daily 364-day EFB yields in percent per annum, and its archived 2026 observations end on the same date. The same pipeline prepared and computed those 162 rows, preserving the supplied 2024-2025 history. There are 184 archived Yahoo closes through 2026-09-30, issuer monthly share returns through 2026-09-04, and converted annual/interim financial inputs. The 22 September yields remain unavailable from the required 364-day source. Eastmoney and Stooq remain market-price fallbacks on network errors. An update refuses to publish a partial requested interval when any quote date lacks a rate.

### Requirement audit (2026-10-01)

| PDF requirement | Current result | Remaining action |
| --- | --- | --- |
| Local Python service, shared batch/daily model path, AV/DTD, validation, simple review UI | Implemented and covered by offline tests | Review deployment controls before real production use |
| Automatic daily market cap and HKMA 12-month EFB rates | 184 Yahoo closes are archived; the official T050303 workbook supplies 162 exact-date 2026 yields. The historical API was rechecked successfully with HTTP 200 on 2026-10-01, but its latest observation is still 2026-08-31 | Obtain the 22 September yields once the official API or workbook publishes them; do not substitute another tenor |
| Extend inputs to latest completed trading day | **Partly met**; prepared input and DTD end 2026-08-31, while quotes cover through 2026-09-30 | Complete September rates, then rerun update |
| Financial statement updates and no look-ahead | Annual and interim RMB lines are mapped to HKD millions with announcement-day FX and effective the next trading day. Raw values, URLs and transformation are archived and used | Compare FX/classification conventions with CRI if available |
| 2024-2026 comparison with CRI output | 631 comparable rows through 2026-08-31 | Finish 2026 inputs through 2026-09-15 to extend comparison |
| Event study and AI disclosure | Company-specific announcements and dated computed DTD observations below; AI statement included | Revisit September when new inputs are available |

The submitted code uses the Python standard library plus `xlrd` to read HKMA's legacy `.xls` file. The numerical implementation itself uses no third-party solver.

### Submission checklist

| PDF submission item | Evidence in this repository | Status |
| --- | --- | --- |
| Code for the integrated daily workflow | `src/dtd_service/core.py`, `src/dtd_service/pipeline.py`, `src/dtd_service/sources.py`, `scripts/update_data.py`, `run_service.py`, and offline regression tests under `tests/` | Implemented |
| Dependencies, setup and rerun instructions | `pyproject.toml`, `requirements.txt`, `README.md`, and the commands above | Implemented |
| Supplied and newly collected data | Root supplied CSVs, copies under `data/raw/`, archived quotes/rates/shares/financials under `data/incoming/`, and the official HKMA workbook | Implemented; September rate coverage remains unavailable |
| Prepared inputs and computed outputs | `data/processed/prepared_inputs.csv`, `computed_dtd.csv`, `comparison_summary.json`, and `input_provenance.csv` | Implemented through 2026-08-31 |
| Workflow design, assumptions and limitations | Sections 1, 2 and 5 of this report | Implemented |
| Data source URLs, transformations and trade-offs | `docs/data_sources.md`, incoming-data README, and provenance columns | Implemented |
| DTD comparison with CRI output | Section 3 and `comparison_summary.json`; 631 comparable rows | Implemented through the available overlap |
| Event study with dated references | Section 4 and linked issuer announcements | Implemented |
| AI use statement and verification | Section 6 | Implemented |
| Update to the most recent completed trading day | The service and pipeline support it, but the official 364-day rate is missing for 22 September 2026 trading dates | Partially met due to external source publication lag |

This checklist is deliberately explicit: the repository is runnable and the requested workflow is implemented, but the final external-data cutoff cannot be claimed until HKMA publishes the missing exact-date 364-day yields.

### How the 2026-08-31 source cutoff was established

The historical HKMA API was checked with `scripts/check_hkma_historical.py`.
The script paginates the response, parses `end_of_day` dates, and reports the
maximum value. The successful 2026-10-01 response returned HTTP 200 and a
maximum date of **2026-08-31**; the official T050303 workbook independently
ended on the same date. This is how the report obtains the “8.31” mentioned in
the diagnostic output: it is the month-day portion of the latest observation
date **2026-08-31**, not the `efb_364d` yield. The script can be rerun with
`python3 scripts/check_hkma_historical.py --target-date 2026-09-15`. A target
date later than the reported maximum is left unmatched and is not filled with
another tenor or an indicative latest-day quote.

## 2. Calculation and numerical checks

For date `t`, the implementation follows the task reference equations:

```text
OL_t = TL_t - CL_t - LTB_t
L_t  = CL_t + 0.5 * LTB_t + delta_t * OL_t

E_t = V_t N(d+) - L_t exp(-r_t tau) N(d-)
d+  = [ln(V_t/L_t) + (r_t + 0.5 sigma^2) tau] / (sigma sqrt(tau))
d-  = d+ - sigma sqrt(tau)

DTD_t = ln(V_t/L_t) / (sigma sqrt(tau))
```

`tau=1`, risk-free rates are converted from percentage units to decimals, and sigma is already a decimal annualized volatility. Implied asset value is solved with bracketed bisection. The solver expands the upper bracket until the equity equation is above the observed market capitalization, then stops when the bracket is tight and checks the equity residual. A failed check is retained as a failed row instead of being silently dropped.

## 3. Supplied data and output comparison

The supplied daily inputs contain 492 rows from 2024-01-02 to 2025-12-31. The supplied parameter and CRI files run to 2026-09-15. The official HKMA workbook and archived market data add 162 rows through 2026-08-31. All 654 input rows calculate successfully. CRI has 23 blank DTD values over this overlap, leaving 631 comparable observations.

The PDF asks for inputs through the most recent completed trading day (2026-09-30 at this review, since 2026-10-01 is a Hong Kong market holiday). **That data requirement is not fully met as of 2026-10-01.** The 184 archived 2026 closes match all 173 parameter dates through 2026-09-15, and statement/share schedules exist. A local API request verified at 2026-10-01 19:50 returned HTTP 200, but its 1,000-record page covered only 2022-08-03 to 2026-08-31; the T050303 workbook has the same latest date. Its 162 exact-date values were extracted from the `364-day` column and are archived with source URLs. The September 22 dates cannot be computed without a verified rate of the required tenor. A subsequent update stops before publication when those rates are missing.

Financial preparation uses the [2025 annual results](https://doc.irasia.com/listco/hk/greentownchina/annual/2025/res.pdf) dated 2026-03-30 and [2026 interim results](https://doc.irasia.com/listco/hk/greentownchina/interim/2026/int.pdf) dated 2026-08-25. Page 5 of each states RMB-thousand current/non-current liabilities and non-current bank borrowings, senior notes, corporate debt instruments and lease liabilities. The annual debt components sum to RMB109,066,403 thousand and total liabilities to RMB344,146,974 thousand; interim figures are RMB100,842,499 thousand and RMB336,867,213 thousand. Total assets are derived from the reported "total assets less current liabilities" line plus current liabilities and reconcile to reported net assets. [2026-03-30 CNY/HKD 1.1335](https://api.frankfurter.dev/v1/2026-03-30?base=CNY&symbols=HKD) and [2026-08-25 CNY/HKD 1.1664](https://api.frankfurter.dev/v1/2026-08-25?base=CNY&symbols=HKD) convert RMB thousands to HKD millions. Annual values start on 2026-03-31 and interim values on 2026-08-26, after each announcement. This is an explicit conversion convention, not a claim to reproduce CRI's unreported FX policy.

The supplied-history calculation remains effectively identical to the CRI reference over its 476 comparable dates. The extended comparison reflects differences in newly sourced market and statement inputs:

| Metric | Result |
| --- | ---: |
| Mean signed difference (computed minus CRI), all dates | -0.004077 |
| Mean absolute difference, all dates | 0.004108 |
| Maximum absolute difference, all dates | 0.046036 |
| Comparable observations, all dates | 631 |
| Mean absolute difference, 2026 only | 0.016723 across 155 dates |

The supplied-history match validates the equation and solver; no parameter was fitted to CRI. The 2026 differences are near zero through 2026-03-30, then become materially negative after the first newly mapped statement takes effect on 2026-03-31: computed DTD is 0.8479 versus CRI 0.8726 that day. April's mean difference is -0.0292. The largest gap is -0.0460 on 2026-05-20. This timing points to financial mapping, announcement timing or FX convention as important possible causes, not a solver failure. After the 2026-08-25 interim announcement, another level shift is visible; CRI's value changes on the 25th while this pipeline first applies the filing on the 26th to avoid uncertain same-day availability. Yahoo's 2025-12-31 close was about HK$8.39; supplied CRI market cap divided by 2,539.59869 million issuer shares implies **HK$8.47**, roughly **0.94% higher**. The supplied 2025 row is not overwritten. The latest official workbook also differs from three supplied 2025 rates and lacks one supplied date, indicating possible source-vintage or preparation differences; supplied rates remain unchanged.

The complete ranked list is in data/processed/comparison_detail.csv. It contains every comparable date, sorted by absolute difference. The ten largest gaps are:

| Rank | Date | Computed DTD | CRI DTD | Difference |
| ---: | --- | ---: | ---: | ---: |
| 1 | 2026-05-20 | 1.1303 | 1.1764 | -0.0460 |
| 2 | 2026-05-14 | 1.3263 | 1.3600 | -0.0337 |
| 3 | 2026-05-12 | 1.3580 | 1.3917 | -0.0336 |
| 4 | 2026-05-11 | 1.3634 | 1.3970 | -0.0336 |
| 5 | 2026-05-08 | 1.3378 | 1.3712 | -0.0334 |
| 6 | 2026-05-13 | 1.3098 | 1.3432 | -0.0334 |
| 7 | 2026-05-15 | 1.2634 | 1.2962 | -0.0328 |
| 8 | 2026-05-07 | 1.2445 | 1.2767 | -0.0322 |
| 9 | 2026-05-06 | 1.2196 | 1.2514 | -0.0318 |
| 10 | 2026-05-18 | 1.1710 | 1.2024 | -0.0314 |

These are not isolated numerical failures: all ten rows have successful solver status and matched parameters. They form a concentration in April-May 2026, with the largest gap on 20 May. The consistent negative sign means the computed DTD is lower than CRI's reference over this episode. The most plausible explanations are differences in newly sourced market capitalization, the effective date or classification of the 2026 financial statements, the CNY/HKD conversion convention, or CRI's internal data vintage. The comparison is diagnostic rather than a calibration target.

## 4. DTD trajectory and event study

The computed DTD reached a sample low of **-0.452 on 2024-04-19**, then recovered during the second half of 2024. It reached a sample high of **1.033 on 2025-08-15** and ended the supplied input period at **0.559 on 2025-12-31**. Monthly averages moved from approximately -0.367 in April 2024 to 0.293 in October 2024, then to 0.838 in June 2025 before easing later in 2025.

The following company announcements are dated primary-source anchors. The numbers are computed DTD, rounded to four decimals; publication time relative to the Hong Kong close was not established, so same-day versus next-day interpretation is cautious. They are not causal tests.

| Date / period | Public information to review | DTD behavior and interpretation |
| --- | --- | --- |
| 2024-08-27 | [Issuer tender offer](https://doc.irasia.com/listco/hk/greentownchina/announcement/a240827.pdf) to repurchase for cash any/all US$100m outstanding 2.3% credit-enhanced bonds due 2025, at par plus accrued interest. This could reduce near-term maturity risk; it is not evidence of default. | DTD was -0.2936 on 08-26, -0.2888 on 08-27 and -0.2962 on 08-28: little immediate change. It rose to -0.1264 by 08-30, but attribution to the tender would be unsupported. |
| 2025-02-03 | [Issuer profit warning](https://doc.irasia.com/listco/hk/greentownchina/announcement/a250203.pdf): expected 2024 owner profit to fall by less than 50%, mainly due to inventory impairment amid the property downturn. The issuer also stated cash/short-term debt exceeded 2x. | DTD was 0.1584 on 01-28, 0.1537 on 02-03, 0.1338 on 02-04, then 0.2449 on 02-10. The brief decline coincides with the warning, but the rapid recovery argues against reading it as a sustained stress signal. |
| 2025-08-08 | [Issuer profit warning](https://doc.irasia.com/listco/hk/greentownchina/announcement/a250808.pdf): expected first-half owner profit to fall about 90% year on year, citing delivery timing and inventory impairments. The issuer reported cash/short-term debt above 2.5x and short-term debt below 20% of total debt. | DTD was 0.9169 on 08-07, 0.9112 on 08-08, 0.9204 on 08-11 and reached 1.0334 on 08-15. The signal was muted or counter-intuitive against the profit warning; accounting profit, liquidity and modeled default distance measure different things. |
| 2026-03-10 | [Issuer profit warning](https://doc.irasia.com/listco/hk/greentownchina/announcement/a260310.pdf): expected 2025 owner profit to fall about 95% year on year, citing lower gross margin, weaker joint-venture/associate results and impairment. The issuer also stated cash/short-term debt exceeded 2.5x. | Computed DTD rose from 0.8115 on 03-09 to 0.8334 on 03-10 and 0.8889 on 03-13; CRI was nearly identical on these dates. It did not show immediate credit deterioration, so liquidity and equity/volatility inputs are plausible alternative explanations. |
| 2026-08-10 | [Issuer profit warning](https://doc.irasia.com/listco/hk/greentownchina/announcement/a260810.pdf): expected first-half owner profit of RMB50m-100m versus RMB210m a year earlier, citing lower property revenue, destocking and impairments; the issuer said cash/short-term debt exceeded 2x. | Computed DTD rose from 0.5881 on 08-07 to 0.6007 on 08-10, while CRI rose from 0.6130 to 0.6257. Both signals were muted or counter-intuitive against the warning. The [2026 interim results](https://doc.irasia.com/listco/hk/greentownchina/interim/2026/int.pdf), released 08-25, are applied only from 08-26 in this pipeline. |

The computed examples do not establish anticipation. The February dip is near the warning and short-lived; the 2025 and 2026 August signals do not reflect clear deterioration despite profit warnings. The bond transaction had a delayed improvement that could reflect unrelated equity-market moves. The 2026 computed path can now be compared independently through August, but source conventions differ from CRI and the September extension remains incomplete. DTD mixes daily equity valuation with lower-frequency liabilities and supplied volatility, so it can lag accounting news or move without new financial statements. The one-year horizon and simplified drift convention make it a relative signal rather than a probability of default. The announcements describe weaker earnings and impairment, not a payment default; the issuer's liquidity statements are an important alternative interpretation.

## 5. Assumptions, limitations, and controls

- Financial statements are carried forward between disclosure dates. The two mapped filings use announcement-day reference FX, not a verified CRI production conversion; neither new value is applied before its release. The supplied 2025-08-22 baseline remains effective until the first new filing.
- HKMA's indicative endpoint provides only the latest business day. The separate historical daily-yield API initially produced repeated HTTP 502 responses, but a 2026-10-01 local recheck returned HTTP 200; the latest `end_of_day` remains 2026-08-31. The official T050303 Excel table supplies the same 364-day tenor and also stops at 2026-08-31. The pipeline requires exact-date rates for every returned quote date and does not mix the indicative series. The supplied parameter dates cross-check quote completeness through 2026-09-15; after that cutoff, there is no independent HKEX calendar check for missing quotes. The service exposes this rate-source cutoff separately from its calendar request limit, so a request can be valid in date order yet fail closed for missing source observations.
- The update relies on dated share counts and financial statement values. The issuer share schedule, original statement lines, FX observations and converted values are archived. As-of joins carry the latest available values forward and the provenance CSV records the source and effective date for every added row. Future share-count changes should take effect only after disclosure; all archived issuer counts are identical here.
- Yahoo Finance is a public-price adapter, Eastmoney is a third-party network fallback with an intermittently successful API, and Stooq did not deliver usable data here. A production deployment needs accessible, approved price sources and checks for coverage/corporate actions.
- Parameter carry-forward after 2026-09-15 is allowed by the task and is flagged. Parameter estimation is outside scope.
- Negative other liabilities are retained as a quality flag. Invalid or non-positive model inputs fail the row.
- CRI output is a comparison reference. No fitting or calibration is performed against it.

## 6. AI use statement

OpenAI Codex (the ChatGPT coding agent) was used in the local workspace to scaffold the repository, implement and revise the service and update pipeline, inspect the supplied PDF and CSV files, draft documentation, and propose regression tests. Python scripts, the standard-library `unittest` runner and `xlrd` were then used to run independent checks; AI suggestions were treated as drafts and were reviewed before being kept. In particular, source-selection decisions, date cutoffs, unit conversions, append-only update rules and fail-closed behavior were checked against the task wording and the original source material.

Event claims were checked against the linked issuer announcements and dated computed DTD rows. Share-count observations were extracted from the original monthly returns and checked against their submission dates. Annual/interim RMB statement lines and announcement dates were rechecked against the original issuer PDFs; the two currency rates were rechecked against their dated public responses. HKMA fields and units were checked against its official catalogue, schema and documentation. The independent official Excel table was downloaded and its headers, dates, values and coverage inspected; its sample rates were compared with supplied history, including the four exceptions documented above. Outputs were reviewed by rerunning calculations, verifying all 654 rows completed, inspecting comparison metrics, checking supplied input preservation, and running offline ingestion and calculation tests. External-source adapters fail closed and do not fabricate data when a public endpoint is unavailable.
