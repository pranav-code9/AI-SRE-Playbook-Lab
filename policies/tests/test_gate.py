import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from sre_policy import ActionRequest, Gate, Rung, load_policy
from sre_policy.gate import plan_hash
from sre_policy.model import Policy
from sre_policy.readiness import readiness, verify_execution

POLICY = Path(__file__).resolve().parents[1] / "trust-ladder.yaml"
T0 = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)

PLAN = {
    "release": "otel-demo", "namespace": "otel-demo", "current_revision": 3, "target_revision": 2,
    "changes": [{"key": "components.checkout.envOverrides[PAYMENT_TIMEOUT].value", "before": "40ms", "after": "2s"}],
    "chart": {"current": "opentelemetry-demo-lab", "target": "opentelemetry-demo-lab"},
    "resources_changed": ["Deployment/checkout"],
    "command": "helm rollback otel-demo 2 -n otel-demo --wait",
}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)


def policy_with(rung="approve", **overrides) -> Policy:
    data = yaml.safe_load(POLICY.read_text())
    data["actions"]["rollback_release"]["rung"] = rung
    data["actions"]["rollback_release"].update(overrides)
    return Policy.model_validate(data)


@pytest.fixture
def clock():
    return Clock()


def gate(tmp_path, clock, rung="approve", **overrides):
    return Gate(policy_with(rung, **overrides), tmp_path, clock=clock)


def req(plan=PLAN, actor="agent", approval_id=None, confidence=0.9, age=25, anchored=True, chain=True):
    return ActionRequest(action="rollback_release", plan=plan, actor=actor, reason="retry storm after revision 3",
                         approval_id=approval_id, agent_confidence=confidence, change_age_minutes=age,
                         change_anchored=anchored, chain_complete=chain)


def test_lab_policy_is_valid():
    assert load_policy(POLICY).problems() == []


def test_policy_problems_are_reported():
    p = policy_with("autonomous", approvers=["nobody"], rate_limit=None, blast_radius={})
    problems = p.problems()
    assert any("approver role 'nobody'" in x for x in problems)
    assert any("blast radius" in x for x in problems)
    assert any("rate limit" in x for x in problems)


def test_lower_rungs_never_execute(tmp_path, clock):
    assert gate(tmp_path / "a", clock, "observe").evaluate(req()).outcome == "shadow"
    assert gate(tmp_path / "b", clock, "suggest").evaluate(req()).outcome == "suggest_only"
    unknown = ActionRequest(action="restart_everything", plan={}, actor="agent", reason="because I said so")
    assert gate(tmp_path / "c", clock).evaluate(unknown).outcome == "shadow"


def test_approval_flow(tmp_path, clock):
    g = gate(tmp_path, clock)
    assert g.evaluate(req()).outcome == "needs_approval"
    a = g.request_approval(req())
    with pytest.raises(PermissionError):
        g.decide_approval(a.id, "cli:mallory", approve=True)
    g.decide_approval(a.id, "cli:pranav", approve=True)
    d = g.evaluate(req(approval_id=a.id, actor="cli:pranav"))
    assert d.outcome == "allowed" and d.reasons[-1] == "approved by cli:pranav"


def test_requester_cannot_approve_own_request(tmp_path, clock):
    g = gate(tmp_path, clock)
    a = g.request_approval(req(actor="cli:pranav"))
    with pytest.raises(PermissionError, match="own request"):
        g.decide_approval(a.id, "cli:pranav", approve=True)


def test_approval_is_bound_to_the_exact_plan(tmp_path, clock):
    radius = {"namespaces": ["otel-demo"], "releases": ["otel-demo"], "max_revisions_back": 5, "max_changed_values": 10}
    g = gate(tmp_path, clock, blast_radius=radius)
    a = g.request_approval(req())
    g.decide_approval(a.id, "cli:pranav", approve=True)
    moved_on = {**PLAN, "current_revision": 4}  # someone deployed revision 4 meanwhile
    d = g.evaluate(req(plan=moved_on, approval_id=a.id))
    assert d.outcome == "denied"
    assert d.checks["approval_for_this_plan"] is False
    assert plan_hash(PLAN) != plan_hash(moved_on)
    assert plan_hash(PLAN) == plan_hash({**PLAN, "command": "anything"})  # command and reason don't count


def test_approvals_expire(tmp_path, clock):
    g = gate(tmp_path, clock)
    a = g.request_approval(req())
    clock.advance(16)
    with pytest.raises(ValueError, match="expired"):
        g.decide_approval(a.id, "cli:pranav", approve=True)


def test_approvals_are_single_use(tmp_path, clock):
    g = gate(tmp_path, clock, rate_limit={"max": 5, "per_minutes": 30})
    a = g.request_approval(req())
    g.decide_approval(a.id, "cli:pranav", approve=True)
    r = req(approval_id=a.id)
    assert g.evaluate(r).outcome == "allowed"
    g.record_execution(r, "done")
    assert g.evaluate(r).checks["approval_granted"] is False


def test_blast_radius(tmp_path, clock):
    g = gate(tmp_path, clock)
    assert g.evaluate(req(plan={**PLAN, "namespace": "payments"})).checks["namespace_in_scope"] is False
    assert g.evaluate(req(plan={**PLAN, "target_revision": 1})).checks["revisions_back_ok"] is False
    many = {**PLAN, "changes": [{"key": str(i), "before": 1, "after": 2} for i in range(11)]}
    assert g.evaluate(req(plan=many)).outcome == "denied"
    assert g.evaluate(req(plan={**PLAN, "changes": None})).checks["changed_values_ok"] is False


def test_kill_switch(tmp_path, clock, monkeypatch):
    g = gate(tmp_path, clock)
    g.stop("rollback_release", "cli:pranav", "rollbacks misbehaving")
    assert g.evaluate(req()).reasons[-1] == "kill switch is on for this action"
    g.resume("rollback_release", "cli:pranav", "fixed")
    assert g.evaluate(req()).outcome == "needs_approval"
    (tmp_path / "STOP").touch()
    assert g.evaluate(req()).outcome == "denied"
    (tmp_path / "STOP").unlink()
    monkeypatch.setenv("SRE_POLICY_STOP", "1")
    assert g.evaluate(req()).outcome == "denied"


def test_autonomous_preconditions_and_rate_limit(tmp_path, clock):
    g = gate(tmp_path, clock, "autonomous")
    assert g.evaluate(req(confidence=0.7)).outcome == "needs_approval"
    assert g.evaluate(req(age=240)).outcome == "needs_approval"
    r = req()
    assert g.evaluate(r).outcome == "allowed"
    g.record_execution(r, "done")
    assert g.evaluate(r).reasons[-1] == "rate limit: 1 per 30 minutes"
    clock.advance(31)
    assert g.evaluate(r).outcome == "allowed"


def test_bad_outcome_demotes_one_rung(tmp_path, clock):
    g = gate(tmp_path, clock, "autonomous")
    r = req()
    execution = g.record_execution(r, "done")
    assert g.record_outcome(execution, "bad", "verifier", "errors didn't fall") == Rung.APPROVE
    assert g.effective_rung("rollback_release") == Rung.APPROVE
    assert g.evaluate(r).outcome == "denied"  # rate limit still applies
    clock.advance(31)
    assert g.evaluate(r).outcome == "needs_approval"
    g.reset_demotion("rollback_release", "cli:pranav", "reviewed")
    assert g.effective_rung("rollback_release") == Rung.AUTONOMOUS


def test_demotion_floor_is_suggest(tmp_path, clock):
    g = gate(tmp_path, clock, "suggest")
    assert g.demote("rollback_release", "verifier", "x") == Rung.SUGGEST


def test_audit_chain_detects_tampering(tmp_path, clock):
    g = gate(tmp_path, clock)
    g.evaluate(req())
    g.request_approval(req())
    assert g.audit.verify() == []
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    lines[0] = lines[0].replace("needs_approval", "allowed")
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
    problems = g.audit.verify()
    assert any("contents changed" in p for p in problems)


def test_readiness_counts_the_track_record(tmp_path, clock):
    g = gate(tmp_path, clock, "suggest")
    for verdict in ["agree"] * 9 + ["disagree"]:
        g.audit.append("suggestion_reviewed", "cli:pranav", "rollback_release", {"verdict": verdict})
    r = readiness(g, "rollback_release", eval_pass_rate=0.97)
    assert all(c.ok for c in r["approve"])
    assert {c.name: c.ok for c in r["autonomous"]} == {
        "min_approved_executions": False, "max_bad_outcomes": True, "min_eval_pass_rate": True}


def test_verify_records_outcomes(tmp_path, clock):
    g = gate(tmp_path, clock, "autonomous")
    execution = g.record_execution(req(), "done")
    assert verify_execution(g, execution, lambda *a: 0.3) == "pending"
    clock.advance(11)
    def measure(service, metric, start, end):
        return 0.34 if end <= T0 else 0.01   # steady before, recovered after

    assert verify_execution(g, execution, measure) == "good"
    execution2 = g.record_execution(req(), "done")
    clock.advance(11)
    assert verify_execution(g, execution2, lambda *a: 0.3) == "bad"
    assert g.effective_rung("rollback_release") == Rung.APPROVE


def test_env_kill_switch_is_not_left_on():
    assert os.environ.get("SRE_POLICY_STOP") != "1"


def test_a_rollback_that_changes_the_chart_is_not_routine(tmp_path, clock):
    g = gate(tmp_path, clock)
    moved = {**PLAN, "chart": {"current": "opentelemetry-demo-2", "target": "opentelemetry-demo-1"}}
    d = g.evaluate(req(plan=moved))
    assert d.outcome == "denied" and d.checks["chart_unchanged"] is False


def test_unknown_or_large_resource_changes_are_outside_the_blast_radius(tmp_path, clock):
    g = gate(tmp_path, clock)
    assert g.evaluate(req(plan={**PLAN, "resources_changed": None})).checks["resources_changed_ok"] is False
    many = {**PLAN, "resources_changed": [f"Deployment/s{i}" for i in range(4)]}
    assert g.evaluate(req(plan=many)).outcome == "denied"


def test_confidence_alone_never_earns_autonomy(tmp_path, clock):
    g = gate(tmp_path, clock, "autonomous")
    assert g.evaluate(req(confidence=0.99, anchored=False)).outcome == "needs_approval"
    assert g.evaluate(req(confidence=0.99, chain=False)).outcome == "needs_approval"
    assert g.evaluate(req(confidence=0.99)).outcome == "allowed"


def test_recovery_that_started_before_the_action_is_not_credited(tmp_path, clock):
    g = gate(tmp_path, clock, "autonomous")
    execution = g.record_execution(req(), "done")
    clock.advance(11)

    def measure(service, metric, start, end):
        if end <= T0 - timedelta(minutes=5):
            return 0.34          # early in the window before the action
        if end <= T0:
            return 0.15          # already falling on its own
        return 0.01

    assert verify_execution(g, execution, measure) == "inconclusive"
    assert g.effective_rung("rollback_release") == Rung.AUTONOMOUS   # not demoted, not credited
