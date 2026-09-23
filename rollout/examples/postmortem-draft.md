# Postmortem: Checkout retry storm, again

**Status:** DRAFT, assembled from the agent's investigation and the audit log. Every section needs a human check. Sections marked TO WRITE are for the team.

This review is blameless. It asks how the system made this failure possible and how it was found and fixed, not who made a mistake. People appear by role.

## Summary

_The agent's summary, to be checked and rewritten by the team:_

> Release otel-demo revision 7 (10:05) cut checkout's payment timeout from 2s to 40ms and enabled five immediate retries (E13). Normal payment calls now exceed the timeout, so each order makes up to six Charge attempts (E12, E15), multiplying payment traffic about fivefold (E14). Payment slows under the extra load, more calls time out, and checkout errors rise from the moment of the release (E16). Autoscaling and node memory pressure followed (E11).

## Impact

TO WRITE: who was affected, for how long, and how badly. Starting points from the evidence:

- E1: checkout 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 5.0 req/s, errors 18.2%, p50 150 ms, p95 565 ms (window averages).
- E16: checkout error_rate: 0.4% before, 34.3% after (+8476%).

## Timeline

| Time (UTC) | What happened |
|---|---|
| 10:05 | otel-demo revision 7 deployed (E10) |
| 10:05 | checkout PAYMENT_TIMEOUT 2s -> 40ms, PAYMENT_MAX_RETRIES 0 -> 5 (E13) |
| 10:20 | Alert `checkout-error-rate-slo` fired: checkout error rate above SLO (over 5% for 10 minutes) |
| 10:30 | The agent requested approval for otel-demo 7 -> 6 |
| 10:32 | The incident commander approved the plan |
| 10:32 | rollback_release executed (otel-demo 7 -> 6), started by the incident commander |
| 10:43 | Outcome of the action recorded as good by the automatic verification: checkout error_rate 0.36 -> 0.004 (99% drop; needed 50%; already falling 0% before the action) |

## How the cause was found

The agent concluded with confidence 0.90. Causal chain, root cause first:

1. **Release otel-demo revision 7 changed how checkout calls payment** (root_cause; evidence E13, E12, E15, E16)
2. **Checkout sends payment more requests than users generate, because it retries failed calls** (mechanism; evidence E12, E14, E15)
3. **Payment is slow, so checkout's calls to it time out** (mechanism; evidence E5, E12)
4. **checkout error rate above SLO (over 5% for 10 minutes)** (symptom; evidence E1)

The investigation took 2 iteration(s) and 16 tool calls. Every evidence ID above is listed, with its query, in the appendix.

## Contributing factors

TO WRITE. Ask what made this possible and what made it hard to catch, not who did it. For example: what let this change reach production, what would have shown the problem before customers did, and what made the diagnosis slower than it needed to be.

Questions the investigation left open:

- Was the timeout change intended, and what value was meant?
- Did the evicted pods affect other services?

## What went well, and what was hard

TO WRITE. Facts from the records:

- Approval took 2 minute(s) from request to decision.
- rollback_release ran at 10:32 UTC.
- Its outcome was recorded as **good**: checkout error_rate 0.36 -> 0.004 (99% drop; needed 50%; already falling 0% before the action)

## Action items

TO WRITE, each with an owner (a team, not a person) and a date. Follow-ups the agent proposed that weren't executed:

- Add backoff and a retry budget to checkout's payment client

## The agent's part

TO WRITE. Was its conclusion right? Did it help, or add work? Was anything in its report misleading? Record the verdict so it counts toward the trust ladder:

```bash
sre-policy feedback rollback_release agree|disagree --reason "..."
```

## Appendix: evidence

| ID | Signal | Query | Finding |
|---|---|---|---|
| E1 | metric | `get_service_red(service=checkout)` | checkout 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 5.0 req/s, errors 18.2%, p50 150 ms, p95 565 ms (window averages). |
| E2 | topology | `get_dependencies(service=checkout, direction=downstream, depth=1)` | checkout calls: cart, currency, payment, shipping, email, product-catalog (depth 1). |
| E3 | metric | `get_service_red(service=cart)` | cart 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 18.0 req/s, errors 0.1%, p50 4 ms, p95 12 ms (window averages). |
| E4 | metric | `get_service_red(service=currency)` | currency 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 40.0 req/s, errors 0.0%, p50 1 ms, p95 3 ms (window averages). |
| E5 | metric | `get_service_red(service=payment)` | payment 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 16.0 req/s, errors 1.1%, p50 45 ms, p95 119 ms (window averages). |
| E6 | metric | `get_service_red(service=shipping)` | shipping 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 5.0 req/s, errors 0.0%, p50 3 ms, p95 9 ms (window averages). |
| E7 | metric | `get_service_red(service=email)` | email 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 4.1 req/s, errors 0.0%, p50 20 ms, p95 60 ms (window averages). |
| E8 | metric | `get_service_red(service=product-catalog)` | product-catalog 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: 35.0 req/s, errors 0.0%, p50 2 ms, p95 8 ms (window averages). |
| E9 | trace | `search_traces(service=checkout, errors_only=True, limit=5)` | 5 traces for checkout: 5 with errors, duration 350–350 ms. Example ids: err0000, err0001, err0002. |
| E10 | change | `list_changes()` | 1 change(s) in otel-demo 2026-10-22T09:50:00Z–2026-10-22T10:30:00Z: otel-demo revision 7 at 2026-10-22T10:05:00Z (Upgrade complete). |
| E11 | k8s | `get_k8s_events()` | 6 events in otel-demo: SuccessfulRescale ×3 (first 2026-10-22T10:10:00Z); FailedScheduling ×3 (first 2026-10-22T10:18:00Z); Evicted ×2 (first 2026-10-22T10:16:00Z). |
| E12 | trace | `summarize_trace(trace_id=err0001)` | Trace err0001: 19 spans across 5 services, root frontend/POST /api/checkout 350 ms (error). checkout called payment (oteldemo.PaymentService/Charge) 6 times under one parent (6 failed). Slowest span: checkout/oteldemo.CheckoutService/PlaceOrder 330 ms. 15 spans in error. |
| E13 | change | `diff_release(release=otel-demo, revision_a=6, revision_b=7)` | otel-demo revision 6 → 7: 2 value(s) changed: components.checkout.envOverrides[PAYMENT_MAX_RETRIES].value: '0' → '5'; components.checkout.envOverrides[PAYMENT_TIMEOUT].value: '2s' → '40ms'. |
| E14 | metric | `compare_windows(service=payment, metric=request_rate, before_start=2026-10-22T09:50:00Z, before_end=2026-10-22T10:05:00Z, after_start=2026-10-22T10:10:00Z, after_end=2026-10-22T10:30:00Z)` | payment request_rate: 5.0 req/s before, 26.0 req/s after (+419%). |
| E15 | log | `search_logs(service=checkout, level=error)` | 72 log lines from checkout; top messages: “failed to charge card: charge failed after 6 attempts: rpc error: code = DeadlineExceeded desc = co…” ×72. |
| E16 | metric | `compare_windows(service=checkout, metric=error_rate, before_start=2026-10-22T09:50:00Z, before_end=2026-10-22T10:05:00Z, after_start=2026-10-22T10:10:00Z, after_end=2026-10-22T10:30:00Z)` | checkout error_rate: 0.4% before, 34.3% after (+8476%). |
