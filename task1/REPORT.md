# dxFeed Task 1 — Support Engineer Assessment

## Summary

The dxFeed REST demo delivers consistent prices, but several behaviours can silently mislead a client. Over 12–14 November 2026 I checked a week of AAPL, TSLA and EUR/USD candles, watched the live feed, ran a bounded load test, and confirmed each finding against the dxFeed documentation.

**Most important findings**

1. **Missing data looks like success.** Quote-based candles (`price=bid/ask/mark`) for **TSLA and NVDA** return no data. I saw this on 12–14 Nov, and requests for history back to 20 Oct fail too, so it may be older. The same gap shows up three different ways: HTTP 200 with `TIMED_OUT`, HTTP 200 with an empty `OK`, or an HTML 504 from the proxy. A status-code check reports all of them as healthy.
2. **Timeframes disagree on volume outside regular hours.** Prices aggregate exactly (1m → 5m → 1h), but outside 09:30–16:00 New York the 1h volume and trade count often differ from the sum of the 5m candles. The documentation's trade-validity rules explain why.
3. **Session settings change the numbers.** Default candles include pre-market, after-hours and overnight trading. With `tho=true` the closing auction (the largest print of the day) disappears, and hourly labels start 30 minutes before the data does.
4. **The newest candle is always unfinished.** It changes exactly once more and is then final. The feed is steadily delayed by about 15.4 minutes.
5. **Under load, latency grows; errors don't.** Doubling parallel requests roughly doubled the wait, with no 429s or dropped connections within the documented limits.

**Priority improvement:** per-symbol synthetic monitoring that checks the response body, the candle count and the data age, not just the HTTP status. It would have caught the TSLA/NVDA gap before a client did.

## Scope and method

- **Interface:** the public REST demo, `events.json` (Candle and Quote events). It redirects from tools.dxfeed.com to demo.dxfeed.com, so each data request is two HTTP requests.
- **Instruments:** AAPL and TSLA (US equities) and EUR/USD (FX), at 1m, 5m and 1h, for the last 7 days including a weekend. MSFT and NVDA were added as controls for the quote-candle issue.
- **Scripts (Python):** `fetch.py` (shared requests, saves raw JSON), `check_aggregation.py` (1m → 5m → 1h), `watch_live.py` (polls the newest candles every minute), `load_test.py` (bounded concurrency), `quick_tests.py` (targeted checks). Raw responses are kept in `data/` and results in `reports/`.
- **Staying within limits:** the documented limits are 100 HTTP requests per minute and 10 simultaneous connections. Because of the redirect, I capped my tests at **25 data requests per minute and 8 in parallel**. The goal was to observe behaviour, not to stress a shared demo.
- **Clock safety:** after my laptop clock (42 minutes fast) produced empty answers, every script compares the local clock with the server's `Date` header and builds request windows from server time.
- **Method:** for each observation I wrote a hypothesis, tested it, and marked it confirmed, refined or retracted. One early finding was retracted after re-running the exact documented request; it had come from a differently formatted response.

## Part 1: Data consistency

### 1.1 Aggregation check (7 days, every interval compared)

| Instrument | 1m → 5m matching | 5m → 1h matching |
| --- | --- | --- |
| AAPL | 1152 of 1152 | 39 of 98 |
| TSLA | 1186 of 1187 | 48 of 98 |
| EUR/USD (`price=bid`) | 1398 of 1398 | 125 of 125 |

Open, high, low and close agree almost everywhere. For stocks, the hourly **volume and trade count** differ from the sum of the 5m candles, but **only outside regular hours** (0.00% difference between 09:30 and 16:00 New York). Usually the 1h candle has more volume; in three TSLA hours it has less. The single TSLA 1m → 5m mismatch (12:35 UTC, pre-market) is the same effect. 

**Worked example: AAPL, 11 Nov 2026, 09:00–10:00 UTC (04:00–05:00 New York, pre-market).** The hourly candle compared with its twelve 5-minute candles (first open, highest high, lowest low, last close, summed volume and count):

| Field | 1h candle | From 12 × 5m | Match |
| --- | --: | --: | --- |
| Open | 347.47 | 347.47 | Yes |
| High | 347.47 | 347.47 | Yes |
| Low | 346.34 | 346.34 | Yes |
| Close | 346.52 | 346.52 | Yes |
| Volume | 51,247.67 | 50,330.75 | No, 916.91 short (1.8%) |
| Trade count | 2,330 | 2,169 | No, 161 short (6.9%) |

To rule out a changed response, I re-requested the data: the 5m candles were identical both times, so the difference is in how the candles are built, not in the request.

**Why (documented, partly hypothesis):** dxFeed's sale-condition rules exclude some trades from high/low/last but still count them in volume, and treat extended-hours trades (Form T, late reports) specially. The Candle reference also says common periods come from separately pre-built records. So each timeframe likely applies the validity rules on its own, and extended-hours volume lands in some candles but not others. **Client impact:** a client who rebuilds hourly bars from 5m bars gets different volume than dxFeed's own hourly bars and may report "wrong data".

### 1.2 VWAP outside the price range

**What I saw:** some candles have a VWAP below their low or above their high, e.g. a 5m AAPL candle with open = high = low = close 351.7807 but VWAP 351.7707.

**Why:** the FAQ confirms that excluded trades (e.g. odd lots) don't set high/low but still count in volume and VWAP.

**Impact:** checks that assume low ≤ VWAP ≤ high raise false alerts.

### 1.3 No trades, no candle

**What I saw:** quiet 5-minute intervals (e.g. 06:05 and 06:50 UTC) have no candle at all.

**Why:** documented in the FAQ: no trades in an interval means no candle.

**Impact:** clients expecting one candle every 5 minutes must handle gaps instead of treating them as missing data.

### 1.4 Session settings change the numbers

**What I saw:** default candles include pre-market, after-hours and overnight trading. With `tho=true` (regular hours only):

- the first hourly candle is still labelled 09:00 but contains data from 09:30, so label and data start 30 minutes apart;
- the 16:00 candle with the closing auction (8.3M shares, official close 355.99) disappears, so the last close becomes 355.79.

With `a=s` (session alignment), hourly candles start at 09:30 and match the 1m data exactly. One 15:30 session candle (12 Nov) is missing, even on re-request.

**Impact:** a regular-hours client undercounts daily volume by the day's largest print and sees a different close than the official one.

### 1.5 Different day definitions

**What I saw:** daily candles are stamped 00:00 UTC (19:00 New York the evening before). The EUR/USD day runs 17:00 → 17:00 New York. A stock's daily candle takes prices from regular hours but volume from the whole extended day.

**Impact:** comparing "daily" figures across instruments or with other providers gives different results unless the day boundary is known.

### 1.6 FX needs a price type

**What I saw:** EUR/USD returns `OK` with zero candles by default; with `price=bid` it returns continuous candles.

**Why:** FX has no exchange trades, so the default (last trade) has nothing to build from. The candle page states this, but the REST page a REST user reads does not.

**Impact:** a request that "succeeds" but is empty, a silent failure for anyone who doesn't know the rule.

### 1.7 "Volume" means different things

**What I saw:** for EUR/USD, volume equals the number of quote updates; for AAPL bid-based candles it is `NaN`.

**Why:** the FAQ says FX has no trade volume at all, so here the docs and the data disagree.

**Impact:** analytics that compare volume across asset classes compare different things.

**Expected market behaviour, not errors:** EUR/USD activity drops at the 17:00 New York rollover; stocks show a clean weekend gap; EUR/USD stops Friday 17:04 New York, with one stray Saturday quote.

## Part 2: Real-time behaviour and load

Live feed: two runs of 15 fetches of the newest 1m candles, one per minute.

### 2.1 Steady delay of ~15.4 minutes

**What I saw:** measured against server time, the newest candle was 922–930 s old at every fetch.

**Why:** the documented 15-minute demo delay plus the minute the candle covers.

**Impact:** a reliable baseline for staleness alerts, e.g. alert above 20 minutes during market hours. No freezes: a new candle appeared every minute.

### 2.2 The newest candle is unfinished

**What I saw:** each minute one new candle appears and the previous newest one changes once more, then never again (older candles seen up to 14 times without change). When first seen, a candle had about half its final volume. Close, volume and count change; high and low only when passed; open never.

**Why:** expected behaviour; dxFeed's own Java examples skip unfinished candles.

**Impact:** a client that stores the last candle as final keeps wrong values.

### 2.3 Client-side traps

**What I saw:**

- My laptop slept for 67 minutes during one run. Nothing logged an error, so it would look like a server freeze, and on waking the script sent catch-up requests in a burst.
- A clock running 42 minutes fast asked for "future" data and got HTTP 200 with zero candles.
- A request window starting 11 s late dropped one candle.

**Impact:** monitoring must separate client-side gaps from server-side ones; clients should sync their clocks and align windows to whole minutes.

### 2.4 Load test (102 requests, waves of 1, 2, 4 and 8 in parallel)

Rotating AAPL, TSLA (`price=bid`) and EUR/USD, 1m candles, at most 25 requests per minute. Latency is for successful requests only; timeouts are counted separately, because each one is the server's ~3 s timeout, not normal latency:

| Parallel | OK | Timed out | p50 (OK) | p95 (OK) | Timeout wait (median) |
| --: | --: | --: | --: | --: | --: |
| 1 | 17 | 8 | 731 ms | 1,124 ms | 3.4 s |
| 2 | 16 | 8 | 1,374 ms | 1,855 ms | 3.5 s |
| 4 | 16 | 8 | 1,937 ms | 3,692 ms | 4.3 s |
| 8 | 16 | 8 | 3,723 ms | 7,799 ms | 7.3 s |

The error rate was a flat 32–33% at every level: all errors were TSLA `price=bid` timeouts (one third of the rotation), which fail even with a single request. No 429s and no dropped connections. The redirect hop stayed at ~200–236 ms, so the slowdown is in the data service. 

- **What fails first?** Not the load: a data-source timeout for one symbol and price type, returned as HTTP 200.
- **What degrades first?** Latency. Doubling parallel requests roughly doubles the typical wait.
- **Is it graceful?** Within the documented limits, yes: slower, but no new errors.
- **Linear or exponential?** Roughly linear up to 8 in parallel. I stopped there to stay below the 10-connection limit, so the breaking point was not tested.
- **Caching?** No sign of caching: no cache headers, and the same URL returned a different newest candle a minute later.

### 2.5 Three timeouts, three failure shapes

The service waits for its data source for `timeout` seconds (default 3). With 3 s or 10 s it returns HTTP 200 with `TIMED_OUT`. With `timeout=60` (the value in the docs' own example) an nginx proxy gives up first at ~61 s and returns an **HTML 504 page**, which breaks a JSON parser. A single-exchange request returns HTTP 200 `OK` with no data. The proxy limit and the effective maximum timeout are not documented.

## Part 3: Operational risk review

The biggest risk is not slowness but **failures that look like success**. Each row is grounded in something observed during testing.

| Risk (seen in testing) | How to detect | Mitigation → escalate when |
| --- | --- | --- |
| **Silent failure:** HTTP 200 with `TIMED_OUT`, or `OK` with zero candles (TSLA/NVDA quotes, FX without price type) | Check body status and candle count, not just the HTTP code | Explicit error codes → escalate when one symbol × price type keeps failing while controls work |
| **Data integrity:** hourly volume ≠ sum of 5m outside regular hours; VWAP outside low–high | Automated cross-timeframe checks, split by session | Document how timeframes are built → escalate if prices (not just volume) disagree, or it happens in regular hours |
| **Staleness:** steady ~15.4 min delay | Newest-candle age per symbol, market-calendar aware | Alert above ~20 min in market hours → escalate when many symbols age at once |
| **Latency under load:** p50 0.7 s → 3.7 s at 8 parallel | p50/p95 per endpoint over time | Per-client rate limits, retry back-off → escalate when p95 rises without more traffic |
| **Unfinished data:** newest candle still changes | — (expected) | Document it; clients mark the last candle provisional → escalate only if older candles change |
| **Blind spots:** 504 HTML only with long timeouts; client sleep looked like a freeze; clock skew gave empty answers | Synthetic checks from outside; client time vs server `Date` header | Monitor body content; log client gaps separately → escalate when a client report can't be reproduced |

## Part 4: Incident simulation

**Scenario (built from a real observation):** a client reports that bid/ask charts for TSLA don't load. Investigating it reveals that quote-based candles for TSLA and NVDA are unavailable, and requests for history back to 20 Oct fail too: likely an older problem, not a fresh outage.

### Incident analysis

| Request | Result |
| --- | --- |
| TSLA live Quote | Data (bid on Cboe BZX, ask on Nasdaq, ~15 min old) |
| TSLA trade candles `TSLA{=1m}` | Data |
| TSLA `price=bid` / `ask` / `mark` candles | `TIMED_OUT` (35 of 35 attempts, timeout 3 s and 10 s) |
| NVDA `price=bid` candles | `TIMED_OUT` |
| AAPL and MSFT `price=bid` candles (controls) | Data |
| TSLA bid candles, history from 20 Oct 2026 | `TIMED_OUT` |
| TSLA bid with `timeout=60` | HTML 504 from nginx after ~61 s |

- **Ruled out:** request format (identical requests work for AAPL/MSFT), slowness (no data even after 60 s), missing source data (live TSLA quotes exist), connection problems (the same connection serves other data), and client clock (all checks use server time).
- **Hypotheses:** the quote-candle builder fails for very actively quoted symbols, or is not configured for them. This cannot be proven from outside.
- **Impact:** clients charting bid/ask for two of the most traded US stocks get nothing, with no clear error. Monitoring stays green because every response is HTTP 200.

### Client message

> Hi \[name\], thanks for reporting this. We've confirmed that bid- and ask-based candles for TSLA and NVDA currently don't return data. Trade-based candles for both (e.g. `TSLA{=1m}`) work normally and can be used as a temporary workaround. Our engineering team is investigating, and we'll update you by \[time\], or sooner if we have news.

The workaround keeps the client working, and a specific update time sets expectations. I promise an update, not a fix.

### Internal escalation note (to L2 / data engineering)

> **Impact:** quote-based candles (bid/ask/mark) for TSLA and NVDA return no data; clients get HTTP 200 + `TIMED_OUT`, or an HTML 504 from nginx at `timeout=60`. Monitoring is green. **Seen:** reproduced 12–14 Nov in pre-market and regular hours; history back to 20 Oct 2026 also fails, so the start date is unknown. **Evidence:** 40+ failed requests with saved raw responses; AAPL/MSFT bid candles, TSLA trade candles and live TSLA quotes all work. **Ruled out:** request format, slowness, connection, client clock. **Done:** reproduced, scoped with controls, workaround given to the client. **Need:** an engineer with access to the candle builder's logs and configuration for these symbols.

Listing what is ruled out lets the engineer start where support stopped instead of repeating the same checks.

### Post-mortem outline

- **Summary and timeline,** including when the gap started.
- **Impact:** symbols, price types, affected clients.
- **Root cause:** once known.
- **Why it went unnoticed:** HTTP 200 counted as success; no per-symbol synthetic checks.
- **What went well:** fast scoping with control symbols.
- **Action items:** body-status and candle-count monitoring; synthetic checks per symbol × price type; explicit "no data source" errors; align the documented timeout with the proxy limit.

## Part 5: Three improvements

In priority order:

### 5.1 Monitoring: synthetic checks per symbol and price type

**Problem:** status-code checks report healthy while data is missing or stale (TSLA/NVDA gap, empty FX results, clock-skew answers).

**Change:** every minute, request each important symbol × price type and check the body status, the candle count and the data age (alert above ~20 min in market hours), aware of the market calendar.

**Complexity:** medium. **Impact:** failures are caught before clients report them; the TSLA/NVDA gap would have been found before a client reported it.

**Why first:** it closes the biggest risk, silent failures.

### 5.2 Documentation: one "candle behaviour" page

**Problem:** the docs don't say which symbols support quote candles, that FX needs `price=bid` (on the REST page), how daily candles are built, how timeframes treat extended-hours trades, or the ~60 s proxy limit. The format also has typos (`YYYMMDD`, `toTime=201901014`).

**Change:** one page covering supported price types, FX defaults, `tho=true` vs extended hours, day boundaries per asset class, the unfinished last candle, timeout limits and the meaning of each status; fix the typos.

**Complexity:** low. **Impact:** fewer "your data is wrong" tickets and faster onboarding.

**Why second:** cheap and fast, can run in parallel.

### 5.3 API: explicit errors instead of three failure shapes

**Problem:** missing data appears three different ways: HTTP 200 + `TIMED_OUT`, an HTML 504, or an empty `OK`.

**Change:** always return JSON with an explicit reason such as `NO_DATA_SOURCE` or `UNSUPPORTED_PRICE_TYPE` and a fitting status code; keep the proxy limit above the maximum allowed `timeout`.

**Complexity:** medium (API, proxy, backward compatibility). **Impact:** clients can tell "slow" from "no data".

**Why third:** it fixes the root cause, but changing responses affects existing clients, so it needs planning and a migration period.

## Limitations

- **Public demo only:** no internal logs or metrics, so root causes (e.g. the TSLA/NVDA gap, the timeframe volume differences) remain hypotheses grounded in the documentation.
- **Delayed data:** the demo feed is ~15 minutes delayed; production latency was not measured.
- **Load kept small on purpose:** at most 8 parallel requests and 25 per minute, below the documented limits. The breaking point and rate-limit behaviour were not tested.
- **Not tested:** live streaming (SSE / WebSocket), timestamps without a time zone, whether `toTime` is inclusive, and symbols beyond the six used.
- **Short window:** one week of history and two live sessions; behaviour on holidays or during market events was not observed.

## Evidence

All raw responses, scripts and results are in this folder.

| Item | Where |
| --- | --- |
| Raw API responses | `data/raw/`, `data/live/` |
| Aggregation results | `reports/part1_summary.md` |
| Live feed results | `reports/live_fetches.csv`, `reports/live_candles.csv` |
| Load test results | `reports/load_results.csv` |
| Documentation used | kb.dxfeed.com: REST API, TimeAndSale sale conditions, How to request candles, FAQ, Exchange codes; docs.dxfeed.com: Candle API reference |
