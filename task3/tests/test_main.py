"""main.py fail-safe: a bad run exits 1 and leaves the last good CSV untouched."""
import contextlib
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import main
from scrapers.base import min_relevant_year

_dec1 = date(min_relevant_year(), 12, 1)
MONDAY = _dec1 + timedelta(days=(7 - _dec1.weekday()) % 7)
SATURDAY = MONDAY + timedelta(days=5)
LAST_GOOD = "exchange,date\nNYSE,last-good-run\n"


def row(exchange: str, day: date) -> dict:
    return {"exchange": exchange, "market": "Test market", "date": day.isoformat(),
            "holiday": "Test Day", "status": "closed", "close_time_et": "", "notes": "",
            "source_url": "https://example.test", "scraped_at_utc": "2026-01-01T00:00:00Z"}


def scrapers(**override) -> dict:
    """One good weekday row per exchange; `override` replaces an exchange's rows."""
    rows = {ex: [row(ex, MONDAY)] for ex in main.SCRAPERS} | override
    return {ex: (lambda r=r: r) for ex, r in rows.items()}


class TestMainFailSafe(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "holidays.csv"
        self.output.write_text(LAST_GOOD, encoding="utf-8")

    def run_main(self, fakes: dict) -> tuple[int, mock.Mock, list[str]]:
        with mock.patch.dict(main.SCRAPERS, fakes), \
             mock.patch.object(main, "OUTPUT", self.output), \
             mock.patch.object(main, "upload") as upload, \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertLogs("main", "WARNING") as logs:
            code = main.main([])
        return code, upload, logs.output

    def assert_kept_last_good(self, code, upload, logs):
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_text(encoding="utf-8"), LAST_GOOD)
        upload.assert_not_called()
        self.assertTrue(any("keeping last good output/holidays.csv" in m for m in logs))

    def test_source_with_zero_rows(self):
        self.assert_kept_last_good(*self.run_main(scrapers(CME=[])))

    def test_row_on_weekend(self):
        weekend = [row("OPRA", MONDAY), row("OPRA", SATURDAY)]
        self.assert_kept_last_good(*self.run_main(scrapers(OPRA=weekend)))


if __name__ == "__main__":
    unittest.main()
