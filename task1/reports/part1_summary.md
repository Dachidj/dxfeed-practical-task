# dxFeed REST demo – Part 1 summary

Data: `tools.dxfeed.com/webservice/rest/events.json` (302 → `demo.dxfeed.com`), 7-day range
2026-11-05 14:14:35 → 2026-11-12 14:14:35 UTC, plus 10-day daily/hourly files.
Rules: clock checked against the server's HTTP `Date` header before every run (local clock
was ~+32 s ahead; scripts use server time), ≤ 25 requests/min (7 s spacing), stop on 429 or
non-OK status, every raw response saved under `data/raw/`.
New York times are EST (UTC−5) throughout. The feed is ~15 min delayed (measured against
the server's `Date` header).

## 1. Aggregation check (rebuild 5m from 1m, 1h from 5m)

| Symbol | 1m → 5m matching | 5m → 1h matching | What differs |
|---|---|---|---|
| AAPL | 1152 / 1152 | 39 / 98 | 59 hours: volume + count only |
| TSLA | 1186 / 1187 | 48 / 98 | 50 hours: volume + count (1–2 also open/high/close) |
| EUR/USD (`price=bid`) | 1398 / 1398 | 125 / 125 | nothing |

- **EUR/USD is perfectly consistent.** Without `price=bid` it returns no candles: forex has
  no exchange trades. Its `volume` equals `count` (number of quotes).
- **For stocks, open/high/low/close agree.** But outside the regular session the bigger candle often holds *more*
  volume and trades than its smaller candles add up to. The one 1m→5m
  mismatch is the same effect (section 4b).

Exactly 0.00% in every hour from 09:00 to 16:00 NY for both stocks. AAPL is up to +40%
around 00:00–02:00 NY and +9–16% at 17:00–19:00. TSLA is smaller (±7%), with 3 hours
where the 1h candle has *less* volume than its 5m candles.

## 2. Weekend (Sat Nov 7 / Sun Nov 8, NY time)

| | Candles on Sat/Sun | Last before weekend | First after weekend |
|---|---|---|---|
| AAPL, TSLA (1m/5m/1h) | none | Fri 19:59 NY (Sat 00:59 UTC), 1m | Mon 00:00 NY (05:00 UTC) |
| EUR/USD (bid) | 458 (1m) | Fri ~17:04 NY (22:04 UTC) | Sun 17:00 NY hour (22:00 UTC) |

- **Stocks:** a clean ~52 h gap. There's no zero volume at the edges, but trading there is thin
  (AAPL: 26 of 60 1m candles missing in the last hour before; 42 of 60 missing in the first hour
  after). Stock candles run 00:00–20:00 NY, and there are none from 20:00 to 24:00.
- **EUR/USD:** quotes fade from 17:00 and stop at 17:04 NY on Friday. One stray quote
  comes on Saturday 09:37 NY. A thin trickle (1–2 quotes/min) runs Sunday 13:21–17:00 NY, then normal
  quoting resumes in the 17:00 NY hour (676 quotes, then ~5,000 at 18:00). The weekend candles are
  real but tiny, with counts of 1–50 versus a median of ~14,000 per hour.

## 3. Daily candles and where a “day” starts

- **Every daily candle is stamped 00:00 UTC** (19:00 NY the evening before). That stamp is a date label,
  not the start of the day.
- **EUR/USD: the day runs 17:00 NY → 17:00 NY.** With that boundary, all 4 price fields match and
  volume matches exactly (ratio 1.0000) on all 7 complete days. Midnight UTC and midnight NY
  don't match. The daily close equals the last 1h candle (16:00–17:00 NY) every day. Monday's
  candle includes the Friday 17:00 hour and the weekend quotes.
- **AAPL/TSLA: the day is the New York trading date, built from two different windows:**
  - prices come from the **regular session 09:30–16:00**: high and low match on 7 of 8 checked
    days, open on 4 of 8;
  - volume covers the **whole extended day** (daily/full-day volume 1.00–1.02, daily/regular 1.2–1.6);
  - the daily close never equals the last 1h candle (19:00 NY) or the last regular 1m close.
    It differs by a few cents, which is consistent with an official closing price. I could not
    reproduce it exactly from candles.

## 4. Quick tests

- **(a) `AAPL{=1h,a=s}` (session-aligned):** yes, the candles start at 09:30 NY and run 09:30, 10:30 …
  14:30, plus a half-hour candle at 15:30–16:00, with no extended hours. All 7 candles on Nov 11 match the
  1m data exactly. Default `{=1h}` candles sit on the hour, 00:00–19:00 NY (20 per day). The two
  sets share no timestamps. Anomaly: **Nov 12 has no 15:30 session candle**, and it was still missing
  on a re-request.
- **(b) TSLA 1m→5m, 12:35 UTC Nov 12 (07:35 NY, pre-market):** open/high/low/close are identical.
  Returned 5m volume is 16,250.75 versus 6,178.58 for its five 1m candles (+10,072.17, 2.6×), and the count is 949 versus
  381 (+568). It's the same effect as section 1, outside the regular session.
- **(c) `TSLA{=1m,price=bid}`, last 30 min:** the server's own time limit is the URL
  parameter `timeout`, which defaults to 3 s.
  - **Default:** HTTP 200 with `{"status":"TIMED_OUT"}` after 3.5 s (3 s + overhead), the same in all 32
    load-test tries.
  - **With `&timeout=10` in the URL** (client timeout 20 s, run 2026-11-13 04:55 NY): still
    `TIMED_OUT`, now after **11.2 s**. The server honors the parameter but finds no data in time.
    This points to TSLA bid candles being unavailable on the demo, not to a request that is just slow.
    `TSLA{=1m}` (trade-based) works.

## 5. Load test (from `load_test.py`)

There were no 429s and no connection errors at 1/2/4/8 parallel requests. Latency rises roughly in proportion
to parallelism (p50 of successful requests: 0.7 → 1.4 → 1.9 → 3.7 s), which suggests the
server queues requests. There's no sign of caching. The 33% error rate is entirely the TSLA bid timeouts
(at the default server timeout of 3 s; see 4c).

## Caveats

- The early 7-day `data/raw/` files carry fetch-time stamps ~42 min late (wrong local clock
  at that time). The candle data inside is unaffected.
- p95 comes from ~16 successful requests per stage, so treat it as a rough figure.
- About 1 week of data: the patterns are consistent, but it's a small sample.
