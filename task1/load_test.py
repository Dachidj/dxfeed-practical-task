"""
Step 4 - a polite load test of the dxFeed REST demo.

Stages with 1, 2, 4 and 8 parallel requests. Each stage lasts 60 s, then 60 s
rest. Afterwards one exact URL is requested 5 times in a row to look for caching.

Limits we stay under (documented: 10 simultaneous connections, 100 HTTP req/min):
  * never more than 8 requests in flight
  * at most 25 requests per minute -> 50 HTTP requests/min, because every
    request is answered with a 302 redirect and then the real response

Outputs:
  reports/load_results.csv   one row per request
  data/load_test.log         the request log

Usage:
    python load_test.py
    python load_test.py --lookback 90
"""

import argparse
import hashlib
import itertools
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from fetch import (PROJECT_DIR, TIMEOUT, StopFetching, build_url, check_clock,
                   now_utc, offset_from_response, setup_logging)

STAGES = [1, 2, 4, 8]
MAX_PARALLEL = 8
MAX_PER_MINUTE = 25
STAGE_SECONDS = 60
REST_SECONDS = 60
CACHE_REPEATS = 5
SYMBOLS = ["AAPL", "TSLA", "EUR/USD"]
CANDLE_ATTRS = "{=1m,price=bid}"            # 1-minute candles built from bid prices
CACHE_HEADERS = ["Age", "Cache-Control", "ETag", "Expires", "Last-Modified", "X-Cache"]

REPORT_DIR = PROJECT_DIR / "reports"
CSV_PATH = REPORT_DIR / "load_results.csv"
LOG_FILE = PROJECT_DIR / "data" / "load_test.log"

log = logging.getLogger("fetch")
_local = threading.local()                  # one requests.Session per thread


def session() -> requests.Session:
    """requests.Session is not guaranteed thread-safe, so each worker thread gets its own."""
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def aligned_window(lookback_min: float) -> tuple[datetime, datetime]:
    """[now - lookback, now], both rounded down to the whole minute.
    Requests sent within the same minute get the exact same URL."""
    end = now_utc().replace(second=0, microsecond=0)     # server-corrected clock
    return end - timedelta(minutes=lookback_min), end


# ---------------------------------------------------------------- one request
def timed_request(symbol: str, window, meta: dict) -> dict:
    """Send one request and record everything about it. Never raises."""
    url = build_url(symbol + CANDLE_ATTRS, *window)
    row = {**meta, "symbol": symbol, "url": url,
           "start_utc": now_utc().isoformat(timespec="milliseconds"),
           "duration_ms": None, "redirect_ms": None, "redirects": 0,
           "http_status": None, "body_status": None, "candles": None,
           "newest_candle_utc": None, "server_date": None, "clock_offset_s": None,
           "bytes": None, "body_sha256": None, "cache_headers": "", "error": ""}
    t0 = time.perf_counter()
    try:
        resp = session().get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:          # timeout, refused, reset ...
        row["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    row["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)  # both hops + body
    offset = offset_from_response(resp, datetime.now(timezone.utc))
    row["server_date"] = resp.headers.get("Date")
    row["clock_offset_s"] = round(offset, 1) if offset is not None else None
    row["redirects"] = len(resp.history)
    row["redirect_ms"] = round(sum(h.elapsed.total_seconds() for h in resp.history) * 1000, 1)
    row["http_status"] = resp.status_code
    row["bytes"] = len(resp.content)
    row["body_sha256"] = hashlib.sha256(resp.content).hexdigest()[:16]
    row["cache_headers"] = "; ".join(f"{h}={resp.headers[h]}"
                                     for h in CACHE_HEADERS if h in resp.headers)
    try:
        payload = resp.json()
        candles = [c for lst in payload.get("Candle", {}).values() for c in lst]
        row["body_status"] = payload.get("status")
        row["candles"] = len(candles)
        if candles:
            newest = max(c["time"] for c in candles)
            row["newest_candle_utc"] = (pd.to_datetime(newest, unit="ms", utc=True)
                                        .strftime("%Y-%m-%d %H:%M:%S"))
    except ValueError:
        row["body_status"] = "NOT_JSON"
    return row


def must_stop(row: dict) -> bool:
    """429 or connection-level error -> stop the whole test immediately."""
    return row["http_status"] == 429 or row["http_status"] is None


def log_row(row: dict) -> None:
    log.info("  %-6s %-9s %-7s HTTP %s  body=%s  %s candles  newest=%s  %.0f ms%s",
             row["phase"], f"x{row['parallel']}", row["symbol"], row["http_status"],
             row["body_status"], row["candles"], row["newest_candle_utc"],
             row["duration_ms"], f"  ERROR {row['error']}" if row["error"] else "")


# ---------------------------------------------------------------- stages
def run_stage(n: int, rotation, lookback: float, results: list) -> bool:
    """
    n parallel requests. Requests go out in WAVES of n at the same moment, so n
    really are in flight together. Waves are spaced so the stage sends at most
    25 requests:   n=1: 25 waves, 2.4 s apart     n=2: 12 waves, 5 s apart
                   n=4:  6 waves, 10 s apart      n=8:  3 waves, 20 s apart
    A wave only starts after the previous wave has finished, so there are
    never more than n requests open. Returns False if we must stop.
    """
    assert n <= MAX_PARALLEL
    waves = MAX_PER_MINUTE // n
    gap = STAGE_SECONDS / waves
    log.info("stage x%d: %d waves of %d, every %.1f s", n, waves, n, gap)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        for w in range(waves):
            delay = t0 + w * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            window = aligned_window(lookback)
            futures = [pool.submit(timed_request, next(rotation), window,
                                   {"phase": "stage", "parallel": n, "wave": w + 1})
                       for _ in range(n)]
            wait(futures)
            rows = [f.result() for f in futures]
            for r in rows:
                log_row(r)
            results.extend(rows)
            bad = [r for r in rows if must_stop(r)]
            if bad:
                log.error("STOPPING: %s %s", bad[0]["http_status"] or "", bad[0]["error"]
                          or "HTTP 429 Too Many Requests")
                return False
    rest = t0 + STAGE_SECONDS - time.monotonic()     # fill the stage to 60 s
    if rest > 0:
        time.sleep(rest)
    return True


def run_cache_test(lookback: float, results: list) -> bool:
    """The same URL 5 times in a row (2.4 s apart = 25/min pace)."""
    window = aligned_window(lookback)                 # fixed -> identical URL
    log.info("cache test: same AAPL request x%d", CACHE_REPEATS)
    for i in range(CACHE_REPEATS):
        if i:
            time.sleep(60 / MAX_PER_MINUTE)
        r = timed_request("AAPL", window, {"phase": "cache", "parallel": 1, "wave": i + 1})
        log_row(r)
        results.append(r)
        if must_stop(r):
            log.error("STOPPING: %s", r["error"] or "HTTP 429")
            return False
    return True


# ---------------------------------------------------------------- analysis
def summarise(df: pd.DataFrame) -> pd.DataFrame:
    """Latency (p50/p95/max) over successful requests only: HTTP 200 and body status OK.
    Server-side timeouts (body status TIMED_OUT) are counted separately."""
    rows = []
    for n, g in df[df.phase == "stage"].groupby("parallel"):
        ok = (g.http_status == 200) & (g.body_status == "OK")
        timed_out = g.body_status == "TIMED_OUT"
        lat = g.loc[ok, "duration_ms"]
        rows.append({"parallel": n, "requests": len(g),
                     "errors": int((~ok).sum()),
                     "error_rate_%": round(100 * (~ok).mean(), 1),
                     "p50_ms": lat.quantile(0.50), "p95_ms": lat.quantile(0.95),
                     "max_ms": lat.max(),
                     "timeouts": int(timed_out.sum()),
                     "timeout_median_ms": g.loc[timed_out, "duration_ms"].median()})
    return pd.DataFrame(rows).round(0)


def identical_report(g: pd.DataFrame) -> str:
    """Per symbol: are responses to the SAME URL byte-identical?
    (A different URL - the window moved to the next minute - may legitimately differ.)"""
    parts = []
    for sym, s in g.groupby("symbol"):
        per_url = s.dropna(subset=["body_sha256"]).groupby("url")["body_sha256"].nunique()
        same = (per_url == 1).all()
        parts.append(f"{sym}: {len(s)} resp, {len(per_url)} distinct URL(s), "
                     f"{'identical' if same else 'DIFFERENT'} per URL")
    return " | ".join(parts)


def report(results: list) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    df = pd.DataFrame(results)
    df.to_csv(CSV_PATH, index=False)

    stages = df[df.phase == "stage"]
    if not stages.empty:
        summary = summarise(df)
        print("\n================ per stage ================")
        print(summary.to_string(index=False))
        print("(p50/p95/max over successful requests only; ~16 per stage, so p95 is rough)")
        for n, g in stages.groupby("parallel"):
            print(f"  x{n}: {identical_report(g)}")

    cache = df[df.phase == "cache"]
    if not cache.empty:
        print("\n================ cache test (same URL x5) ================")
        print(cache[["wave", "start_utc", "duration_ms", "redirect_ms", "http_status",
                     "candles", "newest_candle_utc", "body_sha256",
                     "cache_headers"]].to_string(index=False))
        d = cache["duration_ms"]
        print(f"first {d.iloc[0]:.0f} ms vs. later median {d.iloc[1:].median():.0f} ms; "
              f"{cache.body_sha256.nunique()} distinct body/bodies; "
              f"cache headers: {'none' if not cache.cache_headers.any() else 'present'}")
    print(f"\nall rows -> {CSV_PATH}")


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="Polite load test of the dxFeed demo.")
    parser.add_argument("--lookback", type=float, default=30,
                        help="minutes of 1m candles per request (default 30)")
    args = parser.parse_args()
    setup_logging(LOG_FILE)
    log.info("load test: stages %s, <= %d req/min, lookback %g min",
             STAGES, MAX_PER_MINUTE, args.lookback)
    try:
        check_clock(session())                       # before any time is recorded
    except StopFetching as exc:
        log.error("STOPPING: %s", exc)
        return 1

    results, ok = [], True
    rotation = itertools.cycle(SYMBOLS)              # AAPL, TSLA, EUR/USD, AAPL, ...
    for n in STAGES:
        ok = run_stage(n, rotation, args.lookback, results)
        if not ok:
            break
        log.info("rest %d s", REST_SECONDS)
        time.sleep(REST_SECONDS)                     # also rest before the cache test
    if ok:
        ok = run_cache_test(args.lookback, results)

    report(results)                                  # even after a stop: save what we have
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
