"""
Step 1 - download Candle data from dxFeed's public REST demo.

For every symbol x timeframe combination it sends one GET request to
    https://tools.dxfeed.com/webservice/rest/events.json
and saves the raw JSON body, byte for byte, to data/raw/.

Usage:
    python fetch.py                                   # last 7 days up to now (UTC)
    python fetch.py --from 2026-11-01 --to 2026-11-08
    python fetch.py --from 2026-11-01T13:30 --to 2026-11-01T20:00
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

# ---------------------------------------------------------------- settings
BASE_URL = "https://tools.dxfeed.com/webservice/rest/events.json"
SYMBOLS = ["AAPL", "TSLA", "EUR/USD"]
TIMEFRAMES = ["{=1m}", "{=5m}", "{=1h}"]
# Extra candle attributes per symbol. Forex has no exchange trades, so the
# default (trade-based) EUR/USD candles are empty; bid-price candles exist.
SYMBOL_ATTRS = {"EUR/USD": "price=bid"}
DELAY_SECONDS = 7           # pause between requests (rate-limit courtesy)
TIMEOUT = (10, 120)          # (connect, read) seconds; 7 days of 1m is ~1.5 MB
DEFAULT_DAYS = 7

PROJECT_DIR = Path(__file__).resolve().parent
RAW_DIR = PROJECT_DIR / "data" / "raw"
LOG_FILE = PROJECT_DIR / "data" / "fetch.log"

log = logging.getLogger("fetch")


class StopFetching(Exception):
    """Raised when a response means we must not send any more requests."""


# ---------------------------------------------------------------- helpers
def parse_time(text: str) -> datetime:
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS]'. Naive input is treated as UTC."""
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad date/time {text!r}; use YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dxfeed_time(dt: datetime) -> str:
    """dxFeed's compact time format, e.g. 20261105-000000Z (Z = UTC)."""
    return dt.strftime("%Y%m%d-%H%M%SZ")


def build_url(candle_symbol: str, start: datetime, end: datetime,
              extra_params: dict | None = None) -> str:
    """
    Build the request URL by hand so the encoding is explicit.
    quote(..., safe='') encodes every reserved character:
        AAPL{=1m}      -> AAPL%7B%3D1m%7D
        EUR/USD{=1h}   -> EUR%2FUSD%7B%3D1h%7D
    Without this, '{', '=' and '/' could be misread by the server.
    extra_params are appended as-is (encoded), e.g. {"timeout": 10} -> &timeout=10,
    the SERVER-side time limit in seconds (the server's default is 3 s).
    """
    url = (f"{BASE_URL}?events=Candle"
           f"&symbols={quote(candle_symbol, safe='')}"
           f"&fromTime={dxfeed_time(start)}"
           f"&toTime={dxfeed_time(end)}")
    for key, value in (extra_params or {}).items():
        url += f"&{quote(str(key), safe='')}={quote(str(value), safe='')}"
    return url


def candle_symbol_for(symbol: str, timeframe: str) -> str:
    """'AAPL' + '{=1m}' -> 'AAPL{=1m}';  'EUR/USD' + '{=1m}' -> 'EUR/USD{=1m,price=bid}'."""
    attrs = SYMBOL_ATTRS.get(symbol)
    return symbol + (timeframe[:-1] + "," + attrs + "}" if attrs else timeframe)


def output_path(symbol: str, timeframe: str, fetched_at: datetime) -> Path:
    """data/raw/AAPL_1m_20261112T132200Z.json  ('/' is not allowed in file names)."""
    safe_symbol = symbol.replace("/", "-")
    tf = timeframe.strip("{}=")                     # '{=1m}' -> '1m'
    stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    return RAW_DIR / f"{safe_symbol}_{tf}_{stamp}.json"


def count_candles(payload: dict) -> int:
    """Response looks like {"status":"OK","Candle":{"AAPL{=1m}":[{...},...]}}."""
    return sum(len(rows) for rows in payload.get("Candle", {}).values())


# ---------------------------------------------------------------- clock check
# A wrong local clock silently breaks time measurements: on 2026-11-12 the
# laptop clock ran ~42 min ahead, so a ~16 min feed delay looked like ~58 min
# and "last 30 minutes" requests asked for data from the future.
CLOCK_URL = "https://tools.dxfeed.com/"
CLOCK_WARN_SECONDS = 2
_clock_offset = timedelta(0)             # local clock minus server clock


def offset_from_response(resp: requests.Response, received_at: datetime) -> float | None:
    """
    Local clock minus server clock in seconds (+ = local clock ahead).
    The HTTP Date header is the server's time when it answered, cut to whole
    seconds, so the true server time is D..D+1 s -> compare with D + 0.5 s.
    Accuracy is about +-1 s plus network latency: enough to catch a wrong clock.
    """
    header = resp.headers.get("Date")
    if not header:
        return None
    server = parsedate_to_datetime(header)
    return (received_at - server).total_seconds() - 0.5


def check_clock(session: requests.Session) -> float:
    """
    Run BEFORE measuring anything time-based. Compares the local clock with the
    server's Date header and logs the difference. Afterwards now_utc() returns
    the server's time, so request windows, file names and delays don't depend
    on the local clock.
    """
    global _clock_offset
    try:
        resp = session.head(CLOCK_URL, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise StopFetching(f"clock check failed: {exc}") from exc
    received = datetime.now(timezone.utc)
    offset = offset_from_response(resp, received)
    if offset is None:
        raise StopFetching("clock check: server sent no Date header")
    _clock_offset = timedelta(seconds=offset)
    log.info("clock check: local %s UTC, server Date '%s' -> local - server = %+.1f s",
             received.strftime("%H:%M:%S.%f")[:-3], resp.headers["Date"], offset)
    if abs(offset) > CLOCK_WARN_SECONDS:
        log.warning("local clock is off by %+.1f s; using the server's time instead", offset)
    return offset


def now_utc() -> datetime:
    """Current UTC time by the server's clock (local clock minus measured offset)."""
    return datetime.now(timezone.utc) - _clock_offset


def setup_logging(log_file: Path) -> None:
    """Log to the console and to a file. Shared with watch_live.py."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_file, encoding="utf-8")],
    )


# ---------------------------------------------------------------- one request
def get_candles(session: requests.Session, candle_symbol: str,
                start: datetime, end: datetime, *, timeout=TIMEOUT,
                raw_path: Path | None = None,
                extra_params: dict | None = None) -> dict:
    """
    Send one request and apply all the safety rules. Returns
    {"body": raw bytes, "candles": count, "server_time": Date header as datetime,
     "clock_offset_s": local minus server, "elapsed_s": duration}.
    If raw_path is given, the body is saved there as soon as it arrives -
    also when it is an error body (e.g. TIMED_OUT), so failures are kept too.
    Raises StopFetching on network error, HTTP 429, any non-2xx code,
    non-JSON body, or a JSON status other than "OK".
    Shared by all scripts so they follow identical rules.
    """
    url = build_url(candle_symbol, start, end, extra_params)
    log.info("GET %s", url)
    t0 = time.monotonic()
    try:
        # requests follows the 302 to demo.dxfeed.com automatically
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as exc:      # timeout, DNS, connection reset...
        raise StopFetching(f"{candle_symbol}: request failed after "
                           f"{time.monotonic() - t0:.1f}s: {exc}") from exc
    elapsed = time.monotonic() - t0
    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(resp.content)         # raw body, untouched
        log.info("  saved %s", raw_path.resolve().relative_to(PROJECT_DIR))
    offset = offset_from_response(resp, datetime.now(timezone.utc))
    server_time = (parsedate_to_datetime(resp.headers["Date"])
                   if "Date" in resp.headers else None)

    if resp.status_code == 429:
        raise StopFetching(f"{candle_symbol}: HTTP 429 Too Many Requests "
                           f"(Retry-After={resp.headers.get('Retry-After')})")
    if not resp.ok:
        raise StopFetching(f"{candle_symbol}: HTTP {resp.status_code} {resp.reason}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise StopFetching(f"{candle_symbol}: response is not JSON") from exc

    # HTTP 200 does not mean success: the body has its own status
    # (e.g. "TIMED_OUT"). Anything other than "OK" stops the run too.
    api_status = payload.get("status")
    n = count_candles(payload)
    log.info("  -> HTTP %d, status=%s, %d candles, %.1fs, %d bytes, "
             "server Date %s, local - server %s",
             resp.status_code, api_status, n, elapsed, len(resp.content),
             resp.headers.get("Date", "-"),
             f"{offset:+.1f} s" if offset is not None else "n/a")
    if api_status != "OK":
        raise StopFetching(f"{candle_symbol}: API status {api_status!r} "
                           f"after {elapsed:.1f}s")
    return {"body": resp.content, "candles": n, "elapsed_s": elapsed,
            "server_time": server_time, "clock_offset_s": offset}


def fetch_one(session: requests.Session, symbol: str, timeframe: str,
              start: datetime, end: datetime) -> None:
    candle_symbol = candle_symbol_for(symbol, timeframe)
    fetched_at = now_utc()                         # server-corrected time
    path = output_path(symbol, timeframe, fetched_at)
    n = get_candles(session, candle_symbol, start, end, raw_path=path)["candles"]
    if n == 0:
        log.warning("  %s returned no candles (symbol may not be on the demo feed)",
                    candle_symbol)


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="Download dxFeed demo Candle data.")
    parser.add_argument("--from", dest="start", type=parse_time,
                        help="start (UTC), default: 7 days ago")
    parser.add_argument("--to", dest="end", type=parse_time,
                        help="end (UTC), default: now (by the server's clock)")
    parser.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=SYMBOLS,
                        help="only fetch these symbols (default: all)")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(LOG_FILE)

    jobs = [(s, tf) for s in args.symbols for tf in TIMEFRAMES]
    with requests.Session() as session:
        try:
            check_clock(session)                   # before any time is computed
        except StopFetching as exc:
            log.error("STOPPING: %s", exc)
            return 1
        now = now_utc().replace(microsecond=0)
        args.end = args.end or now
        args.start = args.start or args.end - timedelta(days=DEFAULT_DAYS)
        if args.start >= args.end:
            parser.error("--from must be earlier than --to")
        log.info("range %s -> %s", args.start.isoformat(), args.end.isoformat())

        for i, (symbol, tf) in enumerate(jobs):
            if i > 0:
                time.sleep(DELAY_SECONDS)
            try:
                fetch_one(session, symbol, tf, args.start, args.end)
            except StopFetching as exc:
                log.error("STOPPING: %s", exc)
                return 1
    log.info("done: %d requests", len(jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
