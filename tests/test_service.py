import unittest

from pathlib import Path
import tempfile

from run_service import _freshness, _source_cutoffs, _update_action, _update_window


class ServiceTests(unittest.TestCase):
    def test_health_reports_stale_data_cutoff(self):
        status, age = _freshness([{"Date": "20251231"}])
        self.assertEqual(status, "stale")
        self.assertGreater(age, 7)
        self.assertEqual(_freshness([]), ("missing", None))
        self.assertEqual(_freshness([{"Date": "invalid"}]), ("invalid", None))

    def test_update_window_is_append_only_and_uses_latest_date(self):
        window = _update_window([
            {"Date": "20260901"},
            {"Date": "20260831"},
        ])
        self.assertEqual(window["existing_input_cutoff"], "20260901")
        self.assertEqual(window["next_allowed_start"], "20260902")
        self.assertEqual(window["latest_calendar_request_end"], window["latest_allowed_end"])

    def test_update_window_for_empty_results(self):
        window = _update_window([])
        self.assertIsNone(window["existing_input_cutoff"])
        self.assertIsNone(window["next_allowed_start"])
        self.assertTrue(window["latest_calendar_request_end"])

    def test_health_source_cutoffs_read_archived_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            incoming = root / "data" / "incoming"
            incoming.mkdir(parents=True)
            (incoming / "rates.csv").write_text(
                "Date,Risk_Free_Rate,source_url\n20260831,2.1,https://hkma.example\n",
                encoding="utf-8",
            )
            (incoming / "market_quotes.csv").write_text(
                "Date,close,source_url\n20260930,8.5,https://yahoo.example\n",
                encoding="utf-8",
            )
            self.assertEqual(_source_cutoffs(root), {
                "market_source_cutoff": "20260930",
                "rate_source_cutoff": "20260831",
            })

    def test_update_action_explains_the_two_common_rejections(self):
        self.assertIn("next allowed date", _update_action(
            "start must be the day after existing input cutoff: 20260901"
        ))
        self.assertIn("exact-date HKMA", _update_action(
            "missing exact-date HKMA 12M rates for 22 trading dates"
        ))


if __name__ == "__main__":
    unittest.main()
