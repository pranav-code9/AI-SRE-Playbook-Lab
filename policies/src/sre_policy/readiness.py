"""Promotion evidence: what the audit log says about an action's track record,
measured against the policy's promotion criteria. Promotion itself is a
reviewed change to the policy file; this only says whether the case is there."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Literal, Optional

from sre_policy.gate import Gate


@dataclass
class Criterion:
    name: str
    value: Optional[float]
    required: float
    ok: bool


def track_record(gate: Gate, action: str) -> dict:
    stats = {"suggestions_reviewed": 0, "suggestions_rejected": 0, "approvals_granted": 0, "approvals_denied": 0,
             "approved_executions": 0, "autonomous_executions": 0, "good_outcomes": 0, "bad_outcomes": 0}
    for e in gate.audit.entries():
        if e["action"] != action:
            continue
        ev, d = e["event"], e["data"]
        if ev == "suggestion_reviewed":
            stats["suggestions_reviewed"] += 1
            stats["suggestions_rejected"] += d.get("verdict") == "disagree"
        elif ev == "approved":
            stats["approvals_granted"] += 1
        elif ev == "denied":
            stats["approvals_denied"] += 1
        elif ev == "executed":
            stats["approved_executions" if d.get("approval_id") else "autonomous_executions"] += 1
        elif ev == "outcome" and d["outcome"] != "inconclusive":
            stats["good_outcomes" if d["outcome"] == "good" else "bad_outcomes"] += 1
    return stats


def readiness(gate: Gate, action: str, eval_pass_rate: Optional[float] = None) -> dict[str, list[Criterion]]:
    s = track_record(gate, action)
    promo = gate.policy.action(action).promotion
    reviewed = s["suggestions_reviewed"]
    measured = {
        "min_suggestions_reviewed": reviewed,
        "max_rejected_ratio": (s["suggestions_rejected"] / reviewed) if reviewed else None,
        "min_approved_executions": s["approved_executions"],
        "max_bad_outcomes": s["bad_outcomes"],
        "min_eval_pass_rate": eval_pass_rate,
    }
    out = {}
    for target, criteria in (("approve", promo.to_approve), ("autonomous", promo.to_autonomous)):
        rows = []
        for name, required in criteria.items():
            value = measured.get(name)
            if value is None:
                ok = False
            elif name.startswith("max_"):
                ok = value <= required
            else:
                ok = value >= required
            rows.append(Criterion(name, value, required, ok))
        out[target] = rows
    return out


Measure = Callable[[str, str, datetime, datetime], Optional[float]]


def verify_execution(gate: Gate, execution_id: str, measure: Measure) -> Literal["good", "bad", "inconclusive", "pending"]:
    """Did the action fix what it was meant to fix?

    Compares the policy's verify metric before the execution with the second
    half of the window after it (the first half is the rollout itself). Two
    guards keep it honest about attribution: if the metric was already falling
    before the action, the result is inconclusive rather than a success; and if
    the metric can't be measured, the result is bad, because an action that
    can't show it worked shouldn't keep its rung. Bad outcomes demote.
    """
    executed = next((e for e in gate.audit.entries()
                     if e["event"] == "executed" and e["data"].get("execution_id") == execution_id), None)
    if executed is None:
        raise KeyError(f"no execution {execution_id}")
    v = gate.policy.action(executed["action"]).verify
    if v is None:
        raise ValueError(f"{executed['action']} has no verify rule")
    at = datetime.fromisoformat(executed["at"])
    span = timedelta(minutes=v.after_minutes)
    if gate.now() < at + span:
        return "pending"
    before = measure(v.service, v.metric, at - span, at)
    early = measure(v.service, v.metric, at - span, at - span / 2)
    late = measure(v.service, v.metric, at - span / 2, at)
    after = measure(v.service, v.metric, at + span / 2, at + span)
    if before is None or after is None or before <= 0:
        note = f"couldn't measure {v.service} {v.metric} (before {before}, after {after})"
        gate.record_outcome(execution_id, "bad", "verifier", note)
        return "bad"
    drop = (before - after) / before * 100
    trend = ((early - late) / early * 100) if (early and late is not None and early > 0) else 0.0
    note = (f"{v.service} {v.metric} {before:.4g} -> {after:.4g} ({drop:.0f}% drop; needed {v.must_drop_by_percent:.0f}%; "
            f"already falling {trend:.0f}% before the action)")
    if drop >= v.must_drop_by_percent and trend >= v.must_drop_by_percent / 2:
        outcome = "inconclusive"   # it was recovering anyway: don't credit the action
    else:
        outcome = "good" if drop >= v.must_drop_by_percent else "bad"
    gate.record_outcome(execution_id, outcome, "verifier", note)
    return outcome
