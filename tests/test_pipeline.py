import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dtd_service.pipeline import add_reviewed_source, archive_market_quotes, update_data  # noqa: E402
from dtd_service.sources import DataSourceError  # noqa: E402


class PipelineTests(unittest.TestCase):
    def setUp(self):
        historical = patch("dtd_service.pipeline.fetch_hkma_historical_rates", return_value=[])
        self.historical_rates = historical.start()
        self.addCleanup(historical.stop)
        workbook = patch("dtd_service.pipeline.fetch_hkma_workbook_rates", return_value=[])
        self.workbook_rates = workbook.start()
        self.addCleanup(workbook.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        incoming = self.root / "data" / "incoming"
        incoming.mkdir(parents=True)
        original = Path(__file__).resolve().parents[1]
        for name in ("historical_inputs.csv", "parameters.csv", "full_historical_dtd_output.csv"):
            (self.root / name).write_bytes((original / name).read_bytes())
        (incoming / "rates.csv").write_text(
            "Date,Risk_Free_Rate,source_url\n20260102,2.1,https://hkma.example/20260102\n",
            encoding="utf-8",
        )
        (incoming / "shares.csv").write_text(
            "effective_date,shares_outstanding_m,source_url\n20260101,2500,https://issuer.example/shares\n",
            encoding="utf-8",
        )
        (incoming / "financials.csv").write_text(
            "effective_date,BS_CUR_LIAB(HKD),BS_LT_BORROW(HKD),BS_TOT_LIAB2(HKD),BS_TOT_ASSET(HKD),source_url\n"
            "20260101,303215.33845444146,131735.00486963318,436947.8691728547,567688.0500928201,https://issuer.example/report\n",
            encoding="utf-8",
        )

    @staticmethod
    def quotes(start, end):
        return [{"Date": "20260102", "close": 8.5, "source": "https://query1.finance.yahoo.com/test"}]

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_backfill_publishes_sourced_input_and_recalculated_output(self, fetch):
        fetch.side_effect = self.quotes
        summary = update_data(self.root, end="20260102")
        self.assertEqual(summary["new_rows"], 1)
        self.assertEqual(summary["input_date_end"], "20260102")
        self.assertEqual(summary["failed_rows"], 0)
        fetch.assert_called_once_with(date(2026, 1, 1), date(2026, 1, 2))
        with (self.root / "data/processed/prepared_inputs.csv").open(newline="") as stream:
            inputs = list(csv.DictReader(stream))
        self.assertEqual(len(inputs), 493)
        self.assertEqual(float(inputs[-1]["CUR_MKT_CAP(HKD)"]), 21250.0)
        with (self.root / "data/processed/computed_dtd.csv").open(newline="") as stream:
            results = list(csv.DictReader(stream))
        self.assertEqual(results[-1]["status"], "ok")
        self.assertEqual(results[-1]["Date"], "20260102")
        with (self.root / "data/processed/input_provenance.csv").open(newline="") as stream:
            audit = list(csv.DictReader(stream))
        self.assertEqual(audit[0]["rate_source"], "https://hkma.example/20260102")
        self.assertEqual((self.root / "historical_inputs.csv").read_bytes(),
                         (Path(__file__).resolve().parents[1] / "historical_inputs.csv").read_bytes())

    @patch("dtd_service.pipeline._latest_completed_day", return_value=date(2026, 1, 2))
    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_latest_indicative_rate_is_not_mixed_into_historical_series(self, fetch, latest):
        fetch.side_effect = self.quotes
        (self.root / "data/incoming/rates.csv").write_text(
            "Date,Risk_Free_Rate,source_url\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(DataSourceError, "missing exact-date HKMA 12M rates"):
            update_data(self.root)
        self.assertFalse((self.root / "data/processed/prepared_inputs.csv").exists())

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_missing_as_of_share_count_does_not_publish(self, fetch):
        fetch.side_effect = self.quotes
        (self.root / "data/incoming/shares.csv").write_text(
            "effective_date,shares_outstanding_m,source_url\n20260105,2500,https://issuer.example\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DataSourceError, "no approved share count"):
            update_data(self.root, end="20260102")
        self.assertFalse((self.root / "data/processed/computed_dtd.csv").exists())

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_missing_intermediate_rate_is_not_carried_forward(self, fetch):
        fetch.return_value = [
            {"Date": "20260102", "close": 8.5, "source": "https://query1.finance.yahoo.com/test"},
            {"Date": "20260105", "close": 8.6, "source": "https://query1.finance.yahoo.com/test"},
            {"Date": "20260106", "close": 8.7, "source": "https://query1.finance.yahoo.com/test"},
        ]
        (self.root / "data/incoming/rates.csv").write_text(
            "Date,Risk_Free_Rate,source_url\n"
            "20260102,2.1,https://hkma.example/20260102\n"
            "20260106,2.0,https://hkma.example/20260106\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DataSourceError, "20260105"):
            update_data(self.root, end="20260106")
        self.assertFalse((self.root / "data/processed/prepared_inputs.csv").exists())

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_explicit_start_cannot_skip_history(self, fetch):
        with self.assertRaisesRegex(DataSourceError, "day after existing input cutoff"):
            update_data(self.root, start="20260102", end="20260102")
        fetch.assert_not_called()

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_duplicate_market_dates_do_not_publish(self, fetch):
        fetch.return_value = self.quotes(None, None) * 2
        with self.assertRaisesRegex(DataSourceError, "duplicate or unordered"):
            update_data(self.root, end="20260102")
        self.assertFalse((self.root / "data/processed/prepared_inputs.csv").exists())

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_missing_known_trading_day_does_not_publish(self, fetch):
        fetch.return_value = self.quotes(None, None)
        with self.assertRaisesRegex(DataSourceError, "omitted 1 supplied trading dates: 20260105"):
            update_data(self.root, end="20260105")
        self.assertFalse((self.root / "data/processed/prepared_inputs.csv").exists())

    def test_reviewed_source_entry_requires_source_and_unique_date(self):
        row = {
            "Date": "20260102", "Risk_Free_Rate": "2.1",
            "source_url": "https://www.hkma.gov.hk/example", "reviewed": True,
        }
        with self.assertRaisesRegex(DataSourceError, "already has"):
            add_reviewed_source(self.root, "rates", row)
        row["Date"] = "20260106"
        row["reviewed"] = False
        with self.assertRaisesRegex(DataSourceError, "reviewed"):
            add_reviewed_source(self.root, "rates", row)
        row["reviewed"] = True
        saved = add_reviewed_source(self.root, "rates", row)
        self.assertEqual(saved["Date"], "20260106")
        with (self.root / "data/incoming/rates.csv").open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 2)

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_official_historical_rate_is_archived_after_success(self, fetch):
        fetch.side_effect = self.quotes
        (self.root / "data/incoming/rates.csv").unlink()
        self.historical_rates.return_value = [{
            "Date": "20260102", "Risk_Free_Rate": 2.1,
            "source": "https://api.hkma.gov.hk/public/example",
        }]
        summary = update_data(self.root, end="20260102")
        self.assertEqual(summary["new_rows"], 1)
        with (self.root / "data/incoming/rates.csv").open(newline="") as stream:
            rates = list(csv.DictReader(stream))
        self.assertEqual(rates[0]["Date"], "20260102")
        self.assertEqual(rates[0]["Risk_Free_Rate"], "2.1")

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_official_workbook_fills_api_gap(self, fetch):
        fetch.side_effect = self.quotes
        (self.root / "data/incoming/rates.csv").unlink()
        raw = self.root / "data/raw"
        raw.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "data/raw/hkma_T050303.xls"
        (raw / source.name).write_bytes(source.read_bytes())
        summary = update_data(self.root, end="20260102")
        self.assertEqual(summary["new_rows"], 1)
        self.workbook_rates.assert_not_called()
        with (self.root / "data/incoming/rates.csv").open(newline="") as stream:
            rate = next(csv.DictReader(stream))
        self.assertEqual(rate["Risk_Free_Rate"], "2.26")
        self.assertIn("T050303.xls", rate["source_url"])

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_market_archive_reused_for_covered_nontrading_start(self, fetch):
        fetch.side_effect = self.quotes
        archive_market_quotes(self.root, date(2026, 1, 1), date(2026, 1, 2))
        fetch.reset_mock()
        summary = update_data(self.root, end="20260102")
        self.assertEqual(summary["new_rows"], 1)
        fetch.assert_not_called()
        with (self.root / "data/incoming/market_quotes.csv").open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    @patch("dtd_service.pipeline.fetch_yahoo_daily_quotes")
    def test_market_archive_preserves_prior_coverage_when_extended(self, fetch):
        fetch.return_value = self.quotes(None, None)
        archive_market_quotes(self.root, date(2026, 1, 1), date(2026, 1, 2))
        fetch.return_value = [{
            "Date": "20260105", "close": 8.6,
            "source": "https://query1.finance.yahoo.com/test",
        }]
        archive_market_quotes(self.root, date(2026, 1, 3), date(2026, 1, 5))
        coverage = json.loads((self.root / "data/incoming/market_quotes.coverage.json").read_text())
        self.assertEqual(coverage["intervals"], [{"start": "20260101", "end": "20260105"}])
        fetch.reset_mock()
        (self.root / "data/incoming/rates.csv").write_text(
            "Date,Risk_Free_Rate,source_url\n"
            "20260102,2.1,https://hkma.example/20260102\n"
            "20260105,2.0,https://hkma.example/20260105\n",
            encoding="utf-8",
        )
        summary = update_data(self.root, end="20260105")
        self.assertEqual(summary["new_rows"], 2)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
