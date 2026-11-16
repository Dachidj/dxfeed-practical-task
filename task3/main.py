"""Run enabled exchange scrapers, validate, write output/holidays.csv, upload to Drive + Sheets.

Nothing is written or uploaded unless every scraper succeeded and validation found no problems.

Usage: python main.py [--no-upload]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from scrapers import cme, nasdaq, nyse, opra
from scrapers.base import min_relevant_year
from scrapers.normalize import SCHEMA
from upload import upload
from validate import validate

# name -> scrape callable. CME reads manually downloaded files (see README), no network.
SCRAPERS = {
    "NYSE": nyse.scrape,
    "NASDAQ": nasdaq.scrape,
    "OPRA": opra.scrape,
    "CME": cme.scrape,
}

OUTPUT = Path(__file__).resolve().parent / "output" / "holidays.csv"
log = logging.getLogger("main")


class _WarningCollector(logging.Handler):
    """Keeps WARNING+ messages emitted while scraping so they reappear in the final summary."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"[{record.name}] {record.getMessage()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape US exchange holidays, write CSV, upload.")
    parser.add_argument("--no-upload", action="store_true", help="skip the Google Drive / Sheets upload")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    collector = _WarningCollector()
    logging.getLogger().addHandler(collector)

    rows: list[dict] = []
    failed = []
    for name, scrape in SCRAPERS.items():
        try:
            got = scrape()
            log.info("%s: %d rows", name, len(got))
            rows.extend(got)
        except Exception:
            log.exception("%s scraper failed", name)
            failed.append(name)
    logging.getLogger().removeHandler(collector)

    df = pd.DataFrame(rows, columns=SCHEMA)
    cutoff = min_relevant_year()
    old = df["date"].str[:4].astype(int) < cutoff
    log.info("Dropped %d row(s) dated before %d", int(old.sum()), cutoff)
    df = df[~old].sort_values(["exchange", "date"], kind="stable").reset_index(drop=True)
    problems, report = validate(df, list(SCRAPERS))  # before writing anything

    print("\n=== Summary: rows per exchange / status / year ===")
    if not df.empty:
        summary = (df.assign(year=df["date"].str[:4])
                     .pivot_table(index=["exchange", "status"], columns="year",
                                  values="date", aggfunc="count", fill_value=0))
        with pd.option_context("display.width", 250, "display.max_columns", 50):
            print(summary.to_string())
    print("\n=== Cross-check ===")
    print("\n".join(report) or "(nothing to compare)")
    print(f"\n=== Scraper warnings: {len(collector.messages)} ===")
    for m in collector.messages:
        print(f"  - {m}")
    print(f"\n=== Validation: {len(problems)} problem(s) ===")
    for p in problems:
        print(f"  - {p}")
    if failed or problems:  # a bad or partial result never replaces the last good data
        if failed:
            print(f"\nFAILED SCRAPERS: {', '.join(failed)}")
        if problems:
            print(f"\nVALIDATION FAILED: {len(problems)} problem(s), listed above")
        log.warning("keeping last good output/holidays.csv")
        log.warning("upload skipped: keeping last good data in Drive")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT, index=False, encoding="utf-8")
    log.info("Wrote %d rows -> %s", len(df), OUTPUT)
    if args.no_upload:
        log.info("upload skipped (--no-upload)")
        return 0
    try:
        return 0 if upload(OUTPUT) else 1
    except Exception:
        log.exception("upload failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
