"""
Step 3 - watch the live 1m candles for a while and measure how they behave.

Every 60 seconds for 15 minutes it fetches the 1m candles of the last
30 minutes for AAPL and TSLA and saves each raw response to data/live/.
After the last fetch it compares consecutive responses and prints:

  * partial candles - candle times whose values changed between fetches
                      (the candle was still being built when we saw it)
  * delay           - how old the newest candle was at each fetch
  * freezes         - fetches where nothing new appeared at all

Request rules (timeout, 429, non-OK status) come from fetch.py, so both
scripts behave identically.

Usage:
    python watch_live.py                         # 15 minutes, every 60 s
    python watch_live.py --minutes 5 --interval 60
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from check_aggregation import FIELDS, NY_TZ, load_candles, values_differ
from fetch import (DELAY_SECONDS, PROJECT_DIR, StopFetching, check_clock,
                   get_candles, now_utc, setup_logging)

SYMBOLS = ["AAPL", "TSLA"]
TIMEFRAME = "{=1m}"
DEFAULT_LOOKBACK_MIN = 30                 # fromTime = now - 30 min
# The demo feed is ~15 min delayed, so a 30 min window holds ~15 candles.
# "now" comes from now_utc() (server-corrected), so a wrong local clock can no
# longer make us request data from the future.
CANDLE_COLUMNS = ["symbol", "candle_utc", "candle_ny", "times_seen", "times_changed",
                  "result", "fields_changed", "close_first", "close_last",
                  "volume_first", "volume_last", "count_first", "count_last"]
LIVE_DIR = PROJECT_DIR / "data" / "live"
REPORT_DIR = PROJECT_DIR / "reports"
LOG_FILE = LIVE_DIR / "watch_live.log"

log = logging.getLogger("fetch")          # same logger name -> same format/handlers


# ---------------------------------------------------------------- collecting
def fetch_and_save(session, symbol: str, lookback: timedelta) -> dict:
    """One request; save the raw body; return what the analysis needs."""
    fetched_at = now_utc()                    # server-corrected, not the raw local clock
    result = get_candles(session, symbol + TIMEFRAME,
                         fetched_at - lookback, fetched_at)
    stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    path = LIVE_DIR / f"{symbol}_1m_{stamp}.json"
    path.write_bytes(result["body"])
    log.info("  saved %s", path.relative_to(PROJECT_DIR))
    server_time = result["server_time"]
    return {"symbol": symbol, "fetched_at": pd.Timestamp(fetched_at), "path": path,
            # Delays are measured against the SERVER's Date header, so the
            # local clock cannot distort them.
            "server_time": pd.Timestamp(server_time) if server_time else None,
            "clock_offset_s": result["clock_offset_s"]}


def collect(minutes: float, interval: float,
            lookback: timedelta) -> tuple[list[dict], bool]:
    """
    Run the fetch rounds on a fixed schedule: round k starts at start + k*interval.
    Sleeping until a deadline (instead of 'sleep 60' after each round) means the
    time spent on requests does not push later rounds back.
    Returns (fetches, completed_ok).
    """
    rounds = max(1, int(minutes * 60 // interval))
    fetches = []
    with requests.Session() as session:
        try:
            check_clock(session)              # before any delay is measured
        except StopFetching as exc:
            log.error("STOPPING: %s", exc)
            return fetches, False
        start = time.monotonic()
        for k in range(rounds):
            wait = start + k * interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            log.info("round %d/%d", k + 1, rounds)
            for j, symbol in enumerate(SYMBOLS):
                if j > 0:
                    time.sleep(DELAY_SECONDS)       # same spacing as fetch.py
                try:
                    fetches.append(fetch_and_save(session, symbol, lookback))
                except StopFetching as exc:
                    log.error("STOPPING: %s", exc)
                    return fetches, False
    return fetches, True


# ---------------------------------------------------------------- analysis
def ts(t: pd.Timestamp) -> str:
    return (f"{t.tz_convert('UTC').strftime('%H:%M:%S')} UTC / "
            f"{t.tz_convert(NY_TZ).strftime('%H:%M:%S %Z')}")


def analyse(symbol: str, fetches: list[dict]):
    """Compare each fetch with the previous one for this symbol."""
    snaps = [(f["fetched_at"], load_candles(f["path"])) for f in fetches]
    servers = [(f["server_time"], f["clock_offset_s"]) for f in fetches]

    fetch_rows = []                  # one row per fetch -> delay + freeze
    history = {}                     # candle time -> list of (fetch time, values)
    changes = {}                     # candle time -> list of changed field names
    prev = None
    for i, (fetched_at, df) in enumerate(snaps, start=1):
        for t, row in df.iterrows():
            history.setdefault(t, []).append((fetched_at, row))

        newest = df.index.max() if not df.empty else pd.NaT
        new_times, changed = [], []
        if prev is not None:
            new_times = [t for t in df.index if t > prev.index.max()] \
                if not prev.empty else list(df.index)
            # Candles present in both responses: did any field change?
            common = df.index.intersection(prev.index)
            for t in common:
                diff = [f for f in FIELDS
                        if values_differ(pd.Series([df.at[t, f]]),
                                         pd.Series([prev.at[t, f]])).iloc[0]]
                if diff:
                    changed.append(t)
                    changes.setdefault(t, []).append(",".join(diff))

        if prev is None:
            state = "first fetch"
        elif new_times:
            state = "new candle"
        elif changed:
            state = "updates only"
        else:
            state = "FREEZE"            # no new candle and no value changed

        server_time, clock_offset = servers[i - 1]
        reference = server_time if server_time is not None else fetched_at
        fetch_rows.append({
            "symbol": symbol, "fetch": i,
            "server_date_utc": reference.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "clock_offset_s": round(clock_offset, 1) if clock_offset is not None else None,
            "fetched_at_utc": fetched_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "fetched_at_ny": fetched_at.tz_convert(NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "candles": len(df),
            "newest_candle_utc": newest.strftime("%H:%M UTC") if pd.notna(newest) else "",
            "newest_candle_ny": newest.tz_convert(NY_TZ).strftime("%H:%M %Z") if pd.notna(newest) else "",
            # Age of the newest candle's START, by the server's Date header
            # (+-1 s). A live 1m candle is 0-60 s old; well above 60 s means
            # the feed is behind real time.
            "newest_age_s": round((reference - newest).total_seconds()) if pd.notna(newest) else None,
            "new_candles": len(new_times),
            "changed_candles": len(changed),
            "state": state,
        })
        prev = df

    candle_rows = []                 # one row per candle time -> partial candles
    for t in sorted(history):
        seen = history[t]
        first, last = seen[0][1], seen[-1][1]
        n_changes = len(changes.get(t, []))
        candle_rows.append({
            "symbol": symbol,
            "candle_utc": t.strftime("%Y-%m-%d %H:%M UTC"),
            "candle_ny": t.tz_convert(NY_TZ).strftime("%H:%M %Z"),
            "times_seen": len(seen),
            "times_changed": n_changes,
            "result": ("seen once" if len(seen) == 1 else
                       "CHANGED (partial)" if n_changes else "stable"),
            "fields_changed": ";".join(sorted(set(
                f for c in changes.get(t, []) for f in c.split(",")))),
            "close_first": first["close"], "close_last": last["close"],
            "volume_first": first["volume"], "volume_last": last["volume"],
            "count_first": first["count"], "count_last": last["count"],
        })
    # explicit columns so an all-empty run still prints a (blank) table
    return pd.DataFrame(fetch_rows), pd.DataFrame(candle_rows, columns=CANDLE_COLUMNS)


def report(fetches: list[dict]) -> None:
    all_fetch, all_candle = [], []
    for symbol in SYMBOLS:
        mine = sorted((f for f in fetches if f["symbol"] == symbol),
                      key=lambda f: f["fetched_at"])
        if not mine:
            continue
        per_fetch, per_candle = analyse(symbol, mine)
        all_fetch.append(per_fetch)
        all_candle.append(per_candle)

        print(f"\n================ {symbol} ================")
        print("\n-- Partial candles (values changed between fetches) --")
        print(per_candle[["candle_utc", "candle_ny", "times_seen", "times_changed",
                          "result", "fields_changed", "close_first", "close_last",
                          "volume_first", "volume_last"]].to_string(index=False))
        print("\n-- Delay (age of newest candle at each fetch) --")
        print(per_fetch[["fetch", "server_date_utc", "clock_offset_s", "fetched_at_ny", "candles",
                         "newest_candle_ny", "newest_age_s", "new_candles",
                         "changed_candles", "state"]].to_string(index=False))
        ages = per_fetch["newest_age_s"].dropna()
        freezes = per_fetch[per_fetch["state"] == "FREEZE"]
        if ages.empty:
            print("\nnewest-candle age: no candles in any fetch "
                  "(window shorter than the feed delay?)")
        else:
            print(f"\nnewest-candle age: min {ages.min():.0f}s ({ages.min()/60:.1f} min), "
                  f"median {ages.median():.0f}s, max {ages.max():.0f}s "
                  f"({ages.max()/60:.1f} min)")
        print("-- Freezes --")
        print("none" if freezes.empty else
              "\n".join(f"fetch {r.fetch} at {r.fetched_at_ny}: nothing new"
                        for r in freezes.itertuples()))

    REPORT_DIR.mkdir(exist_ok=True)
    pd.concat(all_fetch).to_csv(REPORT_DIR / "live_fetches.csv", index=False)
    pd.concat(all_candle).to_csv(REPORT_DIR / "live_candles.csv", index=False)
    print(f"\nCSV copies: {REPORT_DIR / 'live_fetches.csv'}, "
          f"{REPORT_DIR / 'live_candles.csv'}")


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="Watch live dxFeed 1m candles.")
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--interval", type=float, default=60, help="seconds")
    parser.add_argument("--lookback", type=float, default=DEFAULT_LOOKBACK_MIN,
                        help="minutes of history per request (fromTime = now - lookback)")
    args = parser.parse_args()

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(LOG_FILE)
    log.info("watching %s for %g min, every %g s, lookback %g min",
             SYMBOLS, args.minutes, args.interval, args.lookback)

    fetches, ok = collect(args.minutes, args.interval,
                          timedelta(minutes=args.lookback))
    if fetches:
        report(fetches)               # analyse whatever we got, even after a stop
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
