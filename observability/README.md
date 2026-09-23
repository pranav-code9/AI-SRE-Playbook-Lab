# Observing the agent (Chapter 7)

The agent is a production service, so it gets what every production service
gets: traces, metrics, SLOs, alerts, a dashboard and a runbook.

| Path | What it holds |
|---|---|
| `../agent/src/investigator/telemetry.py` | The agent's own tracing and metrics (OpenTelemetry) |
| `prometheus/sre-agent-rules.yaml` | Recording rules and alerts; `sre-agent-rules.test.yaml` unit-tests them |
| `grafana/sre-agent-dashboard.json` | Dashboard: outcomes, time, tool calls and failures, guard trips, tokens, spend |
| `slos.yaml` | The agent's SLOs, each with its indicator and data source |
| `runbooks/agent-misbehaving.md` | What to do when the agent is the problem |
| `src/sre_obs/overlap.py` | Detects other automations changing a release near the agent's own actions |

## Sending the agent's telemetry to the lab

The agent's traces and metrics go to the same OpenTelemetry Collector as the
demo's services, so they land in the same Jaeger and Prometheus the agent
investigates. Port-forward the collector's OTLP gRPC port (VERIFY the service
name in your chart version) and point the agent at it:

```bash
pip install -e "../agent[otel]"
kubectl -n otel-demo port-forward svc/otel-collector 4317:4317
investigate --live ... --otlp-endpoint localhost:4317
```

In Jaeger, look for service `sre-investigator`. Load the rules into Prometheus
and import the dashboard into Grafana. Metric names assume the collector's
Prometheus export (dots to underscores, `_total` on counters); check them
against what actually arrives.

## Tests

```bash
pip install -e ../agent -e ".[dev]"
pytest
promtool check rules prometheus/sre-agent-rules.yaml
(cd prometheus && promtool test rules sre-agent-rules.test.yaml)
```

## Overlaps

```bash
sre-obs overlaps --audit ../.sre-policy/audit.jsonl --live
```

Lists changes by anyone else to a release within 30 minutes of the agent's own
executions, and executions on one release by different actors. Exits non-zero
when it finds any, so it can run on a schedule.
