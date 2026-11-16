# US exchange holiday schedules

Collects the trading holiday schedules of **NYSE, NASDAQ, CME and OPRA**, cleans them into one
table and publishes it to **Google Drive (CSV)** and **Google Sheets** with a single command:

```bash
python main.py
```

**Live output:** [Google Sheet](https://docs.google.com/spreadsheets/d/1qQaBioXRRAtLZPuHdLMtuBQ_LCEs-hyQA293yR4vG78/edit) ·
[CSV on Google Drive](https://drive.google.com/file/d/1rYXG7sqeePmRp8qauAIgT0XeTrMBGqYd/view) ·
[`output/holidays.csv`](output/holidays.csv) in this repo

## Results (last run)

| Source | Rows | Closed | Early close | Trading halt | How it is read |
|---|---:|---:|---:|---:|---|
| NYSE | 34 | 29 | 5 | – | Live: HTML table + footnotes (2026–2028) |
| NASDAQ | 12 | 10 | 2 | – | Live: HTML table (2026) |
| OPRA | 12 | 10 | 2 | – | **Proxy** for 2026, see Findings |
| CME Globex | 101 | 68 | – | 33 | Official files, downloaded manually; one row per asset class |
| **Total** | **159** | | | | Current year onward |

Every row has the same 9 columns:
`exchange, market, date, holiday, status, close_time_et, notes, source_url, scraped_at_utc`.
Dates are `YYYY-MM-DD`, times are New York time (ET). `status` is `closed`, `early_close` or
`trading_halt`.

## How it works

```text
nyse.py ──┐
nasdaq.py ┤                      ┌─> output/holidays.csv
opra.py ──┼─> normalize ─> validate ─┤
cme.py ───┘                      └─> Google Drive CSV + Google Sheet (upload.py)
```

1. Each scraper reads one source and returns rows in the shared format.
2. `validate.py` checks that every exchange has rows, and for weekend dates, duplicates and whether
   **NYSE and NASDAQ agree** (they
   list the same US equity holidays, so a mismatch means a parsing problem).
3. The CSV is written and uploaded, and the Sheet is read back to confirm the row count.
4. **If any scraper fails or validation finds a problem, nothing is overwritten**: the last good CSV,
   Drive file and Sheet are kept,
   and the run exits with code 1.

## Findings

- **OPRA has not published a 2026 holiday schedule.** Its site lists schedules up to 2025, and the
  scraper reports this as a warning. OPRA's 2026 rows are copied from NASDAQ's "U.S. Equity and
  Options Markets" calendar and marked `PROXY:` in `notes`.
- **CME blocks automated access** (HTTP 403) and its terms of use prohibit scraping. Instead of
  getting around the block, the project uses CME's official "Download data" files, saved by hand.
- **CME hours differ per asset class.** On Thanksgiving 2026, equity and interest-rate futures halt
  at 13:00 ET, energy and metals at 14:30 ET, and grains and livestock don't trade at all, so CME
  gets one row per asset class instead of a single "closed".
- **NYSE and NASDAQ agree on all 12 dates of 2026.**
- **NASDAQ uses bot protection.** Several requests in a short time got a temporary block page;
  the run then fails safely and keeps the last good data.

## Design decisions

- **Nothing hardcoded:** years, dates and early-close times are read from the sources.
- **No bypassing:** a plain HTTP client with a standard browser User-Agent; no headless browser
  and no getting around blocks. CME blocks scraping, so CME is not scraped.
- **Time zones:** CME publishes Chicago time; it is converted to New York time with real time
  zones (`zoneinfo`), not by adding one hour.
- **Google upload with a service account:** no browser login, so it can run unattended. It only
  updates files that already exist (a service account has no storage of its own). Values are
  written raw, so dates stay `2026-11-26` instead of being reformatted by Sheets.

## Run it

Requires Python 3.11+.

```bash
cd task3
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

python -m unittest discover tests   # offline tests: saved pages + fail-safe
python main.py --no-upload          # scrape and write output/holidays.csv
python main.py                      # ...and upload to Google Drive + Sheets
```

Without Google setup, the upload is skipped with a message. Without CME files, validation reports
`CME: no rows (missing manual files?)`, the run exits with code 1 and the last good CSV is kept.

**Google setup (once):**
1. In Google Cloud, enable the Google Drive API and Google Sheets API, create a service
   account and download its JSON key into `secrets/` (git-ignored, never commit it).
2. In Google Drive, create the target CSV file and Google Sheet and share both with the
   service account's email as Editor.
3. Copy `.env.example` to `.env` and fill in the key path and the two file IDs.

**CME files (once per holiday):**
1. Sign in to cmegroup.com (free account) and open **Holiday and Trading Hours**.
2. Pick a holiday, click **Download data**, and save the file as
   `data/cme_raw/<year>_<holiday>.xlsx`, e.g. `2027_good_friday.xlsx`. The holiday name comes
   from the filename. These files are CME's data and are git-ignored.

To run on a schedule, call `python main.py` from cron or a GitHub Actions workflow.

## Project structure

```text
main.py              runs everything: scrape, validate, write CSV, upload
validate.py          coverage, weekend, duplicate and NYSE/NASDAQ checks
upload.py            Google Drive + Sheets upload with read-back check
scrapers/
  base.py            HTTP download with retries; saves a copy of each page to snapshots/
  normalize.py       shared row format and text cleanup
  nyse.py, nasdaq.py, opra.py, cme.py   one file per source
tests/               offline tests; fixtures/ holds saved pages and a synthetic CME file
output/holidays.csv  latest result
```

## Limitations

- CME: only the holiday itself is covered per asset class. Early closes on the days before or
  after a holiday are not included.
- CME files must be downloaded by hand when new holidays are published.
- CME only offers files for upcoming holidays, so CME rows start from Nov 2026; the earlier 2026
  CME holidays had already passed and can no longer be downloaded.
- OPRA 2026 is a labelled proxy until OPRA publishes its own schedule.
- OPRA also announces unplanned closures (e.g. a national day of mourning) as separate notices;
  these are not covered.
- Drive is updated before the Sheet; if the Sheet update fails, the two can briefly differ until
  the next run.
