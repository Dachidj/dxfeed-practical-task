"""
Part 1 extra - daily candles and where a dxFeed "day" starts.

Fetches (last 10 days, server clock):
    AAPL{=1d}  TSLA{=1d}  EUR/USD{=1d,price=bid}      and the matching 1h candles
into data/raw/daily/, then for every daily candle:
  * shows its timestamp in UTC and New York time
  * tests which day boundary reproduces it from 1h candles:
        midnight UTC | midnight New York | 17:00 New York (previous day)
    by rebuilding open/high/low/close/volume for each boundary and counting matches
  * checks whether the daily close equals the close of the last 1h candle of that day

Usage:
    python daily_check.py              # fetch, then analyse
    python daily_check.py --no-fetch   # analyse the newest files already saved
"""

import argparse
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd
import requests

from check_aggregation import FIELDS, NY_TZ, load_candles, values_differ
from fetch import (DELAY_SECONDS, PROJECT_DIR, StopFetching, candle_symbol_for,
                   check_clock, get_candles, log, now_utc, setup_logging)

SYMBOLS = ["AAPL", "TSLA", "EUR/USD"]
TIMEFRAMES = ["{=1d}", "{=1h}"]
DAYS = 10
OUT_DIR = PROJECT_DIR / "data" / "raw" / "daily"
REPORT_DIR = PROJECT_DIR / "reports"
LOG_FILE = PROJECT_DIR / "data" / "daily_check.log"

# Each candidate turns a calendar date D into the interval [start, end) of that "day".
CANDIDATES = {
    "midnight UTC":       lambda d: (pd.Timestamp(d, tz="UTC"),
                                     pd.Timestamp(d, tz="UTC") + pd.Timedelta(days=1)),
    "midnight New York":  lambda d: (pd.Timestamp(d, tz=NY_TZ),
                                     pd.Timestamp(d + pd.Timedelta(days=1), tz=NY_TZ)),
    "17:00 New York":     lambda d: (pd.Timestamp(d - pd.Timedelta(days=1), tz=NY_TZ) + pd.Timedelta(hours=17),
                                     pd.Timestamp(d, tz=NY_TZ) + pd.Timedelta(hours=17)),
}


def path_for(symbol: str, tf: str, stamp: str):
    return OUT_DIR / f"{symbol.replace('/', '-')}_{tf.strip('{}=')}_{stamp}.json"


def fetch_all() -> bool:
    jobs = [(s, tf) for s in SYMBOLS for tf in TIMEFRAMES]
    with requests.Session() as session:
        try:
            check_clock(session)
            end = now_utc().replace(microsecond=0)
            start = end - timedelta(days=DAYS)
            for i, (symbol, tf) in enumerate(jobs):
                if i:
                    time.sleep(DELAY_SECONDS)
                stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
                get_candles(session, candle_symbol_for(symbol, tf), start, end,
                            raw_path=path_for(symbol, tf, stamp))
        except StopFetching as exc:
            log.error("STOPPING: %s", exc)
            return False
    return True


def newest(symbol: str, tf: str) -> pd.DataFrame:
    files = sorted(OUT_DIR.glob(f"{symbol.replace('/', '-')}_{tf}_*.json"))
    return load_candles(files[-1]) if files else pd.DataFrame(columns=FIELDS)


def rebuild_day(hours: pd.DataFrame, start, end) -> pd.Series | None:
    h = hours[(hours.index >= start) & (hours.index < end)]
    if h.empty:
        return None
    return pd.Series({"open": h.open.iloc[0], "high": h.high.max(), "low": h.low.min(),
                      "close": h.close.iloc[-1], "volume": h.volume.sum(),
                      "count": h["count"].sum(), "hours": len(h),
                      "last_hour": h.index[-1]})


def analyse(symbol: str):
    daily, hours = newest(symbol, "1d"), newest(symbol, "1h")
    first_hour = hours.index.min()
    rows = []
    for t, d in daily.iterrows():
        date = t.tz_convert(NY_TZ).tz_localize(None).normalize() \
            if t.tz_convert("UTC").hour != 0 else t.tz_localize(None).normalize()
        row = {"symbol": symbol,
               "candle_utc": t.strftime("%a %Y-%m-%d %H:%M UTC"),
               "candle_ny": t.tz_convert(NY_TZ).strftime("%a %Y-%m-%d %H:%M %Z"),
               "open": d.open, "high": d.high, "low": d.low, "close": d.close,
               "volume": d.volume}
        for name, rule in CANDIDATES.items():
            start, end = rule(date)
            r = rebuild_day(hours, start, end) if start >= first_hour else None
            if r is None:
                row[f"{name}: OHLC matches"] = None
                continue
            ok = [f for f in ["open", "high", "low", "close"]
                  if not values_differ(pd.Series([d[f]]), pd.Series([r[f]])).iloc[0]]
            row[f"{name}: OHLC matches"] = len(ok)
            row[f"{name}: vol ratio"] = round(d.volume / r.volume, 4) if r.volume else None
            row[f"{name}: last 1h close"] = r.close
            row[f"{name}: last 1h"] = r.last_hour.tz_convert(NY_TZ).strftime("%a %H:%M %Z")
        rows.append(row)
    return pd.DataFrame(rows)


def close_and_session_check(symbol: str) -> pd.DataFrame:
    """
    For each complete NY trading day:
      * daily close vs close of the LAST 1h candle of the day, and vs the
        15:00-16:00 NY candle (last hour of the regular session)
      * stocks only, using the 7-day 1m file: daily OHLC/volume vs the regular
        session (09:30-16:00 NY) and vs the whole day
    EUR/USD uses its 17:00 NY day, which the boundary test above identified.
    """
    from check_aggregation import newest_file
    daily, hours = newest(symbol, "1d"), newest(symbol, "1h")
    m_path = newest_file(symbol, "1m")
    minutes = load_candles(m_path) if m_path else pd.DataFrame(columns=FIELDS)
    fx = symbol == "EUR/USD"
    rows = []
    for t, d in daily.iterrows():
        date = t.tz_localize(None).normalize()
        start, end = (CANDIDATES["17:00 New York"] if fx else CANDIDATES["midnight New York"])(date)
        if start < hours.index.min() or end > hours.index.max() + pd.Timedelta(hours=1):
            continue                                   # day not fully covered
        h = hours[(hours.index >= start) & (hours.index < end)]
        row = {"symbol": symbol, "day": date.strftime("%a %Y-%m-%d"), "daily_close": d.close,
               "last_1h": h.index[-1].tz_convert(NY_TZ).strftime("%H:%M %Z"),
               "last_1h_close": h.close.iloc[-1],
               "last_1h_matches": bool(np.isclose(d.close, h.close.iloc[-1]))}
        if not fx:
            ny = h.index.tz_convert(NY_TZ)
            last_reg = h[ny.hour == 15]
            row["1h_15:00_close"] = last_reg.close.iloc[0] if len(last_reg) else None
            mm = minutes[(minutes.index >= start) & (minutes.index < end)]
            covered = (len(mm) and mm.index.min() < start + pd.Timedelta(hours=5)       # from ~04:00 NY
                       and mm.index.max() >= start + pd.Timedelta(hours=19))           # to ~20:00 NY
            if covered:
                mins = mm.index.tz_convert(NY_TZ).hour * 60 + mm.index.tz_convert(NY_TZ).minute
                reg = mm[(mins >= 570) & (mins < 960)]                        # 09:30-16:00
                row.update({
                    "daily_o/h/l/c": f"{d.open}/{d.high}/{d.low}/{d.close}",
                    "regular_o/h/l/c": f"{reg.open.iloc[0]}/{reg.high.max()}/{reg.low.min()}/{reg.close.iloc[-1]}",
                    "fullday_o/h/l/c": f"{mm.open.iloc[0]}/{mm.high.max()}/{mm.low.min()}/{mm.close.iloc[-1]}",
                    "vol_daily/regular": round(d.volume / reg.volume.sum(), 3),
                    "vol_daily/fullday": round(d.volume / mm.volume.sum(), 3),
                })
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    setup_logging(LOG_FILE)
    if not args.no_fetch and not fetch_all():
        return 1

    REPORT_DIR.mkdir(exist_ok=True)
    out = pd.concat([analyse(s) for s in SYMBOLS], ignore_index=True)
    out.to_csv(REPORT_DIR / "daily_candles.csv", index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 40):
        for symbol, g in out.groupby("symbol", sort=False):
            print(f"\n================ {symbol} ================")
            print(g[["candle_utc", "candle_ny", "open", "close", "volume"]].to_string(index=False))
            print("\nOHLC fields (of 4) reproduced from 1h candles, per day boundary:")
            cols = [c for c in g.columns if c.endswith("OHLC matches") or c.endswith("vol ratio")]
            print(g[["candle_ny"] + cols].to_string(index=False))
    checks = pd.concat([close_and_session_check(s) for s in SYMBOLS], ignore_index=True)
    checks.to_csv(REPORT_DIR / "daily_close_check.csv", index=False)
    with pd.option_context("display.width", 300, "display.max_columns", 40):
        for symbol, g in checks.groupby("symbol", sort=False):
            print(f"\n---- {symbol}: daily close vs 1h candles"
                  + ("" if symbol == "EUR/USD" else " / regular session vs full day (from 1m)") + " ----")
            print(g.dropna(axis=1, how="all").drop(columns="symbol").to_string(index=False))
    print(f"\n-> {REPORT_DIR / 'daily_candles.csv'}, {REPORT_DIR / 'daily_close_check.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
