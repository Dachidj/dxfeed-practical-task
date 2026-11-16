"""NYSE, NASDAQ and OPRA parsers on saved pages in tests/fixtures/ (offline, no network)."""
import unittest
from pathlib import Path

from scrapers import nasdaq, nyse, opra

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParsers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nyse = nyse.parse(read("nyse.html"))
        cls.nasdaq = nasdaq.parse(read("nasdaq.html"))

    def test_row_counts(self):
        self.assertEqual(len(self.nyse), 34)
        self.assertEqual(len(self.nasdaq), 12)
        pdf = (FIXTURES / "opra.pdf").read_bytes()
        self.assertEqual(len(opra.parse_pdf(pdf, "fixture")), 14)

    def test_nyse_and_nasdaq_agree_on_2026(self):
        def days(rows):
            return {(r["date"], r["status"], r["close_time_et"]) for r in rows if r["date"].startswith("2026")}
        self.assertEqual(days(self.nyse), days(self.nasdaq))

    def test_newest_opra_link_is_2025(self):
        year, url = opra.newest_schedule(read("opra_index.html"))
        self.assertEqual(year, 2025)
        self.assertIn("2025", url)


if __name__ == "__main__":
    unittest.main()
