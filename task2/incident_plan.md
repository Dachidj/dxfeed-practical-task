# dxFeed Task 2 — Incident Investigation Plan

## Summary

Leading hypothesis (not confirmed): slow database work holds connections, the pool fills up (100/100), and `/api/orders` requests appear to hit a timeout of about 5 s and fail with 502. My order of work: scope the impact, communicate, reduce the pain, then confirm the root cause. I escalate at the 30-minute mark because errors are still 5–8% and the fix needs database and service owners.

This is a scenario, not a live incident. The steps describe what I would do with access to the logs, the metrics dashboard and the service owners.

## Reading the evidence

| Clue | What it tells me | What it doesn't prove |
| --- | --- | --- |
| Traffic normal, no deploy in 24 h | Not a load spike, not a new release | Data, jobs, config or infrastructure can still have changed |
| 502 after 5.1–5.3 s, "upstream timeout" | Likely a timeout of about 5 s: two failures just over 5 s, plus "context deadline exceeded" | The exact limit, which layer enforces it, or where the time is spent |
| Slow query 4800 ms | If the limit is ~5 s, this one query alone would use ~96% of it | Whether it's the cause, or slow because the DB is overloaded |
| Pool 100 active / 0 idle | New requests must wait for a free connection | Which pool: API → internal-service, or internal-service → DB |
| `retrying request to internal-service` | Our own API retries, sending the same heavy work again | How many retries per request, and with what delay |
| CPU 95% on 2 of 5 pods | Something is different about those 2 pods | Whether CPU is a cause (a job, one client) or a symptom (queued requests) |
| `/api/health` 200 in 8 ms | The process is alive | That the database path works; it doesn't test it |

**Log timing:**

- The first 502 at 14:02:01 took 5.3 s, so that request started around 14:01:56. The problem began before the alert fired, so the timeline must start earlier than 14:02.
- Slow-query warnings are usually written when the query finishes. This one likely started around 14:02:06, after the first failures. So this single query can't explain the first 502. It may be one of many slow queries, or a symptom of a database that was already struggling.
- Only 7% of requests fail, not 100%. A likely explanation: requests that get a connection quickly still finish in time, while requests that wait pass the timeout. That would explain why the errors are intermittent.

**Likely failure chain (to be confirmed):**

1. Database work slows down: a query takes 4.8 s instead of milliseconds.
2. Connections are held much longer, and the pool fills up (100/100).
3. New requests wait for a connection and pass the timeout (likely ~5 s). The gateway returns 502.
4. When the API gives up, the database may keep running a query nobody is waiting for, still holding the connection.
5. The API retries, adding the same heavy work to a pool that is already full. The jam gets worse.

## 1. Investigation plan

### First 5 minutes

1. **Scope the impact.** On the dashboard, split the error rate by endpoint, pod, customer and over time. Is it only `/api/orders`? Only the 2 hot pods? One customer or many? Reads only, or order writes too? Getting better or worse? *Why first:* this decides urgency, who must be told, and which fixes are safe. Draining 2 pods only helps if the errors are concentrated there.
2. **Update the customer.** A short, honest message: *"We're aware of intermittent 502 errors on /api/orders and are actively investigating. Next update by 14:30 at the latest. If you can share timestamps or request IDs of failed calls, it will help us pinpoint the issue."* *Why:* an enterprise customer is already affected and waiting. I promise the time of the next update, not a fix time I can't know yet.
3. **Make the team aware.** Open an incident channel and post a short status. Notify the on-call lead, the owners of the internal-service and the database, and the account manager. *Why:* they can prepare early and nobody is surprised later. This is awareness, not escalation yet.

### Then investigate (about 5–20 minutes)

4. **Build a timeline.** Plot errors, latency, per-pod CPU, pool usage, DB latency and retries from 13:45 onwards. *Why:* the cause usually changes before the symptoms. If pool usage rose before CPU, CPU is likely a symptom.
5. **Check what changed without a deploy.** Scheduled jobs and backups around 14:00, config or feature-flag changes, database maintenance or failover, data growth, an unusual workload from one client such as a large export. *Why:* "no deploy" only means our code didn't change.
6. **Compare the 2 hot pods with the 3 normal ones.** Which processes use the CPU, which requests and customers hit them, and whether their configuration is identical. *Why:* whatever differs between hot and normal pods is the strongest lead.
7. **Find whose pool is full and why.** Check how long connections are held, how many requests wait for one, and how long they wait. In the database, list the queries running right now and for how long. Confirm the actual timeout values in the config, and check whether all failed requests end near 5 s. *Why:* waiting for a connection and running a slow query are different problems with different fixes.
8. **Examine the slow query.** Which query, how often it runs, and its query plan (missing index? table grown?). Check whether all queries got slower or just this one. *Why:* one slow query points to a query problem (H1); everything slow points to an overloaded database (H3).
9. **Measure the retries.** Retries per request, the delay between them, and whether order writes are retried. *Why:* retries multiply the load, and retrying an order write could create a duplicate order.

### Hypotheses

| Hypothesis | Supporting evidence | How I would test it |
| --- | --- | --- |
| H1: a slow query holds connections and fills the pool | 4800 ms query, pool 100/100, ~5 s timeouts | Compare connection wait with query time; list active long queries; check the query plan |
| H2: something special runs on the 2 hot pods | CPU 95% on 2 of 5 only, traffic normal overall | Compare processes, requests and config between hot and normal pods; check jobs at 14:00 |
| H3: the database itself is struggling; the slow query is a symptom | Slow query logged after the first failures | Check DB CPU, I/O and locks; are all queries slower? |
| H4: retries amplify the incident | Retry warnings during the incident | Count retries per request; compare with pool pressure over time |

H1 fits most of the evidence, so I start there, but I keep the others open until the data confirms one.

## Mitigation while investigating

The customer cares that it works again, so I reduce the pain in parallel with finding the cause. Every action should be reversible and based on evidence.

| Action | When it helps | Risk |
| --- | --- | --- |
| Capture evidence first: thread dumps, active DB queries, graph screenshots | Always, before any change | None; takes 1–2 minutes |
| Kill the long-running queries (with the DBA) | A specific query is holding connections | Order writes inside them fail or roll back; needs DBA approval |
| Drain the 2 hot pods from the load balancer | Errors are concentrated on those pods | The other 3 take their load; check they have capacity first |
| Pause a job or limit one client's heavy requests | One job or client is found to trigger it | Affects that job or client; needs approval and a heads-up to them |
| Lower retries or add a delay between them | Retries clearly add pressure | A config change in production; must be reversible |

**What I would not do:**

- **Restart pods blindly.** It wipes the evidence, moves load to the healthy pods (which may then overload too), and if the slow query keeps coming the problem returns within minutes.
- **Raise the pool from 100 to 200.** If the database is the bottleneck, twice as many slow queries hit it at once, and everything gets slower. It's like adding lanes onto a bridge that is already the bottleneck.

After each action I watch the error rate and latency for a few minutes to confirm it helped, and I post the result in the incident channel.

## 2. Escalation decision

**Decision: yes, I escalate at 14:32 at the latest.** After 30 minutes the error rate still moves between 5% and 8%, CPU is still high on 2 pods, an enterprise customer is affected, and my mitigations haven't resolved it. The likely fix (the query, the database, the internal-service) needs access and knowledge that support doesn't have. The 30-minute mark is the latest point for formal escalation, not the first time the team hears about it.

### Timeline

| Time | What I do |
| --- | --- |
| 14:02–14:05 | Acknowledge the alert, scope the impact, update the customer, open the incident channel |
| 14:05–14:10 | Make the on-call lead, service and DB owners and the account manager aware |
| 14:10–14:30 | Investigate, apply safe mitigations, update the channel after each one |
| 14:30 | Customer update, as promised |
| 14:32 | Formal escalation, because the issue isn't resolved |

**I would escalate immediately, without waiting,** if errors keep rising, more customers or endpoints are affected, order writes fail or duplicate, data integrity is at risk, or an SLA limit is close.

**I would not escalate as an emergency** if the mitigations bring the error rate back to ~0.2% and the customer confirms the 502s stopped. The root cause would still go to the owning team as a high-priority ticket, because the slow query is still there.

### To whom

- **L2 / on-call engineer** for the API: owns the service and its configuration
- **DBA or database team:** the slow query, the pool and the database load are their area
- **Internal-service owners:** the upstream that times out
- **Incident manager or team lead,** if it's classed as a major incident or an SLA is at risk
- The **account manager** stays in the loop to manage the enterprise customer

### What the escalation includes

The goal: the next person can act without asking me a single question.

1. **Impact:** `/api/orders` returning intermittent 502s, error rate 5–8% since about 14:01, which customers are affected, and the trend.
2. **Evidence:** pool 100/100, the slow query (4800 ms) with its text, timeouts at ~5 s (value to confirm), CPU 95% on which 2 pods, retries to the internal-service, and the health check that stays green.
3. **Actions taken and their results,** e.g. "killed long-running queries at 14:15: errors dropped to 3%, back to 6% by 14:25."
4. **Hypothesis,** clearly marked as unconfirmed, plus what is ruled out: traffic spike, deploy.
5. **The ask:** what I need from each team, e.g. "DBA: check the query plan and locks; internal-service team: can we lower retries?"

Escalating isn't handing off. I keep investigating, keep the incident channel updated, and keep sending the customer updates at the times I promised.

## 3. Preventive improvements

| # | Improvement | Type | Effort |
| --- | --- | --- | --- |
| 1 | Fix the slow query and add a query timeout | Prevents recurrence | Low–Medium |
| 2 | Alert on causes, not just errors | Improves detection | Low |
| 3 | A readiness check that tests the real path | Reduces impact | Medium |

### 1. Fix the slow query and add a query timeout

- **Problem:** one query takes 4.8 s, likely almost the whole request budget (the timeout appears to be ~5 s). When the API gives up, the database can keep running the query and holding the connection for an answer nobody receives. The timeouts of the layers don't match.
- **Fix:** find why the query is slow (missing index, grown table, bad plan) and fix it. Set a database query timeout below the API deadline, e.g. 3 s, so no single query can block a connection for seconds again.
- **Why it helps here:** it removes the cause of the pool filling up. The timeout also protects against slow queries we don't know about yet.
- **Trade-off:** too short a timeout kills legitimate long queries, such as reports, so it should be set per workload.

### 2. Alert on causes, not just errors

- **Problem:** we found out from a 7% error rate and a customer complaint, which is too late. Pool saturation was invisible until it hit 100/100.
- **Fix:** per-pod metrics and alerts for pool usage (warning above about 80% for 2 minutes), time spent waiting for a connection, slow queries over 1 s, and p95 latency per pod and per endpoint.
- **Why it helps here:** the pool would have warned us before it was full, and "2 of 5 pods are hot" would be visible at a glance. That shortens the time customers are affected.
- **Trade-off:** thresholds need tuning for normal busy periods. Alerts that fire too often teach people to ignore them.

### 3. A readiness check that tests the real path

- **Problem:** `/api/health` answered 200 in 8 ms while real requests failed. It only checks that the process is alive, so the load balancer kept sending traffic to struggling pods.
- **Fix:** keep two checks. A **liveness** check ("is the process alive?") stays shallow. A **readiness** check ("can I serve real requests?") gets a DB connection and runs a tiny query within a short timeout. A pod that fails readiness stops receiving traffic until it recovers.
- **Why it helps here:** the 2 struggling pods would have been taken out of traffic automatically, so fewer customer requests would have failed.
- **Trade-off:** if the database is fully down, every pod fails readiness at once and the service disappears entirely. The check needs a guard so that not all pods are removed at the same time.

**Next steps beyond these three:** retries with exponential backoff and a retry limit, a circuit breaker on the internal-service call, idempotency keys so a retried order can't be created twice, and request tracing (API → internal-service → DB) to see exactly where the 5 seconds go. After the incident, a short blameless post-mortem with the timeline, root cause and follow-up owners.

## Limitations

- Only the scenario and a small log sample are available. They don't show which pool is full, which query is slow, or whether CPU rose before or after the errors.
- The slow query may be the cause or a consequence of wider database load. That's why the root cause stays a hypothesis until it's confirmed with connection and database metrics.
- The scenario doesn't say whether order writes are affected. That would need to be checked early, because failed or duplicated orders raise the severity.

## Conclusion

The evidence most likely points to the internal-service and database path: slow database work holding connections, a full pool, and requests timing out (likely at ~5 s), made worse by retries. A misleading health check let it continue unnoticed by the load balancer. I would scope first, keep the customer and team informed, reduce the pain with safe reversible steps, and escalate with a complete package at 30 minutes. Long-term, the fix is the slow query plus aligned timeouts, earlier alerts on the real causes, and a health check that tests the real path.
