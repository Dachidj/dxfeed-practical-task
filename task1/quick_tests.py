"""
Part 1 extra - three quick tests.

(a) AAPL{=1h,a=s} vs AAPL{=1h} for NY trading days Nov 11-12:
    do session-aligned candles start at 09:30 New York?
(b) the single TSLA 1m->5m mismatch (12:35 UTC, Nov 12): full report row
    plus the five 1m candles behind it (offline, from existing files)
(c) TSLA{=1m,price=bid}, last 30 minutes, timeout=10 s:
    data, TIMED_OUT, or a client timeout - and after how long?

Same rules as everywhere: clock check first, 7 s between requests, raw bodies
saved to data/raw/quick/, stop on 429 / non-OK.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from check_aggregation import NY_TZ, REPORT_DIR, load_candles, newest_file
from fetch import (DELAY_SECONDS, PROJECT_DIR, StopFetching, check_clock,
                   get_candles, log, now_utc, setup_logging)

OUT_DIR = PROJECT_DIR / "data" / "raw" / "quick"
LOG_FILE = PROJECT_DIR / "data" / "quick_tests.log"


def raw_path(label: str):
    return OUT_DIR / f"{label}_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json"


def ny(ts) -> str:
    return ts.tz_convert(NY_TZ).strftime("%a %m-%d %H:%M %Z")


# ---------------------------------------------------------------- (a)
def test_a(session) -> None:
    # NY trading days Nov 11 and Nov 12 = 05:00 UTC Nov 11 .. 05:00 UTC Nov 13 (EST)
    start = datetime(2026, 11, 11, 5, tzinfo=timezone.utc)
    end = datetime(2026, 11, 13, 5, tzinfo=timezone.utc)
    frames = {}
    for i, (label, sym) in enumerate([("AAPL_1h_session", "AAPL{=1h,a=s}"),
                                      ("AAPL_1h_default", "AAPL{=1h}")]):
        if i:
            time.sleep(DELAY_SECONDS)
        p = raw_path(label)
        get_candles(session, sym, start, end, raw_path=p)
        frames[label] = load_candles(p)

    print("\n================ (a) AAPL{=1h,a=s} vs AAPL{=1h} ================")
    for label, df in frames.items():
        ny_idx = df.index.tz_convert(NY_TZ)
        for day in ["2026-11-11", "2026-11-12"]:
            times = [t.strftime("%H:%M") for t in ny_idx[ny_idx.strftime("%Y-%m-%d") == day]]
            print(f"  {label:16s} {day} ({len(times)} candles, NY): {' '.join(times)}")
    s, d = frames["AAPL_1h_session"], frames["AAPL_1h_default"]
    both = s.index.intersection(d.index)
    print(f"  same timestamps in both: {len(both)};  only session-aligned: "
          f"{len(s.index.difference(d.index))};  only default: {len(d.index.difference(s.index))}")
    # Cross-check: does each session candle equal the 1m candles of the next
    # 60 min (or 30 min for the last one, 15:30-16:00)? Only where 1m data exists.
    m_path = newest_file("AAPL", "1m")
    m = load_candles(m_path) if m_path else None
    for t, r in s.iterrows():
        if m is None or t + pd.Timedelta(minutes=30) > m.index.max():
            continue
        verdicts = []
        for span in (60, 30):
            mm = m[(m.index >= t) & (m.index < t + pd.Timedelta(minutes=span))]
            same = (len(mm) and mm.open.iloc[0] == r.open and mm.high.max() == r.high
                    and mm.low.min() == r.low and mm.close.iloc[-1] == r.close
                    and abs(mm.volume.sum() - r.volume) < 1e-6 * r.volume)
            verdicts.append(f"{span}min={'MATCH' if same else 'no'}")
        print(f"  session candle {ny(t)}: rebuilt from 1m -> {', '.join(verdicts)}")
    for day in ["2026-11-11", "2026-11-12"]:
        last = s[s.index.tz_convert(NY_TZ).strftime("%Y-%m-%d") == day].index.max()
        print(f"  last session candle on {day}: {ny(last)}")


# ---------------------------------------------------------------- (b)
def test_b() -> None:
    print("\n================ (b) TSLA 1m->5m mismatch at 12:35 UTC Nov 12 ================")
    rep = pd.read_csv(REPORT_DIR / "aggregation_report.csv")
    row = rep[(rep.symbol == "TSLA") & (rep.check == "1m->5m")]
    with pd.option_context("display.max_rows", 100, "display.float_format", "{:.6f}".format):
        print(row.T.to_string(header=False))
    m = load_candles(newest_file("TSLA", "1m"))
    f = load_candles(newest_file("TSLA", "5m"))
    t0 = pd.Timestamp("2026-11-12 12:35", tz="UTC")
    print("\n  the 1m candles inside 12:35-12:40 UTC:")
    print(m[(m.index >= t0) & (m.index < t0 + pd.Timedelta(minutes=5))].to_string())
    print("\n  the returned 5m candle:")
    print(f.loc[[t0]].to_string())


# ---------------------------------------------------------------- (c)
SERVER_TIMEOUT_S = 10      # &timeout=10 in the URL: how long the SERVER may work (default 3 s)
CLIENT_TIMEOUT_S = 20      # how long WE wait; must exceed the server limit


def test_c(session) -> None:
    print(f"\n================ (c) TSLA{{=1m,price=bid}}, last 30 min, server timeout="
          f"{SERVER_TIMEOUT_S} s, client timeout={CLIENT_TIMEOUT_S} s ================")
    end = now_utc().replace(second=0, microsecond=0)
    t0 = time.monotonic()
    try:
        r = get_candles(session, "TSLA{=1m,price=bid}", end - timedelta(minutes=30), end,
                        timeout=CLIENT_TIMEOUT_S, raw_path=raw_path("TSLA_1m_bid_srv10"),
                        extra_params={"timeout": SERVER_TIMEOUT_S})
        print(f"  status OK, {r['candles']} candles, after {r['elapsed_s']:.2f} s")
    except StopFetching as exc:
        # TIMED_OUT body or a client-side timeout: record it; this is the last request
        print(f"  NOT OK after {time.monotonic() - t0:.2f} s: {exc}")
        log.error("STOPPING: %s", exc)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["a", "b", "c"], help="run a single test")
    only = parser.parse_args().only
    setup_logging(LOG_FILE)
    with requests.Session() as session:
        try:
            check_clock(session)
            if only in (None, "a"):
                test_a(session)
            if only in (None, "b"):
                test_b()
            if only is None:
                time.sleep(DELAY_SECONDS)
            if only in (None, "c"):
                test_c(session)
        except StopFetching as exc:
            log.error("STOPPING: %s", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
