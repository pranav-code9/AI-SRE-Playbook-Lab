# Runbook: checkout errors

**Owner:** Checkout on-call · **Version:** 3 · **Alert:** `checkout-error-rate-slo`

The lab's example of a team runbook, written the way many are: a mix of things
to look at, things to decide, and things to do. Chapter 4 audits it and turns
the parts a tool can do into tools. `checkout-errors.yaml` is the encoded
version and must stay in step with this page.

## When this fires

Checkout's error rate has been above 5% for 10 minutes. Orders are failing.

## Steps

1. **Confirm the symptom.** Open the checkout dashboard. Check error rate,
   latency and request rate. If traffic has also dropped sharply, suspect the
   frontend instead and page web on-call.
2. **Check what checkout depends on.** Look at payment, cart, currency,
   shipping and email. Payment is the usual suspect.
3. **Check for recent deploys.** Run `helm history otel-demo -n otel-demo`.
   Anything in the last hour is a lead.
4. **Look at a failed order.** Find a failed checkout in Jaeger and read the
   trace.
5. **Check the payment provider's status page.** If they report an incident,
   follow the provider-outage runbook instead.
6. **Decide.**
   - A deploy in the last hour, and errors started after it: roll it back
     (`helm rollback otel-demo <previous revision> -n otel-demo`). Get the
     incident commander's OK first.
   - Payment provider outage: turn on the checkout maintenance banner.
   - Neither: page the payments team.
7. **Keep the incident channel updated** every 15 minutes.
