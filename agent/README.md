# Investigation agent (Chapter 3)

A LangGraph agent that investigates an alert with read-only tools and writes a
root-cause report: a causal chain, the evidence behind every claim, and what it
ruled out. It never changes anything; it only proposes actions.

`DESIGN.md` explains the decisions. This file covers running it.

## Quick start (offline, no model, no cluster)

```bash
cd agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest

investigate --scenario scenarios/checkout_retry_storm.json \
            --scripted scenarios/checkout_retry_storm.script.json
```

That replays the book's case study: a recorded version of the checkout retry
storm, with scripted model responses. It finds the Helm release in two
iterations and prints the report, hypotheses and evidence. The full state and
every raw tool result are written to `runs/<timestamp>/`.

To see the failure Chapter 3 opens with, replay the baseline: the same graph
with its structural rules switched off, and a model that blames payment.

```bash
investigate --scenario scenarios/checkout_retry_storm.json \
            --scripted scenarios/checkout_retry_storm.baseline.script.json \
            --no-structural-rules
```

Chapter 7's looping model, replayed with and without the loop guards:

```bash
investigate --scenario scenarios/checkout_retry_storm.json \
            --scripted scenarios/checkout_retry_storm.looping.script.json            # guards on
investigate --scenario scenarios/checkout_retry_storm.json \
            --scripted scenarios/checkout_retry_storm.looping.script.json --no-guards
```

## With a real model

Install the LangChain integration for your provider, then pass any model
string `init_chat_model` accepts:

```bash
pip install -e ".[anthropic]"     # or .[openai], .[google]
investigate --scenario scenarios/checkout_retry_storm.json \
            --model <provider>:<model-name> \
            --price-in <usd-per-million-input-tokens> --price-out <usd-per-million-output-tokens>
```

The recorded scenario is still used, so the only variable is the model. Prices
feed the cost budget (`--max-cost`, default $1); without them cost isn't
limited.

## Against the lab

```bash
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080      # Jaeger at /jaeger/ui
kubectl -n otel-demo port-forward svc/prometheus 9090:9090          # VERIFY the service name
investigate --live --service checkout --namespace otel-demo \
            --symptom "checkout error rate above SLO" \
            --alert-start <ISO time the alert fired> \
            --model <provider>:<model-name>
```

`helm` and `kubectl` must point at the lab cluster.

Don't give the agent permission to read Secrets. Helm stores release history
in Secrets and Kubernetes can't limit access to Helm's alone, so run the change
exporter under its own identity (`lab/k8s/rbac.yaml`) and point the agent at it:

```bash
sre-change-exporter --port 8099        # as the sre-change-exporter account
investigate --live ... --changes-url http://localhost:8099
```

### Topology graph (Chapter 2)

By default the live agent reads service dependencies from Jaeger's dependency
view. To use the Neo4j topology graph instead, run Neo4j locally and keep the
builder running alongside the lab:

```bash
pip install -e ".[neo4j]"
docker run -d --name ai-sre-neo4j -p 7474:7474 -p 7687:7687 \
    -e NEO4J_AUTH=neo4j/<password> neo4j:5
build-topology --neo4j-password <password> --every 300
```

Then add `--neo4j-uri bolt://localhost:7687 --neo4j-password <password>` to
`investigate --live`. The agent only sees edges observed in the last 24 hours.
The builder is unit-tested with a fake driver; its Cypher hasn't yet run
against a live Neo4j.

### Before running live

The live backends (`src/investigator/tools/live.py`) haven't been run against
a real cluster yet; their parsing is unit-tested with sample payloads only.
Check these against the demo version you pin:

- **Metric names.** `RED_QUERIES` assumes span metrics named
  `traces_span_metrics_calls_total` and
  `traces_span_metrics_duration_milliseconds_bucket`, with `service_name`,
  `span_kind` and `status_code` labels.
- **Service names.** They must match what Jaeger and Prometheus report (for
  example `checkout` rather than `checkoutservice`).
- **Logs.** No log backend is wired up yet (DESIGN.md, Decisions).
  `search_logs` returns a failed result, which the agent treats as missing
  evidence.
- **Permissions.** Run as `sre-agent-read` from `lab/k8s/rbac.yaml`: read-only,
  and no Secrets. Change history comes from the change exporter.

## Layout

| Path | What it holds |
|---|---|
| `src/investigator/state.py` | The investigation state: the schema everything else depends on |
| `src/investigator/rules.py` | Confidence limits and routing: code the model can't override |
| `src/investigator/graph.py` | Nodes and the LangGraph wiring |
| `src/investigator/prompts.py`, `schemas.py` | What the model is told, and the structured output it returns |
| `src/investigator/tools/` | Tool contract, the twelve tools, fixture and live backends |
| `src/investigator/topology.py` | Topology graph: edges from traces, Neo4j writer and backend, builder |
| `scenarios/` | Recorded case study, the script that generates it, and scripted model responses |
| `tests/` | Rules, tools, graph behaviour, and live-backend parsing |

## What the tests pin down

- The case study concludes with root cause H3 and chain H3 → H2 → H1 → H0.
- The Chapter 3 opening failure is blocked: a model that insists payment is
  the root cause gets capped at 0.5, and the agent escalates instead of
  concluding.
- Triage is identical run to run, and prompts are built from state, never a
  transcript.
- The change window widens once when nothing changed recently, and the result
  is recorded.
- Budgets stop the agent; malformed plans and model failures degrade to
  escalation rather than crashes.

## Recorded scenario

`scenarios/checkout_retry_storm.json` is synthetic, generated by
`make_checkout_retry_storm.py` in the shape of the lab. Once the lab runs, replace
it with data captured from a real run; the same file format becomes Chapter 6's
eval input.
