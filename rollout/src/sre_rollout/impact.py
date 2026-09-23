"""Measuring whether the agent helps, beyond MTTR.

MTTR is noisy (a handful of incidents a month, each different), easy to move
for the wrong reasons, and says nothing about whether responders trust the
tool. These measures come straight from the records the earlier chapters
built: the shadow log, the audit log, and the agent's own budget.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median


def approval_stats(audit_entries: list[dict]) -> dict:
    requested = {e["data"]["approval_id"]: e for e in audit_entries if e["event"] == "approval_requested"}
    waits, denied, approved = [], 0, 0
    for e in audit_entries:
        if e["event"] in ("approved", "denied") and e["data"].get("approval_id") in requested:
            start = datetime.fromisoformat(requested[e["data"]["approval_id"]]["at"])
            waits.append((datetime.fromisoformat(e["at"]) - start).total_seconds() / 60)
            approved += e["event"] == "approved"
            denied += e["event"] == "denied"
    outcomes = [e["data"]["outcome"] for e in audit_entries if e["event"] == "outcome"]
    feedback = [e["data"]["verdict"] for e in audit_entries if e["event"] == "suggestion_reviewed"]
    return {
        "approval_requests": len(requested),
        "approved": approved,
        "denied": denied,
        "override_rate": (denied / (approved + denied)) if (approved + denied) else None,
        "median_approval_minutes": median(waits) if waits else None,
        "executions": sum(e["event"] == "executed" for e in audit_entries),
        "bad_or_reverted": sum(o in ("bad", "reverted") for o in outcomes),
        "suggestions_reviewed": len(feedback),
        "suggestion_agreement": (feedback.count("agree") / len(feedback)) if feedback else None,
        "kill_switch_uses": sum(e["event"] == "stopped" for e in audit_entries),
    }


def render_impact(shadow: dict, approvals: dict, survey: dict | None = None) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v:.0%}"

    lines = ["# Is the agent helping?", "",
             "## Accuracy and speed (shadow mode)", "",
             f"- Agreed with responders: {pct(shadow.get('agreement'))} of {shadow.get('incidents', 0)} incidents",
             f"- Confidently wrong: {shadow.get('confidently_wrong', 0)}",
             f"- Median minutes to a conclusion: agent {shadow.get('median_agent_minutes')}, "
             f"responders {shadow.get('median_human_minutes')}",
             "", "## Trust (the audit log)", "",
             f"- Suggestions reviewed: {approvals['suggestions_reviewed']}, agreed with: {pct(approvals['suggestion_agreement'])}",
             f"- Approval requests: {approvals['approval_requests']}; approved {approvals['approved']}, "
             f"denied {approvals['denied']} (override rate {pct(approvals['override_rate'])})",
             f"- Median minutes from request to decision: {approvals['median_approval_minutes']}",
             f"- Executions: {approvals['executions']}; bad or reverted: {approvals['bad_or_reverted']}",
             f"- Kill switch used: {approvals['kill_switch_uses']} time(s)"]
    if survey:
        lines += ["", "## On-call experience (survey)", ""] + [f"- {k}: {v}" for k, v in survey.items()]
    return "\n".join(lines) + "\n"
