# Task 1 — dxFeed REST demo assessment

Analysis of dxFeed's public REST demo (`tools.dxfeed.com/webservice/rest/events.json`).
Full write-up: [REPORT.md](REPORT.md) · summary: [reports/part1_summary.md](reports/part1_summary.md)

## Setup
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt

## Scripts
| Script | What it does |
|---|---|
| `fetch.py` | Downloads 1m/5m/1h candles for AAPL, TSLA, EUR/USD and saves the raw JSON to `data/raw/`. |
| `check_aggregation.py` | Rebuilds 5m from 1m and 1h from 5m and lists every interval that differs (`reports/aggregation_*.csv`). |
| `watch_live.py` | Every 60 s for 15 min fetches AAPL/TSLA 1m candles; reports partial candles, delay and freezes. |
| `load_test.py` | Polite load test with 1, 2, 4 and 8 parallel requests plus a caching check (`reports/load_results.csv`). |
| `weekend_check.py` | Offline: candles around the weekend gap (`reports/weekend_edges.csv`). |
| `daily_check.py` | Fetches 1d + 1h candles and tests where a dxFeed "day" starts (`reports/daily_*.csv`). |
| `quick_tests.py` | Session-aligned candles, the single TSLA 1m→5m mismatch, and the TSLA bid timeout. |

    python fetch.py                                  # last 7 days (UTC)
    python fetch.py --from 2026-11-01 --to 2026-11-08
    python check_aggregation.py

`data/` holds the raw responses and request logs, `reports/` the results used in the report.

**Request limits:** all tests stayed at ≤25 requests/min and ≤8 parallel requests (the demo
documents 10 simultaneous connections and 100 HTTP requests/min; each request is a 302 redirect
plus the real response, so 25 requests = 50 HTTP requests).

## Notes
- The endpoint 302-redirects to `demo.dxfeed.com`.
- EUR/USD returns no candles by default (FX has no exchange trades); with `price=bid` it works — fetch.py adds this automatically.
- A JSON `status` other than `OK` (e.g. `TIMED_OUT`) is treated as a failure even with HTTP 200.
- Every script first compares the local clock with the server's HTTP `Date` header
  (`check_clock` in fetch.py), logs the difference, and then uses the server's time
  (`now_utc()`) for request windows, file names and delay measurements.
- Clock incident, 2026-11-12: the laptop clock ran ~42 min ahead during the first
  runs. The real feed delay is ~15.4 min, measured against server time (a 15-minute delayed feed). An early
  measurement of ~58 min was 16 min delay + 42 min clock error, not a feed delay,
  and the first empty watch_live test came from requesting a window in the future.
  The fetch-time stamps in the `data/raw/` file names from that run are ~42 min late;
  the candle data itself (server timestamps) is unaffected.
