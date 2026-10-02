import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dtd_service.sources import (  # noqa: E402
    DataSourceError,
    build_market_cap_input,
    fetch_hkma_12m_rate,
    fetch_hkma_historical_rates,
    read_hkma_workbook_rates,
    fetch_eastmoney_daily_quotes,
    fetch_yahoo_daily_quote,
    fetch_yahoo_daily_quotes,
    fetch_stooq_daily_quotes,
)


class SourceTests(unittest.TestCase):
    def test_yahoo_quote_from_nested_response(self):
        payload = {
            "chart": {
                "result": [{
                    "timestamp": [1704153600, 1704240000],
                    "indicators": {"quote": [{"close": [4.5, 4.75]}]},
                }],
            },
        }
        with patch("dtd_service.sources._get_json", return_value=payload):
            quote = fetch_yahoo_daily_quote()
        self.assertEqual(quote["Date"], "20240103")
        self.assertEqual(quote["close"], 4.75)

    def test_malformed_yahoo_response_raises_data_source_error(self):
        with patch("dtd_service.sources._get_json", return_value={"chart": []}):
            with self.assertRaisesRegex(DataSourceError, "Yahoo chart must be an object"):
                fetch_yahoo_daily_quote()

    def test_hkma_rate_and_market_cap_input(self):
        payload = {"result": {"data": [{
            "term": "12M", "end_of_date": "2024-01-03", "yield": "3.98",
        }]}}
        with patch("dtd_service.sources._get_json", return_value=payload):
            rate = fetch_hkma_12m_rate()
        quote = {"Date": "20240103", "close": 4.75}
        financials = {
            "BS_CUR_LIAB(HKD)": 200.0,
            "BS_LT_BORROW(HKD)": 50.0,
            "BS_TOT_LIAB2(HKD)": 300.0,
            "BS_TOT_ASSET(HKD)": 400.0,
        }
        row = build_market_cap_input(quote, rate, 100.0, financials)
        self.assertEqual(row["CUR_MKT_CAP(HKD)"], 475.0)
        self.assertEqual(row["Risk_Free_Rate"], 3.98)

    def test_hkma_historical_uses_dated_364_day_yield(self):
        payload = {"header": {"success": True}, "result": {"records": [
            {"end_of_day": "2026-01-02", "efb_364d": 2.51},
            {"end_of_day": "2026-01-05", "efb_364d": ""},
        ]}}
        with patch("dtd_service.sources._get_json", return_value=payload) as get:
            rates = fetch_hkma_historical_rates(date(2026, 1, 2), date(2026, 1, 5))
        self.assertEqual(rates[0]["Date"], "20260102")
        self.assertEqual(rates[0]["Risk_Free_Rate"], 2.51)
        self.assertEqual(len(rates), 1)
        self.assertIn("from=2026-01-02", get.call_args.args[0])

    def test_official_workbook_matches_supplied_rate_and_covers_august(self):
        workbook = Path(__file__).resolve().parents[1] / "data/raw/hkma_T050303.xls"
        rates = read_hkma_workbook_rates(workbook, date(2025, 12, 31), date(2026, 8, 31))
        self.assertEqual(rates[0]["Risk_Free_Rate"], 2.21)
        self.assertEqual(rates[-1]["Date"], "20260831")
        self.assertEqual(rates[-1]["Risk_Free_Rate"], 2.94)
        self.assertEqual(len(rates), 163)

    def test_invalid_close_raises_data_source_error(self):
        with self.assertRaisesRegex(DataSourceError, "Yahoo close must be a finite number"):
            build_market_cap_input(
                {"Date": "20240103", "close": object()},
                {"Date": "20240103", "Risk_Free_Rate": 3.98},
                100.0,
                {},
            )

    def test_yahoo_range_uses_unadjusted_closes_and_inclusive_end(self):
        payload = {"chart": {"result": [{
            "timestamp": [1704153600, 1704240000],
            "indicators": {"quote": [{"close": [4.5, 4.75]}]},
        }]}}
        with patch("dtd_service.sources._get_json", return_value=payload) as get:
            quotes = fetch_yahoo_daily_quotes(date(2024, 1, 2), date(2024, 1, 3))
        self.assertEqual([row["Date"] for row in quotes], ["20240102", "20240103"])
        self.assertIn("period2=1704326400", get.call_args.args[0])

    def test_yahoo_range_rejects_missing_close(self):
        payload = {"chart": {"result": [{
            "timestamp": [1704153600],
            "indicators": {"quote": [{"close": [None]}]},
        }]}}
        with patch("dtd_service.sources._get_json", return_value=payload):
            with self.assertRaisesRegex(DataSourceError, "missing on 20240102"):
                fetch_yahoo_daily_quotes(date(2024, 1, 2), date(2024, 1, 3))

    def test_stooq_csv_daily_quotes(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"Date,Open,High,Low,Close,Volume\n2024-01-02,4,5,3,4.75,100\n"
        with patch("dtd_service.sources.urllib.request.urlopen", return_value=Response()):
            quotes = fetch_stooq_daily_quotes(date(2024, 1, 2), date(2024, 1, 3))
        self.assertEqual(quotes[0]["Date"], "20240102")
        self.assertEqual(quotes[0]["close"], 4.75)

    def test_eastmoney_unadjusted_hk_closes(self):
        payload = {"rc": 0, "data": {"code": "03900", "market": 116, "klines": [
            "2026-09-01,6.300,6.085,6.300,6.000,31355820,190903807.000",
            "2026-09-02,5.940,5.850,6.045,5.770,34020470,200162666.000",
        ]}}
        with patch("dtd_service.sources._get_json", return_value=payload) as get:
            quotes = fetch_eastmoney_daily_quotes(date(2026, 9, 1), date(2026, 9, 2))
        self.assertEqual([row["Date"] for row in quotes], ["20260901", "20260902"])
        self.assertEqual([row["close"] for row in quotes], [6.085, 5.85])
        self.assertIn("fqt=0", get.call_args.args[0])
        self.assertIn("secid=116.03900", get.call_args.args[0])

    def test_eastmoney_rejects_wrong_security_and_duplicate(self):
        payload = {"rc": 0, "data": {"code": "03901", "market": 116, "klines": []}}
        with patch("dtd_service.sources._get_json", return_value=payload):
            with self.assertRaisesRegex(DataSourceError, "different security"):
                fetch_eastmoney_daily_quotes(date(2026, 9, 1), date(2026, 9, 2))
        payload["data"] = {"code": "03900", "market": 116, "klines": [
            "2026-09-01,6.3,6.085", "2026-09-01,6.3,6.085",
        ]}
        with patch("dtd_service.sources._get_json", return_value=payload):
            with self.assertRaisesRegex(DataSourceError, "duplicate date"):
                fetch_eastmoney_daily_quotes(date(2026, 9, 1), date(2026, 9, 2))

    @patch("dtd_service.sources.fetch_eastmoney_daily_quotes", side_effect=DataSourceError("feed unavailable"))
    @patch("dtd_service.sources.fetch_stooq_daily_quotes")
    @patch("dtd_service.sources._get_json", side_effect=DataSourceError("could not read Yahoo: timeout"))
    def test_yahoo_network_failure_uses_stooq(self, get_json, stooq, eastmoney):
        stooq.return_value = [{"Date": "20240102", "close": 4.75, "source": "https://stooq.com"}]
        quotes = fetch_yahoo_daily_quotes(date(2024, 1, 2), date(2024, 1, 2))
        self.assertEqual(quotes[0]["source"], "https://stooq.com")
        stooq.assert_called_once()

    @patch("dtd_service.sources.fetch_eastmoney_daily_quotes")
    @patch("dtd_service.sources.fetch_stooq_daily_quotes")
    @patch("dtd_service.sources._get_json", side_effect=DataSourceError("could not read Yahoo: 429"))
    def test_yahoo_rate_limit_uses_eastmoney(self, get_json, stooq, eastmoney):
        eastmoney.return_value = [{"Date": "20260901", "close": 6.085, "source": "https://push2his.eastmoney.com"}]
        quotes = fetch_yahoo_daily_quotes(date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(quotes[0]["close"], 6.085)
        stooq.assert_not_called()

    @patch("dtd_service.sources.fetch_eastmoney_daily_quotes", side_effect=DataSourceError("feed unavailable"))
    @patch("dtd_service.sources.fetch_stooq_daily_quotes")
    @patch("dtd_service.sources._get_json", side_effect=DataSourceError("could not read Yahoo: timeout"))
    def test_single_yahoo_network_failure_uses_stooq(self, get_json, stooq, eastmoney):
        stooq.return_value = [{"Date": "20240102", "close": 4.75, "source": "https://stooq.com"}]
        quote = fetch_yahoo_daily_quote()
        self.assertEqual(quote["Date"], "20240102")
        stooq.assert_called_once()


if __name__ == "__main__":
    unittest.main()
