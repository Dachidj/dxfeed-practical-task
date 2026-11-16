"""Sanity checks on the combined rows: coverage, weekdays, duplicates, NYSE vs NASDAQ agreement."""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger("validate")


def check_coverage(df: pd.DataFrame, expected: list[str]) -> list[str]:
    """Every expected exchange must have at least one row."""
    present = set(df["exchange"])
    return [f"{ex}: no rows" + (" (missing manual files?)" if ex == "CME" else "")
            for ex in expected if ex not in present]


def check_weekdays(df: pd.DataFrame) -> list[str]:
    weekend = df[pd.to_datetime(df["date"]).dt.day_of_week >= 5]
    return [f"{r.exchange} {r.date} ({r.holiday}) falls on a weekend" for r in weekend.itertuples()]


def check_duplicates(df: pd.DataFrame) -> list[str]:
    """CME has one row per asset class on the same date, so the key includes the market."""
    dups = df[df.duplicated(["exchange", "market", "date"], keep=False)]
    return [f"{ex} [{mkt}] has {len(g)} rows for {d}"
            for (ex, mkt, d), g in dups.groupby(["exchange", "market", "date"])]


def check_nyse_nasdaq(df: pd.DataFrame, report: list[str]) -> list[str]:
    """Both list US equity holidays: status and close time must match on every date
    in the years both exchanges cover."""
    nyse, nasdaq = df[df.exchange == "NYSE"], df[df.exchange == "NASDAQ"]
    years = set(nyse.date.str[:4]) & set(nasdaq.date.str[:4])
    if not years:
        report.append("NYSE/NASDAQ: no overlapping years")
        return []
    cols = ["date", "status", "close_time_et"]
    a = nyse[nyse.date.str[:4].isin(years)][cols].set_index("date")
    b = nasdaq[nasdaq.date.str[:4].isin(years)][cols].set_index("date")
    j = a.join(b, how="outer", suffixes=("_nyse", "_nasdaq")).fillna("-")
    bad = j[(j.status_nyse != j.status_nasdaq) | (j.close_time_et_nyse != j.close_time_et_nasdaq)]
    report.append(f"NYSE/NASDAQ agree on {len(j) - len(bad)} of {len(j)} dates ({', '.join(sorted(years))})")
    return [f"NYSE/NASDAQ mismatch on {d}: NYSE={r.status_nyse} {r.close_time_et_nyse}, "
            f"NASDAQ={r.status_nasdaq} {r.close_time_et_nasdaq}" for d, r in bad.iterrows()]


def validate(df: pd.DataFrame, expected: list[str] = ()) -> tuple[list[str], list[str]]:
    """-> (problems, report lines). `expected`: exchanges that must have rows."""
    report: list[str] = []
    problems = check_coverage(df, list(expected))
    if df.empty:
        return problems or ["No rows to validate"], report
    problems += check_weekdays(df) + check_duplicates(df) + check_nyse_nasdaq(df, report)
    for p in problems:
        log.warning(p)
    return problems, report
