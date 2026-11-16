"""Shared text cleanup + row normalization used by every scraper."""
from __future__ import annotations

import logging
import re
from datetime import date

log = logging.getLogger(__name__)

SCHEMA = ["exchange", "market", "date", "holiday", "status", "close_time_et",
          "notes", "source_url", "scraped_at_utc"]
STATUSES = {"closed", "early_close", "trading_halt"}

_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "\xa0": " ", " ": " ", " ": " ",
})
_MARKER_RE = re.compile(r"\s*(\*+|[†‡]+)\s*$")   # trailing *, **, †, ‡
_ANY_MARKER_RE = re.compile(r"\*+")
_TIME_RE = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>[ap])\.?\s*m\.?", re.I)


def clean_text(text: str | None, strip_markers: bool = True) -> str:
    """Straight quotes, collapsed whitespace, optionally drop footnote asterisks."""
    if text is None:
        return ""
    s = str(text).translate(_QUOTES)
    if strip_markers:
        s = _ANY_MARKER_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def split_marker(text: str) -> tuple[str, str]:
    """'Thursday, November 26***' -> ('Thursday, November 26', '***')."""
    s = clean_text(text, strip_markers=False)
    m = _MARKER_RE.search(s)
    if not m:
        return s, ""
    return s[:m.start()].strip(), m.group(1)


def parse_time_et(text: str) -> str | None:
    """'1:00 p.m.' -> '13:00'. Returns None if no clock time present."""
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    h = int(m["h"]) % 12 + (12 if m["ap"].lower() == "p" else 0)
    return f"{h:02d}:{m['m']}"


def early_close_name(holiday: str, early: date, holiday_date: date) -> str:
    """Human name for an early-close session adjacent to a holiday.

    Christmas Eve gets its common name; otherwise "Day before/after <holiday>",
    using the colloquial short form for Thanksgiving ("Day after Thanksgiving").
    """
    h = clean_text(holiday)
    before = early < holiday_date
    if before and h.lower().startswith("christmas"):
        return "Christmas Eve"
    short = re.sub(r"\s+Day$", "", h) if h.lower().startswith("thanksgiving") else h
    return f"Day {'before' if before else 'after'} {short}"


def normalize_rows(rows: list[dict]) -> list[dict]:
    """Clean every string field and enforce the shared schema. Never raises on bad rows: logs and drops."""
    out = []
    for r in rows:
        r = {k: clean_text(r.get(k)) for k in SCHEMA}
        if r["status"] not in STATUSES:
            log.warning("Dropping row with unknown status %r: %s", r["status"], r)
            continue
        try:
            date.fromisoformat(r["date"])
        except ValueError:
            log.warning("Dropping row with bad date %r: %s", r["date"], r)
            continue
        out.append(r)
    return out
