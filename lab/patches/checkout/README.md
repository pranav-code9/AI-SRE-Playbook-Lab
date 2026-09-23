# Checkout patch

`payment_retry.go` adds `chargeWithRetry`, which wraps checkout's call to the
payment service's `Charge` method in a configurable timeout-and-retry loop.

## Apply

1. Clone `open-telemetry/opentelemetry-demo` at the tag matching the pinned
   chart's `appVersion`, and set `DEMO_SRC` in `lab/config.env`.
2. In `src/checkout/main.go`, find the function that calls
   `cs.paymentSvcClient.Charge(...)` (named `chargeCard` in past versions) and
   change that call to `cs.chargeWithRetry(ctx, req)`, passing the same
   `ChargeRequest`. That one-line change is the whole edit to upstream code.
3. Run `make build`. It copies `payment_retry.go` into `src/checkout/`,
   checks that main.go calls `chargeWithRetry`, then builds and pushes the image.
4. Save the edit as `checkout.diff` in this folder
   (`git -C $DEMO_SRC diff > checkout.diff`) so readers can apply it directly.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PAYMENT_TIMEOUT` | `2s` | Per-attempt timeout (Go duration, e.g. `40ms`) |
| `PAYMENT_MAX_RETRIES` | `0` | Immediate retries after a failed attempt |

With neither variable set, checkout behaves like upstream apart from the 2s
per-attempt timeout.

No extra tracing is needed: checkout's outgoing gRPC calls are already
instrumented, so each retry appears as its own `Charge` span in Jaeger.

This file is derived from Apache-2.0 code in the OpenTelemetry Demo; keep the
license header.
