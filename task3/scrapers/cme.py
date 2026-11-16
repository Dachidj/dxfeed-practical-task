"""CME Globex holiday hours from manually downloaded files in data/cme_raw/*.xlsx.

CME blocks automated access and its terms prohibit scraping, so the official
"Download data" files from cmegroup.com are saved by hand as <year>_<holiday>.xlsx
(see README). First sheet:

    Asset Class | 2026-11-25 | 2026-11-26 | 2026-11-27
    Equities    | "12:00 Trade Date: 2026-11-27 (PREOPEN)\\n17:00 Trade Date: 2026-11-27 (OPEN)"

Each cell line is a state change at HH:MM US Central time. An empty cell means no
change that day, so the state carries over from the previous column.
Output: one row per asset class for the holiday itself - closed or trading_halt.
"""
from __future__ import annotations

import logging
import re
from collections import namedtuple
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

from .base import PROJECT_ROOT
from .normalize import normalize_rows

log = logging.getLogger(__name__)

EXCHANGE = "CME"
RAW_DIR = PROJECT_ROOT / "data" / "cme_raw"
SOURCE_URL = "https://www.cmegroup.com/trading-hours.html"
CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
# Globex's normal daily break starts at 16:00 CT (crypto: 16:00-16:02). A pause at or
# after this time on the holiday is that ordinary break, not a holiday halt.
DAILY_BREAK_CT = time(16, 0)

EVENT_RE = re.compile(r"(\d{1,2}):(\d{2})\s+Trade Date:\s*(\d{4}-\d{2}-\d{2})\s*\((\w+)\)")
NAME_OVERRIDES = {"mlk_day": "Martin Luther King Jr. Day", "new_years_day": "New Year's Day"}

Event = namedtuple("Event", "at trade_date state")


def holiday_name(slug: str) -> str:
    """'good_friday' -> 'Good Friday'."""
    return NAME_OVERRIDES.get(slug) or slug.replace("_", " ").title()


def to_et(d: date, t: time) -> str:
    """CT wall-clock time on date d -> ET wall-clock time (via time zones, not +1h)."""
    return datetime.combine(d, t, tzinfo=CT).astimezone(ET).strftime("%H:%M")


def parse_cell(text, ctx: str) -> list[Event]:
    events = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        m = EVENT_RE.search(line)
        if not m:
            log.warning("CME %s: unparseable line %r skipped", ctx, line)
            continue
        events.append(Event(time(int(m[1]), int(m[2])), date.fromisoformat(m[3]), m[4].upper()))
    return events


def read_file(path: Path) -> tuple[list[date], dict[str, list[list[Event]]]]:
    """-> (column dates, {asset class: [events of each column]})."""
    wb = openpyxl.load_workbook(path, read_only=True)
    rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    wb.close()
    start = next(i for i, r in enumerate(rows) if r and str(r[0] or "").startswith("Asset Class"))
    dates = [date.fromisoformat(str(v)[:10]) for v in rows[start][1:] if v]
    table = {}
    for r in rows[start + 1:]:
        if r and r[0]:
            cells = (r[1:] + [None] * len(dates))[:len(dates)]
            table[r[0]] = [parse_cell(c, f"{path.name} {r[0]} {d}") for c, d in zip(cells, dates)]
    return dates, table


def find_holiday(dates: list[date], table: dict, fname: str) -> int:
    """Column where the most asset classes have no OPEN for that trade date.
    Ties (e.g. the Saturday after) go to the weekday, then the earlier date."""
    def score(i):
        d = dates[i]
        no_trading = sum(not any(e.state == "OPEN" and e.trade_date == d for e in cols[i])
                         for cols in table.values())
        return no_trading, d.weekday() < 5
    best = max(range(len(dates)), key=lambda i: (score(i), -i))
    log.info("CME %s: holiday = %s (%s); no-trading counts per column: %s", fname, dates[best],
             f"{dates[best]:%a}", ", ".join(f"{d} {score(i)[0]}/{len(table)}" for i, d in enumerate(dates)))
    return best


def start_state(cols: list[list[Event]], i: int) -> str | None:
    """State at the start of column i = last event of the nearest earlier non-empty column."""
    for j in range(i - 1, -1, -1):
        if cols[j]:
            return cols[j][-1].state
    return None


def classify(start: str | None, events: list[Event]):
    """Holiday cell -> (status, halt event, reopen event, reason); status None = normal trading."""
    reopen = next((e for e in events if e.state == "OPEN"), None)
    if not events:
        return "closed", None, None, "no trading events"
    # unknown start: a first event that leaves OPEN means the session was open
    was_open = start == "OPEN" if start else events[0].state != "OPEN"
    if not was_open:
        return "closed", None, reopen, "not open during the day"
    pause = next((e for e in events if e.state != "OPEN"), None)
    if pause and pause.at < DAILY_BREAK_CT:
        reopen = next((e for e in events[events.index(pause):] if e.state == "OPEN"), None)
        return "trading_halt", pause, reopen, "halted"
    return None, None, None, "only the daily break" if pause else "open all day"


def parse_file(path: Path, scraped_at: str | None = None) -> list[dict]:
    path = Path(path)
    year, _, slug = path.stem.partition("_")
    holiday = holiday_name(slug)
    scraped_at = scraped_at or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    dates, table = read_file(path)
    hi = find_holiday(dates, table, path.name)
    day = dates[hi]
    if str(day.year) != year:
        log.warning("CME %s: filename year %s but holiday date is %s", path.name, year, day)

    out, normal = [], []
    for asset, cols in table.items():
        status, pause, reopen, reason = classify(start_state(cols, hi), cols[hi])
        reopens = f"; reopens {reopen.at:%H:%M} CT for trade date {reopen.trade_date}" if reopen else ""
        if status == "closed":
            close_et, notes = "", f"No trading ({reason}){reopens}"
        elif status == "trading_halt":
            close_et = to_et(day, pause.at)
            notes = f"Halted {pause.at:%H:%M} CT ({pause.state}){reopens or '; no reopen that day'}"
        else:
            normal.append(f"{asset} ({reason})")
            continue
        out.append({"exchange": EXCHANGE, "market": f"Globex: {asset}", "date": day.isoformat(),
                    "holiday": holiday, "status": status, "close_time_et": close_et,
                    "notes": f"{notes}; file {path.name}", "source_url": SOURCE_URL,
                    "scraped_at_utc": scraped_at})
    if normal:
        log.info("CME %s %s: normal trading, no row: %s", path.name, day, "; ".join(normal))
    return normalize_rows(out)


def scrape() -> list[dict]:
    files = sorted(RAW_DIR.glob("[0-9]*.xlsx"))
    if not files:
        log.warning("CME: no manual files found — see README")
        return []
    rows = []
    for f in files:
        try:
            rows.extend(parse_file(f))
        except Exception:
            log.exception("CME file %s failed", f.name)
    log.info("CME: %d rows from %d manual file(s)", len(rows), len(files))
    return rows
