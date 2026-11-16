"""NYSE holiday + early-close scraper.

Source: https://www.nyse.com/markets/hours-calendars
Table layout: Holiday | <year> | <year> | ...  (years read from the header).
Cell examples:
    "Thursday, January 1"
    "Friday, July 3 (Independence Day observed)"
    "Thursday, November 26***"           -> footnote marker
    "—*"                                 -> not observed (skip, log)
Footnotes below the table ("*** Each market will close early at 1:00 p.m. ... on
Friday, November 27, 2026, ...") produce separate early_close rows.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from .base import fetch, utc_now_iso
from .normalize import clean_text, early_close_name, normalize_rows, parse_time_et, split_marker

log = logging.getLogger(__name__)

EXCHANGE = "NYSE"
MARKET = "equities"
URL = "https://www.nyse.com/markets/hours-calendars"

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
CELL_RE = re.compile(
    r"^(?P<weekday>[A-Za-z]+),\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})"
    r"\s*(?:\((?P<note>[^)]*)\))?$"
)
NOT_OBSERVED_RE = re.compile(r"^[—–\-]+$")  # em/en dash or hyphen
FOOTNOTE_RE = re.compile(r"^(?P<marker>\*+)\s+(?P<text>.+)$", re.S)
EARLY_RE = re.compile(r"close early at\s+(?P<t>\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?)", re.I)
OPTIONS_RE = re.compile(r"(?P<t>\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?)\s*for eligible options", re.I)
LATE_RE = re.compile(r"late trading sessions? will close at\s+(?P<t>\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?)", re.I)
FULL_DATE_RE = re.compile(
    r"(?P<weekday>[A-Z][a-z]+day),\s+(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})")


def _to_date(weekday: str, month: str, day: str, year: int, ctx: str) -> date:
    dt = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
    if dt.strftime("%A").lower() != weekday.lower():
        log.warning("Weekday mismatch in %s: page says %s, %s is a %s", ctx, weekday, dt, dt.strftime("%A"))
    return dt


def _find_table(soup: BeautifulSoup) -> Tag:
    for table in soup.find_all("table"):
        header = [clean_text(c.get_text()) for c in table.find("tr").find_all(["th", "td"])]
        if header and header[0].lower().startswith("holiday") and any(YEAR_RE.fullmatch(h) for h in header[1:]):
            return table
    raise RuntimeError("NYSE holiday table not found (layout changed?)")


def _find_footnotes(table: Tag) -> dict[str, str]:
    """Climb from the table until a container holds '*'-prefixed footnote paragraphs."""
    node: Tag | None = table
    for _ in range(8):
        node = node.parent
        if node is None:
            break
        notes: dict[str, str] = {}
        for line in node.get_text("\n").split("\n"):
            m = FOOTNOTE_RE.match(clean_text(line, strip_markers=False))
            if m:
                notes[m["marker"]] = clean_text(m["text"], strip_markers=False)
        if notes:
            log.info("Found %d footnotes: %s", len(notes), ", ".join(sorted(notes, key=len)))
            return notes
    log.warning("No footnotes found near holiday table")
    return {}


def _early_close_notes(footnote: str) -> str:
    parts = []
    if m := OPTIONS_RE.search(footnote):
        parts.append(f"Eligible options close {parse_time_et(m['t'])} ET")
    if m := LATE_RE.search(footnote):
        parts.append(f"late trading sessions close {parse_time_et(m['t'])} ET")
    return "; ".join(parts)


def parse(html: str, scraped_at: str | None = None) -> list[dict]:
    scraped_at = scraped_at or utc_now_iso()
    soup = BeautifulSoup(html, "lxml")
    table = _find_table(soup)
    footnotes = _find_footnotes(table)

    rows = table.find_all("tr")
    header = [clean_text(c.get_text()) for c in rows[0].find_all(["th", "td"])]
    years: list[int | None] = [int(h) if YEAR_RE.fullmatch(h) else None for h in header]
    log.info("Header: %s -> years %s", header, [y for y in years if y])

    def row(d: date, holiday, status, close_time="", notes=""):
        return {"exchange": EXCHANGE, "market": MARKET, "date": d.isoformat(), "holiday": holiday,
                "status": status, "close_time_et": close_time, "notes": notes,
                "source_url": URL, "scraped_at_utc": scraped_at}

    out: list[dict] = []
    # footnote marker -> [(holiday name, holiday date)] for early-close footnotes
    early_refs: dict[str, list[tuple[str, date]]] = {}

    for tr in rows[1:]:
        raw_cells = [c.get_text() for c in tr.find_all(["th", "td"])]
        if not raw_cells or not clean_text(raw_cells[0]):
            continue
        holiday = clean_text(raw_cells[0])
        for idx, raw in enumerate(raw_cells[1:], start=1):
            year = years[idx] if idx < len(years) else None
            text, marker = split_marker(raw)
            if year is None:
                log.warning("Cell %r under non-year header skipped", text)
                continue
            ctx = f"{holiday} / {year}"

            if NOT_OBSERVED_RE.match(text):
                fn = footnotes.get(marker, "")
                log.info("Not observed, skipped: %s %s", ctx, f"({clean_text(fn)})" if fn else "")
                continue

            m = CELL_RE.match(text)
            if not m:
                log.warning("Unparseable cell %r (%s) skipped", text, ctx)
                continue
            d = _to_date(m["weekday"], m["month"], m["day"], year, ctx)

            if marker:
                fn = footnotes.get(marker)
                if fn is None:
                    log.warning("Footnote %s referenced by %s not found", marker, ctx)
                elif EARLY_RE.search(fn):
                    early_refs.setdefault(marker, []).append((holiday, d))
                else:
                    log.info("Footnote %s on %s is not an early close; ignored: %s", marker, ctx, fn)
            out.append(row(d, holiday, "closed", notes=m["note"] or ""))

    # Early closes: dates (with years) come from the footnote text itself.
    seen: set[date] = set()
    for marker, refs in early_refs.items():
        fn = footnotes[marker]
        close_time = parse_time_et(EARLY_RE.search(fn)["t"])
        notes = _early_close_notes(fn)
        for dm in FULL_DATE_RE.finditer(fn):
            d = _to_date(dm["weekday"], dm["month"], dm["day"], int(dm["year"]), f"footnote {marker}")
            if d in seen:
                continue
            seen.add(d)
            # attach to the referencing holiday closest in time to this early-close date
            hol_name, hol_date = min(refs, key=lambda r: abs((r[1] - d).days))
            if abs((hol_date - d).days) > 7:
                log.warning("Early close %s is far from its holiday %s (%s)", d, hol_name, hol_date)
            out.append(row(d, early_close_name(hol_name, d, hol_date), "early_close", close_time, notes))

    return normalize_rows(out)


def scrape() -> list[dict]:
    return parse(fetch(URL, EXCHANGE))
