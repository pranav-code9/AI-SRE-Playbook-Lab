# The AI SRE Playbook: companion code

Code for *The AI SRE Playbook: Designing, Governing and Evaluating AI Agents
for Incident Response*.

The book follows one incident, a checkout retry storm on GKE, from the first
page through an agent that investigates it, the guardrails that decide what the
agent may do, and the rollout to an on-call team. Each chapter adds a folder
here.

| Folder | Chapter | Status |
|---|---|---|
| [`lab/`](lab/) | 1–2 | Lab and incident scenario |
| [`agent/`](agent/) | 3 | Working agent; runs offline against a recorded scenario |
| [`mcp-server/`](mcp-server/) | 4 | Read and actions MCP servers, encoded runbook |
| [`policies/`](policies/) | 5 | Trust ladder: policy, approvals, audit, kill switch |
| [`evals/`](evals/) | 6 | Twelve scenarios (ten retry-storm variants plus two other shapes), grader, CI workflow |
| [`observability/`](observability/) | 7 | Agent telemetry, alerts, dashboard, SLOs, runbook |
| [`rollout/`](rollout/) | 8 | Shadow mode, postmortem drafts, impact, 90-day plan |

## Quick start

You need a GCP project, `gcloud`, `kubectl`, Helm 3.14+, Docker and Go.

```bash
cp lab/config.env.example lab/config.env   # then fill it in
make cluster
make install
make forward                                # open http://localhost:8080
```

`make help` lists every target. `lab/README.md` covers the full scenario.

The lab costs money while the cluster exists. Run `make destroy` when you're
done.

## Licensing

Apache-2.0. See [`LICENSE`](LICENSE).

`lab/patches/checkout/payment_retry.go` is derived from the OpenTelemetry Demo,
which is also Apache-2.0 licensed.
