"""
Step 2 - check that dxFeed's bigger candles agree with its smaller ones.

For each symbol it rebuilds
    5m candles from 1m candles, and
    1h candles from 5m candles
using  open=first, high=max, low=min, close=last, volume=sum, count=sum,
then compares them with the 5m / 1h candles the API returned.

Every interval where something differs is written to
    reports/aggregation_report.csv
together with how many sub-candles were missing in that interval.
A per-check summary goes to reports/aggregation_summary.csv.

Input: the newest raw file per symbol+timeframe in data/raw/ (made by fetch.py).

Usage:
    python check_aggregation.py
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
RAW_DIR = PROJECT_DIR / "data" / "raw"
REPORT_DIR = PROJECT_DIR / "reports"

SYMBOLS = ["AAPL", "TSLA", "EUR/USD"]
NY_TZ = "America/New_York"

# (smaller timeframe, bigger timeframe, pandas frequency of the bigger one)
CHECKS = [("1m", "5m", "5min"), ("5m", "1h", "1h")]
PERIOD = {"1m": pd.Timedelta(minutes=1),
          "5m": pd.Timedelta(minutes=5),
          "1h": pd.Timedelta(hours=1)}

FIELDS = ["open", "high", "low", "close", "volume", "count"]
AGG_RULES = {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum", "count": "sum"}

# Floats from JSON plus summing many volumes -> compare with a tiny tolerance.
RTOL, ATOL = 1e-9, 1e-9
REMOVE_EVENT_FLAG = 0x02     # dxFeed eventFlags bit meaning "this event was removed"

FILE_RE = re.compile(r"^(?P<sym>.+)_(?P<tf>1m|5m|1h)_(?P<stamp>\d{8}T\d{6}Z)\.json$")


# ---------------------------------------------------------------- loading
def newest_file(symbol: str, tf: str) -> Path | None:
    """Newest data/raw/<SYMBOL>_<tf>_<stamp>.json. The stamp sorts as text."""
    safe = symbol.replace("/", "-")
    files = sorted(p for p in RAW_DIR.glob(f"{safe}_{tf}_*.json")
                   if FILE_RE.match(p.name))
    return files[-1] if files else None


def fetch_time(path: Path) -> pd.Timestamp:
    stamp = FILE_RE.match(path.name)["stamp"]
    return pd.Timestamp(pd.to_datetime(stamp, format="%Y%m%dT%H%M%SZ"), tz="UTC")


def load_candles(path: Path) -> pd.DataFrame:
    """Raw JSON -> DataFrame indexed by candle start time (UTC), sorted, no duplicates."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for lst in payload.get("Candle", {}).values() for row in lst]
    if not rows:
        return pd.DataFrame(columns=FIELDS)

    df = pd.DataFrame(rows)
    # Missing numbers arrive as the string "NaN"; to_numeric turns them into real NaN.
    for col in FIELDS + ["eventFlags"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[(df["eventFlags"].fillna(0).astype(int) & REMOVE_EVENT_FLAG) == 0]

    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)   # epoch ms -> UTC
    df = (df.drop_duplicates("time", keep="last")
            .sort_values("time")
            .set_index("time"))
    return df[FIELDS]


# ---------------------------------------------------------------- core logic
def rebuild(sub: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Group sub-candles into buckets of `freq` (aligned to UTC) and aggregate."""
    bucket = sub.index.floor(freq)            # 09:37 -> 09:35 for '5min'
    grouped = sub.groupby(bucket)
    out = grouped.agg(AGG_RULES)
    out["sub_found"] = grouped.size()
    out.index.name = "time"
    return out


def comparison_window(sub, tgt, sub_tf, tgt_tf, freq, fetched):
    """
    Only compare intervals that BOTH files fully cover and that were finished
    when the files were downloaded. Otherwise the first/last interval of the
    date range (cut by fromTime/toTime) or the candle still in progress
    would show up as fake differences.
    """
    start = max(sub.index.min(), tgt.index.min()).ceil(freq)
    end = min(sub.index.max() + PERIOD[sub_tf],
              tgt.index.max() + PERIOD[tgt_tf],
              fetched).floor(freq)
    return start, end


def values_differ(a: pd.Series, b: pd.Series) -> pd.Series:
    """True where a and b differ. NaN vs NaN counts as equal; NaN vs number differs."""
    both_nan = a.isna() & b.isna()
    close = np.isclose(a.astype(float), b.astype(float), rtol=RTOL, atol=ATOL)
    return ~(both_nan | close)


def compare(symbol: str, sub_tf: str, tgt_tf: str, freq: str):
    sub_path, tgt_path = newest_file(symbol, sub_tf), newest_file(symbol, tgt_tf)
    if not sub_path or not tgt_path:
        print(f"  {symbol} {sub_tf}->{tgt_tf}: raw file missing, skipped")
        return None, None
    sub, tgt = load_candles(sub_path), load_candles(tgt_path)
    if sub.empty or tgt.empty:
        print(f"  {symbol} {sub_tf}->{tgt_tf}: no candles in "
              f"{sub_path.name if sub.empty else tgt_path.name}, skipped")
        return None, None

    fetched = min(fetch_time(sub_path), fetch_time(tgt_path))
    start, end = comparison_window(sub, tgt, sub_tf, tgt_tf, freq, fetched)
    rebuilt = rebuild(sub, freq)

    def in_window(df):
        return df[(df.index >= start) & (df.index + PERIOD[tgt_tf] <= end)]
    rebuilt, returned = in_window(rebuilt), in_window(tgt)

    # Outer join: keep intervals that exist on only one side as well.
    merged = returned.add_prefix("returned_").join(
        rebuilt.add_prefix("rebuilt_"), how="outer")
    merged = merged.rename(columns={"rebuilt_sub_found": "sub_candles_found"})
    merged["sub_candles_found"] = merged["sub_candles_found"].fillna(0).astype(int)
    expected = int(PERIOD[tgt_tf] / PERIOD[sub_tf])           # 5 or 12
    merged["sub_candles_expected"] = expected
    merged["sub_candles_missing"] = expected - merged["sub_candles_found"]

    has_returned = merged["returned_open"].notna() | merged["returned_count"].notna()
    has_rebuilt = merged["sub_candles_found"] > 0

    diff_flags = pd.DataFrame(index=merged.index)
    for f in FIELDS:
        merged[f"diff_{f}"] = merged[f"returned_{f}"] - merged[f"rebuilt_{f}"]
        diff_flags[f] = values_differ(merged[f"returned_{f}"], merged[f"rebuilt_{f}"])
    merged["mismatched_fields"] = diff_flags.apply(
        lambda r: ",".join(f for f in FIELDS if r[f]), axis=1)

    merged["status"] = np.select(
        [~has_returned, ~has_rebuilt, merged["mismatched_fields"] != ""],
        ["missing_returned_candle", "no_sub_candles", "mismatch"],
        default="ok")

    report = merged[merged["status"] != "ok"].copy()
    report = add_readable_times(report, PERIOD[tgt_tf])
    report.insert(0, "check", f"{sub_tf}->{tgt_tf}")
    report.insert(0, "symbol", symbol)

    summary = {
        "symbol": symbol, "check": f"{sub_tf}->{tgt_tf}",
        "window_start_utc": fmt(start), "window_end_utc": fmt(end),
        "intervals_compared": len(merged),
        "ok": int((merged["status"] == "ok").sum()),
        "mismatch": int((merged["status"] == "mismatch").sum()),
        "missing_returned_candle": int((merged["status"] == "missing_returned_candle").sum()),
        "no_sub_candles": int((merged["status"] == "no_sub_candles").sum()),
        "intervals_with_gaps": int((merged["sub_candles_missing"] > 0).sum()),
        "sub_file": sub_path.name, "target_file": tgt_path.name,
    }
    for f in FIELDS:
        summary[f"{f}_mismatches"] = int((diff_flags[f] & has_returned & has_rebuilt).sum())
    return report, summary


# ---------------------------------------------------------------- formatting
def fmt(ts: pd.Timestamp, tz: str = "UTC") -> str:
    return ts.tz_convert(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def add_readable_times(df: pd.DataFrame, period: pd.Timedelta) -> pd.DataFrame:
    starts = df.index.to_series()
    ends = starts + period
    times = pd.DataFrame({
        "interval_start_utc": starts.dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "interval_end_utc": ends.dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        # %Z gives EST or EDT, so daylight-saving is visible in the report
        "interval_start_ny": starts.dt.tz_convert(NY_TZ).dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "interval_end_ny": ends.dt.tz_convert(NY_TZ).dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }, index=df.index)
    lead = ["status", "mismatched_fields", "sub_candles_found",
            "sub_candles_expected", "sub_candles_missing"]
    per_field = [f"{p}_{f}" for f in FIELDS for p in ("returned", "rebuilt", "diff")]
    return pd.concat([times, df[lead + per_field]], axis=1).reset_index(drop=True)


# ---------------------------------------------------------------- main
def main() -> int:
    REPORT_DIR.mkdir(exist_ok=True)
    reports, summaries = [], []
    for symbol in SYMBOLS:
        for sub_tf, tgt_tf, freq in CHECKS:
            report, summary = compare(symbol, sub_tf, tgt_tf, freq)
            if summary is None:
                continue
            reports.append(report)
            summaries.append(summary)

    if not summaries:
        print("Nothing to compare - run fetch.py first.")
        return 1

    report = pd.concat(reports, ignore_index=True)
    summary = pd.DataFrame(summaries)
    report.to_csv(REPORT_DIR / "aggregation_report.csv", index=False)
    summary.to_csv(REPORT_DIR / "aggregation_summary.csv", index=False)

    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(summary[["symbol", "check", "intervals_compared", "ok", "mismatch",
                       "missing_returned_candle", "no_sub_candles",
                       "intervals_with_gaps"] + [f"{f}_mismatches" for f in FIELDS]]
              .to_string(index=False))
    print(f"\n{len(report)} differing intervals -> {REPORT_DIR / 'aggregation_report.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
