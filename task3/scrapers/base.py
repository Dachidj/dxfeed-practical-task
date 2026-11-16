"""Shared HTTP fetching + snapshotting for all exchange scrapers."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "snapshots"

# Standard browser User-Agent (ran reliably during development). NASDAQ's bot protection
# can temporarily block after several requests in a short time; the run then fails safely.
# CME, which blocks scraping, is not scraped (see README).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT = 20
RETRIES = 3
BACKOFF = 1.5  # sleeps 1.5s, 3s, 6s between attempts


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    retry = Retry(
        total=RETRIES,
        connect=RETRIES,
        read=RETRIES,
        status=RETRIES,
        backoff_factor=BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def min_relevant_year(today: date | None = None) -> int:
    """Oldest year kept in the output: the current calendar year (UTC)."""
    today = today or datetime.now(timezone.utc).date()
    return today.year


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, exchange: str) -> requests.Response:
    log.info("Fetching %s (%s)", url, exchange)
    with _session() as s:
        resp = s.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
    return resp


def _snapshot_path(exchange: str, tag: str, ext: str) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    name = "_".join(p for p in (exchange.lower(), tag, stamp) if p)
    return SNAPSHOT_DIR / f"{name}.{ext}"


def fetch(url: str, exchange: str) -> str:
    """GET url (with retries/backoff), save raw body to snapshots/<exchange>_<YYYYMMDD>.html, return text."""
    resp = _get(url, exchange)
    path = _snapshot_path(exchange, "", "html")
    path.write_text(resp.text, encoding="utf-8")
    log.info("HTTP %s, %d bytes -> %s", resp.status_code, len(resp.content), path.name)
    return resp.text


def fetch_bytes(url: str, exchange: str, tag: str, ext: str = "pdf") -> bytes:
    """Binary variant (PDFs): saves to snapshots/<exchange>_<tag>_<YYYYMMDD>.<ext>."""
    resp = _get(url, exchange)
    path = _snapshot_path(exchange, tag, ext)
    path.write_bytes(resp.content)
    log.info("HTTP %s, %d bytes -> %s", resp.status_code, len(resp.content), path.name)
    return resp.content
