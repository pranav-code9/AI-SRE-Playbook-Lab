# Lab: the checkout retry storm

Reproduces the book's running incident on GKE using the
[OpenTelemetry Demo](https://opentelemetry.io/docs/demo/). A Helm release
shortens checkout's payment-call timeout and enables immediate retries.
Timeouts turn into a retry storm, payment latency climbs, autoscalers add
pods, node pressure causes evictions, and on-call is paged for checkout errors.

## Layout

| Path | What it holds |
|---|---|
| `config.env.example` | All settings; copy to `config.env` (gitignored) |
| `cluster/` | Create and delete the GKE cluster and Artifact Registry repo |
| `patches/checkout/` | The checkout patch that makes the fault configurable |
| `helm/values-lab.yaml` | Healthy release |
| `helm/values-fault.yaml` | The release that causes the incident |
| `k8s/hpa.yaml` | Autoscalers, if the chart doesn't define them |
| `scripts/` | Build, capture and full-scenario scripts |
| `runs/` | Output of scenario runs (gitignored) |

## First run

```bash
cp lab/config.env.example lab/config.env   # fill in project, chart version, demo source path
make cluster
make install          # upstream checkout image until you build the patch
make forward          # store, /jaeger/ui, /grafana, /loadgen on localhost:8080
```

Let the load generator run for 15 minutes, then record the p50 and p95 latency
of checkout's `Charge` calls to payment in Jaeger.

Then apply the patch (see `patches/checkout/README.md`) and:

```bash
make build
# set USE_PATCHED_CHECKOUT=1 in lab/config.env
make install          # now with the patched image
# set PAYMENT_TIMEOUT in helm/values-fault.yaml just below the measured p95
make run
```

`make run` applies the healthy release, waits for a baseline, applies the
fault, waits, captures cluster state at each stage, and rolls back. Stage
timestamps and Helm revisions go to `lab/runs/<run id>/run.env`; use them to
set time windows in Jaeger and Grafana. Ctrl-C during a run rolls back
automatically.

`make run-keep` leaves the incident running, which is what the agent chapters
need. End it with `make rollback`.

## Tuning the cascade

The retry storm appears as soon as the fault release lands. The later stages
need tuning, and settings that work should be recorded here:

- **Load:** raise users in the load generator UI until retries push payment
  latency up, which causes more timeouts.
- **Autoscaling:** `make hpa` if the chart doesn't define HPAs; the targets
  need CPU requests.
- **Evictions:** tighten requests, limits or node size until extra pods
  create real node pressure.

## Before relying on this

The chart and demo change between versions. Against the version you pin, check:

- the checkout function that calls payment's `Charge`, its receiver type and
  client field (`patches/checkout/payment_retry.go`)
- the `components.checkout.imageOverride` and `envOverrides` keys in the
  chart's values.yaml
- the checkout Dockerfile path and build context (`scripts/build-checkout.sh`)
- the deployment names targeted by `k8s/hpa.yaml`

The demo chart doesn't support upgrading between chart versions, which is why
every script uses the pinned `CHART_VERSION`.
