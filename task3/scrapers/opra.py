"""OPRA holiday schedule scraper (PDF notices).

Discovery: https://www.opraplan.com links its notices (PDFs). Of the links mentioning
"holiday schedule", only the newest year's PDF is downloaded.

PDF layout (text, no table), e.g. 2025:
    2025 HOLIDAY SCHEDULE
    NEW YEAR'S DAY JANUARY 1, 2025
    MARTIN LUTHER KING JR DAY JANUARY 20          <- year inherited from title
    INDEPENDENCE DAY JULY 4**
    CHRISTMAS DAY (observed) DECEMBER 24          <- "(observed)" goes to notes
    NEW YEAR'S DAY January 1, 2026                <- next-year carry-over
    * Each market will close 3 hours early on Friday, November 28, 2025 (...) and ...
Early closes come from the footnotes. Each footnote date is checked against its stated
weekday; a mismatch is logged as a WARNING and the date skipped.

If OPRA has not published the current year, its rows for that year are a labelled
PROXY copied from NASDAQ ("U.S. Equity and Options Markets" calendar).
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup

from . import nasdaq
from .base import fetch, fetch_bytes, utc_now_iso
from .normalize import clean_text, early_close_name, normalize_rows

log = logging.getLogger(__name__)

EXCHANGE = "OPRA"
MARKET = "options (consolidated feed)"
INDEX_URL = "https://www.opraplan.com"
NORMAL_CLOSE_ET = "16:00"   # regular options close; "N hours early" is relative to this
MAX_ATTACH_DAYS = 3

SCHEDULE_LINK_RE = re.compile(r"holiday[\s_-]*schedule", re.I)
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
TITLE_RE = re.compile(r"((?:19|20)\d{2})\s+HOLIDAY\s+SCHEDULE", re.I)
MONTHS = "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"
ROW_RE = re.compile(
    rf"^(?P<name>.+?)\s+(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,\s*(?P<year>\d{{4}}))?\s*(?P<marker>\**)\s*(?P<tail>\(observed\))?\s*$", re.I)
FOOTNOTE_START_RE = re.compile(r"^(?P<marker>\*+)\s*(?P<text>.*)$")
OBSERVED_RE = re.compile(r"\s*\(observed\)\s*", re.I)
FOOT_DATE_RE = re.compile(
    rf"(?P<weekday>(?:MON|TUES|WEDNES|THURS|FRI|SATUR|SUN)DAY),?\s+(?P<month>{MONTHS})\s+"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})", re.I)
HOURS_EARLY_RE = re.compile(r"close\s+(?P<n>\d+)\s+hours?\s+early", re.I)
EARLY_SENTENCE_RE = re.compile(r"(?:each\s+)?markets?\s+will\s+close[^.:]*?early", re.I)


# ----------------------------------------------------------------- discovery
def newest_schedule(index_html: str, base_url: str = INDEX_URL) -> tuple[int, str]:
    """(year, url) of the holiday-schedule link with the highest year (first listed on ties)."""
    soup = BeautifulSoup(index_html, "lxml")
    cands = []
    for pos, a in enumerate(soup.find_all("a", href=True)):
        text = clean_text(a.get_text(" "))
        url = urljoin(base_url + "/", a["href"].strip())
        m = YEAR_RE.search(text) or YEAR_RE.search(url.rsplit("/", 1)[-1])
        if SCHEDULE_LINK_RE.search(f"{text} {url}") and m:
            cands.append((int(m[1]), -pos, url))
    if not cands:
        raise RuntimeError("No OPRA holiday-schedule links found on " + base_url)
    year, _, url = max(cands)
    log.info("OPRA schedules listed: %s; using %d", ", ".join(sorted({str(c[0]) for c in cands})), year)
    return year, url


# ----------------------------------------------------------------- PDF parsing
def _title_case(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:].lower() for w in clean_text(s).split())


def _early_close_time(text: str) -> str | None:
    """'close 3 hours early' -> '13:00' (relative to the normal 16:00 ET close)."""
    if m := HOURS_EARLY_RE.search(text):
        base = datetime.strptime(NORMAL_CLOSE_ET, "%H:%M")
        return (base - timedelta(hours=int(m["n"]))).strftime("%H:%M")
    return None


def _early_wording(text: str) -> str:
    m = EARLY_SENTENCE_RE.search(text)
    return clean_text(m.group(0)) if m else clean_text(text)


def _foot_date(m: re.Match, ctx: str) -> date | None:
    """Footnote date, or None (logged) if invalid or its stated weekday doesn't match."""
    wd, month, day, year = m["weekday"].title(), m["month"].title(), int(m["day"]), int(m["year"])
    try:
        d = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
    except ValueError:
        log.warning("%s: invalid date %r skipped", ctx, m.group(0))
        return None
    if d.strftime("%A") != wd:
        log.warning("%s: %r - %s is a %s, not %s; date skipped", ctx, clean_text(m.group(0)), d, d.strftime("%A"), wd)
        return None
    return d


def parse_pdf(pdf_bytes: bytes, source_url: str, scraped_at: str | None = None,
              expected_year: int | None = None) -> list[dict]:
    """Parse one OPRA holiday-schedule PDF into rows."""
    scraped_at = scraped_at or utc_now_iso()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    lines = [clean_text(l, strip_markers=False) for l in text.splitlines()]
    lines = [l.lstrip("•▪- ").strip() for l in lines if l]

    title = next((TITLE_RE.search(l) for l in lines if TITLE_RE.search(l)), None)
    if not title:
        raise RuntimeError(f"No 'YYYY HOLIDAY SCHEDULE' title in {source_url}")
    year = int(title[1])
    if expected_year and expected_year != year:
        log.warning("Link said %d but PDF title says %d (%s)", expected_year, year, source_url)
    ctx = f"OPRA {year} PDF"

    holidays: list[dict] = []          # closed rows: date, name, notes, marker
    footnotes: dict[str, str] = {}
    current_fn = None
    for line in lines[lines.index(next(l for l in lines if TITLE_RE.search(l))) + 1:]:
        if fm := FOOTNOTE_START_RE.match(line):
            current_fn = fm["marker"]
            footnotes[current_fn] = clean_text(fm["text"], strip_markers=False)
            continue
        if current_fn:  # continuation of a wrapped / bulleted footnote
            footnotes[current_fn] += " " + line
            continue
        m = ROW_RE.match(line)
        if not m:
            log.info("%s: unrecognised line ignored: %r", ctx, line)
            continue
        raw_name = m["name"]
        marker = m["marker"] or ("".join(re.findall(r"\*+", raw_name)) or "")
        observed = bool(OBSERVED_RE.search(raw_name) or m["tail"])
        name = _title_case(OBSERVED_RE.sub(" ", clean_text(raw_name)))
        row_year = int(m["year"]) if m["year"] else year
        d = datetime.strptime(f"{m['month'].title()} {m['day']} {row_year}", "%B %d %Y").date()
        holidays.append({"date": d, "name": name, "marker": marker,
                         "notes": f"{name} observed" if observed else ""})

    log.info("%s: %d holiday rows, footnotes %s", ctx, len(holidays), sorted(footnotes, key=len))

    def row(d: date, holiday: str, status: str, close_time="", notes=""):
        return {"exchange": EXCHANGE, "market": MARKET, "date": d.isoformat(), "holiday": holiday,
                "status": status, "close_time_et": close_time, "notes": notes,
                "source_url": source_url, "scraped_at_utc": scraped_at}

    out = []
    for h in holidays:
        fn = footnotes.get(h["marker"], "") if h["marker"] else ""
        notes = h["notes"]
        if fn and not _early_close_time(fn) and re.search(r"observed", fn, re.I) and not notes:
            notes = f"{h['name']} observed"
        out.append(row(h["date"], h["name"], "closed", notes=notes))

    def name_for(d: date) -> str:
        near = min(holidays, key=lambda h: abs((h["date"] - d).days), default=None)
        if near and abs((near["date"] - d).days) <= MAX_ATTACH_DAYS:
            return early_close_name(near["name"], d, near["date"])
        log.warning("%s: early close %s not adjacent to a holiday", ctx, d)
        return "Early close"

    early_seen: set[date] = set()
    for marker, fn in footnotes.items():
        t = _early_close_time(fn)
        if not t:
            continue
        wording = _early_wording(fn)
        for fm in FOOT_DATE_RE.finditer(fn):
            d = _foot_date(fm, f"{ctx} footnote {marker}")
            if d is None or d in early_seen:
                continue
            early_seen.add(d)
            out.append(row(d, name_for(d), "early_close", t, wording))
    return out


# ----------------------------------------------------------------- entry point
def proxy_from_nasdaq(nasdaq_rows: list[dict], year: int) -> list[dict]:
    """NASDAQ's rows for `year`, relabelled as OPRA and clearly marked as a proxy."""
    note = f"PROXY: OPRA has not published {year}; dates from Nasdaq's equity and options calendar"
    return [{**r, "exchange": EXCHANGE, "market": MARKET, "notes": note}
            for r in nasdaq_rows if r["date"].startswith(str(year))]


def scrape() -> list[dict]:
    year, url = newest_schedule(fetch(INDEX_URL, EXCHANGE))
    rows = parse_pdf(fetch_bytes(url, EXCHANGE, tag=str(year)), url, expected_year=year)
    this_year = datetime.now(timezone.utc).year
    if year < this_year:
        log.warning("OPRA has not published %d holiday schedule; latest is %d - using NASDAQ proxy",
                    this_year, year)
        rows = [r for r in rows if not r["date"].startswith(str(this_year))]  # e.g. Jan 1 carry-over
        rows += proxy_from_nasdaq(nasdaq.scrape(), this_year)
    return normalize_rows(rows)
