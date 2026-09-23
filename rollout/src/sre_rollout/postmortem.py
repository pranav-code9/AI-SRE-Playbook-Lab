"""An agent-assisted, blameless postmortem draft.

The draft is assembled from records, not written from memory: the agent's
investigation state (hypotheses, evidence, timeline) and the trust ladder's
audit log (approvals, executions, outcomes). Facts the records support are
filled in. Everything that needs judgment is left for the team, marked
TO WRITE, with prompts.

Blameless by construction: people appear by role, never by name. Identities
from the audit log are mapped to the roles defined in the policy file; anyone
without a role is "an engineer".
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def role_of(identity: str, roles: dict[str, list[str]]) -> str:
    if identity == "agent":
        return "the agent"
    if identity == "verifier":
        return "the automatic verification"
    for role, members in roles.items():
        if identity in members:
            return f"the {role.replace('-', ' ')}"
    return "an engineer"


def _t(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M")


def _audit_line(e: dict, roles) -> Optional[str]:
    who, d = role_of(e["actor"], roles), e["data"]
    if e["event"] == "approval_requested":
        return f"{who.capitalize()} requested approval for {d.get('summary', e['action'])}"
    if e["event"] == "approved":
        return f"{who.capitalize()} approved the plan"
    if e["event"] == "denied":
        return f"{who.capitalize()} denied the plan"
    if e["event"] == "executed":
        return f"{e['action']} executed ({d.get('summary', '')}), started by {who}"
    if e["event"] == "outcome":
        return f"Outcome of the action recorded as {d['outcome']} by {who}: {d.get('reason', '')}"
    if e["event"] == "stopped":
        return f"{who.capitalize()} turned on the kill switch for {e['action']}"
    return None


def draft(state: dict, audit_entries: list[dict], roles: dict[str, list[str]], title: str) -> str:
    inc, c = state["incident"], state.get("conclusion") or {}
    hyps = {h["id"]: h for h in state["hypotheses"]}
    ev = {e["id"]: e for e in state["evidence"]}

    timeline = [(inc["alert_started_at"], f"Alert `{inc['alert_id']}` fired: {inc['symptom']}")]
    timeline += [(t["at"], f"{t['what']} ({t['evidence_id']})") for t in state.get("timeline", [])]
    timeline += [(e["at"], line) for e in audit_entries if (line := _audit_line(e, roles))]
    timeline.sort(key=lambda x: datetime.fromisoformat(x[0].replace("Z", "+00:00")))

    chain = c.get("causal_chain", [])
    ruled_out = [h for h in hyps.values() if h["status"] == "refuted"]
    executed = [e for e in audit_entries if e["event"] == "executed"]
    outcomes = [e for e in audit_entries if e["event"] == "outcome"]
    b = state["budget"]

    out = [f"# Postmortem: {title}", "",
           "**Status:** DRAFT, assembled from the agent's investigation and the audit log. "
           "Every section needs a human check. Sections marked TO WRITE are for the team.", "",
           "This review is blameless. It asks how the system made this failure possible and how it was found "
           "and fixed, not who made a mistake. People appear by role.", "",
           "## Summary", "", "_The agent's summary, to be checked and rewritten by the team:_", "",
           f"> {c.get('summary', '(no summary)')}", "",
           "## Impact", "", "TO WRITE: who was affected, for how long, and how badly. "
           "Starting points from the evidence:", ""]
    for e in ev.values():
        if e["ok"] and e["tool"] in ("compare_windows", "get_service_red") and e["args"].get("service") == inc["service"]:
            out.append(f"- {e['id']}: {e['summary']}")
    out += ["", "## Timeline", "", "| Time (UTC) | What happened |", "|---|---|"]
    out += [f"| {_t(ts)} | {what} |" for ts, what in timeline]

    out += ["", "## How the cause was found", ""]
    if chain:
        out.append(f"The agent concluded with confidence {c.get('confidence', 0):.2f}. Causal chain, root cause first:")
        out.append("")
        for i, hid in enumerate(chain, 1):
            h = hyps[hid]
            support = ", ".join(s["evidence_id"] for s in h["stances"] if s["supports"]) or "the alert"
            out.append(f"{i}. **{h['statement']}** ({h['kind']}; evidence {support})")
    else:
        out.append(f"The agent escalated: {c.get('escalation_reason', 'no conclusion')}.")
    if ruled_out:
        out += ["", "Ruled out:", ""]
        out += [f"- {h['statement']}: {h['refuted_reason']}" for h in ruled_out]
    out += ["", f"The investigation took {b['iterations']} iteration(s) and {b['tool_calls']} tool calls. "
            "Every evidence ID above is listed, with its query, in the appendix."]

    out += ["", "## Contributing factors", "",
            "TO WRITE. Ask what made this possible and what made it hard to catch, not who did it. For example: "
            "what let this change reach production, what would have shown the problem before customers did, and "
            "what made the diagnosis slower than it needed to be.", ""]
    if c.get("open_questions"):
        out += ["Questions the investigation left open:", ""] + [f"- {q}" for q in c["open_questions"]]

    out += ["", "## What went well, and what was hard", "", "TO WRITE. Facts from the records:", ""]
    requested = next((e for e in audit_entries if e["event"] == "approval_requested"), None)
    approved = next((e for e in audit_entries if e["event"] == "approved"), None)
    if requested and approved:
        wait = (datetime.fromisoformat(approved["at"]) - datetime.fromisoformat(requested["at"])).total_seconds() / 60
        out.append(f"- Approval took {wait:.0f} minute(s) from request to decision.")
    for e in executed:
        out.append(f"- {e['action']} ran at {_t(e['at'])} UTC.")
    for e in outcomes:
        out.append(f"- Its outcome was recorded as **{e['data']['outcome']}**: {e['data'].get('reason', '')}")

    out += ["", "## Action items", "", "TO WRITE, each with an owner (a team, not a person) and a date. "
            "Follow-ups the agent proposed that weren't executed:", ""]
    done = {e["data"].get("summary") for e in executed}
    out += [f"- {a}" for a in c.get("proposed_actions", []) if not any(s and s.split()[0] in a for s in done)] or ["- (none)"]

    out += ["", "## The agent's part", "",
            "TO WRITE. Was its conclusion right? Did it help, or add work? Was anything in its report misleading? "
            "Record the verdict so it counts toward the trust ladder:", "",
            "```bash", "sre-policy feedback rollback_release agree|disagree --reason \"...\"", "```", "",
            "## Appendix: evidence", "", "| ID | Signal | Query | Finding |", "|---|---|---|---|"]
    for e in ev.values():
        args = ", ".join(f"{k}={v}" for k, v in e["args"].items())
        finding = e["summary"].replace("|", "\\|")
        out.append(f"| {e['id']} | {e['signal']}{'' if e['ok'] else ' (failed)'} | `{e['tool']}({args})` | {finding} |")
    return "\n".join(out) + "\n"
