# 90-day rollout checklist

One pilot team, one service, one action. Each phase has exit criteria; don't
move on until they're met, and stop if any "reasons to pause" appear.

## Days 1–14: foundations

- [ ] Name an owner for the agent (a team, not a person) and add the agent to their on-call runbooks
- [ ] Pick the pilot: one team, one service with frequent, well-understood alerts
- [ ] Audit the pilot service's telemetry against Chapter 2's contract (names, propagation, error status, visible retries, versions, trace IDs in logs)
- [ ] Make changes queryable: every deploy, config change and flag flip for the pilot service reaches one place
- [ ] Give the agent read-only credentials; confirm it can't write anywhere
- [ ] Run the evaluation suite with your model; commit the reviewed result as `evals/baselines/model.json`
- [ ] Send the agent's telemetry to your collector; load the alerts and dashboard (Chapter 7)
- [ ] Kill switch drill: someone on the pilot rota turns it on and off, with the runbook, in under two minutes
- [ ] Set every action to `observe` or `suggest` in the policy file

Exit: evaluation passes the `suggest` bar; telemetry gaps for the pilot service are fixed or written down.

## Days 15–45: shadow mode

- [ ] The agent investigates every pilot alert; its report goes to a shadow channel, labelled as such
- [ ] Responders investigate exactly as before and don't rely on the report
- [ ] After each incident, record the responders' conclusion (`sre-rollout shadow record-human`)
- [ ] Weekly: review every disagreement with the pilot team; turn each into an evaluation scenario
- [ ] Mid-phase: survey the pilot rota (see Measuring below)

Exit: at least 10 shadowed incidents; agreement at or above 80%; no confidently wrong conclusions in the last 5; the pilot team agrees to see the reports in the main incident channel.

## Days 46–75: suggestions

- [ ] Reports appear in the incident channel, with evidence links and what was ruled out
- [ ] Actions move to `suggest`: responders see the plan and run it themselves if they agree
- [ ] After each incident, record agree or disagree (`sre-policy feedback`)
- [ ] Train the rota as approvers (Chapter 8, "From investigator to approver")
- [ ] Game day: reproduce an incident in the lab or staging and run the full flow

Exit: the policy's `to_approve` criteria are met (`sre-policy readiness`); the evaluation passes the `approve` bar; the pilot team wants approvals.

## Days 76–90: approvals

- [ ] A reviewed change moves the pilot action to `approve`, scoped to the pilot namespace and release
- [ ] Approvals arrive in Slack (or your incident tool) with the plan and the evidence
- [ ] Every execution is verified; every postmortem starts from the agent's draft
- [ ] Day 90 retrospective with the pilot team: expand, hold, or roll back

## Measuring

- Shadow agreement and confidently-wrong count (`sre-rollout shadow report`)
- Median minutes to a conclusion: agent versus responders
- Approval latency, override rate, bad or reverted outcomes (`sre-rollout impact`)
- Escalation rate, inside the 10–50% band
- On-call survey, at days 1, 45 and 90: time spent gathering evidence, trust in the reports, whether the agent added work

## Reasons to pause

- Any confidently wrong conclusion that a responder acted on
- An override rate near zero for weeks with many approvals (possible rubber-stamping)
- Responders reporting that the agent adds work rather than removing it
- Telemetry or change-tracking gaps the agent keeps hitting on the pilot service
