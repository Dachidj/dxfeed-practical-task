"""NASDAQ holiday + early-close scraper.

Source: https://www.nasdaqtrader.com/Trader.aspx?id=Calendar
Page heading: "U.S. Equity and Options Markets Holiday Schedule <year>"
Table layout:  <year> | Holiday | Status
    "November 26, 2026 | Thanksgiving Day - U.S.     | Closed"
    "November 27, 2026 | Early Close* - U.S.         | 1:00 p.m."
The year is taken from the header cell / heading; dates that carry their own
year are cross-checked against it.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from .base import fetch, utc_now_iso
from .normalize import clean_text, early_close_name, normalize_rows, parse_time_et

log = logging.getLogger(__name__)

EXCHANGE = "NASDAQ"
MARKET = "equities and options"
URL = "https://www.nasdaqtrader.com/Trader.aspx?id=Calendar"
HEADING = "holiday schedule"

YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
OBSERVED_RE = re.compile(r"\s*\(observed\)\s*", re.I)
REGION_SUFFIX_RE = re.compile(r"\s*-\s*U\.?S\.?\s*$", re.I)
EARLY_LABEL_RE = re.compile(r"^early close\b", re.I)
MAX_ATTACH_DAYS = 3  # an early close must be within this many days of a closed holiday


def _find_table(soup: BeautifulSoup) -> tuple[Tag, int | None]:
    for table in soup.find_all("table"):
        first = table.find("tr")
        if not first:
            continue
        header = [clean_text(c.get_text()).lower() for c in first.find_all(["th", "td"])]
        if len(header) >= 3 and "holiday" in header[1] and "status" in header[2]:
            year = None
            if m := YEAR_RE.search(header[0]):
                year = int(m[1])
            heading = table.find_previous(["h1", "h2", "h3"])
            htxt = clean_text(heading.get_text()) if heading else ""
            if HEADING not in htxt.lower():
                log.warning("Table heading is %r, expected '...Holiday Schedule...'", htxt)
            elif year is None and (m := YEAR_RE.search(htxt)):
                year = int(m[1])
            log.info("Heading %r, header %r -> year %s", htxt, header, year)
            return table, year
    raise RuntimeError("NASDAQ holiday table not found (layout changed?)")


def _parse_date(text: str, year: int | None, ctx: str) -> date | None:
    """'November 26, 2026' -> date; warns if its year differs from the table's year."""
    try:
        d = datetime.strptime(clean_text(text), "%B %d, %Y").date()
    except ValueError:
        log.warning("%s: unparseable date %r skipped", ctx, text)
        return None
    if year and d.year != year:
        log.warning("%s: date year %s differs from table year %s", ctx, d.year, year)
    return d


def _find_footnote(table: Tag) -> str:
    for s in table.find_all_next(string=True, limit=60):
        t = clean_text(s, strip_markers=False)
        if t.startswith("*"):
            return clean_text(t)
    return ""


def parse(html: str, scraped_at: str | None = None) -> list[dict]:
    scraped_at = scraped_at or utc_now_iso()
    soup = BeautifulSoup(html, "lxml")
    table, year = _find_table(soup)
    footnote = _find_footnote(table)
    if footnote:
        log.info("Footnote: %s", footnote)

    closed: list[dict] = []
    early: list[tuple[date, str, str]] = []  # (date, close_time, raw label)

    for tr in table.find_all("tr")[1:]:
        cells = [c.get_text() for c in tr.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        raw_date, raw_hol, raw_status = cells[:3]
        label = clean_text(raw_hol)
        status_txt = clean_text(raw_status)
        d = _parse_date(raw_date, year, label)
        if d is None:
            continue

        if status_txt.lower() == "closed":
            observed = bool(OBSERVED_RE.search(label))
            name = REGION_SUFFIX_RE.sub("", OBSERVED_RE.sub(" ", label)).strip()
            closed.append({"date": d, "holiday": name, "notes": f"{name} observed" if observed else ""})
        elif (t := parse_time_et(status_txt)) is not None:
            if not EARLY_LABEL_RE.search(label):
                log.info("%s has a time status but label %r is not 'Early Close'", d, label)
            early.append((d, t, label))
        else:
            log.warning("Unknown status %r on %s (%s) skipped", status_txt, d, label)

    def row(d: date, holiday, status, close_time="", notes=""):
        return {"exchange": EXCHANGE, "market": MARKET, "date": d.isoformat(), "holiday": holiday,
                "status": status, "close_time_et": close_time, "notes": notes,
                "source_url": URL, "scraped_at_utc": scraped_at}

    out = [row(c["date"], c["holiday"], "closed", notes=c["notes"]) for c in closed]
    for d, t, label in early:
        near = min(closed, key=lambda c: abs((c["date"] - d).days), default=None)
        if near and abs((near["date"] - d).days) <= MAX_ATTACH_DAYS:
            name = early_close_name(near["holiday"], d, near["date"])
        else:
            name = REGION_SUFFIX_RE.sub("", label).strip()
            log.warning("Early close %s not adjacent to any closed holiday; named %r", d, name)
        out.append(row(d, name, "early_close", t, footnote))
    return normalize_rows(out)


def scrape() -> list[dict]:
    return parse(fetch(URL, EXCHANGE))
