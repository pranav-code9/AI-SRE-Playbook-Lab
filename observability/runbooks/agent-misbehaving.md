# Runbook: the agent is misbehaving

**Owner:** whoever runs the agent · **Alerts:** `SREAgent*` in `prometheus/sre-agent-rules.yaml`

The agent is a production service with its own failure modes. This runbook is
for when it, rather than the system it investigates, is the problem.

## First: stop it acting

If there's any chance the agent is proposing or taking wrong actions, turn off
execution before diagnosing anything. Investigation can continue; actions can't.

```bash
sre-policy --policy policies/trust-ladder.yaml --state-dir <state> stop rollback_release --reason "<why>"
touch <state>/STOP        # every action, at once
```

## Crashes

`SREAgentInvestigationsCrashing`. Find the failed trace in Jaeger: service
`sre-investigator`, root span `invoke_agent sre-investigator` with an error
status. The recorded exception says where it broke. Model provider outages
and changed structured-output behaviour after a model upgrade are the usual
causes.

## Tools failing

`SREAgentToolsFailing`. The agent is investigating with missing signals, and
its conclusions are weaker than they look. Filter traces for
`execute_tool` spans with an error status and group by `gen_ai.tool.name`.
A single tool failing usually means its backend is down or its credentials
expired. Everything failing usually means the agent can't reach the cluster.

## Looping

`SREAgentLooping`. Guard trips appear as `guard_tripped` events on the
investigation's spans, with the guard's name and what it saw. Repeated
`repeated_question` trips on one tool often mean its results are too noisy to
be useful: tighten the tool's summary or cap. Repeated `no_progress` trips
mean the model isn't learning from what it gathers: check recent prompt or
model changes, and rerun the evaluation suite.

## Spend

`SREAgentSpendHigh`. Sort today's investigation traces by
`sre.budget.cost_usd`. One very expensive investigation is usually a loop the
guards didn't catch; many moderately expensive ones usually mean more
incidents, or a model or prompt change that made each call larger.

## Fighting another automation

`sre-obs overlaps --audit <state>/audit.jsonl --live` lists changes to the
same release close to the agent's own actions. Two automations acting on one
release is a coordination problem, not an agent bug: decide which one owns it,
and use the kill switch on the other until you do.
