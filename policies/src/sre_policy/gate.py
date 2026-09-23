"""The gate every action passes through before it runs.

`Gate.evaluate` answers one question for one request: may this exact plan run
now, and if not, why not? It never executes anything itself; the action tool
asks, then acts only on `allowed`.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

from sre_policy.audit import AuditLog
from sre_policy.model import Policy, Rung

Outcome = Literal["shadow", "suggest_only", "needs_approval", "allowed", "denied"]


def plan_hash(plan: dict) -> str:
    """Identity of a plan. Approvals are bound to it: if anything in the plan
    changes between approval and execution, the approval no longer applies."""
    stable = {k: v for k, v in plan.items() if k not in ("reason", "command")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ActionRequest(BaseModel):
    action: str
    plan: dict
    actor: str                             # "agent" or "cli:<name>" / "slack:<id>"
    reason: str
    approval_id: Optional[str] = None
    agent_confidence: Optional[float] = None  # recorded for the audit trail; never a gate
    change_age_minutes: Optional[float] = None
    change_anchored: Optional[bool] = None   # from the investigation: root cause backed by change evidence
    chain_complete: Optional[bool] = None    # from the investigation: why-chain reaches the symptom
    sole_root_cause: Optional[bool] = None   # from the investigation: no rival root cause still supported

    @property
    def hash(self) -> str:
        return plan_hash(self.plan)


class Decision(BaseModel):
    outcome: Outcome
    rung: str
    reasons: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class Approval(BaseModel):
    id: str
    action: str
    plan_hash: str
    plan: dict
    reason: str
    requested_by: str
    requested_at: str
    expires_at: str
    status: Literal["pending", "approved", "denied", "used", "expired"] = "pending"
    decided_by: Optional[str] = None


class Gate:
    def __init__(self, policy: Policy, state_dir: str | Path, clock: Callable[[], datetime] | None = None) -> None:
        self.policy = policy
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.dir / "audit.jsonl")
        self.now = clock or (lambda: datetime.now(tz=timezone.utc))

    # --- persisted state ---------------------------------------------------

    def _load(self, name: str, default):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else default

    def _save(self, name: str, value) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, default=str))
        tmp.replace(self.dir / name)

    def _approvals(self) -> dict[str, Approval]:
        return {k: Approval(**v) for k, v in self._load("approvals.json", {}).items()}

    def _save_approvals(self, approvals: dict[str, Approval]) -> None:
        self._save("approvals.json", {k: v.model_dump() for k, v in approvals.items()})

    def _trust(self) -> dict:
        return self._load("trust-state.json", {"demoted_to": {}, "stopped": []})

    # --- rungs, kill switch, demotion ---------------------------------------

    def effective_rung(self, action: str) -> Rung:
        configured = self.policy.action(action).rung
        demoted = self._trust()["demoted_to"].get(action)
        return min(configured, Rung.parse(demoted)) if demoted is not None else configured

    def stopped(self, action: str) -> bool:
        if os.environ.get("SRE_POLICY_STOP") == "1" or (self.dir / "STOP").exists():
            return True
        return action in self._trust()["stopped"]

    def stop(self, action: str, by: str, reason: str) -> None:
        t = self._trust()
        if action not in t["stopped"]:
            t["stopped"].append(action)
        self._save("trust-state.json", t)
        self.audit.append("stopped", by, action, {"reason": reason}, self.now())

    def resume(self, action: str, by: str, reason: str) -> None:
        t = self._trust()
        t["stopped"] = [a for a in t["stopped"] if a != action]
        self._save("trust-state.json", t)
        self.audit.append("resumed", by, action, {"reason": reason}, self.now())

    def demote(self, action: str, by: str, reason: str) -> Rung:
        current = self.effective_rung(action)
        lower = Rung(max(Rung.SUGGEST, current - 1))
        t = self._trust()
        t["demoted_to"][action] = lower.label
        self._save("trust-state.json", t)
        self.audit.append("demoted", by, action, {"from": current.label, "to": lower.label, "reason": reason}, self.now())
        return lower

    def reset_demotion(self, action: str, by: str, reason: str) -> None:
        t = self._trust()
        t["demoted_to"].pop(action, None)
        self._save("trust-state.json", t)
        self.audit.append("demotion_reset", by, action, {"reason": reason}, self.now())

    # --- approvals ----------------------------------------------------------

    def request_approval(self, req: ActionRequest) -> Approval:
        now = self.now()
        ttl = timedelta(minutes=self.policy.defaults.approval_ttl_minutes)
        a = Approval(
            id=uuid.uuid4().hex[:10], action=req.action, plan_hash=req.hash, plan=req.plan, reason=req.reason,
            requested_by=req.actor, requested_at=now.isoformat(), expires_at=(now + ttl).isoformat(),
        )
        approvals = self._approvals()
        approvals[a.id] = a
        self._save_approvals(approvals)
        self.audit.append("approval_requested", req.actor, req.action,
                          {"approval_id": a.id, "plan_hash": a.plan_hash, "reason": req.reason,
                           "summary": _plan_line(req.plan)}, now)
        return a

    def decide_approval(self, approval_id: str, approver: str, approve: bool, note: str = "") -> Approval:
        approvals = self._approvals()
        a = approvals.get(approval_id)
        if a is None:
            raise KeyError(f"no approval request {approval_id}")
        if a.status != "pending":
            raise ValueError(f"approval {approval_id} is already {a.status}")
        if datetime.fromisoformat(a.expires_at) < self.now():
            a.status = "expired"
            self._save_approvals(approvals)
            raise ValueError(f"approval {approval_id} expired at {a.expires_at}")
        if not self.policy.is_approver(a.action, approver):
            self.audit.append("approval_rejected", approver, a.action,
                              {"approval_id": a.id, "reason": "not an approver for this action"}, self.now())
            raise PermissionError(f"{approver} is not an approver for {a.action}")
        if approver == a.requested_by:
            raise PermissionError("the requester can't approve their own request")
        a.status = "approved" if approve else "denied"
        a.decided_by = approver
        self._save_approvals(approvals)
        self.audit.append("approved" if approve else "denied", approver, a.action,
                          {"approval_id": a.id, "plan_hash": a.plan_hash, "reason": note or a.reason}, self.now())
        return a

    def pending(self) -> list[Approval]:
        return [a for a in self._approvals().values() if a.status == "pending"]

    def get_approval(self, approval_id: str) -> Optional[Approval]:
        return self._approvals().get(approval_id)

    # --- the decision -------------------------------------------------------

    def evaluate(self, req: ActionRequest) -> Decision:
        ap = self.policy.action(req.action)
        rung = self.effective_rung(req.action)
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        def decide(outcome: Outcome, why: str) -> Decision:
            d = Decision(outcome=outcome, rung=rung.label, reasons=reasons + [why], checks=checks)
            self.audit.append("decision", req.actor, req.action,
                              {"outcome": outcome, "rung": rung.label, "reasons": d.reasons, "checks": checks,
                               "plan_hash": req.hash, "summary": _plan_line(req.plan)}, self.now())
            return d

        if req.action not in self.policy.actions:
            return decide("shadow", "action has no policy entry; treated as observe")
        if rung == Rung.OBSERVE:
            return decide("shadow", "rung observe: recorded, not shown or run")
        if rung == Rung.SUGGEST:
            return decide("suggest_only", "rung suggest: a human runs it if they agree")

        checks["not_stopped"] = not self.stopped(req.action)
        if not checks["not_stopped"]:
            return decide("denied", "kill switch is on for this action")

        br = ap.blast_radius
        p = req.plan
        checks["namespace_in_scope"] = p.get("namespace") in br.namespaces
        checks["release_in_scope"] = p.get("release") in br.releases
        if br.max_revisions_back is not None and "current_revision" in p:
            checks["revisions_back_ok"] = p["current_revision"] - p["target_revision"] <= br.max_revisions_back
        if br.max_changed_values is not None:
            changes = p.get("changes")
            checks["changed_values_ok"] = changes is not None and len(changes) <= br.max_changed_values
        if br.max_resources_changed is not None:
            resources = p.get("resources_changed")
            checks["resources_changed_ok"] = resources is not None and len(resources) <= br.max_resources_changed
        if not br.allow_chart_change and "chart" in p:
            checks["chart_unchanged"] = p["chart"].get("current") == p["chart"].get("target")
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            return decide("denied", "outside blast radius: " + ", ".join(failed))

        if ap.rate_limit:
            window_start = self.now() - timedelta(minutes=ap.rate_limit.per_minutes)
            recent = [e for e in self.audit.entries()
                      if e["event"] == "executed" and e["action"] == req.action
                      and datetime.fromisoformat(e["at"]) >= window_start]
            checks["rate_limit_ok"] = len(recent) < ap.rate_limit.max
            if not checks["rate_limit_ok"]:
                return decide("denied", f"rate limit: {ap.rate_limit.max} per {ap.rate_limit.per_minutes} minutes")

        if rung == Rung.APPROVE:
            if not req.approval_id:
                return decide("needs_approval", "rung approve: request an approval for this plan")
            a = self.get_approval(req.approval_id)
            checks["approval_exists"] = a is not None
            if a is None:
                return decide("denied", "approval not found")
            checks["approval_granted"] = a.status == "approved"
            checks["approval_for_this_plan"] = a.plan_hash == req.hash and a.action == req.action
            checks["approval_unexpired"] = datetime.fromisoformat(a.expires_at) >= self.now()
            failed = [k for k in ("approval_granted", "approval_for_this_plan", "approval_unexpired") if not checks[k]]
            if failed:
                return decide("denied", "approval not valid: " + ", ".join(failed))
            return decide("allowed", f"approved by {a.decided_by}")

        # autonomous
        au = ap.autonomy
        if au.max_change_age_minutes is not None:
            checks["change_recent"] = req.change_age_minutes is not None and req.change_age_minutes <= au.max_change_age_minutes
        if au.require_change_anchor:
            checks["change_anchored"] = bool(req.change_anchored)
        if au.require_complete_chain:
            checks["chain_complete"] = bool(req.chain_complete)
        if au.require_sole_root_cause:
            checks["sole_root_cause"] = bool(req.sole_root_cause)
        failed = [k for k in ("change_recent", "change_anchored", "chain_complete", "sole_root_cause")
                  if k in checks and not checks[k]]
        if failed:
            return decide("needs_approval", "outside autonomy preconditions: " + ", ".join(failed))
        return decide("allowed", "rung autonomous: preconditions met")

    # --- execution bookkeeping ----------------------------------------------

    def record_execution(self, req: ActionRequest, output: str) -> str:
        execution_id = uuid.uuid4().hex[:10]
        if req.approval_id:
            approvals = self._approvals()
            if req.approval_id in approvals:
                approvals[req.approval_id].status = "used"
                self._save_approvals(approvals)
        self.audit.append("executed", req.actor, req.action,
                          {"execution_id": execution_id, "approval_id": req.approval_id, "plan_hash": req.hash,
                           "plan": req.plan, "reason": req.reason, "output": output,
                           "summary": _plan_line(req.plan)}, self.now())
        return execution_id

    def record_outcome(self, execution_id: str, outcome: Literal["good", "bad", "reverted", "inconclusive"],
                       by: str, note: str) -> Optional[Rung]:
        executed = next((e for e in self.audit.entries()
                         if e["event"] == "executed" and e["data"].get("execution_id") == execution_id), None)
        if executed is None:
            raise KeyError(f"no execution {execution_id}")
        action = executed["action"]
        self.audit.append("outcome", by, action, {"execution_id": execution_id, "outcome": outcome, "reason": note}, self.now())
        if outcome in ("bad", "reverted"):
            return self.demote(action, by, f"{outcome} outcome for execution {execution_id}: {note}")
        return None


def _plan_line(plan: dict) -> str:
    if {"release", "current_revision", "target_revision"} <= plan.keys():
        return f"{plan['release']} {plan['current_revision']} -> {plan['target_revision']}"
    return json.dumps(plan)[:80]
