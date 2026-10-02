import csv
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_financials import convert_row  # noqa: E402


class FinancialConversionTests(unittest.TestCase):
    def test_disclosed_lines_reconcile_to_prepared_financials(self):
        incoming = ROOT / "data" / "incoming"
        with (incoming / "financial_conversion.csv").open(newline="", encoding="utf-8") as stream:
            source = list(csv.DictReader(stream))
        with (incoming / "financials.csv").open(newline="", encoding="utf-8") as stream:
            prepared = {row["effective_date"]: row for row in csv.DictReader(stream)}
        self.assertEqual([row["effective_date"] for row in source], ["20260331", "20260826"])
        for row in source:
            with self.subTest(date=row["effective_date"]):
                self.assertEqual(convert_row(row), prepared[row["effective_date"]])
                self.assertLess(
                    Decimal(prepared[row["effective_date"]]["BS_LT_BORROW(HKD)"]),
                    Decimal(prepared[row["effective_date"]]["BS_TOT_LIAB2(HKD)"]),
                )


if __name__ == "__main__":
    unittest.main()
