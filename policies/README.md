# Trust ladder (Chapter 5)

The policy that decides what an action may do, and the machinery that enforces
it: approvals bound to exact plans, blast-radius limits, rate limits, a kill
switch, a hash-chained audit log, automatic demotion after a bad outcome, and
a readiness report for promotion.

`trust-ladder.yaml` is the policy. Every action tool has an entry; an action
without one is treated as `observe` and never runs.

| Rung | What happens to a request |
|---|---|
| `observe` | Recorded in the audit log. Nothing shown, nothing run. |
| `suggest` | The plan is shown to a human, who runs it themselves if they agree. |
| `approve` | Runs with a valid approval of this exact plan, inside the blast radius and rate limit. |
| `autonomous` | Runs without approval when the autonomy preconditions hold; otherwise falls back to approval. |

## Install

```bash
pip install -e ".[dev]"
pytest
sre-policy check
```

## The approval flow on the recorded case study

From the repository root, with the agent, MCP server and policy packages
installed. The recorded incident ends at 10:30, so replays pin "now" there.

```bash
SC=agent/scenarios/checkout_retry_storm.json
POL=policies/trust-ladder.yaml
NOW=2026-09-22T10:30:00Z

investigate --scenario $SC --scripted agent/scenarios/checkout_retry_storm.script.json --out runs
sre-mcp propose runs/<timestamp>/state.json --scenario $SC --policy $POL --state-dir .sre-policy

sre-policy --policy $POL --state-dir .sre-policy pending --now $NOW
sre-policy --policy $POL --state-dir .sre-policy approve <approval id> --as cli:pranav --now $NOW
sre-mcp execute <approval id> --scenario $SC --policy $POL --state-dir .sre-policy --actor cli:pranav

sre-policy --policy $POL --state-dir .sre-policy audit --now $NOW
sre-policy --policy $POL --state-dir .sre-policy audit --verify
```

Approver identities come from `roles` in the policy: `cli:<name>` for the
command line and `slack:<user id>` for Slack. Replace the examples with your own.

## Slack

`sre-mcp ... --slack-webhook <incoming webhook URL>` posts each approval request
with the plan and Approve and Deny buttons. To make the buttons work, create a
Slack app with interactivity enabled, point its request URL at
`/slack/interactions`, and run:

```bash
SLACK_SIGNING_SECRET=... sre-policy --policy $POL --state-dir .sre-policy serve-slack --port 8787
```

Every request is checked against Slack's signing secret before any decision is
recorded. The endpoint must be reachable from Slack; in the lab, a tunnel works.

## Other commands

```bash
sre-policy stop rollback_release --reason "..."          # kill switch for one action
touch .sre-policy/STOP                                    # kill switch for everything
sre-policy outcome <execution id> bad --reason "..."      # demotes one rung (inconclusive doesn't)
sre-policy feedback rollback_release agree --reason "..." # review a suggestion
sre-policy readiness rollback_release --eval-pass-rate 0.97
sre-mcp verify <execution id> --live --policy $POL        # measure it; demotes if it didn't work
```

State (approvals, demotions, the audit log) lives in `--state-dir`. It's
file-based and single-writer, which is right for the lab and not for
production: there, back it with a database and ship the audit log somewhere
append-only.

## What the gate checks

Blast radius at every executing rung: namespace, release, revisions back,
changed values, changed rendered resources (unknown counts as too many), and
no chart-version change unless `allow_chart_change` is set. At the autonomous
rung, confidence is never enough alone: the root cause must be anchored to a
change and its chain must reach the symptom. Verification compares the ten
minutes before an action with the second half of the ten after it, and records
"inconclusive" rather than "good" if the metric was already falling.

