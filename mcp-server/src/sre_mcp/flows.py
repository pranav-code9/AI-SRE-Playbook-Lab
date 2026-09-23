"""Chapter 5 flows around the actions server.

propose: take the agent's structured action proposals and put each one through
         the trust ladder: shadow, suggest, request approval, or (at the
         autonomous rung) execute.
execute: run a plan a human has approved.
verify:  check an execution against the policy's verify rule; a failure
         records a bad outcome, which demotes the action.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

from investigator.tools.base import parse_ts
from sre_mcp.actions import RequestApprovalArgs, Rollback, RollbackArgs


def _change_age_minutes(state: dict, release: str, now: datetime):
    times = [parse_ts(t["at"]) for t in state.get("timeline", []) if t["what"].startswith(f"{release} revision")]
    return (now - max(times)).total_seconds() / 60 if times else None


def structural_facts(state: dict) -> tuple[bool, bool]:
    """(change_anchored, chain_complete) for the concluded root cause, read from
    the investigation itself, so autonomy never rests on confidence alone."""
    c = state.get("conclusion") or {}
    hyps = {h["id"]: h for h in state.get("hypotheses", [])}
    evidence = {e["id"]: e for e in state.get("evidence", [])}
    root = hyps.get(c.get("root_cause_id"))
    chain = c.get("causal_chain") or []
    chain_complete = bool(chain) and chain[0] == c.get("root_cause_id") and hyps.get(chain[-1], {}).get("kind") == "symptom"
    anchored = bool(root) and any(
        s["supports"] and evidence.get(s["evidence_id"], {}).get("signal") == "change" and evidence[s["evidence_id"]]["ok"]
        for s in root.get("stances", []))
    return anchored, chain_complete


def propose(state_path: Path, gate, backend, registry, context, notify, now: datetime, out=print) -> int:
    state = json.loads(Path(state_path).read_text())
    c = state.get("conclusion") or {}
    proposals = c.get("action_proposals") or []
    if not proposals:
        out("The agent made no structured action proposals.")
        return 0
    rollback = Rollback(backend, registry, gate=gate, actor="agent", notify=notify)
    for p in proposals:
        if p["tool"] != Rollback.name:
            out(f"{p['tool']}: no action tool by that name; recorded nothing.")
            continue
        args = RequestApprovalArgs(
            release=p["args"]["release"], to_revision=p["args"]["to_revision"], reason=p["reason"],
            agent_confidence=c.get("confidence"),
            change_age_minutes=_change_age_minutes(state, p["args"]["release"], now),
        )
        ctx = context()
        plan, summary, _ = rollback.plan(ctx, args)
        if plan is None:
            out(f"{Rollback.name}: {summary}")
            continue
        req = rollback.request(args, plan)
        req.change_anchored, req.chain_complete = structural_facts(state)
        d = gate.evaluate(req)
        out(f"{Rollback.name} {plan['release']} {plan['current_revision']} -> {plan['target_revision']}: "
            f"{d.outcome} at rung {d.rung} ({'; '.join(d.reasons)})")
        if d.outcome == "suggest_only":
            out(f"  Suggested to a human: {plan['command']}")
        elif d.outcome == "needs_approval":
            approval = gate.request_approval(req)
            if notify:
                notify(approval)
            out(f"  Approval {approval.id} requested; expires {approval.expires_at[:16]}Z")
        elif d.outcome == "allowed":
            output = backend.rollback(plan["namespace"], plan["release"], plan["target_revision"])
            out(f"  Executed: {output}. Execution {gate.record_execution(req, output)}")
    return 0


def execute(approval_id: str, gate, backend, registry, context, actor: str, out=print) -> int:
    approval = gate.get_approval(approval_id)
    if approval is None:
        out(f"No approval {approval_id}.")
        return 1
    p = approval.plan
    args = RollbackArgs(release=p["release"], to_revision=p["target_revision"], reason=approval.reason,
                        namespace=p["namespace"], dry_run=False, approval_id=approval_id)
    result = Rollback(backend, registry, gate=gate, actor=actor)(context(), args)
    out(result.summary)
    return 0 if result.ok else 1


def verify(execution_id: str, gate, backends, out=print) -> int:
    from sre_policy.readiness import verify_execution

    def measure(service, metric, start, end):
        points = backends.metrics.series(service, metric, start, end)
        return mean(v for _, v in points) if points else None

    try:
        result = verify_execution(gate, execution_id, measure)
    except (KeyError, ValueError) as exc:
        print(f"Can't verify: {exc}", file=sys.stderr)
        return 1
    out(f"Execution {execution_id}: {result}")
    if result == "bad":
        out(f"{Rollback.name} is now at rung {gate.effective_rung(Rollback.name).label}")
    return 0 if result != "bad" else 1
