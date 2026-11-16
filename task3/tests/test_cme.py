"""CME parser on the synthetic fixture."""
import unittest
from datetime import date, time
from pathlib import Path

from scrapers import cme

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cme" / "2030_synthetic_day.xlsx"


class TestCme(unittest.TestCase):
    def test_fixture_rows(self):
        rows = cme.parse_file(FIXTURE, scraped_at="2030-01-01T00:00:00Z")
        got = {(r["market"], r["date"], r["status"], r["close_time_et"]) for r in rows}
        self.assertEqual(got, {                     # crypto: only the daily break -> no row
            ("Globex: Equities", "2030-03-13", "trading_halt", "13:00"),
            ("Globex: Grains", "2030-03-13", "closed", ""),
            ("Globex: Dairy", "2030-03-13", "closed", ""),
        })
        self.assertTrue(all(r["holiday"] == "Synthetic Day" for r in rows))
        halt = next(r for r in rows if r["status"] == "trading_halt")
        self.assertIn("Halted 12:00 CT", halt["notes"])
        self.assertIn("2030_synthetic_day.xlsx", halt["notes"])

    def test_names_and_timezones(self):
        self.assertEqual(cme.holiday_name("good_friday"), "Good Friday")
        self.assertEqual(cme.to_et(date(2027, 3, 26), time(12, 0)), "13:00")   # daylight time
        self.assertEqual(cme.to_et(date(2026, 12, 24), time(12, 15)), "13:15")  # standard time


if __name__ == "__main__":
    unittest.main()
