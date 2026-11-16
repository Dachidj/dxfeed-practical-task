"""
Part 1 extra - what happens around the weekend (Sat Nov 7 / Sun Nov 8, 2026).

Offline: reads the newest 7-day raw files (same ones check_aggregation.py uses).
For every symbol and timeframe it reports
  * candles stamped on Saturday or Sunday (New York time)
  * the weekend gap: last candle before it and first candle after it
  * anything odd at those two edge candles: zero/NaN volume, a trade count
    far below normal (partial-looking), and missing candles around the edge

Output: printed tables + reports/weekend_edges.csv
"""

import pandas as pd

from check_aggregation import (NY_TZ, PERIOD, REPORT_DIR, SYMBOLS, load_candles,
                               newest_file)

TIMEFRAMES = ["1m", "5m", "1h"]
WEEKEND_START = pd.Timestamp("2026-11-07 00:00", tz=NY_TZ)   # Saturday 00:00 NY
WEEKEND_END = pd.Timestamp("2026-11-09 00:00", tz=NY_TZ)     # Monday 00:00 NY
LOW_COUNT_RATIO = 0.10      # edge candle with < 10% of the median trade count
EDGE_WINDOW = pd.Timedelta(hours=1)


def both(ts: pd.Timestamp) -> str:
    return (f"{ts.tz_convert('UTC').strftime('%a %m-%d %H:%M')} UTC / "
            f"{ts.tz_convert(NY_TZ).strftime('%a %m-%d %H:%M %Z')}")


def weekend_gap(df: pd.DataFrame):
    """The longest gap between two consecutive candles that overlaps Sat-Sun (NY)."""
    t = df.index.to_series()
    gaps = pd.DataFrame({"before": t.shift(1), "after": t}).dropna()
    gaps = gaps[(gaps.before < WEEKEND_END) & (gaps.after > WEEKEND_START)]
    if gaps.empty:
        return None, None
    g = gaps.loc[(gaps.after - gaps.before).idxmax()]
    return g.before, g.after


def edge_notes(df: pd.DataFrame, ts: pd.Timestamp, tf: str, side: str) -> str:
    """Flag zero/NaN volume, a tiny trade count, and missing candles near the edge."""
    row = df.loc[ts]
    notes = []
    if pd.isna(row.volume) or row.volume == 0:
        notes.append("zero/NaN volume")
    median = df["count"].median()
    if row["count"] < LOW_COUNT_RATIO * median:
        notes.append(f"count {row['count']:.0f} vs median {median:.0f} (partial-looking)")
    if tf != "1h":
        # How many of the expected candles exist in the hour before/after the edge?
        lo, hi = ((ts - EDGE_WINDOW + PERIOD[tf], ts + PERIOD[tf]) if side == "before"
                  else (ts, ts + EDGE_WINDOW))
        present = ((df.index >= lo) & (df.index < hi)).sum()
        expected = int(EDGE_WINDOW / PERIOD[tf])
        if present < expected:
            notes.append(f"{expected - present}/{expected} candles missing in the hour "
                         f"{'before' if side == 'before' else 'after'}")
    return "; ".join(notes) or "-"


def main() -> None:
    rows = []
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            path = newest_file(symbol, tf)
            df = load_candles(path) if path else pd.DataFrame()
            if df.empty:
                continue
            ny = df.index.tz_convert(NY_TZ)
            weekend = df[ny.day_of_week >= 5]                 # 5 = Sat, 6 = Sun
            before, after = weekend_gap(df)
            rows.append({
                "symbol": symbol, "tf": tf,
                "weekend_candles": len(weekend),
                "first_weekend": both(weekend.index.min()) if len(weekend) else "",
                "last_weekend": both(weekend.index.max()) if len(weekend) else "",
                "last_before_gap": both(before) if before is not None else "",
                "first_after_gap": both(after) if after is not None else "",
                "gap_hours": round((after - before).total_seconds() / 3600, 2)
                             if before is not None else None,
                "before_volume": df.loc[before, "volume"] if before is not None else None,
                "before_count": df.loc[before, "count"] if before is not None else None,
                "after_volume": df.loc[after, "volume"] if after is not None else None,
                "after_count": df.loc[after, "count"] if after is not None else None,
                "before_notes": edge_notes(df, before, tf, "before") if before is not None else "",
                "after_notes": edge_notes(df, after, tf, "after") if after is not None else "",
                "file": path.name,
            })
    out = pd.DataFrame(rows)
    REPORT_DIR.mkdir(exist_ok=True)
    out.to_csv(REPORT_DIR / "weekend_edges.csv", index=False)
    with pd.option_context("display.width", 250, "display.max_colwidth", 80):
        for r in out.itertuples():
            print(f"\n{r.symbol} {r.tf}: {r.weekend_candles} candles on Sat/Sun NY"
                  + (f"  ({r.first_weekend}  ...  {r.last_weekend})" if r.weekend_candles else ""))
            print(f"  last before gap : {r.last_before_gap}   vol {r.before_volume:,.2f}  "
                  f"count {r.before_count:,.0f}   -> {r.before_notes}")
            print(f"  first after gap : {r.first_after_gap}   vol {r.after_volume:,.2f}  "
                  f"count {r.after_count:,.0f}   -> {r.after_notes}")
            print(f"  gap: {r.gap_hours} h")
    print(f"\n-> {REPORT_DIR / 'weekend_edges.csv'}")


if __name__ == "__main__":
    main()
