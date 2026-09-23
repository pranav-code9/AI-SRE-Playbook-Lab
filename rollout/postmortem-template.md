# Postmortem: <title>

**Status:** draft | reviewed · **Incident window:** <start>–<end> UTC · **Severity:** <n>

This review is blameless: it asks how the system made the failure possible and
how it was found and fixed, not who made a mistake. People appear by role.

`sre-rollout postmortem` fills in everything marked (records) from the agent's
investigation and the audit log. The team writes the rest.

## Summary
Three to five sentences a reader outside the team would understand. Start from
the agent's summary (records); rewrite it in your own words.

## Impact
Who was affected, for how long, how badly. Evidence to start from (records).

## Timeline (records, then checked)
Changes, the alert, approvals, actions and verified outcomes, in order. Add what
the records can't see: when people were paged, joined, and what they decided.

## How the cause was found (records)
The causal chain with evidence, and what was ruled out. Note where responders
and the agent disagreed, and who was right.

## Contributing factors
What made this possible, and what made it hard to catch or diagnose. Systems,
processes, defaults, missing signals. Never "person X should have".

## What went well, and what was hard
Facts from the records (approval time, verification result), plus the team's view.

## Action items
Each with an owning team and a date. Include follow-ups the agent proposed (records)
only if the team agrees with them.

## The agent's part
Was its conclusion right? Did it help, add work, or mislead? Record the verdict
with `sre-policy feedback`, and turn any mistake into an evaluation scenario.

## Appendix: evidence (records)
Every evidence ID with its exact query and finding.
