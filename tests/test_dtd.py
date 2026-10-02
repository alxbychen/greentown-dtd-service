import csv
import sys
import unittest
from pathlib import Path
from typing import Union, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dtd_service.core import compute_dtd_row, merge_daily_inputs, solve_asset_value


class DtdTests(unittest.TestCase):
    def test_solver_reprices_equity(self):
        av, dtd, iterations, residual = solve_asset_value(100.0, 120.0, 0.03, 0.30)
        self.assertGreater(av, 100.0)
        self.assertLess(abs(residual), 1e-6)
        self.assertGreater(iterations, 0)
        self.assertTrue(abs(dtd) < 10.0)

    def test_first_supplied_row_matches_cri(self):
        root = Path(__file__).resolve().parents[1]
        with (root / "historical_inputs.csv").open(newline="") as stream:
            input_row = next(csv.DictReader(stream))
        with (root / "parameters.csv").open(newline="") as stream:
            params = list(csv.DictReader(stream))
        with (root / "full_historical_dtd_output.csv").open(newline="") as stream:
            cri = next(csv.DictReader(stream))
        output = compute_dtd_row(input_row, params, cri)
        self.assertEqual(output["status"], "ok")
        self.assertAlmostEqual(
            float(cast(Union[float, str], output["dtd"])),
            float(cri["DTD_VALUE"]),
            places=5,
        )

    def test_carry_forward_is_explicit(self):
        row = {
            "Comp_no": "23286", "Date": "20990101", "CUR_MKT_CAP(HKD)": "100",
            "BS_CUR_LIAB(HKD)": "200", "BS_LT_BORROW(HKD)": "50", "BS_TOT_LIAB2(HKD)": "300",
            "BS_TOT_ASSET(HKD)": "400", "Risk_Free_Rate": "2.0",
        }
        params = [{"COMPANY_ID": "23286", "REAL_DTD_DATE": "20980101", "DTD_SIGMA": "0.3", "DTD_WEIGHT": "0.5"}]
        output = compute_dtd_row(row, params)
        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["parameter_source"], "carried_forward")
        self.assertIn("parameter_carried_forward", cast(str, output["flags"]))

    def test_merge_does_not_overwrite_history(self):
        old = [{"Comp_no": "23286", "Date": "20240102", "CUR_MKT_CAP(HKD)": "1"}]
        new = [{"Comp_no": "23286", "Date": "20240103", "CUR_MKT_CAP(HKD)": "2"}]
        self.assertEqual([r["Date"] for r in merge_daily_inputs(old, new)], ["20240102", "20240103"])
        with self.assertRaises(ValueError):
            merge_daily_inputs(old, [{"Comp_no": "23286", "Date": "20240102", "CUR_MKT_CAP(HKD)": "999"}])
        with self.assertRaisesRegex(ValueError, "duplicate existing"):
            merge_daily_inputs(old + old, new)
        with self.assertRaisesRegex(ValueError, "duplicate incoming"):
            merge_daily_inputs(old, new + new)

    def test_invalid_date_and_wrong_company_fail(self):
        row = {
            "Comp_no": "23286", "Date": "20240230", "CUR_MKT_CAP(HKD)": "100",
            "BS_CUR_LIAB(HKD)": "200", "BS_LT_BORROW(HKD)": "50",
            "BS_TOT_LIAB2(HKD)": "300", "Risk_Free_Rate": "2",
        }
        params = [{"COMPANY_ID": "99999", "REAL_DTD_DATE": "20240229", "DTD_SIGMA": "0.3", "DTD_WEIGHT": "0.5"}]
        self.assertIn("calendar date", cast(str, compute_dtd_row(row, params)["error"]))
        row["Date"] = "20240229"
        self.assertIn("no parameter record", cast(str, compute_dtd_row(row, params)["error"]))

    def test_missing_parameter_inside_coverage_is_not_carried(self):
        row = {
            "Comp_no": "23286", "Date": "20240103", "CUR_MKT_CAP(HKD)": "100",
            "BS_CUR_LIAB(HKD)": "200", "BS_LT_BORROW(HKD)": "50",
            "BS_TOT_LIAB2(HKD)": "300", "Risk_Free_Rate": "2",
        }
        params = [
            {"COMPANY_ID": "23286", "REAL_DTD_DATE": day, "DTD_SIGMA": "0.3", "DTD_WEIGHT": "0.5"}
            for day in ("20240102", "20240104")
        ]
        result = compute_dtd_row(row, params)
        self.assertEqual(result["status"], "failed")
        self.assertIn("no parameter record", cast(str, result["error"]))


if __name__ == "__main__":
    unittest.main()
